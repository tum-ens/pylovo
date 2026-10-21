"""Queries of the external MV inputs: OSM power features and the eGon-data HV/MV substations.

Reads HV/MV substation candidates and their tags from the OSM schema of InfDB (``INFDB_OSM_SCHEMA``), the
eGon-data HV/MV substations (``pylovo.egon_*`` if populated, else the copies in ``INFDB_OPENDATA_SCHEMA``). The rules
that turn these rows into MV inputs are pure functions of :mod:`pylovo.mv_grid.substations`.

Every method takes its region or PLZ list explicitly, returns a DataFrame in ``TARGET_EPSG`` coordinates, is
read-only and never commits.
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd
from psycopg2 import sql

from pylovo.config_loader import INFDB_OPENDATA_SCHEMA, INFDB_OSM_SCHEMA, TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin
from pylovo.database.mv_graph_mixin import SRID_SQL

#: Columns of :meth:`MvSourceMixin.osm_substation_candidates` (= ``substations.CANDIDATE_COLUMNS``).
OSM_CANDIDATE_COLUMNS: tuple[str, ...] = ("source_id", "voltage", "substation_tag", "frequency", "operator", "name",
                                          "railway", "x", "y", "area_m2", "footprint_wkb", "in_plant", "plant")
#: Columns of :meth:`MvSourceMixin.egon_substations`.
EGON_SUBSTATION_COLUMNS: tuple[str, ...] = ("bus_id", "voltage", "osm_id", "x", "y")

#: Columns of :meth:`MvSourceMixin.osm_power_objects`.
OSM_OBJECT_COLUMNS: tuple[str, ...] = ("source_id", "kind", "tags", "x", "y", "area_m2")
#: Columns of :meth:`MvSourceMixin.osm_landuse`.
OSM_LANDUSE_COLUMNS: tuple[str, ...] = ("osm_type", "geom_wkb")

#: The eGon-data table of the HV/MV substations (their bus ids are the 110 kV buses of eTraGo).
EGON_SUBSTATION_TABLE: str = "egon_hvmv_substation"
#: Margin (m) of the box of the GiST prefilter of the OSM queries (:func:`_search_box`) beyond the reach of their exact
#: test: it covers the difference between a transformed box and the transformed region, so results stay exact.
SEARCH_BOX_MARGIN_M: float = 1000.0

# power=substation features (points, ways, relations) of the OSM schema with their tags, inside the search buffer
# of the planning region. The tags join uses (geom_type, osm_id): node, way and relation ids can collide. Every OSM
# layer is first filtered in its own SRID (``geom && box``, GiST index) by the search box of the region (or of the
# candidate for the plants within 100 m), then tested exactly in TARGET_EPSG.
_OSM_CANDIDATES_SQL = sql.SQL("""
WITH cand AS (
    SELECT 'N'::char(1) AS gt, p.osm_id AS tag_id, 'node/' || p.osm_id AS source_id,
           p.name AS f_name, p.operator AS f_operator, p.geom AS pt_src, NULL::geometry AS fp_src
    FROM {osm}.infrastructure_point p
    WHERE p.osm_type = 'power' AND p.osm_subtype = 'substation'
      AND p.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(point_srid)s)
    UNION ALL
    SELECT CASE WHEN g.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(g.osm_id),
           CASE WHEN g.osm_id < 0 THEN 'relation/' || -g.osm_id ELSE 'way/' || g.osm_id END,
           g.name, g.operator, ST_PointOnSurface(g.geom), g.geom
    FROM {osm}.infrastructure_polygon g
    WHERE g.osm_type = 'power' AND g.osm_subtype = 'substation'
      AND g.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(polygon_srid)s)
),
pts AS (
    SELECT c.*, ST_Transform(c.pt_src, {epsg}) AS pt, ST_Multi(ST_Transform(c.fp_src, {epsg})) AS fp
    FROM cand c
)
SELECT c.source_id, t.tags->>'voltage' AS voltage, t.tags->>'substation' AS substation_tag,
       t.tags->>'frequency' AS frequency, COALESCE(t.tags->>'operator', c.f_operator) AS operator,
       COALESCE(t.tags->>'name', c.f_name) AS name, t.tags->>'railway' AS railway,
       ST_X(c.pt) AS x, ST_Y(c.pt) AS y, COALESCE(ST_Area(c.fp), 0) AS area_m2,
       ST_AsBinary(c.fp) AS footprint_wkb,
       EXISTS (SELECT 1 FROM {osm}.infrastructure_polygon pl
               WHERE pl.osm_type = 'power' AND pl.osm_subtype IN ('plant', 'generator')
                 AND ST_Intersects(pl.geom, c.pt_src)) AS in_plant,
       (SELECT string_agg(DISTINCT lower(concat_ws(':', COALESCE(pt.tags->>'plant:source',
                                                                 pt.tags->>'generator:source', ''),
                                                    COALESCE(pt.tags->>'name', pl.name, ''))), '|')
        FROM {osm}.infrastructure_polygon pl
        LEFT JOIN {osm}.tags pt ON pt.geom_type = CASE WHEN pl.osm_id < 0 THEN 'R' ELSE 'W' END
                               AND pt.osm_id = abs(pl.osm_id)
        WHERE pl.osm_type = 'power' AND pl.osm_subtype IN ('plant', 'generator')
          AND pl.geom && ST_Transform(ST_Expand(c.pt, 100.0 + %(margin_m)s), %(polygon_srid)s)
          AND ST_DWithin(ST_Transform(pl.geom, {epsg}), c.pt, 100)) AS plant
