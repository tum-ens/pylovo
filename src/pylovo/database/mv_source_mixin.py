"""Queries of the external MV inputs: OSM power features and the eGon-data HV/MV substations.

Reads HV/MV substation candidates and their tags from the OSM schema of InfDB (``INFDB_OSM_SCHEMA``), the
eGon-data HV/MV substations (``pylovo.egon_hvmv_substation`` if populated, else the copy in
``INFDB_OPENDATA_SCHEMA``). The rules that turn these rows into MV inputs are pure functions of
:mod:`pylovo.mv_grid.substations`.

The InfDB tables are read through the source reader (:mod:`pylovo.infdb.source_reader`): in the pylovo database, or
with ``USE_EXTERNAL_BASEDATA`` on a separate read-only connection. Those queries filter by index only (bounding boxes
in the SRID of each layer); the exact tests against the planning region run in the pylovo database on its
``ST_Subdivide`` pieces (:meth:`MvSourceMixin.near_region`), which gives the same result as one test against the
whole region.

Every method takes its region or PLZ list explicitly, returns a DataFrame in ``TARGET_EPSG`` coordinates, is
read-only (temporary tables aside) and never commits.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd
from psycopg2 import sql

from pylovo.config_loader import INFDB_OPENDATA_SCHEMA, INFDB_OSM_SCHEMA, INFDB_SOURCE_SCHEMA, TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin
from pylovo.infdb.source_reader import regclass_text

#: Columns of :meth:`MvSourceMixin.osm_substation_candidates` (= ``substations.CANDIDATE_COLUMNS``).
OSM_CANDIDATE_COLUMNS: tuple[str, ...] = ("source_id", "voltage", "substation_tag", "frequency", "operator", "name",
                                          "railway", "x", "y", "area_m2", "footprint_wkb", "in_plant", "plant")
#: Columns of :meth:`MvSourceMixin.egon_substations`.
EGON_SUBSTATION_COLUMNS: tuple[str, ...] = ("bus_id", "voltage", "osm_id", "x", "y")

#: Columns of :meth:`MvSourceMixin.osm_power_objects`.
OSM_OBJECT_COLUMNS: tuple[str, ...] = ("source_id", "kind", "tags", "x", "y", "area_m2")
#: Columns of :meth:`MvSourceMixin.osm_landuse`.
OSM_LANDUSE_COLUMNS: tuple[str, ...] = ("osm_type", "geom_wkb")
#: Columns of :meth:`MvSourceMixin.osm_hv_lines`.
OSM_HV_LINE_COLUMNS: tuple[str, ...] = ("source_id", "subtype", "tags", "wkb")
#: ``power`` values of the ways :meth:`MvSourceMixin.osm_hv_lines` reads (= ``substations.HV_LINE_SUBTYPES``).
OSM_HV_LINE_SUBTYPES: tuple[str, ...] = ("line", "cable")

#: The eGon-data table of the HV/MV substations (their bus ids are the 110 kV buses of eTraGo).
EGON_SUBSTATION_TABLE: str = "egon_hvmv_substation"
#: Margin (m) of the box of the GiST prefilter of the OSM queries (:func:`_search_box`) beyond the reach of their exact
#: test: it covers the difference between a transformed box and the transformed region, so results stay exact.
SEARCH_BOX_MARGIN_M: float = 1000.0

#: Largest number of vertices of a piece of the planning region in the exact tests (``ST_Subdivide``).
REGION_PIECE_VERTICES: int = 256

# The planning region as pieces with a GiST index: a test against the pieces is exact (a point lies within a distance
# of the region when it does of one piece) and finds the near pieces through the index, while one test against a
# region of hundreds of postcodes evaluates the whole multipolygon per row.
_REGION_PIECES_SQL = sql.SQL("""
CREATE TEMP TABLE mv_region_piece ON COMMIT DROP AS
SELECT ST_Subdivide(ST_GeomFromWKB(%(region)s, {epsg}), %(vertices)s) AS g
""")

_REGION_POINTS_SQL = sql.SQL("""
CREATE TEMP TABLE mv_region_point ON COMMIT DROP AS
SELECT (p.k - 1)::integer AS k, ST_SetSRID(ST_MakePoint(p.x, p.y), {epsg}) AS pt
FROM unnest(%(x)s::float8[], %(y)s::float8[]) WITH ORDINALITY AS p(x, y, k)
""")

_NEAR_REGION_SQL = """
SELECT p.k FROM mv_region_point p
WHERE EXISTS (SELECT 1 FROM mv_region_piece r WHERE ST_DWithin(p.pt, r.g, %(d)s))
"""

# power=substation features (points, ways, relations) of the OSM schema with their tags, in the search box of the
# planning region. The tags join uses (geom_type, osm_id): node, way and relation ids can collide. Every OSM layer is
# filtered in its own SRID (``geom && box``, GiST index) by the search box of the region (or of the candidate for the
# plants within 100 m); the exact test within the search buffer runs afterwards on the points (:meth:`near_region`).
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
ORDER BY c.source_id;
""")

