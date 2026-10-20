"""Queries of the external MV inputs: OSM power features, eGon-data tables and the MaStR units.

Reads HV/MV substation candidates and their tags from the OSM schema of InfDB (``INFDB_OSM_SCHEMA``), the
ratings of the transformers mapped inside their footprints, the eGon-data HV/MV substations and MV grid
districts (``pylovo.egon_*`` if populated, else the copies in ``INFDB_OPENDATA_SCHEMA``), and
the generating units of ``pylovo.mastr_units`` and ``pylovo.mastr_postcode_aggregates``
(``pylovo-import mastr``). The rules that turn these rows into MV inputs are pure functions of
:mod:`pylovo.mv_grid.substations` and :mod:`pylovo.mv_grid.inputs`.

Every method takes its region or PLZ list explicitly, returns a DataFrame in ``TARGET_EPSG`` coordinates, is
read-only and never commits.
"""

from __future__ import annotations

from typing import Any, Sequence

import pandas as pd
from psycopg2 import sql

from pylovo.config_loader import INFDB_OPENDATA_SCHEMA, INFDB_OSM_SCHEMA, TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin

#: Columns of :meth:`MvSourceMixin.osm_substation_candidates` (= ``substations.CANDIDATE_COLUMNS``).
OSM_CANDIDATE_COLUMNS: tuple[str, ...] = ("source_id", "voltage", "substation_tag", "frequency", "operator", "name",
                                          "railway", "x", "y", "area_m2", "footprint_wkb", "in_plant", "plant")
#: Columns of :meth:`MvSourceMixin.osm_transformer_ratings`.
OSM_RATING_COLUMNS: tuple[str, ...] = ("site_index", "source_id", "voltage", "rating")
#: Columns of :meth:`MvSourceMixin.egon_substations`.
EGON_SUBSTATION_COLUMNS: tuple[str, ...] = ("bus_id", "voltage", "osm_id", "x", "y")
#: Columns of :meth:`MvSourceMixin.egon_districts`.
EGON_DISTRICT_COLUMNS: tuple[str, ...] = ("bus_id", "geom_wkb")
#: Columns of :meth:`MvSourceMixin.mastr_units_in_region`.
MASTR_UNIT_COLUMNS: tuple[str, ...] = ("mastr_id", "location_id", "technology", "solar_kind", "net_power_kw",
                                       "voltage_level", "x", "y")
#: Columns of :meth:`MvSourceMixin.mastr_postcode_aggregates`.
MASTR_AGGREGATE_COLUMNS: tuple[str, ...] = ("postcode", "plz", "technology", "n_units", "net_power_kw",
                                            "area_ratio")

#: Columns of :meth:`MvSourceMixin.osm_power_objects`.
OSM_OBJECT_COLUMNS: tuple[str, ...] = ("source_id", "kind", "tags", "x", "y", "area_m2")
#: Columns of :meth:`MvSourceMixin.osm_landuse`.
OSM_LANDUSE_COLUMNS: tuple[str, ...] = ("osm_type", "geom_wkb")

#: The eGon-data tables and the schemas they are read from, in this order (the first non-empty one wins).
EGON_TABLES: tuple[str, str] = ("egon_hvmv_substation", "egon_mv_grid_district")

