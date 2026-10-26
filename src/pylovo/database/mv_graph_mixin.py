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
   settlement_spacing_m)`` address buildings lie within ``settlement_buffer_m``, or if its midpoint lies in built-up
   OSM land use (:data:`SETTLEMENT_LANDUSE_TYPES`, when ``INFDB_OSM_SCHEMA`` has ``landuse_polygon``); settlements
   are the connected components of the settlement edges, urban with at least ``urban_min_buildings`` buildings;
5. summary columns of ``pylovo.mv_graph`` (counts, largest component share, settlement share, seam diagnostic).

The MV overhead lines of ``INFDB_OSM_SCHEMA.infrastructure_line`` (rules of
:mod:`pylovo.mv_grid.osm_lines`) are added after step 2 as edges of ``klasse`` :data:`OSM_LINE_KLASSE`. Where an OSM
line crosses a street both are split, so the crossing is a common vertex, and every loose end of an OSM line gets a
straight connector (:data:`OSM_CONNECTOR_KLASSE`) to the nearest street vertex within ``osm_line_snap_m``. The OSM
edges are never in a settlement, never an attachment edge of a node and not part of the supply-area routing; only
the routes of the MV lines use them.

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

from pylovo.config_loader import INFDB_OSM_SCHEMA, INFDB_SOURCE_SCHEMA, TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin
from pylovo.mv_grid.model import EdgeSet
from pylovo.mv_grid.osm_lines import OVERHEAD_SUBTYPES, is_mv_overhead_line
from pylovo.mv_grid.parameters import MvStageError

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import MvParameters

#: Advisory lock key of graph builds (next to 907360 setup, 907361 PLZ, 907362 MV variant).
GRAPH_LOCK_KEY: int = 907363
#: Version of the build procedure; part of the graph id, bumped whenever the build changes its result.
GRAPH_BUILD_VERSION: int = 2
#: Seam diagnostic: a lone edge end this close to the interior of another edge (m).
SEAM_TOLERANCE_M: float = 0.5
#: Seam repair: edges are split where an edge end lies this close to their interior (m), so T-junctions at
#: municipality borders (InfDB splits ways per AGS) become graph vertices; ``None`` keeps the seams. Part of the
#: build parameters (and so of the graph id).
SEAM_SPLIT_M: float | None = 0.5
#: Building classes of the settlement flags (the classes of the LV building fetch, ``infdb_client.py``).
SETTLEMENT_BUILDING_USES: tuple[str, ...] = ("Commercial", "Public", "Residential", "Mixed", "Unknown")
#: Built-up OSM land use (``osm_type`` of ``INFDB_OSM_SCHEMA.landuse_polygon``): a street edge whose midpoint lies in
#: such a polygon is in a settlement whatever its address-building count. Industrial and commercial zones have large
#: plots (about one address building per 200 m of street), so the building rule alone leaves them as open field and
#: their MV customers would be linked by overhead lines. Part of the build parameters (and so of the graph id).
SETTLEMENT_LANDUSE_TYPES: tuple[str, ...] = ("commercial", "industrial", "residential", "retail")
#: Graph ids are 'g' + 16 hex digits of an md5 (safe to inline as SQL literals).
GRAPH_ID_PATTERN = re.compile(r"^g[0-9a-f]{16}$")
#: ``klasse`` of the edges from OSM MV overhead lines ...
OSM_LINE_KLASSE: str = "osm_mv_line"
#: ... and of the straight connectors from their loose ends to the street graph.
OSM_CONNECTOR_KLASSE: str = "osm_mv_connector"
_OSM_KLASSEN = (OSM_LINE_KLASSE, OSM_CONNECTOR_KLASSE)

_EDGE_COLUMNS = "edge_id AS id, source, target, length_m AS cost, length_m AS reverse_cost"

# ---------------------------------------------------------------------------------------------------- SQL templates
# {ways}, {buildings}: sql.Identifier(INFDB_SOURCE_SCHEMA, ...); {epsg}: sql.Literal(TARGET_EPSG). The statements on
# the source tables run through the source reader (pylovo.infdb.source_reader): in the pylovo database, or with
# USE_EXTERNAL_BASEDATA on a separate read-only connection; they filter by index only (AGS, bounding boxes) and the
# exact tests against the region run in the pylovo database.

# The municipalities of the graph: the AGS whose ways intersect the buffered planning region. The buffer of the
# constant region is computed once (in the source SRID) and tested as a prepared geometry; ST_DWithin against a large
# multipolygon evaluates the distance per row and took 35 times longer on the test regions. Per AGS the extent of its
# ways inside the bounding box of that buffer is read from the source; an AGS whose extent misses the buffer has no
# way in it, one whose extent lies inside it has all of them in it, and only the ways of the AGS between are read
# and tested one by one.
AGS_REGION_SQL = sql.SQL("""
    CREATE TEMP TABLE mv_ags_region ON COMMIT DROP AS
    SELECT ST_Transform(ST_Buffer(ST_GeomFromWKB(%(region)s, {epsg}), %(buffer_m)s), %(src_srid)s) AS g
""")
AGS_REGION_BOX_SQL = "SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g) FROM mv_ags_region WHERE NOT ST_IsEmpty(g)"

AGS_EXTENTS_SQL = sql.SQL("""
    SELECT x.ags, ST_XMin(x.b), ST_YMin(x.b), ST_XMax(x.b), ST_YMax(x.b)
    FROM (SELECT w.ags::text AS ags, ST_Extent(w.geom) AS b
          FROM {ways} w
          WHERE w.geom && ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, %(src_srid)s) AND w.ags IS NOT NULL
          GROUP BY w.ags::text) x
""")

AGS_EXTENT_TEMP_SQL = """
    CREATE TEMP TABLE mv_ags_extent ON COMMIT DROP AS
    SELECT * FROM unnest(%(ags)s::text[], %(x0)s::float8[], %(y0)s::float8[], %(x1)s::float8[], %(y1)s::float8[])
                  AS e(ags, x0, y0, x1, y1)
"""