FROM pts c
LEFT JOIN {osm}.tags t ON t.geom_type = c.gt AND t.osm_id = c.tag_id
WHERE ST_DWithin(c.pt, ST_GeomFromWKB(%(region)s, {epsg}), %(buffer_m)s)
ORDER BY c.source_id;
""")

# power=substation and power=transformer features, power=pole nodes, and transformer-tower buildings inside the
# planning region (plus 1 km for the HV/MV sites at its edge), with their tags (pylovo.mv_grid.osm_coverage tells the
# MV/LV stations from the sites of a higher level). Prefiltered in the source SRIDs as the candidates.
_OSM_OBJECTS_SQL = sql.SQL("""
WITH obj AS (
    SELECT 'N'::char(1) AS gt, p.osm_id AS tag_id, 'node/' || p.osm_id AS source_id, p.osm_subtype AS kind,
           p.geom AS src, 0.0::double precision AS area_src
    FROM {osm}.infrastructure_point p
    WHERE p.osm_type = 'power' AND p.osm_subtype IN ('substation', 'transformer', 'pole')
      AND p.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(point_srid)s)
    UNION ALL
    SELECT CASE WHEN g.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(g.osm_id),
           CASE WHEN g.osm_id < 0 THEN 'relation/' || -g.osm_id ELSE 'way/' || g.osm_id END, g.osm_subtype,
           ST_PointOnSurface(g.geom), ST_Area(ST_Transform(g.geom, {epsg}))
    FROM {osm}.infrastructure_polygon g
    WHERE g.osm_type = 'power' AND g.osm_subtype IN ('substation', 'transformer')
      AND g.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(polygon_srid)s)
    UNION ALL
    SELECT CASE WHEN b.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(b.osm_id),
           CASE WHEN b.osm_id < 0 THEN 'relation/' || -b.osm_id ELSE 'way/' || b.osm_id END, 'transformer_tower',
           ST_PointOnSurface(b.geom), ST_Area(ST_Transform(b.geom, {epsg}))
    FROM {osm}.building_polygon b
    WHERE b.osm_subtype = 'transformer_tower'
      AND b.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(building_srid)s)
),
pts AS (SELECT o.*, ST_Transform(o.src, {epsg}) AS pt FROM obj o)
SELECT o.source_id, o.kind, t.tags::text AS tags, ST_X(o.pt) AS x, ST_Y(o.pt) AS y, o.area_src AS area_m2
FROM pts o
LEFT JOIN {osm}.tags t ON t.geom_type = o.gt AND t.osm_id = o.tag_id
WHERE ST_DWithin(o.pt, ST_GeomFromWKB(%(region)s, {epsg}), 1000.0)
ORDER BY o.source_id;
""")

# OSM land-use polygons clipped to the planning region (prefiltered in the source SRID as the candidates).
_OSM_LANDUSE_SQL = sql.SQL("""
WITH r AS (SELECT ST_GeomFromWKB(%(region)s, {epsg}) AS g)
SELECT l.osm_type, ST_AsBinary(ST_Intersection(ST_MakeValid(ST_Transform(l.geom, {epsg})), r.g))
FROM {osm}.landuse_polygon l, r
WHERE l.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(landuse_srid)s)
  AND ST_Intersects(ST_Transform(l.geom, {epsg}), r.g)