# power=substation features (points, ways, relations) of the OSM schema with their tags, inside the search buffer
# of the planning region. The tags join uses (geom_type, osm_id): node, way and relation ids can collide. The
# plant test and the region test use the source geometries so that the GiST indexes of the OSM layers are used.
_OSM_CANDIDATES_SQL = sql.SQL("""
WITH cand AS (
    SELECT 'N'::char(1) AS gt, p.osm_id AS tag_id, 'node/' || p.osm_id AS source_id,
           p.name AS f_name, p.operator AS f_operator, p.geom AS pt_src, NULL::geometry AS fp_src
    FROM {osm}.infrastructure_point p
    WHERE p.osm_type = 'power' AND p.osm_subtype = 'substation'
    UNION ALL
    SELECT CASE WHEN g.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(g.osm_id),
           CASE WHEN g.osm_id < 0 THEN 'relation/' || -g.osm_id ELSE 'way/' || g.osm_id END,
           g.name, g.operator, ST_PointOnSurface(g.geom), g.geom
    FROM {osm}.infrastructure_polygon g
    WHERE g.osm_type = 'power' AND g.osm_subtype = 'substation'
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
          AND ST_DWithin(ST_Transform(pl.geom, {epsg}), c.pt, 100)) AS plant
FROM pts c
LEFT JOIN {osm}.tags t ON t.geom_type = c.gt AND t.osm_id = c.tag_id
WHERE ST_DWithin(c.pt, ST_GeomFromWKB(%(region)s, {epsg}), %(buffer_m)s)
ORDER BY c.source_id;
""")

# power=transformer features (points, ways, relations) within margin_m of each given footprint, with their tags.
_OSM_RATINGS_SQL = sql.SQL("""
WITH fp AS (
    SELECT (u.ord - 1)::integer AS site_index, ST_GeomFromWKB(u.wkb, {epsg}) AS geom
    FROM unnest(%(footprints)s::bytea[]) WITH ORDINALITY AS u(wkb, ord)
    WHERE u.wkb IS NOT NULL
),
tr AS (
    SELECT 'N'::char(1) AS gt, p.osm_id AS tag_id, 'node/' || p.osm_id AS source_id, p.geom
    FROM {osm}.infrastructure_point p
    WHERE p.osm_type = 'power' AND p.osm_subtype = 'transformer'
    UNION ALL
    SELECT CASE WHEN g.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(g.osm_id),
           CASE WHEN g.osm_id < 0 THEN 'relation/' || -g.osm_id ELSE 'way/' || g.osm_id END, g.geom
    FROM {osm}.infrastructure_polygon g
    WHERE g.osm_type = 'power' AND g.osm_subtype = 'transformer'
)
SELECT f.site_index, tr.source_id, t.tags->>'voltage' AS voltage, t.tags->>'rating' AS rating
FROM fp f
JOIN tr ON ST_DWithin(ST_Transform(tr.geom, {epsg}), f.geom, %(margin_m)s)
LEFT JOIN {osm}.tags t ON t.geom_type = tr.gt AND t.osm_id = tr.tag_id
ORDER BY f.site_index, tr.source_id;
""")

# power=substation and power=transformer features, power=pole nodes, and transformer-tower buildings inside the
# planning region (plus 1 km for the HV/MV sites at its edge), with their tags (pylovo.mv_grid.osm_coverage tells the
# MV/LV stations from the sites of a higher level).
_OSM_OBJECTS_SQL = sql.SQL("""
WITH obj AS (
    SELECT 'N'::char(1) AS gt, p.osm_id AS tag_id, 'node/' || p.osm_id AS source_id, p.osm_subtype AS kind,
           p.geom AS src, 0.0::double precision AS area_src
    FROM {osm}.infrastructure_point p
    WHERE p.osm_type = 'power' AND p.osm_subtype IN ('substation', 'transformer', 'pole')
    UNION ALL
    SELECT CASE WHEN g.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(g.osm_id),
           CASE WHEN g.osm_id < 0 THEN 'relation/' || -g.osm_id ELSE 'way/' || g.osm_id END, g.osm_subtype,
           ST_PointOnSurface(g.geom), ST_Area(ST_Transform(g.geom, {epsg}))
    FROM {osm}.infrastructure_polygon g
    WHERE g.osm_type = 'power' AND g.osm_subtype IN ('substation', 'transformer')
    UNION ALL
    SELECT CASE WHEN b.osm_id < 0 THEN 'R' ELSE 'W' END::char(1), abs(b.osm_id),
           CASE WHEN b.osm_id < 0 THEN 'relation/' || -b.osm_id ELSE 'way/' || b.osm_id END, 'transformer_tower',
           ST_PointOnSurface(b.geom), ST_Area(ST_Transform(b.geom, {epsg}))
    FROM {osm}.building_polygon b
    WHERE b.osm_subtype = 'transformer_tower'
),
pts AS (SELECT o.*, ST_Transform(o.src, {epsg}) AS pt FROM obj o)
SELECT o.source_id, o.kind, t.tags::text AS tags, ST_X(o.pt) AS x, ST_Y(o.pt) AS y, o.area_src AS area_m2
FROM pts o
LEFT JOIN {osm}.tags t ON t.geom_type = o.gt AND t.osm_id = o.tag_id
WHERE ST_DWithin(o.pt, ST_GeomFromWKB(%(region)s, {epsg}), 1000.0)
ORDER BY o.source_id;
""")

