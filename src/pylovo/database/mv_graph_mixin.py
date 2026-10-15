"""Queries of the MV routing graph (``pylovo.mv_graph``, ``mv_graph_vertex``, ``mv_graph_edge``, ``mv_graph_settlement``).

The graph is built server-side from the regional street graph of InfDB, with per-edge settlement
flags and settlement components. It is unversioned reference data, content-addressed by ``graph_id``.
This mixin builds or reuses it, attaches MV terminals to it, reads the subgraph of a supply district and
reports QA figures. Like the other mixins its methods run in the current transaction and do not commit
unless their docstring says so.

Build (:meth:`MvGraphMixin.ensure_graph`), one transaction under an advisory lock:

1. the ways of every municipality (AGS) within ``region_buffer_m`` of the planning region, without the excluded
   ``klasse`` values (motorways), transformed to ``TARGET_EPSG``;
2. vertices from a DBSCAN of the edge ends (``graph_snap_tolerance_m``), which also joins the ends of
   neighbouring municipalities; the edge ends are moved onto their vertex, loops created by the snap are dropped.
   Before that, T-junctions at municipality borders (an edge end within :data:`SEAM_SPLIT_M` of the interior of
   another edge; the InfDB splits ways per AGS only) are repaired by splitting the touched edge there;
3. degrees, connected components (``pgr_connectedComponents``, label = smallest vertex id);
4. settlement flags: an edge is in a settlement if at least ``max(settlement_min_buildings, length /
   settlement_spacing_m)`` address buildings lie within ``settlement_buffer_m``; settlements are the connected
   components of the settlement edges, urban with at least ``urban_min_buildings`` buildings;
5. summary columns of ``pylovo.mv_graph`` (counts, largest component share, settlement share, seam diagnostic).

A changed source or build parameter gives another ``graph_id``, so a stored graph is never rebuilt in place.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np
import pandas as pd
from psycopg2 import sql
from psycopg2.extras import execute_values

from pylovo.config_loader import INFDB_SOURCE_SCHEMA, TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin
from pylovo.mv_grid.model import EdgeSet
from pylovo.mv_grid.parameters import MvStageError

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import MvParameters

#: Advisory lock key of graph builds (next to 907360 setup, 907361 PLZ, 907362 MV variant).
GRAPH_LOCK_KEY: int = 907363
#: Version of the build procedure; part of the graph id, bumped whenever the build changes its result.
GRAPH_BUILD_VERSION: int = 1
#: Seam diagnostic: a lone edge end this close to the interior of another edge (m).
SEAM_TOLERANCE_M: float = 0.5
#: Seam repair: edges are split where an edge end lies this close to their interior (m), so T-junctions at
#: municipality borders (InfDB splits ways per AGS) become graph vertices; ``None`` keeps the seams. Part of the
#: build parameters (and so of the graph id).
SEAM_SPLIT_M: float | None = 0.5
#: Building classes of the settlement flags (the classes of the LV building fetch, ``infdb_client.py``).
SETTLEMENT_BUILDING_USES: tuple[str, ...] = ("Commercial", "Public", "Residential", "Mixed", "Unknown")
#: Graph ids are 'g' + 16 hex digits of an md5 (safe to inline as SQL literals).
GRAPH_ID_PATTERN = re.compile(r"^g[0-9a-f]{16}$")

_EDGE_COLUMNS = "edge_id AS id, source, target, length_m AS cost, length_m AS reverse_cost"

# ---------------------------------------------------------------------------------------------------- SQL templates
# {ways}, {buildings}: sql.Identifier(INFDB_SOURCE_SCHEMA, ...); {epsg}: sql.Literal(TARGET_EPSG)
SRID_SQL = sql.SQL("SELECT ST_SRID(geom) FROM {table} WHERE geom IS NOT NULL LIMIT 1")

# the buffer of the constant region is computed once and tested as a prepared geometry; ST_DWithin against a large
# multipolygon evaluates the distance per row and took 35 times longer on the test regions
GRAPH_AGS_SQL = sql.SQL("""
    SELECT COALESCE(array_agg(DISTINCT w.ags::text ORDER BY w.ags::text), ARRAY[]::text[])
    FROM {ways} w
    WHERE ST_Intersects(w.geom, ST_Transform(ST_Buffer(ST_GeomFromWKB(%(region)s, {epsg}), %(buffer_m)s),
                                             %(src_srid)s))
""")

WAYS_FINGERPRINT_SQL = sql.SQL("""
    SELECT count(*), COALESCE(round(sum(ST_Length(w.geom))::numeric), 0), max(w.changelog_id)
    FROM {ways} w
    WHERE w.ags::text = ANY(%(ags)s)
""")

BUILDINGS_FINGERPRINT_SQL = sql.SQL("""
    SELECT count(*), max(b.changelog_id)
    FROM {buildings} b
    WHERE b.gemeindeschluessel::text = ANY(%(ags)s)