AGS_CLASSES_SQL = """
    SELECT e.ags, CASE WHEN NOT ST_Intersects(r.g, e.b) THEN 0 WHEN ST_Contains(r.g, e.b) THEN 1 ELSE 2 END
    FROM (SELECT ags, ST_SetSRID(ST_MakeBox2D(ST_Point(x0, y0), ST_Point(x1, y1))::geometry, %(src_srid)s) AS b
          FROM mv_ags_extent) e, mv_ags_region r
"""

AGS_WAYS_SQL = sql.SQL("""
    SELECT w.ags, w.geom FROM {ways} w
    WHERE w.ags::text = ANY(%(ags)s) AND w.geom && ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, %(src_srid)s)
""")
AGS_EXACT_SQL = "SELECT DISTINCT w.ags::text FROM mvg_ags_ways w, mv_ags_region r WHERE ST_Intersects(w.geom, r.g)"
DROP_AGS_TEMP_SQL = "DROP TABLE IF EXISTS pg_temp.mv_ags_region, pg_temp.mv_ags_extent, pg_temp.mvg_ags_ways"

# also the extent of the ways (source SRID), which bounds the land use and OSM lines of the fingerprint
WAYS_FINGERPRINT_SQL = sql.SQL("""
    SELECT count(*), COALESCE(round(sum(ST_Length(w.geom))::numeric), 0), max(w.changelog_id),
           ST_XMin(ST_Extent(w.geom)), ST_YMin(ST_Extent(w.geom)), ST_XMax(ST_Extent(w.geom)),
           ST_YMax(ST_Extent(w.geom))
    FROM {ways} w
    WHERE w.ags::text = ANY(%(ags)s)
""")

# The extent of the graph's ways (source SRID) as a box in another SRID (EWKB), the prefilter of the source rows in
# the extent: through TARGET_EPSG, widened by %(pad)s there, as the build widens the extent of its edges.
EXTENT_BOX_SQL = sql.SQL("""
    SELECT ST_AsEWKB(ST_Transform(ST_Expand(ST_Transform(ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, %(srid)s),
                                                         {epsg}), %(pad)s), %(to_srid)s))
""")

BUILDINGS_FINGERPRINT_SQL = sql.SQL("""
    SELECT count(*), max(b.changelog_id)
    FROM {buildings} b
    WHERE b.gemeindeschluessel::text = ANY(%(ags)s)
""")

GRAPH_EXISTS_SQL = "SELECT 1 FROM pylovo.mv_graph WHERE graph_id = %(g)s"

LOCK_SQL = "SELECT pg_advisory_xact_lock(%(key)s, hashtext(%(g)s))"
#: Sessions that hold the lock of an MV variant (key 907362 of ``MvResultMixin.try_lock_variant``): an MV run.
ACTIVE_MV_RUNS_SQL = ("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted AND classid = 907362 "
                      "AND objsubid = 2 AND pid <> pg_backend_pid()")
#: The routing graphs that no MV variant references, each under its build lock (a graph being built is kept).
PRUNE_GRAPHS_SQL = """DELETE FROM pylovo.mv_graph g
    WHERE NOT EXISTS (SELECT 1 FROM pylovo.mv_variant v WHERE v.graph_id = g.graph_id)
      AND pg_try_advisory_xact_lock(%(key)s, hashtext(g.graph_id))
    RETURNING g.graph_id"""

DROP_BUILD_TEMP_SQL = ("DROP TABLE IF EXISTS pg_temp.mvg_src, pg_temp.mvg_end, pg_temp.mvg_vert, pg_temp.mvg_bld, "
                       "pg_temp.mvg_cut, pg_temp.mvg_split, pg_temp.mvg_lu, pg_temp.mvg_osm, pg_temp.mvg_conn, "
                       "pg_temp.mvg_src_raw, pg_temp.mvg_bld_raw, pg_temp.mvg_lu_raw")

# the source rows of the graph, staged into pg_temp.mvg_src_raw (source columns and types) ...
WAYS_STAGE_COLUMNS: tuple[str, ...] = ("ags", "id", "klasse", "objektart", "geom")
WAYS_STAGE_SQL = sql.SQL("""
    SELECT w.ags, w.id, w.klasse, w.objektart, w.geom
    FROM {ways} w
    WHERE w.ags::text = ANY(%(ags)s) AND w.geom IS NOT NULL
      AND NOT (COALESCE(btrim(w.klasse), '') = ANY(%(excluded)s))
""")

# ... and turned into the edges ({ways}: pg_temp.mvg_src_raw)
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

# overhead power ways with their tags inside the extent of the street edges ({osm}: INFDB_OSM_SCHEMA)
OSM_LINES_SQL = sql.SQL("""
    SELECT l.osm_id, l.osm_subtype, t.tags, ST_AsBinary(ST_Force2D(ST_Transform(l.geom, {epsg}))) AS wkb
    FROM {osm}.infrastructure_line l
    LEFT JOIN {osm}.tags t ON t.geom_type = 'W' AND t.osm_id = l.osm_id
    WHERE l.osm_type = 'power' AND l.osm_subtype = ANY(%(subtypes)s)
      AND l.geom && ST_Transform(ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, {epsg}), %(osm_srid)s)
    ORDER BY l.osm_id
""")

# the overhead power ways in the extent of the graph's ways (%(box)s: EWKB in the SRID of the OSM layer)
OSM_FINGERPRINT_SQL = sql.SQL("""
    SELECT count(*), COALESCE(round(sum(ST_Length(l.geom))::numeric), 0), max(l.osm_id)
    FROM {osm}.infrastructure_line l
    WHERE l.osm_type = 'power' AND l.osm_subtype = ANY(%(subtypes)s) AND l.geom && ST_GeomFromEWKB(%(box)s)
""")

OSM_INSERT_SQL = """
    INSERT INTO mvg_src (edge_id, ags, src_id, klasse, objektart, geom)
    SELECT (SELECT max(edge_id) FROM mvg_src) + row_number() OVER (ORDER BY o.osm_id, d.path),
           NULL, 'way/' || o.osm_id, %(klasse)s, o.subtype, d.geom
    FROM mvg_osm o
    CROSS JOIN LATERAL ST_Dump(o.geom) d
    WHERE GeometryType(d.geom) = 'LINESTRING' AND ST_Length(d.geom) > 0
"""