# OSM land-use polygons clipped to the planning region.
_OSM_LANDUSE_SQL = sql.SQL("""
WITH r AS (SELECT ST_GeomFromWKB(%(region)s, {epsg}) AS g)
SELECT l.osm_type, ST_AsBinary(ST_Intersection(ST_MakeValid(ST_Transform(l.geom, {epsg})), r.g))
FROM {osm}.landuse_polygon l, r
WHERE ST_Intersects(ST_Transform(l.geom, {epsg}), r.g)
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

_EGON_DISTRICTS_SQL = sql.SQL("""
SELECT d.bus_id::bigint AS bus_id, ST_AsBinary(ST_Multi(ST_Transform(d.geom, {epsg}))) AS geom_wkb
FROM {table} d
WHERE ST_Intersects(ST_Transform(d.geom, {epsg}), ST_GeomFromWKB(%(region)s, {epsg}))
ORDER BY d.bus_id;
""")

# Units in operation with coordinates inside the planning region; storage takes part in no MV case.
_MASTR_UNITS_SQL = sql.SQL("""
SELECT u.mastr_id, u.location_id, u.technology, u.solar_kind, u.net_power_kw, u.voltage_level,
       ST_X(u.geom) AS x, ST_Y(u.geom) AS y
FROM pylovo.mastr_units u
WHERE ST_Within(u.geom, ST_GeomFromWKB(%(region)s, {epsg}))
  AND u.net_power_kw > 0 AND u.technology IS DISTINCT FROM 'storage'
ORDER BY u.mastr_id;
""")

# Units without coordinates per postcode. The aggregates cover the whole postcode; area_ratio is the share of the
# postcode area inside pylovo.postcode (clipped at the border of the imported region; 1 when unknown).
_MASTR_AGGREGATES_SQL = sql.SQL("""
SELECT a.postcode, a.technology, a.n_units, a.net_power_kw,
       COALESCE(LEAST(1.0, ST_Area(p.geom) / NULLIF(p.qkm * 1e6, 0)), 1.0) AS area_ratio
FROM pylovo.mastr_postcode_aggregates a
LEFT JOIN pylovo.postcode p ON lpad(p.plz::text, 5, '0') = a.postcode
WHERE a.postcode = ANY(%(plz_text)s) AND a.technology IS DISTINCT FROM 'storage'
ORDER BY a.postcode, a.technology;
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
    """Read OSM, eGon and MaStR inputs of the MV stage."""

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
        self.cur.execute(query, {"region": _wkb(region_wkb), "buffer_m": float(buffer_m)})
        rows = [(*row[:10], _wkb(row[10]), row[11], row[12] if len(row) > 12 else None) for row in self.cur.fetchall()]
        return _frame(rows, OSM_CANDIDATE_COLUMNS, floats=("x", "y", "area_m2"), bools=("in_plant",))

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
        self.cur.execute(query, {"region": _wkb(region_wkb)})
        return _frame(self.cur.fetchall(), OSM_OBJECT_COLUMNS, floats=("x", "y", "area_m2"))

    def osm_landuse(self, region_wkb: bytes) -> pd.DataFrame:
        """The OSM ``landuse`` polygons clipped to the planning region: ``osm_type`` and ``geom_wkb`` (WKB,
        ``TARGET_EPSG``).

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        query = _OSM_LANDUSE_SQL.format(osm=_schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA"),
                                        epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb)})
        return _frame([(t, _wkb(g)) for t, g in self.cur.fetchall()], OSM_LANDUSE_COLUMNS)

    def osm_transformer_ratings(self, footprints_wkb: Sequence[bytes | None], margin_m: float = 10.0
                                ) -> pd.DataFrame:
        """The ``power=transformer`` features within ``margin_m`` of each footprint, with their voltage and rating.

        Args:
            footprints_wkb: Footprints (WKB, ``TARGET_EPSG``) of the sites; None for a site without one.
            margin_m: Distance around a footprint in which a transformer counts (10 m).

        Returns:
            Columns of :data:`OSM_RATING_COLUMNS`: ``site_index`` (position in ``footprints_wkb``), the transformer's
            ``source_id`` and its raw ``voltage`` and ``rating`` tags (see
            :func:`~pylovo.mv_grid.substations.site_ratings`).

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        footprints = [_wkb(fp) for fp in footprints_wkb]
        if not any(fp is not None for fp in footprints):
            return _frame([], OSM_RATING_COLUMNS, ints=("site_index",))
        query = _OSM_RATINGS_SQL.format(osm=_schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA"),
                                        epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"footprints": footprints, "margin_m": float(margin_m)})
        return _frame(self.cur.fetchall(), OSM_RATING_COLUMNS, ints=("site_index",))

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
                               f"{INFDB_OPENDATA_SCHEMA or '<INFDB_OPENDATA_SCHEMA>'}.{table} exists with rows; "
                               "use MV_SUBSTATIONS=osm and MV_SUPPLY_AREAS=routed")
        return found[1]

    def egon_available(self) -> tuple[bool, str]:
        """Whether the eGon-data substations and MV grid districts can be read, and from which schema.

        The tables ``pylovo.egon_hvmv_substation`` and ``pylovo.egon_mv_grid_district`` are used if they exist and
        hold rows, else the copies in ``INFDB_OPENDATA_SCHEMA``.

        Returns:
            ``(True, schema)`` (``"pylovo"``, ``"opendata"``, or ``"<a> (substations), <b> (districts)"`` when they
            differ), or ``(False, reason)``.
        """
        found = {table: self._egon_table(table) for table in EGON_TABLES}
        missing = [table for table, hit in found.items() if hit is None]
        if missing:
            where = f"pylovo or {INFDB_OPENDATA_SCHEMA}" if INFDB_OPENDATA_SCHEMA else "pylovo"
            return False, (f"no rows in {', '.join(missing)} ({where}); populate the eGon-data tables or provide "
                           "the eGon-data copies in the opendata schema")
        schemas = [found[table][0] for table in EGON_TABLES]
        if schemas[0] == schemas[1]:
            return True, schemas[0]
        return True, f"{schemas[0]} (substations), {schemas[1]} (districts)"

    def egon_substations(self, region_wkb: bytes, buffer_m: float) -> pd.DataFrame:
        """The eGon-data HV/MV substations within ``buffer_m`` of the planning region.

        Returns:
            Columns of :data:`EGON_SUBSTATION_COLUMNS` sorted by ``bus_id``; ``voltage`` and ``osm_id``
            (``w<way id>``, ``n<node id>``) are None where eGon-data writes ``NA``.

        Raises:
            MvStageError: If no eGon-data substation table exists with rows.
        """
        query = _EGON_SUBSTATIONS_SQL.format(table=self._egon_table_or_raise("egon_hvmv_substation"),
                                             epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb), "buffer_m": float(buffer_m)})
        rows = [(bus_id, _na(voltage), _na(osm_id), x, y) for bus_id, voltage, osm_id, x, y in self.cur.fetchall()]
        return _frame(rows, EGON_SUBSTATION_COLUMNS, floats=("x", "y"), ints=("bus_id",))

    def egon_districts(self, region_wkb: bytes) -> pd.DataFrame:
        """The eGon-data MV grid districts that intersect the planning region (whole polygons, not clipped).

        Returns:
            Columns of :data:`EGON_DISTRICT_COLUMNS` sorted by ``bus_id``; ``geom_wkb`` is MultiPolygon WKB in
            ``TARGET_EPSG``.

        Raises:
            MvStageError: If no eGon-data district table exists with rows.
        """
        query = _EGON_DISTRICTS_SQL.format(table=self._egon_table_or_raise("egon_mv_grid_district"),
                                           epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb)})
        rows = [(bus_id, _wkb(geom)) for bus_id, geom in self.cur.fetchall()]
        return _frame(rows, EGON_DISTRICT_COLUMNS, ints=("bus_id",))

    # ------------------------------------------------------------------ MaStR
    def mastr_available(self) -> tuple[bool, str]:
        """Whether ``pylovo.mastr_units`` exists and holds units (``pylovo-import mastr``).

        Returns:
            ``(True, "<n> units")`` or ``(False, reason)``.
        """
        self.cur.execute("SELECT to_regclass(%s) IS NOT NULL", (_regclass_text("pylovo", "mastr_units"),))
        if not self.cur.fetchone()[0]:
            return False, "pylovo.mastr_units does not exist; run pylovo-setup first"
        self.cur.execute("SELECT count(*) FROM pylovo.mastr_units")
        count = int(self.cur.fetchone()[0])
        if count == 0:
            return False, "pylovo.mastr_units is empty; run pylovo-import mastr --sqlite PATH"
        return True, f"{count} units"

    def mastr_units_in_region(self, region_wkb: bytes) -> pd.DataFrame:
        """The MaStR units in operation inside the planning region (without storage, ``net_power_kw > 0``).

        Returns:
            Columns of :data:`MASTR_UNIT_COLUMNS` sorted by ``mastr_id``: ``location_id``
            (``LokationMastrNummer``, the grid connection point), ``technology`` (solar, wind, biomass, hydro,
            gsgk, combustion), ``solar_kind``, ``net_power_kw``, ``voltage_level`` (MaStR ``Spannungsebene`` text
            or None) and the unit position ``x``, ``y``.
        """
        query = _MASTR_UNITS_SQL.format(epsg=sql.Literal(TARGET_EPSG))
        self.cur.execute(query, {"region": _wkb(region_wkb)})
        return _frame(self.cur.fetchall(), MASTR_UNIT_COLUMNS, floats=("net_power_kw", "x", "y"))

    def mastr_postcode_aggregates(self, plz_list: Sequence[int]) -> pd.DataFrame:
        """The MaStR units without coordinates of the given PLZ, per technology (without storage).

        Returns:
            Columns of :data:`MASTR_AGGREGATE_COLUMNS` sorted by postcode and technology: ``postcode`` (text,
            5 digits), ``plz`` (int), ``technology``, ``n_units``, ``net_power_kw`` (of the whole postcode) and
            ``area_ratio``: the share of the postcode area inside ``pylovo.postcode`` (below 1 for postcodes
            clipped at the region border; 1 when unknown). The values are not scaled by it.
        """
        plz_text = sorted({f"{int(plz):05d}" for plz in plz_list})
        if not plz_text:
            return _frame([], MASTR_AGGREGATE_COLUMNS, floats=("net_power_kw", "area_ratio"),
                          ints=("plz", "n_units"))
        self.cur.execute(_MASTR_AGGREGATES_SQL, {"plz_text": plz_text})
        rows = [(postcode, int(postcode), technology, n_units, kw, ratio)
                for postcode, technology, n_units, kw, ratio in self.cur.fetchall()]
        return _frame(rows, MASTR_AGGREGATE_COLUMNS, floats=("net_power_kw", "area_ratio"), ints=("plz", "n_units"))