""")

GRAPH_EXISTS_SQL = "SELECT 1 FROM pylovo.mv_graph WHERE graph_id = %(g)s"

LOCK_SQL = "SELECT pg_advisory_xact_lock(%(key)s, hashtext(%(g)s))"

DROP_BUILD_TEMP_SQL = ("DROP TABLE IF EXISTS pg_temp.mvg_src, pg_temp.mvg_end, pg_temp.mvg_vert, pg_temp.mvg_bld, "
                       "pg_temp.mvg_cut, pg_temp.mvg_split")

SOURCE_EDGES_SQL = sql.SQL("""
    CREATE TEMP TABLE mvg_src ON COMMIT DROP AS
    SELECT row_number() OVER (ORDER BY w.ags, w.id, d.path)::bigint AS edge_id,
           w.ags::text AS ags, w.id::text AS src_id, btrim(w.klasse) AS klasse, w.objektart::text AS objektart,
           d.geom
    FROM {ways} w
    CROSS JOIN LATERAL ST_Dump(ST_Transform(ST_Force2D(w.geom), {epsg})) d
    WHERE w.ags::text = ANY(%(ags)s) AND w.geom IS NOT NULL
      AND NOT (COALESCE(btrim(w.klasse), '') = ANY(%(excluded)s))
      AND GeometryType(d.geom) = 'LINESTRING' AND ST_Length(d.geom) > 0
""")

ENDPOINTS_SQL = """
    CREATE TEMP TABLE mvg_end ON COMMIT DROP AS
    SELECT edge_id, end_no, pt, ST_ClusterDBSCAN(pt, eps := %(snap)s, minpoints := 1) OVER () AS cluster_id
    FROM (SELECT edge_id, 0 AS end_no, ST_StartPoint(geom) AS pt FROM mvg_src
          UNION ALL
          SELECT edge_id, 1, ST_EndPoint(geom) FROM mvg_src) ep
"""

# vertex ids numbered by the first edge end of each cluster: deterministic for the same source rows
VERTICES_TEMP_SQL = """
    CREATE TEMP TABLE mvg_vert ON COMMIT DROP AS
    SELECT cluster_id, row_number() OVER (ORDER BY min(edge_id * 2 + end_no))::bigint AS vertex_id,
           ST_Centroid(ST_Collect(pt)) AS geom
    FROM mvg_end GROUP BY cluster_id
"""

SEAM_COUNT_SQL = """
    WITH lone AS (SELECT cluster_id FROM mvg_end GROUP BY cluster_id HAVING count(*) = 1)
    SELECT count(*)
    FROM mvg_end p JOIN lone l ON l.cluster_id = p.cluster_id
    WHERE EXISTS (SELECT 1 FROM mvg_src e
                  WHERE e.edge_id <> p.edge_id AND ST_DWithin(e.geom, p.pt, %(seam)s)
                    AND ST_Distance(ST_StartPoint(e.geom), p.pt) > %(seam)s
                    AND ST_Distance(ST_EndPoint(e.geom), p.pt) > %(seam)s)
"""

# seam repair: every edge end within %(split)s of another edge's interior cuts that edge (nearest host per end);
# the pieces end exactly at the cutting edge end, so the endpoint clustering joins them
SEAM_CUTS_SQL = """
    CREATE TEMP TABLE mvg_cut ON COMMIT DROP AS
    SELECT DISTINCT ON (p.edge_id, p.end_no) h.edge_id AS host, ST_LineLocatePoint(h.geom, p.pt) AS f, p.pt
    FROM mvg_end p
    JOIN mvg_src h ON h.edge_id <> p.edge_id AND ST_DWithin(h.geom, p.pt, %(split)s)
    WHERE ST_Distance(ST_StartPoint(h.geom), p.pt) > %(split)s AND ST_Distance(ST_EndPoint(h.geom), p.pt) > %(split)s
    ORDER BY p.edge_id, p.end_no, ST_Distance(h.geom, p.pt), h.edge_id
"""

SEAM_PIECES_SQL = """
    CREATE TEMP TABLE mvg_split ON COMMIT DROP AS
    WITH cut AS (SELECT DISTINCT ON (host, round((f * 1e6)::numeric)) host, f, pt
                 FROM mvg_cut WHERE f > 0 AND f < 1
                 ORDER BY host, round((f * 1e6)::numeric), f),
         c AS (SELECT host, array_agg(f ORDER BY f) AS fs, array_agg(pt ORDER BY f) AS pts FROM cut GROUP BY host),
         parts AS (SELECT c.host, i AS part,
                          CASE WHEN i = 1 THEN 0.0 ELSE c.fs[i - 1] END AS f0,
                          CASE WHEN i = cardinality(c.fs) + 1 THEN 1.0 ELSE c.fs[i] END AS f1,
                          CASE WHEN i = 1 THEN NULL ELSE c.pts[i - 1] END AS p0,
                          CASE WHEN i = cardinality(c.fs) + 1 THEN NULL ELSE c.pts[i] END AS p1
                   FROM c CROSS JOIN LATERAL generate_series(1, cardinality(c.fs) + 1) AS i)
    SELECT p.host, p.part,
           CASE WHEN p.p1 IS NULL THEN a.g ELSE ST_SetPoint(a.g, ST_NPoints(a.g) - 1, p.p1) END AS geom
    FROM parts p
    JOIN mvg_src s ON s.edge_id = p.host
    CROSS JOIN LATERAL (SELECT CASE WHEN p.p0 IS NULL THEN ST_LineSubstring(s.geom, p.f0, p.f1)
                                    ELSE ST_SetPoint(ST_LineSubstring(s.geom, p.f0, p.f1), 0, p.p0) END AS g) a