# both an OSM line and a street are cut where they cross (points of their intersection only, no overlaps)
OSM_CROSSING_CUTS_SQL = """
    INSERT INTO mvg_cut (host, f, pt)
    WITH x AS (SELECT o.edge_id AS o_id, s.edge_id AS s_id, d.geom AS pt
               FROM mvg_src o
               JOIN mvg_src s ON COALESCE(s.klasse, '') <> ALL(%(osm)s) AND ST_Intersects(o.geom, s.geom)
               CROSS JOIN LATERAL ST_Dump(ST_Intersection(o.geom, s.geom)) d
               WHERE o.klasse = %(line)s AND GeometryType(d.geom) = 'POINT'),
         h AS (SELECT o_id AS host, pt FROM x UNION ALL SELECT s_id, pt FROM x)
    SELECT h.host, ST_LineLocatePoint(e.geom, h.pt), h.pt
    FROM h JOIN mvg_src e ON e.edge_id = h.host
"""

# an OSM line that ends on the interior of another OSM line (a branch: OSM ways are not split at junctions) cuts it
OSM_JUNCTION_CUTS_SQL = """
    INSERT INTO mvg_cut (host, f, pt)
    SELECT DISTINCT ON (p.edge_id, p.end_no) h.edge_id, ST_LineLocatePoint(h.geom, p.pt), p.pt
    FROM mvg_end p
    JOIN mvg_src pe ON pe.edge_id = p.edge_id AND pe.klasse = %(line)s
    JOIN mvg_src h ON h.edge_id <> p.edge_id AND h.klasse = %(line)s AND ST_DWithin(h.geom, p.pt, %(tol)s)
    WHERE ST_Distance(ST_StartPoint(h.geom), p.pt) > %(tol)s AND ST_Distance(ST_EndPoint(h.geom), p.pt) > %(tol)s
    ORDER BY p.edge_id, p.end_no, ST_Distance(h.geom, p.pt), h.edge_id
"""

# a loose end of an OSM line (an end cluster of its own) and its nearest street point within %(snap)s
OSM_LOOSE_ENDS_SQL = """
    CREATE TEMP TABLE mvg_conn ON COMMIT DROP AS
    SELECT l.edge_id, l.end_no, l.pt, e.src_id, n.host, n.f, n.cp
    FROM (SELECT p.edge_id, p.end_no, p.pt
          FROM mvg_end p
          WHERE p.cluster_id IN (SELECT cluster_id FROM mvg_end GROUP BY cluster_id HAVING count(*) = 1)) l
    JOIN mvg_src e ON e.edge_id = l.edge_id AND e.klasse = %(line)s
    CROSS JOIN LATERAL (SELECT s.edge_id AS host, ST_LineLocatePoint(s.geom, l.pt) AS f,
                               ST_ClosestPoint(s.geom, l.pt) AS cp
                        FROM mvg_src s
                        WHERE COALESCE(s.klasse, '') <> ALL(%(osm)s) AND ST_DWithin(s.geom, l.pt, %(snap)s)
                        ORDER BY ST_Distance(s.geom, l.pt), s.edge_id LIMIT 1) n
"""

# the straight connectors from the loose ends to their street points (cut into the streets before)
OSM_CONNECTORS_SQL = """
    INSERT INTO mvg_src (edge_id, ags, src_id, klasse, objektart, geom)
    SELECT (SELECT max(edge_id) FROM mvg_src) + row_number() OVER (ORDER BY c.edge_id, c.end_no),
           NULL, c.src_id, %(connector)s, NULL, ST_MakeLine(c.pt, c.cp)
    FROM mvg_conn c
    WHERE ST_Distance(c.pt, c.cp) > 0
"""

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

# an envelope in TARGET_EPSG as EWKB in the SRID of a source table (the prefilter of its rows)
BOX_SQL = sql.SQL("SELECT ST_AsEWKB(ST_Transform(ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, {epsg}), %(srid)s))")

# the point of every address building in the box (staged into pg_temp.mvg_bld_raw) ...
SETTLEMENT_POINTS_SQL = sql.SQL("""
    SELECT COALESCE(b.centroid, ST_PointOnSurface(b.geom)) AS pt
    FROM {buildings} b
    WHERE b.geom && ST_GeomFromEWKB(%(box)s)
      AND b.building_use = ANY(%(uses)s)
      AND COALESCE(b.building_use_id, '') <> '31001_2523'
      AND COALESCE(b.street, '') <> '' AND COALESCE(b.house_number, '') <> ''
""")

# ... in TARGET_EPSG
SETTLEMENT_BUILDINGS_SQL = sql.SQL("""
    CREATE TEMP TABLE mvg_bld ON COMMIT DROP AS
    SELECT row_number() OVER ()::bigint AS bid, ST_Transform(r.pt, {epsg}) AS pt
    FROM pg_temp.mvg_bld_raw r
""")

EDGE_BUILDINGS_SQL = """
    UPDATE pylovo.mv_graph_edge e SET n_buildings = c.n
    FROM (SELECT e2.edge_id, count(b.bid) AS n
          FROM pylovo.mv_graph_edge e2 LEFT JOIN mvg_bld b ON ST_DWithin(b.pt, e2.geom, %(sbuf)s)
          WHERE e2.graph_id = %(g)s GROUP BY e2.edge_id) c
    WHERE e.graph_id = %(g)s AND e.edge_id = c.edge_id
"""

# the built-up land use in the extent of the graph's ways (%(box)s: EWKB in the SRID of the layer)
LANDUSE_FINGERPRINT_SQL = sql.SQL("""
    SELECT count(*) FROM {landuse} l WHERE l.osm_type = ANY(%(types)s) AND l.geom && ST_GeomFromEWKB(%(box)s)
""")