# power=substation and power=transformer features, power=pole nodes, and transformer-tower buildings in the search
# box of the planning region with their tags (pylovo.mv_grid.osm_coverage tells the MV/LV stations from the sites of
# a higher level); prefiltered in the source SRIDs as the candidates, then kept within 1 km of the region (the HV/MV
# sites at its edge) by :meth:`near_region`.
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
ORDER BY o.source_id;
""")

# power=line and power=cable ways in the search box of the planning region with their tags, prefiltered in the SRID
# of the line layer (pylovo.mv_grid.substations.is_hv_line keeps the 60 kV and 110 kV ones).
_OSM_HV_LINES_SQL = sql.SQL("""
SELECT l.osm_id, l.osm_subtype, t.tags, ST_AsBinary(ST_Force2D(ST_Transform(l.geom, {epsg}))) AS wkb
FROM {osm}.infrastructure_line l
LEFT JOIN {osm}.tags t ON t.geom_type = 'W' AND t.osm_id = l.osm_id
WHERE l.osm_type = 'power' AND l.osm_subtype = ANY(%(subtypes)s)
  AND l.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(line_srid)s)
ORDER BY l.osm_id
""")

# OSM land-use polygons in the search box of the planning region (staged into pg_temp.mv_landuse_raw) ...
_OSM_LANDUSE_STAGE_SQL = sql.SQL("""
SELECT l.osm_id, l.osm_type, l.geom FROM {osm}.landuse_polygon l
WHERE l.geom && ST_Transform(ST_GeomFromWKB(%(box)s, {epsg}), %(landuse_srid)s)
""")
# ... clipped to the region. Only a polygon across the region border needs the overlay: a polygon inside it (the
# prepared ST_Covers test, which ST_CoveredBy is not) stays as it is. A staged table is read without parallel workers,
# so the overlay of every polygon took three times as long as in the source table.
_OSM_LANDUSE_SQL = sql.SQL("""
WITH r AS (SELECT ST_GeomFromWKB(%(region)s, {epsg}) AS g)
SELECT l.osm_type, ST_AsBinary(CASE WHEN ST_Covers(r.g, v.geom) THEN v.geom ELSE ST_Intersection(v.geom, r.g) END)
FROM pg_temp.mv_landuse_raw l CROSS JOIN r
CROSS JOIN LATERAL (SELECT ST_MakeValid(ST_Transform(l.geom, {epsg})) AS geom) v
WHERE ST_Intersects(ST_Transform(l.geom, {epsg}), r.g)
ORDER BY l.osm_id;
""")

# the municipality polygons of the AGS (staged into pg_temp.mv_scope_raw) ...
_SCOPE_STAGE_SQL = sql.SQL("SELECT s.ags, s.geom FROM {scope} s WHERE s.ags::text = ANY(%(ags)s)")
# ... and their area inside the region (km²)
_SCOPE_AREAS_SQL = sql.SQL("""
SELECT s.ags::text, ST_Area(ST_Intersection(ST_Transform(s.geom, {epsg}), ST_GeomFromWKB(%(region)s, {epsg}))) / 1e6
FROM pg_temp.mv_scope_raw s
""")

# every eGon-data substation (a small table of Germany); the test within the search buffer runs on the points
_EGON_SUBSTATIONS_SQL = sql.SQL("""
SELECT s.bus_id::bigint AS bus_id, NULLIF(btrim(s.voltage::text), 'NA') AS voltage,
       NULLIF(btrim(s.osm_id::text), 'NA') AS osm_id,
       ST_X(ST_Transform(s.geom, {epsg})) AS x, ST_Y(ST_Transform(s.geom, {epsg})) AS y