"""

SEAM_REPLACE_SQL = (
    """INSERT INTO mvg_src (edge_id, ags, src_id, klasse, objektart, geom)
       SELECT (SELECT max(edge_id) FROM mvg_src) + row_number() OVER (ORDER BY sp.host, sp.part),
              s.ags, s.src_id, s.klasse, s.objektart, sp.geom
       FROM mvg_split sp JOIN mvg_src s ON s.edge_id = sp.host
       WHERE GeometryType(sp.geom) = 'LINESTRING' AND ST_Length(sp.geom) > 0""",
    "DELETE FROM mvg_src WHERE edge_id IN (SELECT host FROM mvg_split)",
    "ANALYZE mvg_src",
)

INSERT_GRAPH_SQL = """
    INSERT INTO pylovo.mv_graph (graph_id, source_table, ags, excluded_klasse, snap_tolerance_m, build_parameters,
                                 source_fingerprint)
    VALUES (%(g)s, %(source)s, %(ags)s, %(excluded)s, %(snap)s, %(params)s::jsonb, %(fingerprint)s)
"""

INSERT_VERTICES_SQL = """
    INSERT INTO pylovo.mv_graph_vertex (graph_id, vertex_id, geom)
    SELECT %(g)s, vertex_id, geom FROM mvg_vert
"""

INSERT_EDGES_SQL = """
    INSERT INTO pylovo.mv_graph_edge (graph_id, edge_id, ags, src_id, klasse, objektart, source, target, length_m, geom)
    SELECT %(g)s, e.edge_id, e.ags, e.src_id, e.klasse, e.objektart, vs.vertex_id, vt.vertex_id,
           ST_Length(x.geom), x.geom
    FROM mvg_src e
    JOIN mvg_end s ON s.edge_id = e.edge_id AND s.end_no = 0
    JOIN mvg_end t ON t.edge_id = e.edge_id AND t.end_no = 1
    JOIN mvg_vert vs ON vs.cluster_id = s.cluster_id
    JOIN mvg_vert vt ON vt.cluster_id = t.cluster_id
    CROSS JOIN LATERAL (SELECT ST_SetPoint(ST_SetPoint(e.geom, 0, vs.geom), ST_NPoints(e.geom) - 1, vt.geom) AS geom) x
    WHERE s.cluster_id <> t.cluster_id
"""

DEGREE_SQL = """
    UPDATE pylovo.mv_graph_vertex v SET degree = d.n
    FROM (SELECT vid, count(*) AS n
          FROM (SELECT source AS vid FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s
                UNION ALL
                SELECT target FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s) x
          GROUP BY vid) d
    WHERE v.graph_id = %(g)s AND v.vertex_id = d.vid
"""

DROP_ISOLATED_SQL = "DELETE FROM pylovo.mv_graph_vertex WHERE graph_id = %(g)s AND degree IS NULL"

VERTEX_COMPONENT_SQL = """
    UPDATE pylovo.mv_graph_vertex v SET component = c.component
    FROM pgr_connectedComponents(%(edges_sql)s) c
    WHERE v.graph_id = %(g)s AND v.vertex_id = c.node
"""

EDGE_COMPONENT_SQL = """
    UPDATE pylovo.mv_graph_edge e SET component = v.component
    FROM pylovo.mv_graph_vertex v
    WHERE e.graph_id = %(g)s AND v.graph_id = e.graph_id AND v.vertex_id = e.source
"""

EXTENT_SQL = "SELECT ST_XMin(x.b), ST_YMin(x.b), ST_XMax(x.b), ST_YMax(x.b) FROM (SELECT ST_Extent(geom) AS b FROM mvg_src) x"

SETTLEMENT_BUILDINGS_SQL = sql.SQL("""
    CREATE TEMP TABLE mvg_bld ON COMMIT DROP AS
    SELECT row_number() OVER ()::bigint AS bid,
           ST_Transform(COALESCE(b.centroid, ST_PointOnSurface(b.geom)), {epsg}) AS pt
    FROM {buildings} b
    WHERE b.geom && ST_Transform(ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, {epsg}), %(bld_srid)s)
      AND b.building_use = ANY(%(uses)s)
      AND COALESCE(b.building_use_id, '') <> '31001_2523'
      AND COALESCE(b.street, '') <> '' AND COALESCE(b.house_number, '') <> ''
""")

EDGE_BUILDINGS_SQL = """
    UPDATE pylovo.mv_graph_edge e SET n_buildings = c.n
    FROM (SELECT e2.edge_id, count(b.bid) AS n
          FROM pylovo.mv_graph_edge e2 LEFT JOIN mvg_bld b ON ST_DWithin(b.pt, e2.geom, %(sbuf)s)
          WHERE e2.graph_id = %(g)s GROUP BY e2.edge_id) c
    WHERE e.graph_id = %(g)s AND e.edge_id = c.edge_id
"""

IN_SETTLEMENT_SQL = """
    UPDATE pylovo.mv_graph_edge SET in_settlement = n_buildings >= GREATEST(%(smin)s, length_m / %(sspacing)s)
    WHERE graph_id = %(g)s