# {landuse}: sql.Identifier(INFDB_OSM_SCHEMA, 'landuse_polygon'), staged into pg_temp.mvg_lu_raw ...
LANDUSE_STAGE_SQL = sql.SQL("""
    SELECT l.geom FROM {landuse} l WHERE l.osm_type = ANY(%(types)s) AND l.geom && ST_GeomFromEWKB(%(box)s)
""")
# ... in TARGET_EPSG; empty table without land use
LANDUSE_TEMP_SQL = sql.SQL("""
    CREATE TEMP TABLE mvg_lu ON COMMIT DROP AS SELECT ST_Transform(r.geom, {epsg}) AS geom FROM pg_temp.mvg_lu_raw r
""")
NO_LANDUSE_TEMP_SQL = "CREATE TEMP TABLE mvg_lu (geom geometry) ON COMMIT DROP"

IN_SETTLEMENT_SQL = """
    UPDATE pylovo.mv_graph_edge e
    SET in_settlement = COALESCE(e.klasse, '') <> ALL(%(osm)s)
                        AND (e.n_buildings >= GREATEST(%(smin)s, e.length_m / %(sspacing)s)
                             OR EXISTS (SELECT 1 FROM mvg_lu l
                                        WHERE ST_Intersects(l.geom, ST_LineInterpolatePoint(e.geom, 0.5))))
    WHERE e.graph_id = %(g)s
"""

SETTLEMENT_ID_SQL = """
    UPDATE pylovo.mv_graph_edge e SET settlement_id = c.component
    FROM pgr_connectedComponents(%(settlement_edges_sql)s) c
    WHERE e.graph_id = %(g)s AND e.in_settlement AND c.node = e.source
"""

# The edges of a settlement are summed and collected in edge order, so the last digits of its length do not depend
# on the plan.
INSERT_SETTLEMENTS_SQL = """
    WITH sb AS (SELECT e.settlement_id, count(DISTINCT b.bid) AS n
                FROM pylovo.mv_graph_edge e JOIN mvg_bld b ON ST_DWithin(b.pt, e.geom, %(sbuf)s)
                WHERE e.graph_id = %(g)s AND e.in_settlement GROUP BY e.settlement_id),
         sg AS (SELECT settlement_id, sum(length_m ORDER BY edge_id) / 1000.0 AS km,
                       ST_Multi(ST_Buffer(ST_Collect(geom ORDER BY edge_id), %(spoly)s, 'quad_segs=4')) AS geom
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
    SELECT edge_id, source, target, length_m FROM pylovo.mv_graph_edge
    WHERE graph_id = %(g)s AND COALESCE(klasse, '') <> ALL(%(osm)s)
    ORDER BY edge_id
"""

SETTLEMENT_POLYGONS_SQL = sql.SQL("""
    SELECT s.settlement_id, s.urban, ST_AsBinary(s.geom) FROM pylovo.mv_graph_settlement s
    WHERE s.graph_id = %(g)s AND s.geom IS NOT NULL AND ST_Intersects(s.geom, ST_GeomFromWKB(%(region)s, {epsg}))
    ORDER BY s.settlement_id
""")

OSM_LINE_GEOMS_SQL = sql.SQL("""
    SELECT ST_AsBinary(e.geom) FROM pylovo.mv_graph_edge e
    WHERE e.graph_id = %(g)s AND e.klasse = %(line)s AND ST_Intersects(e.geom, ST_GeomFromWKB(%(region)s, {epsg}))
    ORDER BY e.edge_id
""")

EDGE_AGS_SQL = """
    SELECT edge_id, ags FROM pylovo.mv_graph_edge
    WHERE graph_id = %(g)s AND ags IS NOT NULL AND COALESCE(klasse, '') <> ALL(%(osm)s)
    ORDER BY edge_id
"""

ATTACH_TEMP_SQL = sql.SQL(
    "CREATE TEMP TABLE mv_point_tem (key bigint PRIMARY KEY, geom geometry(Point, {epsg}) NOT NULL) ON COMMIT DROP"
)

# Of equally near edges (a point at a vertex shared by several edges) the lowest edge id wins: otherwise the order of
# the index scan decides, which changes with the insert order of the graph and with vacuum.
ATTACH_SQL = """
    SELECT p.key, e.edge_id, e.component, e.in_settlement, e.settlement_id, e.settlement_urban,
           ST_LineLocatePoint(e.geom, p.geom) AS fraction, ST_Distance(e.geom, p.geom) AS offset_m,
           ST_X(ST_ClosestPoint(e.geom, p.geom)) AS ax, ST_Y(ST_ClosestPoint(e.geom, p.geom)) AS ay
    FROM mv_point_tem p
    LEFT JOIN LATERAL (SELECT e.edge_id, e.component, e.in_settlement, e.settlement_id, e.settlement_urban, e.geom
                       FROM pylovo.mv_graph_edge e
                       WHERE e.graph_id = %(g)s AND COALESCE(e.klasse, '') <> ALL(%(osm)s)
                       ORDER BY e.geom <-> p.geom, e.edge_id LIMIT 1) e ON true
    ORDER BY p.key
"""

SUBGRAPH_EDGES_SQL = sql.SQL("""
    SELECT e.edge_id, e.source, e.target, e.length_m, e.in_settlement, COALESCE(e.settlement_id, -1),
           COALESCE(e.klasse = %(line)s, false)
    FROM pylovo.mv_graph_edge e
    WHERE e.graph_id = %(g)s
      AND (ST_DWithin(e.geom, ST_GeomFromWKB(%(hull)s, {epsg}), %(buffer_m)s) OR e.edge_id = ANY(%(must)s))
    ORDER BY e.edge_id
""")

# The edges within the buffer of any of many points (the ring members of a whole variant for its ties and links):
# each point finds its edges through the edge index, which gives the edges of one ST_DWithin test against the
# MultiPoint of all points, while that test computes the distance to every point for every edge of the graph.
SUBGRAPH_POINTS_SQL = sql.SQL("""
    CREATE TEMP TABLE mv_subgraph_point ON COMMIT DROP AS
    SELECT ST_SetSRID(ST_MakePoint(p.x, p.y), {epsg}) AS pt FROM unnest(%(x)s::float8[], %(y)s::float8[]) AS p(x, y)
""")