FROM {table} s
WHERE s.geom IS NOT NULL
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

    def check_mv_sources(self) -> list[str]:
        """Check, before any work, that the InfDB source tables of the MV stage exist and may be read.

        Required: the ways and buildings of ``INFDB_SOURCE_SCHEMA`` (routing graph, settlements) and the OSM power
        features with their tags (HV/MV substations). The OSM land use, MV lines and transformer towers and the
        municipality areas (``INFDB_OPENDATA_SCHEMA.scope``) are used when they exist.

        Returns:
            One message per missing optional table (for the log).

        Raises:
            MvStageError: A schema is not set, or a required table is missing; the message names the tables and
                the database that was searched.
        """
        reader = self.source_reader()
        required = [(INFDB_SOURCE_SCHEMA, "ways_per_junction"), (INFDB_SOURCE_SCHEMA, "buildings"),
                    (INFDB_OSM_SCHEMA, "infrastructure_point"), (INFDB_OSM_SCHEMA, "infrastructure_polygon"),
                    (INFDB_OSM_SCHEMA, "tags")]
        optional = {(INFDB_OSM_SCHEMA, "landuse_polygon"): "MV settlements from address buildings only",
                    (INFDB_OSM_SCHEMA, "infrastructure_line"): "no OSM MV lines, every MV line is a cable, and "
                                                               "no check of the OSM HV lines at the substations",
                    (INFDB_OSM_SCHEMA, "building_polygon"): "no transformer towers in the OSM coverage report",
                    (INFDB_OPENDATA_SCHEMA, "scope"): "MV_NOMINAL_VOLTAGE_KV auto finds no municipality areas"}
        if not INFDB_SOURCE_SCHEMA or not INFDB_OSM_SCHEMA:
            raise _stage_error("the MV stage reads the InfDB schemas: set USE_INFDB, INFDB_SOURCE_SCHEMA and "
                               "INFDB_OSM_SCHEMA in the .env file")
        missing = [f"{s}.{t}" for s, t in required if not reader.table_exists(s, t)]
        if missing:
            raise _stage_error(f"the MV stage needs {', '.join(missing)} in {reader.where}")
        return [f"no {s or '<INFDB_OPENDATA_SCHEMA>'}.{t} in {reader.where}: {effect}"
                for (s, t), effect in optional.items() if not reader.table_exists(s, t)]

    def _osm_srid(self, table: str) -> int:
        """The SRID of the geometries of an OSM layer (``TARGET_EPSG`` without geometries), for the prefilters."""
        return int(self.source_reader().srid(INFDB_OSM_SCHEMA, table) or TARGET_EPSG)

    def near_region(self, xy: np.ndarray, region_wkb: bytes, distance_m: float) -> np.ndarray:
        """Which points lie within ``distance_m`` of the planning region (``ST_DWithin`` in ``TARGET_EPSG``).

        The test runs in the pylovo database against the ``ST_Subdivide`` pieces of the region (temporary tables
        ``mv_region_piece`` and ``mv_region_point``, dropped at commit), with the same result as one test against the
        whole region.

        Args:
            xy: ``(n, 2)`` point coordinates (``TARGET_EPSG``).
            region_wkb: The planning region (WKB, ``TARGET_EPSG``).
            distance_m: The distance.

        Returns:
            Boolean mask of length ``n``.
        """
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        if not len(xy):
            return np.zeros(0, dtype=bool)
        epsg = int(TARGET_EPSG)
        self.cur.execute("DROP TABLE IF EXISTS pg_temp.mv_region_piece, pg_temp.mv_region_point")
        self.cur.execute(_REGION_PIECES_SQL.format(epsg=sql.Literal(epsg)),
                         {"region": _wkb(region_wkb), "vertices": REGION_PIECE_VERTICES})
        self.cur.execute("CREATE INDEX ON mv_region_piece USING gist (g)")
        self.cur.execute(_REGION_POINTS_SQL.format(epsg=sql.Literal(epsg)),
                         {"x": [float(v) for v in xy[:, 0]], "y": [float(v) for v in xy[:, 1]]})
        self.cur.execute("ANALYZE mv_region_piece")
        self.cur.execute(_NEAR_REGION_SQL, {"d": float(distance_m)})
        mask = np.zeros(len(xy), dtype=bool)
        mask[[int(r[0]) for r in self.cur.fetchall()]] = True
        self.cur.execute("DROP TABLE pg_temp.mv_region_piece, pg_temp.mv_region_point")
        return mask

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
        found = self.source_reader().fetchall(query, {
            "box": _search_box(_wkb(region_wkb), buffer_m), "margin_m": SEARCH_BOX_MARGIN_M,
            "point_srid": self._osm_srid("infrastructure_point"),
            "polygon_srid": self._osm_srid("infrastructure_polygon")})
        rows = [(*row[:10], _wkb(row[10]), row[11], row[12] if len(row) > 12 else None) for row in found]
        frame = _frame(rows, OSM_CANDIDATE_COLUMNS, floats=("x", "y", "area_m2"), bools=("in_plant",))
        keep = self.near_region(frame[["x", "y"]].to_numpy(dtype=float), region_wkb, buffer_m)
        return frame[keep].reset_index(drop=True)

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
        reader = self.source_reader()
        schema = _schema(INFDB_OPENDATA_SCHEMA, "INFDB_OPENDATA_SCHEMA")
        reader.stage("mv_scope_raw", _SCOPE_STAGE_SQL.format(scope=sql.Identifier(INFDB_OPENDATA_SCHEMA, "scope")),
                     {"ags": [str(a) for a in ags]},
                     lambda: reader.column_types(schema.strings[0], "scope", ("ags", "geom")))
        self.cur.execute(_SCOPE_AREAS_SQL.format(epsg=sql.Literal(TARGET_EPSG)), {"region": _wkb(region_wkb)})
        areas = {str(a): float(km2) for a, km2 in self.cur.fetchall() if km2 is not None}
        self.cur.execute("DROP TABLE pg_temp.mv_scope_raw")
        return areas

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
        found = self.source_reader().fetchall(query, {
            "box": _search_box(_wkb(region_wkb), 1000.0),
            "point_srid": self._osm_srid("infrastructure_point"),
            "polygon_srid": self._osm_srid("infrastructure_polygon"),
            "building_srid": self._osm_srid("building_polygon")})
        frame = _frame(found, OSM_OBJECT_COLUMNS, floats=("x", "y", "area_m2"))
        keep = self.near_region(frame[["x", "y"]].to_numpy(dtype=float), region_wkb, 1000.0)
        return frame[keep].reset_index(drop=True)

    def osm_landuse(self, region_wkb: bytes) -> pd.DataFrame:
        """The OSM ``landuse`` polygons clipped to the planning region: ``osm_type`` and ``geom_wkb`` (WKB,
        ``TARGET_EPSG``).

        The polygons in the search box are staged (``pg_temp.mv_landuse_raw``) and clipped in the pylovo database.

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        reader = self.source_reader()
        epsg = sql.Literal(TARGET_EPSG)
        osm = _schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA")
        reader.stage("mv_landuse_raw", _OSM_LANDUSE_STAGE_SQL.format(osm=osm, epsg=epsg),
                     {"box": _search_box(_wkb(region_wkb), 0.0), "landuse_srid": self._osm_srid("landuse_polygon")},
                     lambda: reader.column_types(INFDB_OSM_SCHEMA, "landuse_polygon", ("osm_id", "osm_type", "geom")))
        self.cur.execute(_OSM_LANDUSE_SQL.format(epsg=epsg), {"region": _wkb(region_wkb)})
        frame = _frame([(t, _wkb(g)) for t, g in self.cur.fetchall()], OSM_LANDUSE_COLUMNS)
        self.cur.execute("DROP TABLE pg_temp.mv_landuse_raw")
        return frame

    def osm_hv_lines(self, region_wkb: bytes, reach_m: float) -> pd.DataFrame | None:
        """The ``power=line`` and ``power=cable`` ways of the OSM schema in the search box of the planning region,
        the inputs of :func:`pylovo.mv_grid.substations.hv_line_ends` (which keeps the 60 kV and 110 kV ones).

        Args:
            region_wkb: The planning region (WKB, ``TARGET_EPSG``).
            reach_m: How far beyond the region the ways are read (the search buffer of the sites plus the end
                distance of the check).

        Returns:
            Columns of :data:`OSM_HV_LINE_COLUMNS`, sorted by ``source_id`` (``way/<id>``): ``subtype`` (the power
            value), ``tags`` (dict, None without tags) and ``wkb`` (the line, WKB in ``TARGET_EPSG``); None without
            ``infrastructure_line`` in the OSM schema.

        Raises:
            MvStageError: If ``INFDB_OSM_SCHEMA`` is not set.
        """
        osm = _schema(INFDB_OSM_SCHEMA, "INFDB_OSM_SCHEMA")
        if not self.source_reader().table_exists(INFDB_OSM_SCHEMA, "infrastructure_line"):
            return None
        found = self.source_reader().fetchall(_OSM_HV_LINES_SQL.format(osm=osm, epsg=sql.Literal(TARGET_EPSG)), {
            "subtypes": list(OSM_HV_LINE_SUBTYPES), "box": _search_box(_wkb(region_wkb), reach_m),
            "line_srid": self._osm_srid("infrastructure_line")})
        return _frame([(f"way/{osm_id}", subtype, tags if isinstance(tags, dict) else None, _wkb(wkb))
                       for osm_id, subtype, tags, wkb in found], OSM_HV_LINE_COLUMNS)

    # ------------------------------------------------------------------ eGon
    def _egon_table(self, table: str) -> tuple[str, sql.Identifier, bool] | None:
        """Where an eGon-data table is read: ``pylovo`` (the pylovo database) if filled, else the opendata schema
        of the InfDB source.

        Returns:
            ``(schema, identifier, local)`` of the first existing, non-empty table (``local``: in the pylovo
            database, else read through the source reader), or None.
        """
        reader = self.source_reader()
        for schema, local in (("pylovo", True), (INFDB_OPENDATA_SCHEMA, not reader.remote)):
            if not schema:
                continue
            identifier = sql.Identifier(schema, table)
            if local:
                # a probe that fails would abort the caller's transaction: check existence and privilege first
                self.cur.execute("SELECT c IS NOT NULL AND has_table_privilege(c, 'SELECT') "
                                 "FROM (SELECT to_regclass(%s) AS c) AS r", (regclass_text(schema, table),))
                if not self.cur.fetchone()[0]:
                    continue
                self.cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(identifier))
                filled = bool(self.cur.fetchone()[0])
            else:
                filled = reader.table_exists(schema, table) and bool(
                    reader.fetchone(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(identifier))[0])
            if filled:
                return schema, identifier, local
        return None

    def _egon_table_or_raise(self, table: str) -> tuple[sql.Identifier, bool]:
        found = self._egon_table(table)
        if found is None:
            raise _stage_error(f"no eGon-data table {table}: neither populated pylovo.{table} nor "
                               f"{INFDB_OPENDATA_SCHEMA or '<INFDB_OPENDATA_SCHEMA>'}.{table} exists with rows")
        return found[1], found[2]

    def egon_available(self) -> tuple[bool, str]:
        """Whether the eGon-data HV/MV substations can be read (for the eTraGo bus of each site), and from which schema.

        The table ``pylovo.egon_hvmv_substation`` (filled by ``pylovo-setup``) is used if it holds rows, else the copy
        in ``INFDB_OPENDATA_SCHEMA`` of the InfDB source.

        Returns:
            ``(True, schema)`` (``"pylovo"`` or the opendata schema), or ``(False, reason)``.
        """
        found = self._egon_table(EGON_SUBSTATION_TABLE)
        if found is None:
            where = f"pylovo or {INFDB_OPENDATA_SCHEMA}" if INFDB_OPENDATA_SCHEMA else "pylovo"
            return False, (f"no rows in {EGON_SUBSTATION_TABLE} ({where}); run pylovo-setup to import the eGon-data "
                           "substations")
        return True, found[0]

    def egon_substations(self, region_wkb: bytes, buffer_m: float) -> pd.DataFrame:
        """The eGon-data HV/MV substations within ``buffer_m`` of the planning region.

        Returns:
            Columns of :data:`EGON_SUBSTATION_COLUMNS` sorted by ``bus_id``; ``voltage`` and ``osm_id``
            (``w<way id>``, ``n<node id>``) are None where eGon-data writes ``NA``.

        Raises:
            MvStageError: If no eGon-data substation table exists with rows.
        """
        table, local = self._egon_table_or_raise(EGON_SUBSTATION_TABLE)
        query = _EGON_SUBSTATIONS_SQL.format(table=table, epsg=sql.Literal(TARGET_EPSG))
        if local:
            self.cur.execute(query)
            found = self.cur.fetchall()
        else:
            found = self.source_reader().fetchall(query)
        rows = [(bus_id, _na(voltage), _na(osm_id), x, y) for bus_id, voltage, osm_id, x, y in found]
        frame = _frame(rows, EGON_SUBSTATION_COLUMNS, floats=("x", "y"), ints=("bus_id",))
        keep = self.near_region(frame[["x", "y"]].to_numpy(dtype=float), region_wkb, buffer_m)
        return frame[keep].reset_index(drop=True)