"""

SETTLEMENT_ID_SQL = """
    UPDATE pylovo.mv_graph_edge e SET settlement_id = c.component
    FROM pgr_connectedComponents(%(settlement_edges_sql)s) c
    WHERE e.graph_id = %(g)s AND e.in_settlement AND c.node = e.source
"""

INSERT_SETTLEMENTS_SQL = """
    WITH sb AS (SELECT e.settlement_id, count(DISTINCT b.bid) AS n
                FROM pylovo.mv_graph_edge e JOIN mvg_bld b ON ST_DWithin(b.pt, e.geom, %(sbuf)s)
                WHERE e.graph_id = %(g)s AND e.in_settlement GROUP BY e.settlement_id),
         sg AS (SELECT settlement_id, sum(length_m) / 1000.0 AS km,
                       ST_Multi(ST_Buffer(ST_Collect(geom), %(spoly)s, 'quad_segs=4')) AS geom
                FROM pylovo.mv_graph_edge
                WHERE graph_id = %(g)s AND in_settlement AND settlement_id IS NOT NULL
                GROUP BY settlement_id)
    INSERT INTO pylovo.mv_graph_settlement (graph_id, settlement_id, n_buildings, length_km, urban, geom)
    SELECT %(g)s, sg.settlement_id, COALESCE(sb.n, 0), sg.km, COALESCE(sb.n, 0) >= %(urban_min)s, sg.geom
    FROM sg LEFT JOIN sb USING (settlement_id)
"""

SETTLEMENT_URBAN_SQL = """
    UPDATE pylovo.mv_graph_edge e SET settlement_urban = s.urban
    FROM pylovo.mv_graph_settlement s
    WHERE e.graph_id = %(g)s AND s.graph_id = e.graph_id AND s.settlement_id = e.settlement_id
"""

SUMMARY_SQL = """
    WITH e AS (SELECT count(*) AS n, COALESCE(sum(length_m), 0) AS len,
                      COALESCE(sum(length_m) FILTER (WHERE in_settlement), 0) AS slen
               FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s),
         c AS (SELECT component, sum(length_m) AS len FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s
               GROUP BY component),
         v AS (SELECT count(*) AS n FROM pylovo.mv_graph_vertex WHERE graph_id = %(g)s),
         s AS (SELECT count(*) AS n FROM pylovo.mv_graph_settlement WHERE graph_id = %(g)s)
    UPDATE pylovo.mv_graph g
    SET edge_count = e.n, vertex_count = v.n, component_count = (SELECT count(*) FROM c),
        largest_component_share = (SELECT max(len) FROM c) / NULLIF(e.len, 0),
        total_length_km = e.len / 1000.0, settlement_count = s.n,
        settlement_edge_share = e.slen / NULLIF(e.len, 0),
        seam_endpoint_count = %(seams)s, build_seconds = %(seconds)s
    FROM e, v, s
    WHERE g.graph_id = %(g)s
"""

ANALYZE_SQL = ("ANALYZE pylovo.mv_graph_vertex", "ANALYZE pylovo.mv_graph_edge", "ANALYZE pylovo.mv_graph_settlement")

REGION_ARRAYS_SQL = """
    SELECT edge_id, source, target, length_m FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s ORDER BY edge_id
"""

ATTACH_TEMP_SQL = sql.SQL(
    "CREATE TEMP TABLE mv_point_tem (key bigint PRIMARY KEY, geom geometry(Point, {epsg}) NOT NULL) ON COMMIT DROP"
)

ATTACH_SQL = """
    SELECT p.key, e.edge_id, e.component, e.in_settlement, e.settlement_id, e.settlement_urban,
           ST_LineLocatePoint(e.geom, p.geom) AS fraction, ST_Distance(e.geom, p.geom) AS offset_m,
           ST_X(ST_ClosestPoint(e.geom, p.geom)) AS ax, ST_Y(ST_ClosestPoint(e.geom, p.geom)) AS ay
    FROM mv_point_tem p
    LEFT JOIN LATERAL (SELECT e.edge_id, e.component, e.in_settlement, e.settlement_id, e.settlement_urban, e.geom
                       FROM pylovo.mv_graph_edge e WHERE e.graph_id = %(g)s
                       ORDER BY e.geom <-> p.geom LIMIT 1) e ON true
    ORDER BY p.key
"""

SUBGRAPH_EDGES_SQL = sql.SQL("""
    SELECT e.edge_id, e.source, e.target, e.length_m, e.in_settlement, COALESCE(e.settlement_id, -1)
    FROM pylovo.mv_graph_edge e
    WHERE e.graph_id = %(g)s
      AND (ST_DWithin(e.geom, ST_GeomFromWKB(%(hull)s, {epsg}), %(buffer_m)s) OR e.edge_id = ANY(%(must)s))
    ORDER BY e.edge_id
""")

SUBGRAPH_VERTICES_SQL = """
    SELECT v.vertex_id, ST_X(v.geom), ST_Y(v.geom) FROM pylovo.mv_graph_vertex v
    WHERE v.graph_id = %(g)s AND v.vertex_id = ANY(%(vertex_ids)s)
    ORDER BY v.vertex_id