ORDER BY l.osm_id;
""")

_EGON_SUBSTATIONS_SQL = sql.SQL("""
SELECT s.bus_id::bigint AS bus_id, NULLIF(btrim(s.voltage::text), 'NA') AS voltage,
       NULLIF(btrim(s.osm_id::text), 'NA') AS osm_id,
       ST_X(ST_Transform(s.geom, {epsg})) AS x, ST_Y(ST_Transform(s.geom, {epsg})) AS y
FROM {table} s
WHERE ST_DWithin(ST_Transform(s.geom, {epsg}), ST_GeomFromWKB(%(region)s, {epsg}), %(buffer_m)s)
ORDER BY s.bus_id;
""")


def _stage_error(message: str) -> Exception:
    """An :class:`~pylovo.mv_grid.parameters.MvStageError` (imported on use: LV runs never load the MV stage)."""
    from pylovo.mv_grid.parameters import MvStageError

    return MvStageError(message)


def _schema(name: str | None, key: str) -> sql.Identifier:
    if not name:
        raise _stage_error(f"{key} is not set: the MV stage reads the InfDB schemas in the pylovo database "
                           "(USE_INFDB=True)")
    return sql.Identifier(name)


def _regclass_text(schema: str, table: str) -> str:
    """``"schema"."table"`` for ``to_regclass`` (quoted, so mixed case and odd names stay exact)."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in (schema, table))


def _wkb(value: Any) -> bytes | None:
    """``bytea`` values come back as ``memoryview``; shapely needs ``bytes``."""
    if value is None:
        return None
    return bytes(value)


def _na(value: Any) -> Any:
    """eGon-data text columns write a missing value as the string ``NA``."""
    if isinstance(value, str) and value.strip() in ("", "NA"):
        return None
    return value


def _search_box(region_wkb: bytes, reach_m: float) -> bytes:
    """The prefilter box of an OSM query (WKB, ``TARGET_EPSG``): the bounding box of the region widened by the reach of
    the exact test and :data:`SEARCH_BOX_MARGIN_M`, with a vertex every 1 km, so that its outline still bounds the
    reach of the region after the transformation into the SRID of an OSM layer."""
    import shapely

    x0, y0, x1, y1 = shapely.from_wkb(region_wkb).bounds
    pad = float(reach_m) + SEARCH_BOX_MARGIN_M
    return shapely.to_wkb(shapely.segmentize(shapely.box(x0 - pad, y0 - pad, x1 + pad, y1 + pad), 1000.0))


def _frame(rows: list[tuple], columns: Sequence[str], floats: Sequence[str] = (), ints: Sequence[str] = (),
           bools: Sequence[str] = ()) -> pd.DataFrame:
    frame = pd.DataFrame([tuple(row) for row in rows], columns=list(columns))
    for column in floats:
        frame[column] = frame[column].astype(float)
    for column in ints:
        frame[column] = frame[column].astype("int64")
    for column in bools:
        frame[column] = frame[column].astype(bool)
    return frame