SUBGRAPH_NEAR_SQL = """
    SELECT e.edge_id, e.source, e.target, e.length_m, e.in_settlement, COALESCE(e.settlement_id, -1),
           COALESCE(e.klasse = %(line)s, false)
    FROM pylovo.mv_graph_edge e
    WHERE e.graph_id = %(g)s
      AND (e.edge_id IN (SELECT n.edge_id FROM mv_subgraph_point p
                         JOIN pylovo.mv_graph_edge n ON n.graph_id = %(g)s AND ST_DWithin(n.geom, p.pt, %(buffer_m)s))
           OR e.edge_id = ANY(%(must)s))
    ORDER BY e.edge_id
"""

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
        "settlement_landuse": sorted(SETTLEMENT_LANDUSE_TYPES),
        "seam_split_m": SEAM_SPLIT_M,
        "epsg": int(TARGET_EPSG),
        "build_version": GRAPH_BUILD_VERSION,
        "osm_mv_lines": True,
        "osm_line_snap_m": float(params.osm_line_snap_m),
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
        schema, _ = _source(table).strings                 # raises without INFDB_SOURCE_SCHEMA
        srid = self.source_reader().srid(schema, table)
        if not srid:
            raise MvStageError(f"{INFDB_SOURCE_SCHEMA}.{table} has no geometries with an SRID")
        return int(srid)

    def _osm_layer_srid(self, table: str) -> int:
        """The SRID of an OSM layer, ``TARGET_EPSG`` without geometries."""
        return int(self.source_reader().srid(INFDB_OSM_SCHEMA, table) or TARGET_EPSG)

    def graph_ags(self, region_wkb: bytes, buffer_m: float) -> list[str]:
        """The municipalities (AGS) whose ways lie within ``buffer_m`` of the planning region.

        The graph always holds whole municipalities, so graphs of overlapping requests are shared when they cover
        the same AGS. The buffer is measured in ``TARGET_EPSG``. The source is read through the source reader (see
        :data:`AGS_REGION_SQL`): the extent of the ways per AGS near the buffer, and the ways themselves only of the
        AGS whose extent crosses the border of the buffer; the tests against the buffer run in the pylovo database.

        Args:
            region_wkb: Planning region (WKB, ``TARGET_EPSG``).
            buffer_m: Search buffer (``region_buffer_m``).

        Returns:
            Sorted AGS (8-digit text).
        """
        reader = self.source_reader()
        src_srid = self._source_srid("ways_per_junction")
        ways = _source("ways_per_junction")
        self.cur.execute(DROP_AGS_TEMP_SQL)
        self.cur.execute(AGS_REGION_SQL.format(epsg=sql.Literal(TARGET_EPSG)),
                         {"region": bytes(region_wkb), "src_srid": src_srid, "buffer_m": float(buffer_m)})
        self.cur.execute(AGS_REGION_BOX_SQL)
        box = self.cur.fetchone()
        if not box or box[0] is None:
            self.cur.execute(DROP_AGS_TEMP_SQL)
            return []
        envelope = {"x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3], "src_srid": src_srid}
        extents = reader.fetchall(AGS_EXTENTS_SQL.format(ways=ways), envelope)
        columns = list(zip(*extents)) if extents else [()] * 5
        self.cur.execute(AGS_EXTENT_TEMP_SQL, {name: list(values) for name, values in
                                               zip(("ags", "x0", "y0", "x1", "y1"), columns)})
        self.cur.execute(AGS_CLASSES_SQL, {"src_srid": src_srid})
        classes = self.cur.fetchall()
        inside = {str(a) for a, c in classes if c == 1}
        border = sorted(str(a) for a, c in classes if c == 2)
        if border:
            reader.stage("mvg_ags_ways", AGS_WAYS_SQL.format(ways=ways), {**envelope, "ags": border},
                         lambda: reader.column_types(INFDB_SOURCE_SCHEMA, "ways_per_junction", ("ags", "geom")))
            self.cur.execute(AGS_EXACT_SQL)
            inside.update(str(row[0]) for row in self.cur.fetchall())
        self.cur.execute(DROP_AGS_TEMP_SQL)
        return sorted(inside)

    def _landuse_table(self) -> sql.Identifier | None:
        """``INFDB_OSM_SCHEMA.landuse_polygon`` if it exists, else None (settlements from address buildings only)."""
        if not self.source_reader().table_exists(INFDB_OSM_SCHEMA, "landuse_polygon"):
            return None
        return sql.Identifier(INFDB_OSM_SCHEMA, "landuse_polygon")

    def _osm_lines_available(self) -> bool:
        """Whether ``INFDB_OSM_SCHEMA`` has ``infrastructure_line`` and ``tags`` (the OSM MV lines)."""
        reader = self.source_reader()
        return reader.table_exists(INFDB_OSM_SCHEMA, "infrastructure_line") and reader.table_exists(
            INFDB_OSM_SCHEMA, "tags")

    def _extent_box(self, extent: Sequence[float], srid: int, to_srid: int, pad_m: float) -> bytes:
        """The extent of the graph's ways (``srid``) widened by ``pad_m`` in ``TARGET_EPSG``, as EWKB in ``to_srid``."""
        x0, y0, x1, y1 = extent
        self.cur.execute(EXTENT_BOX_SQL.format(epsg=sql.Literal(TARGET_EPSG)),
                         {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "srid": int(srid), "to_srid": int(to_srid),
                          "pad": float(pad_m)})
        return bytes(self.cur.fetchone()[0])

    def graph_fingerprint(self, ags: Sequence[str], osm_lines: bool = False) -> str:
        """Fingerprint of the source rows of the AGS: ways ``count:length:max changelog``, buildings
        ``count:max changelog``, and the number of built-up OSM land-use polygons in the extent of the ways
        (``none`` without the table); with ``osm_lines`` also the overhead power ways of the OSM schema in that
        extent ``count:length:max id`` (``none`` without the tables).

        The land use and the OSM lines are counted in the extent of the ways (with the margins of the build), not in
        the whole table, so a change of the OSM data elsewhere keeps the graph id."""
        reader = self.source_reader()
        params = {"ags": list(ags)}
        n_ways, length, ways_changelog, *extent = reader.fetchone(
            WAYS_FINGERPRINT_SQL.format(ways=_source("ways_per_junction")), params)
        n_buildings, buildings_changelog = reader.fetchone(
            BUILDINGS_FINGERPRINT_SQL.format(buildings=_source("buildings")), params)
        src_srid = self._source_srid("ways_per_junction") if extent[0] is not None else None
        landuse = self._landuse_table()
        n_landuse = "none"
        if landuse is not None and src_srid is not None:
            box = self._extent_box(extent, src_srid, self._osm_layer_srid("landuse_polygon"), 100.0)
            row = reader.fetchone(LANDUSE_FINGERPRINT_SQL.format(landuse=landuse),
                                  {"types": list(SETTLEMENT_LANDUSE_TYPES), "box": box})
            n_landuse = str(int(row[0])) if row and row[0] is not None else "none"
        elif landuse is not None:
            n_landuse = "0"
        text = (f"ways {int(n_ways)}:{int(length)}:{'' if ways_changelog is None else ways_changelog}"
                f"|buildings {int(n_buildings)}:{'' if buildings_changelog is None else buildings_changelog}"
                f"|landuse {n_landuse}")
        if osm_lines:
            lines = "none"
            if self._osm_lines_available() and src_srid is not None:
                box = self._extent_box(extent, src_srid, self._osm_layer_srid("infrastructure_line"), 0.0)
                n, km, top = reader.fetchone(OSM_FINGERPRINT_SQL.format(osm=sql.Identifier(INFDB_OSM_SCHEMA)),
                                             {"subtypes": list(OVERHEAD_SUBTYPES), "box": box}) or (0, 0, None)
                lines = f"{int(n)}:{int(km)}:{'' if top is None else top}"
            elif self._osm_lines_available():
                lines = "0:0:"
            text += f"|osm_lines {lines}"
        return text

    def prune_graphs(self) -> list[str]:
        """Delete the routing graphs that no MV variant references, with their vertices, edges and settlements;
        commits.

        A graph is shared by every variant with the same inputs and kept when variants are deleted. This removes
        the ones left over. Each graph is deleted under its build lock (``pg_try_advisory_xact_lock(907363,
        hashtext(graph_id))``); nothing is deleted while an MV run holds a variant lock, because ``pylovo-mv
        generate`` builds or reuses its graph before it registers the variant.

        Returns:
            The ids of the deleted graphs, sorted.

        Raises:
            RuntimeError: An MV run (``pylovo-mv generate`` or ``couple``) is active.
        """
        try:
            self.cur.execute(ACTIVE_MV_RUNS_SQL)
            if int(self.cur.fetchone()[0]):
                raise RuntimeError("an MV run (pylovo-mv generate or couple) is active: prune the routing graphs "
                                   "when it has finished")
            self.cur.execute(PRUNE_GRAPHS_SQL, {"key": GRAPH_LOCK_KEY})
            deleted = sorted(str(row[0]) for row in self.cur.fetchall())
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        return deleted

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
        fingerprint = self.graph_fingerprint(ags, osm_lines=bool(parameters.get("osm_mv_lines")))
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
        reader = self.source_reader()
        ways = _source("ways_per_junction")
        buildings = _source("buildings")
        excluded = list(parameters["excluded_klasse"])
        self.cur.execute(DROP_BUILD_TEMP_SQL)
        n_rows = reader.stage("mvg_src_raw", WAYS_STAGE_SQL.format(ways=ways), {"ags": ags, "excluded": excluded},
                              lambda: reader.column_types(INFDB_SOURCE_SCHEMA, "ways_per_junction",
                                                          WAYS_STAGE_COLUMNS))
        self.logger.info("MV routing graph %s: %d ways read from %s", graph_id, n_rows, reader.where)
        self.cur.execute(SOURCE_EDGES_SQL.format(ways=sql.Identifier("pg_temp", "mvg_src_raw"), epsg=epsg),
                         {"ags": ags, "excluded": excluded})
        self.cur.execute("DROP TABLE pg_temp.mvg_src_raw")
        self.cur.execute("CREATE INDEX ON mvg_src USING gist (geom)")
        self.cur.execute("CREATE INDEX ON mvg_src (edge_id)")
        self.cur.execute("ANALYZE mvg_src")
        self._cluster_endpoints(parameters["snap_tolerance_m"])
        self.logger.info("MV routing graph %s: edge ends clustered (%.0f s)", graph_id, time.perf_counter() - started)
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
        if parameters.get("osm_mv_lines"):
            self._add_osm_lines(parameters["snap_tolerance_m"], float(parameters["osm_line_snap_m"]))
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
        # Fresh statistics for the new rows: with the statistics of an earlier graph the planner estimates one edge
        # for this graph_id and the building count below ran for many minutes instead of seconds.
        for statement in ANALYZE_SQL[:2]:
            self.cur.execute(statement)
        self.logger.info("MV routing graph %s: vertices and edges stored (%.0f s)", graph_id,
                         time.perf_counter() - started)
        self.cur.execute(VERTEX_COMPONENT_SQL, {**g, "edges_sql": edges_sql(graph_id)})
        self.cur.execute(EDGE_COMPONENT_SQL, g)
        # settlement flags from address buildings near the edges
        self.cur.execute(EXTENT_SQL)
        x0, y0, x1, y1 = self.cur.fetchone()
        padded = {"x0": x0 - 100.0, "y0": y0 - 100.0, "x1": x1 + 100.0, "y1": y1 + 100.0}
        self.cur.execute(BOX_SQL.format(epsg=epsg), {**padded, "srid": self._source_srid("buildings")})
        bld_box = bytes(self.cur.fetchone()[0])
        n_rows = reader.stage("mvg_bld_raw", SETTLEMENT_POINTS_SQL.format(buildings=buildings),
                              {"box": bld_box, "uses": list(SETTLEMENT_BUILDING_USES)}, [("pt", "geometry")])
        self.logger.info("MV routing graph %s: %d address buildings read from %s", graph_id, n_rows, reader.where)
        self.cur.execute(SETTLEMENT_BUILDINGS_SQL.format(epsg=epsg))
        self.cur.execute("DROP TABLE pg_temp.mvg_bld_raw")
        self.cur.execute("CREATE INDEX ON mvg_bld USING gist (pt)")
        self.cur.execute("ANALYZE mvg_bld")
        landuse = self._landuse_table()
        if landuse is not None:
            self.cur.execute(BOX_SQL.format(epsg=epsg), {**padded, "srid": self._osm_layer_srid("landuse_polygon")})
            lu_box = bytes(self.cur.fetchone()[0])
            reader.stage("mvg_lu_raw", LANDUSE_STAGE_SQL.format(landuse=landuse),
                         {"box": lu_box, "types": list(SETTLEMENT_LANDUSE_TYPES)},
                         lambda: reader.column_types(INFDB_OSM_SCHEMA, "landuse_polygon", ("geom",)))
            self.cur.execute(LANDUSE_TEMP_SQL.format(epsg=epsg))
            self.cur.execute("DROP TABLE pg_temp.mvg_lu_raw")
        else:
            self.logger.warning("no %s.landuse_polygon: MV settlements from address buildings only",
                                INFDB_OSM_SCHEMA or "OSM schema")
            self.cur.execute(NO_LANDUSE_TEMP_SQL)
        self.cur.execute("CREATE INDEX ON mvg_lu USING gist (geom)")
        self.cur.execute("ANALYZE mvg_lu")
        sbuf = parameters["settlement_buffer_m"]
        self.logger.info("MV routing graph %s: components and settlement inputs ready (%.0f s)", graph_id,
                         time.perf_counter() - started)
        self.cur.execute(EDGE_BUILDINGS_SQL, {**g, "sbuf": sbuf})
        self.cur.execute(IN_SETTLEMENT_SQL, {**g, "smin": parameters["settlement_min_buildings"],
                                             "sspacing": parameters["settlement_spacing_m"], "osm": list(_OSM_KLASSEN)})
        self.cur.execute(SETTLEMENT_ID_SQL, {**g, "settlement_edges_sql": edges_sql(graph_id, settlement_only=True)})
        # Fresh statistics of the settlement flags: with those from before the flags the planner expects no settlement
        # edge and joins the settlement polygons to their building counts by a nested loop that reads the sorted
        # counts again for every settlement (quadratic: 4 of the 5 minutes of a build of 364 municipalities).
        self.cur.execute(ANALYZE_SQL[1])
        self.cur.execute(INSERT_SETTLEMENTS_SQL, {**g, "sbuf": sbuf, "spoly": parameters["settlement_polygon_buffer_m"],
                                                  "urban_min": parameters["urban_min_buildings"]})
        self.cur.execute(SETTLEMENT_URBAN_SQL, g)
        self.cur.execute(SUMMARY_SQL, {**g, "seams": seams, "seconds": time.perf_counter() - started})
        for statement in ANALYZE_SQL:
            self.cur.execute(statement)
        self.logger.info("MV routing graph %s built from %d municipalities in %.1f s (%d seam endpoints, "
                         "%d seam cuts)", graph_id, len(ags), time.perf_counter() - started, seams, cuts)

    def _add_osm_lines(self, snap_m: float, connect_m: float) -> None:
        """Add the OSM MV overhead lines to ``mvg_src``: cut them and the streets at their crossings, cut OSM lines
        where a branch line ends on them, and join their loose ends to the nearest street point within ``connect_m``
        (see the module docstring); re-clusters the edge ends. Without the OSM tables the graph stays a street graph
        (logged)."""
        import shapely

        if not self._osm_lines_available():
            self.logger.warning("no %s.infrastructure_line and tags: no OSM MV lines are found, so every MV line "
                                "is a cable", INFDB_OSM_SCHEMA or "OSM schema")
            return
        osm = sql.Identifier(INFDB_OSM_SCHEMA)
        epsg = sql.Literal(TARGET_EPSG)
        osm_srid = self._osm_layer_srid("infrastructure_line")
        self.cur.execute(EXTENT_SQL)
        x0, y0, x1, y1 = self.cur.fetchone()
        rows = self.source_reader().fetchall(OSM_LINES_SQL.format(osm=osm, epsg=epsg),
                                             {"subtypes": list(OVERHEAD_SUBTYPES), "x0": x0, "y0": y0, "x1": x1,
                                              "y1": y1, "osm_srid": osm_srid})
        kept = [(int(osm_id), str(subtype), bytes(wkb)) for osm_id, subtype, tags, wkb in rows
                if wkb is not None and is_mv_overhead_line(subtype, tags if isinstance(tags, dict) else {})]
        self.cur.execute(f"CREATE TEMP TABLE mvg_osm (osm_id bigint, subtype text, geom geometry(Geometry, "
                         f"{int(TARGET_EPSG)})) ON COMMIT DROP")
        if kept:
            execute_values(self.cur, "INSERT INTO mvg_osm (osm_id, subtype, geom) VALUES %s", kept,
                           template=f"(%s, %s, ST_GeomFromWKB(%s, {int(TARGET_EPSG)}))", page_size=2000)
        self.cur.execute(OSM_INSERT_SQL, {"klasse": OSM_LINE_KLASSE})
        n_edges = self.cur.rowcount
        km = sum(shapely.from_wkb(wkb).length for _, _, wkb in kept) / 1000.0
        self.cur.execute("ANALYZE mvg_src")
        self._cluster_endpoints(snap_m)
        osm_params = {"osm": list(_OSM_KLASSEN), "line": OSM_LINE_KLASSE}
        # 1. cut OSM lines and streets at their crossings, and OSM lines where a branch line ends on them
        self.cur.execute("DROP TABLE IF EXISTS pg_temp.mvg_cut, pg_temp.mvg_split")
        self.cur.execute("CREATE TEMP TABLE mvg_cut (host bigint, f double precision, pt geometry) ON COMMIT DROP")
        self.cur.execute(OSM_CROSSING_CUTS_SQL, osm_params)
        crossings = self.cur.rowcount // 2
        self.cur.execute(OSM_JUNCTION_CUTS_SQL, {**osm_params, "tol": SEAM_TOLERANCE_M})
        junctions = self.cur.rowcount
        self._apply_cuts(snap_m)
        # 2. join the loose ends to the nearest street point (the street is cut there)
        self.cur.execute("DROP TABLE IF EXISTS pg_temp.mvg_conn, pg_temp.mvg_cut, pg_temp.mvg_split")
        self.cur.execute(OSM_LOOSE_ENDS_SQL, {**osm_params, "snap": connect_m})
        self.cur.execute("CREATE TEMP TABLE mvg_cut ON COMMIT DROP AS SELECT host, f, cp AS pt FROM mvg_conn")
        self.cur.execute(OSM_CONNECTORS_SQL, {"connector": OSM_CONNECTOR_KLASSE})
        connectors = self.cur.rowcount
        self._apply_cuts(snap_m)
        self.logger.info("OSM MV lines: %d ways (%.0f km, %d edges), %d street crossings, %d branch junctions, "
                         "%d end connectors", len(kept), km, n_edges, crossings, junctions, connectors)

    def _apply_cuts(self, snap_m: float) -> None:
        """Split the hosts of ``mvg_cut`` at their cut points (seam pieces) and re-cluster the edge ends."""
        self.cur.execute(SEAM_PIECES_SQL)
        for statement in SEAM_REPLACE_SQL:
            self.cur.execute(statement)
        self._cluster_endpoints(snap_m)

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
        self.cur.execute(REGION_ARRAYS_SQL, {"g": graph_id, "osm": list(_OSM_KLASSEN)})
        rows = self.cur.fetchall()
        if not rows:
            return (np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.float64))
        edge_id, source, target, length = zip(*rows)
        return (np.asarray(edge_id, dtype=np.int64), np.asarray(source, dtype=np.int64),
                np.asarray(target, dtype=np.int64), np.asarray(length, dtype=np.float64))

    def settlement_polygons(self, graph_id: str, region_wkb: bytes) -> pd.DataFrame:
        """The settlement polygons of a graph that touch the planning region: ``settlement_id``, ``urban`` and
        ``geom_wkb`` (WKB, ``TARGET_EPSG``)."""
        self.cur.execute(SETTLEMENT_POLYGONS_SQL.format(epsg=sql.Literal(TARGET_EPSG)),
                         {"g": graph_id, "region": bytes(region_wkb)})
        rows = [(int(i), bool(u), bytes(g)) for i, u, g in self.cur.fetchall()]
        return pd.DataFrame(rows, columns=["settlement_id", "urban", "geom_wkb"])

    def osm_line_geometries(self, graph_id: str, region_wkb: bytes) -> list[bytes]:
        """WKB of the OSM MV line edges of a graph that touch the planning region (empty without OSM lines)."""
        self.cur.execute(OSM_LINE_GEOMS_SQL.format(epsg=sql.Literal(TARGET_EPSG)),
                         {"g": graph_id, "line": OSM_LINE_KLASSE, "region": bytes(region_wkb)})
        return [bytes(row[0]) for row in self.cur.fetchall() if row[0] is not None]

    def edge_municipalities(self, graph_id: str) -> pd.Series:
        """The municipality key (AGS, text) of every street edge of a graph, indexed by ``edge_id``."""
        self.cur.execute(EDGE_AGS_SQL, {"g": graph_id, "osm": list(_OSM_KLASSEN)})
        rows = self.cur.fetchall()
        return pd.Series([str(a) for _, a in rows], index=pd.Index([int(e) for e, _ in rows], name="edge_id"),
                         dtype=object, name="ags")

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
        self.cur.execute(ATTACH_SQL, {"g": graph_id, "osm": list(_OSM_KLASSEN)})
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
                         {"g": graph_id, "hull": bytes(hull_wkb), "buffer_m": float(buffer_m), "must": must_ids,
                          "line": OSM_LINE_KLASSE})
        return self._edge_set(graph_id, self.cur.fetchall())

    def subgraph_edges_near(self, graph_id: str, xy: np.ndarray, buffer_m: float, must: Sequence[int]) -> EdgeSet:
        """The edges within ``buffer_m`` of any of the points ``xy`` and the ``must`` edges, as an
        :class:`~pylovo.mv_grid.model.EdgeSet` like :meth:`subgraph_edges`.

        The same edges as :meth:`subgraph_edges` with the MultiPoint of the points, found point by point through the
        edge index (temporary table ``mv_subgraph_point``, dropped at commit; see :data:`SUBGRAPH_NEAR_SQL`): the
        ties and links of a variant search around thousands of ring members.

        Args:
            graph_id: Graph id.
            xy: ``(n, 2)`` point coordinates (``TARGET_EPSG``).
            buffer_m: Edges within this distance of a point are taken.
            must: Edge ids always taken.
        """
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        must_ids = sorted({int(e) for e in must if e is not None and not pd.isna(e)})
        self.cur.execute("DROP TABLE IF EXISTS pg_temp.mv_subgraph_point")
        self.cur.execute(SUBGRAPH_POINTS_SQL.format(epsg=sql.Literal(TARGET_EPSG)),
                         {"x": [float(v) for v in xy[:, 0]], "y": [float(v) for v in xy[:, 1]]})
        self.cur.execute("CREATE INDEX ON mv_subgraph_point USING gist (pt)")
        self.cur.execute("ANALYZE mv_subgraph_point")
        self.cur.execute(SUBGRAPH_NEAR_SQL, {"g": graph_id, "buffer_m": float(buffer_m), "must": must_ids,
                                             "line": OSM_LINE_KLASSE})
        rows = self.cur.fetchall()
        self.cur.execute("DROP TABLE pg_temp.mv_subgraph_point")
        return self._edge_set(graph_id, rows)

    def _edge_set(self, graph_id: str, rows: list[tuple]) -> EdgeSet:
        """The :class:`~pylovo.mv_grid.model.EdgeSet` of subgraph edge rows (ordered by ``edge_id``) and their
        vertices."""
        if rows:
            edge_id, source, target, length, in_settlement, settlement_id, overhead = (np.asarray(c)
                                                                                       for c in zip(*rows))
        else:
            edge_id = source = target = settlement_id = np.zeros(0, np.int64)
            length = np.zeros(0, np.float64)
            in_settlement = overhead = np.zeros(0, bool)
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
                       vertex_xy=xy, overhead=overhead.astype(bool))

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