"""

EDGE_GEOMETRIES_SQL = """
    SELECT edge_id, ST_AsBinary(geom) FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s AND edge_id = ANY(%(ids)s)
"""

QA_SQL: dict[str, str] = {
    "summary": """
        SELECT graph_id, source_table, cardinality(ags) AS n_ags, excluded_klasse, snap_tolerance_m,
               source_fingerprint, edge_count, vertex_count, component_count, largest_component_share,
               total_length_km, settlement_count, settlement_edge_share, seam_endpoint_count, build_seconds,
               created_at
        FROM pylovo.mv_graph WHERE graph_id = %(g)s""",
    "klasse": """
        SELECT klasse, count(*) AS edges, round((sum(length_m) / 1000.0)::numeric, 1)::float AS km,
               round((sum(length_m) FILTER (WHERE in_settlement) / 1000.0)::numeric, 1)::float AS settlement_km
        FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s GROUP BY klasse ORDER BY km DESC NULLS LAST""",
    "components": """
        SELECT component, count(*) AS edges, round((sum(length_m) / 1000.0)::numeric, 2)::float AS km,
               sum(length_m) / NULLIF(sum(sum(length_m)) OVER (), 0) AS share,
               count(DISTINCT ags) AS n_ags, array_agg(DISTINCT ags ORDER BY ags) AS ags
        FROM pylovo.mv_graph_edge WHERE graph_id = %(g)s GROUP BY component ORDER BY km DESC""",
    "degrees": """
        SELECT degree, count(*) AS vertices FROM pylovo.mv_graph_vertex WHERE graph_id = %(g)s
        GROUP BY degree ORDER BY degree""",
    "settlements": """
        SELECT urban, count(*) AS settlements, sum(n_buildings) AS buildings,
               round(sum(length_km)::numeric, 1)::float AS km,
               round((sum(ST_Area(geom)) / 1e6)::numeric, 1)::float AS area_km2, max(n_buildings) AS max_buildings
        FROM pylovo.mv_graph_settlement WHERE graph_id = %(g)s GROUP BY urban ORDER BY urban""",
    "largest_settlements": """
        SELECT settlement_id, n_buildings, round(length_km::numeric, 1)::float AS km, urban,
               round((ST_Area(geom) / 1e6)::numeric, 2)::float AS area_km2
        FROM pylovo.mv_graph_settlement WHERE graph_id = %(g)s ORDER BY n_buildings DESC, settlement_id LIMIT 15""",
}


def edges_sql(graph_id: str, settlement_only: bool = False) -> str:
    """The pgRouting edge query of a stored graph, with the graph id inlined as a literal.

    Raises:
        ValueError: If ``graph_id`` is not a graph id ('g' + 16 hex digits), so it can never inject SQL.
    """
    if not GRAPH_ID_PATTERN.match(graph_id or ""):
        raise ValueError(f"not an MV graph id: {graph_id!r}")
    query = f"SELECT {_EDGE_COLUMNS} FROM pylovo.mv_graph_edge WHERE graph_id = '{graph_id}'"
    return query + " AND in_settlement" if settlement_only else query


def build_parameters(params: "MvParameters") -> dict[str, Any]:
    """The parameters of a graph build (stored in ``mv_graph.build_parameters``, part of the graph id)."""
    return {
        "excluded_klasse": sorted(str(k) for k in params.graph_excluded_klasse),
        "snap_tolerance_m": float(params.graph_snap_tolerance_m),
        "settlement_buffer_m": float(params.settlement_buffer_m),
        "settlement_spacing_m": float(params.settlement_spacing_m),
        "settlement_min_buildings": int(params.settlement_min_buildings),
        "urban_min_buildings": int(params.urban_min_buildings),
        "settlement_polygon_buffer_m": float(params.settlement_polygon_buffer_m),
        "seam_split_m": SEAM_SPLIT_M,
        "epsg": int(TARGET_EPSG),
        "build_version": GRAPH_BUILD_VERSION,
    }


def graph_id_for(source_table: str, ags: Sequence[str], parameters: dict[str, Any], fingerprint: str) -> str:
    """Content address of a graph: 'g' + 16 hex digits of md5(source | AGS | build parameters | fingerprint)."""
    text = "|".join([source_table, ",".join(sorted(str(a) for a in ags)), json.dumps(parameters, sort_keys=True),
                     fingerprint])
    return "g" + hashlib.md5(text.encode("utf-8")).hexdigest()[:16]


def _source(table: str) -> sql.Identifier:
    if not INFDB_SOURCE_SCHEMA:
        raise MvStageError("the MV routing graph needs the InfDB source tables: set USE_INFDB and "
                           "INFDB_SOURCE_SCHEMA in the .env file")
    return sql.Identifier(INFDB_SOURCE_SCHEMA, table)


def _frame(cur: Any, rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=[d[0] for d in cur.description])


class MvGraphMixin(BaseMixin):
    """Build, attach to and read the persistent MV routing graph."""

    # ------------------------------------------------------------------------------------------------ build
    def _source_srid(self, table: str) -> int:
        self.cur.execute(SRID_SQL.format(table=_source(table)))
        row = self.cur.fetchone()
        if not row or not row[0]:
            raise MvStageError(f"{INFDB_SOURCE_SCHEMA}.{table} has no geometries with an SRID")
        return int(row[0])

    def graph_ags(self, region_wkb: bytes, buffer_m: float) -> list[str]:
        """The municipalities (AGS) whose ways lie within ``buffer_m`` of the planning region.

        The graph always holds whole municipalities, so graphs of overlapping requests are shared when they cover
        the same AGS. The buffer is measured in ``TARGET_EPSG``.

        Args:
            region_wkb: Planning region (WKB, ``TARGET_EPSG``).
            buffer_m: Search buffer (``region_buffer_m``).

        Returns:
            Sorted AGS (8-digit text).
        """
        src_srid = self._source_srid("ways_per_junction")
        self.cur.execute(GRAPH_AGS_SQL.format(ways=_source("ways_per_junction"), epsg=sql.Literal(TARGET_EPSG)),
                         {"region": bytes(region_wkb), "src_srid": src_srid, "buffer_m": float(buffer_m)})
        row = self.cur.fetchone()
        return [str(a) for a in (row[0] if row and row[0] else [])]

    def graph_fingerprint(self, ags: Sequence[str]) -> str:
        """Fingerprint of the source rows of the AGS: ways ``count:length:max changelog``, buildings
        ``count:max changelog``."""
        params = {"ags": list(ags)}
        self.cur.execute(WAYS_FINGERPRINT_SQL.format(ways=_source("ways_per_junction")), params)
        n_ways, length, ways_changelog = self.cur.fetchone()
        self.cur.execute(BUILDINGS_FINGERPRINT_SQL.format(buildings=_source("buildings")), params)
        n_buildings, buildings_changelog = self.cur.fetchone()
        return (f"ways {int(n_ways)}:{int(length)}:{'' if ways_changelog is None else ways_changelog}"
                f"|buildings {int(n_buildings)}:{'' if buildings_changelog is None else buildings_changelog}")

    def ensure_graph(self, region_wkb: bytes, params: "MvParameters") -> str:
        """Return the id of the routing graph of a planning region, building and committing it if it is missing.

        The graph covers every municipality within ``params.region_buffer_m`` of the region (:meth:`graph_ags`). Its
        id is the content address of the source table, the AGS, the build parameters and a fingerprint of the
        source rows (:func:`graph_id_for`); a stored graph with that id is reused as it is.

        **Commits**: the build runs in one transaction under ``pg_advisory_xact_lock(907363, hashtext(graph_id))``
        and commits it (a graph built meanwhile by another process is reused). Call it without uncommitted work of
        your own: a failed build rolls the transaction back and re-raises.

        Args:
            region_wkb: Planning region (WKB, ``TARGET_EPSG``).
            params: MV parameters (graph and settlement constants).

        Returns:
            The graph id ('g' + 16 hex digits).

        Raises:
            MvStageError: If the source has no ways near the region or no SRID.
        """
        ags = self.graph_ags(region_wkb, params.region_buffer_m)
        if not ags:
            raise MvStageError(f"no ways of {INFDB_SOURCE_SCHEMA}.ways_per_junction lie within "
                               f"{params.region_buffer_m:g} m of the planning region")
        source_table = f"{INFDB_SOURCE_SCHEMA}.ways_per_junction"
        parameters = build_parameters(params)
        fingerprint = self.graph_fingerprint(ags)
        graph_id = graph_id_for(source_table, ags, parameters, fingerprint)
        self.cur.execute(GRAPH_EXISTS_SQL, {"g": graph_id})
        if self.cur.fetchone():
            self.logger.info("MV routing graph %s reused (%d municipalities)", graph_id, len(ags))
            return graph_id
        try:
            self._build_graph(graph_id, source_table, ags, parameters, fingerprint)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return graph_id

    def _build_graph(self, graph_id: str, source_table: str, ags: list[str], parameters: dict[str, Any],
                     fingerprint: str) -> None:
        """Run the build statements of :meth:`ensure_graph` in the current transaction (no commit)."""
        started = time.perf_counter()
        g = {"g": graph_id}
        self.cur.execute(LOCK_SQL, {"key": GRAPH_LOCK_KEY, "g": graph_id})
        self.cur.execute(GRAPH_EXISTS_SQL, g)
        if self.cur.fetchone():
            self.logger.info("MV routing graph %s was built meanwhile by another process", graph_id)
            return
        epsg = sql.Literal(TARGET_EPSG)
        ways = _source("ways_per_junction")
        buildings = _source("buildings")
        excluded = list(parameters["excluded_klasse"])
        self.cur.execute(DROP_BUILD_TEMP_SQL)
        self.cur.execute(SOURCE_EDGES_SQL.format(ways=ways, epsg=epsg), {"ags": ags, "excluded": excluded})
        self.cur.execute("CREATE INDEX ON mvg_src USING gist (geom)")
        self.cur.execute("CREATE INDEX ON mvg_src (edge_id)")
        self.cur.execute("ANALYZE mvg_src")
        self._cluster_endpoints(parameters["snap_tolerance_m"])
        self.cur.execute(SEAM_COUNT_SQL, {"seam": SEAM_TOLERANCE_M})
        seams = int(self.cur.fetchone()[0])
        cuts = 0
        if parameters.get("seam_split_m"):
            self.cur.execute(SEAM_CUTS_SQL, {"split": float(parameters["seam_split_m"])})
            cuts = self.cur.rowcount
            self.cur.execute(SEAM_PIECES_SQL)
            for statement in SEAM_REPLACE_SQL:
                self.cur.execute(statement)
            self._cluster_endpoints(parameters["snap_tolerance_m"])
        self.cur.execute(VERTICES_TEMP_SQL)
        self.cur.execute("CREATE INDEX ON mvg_vert (cluster_id)")
        self.cur.execute("ANALYZE mvg_vert")
        self.cur.execute(INSERT_GRAPH_SQL, {**g, "source": source_table, "ags": ags, "excluded": excluded,
                                            "snap": parameters["snap_tolerance_m"],
                                            "params": json.dumps(parameters, sort_keys=True),
                                            "fingerprint": fingerprint})
        self.cur.execute(INSERT_VERTICES_SQL, g)
        self.cur.execute(INSERT_EDGES_SQL, g)
        self.cur.execute(DEGREE_SQL, g)
        self.cur.execute(DROP_ISOLATED_SQL, g)
        self.cur.execute(VERTEX_COMPONENT_SQL, {**g, "edges_sql": edges_sql(graph_id)})
        self.cur.execute(EDGE_COMPONENT_SQL, g)
        # settlement flags from address buildings near the edges
        self.cur.execute(EXTENT_SQL)
        x0, y0, x1, y1 = self.cur.fetchone()
        bld_srid = self._source_srid("buildings")
        self.cur.execute(SETTLEMENT_BUILDINGS_SQL.format(buildings=buildings, epsg=epsg),
                         {"x0": x0 - 100.0, "y0": y0 - 100.0, "x1": x1 + 100.0, "y1": y1 + 100.0,
                          "bld_srid": bld_srid, "uses": list(SETTLEMENT_BUILDING_USES)})
        self.cur.execute("CREATE INDEX ON mvg_bld USING gist (pt)")
        self.cur.execute("ANALYZE mvg_bld")
        sbuf = parameters["settlement_buffer_m"]
        self.cur.execute(EDGE_BUILDINGS_SQL, {**g, "sbuf": sbuf})
        self.cur.execute(IN_SETTLEMENT_SQL, {**g, "smin": parameters["settlement_min_buildings"],
                                             "sspacing": parameters["settlement_spacing_m"]})
        self.cur.execute(SETTLEMENT_ID_SQL, {**g, "settlement_edges_sql": edges_sql(graph_id, settlement_only=True)})
        self.cur.execute(INSERT_SETTLEMENTS_SQL, {**g, "sbuf": sbuf, "spoly": parameters["settlement_polygon_buffer_m"],
                                                  "urban_min": parameters["urban_min_buildings"]})
        self.cur.execute(SETTLEMENT_URBAN_SQL, g)
        self.cur.execute(SUMMARY_SQL, {**g, "seams": seams, "seconds": time.perf_counter() - started})
        for statement in ANALYZE_SQL:
            self.cur.execute(statement)
        self.logger.info("MV routing graph %s built from %d municipalities in %.1f s (%d seam endpoints, "
                         "%d seam cuts)", graph_id, len(ags), time.perf_counter() - started, seams, cuts)

    def _cluster_endpoints(self, snap_m: float) -> None:
        """(Re)create ``mvg_end``: the edge ends of ``mvg_src`` with their DBSCAN cluster."""
        self.cur.execute("DROP TABLE IF EXISTS pg_temp.mvg_end")
        self.cur.execute(ENDPOINTS_SQL, {"snap": snap_m})
        self.cur.execute("CREATE INDEX ON mvg_end (edge_id, end_no)")
        self.cur.execute("CREATE INDEX ON mvg_end USING gist (pt)")
        self.cur.execute("ANALYZE mvg_end")

    # ------------------------------------------------------------------------------------------------ readers
    def region_graph_arrays(self, graph_id: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """All edges of a graph as arrays ``(edge_id, source, target, length_m)``, ordered by ``edge_id``."""
        self.cur.execute(REGION_ARRAYS_SQL, {"g": graph_id})
        rows = self.cur.fetchall()
        if not rows:
            return (np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.float64))
        edge_id, source, target, length = zip(*rows)
        return (np.asarray(edge_id, dtype=np.int64), np.asarray(source, dtype=np.int64),
                np.asarray(target, dtype=np.int64), np.asarray(length, dtype=np.float64))

    def attach_points(self, graph_id: str, points: pd.DataFrame) -> pd.DataFrame:
        """Attach points to their nearest graph edge (KNN on the GiST index), all in one statement.

        Runs in the current transaction (temporary table ``mv_point_tem`` ``ON COMMIT DROP``); no commit.

        Args:
            graph_id: Graph id.
            points: Columns ``key`` (unique int), ``x``, ``y`` (``TARGET_EPSG``).

        Returns:
            One row per key, ordered by key: ``key``, ``edge_id``, ``component``, ``in_settlement``,
            ``settlement_id`` (nullable: NULL outside settlements), ``settlement_urban``, ``fraction`` (along the
            edge from its source), ``offset_m`` (distance to the edge), ``ax``, ``ay`` (closest point on the edge).
            ``edge_id`` is NULL only for an empty graph.
        """
        columns = ["key", "edge_id", "component", "in_settlement", "settlement_id", "settlement_urban", "fraction",
                   "offset_m", "ax", "ay"]
        self.cur.execute("DROP TABLE IF EXISTS pg_temp.mv_point_tem")
        self.cur.execute(ATTACH_TEMP_SQL.format(epsg=sql.Literal(TARGET_EPSG)))
        values = [(int(k), float(x), float(y)) for k, x, y in points[["key", "x", "y"]].itertuples(index=False)]
        if values:
            execute_values(self.cur, "INSERT INTO mv_point_tem (key, geom) VALUES %s", values,
                           template=f"(%s, ST_SetSRID(ST_MakePoint(%s, %s), {int(TARGET_EPSG)}))", page_size=5000)
        self.cur.execute("CREATE INDEX ON mv_point_tem USING gist (geom)")
        self.cur.execute("ANALYZE mv_point_tem")
        self.cur.execute(ATTACH_SQL, {"g": graph_id})
        frame = pd.DataFrame(self.cur.fetchall(), columns=columns)
        for name in ("key", "edge_id", "component", "settlement_id"):
            frame[name] = pd.array(frame[name].tolist(), dtype="Int64")
        for name in ("in_settlement", "settlement_urban"):
            frame[name] = frame[name].fillna(False).astype(bool)
        for name in ("fraction", "offset_m", "ax", "ay"):
            frame[name] = pd.to_numeric(frame[name], errors="coerce").astype(np.float64)
        return frame

    def subgraph_edges(self, graph_id: str, hull_wkb: bytes, buffer_m: float, must: Sequence[int]) -> EdgeSet:
        """The edges of a district subgraph (no geometries) and their vertices.

        Args:
            graph_id: Graph id.
            hull_wkb: Convex hull of the district's node points (WKB, ``TARGET_EPSG``).
            buffer_m: Edges within this distance of the hull are taken
                (``max(subgraph_buffer_m, largest attach offset + 100)``).
            must: Edge ids always taken (the attachment edges of the district's nodes).

        Returns:
            :class:`~pylovo.mv_grid.model.EdgeSet` ordered by ``edge_id``, vertices ordered by ``vertex_id``.
        """
        must_ids = sorted({int(e) for e in must if e is not None and not pd.isna(e)})
        self.cur.execute(SUBGRAPH_EDGES_SQL.format(epsg=sql.Literal(TARGET_EPSG)),
                         {"g": graph_id, "hull": bytes(hull_wkb), "buffer_m": float(buffer_m), "must": must_ids})
        rows = self.cur.fetchall()
        if rows:
            edge_id, source, target, length, in_settlement, settlement_id = (np.asarray(c) for c in zip(*rows))
        else:
            edge_id = source = target = settlement_id = np.zeros(0, np.int64)
            length = np.zeros(0, np.float64)
            in_settlement = np.zeros(0, bool)
        source = source.astype(np.int64)
        target = target.astype(np.int64)
        vertex_ids = np.unique(np.concatenate([source, target]))
        xy = np.zeros((0, 2), dtype=np.float64)
        if len(vertex_ids):
            self.cur.execute(SUBGRAPH_VERTICES_SQL, {"g": graph_id, "vertex_ids": vertex_ids.tolist()})
            vrows = self.cur.fetchall()
            found = np.asarray([r[0] for r in vrows], dtype=np.int64)
            if len(found) != len(vertex_ids) or not np.array_equal(found, vertex_ids):
                raise MvStageError(f"graph {graph_id} misses vertices of its edges")
            xy = np.asarray([(r[1], r[2]) for r in vrows], dtype=np.float64).reshape(-1, 2)
        return EdgeSet(edge_id=edge_id.astype(np.int64), source=source, target=target,
                       length_m=length.astype(np.float64), in_settlement=in_settlement.astype(bool),
                       settlement_id=settlement_id.astype(np.int64), vertex_id=vertex_ids.astype(np.int64),
                       vertex_xy=xy)

    def edge_geometries(self, graph_id: str, edge_ids: Sequence[int]) -> dict[int, Any]:
        """Geometries of graph edges (shapely LineStrings in ``TARGET_EPSG``, oriented source -> target)."""
        import shapely

        ids = sorted({int(e) for e in edge_ids})
        if not ids:
            return {}
        self.cur.execute(EDGE_GEOMETRIES_SQL, {"g": graph_id, "ids": ids})
        return {int(edge_id): shapely.from_wkb(bytes(wkb)) for edge_id, wkb in self.cur.fetchall()}

    def graph_qa(self, graph_id: str) -> dict[str, pd.DataFrame]:
        """QA tables of a graph: ``summary``, km per ``klasse``, ``components``, ``degrees``, ``settlements``
        (urban and rural) and the ``largest_settlements``."""
        out: dict[str, pd.DataFrame] = {}
        for name, query in QA_SQL.items():
            self.cur.execute(query, {"g": graph_id})
            out[name] = _frame(self.cur, self.cur.fetchall())
        return out