class MvSourceMixin(BaseMixin):
    """Read the OSM and eGon-data inputs of the MV stage."""

    def _osm_srid(self, table: str) -> int:
        """The SRID of the geometries of an OSM layer (``TARGET_EPSG`` without geometries), for the prefilters."""
        self.cur.execute(SRID_SQL.format(table=sql.Identifier(INFDB_OSM_SCHEMA, table)))
        row = self.cur.fetchone()
        return int(row[0]) if row and row[0] else int(TARGET_EPSG)

    # ------------------------------------------------------------------ OSM
    def osm_substation_candidates(self, region_wkb: bytes, buffer_m: float) -> pd.DataFrame:
        """The ``power=substation`` features of the OSM schema within ``buffer_m`` of the planning region.

        One row per OSM node, way or relation (multipolygon), with the tags the filter of
        :func:`~pylovo.mv_grid.substations.classify_candidate` reads.

        Args:
            region_wkb: The planning region (WKB, ``TARGET_EPSG``).
            buffer_m: The search buffer around it (``region_buffer_m``).

        Returns:
            Columns of :data:`OSM_CANDIDATE_COLUMNS`, sorted by ``source_id`` (``node/<id>``, ``way/<id>``,
            ``relation/<id>``): the raw tags ``voltage``, ``substation_tag``, ``frequency``, ``operator``,
            ``name`` (falling back to the feature columns), ``railway``; the point (point-on-surface of a
            polygon) ``x``, ``y``; the footprint ``area_m2`` (0 for nodes) and ``footprint_wkb`` (MultiPolygon WKB
            or None); ``in_plant``: the point lies inside a ``power=plant`` or ``power=generator`` polygon;
            ``plant``: the ``plant:source`` (or ``generator:source``) and name of the plant and generator polygons
            within 100 m, lower case, ``source:name`` joined with ``|`` (a substation inside an untagged plant
            polygon next to a named generator takes the generator's description; None without such polygons).

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        query = _OSM_CANDIDATES_SQL.format(osm=_schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA"),
                                           epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb), "buffer_m": float(buffer_m),
                                 "box": _search_box(_wkb(region_wkb), buffer_m), "margin_m": SEARCH_BOX_MARGIN_M,
                                 "point_srid": self._osm_srid("infrastructure_point"),
                                 "polygon_srid": self._osm_srid("infrastructure_polygon")})
        rows = [(*row[:10], _wkb(row[10]), row[11], row[12] if len(row) > 12 else None) for row in self.cur.fetchall()]
        return _frame(rows, OSM_CANDIDATE_COLUMNS, floats=("x", "y", "area_m2"), bools=("in_plant",))

    def municipality_areas(self, ags: Sequence[str], region_wkb: bytes) -> dict[str, float]:
        """The area (km²) of each municipality inside the planning region (``INFDB_OPENDATA_SCHEMA.scope``).

        Args:
            ags: Municipality keys (AGS, text).
            region_wkb: The planning region (WKB, ``TARGET_EPSG``).

        Returns:
            ``{ags: km²}`` for the keys found in ``scope``.
        """
        if not len(ags):
            return {}
        query = sql.SQL("""SELECT s.ags::text, ST_Area(ST_Intersection(ST_Transform(s.geom, {epsg}),
                                                                    ST_GeomFromWKB(%(region)s, {epsg}))) / 1e6
                           FROM {schema}.scope s WHERE s.ags::text = ANY(%(ags)s)""").format(
            schema=_schema(INFDB_OPENDATA_SCHEMA, "INFDB_OPENDATA_SCHEMA"), epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb), "ags": [str(a) for a in ags]})
        return {str(a): float(km2) for a, km2 in self.cur.fetchall() if km2 is not None}

    def osm_power_objects(self, region_wkb: bytes) -> pd.DataFrame:
        """The ``power=substation``, ``power=transformer`` and ``power=pole`` features and the ``transformer_tower``
        buildings of the OSM schema within 1 km of the planning region (the inputs of
        :func:`pylovo.mv_grid.osm_coverage.station_objects`).

        Returns:
            Columns of :data:`OSM_OBJECT_COLUMNS`: ``source_id``, ``kind`` (``substation``, ``transformer``,
            ``pole`` or ``transformer_tower``), ``tags`` (JSON text, None without tags), the point (point-on-surface
            of a polygon) ``x``, ``y`` and the footprint ``area_m2`` (0 for nodes).

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        query = _OSM_OBJECTS_SQL.format(osm=_schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA"),
                                        epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb), "box": _search_box(_wkb(region_wkb), 1000.0),
                                 "point_srid": self._osm_srid("infrastructure_point"),
                                 "polygon_srid": self._osm_srid("infrastructure_polygon"),
                                 "building_srid": self._osm_srid("building_polygon")})
        return _frame(self.cur.fetchall(), OSM_OBJECT_COLUMNS, floats=("x", "y", "area_m2"))

    def osm_landuse(self, region_wkb: bytes) -> pd.DataFrame:
        """The OSM ``landuse`` polygons clipped to the planning region: ``osm_type`` and ``geom_wkb`` (WKB,
        ``TARGET_EPSG``).

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        query = _OSM_LANDUSE_SQL.format(osm=_schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA"),
                                        epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb), "box": _search_box(_wkb(region_wkb), 0.0),
                                 "landuse_srid": self._osm_srid("landuse_polygon")})
        return _frame([(t, _wkb(g)) for t, g in self.cur.fetchall()], OSM_LANDUSE_COLUMNS)

    # ------------------------------------------------------------------ eGon
    def _egon_table(self, table: str) -> tuple[str, sql.Identifier] | None:
        """The schema and qualified name of an eGon-data table: ``pylovo`` if filled, else the opendata schema.

        Returns:
            ``(schema, identifier)`` of the first existing, non-empty table, or None.
        """
        for schema in ("pylovo", INFDB_OPENDATA_SCHEMA):
            if not schema:
                continue
            # a probe that fails would abort the caller's transaction: check existence and privilege first
            self.cur.execute("SELECT c IS NOT NULL AND has_table_privilege(c, 'SELECT') "
                             "FROM (SELECT to_regclass(%s) AS c) AS r", (_regclass_text(schema, table),))
            if not self.cur.fetchone()[0]:
                continue
            identifier = sql.Identifier(schema, table)
            self.cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(identifier))
            if self.cur.fetchone()[0]:
                return schema, identifier
        return None

    def _egon_table_or_raise(self, table: str) -> sql.Identifier:
        found = self._egon_table(table)
        if found is None:
            raise _stage_error(f"no eGon-data table {table}: neither populated pylovo.{table} nor "
                               f"{INFDB_OPENDATA_SCHEMA or '<INFDB_OPENDATA_SCHEMA>'}.{table} exists with rows")
        return found[1]

    def egon_available(self) -> tuple[bool, str]:
        """Whether the eGon-data HV/MV substations can be read (for the eTraGo bus of each site), and from which schema.

        The table ``pylovo.egon_hvmv_substation`` is used if it exists and holds rows, else the copy in
        ``INFDB_OPENDATA_SCHEMA``.

        Returns:
            ``(True, schema)`` (``"pylovo"`` or the opendata schema), or ``(False, reason)``.
        """
        found = self._egon_table(EGON_SUBSTATION_TABLE)
        if found is None:
            where = f"pylovo or {INFDB_OPENDATA_SCHEMA}" if INFDB_OPENDATA_SCHEMA else "pylovo"
            return False, (f"no rows in {EGON_SUBSTATION_TABLE} ({where}); populate the eGon-data table or provide "
                           "the eGon-data copy in the opendata schema")
        return True, found[0]

    def egon_substations(self, region_wkb: bytes, buffer_m: float) -> pd.DataFrame:
        """The eGon-data HV/MV substations within ``buffer_m`` of the planning region.

        Returns:
            Columns of :data:`EGON_SUBSTATION_COLUMNS` sorted by ``bus_id``; ``voltage`` and ``osm_id``
            (``w<way id>``, ``n<node id>``) are None where eGon-data writes ``NA``.

        Raises:
            MvStageError: If no eGon-data substation table exists with rows.
        """
        query = _EGON_SUBSTATIONS_SQL.format(table=self._egon_table_or_raise(EGON_SUBSTATION_TABLE),
                                             epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb), "buffer_m": float(buffer_m)})
        rows = [(bus_id, _na(voltage), _na(osm_id), x, y) for bus_id, voltage, osm_id, x, y in self.cur.fetchall()]
        return _frame(rows, EGON_SUBSTATION_COLUMNS, floats=("x", "y"), ints=("bus_id",))

