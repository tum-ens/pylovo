"""The map layers: what each one shows, where its data comes from and how it is filtered.

Every layer is one :class:`Layer` with a SELECT that returns ``fid``, ``geometry`` (GeoJSON text in
EPSG:4326) and the few properties the map styles and tooltips need. The full row of a clicked
feature comes from the layer's ``detail`` query. Layers whose tables are missing in the connected
database are listed as unavailable, with the reason.

Scopes (which request parameters a layer needs):

* ``version``: all features of one LV version (stations, grid areas, postcode areas).
* ``region``: LV detail (cables, buildings, cabinets) of a region (postcode, MV supply area, grid)
  or, without a region, of the map view (``bbox``).
* ``run``: one MV run (``version`` + ``mv``); ``run_bbox`` also needs the map view.
* ``bbox``: the map view (input and context data).
* ``germany``: small nation-wide tables, loaded once.
* ``file``: the downloaded eTraGo grid (``pylovo-viewer fetch etrago``).
"""
from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database
from pylovo_viewer.topology import colour_slots, feeders

GROUPS = {"regions": "Regions", "lv": "LV grids", "inputs": "LV input data", "classes": "Grid classification",
          "mv": "MV grids", "context": "Grid context"}
DROP = {"geom", "centroid", "footprint", "geometry", "geo", "net", "grid"}   # never sent as attributes

# Same classification as the generator (preprocessing_mixin.insert_transformers) and pylovo-api.
SOURCE_SQL = """CASE WHEN t.type IN ('dso', 'dso_validation') OR t.osm_id LIKE 'dso/%%'
                       OR t.osm_id LIKE 'dso_validation/%%' THEN 'dso'
                     WHEN t.osm_id LIKE 'manual/%%' THEN 'manual'
                     WHEN NOT t.osm AND t.lod2 THEN 'lod2' ELSE 'osm' END"""

# Highest voltage of an OSM ``voltage`` tag ('110000;20000') as a level: 380, 220, 110, mv, lv, unknown.
LEVEL_SQL = """CASE WHEN kv IS NULL THEN 'unknown' WHEN kv >= 300 THEN '380' WHEN kv >= 200 THEN '220'
                    WHEN kv >= 60 THEN '110' WHEN kv >= 1 THEN 'mv' ELSE 'lv' END"""
KV_SQL = """(SELECT max(v::numeric) / 1000 FROM regexp_split_to_table(t.tags->>'voltage', '[;,]') AS v
             WHERE v ~ '^[0-9]+(\\.[0-9]+)?$')"""

MASTR_LEVEL_SQL = """CASE WHEN m.voltage_level ILIKE 'Niederspannung%%' THEN 'LV'
                          WHEN m.voltage_level ILIKE '%%Mittelspannung/Niederspannung%%' THEN 'MV/LV'
                          WHEN m.voltage_level ILIKE 'Mittelspannung%%' THEN 'MV'
                          WHEN m.voltage_level ILIKE '%%Hochspannung/Mittelspannung%%' THEN 'HV/MV'
                          WHEN m.voltage_level ILIKE 'Hochspannung%%' THEN 'HV'
                          WHEN m.voltage_level ILIKE '%%Höchstspannung%%' THEN 'EHV' END"""

def run(alias: str = "") -> str:
    """Filter on one MV run (``alias.`` prefixed when given)."""
    a = f"{alias}." if alias else ""
    return f"{a}version_id = %(version)s AND {a}mv_variant_id = %(mv)s"


RUN = run()
GRAPH = "(SELECT graph_id FROM pylovo.mv_variant WHERE " + RUN + ")"


def geo(expr: str, digits: int = 6) -> str:
    return f"ST_AsGeoJSON(ST_Transform({expr}, 4326), {digits})"


def num(expr: str, digits: int) -> str:
    return f"round(({expr})::numeric, {digits})::float"


@dataclass(frozen=True)
class Query:
    """Request parameters of a layer, already validated by the API."""

    version: str | None = None
    mv: str | None = None
    plz: int | None = None
    area: int | None = None
    grid: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    def params(self) -> dict[str, Any]:
        out: dict[str, Any] = {"version": self.version, "mv": self.mv, "plz": self.plz, "area": self.area,
                               "grid": self.grid}
        if self.bbox:
            out.update(zip(("minx", "miny", "maxx", "maxy"), self.bbox))
        return out

    def region(self) -> str | None:
        return "grid" if self.grid is not None else "area" if self.area is not None else \
            "plz" if self.plz is not None else None

    def key(self) -> tuple:
        return (self.version, self.mv, self.plz, self.area, self.grid, self.bbox)


# Grid ids of a region; the region filters of the LV detail layers are built from these.
REGION_GRIDS = {
    "plz": "SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(version)s AND plz = %(plz)s",
    "area": "SELECT grid_result_id FROM pylovo.mv_node WHERE " + RUN
            + " AND mv_grid_id = %(area)s AND grid_result_id IS NOT NULL",
    "grid": "SELECT %(grid)s::bigint",
}


@dataclass(frozen=True)
class Layer:
    """One map layer (see the module docstring for ``scope``)."""

    id: str
    group: str
    title: str
    description: str
    geometry: str                     # point | line | polygon
    scope: str
    source: str
    sql: str = ""                     # placeholders: {filter} {env} {epsg} {src} {od} {osm}
    detail: str = ""                  # one feature, parameter %(fid)s
    requires: tuple[str, ...] = ()    # "schema.table" or "schema.table(col, col)"
    rows: tuple[str, ...] = ()        # tables that must hold rows
    region_filter: str = ""           # region scope: column with the grid id
    bbox_filter: str = ""             # region scope without a region: filter on the map view
    default: bool = False
    min_zoom: float = 0.0
    cap: int = 60_000
    attribution: str | None = None
    post: tuple[str, ...] = ()        # post-processing steps, see LayerService._post


def build_layers(od: str = "opendata", osm: str = "osm", src: str = "pylovo_input") -> list[Layer]:
    """All layers in display order (the schema names come from the ``.env``)."""
    lv_region = ("c.grid_result_id", "c.version_id = %(version)s AND c.geom && {env}")
    return [
        # ---------------------------------------------------------------------------------- regions
        Layer("postcodes", "regions", "Postcode areas",
              "Postcode areas of the version, with the number of grids. Click one to select it as region.",
              "polygon", "version", "pylovo.postcode_result",
              sql=f"""SELECT pr.postcode_result_plz AS fid, pr.postcode_result_plz AS plz, p.note,
                             pr.settlement_type AS settlement,
                             (SELECT count(*) FROM pylovo.grid_result g WHERE g.version_id = pr.version_id
                                AND g.plz = pr.postcode_result_plz) AS grids,
                             {num("ST_X(ST_Transform(ST_PointOnSurface(pr.geom), 4326))", 6)} AS lon,
                             {num("ST_Y(ST_Transform(ST_PointOnSurface(pr.geom), 4326))", 6)} AS lat,
                             {geo("ST_SimplifyPreserveTopology(pr.geom, 5)")} AS geometry
                      FROM pylovo.postcode_result pr LEFT JOIN pylovo.postcode p ON p.plz = pr.postcode_result_plz
                      WHERE pr.version_id = %(version)s""",
              detail="""SELECT pr.version_id, pr.postcode_result_plz AS plz, p.note, p.qkm, p.population,
                               pr.settlement_type, pr.house_distance, pr.avg_households_per_building
                        FROM pylovo.postcode_result pr LEFT JOIN pylovo.postcode p ON p.plz = pr.postcode_result_plz
                        WHERE pr.version_id = %(version)s AND pr.postcode_result_plz = %(fid)s""",
              requires=("pylovo.postcode_result", "pylovo.postcode"), default=True),
        # --------------------------------------------------------------------------------- LV grids
        Layer("lv_areas", "lv", "Grid areas",
              "Area of each LV grid (convex hull of its buildings), coloured so that neighbours differ.",
              "polygon", "version", "pylovo.grid_result + buildings_result",
              sql=f"""SELECT g.grid_result_id AS fid, g.kcid, g.bcid, g.plz, g.transformer_rated_power AS kva,
                             g.power_flow_status AS status, count(*) AS buildings,
                             {geo("ST_SimplifyPreserveTopology(ST_Buffer(ST_ConvexHull(ST_Collect(b.centroid)), 8, 2), 2)")}
                               AS geometry
                      FROM pylovo.grid_result g JOIN pylovo.buildings_result b ON b.grid_result_id = g.grid_result_id
                      WHERE g.version_id = %(version)s GROUP BY g.grid_result_id""",
              detail="grid", requires=("pylovo.grid_result", "pylovo.buildings_result"), default=True,
              post=("grid_slots",)),
        Layer("lv_stations", "lv", "Substations (MV/LV)",
              "Transformer station of every LV grid: size in kVA and power-flow result of the grid.",
              "point", "version", "pylovo.grid_result + transformer_positions",
              sql=f"""SELECT g.grid_result_id AS fid, g.kcid, g.bcid, g.plz, g.transformer_rated_power AS kva,
                             g.power_flow_status AS status,
                             CASE WHEN tp.lod2 THEN 'lod2' WHEN tp.osm THEN 'osm' ELSE 'new' END AS origin,
                             {geo("tp.geom")} AS geometry
                      FROM pylovo.grid_result g JOIN pylovo.transformer_positions tp
                        ON tp.grid_result_id = g.grid_result_id
                      WHERE g.version_id = %(version)s""",
              detail="grid", requires=("pylovo.grid_result", "pylovo.transformer_positions"), default=True,
              post=("grid_slots",)),
        Layer("lv_cables", "lv", "Cables",
              "LV cables: feeder cables along the streets and service cables to the buildings. Loaded for the "
              "selected region, or for the map view when zoomed in.",
              "line", "region", "pylovo.lines_result_cache",
              sql=f"""SELECT c.id AS fid, c.grid_result_id AS grid, c.std_type, COALESCE(c.parallel, 1) AS parallel,
                             {num("c.length_km * 1000", 1)} AS length_m,
                             CASE WHEN c.feeder_section_id IS NULL THEN 'service' ELSE 'feeder' END AS role,
                             NULLIF(substring(c.std_type from '([0-9]+)$'), '')::int AS mm2,
                             {geo("c.geom")} AS geometry
                      FROM pylovo.lines_result_cache c WHERE {{filter}}""",
              detail="""SELECT c.*, l.pp_index, l.max_i_ka, l.r_ohm_per_km, l.x_ohm_per_km, l.feeder_sizing_basis,
                               l.service_sizing_basis, l.service_selected_voltage_drop_percent,
                               l.total_design_voltage_drop_percent
                        FROM pylovo.lines_result_cache c
                        LEFT JOIN pylovo.pandapower_line l ON l.grid_result_id = c.grid_result_id
                         AND c.line_name = regexp_replace(l.name, '^Line to ', 'L')
                        WHERE c.id = %(fid)s""",
              requires=("pylovo.lines_result_cache(feeder_section_id, version_id)", "pylovo.pandapower_line"),
              region_filter=lv_region[0], bbox_filter=lv_region[1], default=True, min_zoom=14,
              post=("grid_slots", "cable_feeders")),
        Layer("lv_cabinets", "lv", "Cable cabinets",
              "Cable distribution cabinets where feeders split, with the number of outgoing cables.",
              "point", "region", "pylovo.split_points",
              sql=f"""SELECT sp.split_point_id AS fid, sp.grid_result_id AS grid, sp.split_type,
                             sp.outgoing_count AS outgoing, {geo("sp.geom")} AS geometry
                      FROM pylovo.split_points sp WHERE {{filter}}""",
              detail="SELECT * FROM pylovo.split_points WHERE split_point_id = %(fid)s",
              requires=("pylovo.split_points",), region_filter="sp.grid_result_id",
              bbox_filter="sp.geom && {env} AND sp.grid_result_id IN "
                          "(SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(version)s)",
              default=True, min_zoom=14, post=("grid_slots",)),
        Layer("lv_buildings", "lv", "Buildings",
              "Buildings supplied by the LV grids, with type, households and peak load.",
              "polygon", "region", "pylovo.buildings_result",
              sql=f"""SELECT b.objectid AS fid, b.grid_result_id AS grid, b.type, b.building_use AS use,
                             b.households, {num("b.peak_load_in_kw", 1)} AS peak_kw, {num("b.height", 1)} AS height,
                             b.floor_number AS floors, b.nonresidential_mv_direct AS mv_direct, b.vertice_id,
                             {geo("b.geom")} AS geometry
                      FROM pylovo.buildings_result b WHERE b.version_id = %(version)s AND {{filter}}""",
              detail="SELECT * FROM pylovo.buildings_result WHERE version_id = %(version)s AND objectid = %(fid)s",
              requires=("pylovo.buildings_result",), region_filter="b.grid_result_id", bbox_filter="b.geom && {env}",
              default=True, min_zoom=14, post=("grid_slots", "building_feeders")),
        # ---------------------------------------------------------------------------- LV input data
        Layer("in_buildings", "inputs", "InfDB buildings",
              "Input buildings of the InfDB with their building type (SFH, MFH, TH, AB, ...).",
              "polygon", "bbox", f"{src}.buildings",
              sql=f"""SELECT b.id AS fid, b.objectid, COALESCE(b.building_type, b.building_use) AS type, b.building_use AS use,
                             {num("b.height", 1)} AS height, b.floor_number AS floors, b.households,
                             {geo("b.geom")} AS geometry
                      FROM {src}.buildings b WHERE b.geom && {{env}}""",
              detail=f"SELECT * FROM {src}.buildings WHERE id = %(fid)s",
              requires=(f"{src}.buildings",), min_zoom=15),
        Layer("in_transformers", "inputs", "Transformer candidates",
              "Known transformer positions the generator can use: OSM, LoD2, DSO or manual.",
              "point", "bbox", "pylovo.transformers",
              sql=f"""SELECT t.osm_id AS fid, t.type, t.transformer_rated_power AS kva, {SOURCE_SQL} AS source,
                             {geo("ST_Centroid(t.geom)")} AS geometry
                      FROM pylovo.transformers t WHERE t.geom && {{env}}""",
              detail="SELECT * FROM pylovo.transformers WHERE osm_id = %(fid)s",
              requires=("pylovo.transformers",), min_zoom=12),
        Layer("in_ways", "inputs", "Streets",
              "Street network used for routing the cables.",
              "line", "bbox", f"{src}.ways_per_connection",
              sql=f"""SELECT w.ags || ':' || w.id AS fid, w.klasse, w.objektart, {num("w.length_geo", 1)} AS length_m,
                             {geo("w.geom")} AS geometry
                      FROM {src}.ways_per_connection w WHERE w.geom && {{env}}""",
              detail=f"""SELECT * FROM {src}.ways_per_connection
                         WHERE ags = split_part(%(fid)s, ':', 1) AND id = split_part(%(fid)s, ':', 2)""",
              requires=(f"{src}.ways_per_connection",), min_zoom=15),
        Layer("in_connections", "inputs", "House connection lines",
              "Lines from the buildings to the street network.",
              "line", "bbox", f"{src}.connection_lines",
              sql=f"""SELECT w.ags || ':' || w.id AS fid, {num("w.length_geo", 1)} AS length_m,
                             {geo("w.geom")} AS geometry
                      FROM {src}.connection_lines w WHERE w.geom && {{env}}""",
              detail=f"""SELECT * FROM {src}.connection_lines
                         WHERE ags = split_part(%(fid)s, ':', 1) AND id = split_part(%(fid)s, ':', 2)""",
              requires=(f"{src}.connection_lines",), min_zoom=16),
        # ---------------------------------------------------------------------------- classification
        Layer("lv_classes", "classes", "Grid clusters",
              "Cluster of each grid from pylovo-classify (k-means, k-medoids or GMM); representative grids "
              "are marked with a ring.",
              "point", "version", "pylovo.transformer_classified",
              sql=f"""SELECT DISTINCT ON (tc.grid_result_id) tc.grid_result_id AS fid, g.kcid, g.bcid, g.plz,
                             tc.classification_id, tc.kmeans_clusters AS kmeans, tc.kmedoid_clusters AS kmedoid,
                             tc.gmm_clusters AS gmm, tc.kmeans_representative_grid AS kmeans_rep,
                             tc.kmedoid_representative_grid AS kmedoid_rep, tc.gmm_representative_grid AS gmm_rep,
                             {geo("tc.geom")} AS geometry
                      FROM pylovo.transformer_classified tc JOIN pylovo.grid_result g
                        ON g.grid_result_id = tc.grid_result_id
                      WHERE g.version_id = %(version)s ORDER BY tc.grid_result_id, tc.classification_id DESC""",
              detail="""SELECT * FROM pylovo.transformer_classified WHERE grid_result_id = %(fid)s
                        ORDER BY classification_id DESC LIMIT 1""",
              requires=("pylovo.transformer_classified",), rows=("pylovo.transformer_classified",)),
        # ---------------------------------------------------------------------------------- MV grids
        Layer("mv_areas", "mv", "Supply areas",
              "Area supplied by each HV/MV substation (MV grid district), with its voltage and status. "
              "Areas at the border of the planning region are incomplete.",
              "polygon", "run", "pylovo.mv_grid",
              sql=f"""SELECT g.mv_grid_id AS fid, g.substation_id, s.name AS substation, g.vn_kv, g.status,
                             g.power_flow_status AS pf, g.complete, g.n_stations AS stations,
                             g.n_customers AS customers, g.n_rings AS rings, {num("g.area_km2", 1)} AS area_km2,
                             {num("g.peak_load_mw", 2)} AS peak_mw, {num("g.max_line_loading_percent", 1)} AS max_loading,
                             {num("g.max_voltage_drop_percent", 2)} AS max_drop,
                             {geo("ST_SimplifyPreserveTopology(g.geom, 5)")} AS geometry
                      FROM pylovo.mv_grid g LEFT JOIN pylovo.mv_substation s ON s.substation_id = g.substation_id
                      WHERE g.version_id = %(version)s AND g.mv_variant_id = %(mv)s""",
              detail="SELECT * FROM pylovo.mv_grid WHERE mv_grid_id = %(fid)s",
              requires=("pylovo.mv_grid", "pylovo.mv_substation"), rows=("pylovo.mv_variant",), default=True,
              post=("area_slots",)),
        Layer("mv_sites", "mv", "Substation sites",
              "Site outlines of the HV/MV substations (from OSM).",
              "polygon", "run", "pylovo.mv_substation",
              sql=f"""SELECT s.substation_id AS fid, s.name, s.source, {geo("s.footprint")} AS geometry
                      FROM pylovo.mv_substation s WHERE {run('s')} AND s.footprint IS NOT NULL""",
              detail="SELECT * FROM pylovo.mv_substation WHERE substation_id = %(fid)s",
              requires=("pylovo.mv_substation(footprint)",), rows=("pylovo.mv_variant",), default=True, min_zoom=12),
        Layer("mv_lines", "mv", "MV lines",
              "MV cables and overhead lines: open rings, stubs, dedicated feeders and ties between supply areas.",
              "line", "run", "pylovo.mv_line",
              sql=f"""SELECT l.mv_line_id AS fid, l.mv_grid_id AS area, l.role, l.kind, l.ring_id AS ring,
                             l.feeder_id AS feeder, l.std_type, COALESCE(l.parallel, 1) AS parallel,
                             {num("l.length_km", 3)} AS length_km, l.normally_open AS open,
                             {num("l.loading_load_percent", 1)} AS loading, {num("l.loading_n1_percent", 1)} AS loading_n1,
                             {num("l.loading_feed_in_percent", 1)} AS loading_feed_in,
                             {geo("ST_Simplify(l.geom, 1)")} AS geometry
                      FROM pylovo.mv_line l WHERE {run('l')}""",
              detail="SELECT * FROM pylovo.mv_line WHERE mv_line_id = %(fid)s",
              requires=("pylovo.mv_line",), rows=("pylovo.mv_variant",), default=True,
              post=("area_slots", "ring_slots")),
        Layer("mv_open_points", "mv", "Normally-open points",
              "Where each open ring is opened (normally-open point), drawn next to the node it opens at.",
              "point", "run", "pylovo.mv_line",
              sql=f"""SELECT l.mv_line_id AS fid, l.mv_grid_id AS area, l.ring_id AS ring, n.mv_node_id AS node,
                             {geo('''ST_LineInterpolatePoint(l.geom, CASE
                                 WHEN COALESCE(l.open_at_node, l.to_node) = l.from_node
                                   THEN LEAST(0.5, 12 / NULLIF(ST_Length(l.geom), 0))
                                 ELSE GREATEST(0.5, 1 - 12 / NULLIF(ST_Length(l.geom), 0)) END)''')} AS geometry
                      FROM pylovo.mv_line l JOIN pylovo.mv_node n ON n.mv_node_id = COALESCE(l.open_at_node, l.to_node)
                      WHERE {run('l')} AND l.normally_open AND l.role <> 'tie' AND ST_Length(l.geom) > 0""",
              detail="SELECT * FROM pylovo.mv_line WHERE mv_line_id = %(fid)s",
              requires=("pylovo.mv_line", "pylovo.mv_node"), rows=("pylovo.mv_variant",), default=True, min_zoom=11),
        Layer("mv_nodes", "mv", "MV nodes",
              "HV/MV busbars, MV/LV stations, MV customers, generators and joints of the MV grid.",
              "point", "run", "pylovo.mv_node",
              sql=f"""SELECT n.mv_node_id AS fid, n.mv_grid_id AS area, n.kind, n.connection, n.grid_result_id AS grid,
                             n.customer_objectid AS objectid, {num("n.sn_kva", 0)} AS kva, n.station_source AS origin,
                             n.ring_id AS ring, n.feeder_id AS feeder, {num("n.households", 0)} AS households,
                             n.technology, {num("n.vm_load_pu", 4)} AS vm, {num("n.vm_n1_pu", 4)} AS vm_n1,
                             {num("n.lv_busbar_vm_pu", 4)} AS lv_vm, n.band_conflict,
                             CASE WHEN 'lv_not_converged' = ANY(n.flags) THEN 'not_converged'
                                  WHEN 'lv_voltage_violation' = ANY(n.flags) THEN 'voltage_violation' END AS lv_status,
                             array_to_string(n.flags, ', ') AS flags, {geo("n.geom")} AS geometry
                      FROM pylovo.mv_node n WHERE {run('n')}""",
              detail="SELECT * FROM pylovo.mv_node WHERE mv_node_id = %(fid)s",
              requires=("pylovo.mv_node",), rows=("pylovo.mv_variant",), min_zoom=12,
              post=("area_slots", "ring_slots")),
        Layer("mv_substations", "mv", "HV/MV substations",
              "HV/MV substations that supply the MV grids (unused candidates are drawn hollow).",
              "point", "run", "pylovo.mv_substation",
              sql=f"""SELECT s.substation_id AS fid, s.source, s.name, s.operator, s.voltage_tag AS voltage,
                             s.n_transformers AS transformers, s.transformer_mva AS mva,
                             {num("s.loading_n0_percent", 1)} AS loading, {num("s.loading_n1_percent", 1)} AS loading_n1,
                             array_to_string(s.flags, ', ') AS flags, s.in_region,
                             EXISTS (SELECT 1 FROM pylovo.mv_grid g WHERE g.substation_id = s.substation_id) AS used,
                             {geo("s.geom")} AS geometry
                      FROM pylovo.mv_substation s WHERE {run('s')}""",
              detail="SELECT * FROM pylovo.mv_substation WHERE substation_id = %(fid)s",
              requires=("pylovo.mv_substation", "pylovo.mv_grid"), rows=("pylovo.mv_variant",), default=True),
        Layer("mv_customers", "mv", "MV customer buildings",
              "Buildings connected directly to the MV grid (large non-residential loads).",
              "polygon", "version", "pylovo.mv_customers",
              sql=f"""SELECT c.objectid AS fid, c.building_use AS use, c.nonresidential_use AS nonres_use,
                             {num("COALESCE(c.nonresidential_peak_load_in_kw, 0) + COALESCE(c.residential_peak_load_in_kw, 0)", 1)}
                               AS peak_kw, c.households, {geo("c.geom")} AS geometry
                      FROM pylovo.mv_customers c WHERE c.version_id = %(version)s""",
              detail="SELECT * FROM pylovo.mv_customers WHERE version_id = %(version)s AND objectid = %(fid)s",
              requires=("pylovo.mv_customers",), rows=("pylovo.mv_customers",), min_zoom=12),
        Layer("mv_settlements", "mv", "Settlements",
              "Settlement areas of the MV routing graph (urban or rural); cables run inside, overhead lines "
              "between rural settlements.",
              "polygon", "run", "pylovo.mv_graph_settlement",
              sql=f"""SELECT s.settlement_id AS fid, s.urban, s.n_buildings AS buildings, {num("s.length_km", 2)} AS km,
                             {geo("ST_SimplifyPreserveTopology(s.geom, 5)")} AS geometry
                      FROM pylovo.mv_graph_settlement s WHERE s.graph_id = {GRAPH}""",
              detail=f"""SELECT * FROM pylovo.mv_graph_settlement WHERE graph_id = {GRAPH}
                         AND settlement_id = %(fid)s""",
              requires=("pylovo.mv_graph_settlement", "pylovo.mv_variant(graph_id)"), rows=("pylovo.mv_variant",)),
        Layer("mv_graph", "mv", "Routing graph (debug)",
              "Street edges the MV router could use; edges inside settlements are highlighted.",
              "line", "run_bbox", "pylovo.mv_graph_edge",
              sql=f"""SELECT e.edge_id AS fid, e.klasse, e.in_settlement, e.settlement_urban AS urban,
                             {num("e.length_m", 1)} AS length_m, {geo("e.geom")} AS geometry
                      FROM pylovo.mv_graph_edge e WHERE e.graph_id = {GRAPH} AND e.geom && {{env}}""",
              detail=f"SELECT * FROM pylovo.mv_graph_edge WHERE graph_id = {GRAPH} AND edge_id = %(fid)s",
              requires=("pylovo.mv_graph_edge", "pylovo.mv_variant(graph_id)"), rows=("pylovo.mv_variant",),
              min_zoom=13),
        # ------------------------------------------------------------------------------- grid context
        Layer("ctx_etrago_lines", "context", "eTraGo HV/EHV lines",
              "Germany's 110, 220 and 380 kV grid from eGon-data (eTraGo, PyPSA format). Download it once "
              "with: pylovo-viewer fetch etrago",
              "line", "file", "eGon-data eTraGo (Open Energy Platform)",
              attribution="eGon-data eTraGo grid (osmTGmod), © OpenStreetMap contributors (ODbL)"),
        Layer("ctx_etrago_buses", "context", "eTraGo HV/EHV buses",
              "Buses of the eTraGo grid (substations and line ends).",
              "point", "file", "eGon-data eTraGo (Open Energy Platform)", min_zoom=9,
              attribution="eGon-data eTraGo grid (osmTGmod), © OpenStreetMap contributors (ODbL)"),
        Layer("ctx_egon_districts", "context", "eGon MV grid districts",
              "MV grid districts of eGon-data for all of Germany (the reference for the supply areas).",
              "polygon", "germany", f"{od}.egon_mv_grid_district",
              sql=f"""SELECT d.bus_id AS fid, {geo("ST_SimplifyPreserveTopology(d.geom, 100)", 5)} AS geometry
                      FROM {od}.egon_mv_grid_district d""",
              detail=f"""SELECT bus_id, area_raw, {num("ST_Area(geom) / 1e6", 1)} AS area_km2
                         FROM {od}.egon_mv_grid_district WHERE bus_id = %(fid)s""",
              requires=(f"{od}.egon_mv_grid_district",), rows=(f"{od}.egon_mv_grid_district",),
              attribution="eGon-data"),
        Layer("ctx_egon_substations", "context", "eGon HV/MV substations",
              "HV/MV substations of eGon-data for all of Germany.",
              "point", "germany", f"{od}.egon_hvmv_substation",
              sql=f"""SELECT s.bus_id AS fid, NULLIF(s.subst_name, 'NA') AS name, NULLIF(s.operator, 'NA') AS operator,
                             NULLIF(s.voltage, 'NA') AS voltage, {geo("s.geom")} AS geometry
                      FROM {od}.egon_hvmv_substation s""",
              detail=f"SELECT * FROM {od}.egon_hvmv_substation WHERE bus_id = %(fid)s",
              requires=(f"{od}.egon_hvmv_substation",), rows=(f"{od}.egon_hvmv_substation",), min_zoom=6,
              attribution="eGon-data"),
        Layer("ctx_osm_lines", "context", "OSM power lines",
              "Power lines and cables from OpenStreetMap in the database, by voltage.",
              "line", "bbox", f"{osm}.infrastructure_line",
              sql=f"""SELECT i.osm_id AS fid, i.osm_subtype AS subtype, NULLIF(i.name, '') AS name,
                             NULLIF(i.operator, '') AS operator, t.tags->>'voltage' AS voltage,
                             t.tags->>'circuits' AS circuits, {LEVEL_SQL} AS level, {geo("i.geom")} AS geometry
                      FROM {osm}.infrastructure_line i
                      LEFT JOIN {osm}.tags t ON t.osm_id = i.osm_id AND t.geom_type = 'W'
                      CROSS JOIN LATERAL (SELECT {KV_SQL} AS kv) v
                      WHERE i.osm_type = 'power' AND i.osm_subtype NOT LIKE 'abandoned%%' AND i.geom && {{env}}""",
              detail=f"""SELECT i.*, t.tags, t.osm_url FROM {osm}.infrastructure_line i
                         LEFT JOIN {osm}.tags t ON t.osm_id = i.osm_id AND t.geom_type = 'W' WHERE i.osm_id = %(fid)s""",
              requires=(f"{osm}.infrastructure_line", f"{osm}.tags"), rows=(f"{osm}.infrastructure_line",), min_zoom=7,
              attribution="© OpenStreetMap contributors (ODbL)"),
        Layer("ctx_osm_substations", "context", "OSM substations",
              "Substation sites from OpenStreetMap in the database, by highest voltage.",
              "polygon", "bbox", f"{osm}.infrastructure_polygon",
              sql=f"""SELECT p.osm_id AS fid, NULLIF(p.name, '') AS name, NULLIF(p.operator, '') AS operator,
                             t.tags->>'voltage' AS voltage, t.tags->>'substation' AS kind, {LEVEL_SQL} AS level,
                             {geo("p.geom")} AS geometry
                      FROM {osm}.infrastructure_polygon p
                      LEFT JOIN {osm}.tags t ON t.osm_id = abs(p.osm_id)
                        AND t.geom_type = CASE WHEN p.osm_id < 0 THEN 'R' ELSE 'W' END
                      CROSS JOIN LATERAL (SELECT {KV_SQL} AS kv) v
                      WHERE p.osm_type = 'power' AND p.osm_subtype = 'substation' AND p.geom && {{env}}""",
              detail=f"""SELECT p.*, t.tags, t.osm_url FROM {osm}.infrastructure_polygon p
                         LEFT JOIN {osm}.tags t ON t.osm_id = abs(p.osm_id)
                           AND t.geom_type = CASE WHEN p.osm_id < 0 THEN 'R' ELSE 'W' END
                         WHERE p.osm_id = %(fid)s""",
              requires=(f"{osm}.infrastructure_polygon", f"{osm}.tags"), rows=(f"{osm}.infrastructure_polygon",),
              min_zoom=9, attribution="© OpenStreetMap contributors (ODbL)"),
        Layer("ctx_mastr", "context", "MaStR units",
              "Generating units and storage from the Marktstammdatenregister, by technology and grid level.",
              "point", "bbox", "pylovo.mastr_units",
              sql=f"""SELECT m.mastr_id AS fid, m.technology, {num("m.net_power_kw", 1)} AS kw, {MASTR_LEVEL_SQL} AS level,
                             m.status, extract(year FROM m.commissioning_date)::int AS year, {geo("m.geom")} AS geometry
                      FROM pylovo.mastr_units m WHERE m.geom && {{env}}""",
              detail="SELECT * FROM pylovo.mastr_units WHERE mastr_id = %(fid)s",
              requires=("pylovo.mastr_units",), rows=("pylovo.mastr_units",), min_zoom=11,
              attribution="Marktstammdatenregister (dl-de/by-2-0)"),
    ]


def to_json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, memoryview)):
        return None
    return str(value)


def feature_collection(rows: list[dict[str, Any]], truncated: bool = False) -> str:
    """GeoJSON text; ``geometry`` is already GeoJSON text, ``fid`` stays a property (``promoteId``)."""
    dumps = json.JSONEncoder(default=to_json, separators=(",", ":"), ensure_ascii=False).encode
    parts = []
    for row in rows:
        geometry = row.pop("geometry", None)
        if geometry:
            parts.append(f'{{"type":"Feature","geometry":{geometry},"properties":{dumps(row)}}}')
    flag = "true" if truncated else "false"
    return f'{{"type":"FeatureCollection","truncated":{flag},"features":[{",".join(parts)}]}}'


class LRU:
    """A small thread-safe least-recently-used cache."""

    def __init__(self, size: int) -> None:
        self.size, self._data, self._lock = size, OrderedDict(), threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: Any, value: Any) -> Any:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


@dataclass
class LayerService:
    """Layer catalogue and data access for the API."""

    db: Database
    catalog: Catalog
    data_dir: Path
    layers: dict[str, Layer] = field(init=False)

    def __post_init__(self) -> None:
        s = self.db.settings
        self.layers = {layer.id: layer for layer in build_layers(s.opendata_schema, s.osm_schema, s.source_schema)}
        self._slots = LRU(64)
        self._feeders = LRU(20_000)

    # ------------------------------------------------------------------------------ availability
    def unavailable(self, layer: Layer) -> str | None:
        """Why ``layer`` cannot be shown with this database, or ``None``."""
        if layer.scope == "file":
            from pylovo_viewer import etrago
            if not etrago.status(self.data_dir)["available"]:
                return "not downloaded yet: run pylovo-viewer fetch etrago"
            return None
        for requirement in layer.requires:
            reason = self.catalog.missing(requirement)
            if reason:
                return reason
        for table in layer.rows:
            if not self.catalog.has_rows.get(table, False):
                return f"{table} is empty"
        return None

    def describe(self) -> list[dict[str, Any]]:
        out = []
        for layer in self.layers.values():
            reason = self.unavailable(layer)
            out.append({"id": layer.id, "group": layer.group, "group_title": GROUPS[layer.group],
                        "title": layer.title, "description": layer.description, "geometry": layer.geometry,
                        "scope": layer.scope, "source": layer.source, "default": layer.default,
                        "min_zoom": layer.min_zoom, "attribution": layer.attribution,
                        "available": reason is None, "reason": reason})
        return out

    def clear(self) -> None:
        self._slots.clear()
        self._feeders.clear()

    # -------------------------------------------------------------------------------------- data
    def _check(self, layer: Layer, q: Query) -> None:
        needs = {"version": ("version",), "region": ("version",), "run": ("version", "mv"),
                 "run_bbox": ("version", "mv", "bbox"), "bbox": ("bbox",)}.get(layer.scope, ())
        missing = [name for name in needs if getattr(q, name) is None]
        if layer.scope == "region" and q.region() is None and q.bbox is None:
            missing.append("plz, area, grid or bbox")
        if q.region() == "area" and q.mv is None:
            missing.append("mv")
        if missing:
            raise ValueError(f"layer {layer.id} needs {', '.join(missing)}")

    def sql(self, layer: Layer, q: Query) -> str:
        epsg = self.db.settings.target_epsg
        env = f"ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326), {epsg})"
        flt = ""
        if layer.scope == "region":
            region = q.region()
            flt = f"{layer.region_filter} IN ({REGION_GRIDS[region]})" if region else layer.bbox_filter
        return layer.sql.replace("{filter}", flt).replace("{env}", env)

    def geojson(self, layer_id: str, q: Query) -> str:
        layer = self.layers[layer_id]
        self._check(layer, q)
        if layer.scope == "file":
            from pylovo_viewer import etrago
            blob = etrago.read(self.data_dir, "lines" if layer_id.endswith("lines") else "buses")
            if blob is None:
                raise LookupError(self.unavailable(layer) or "not available")
            return blob.decode()
        rows = self.db.query(self.sql(layer, q) + f"\nLIMIT {layer.cap + 1}", q.params())
        truncated = len(rows) > layer.cap
        rows = rows[:layer.cap]
        for step in layer.post:
            getattr(self, "_" + step)(rows, q)
        return feature_collection(rows, truncated)

    def feature(self, layer_id: str, fid: str, q: Query) -> dict[str, Any]:
        """All attributes of one feature (no geometry, no stored pandapower nets)."""
        layer = self.layers[layer_id]
        sql = layer.detail
        if sql == "grid":
            sql = GRID_DETAIL.replace("{grid_columns}", self._columns("pylovo.grid_result", "g"))
        if not sql:
            raise LookupError(f"layer {layer_id} has no attribute table")
        star = re.match(r"\s*SELECT \* FROM (\S+)", sql)
        if star:   # list the columns: large stored nets and geometries are never read
            sql = sql.replace("*", self._columns(star.group(1)), 1)
        row = self.db.one(sql, {**q.params(), "fid": fid})
        if row is None:
            raise LookupError(f"no feature {fid} in {layer_id}")
        return {key: value for key, value in row.items() if key not in DROP}

    def _columns(self, table: str, alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        skip = DROP | self.catalog.geometry_columns(table)
        cols = [c for c in self.catalog.columns.get(table, {}) if c not in skip]
        return ", ".join(f'{prefix}"{c}"' for c in cols) or "*"

    # ------------------------------------------------------------------------ post-processing
    def grid_slots(self, version: str) -> dict[int, int]:
        key = ("grid", version)
        cached = self._slots.get(key)
        if cached is None:
            rows = self.db.query("""SELECT g.grid_result_id AS id, ST_X(tp.geom) AS x, ST_Y(tp.geom) AS y
                                    FROM pylovo.grid_result g JOIN pylovo.transformer_positions tp
                                      ON tp.grid_result_id = g.grid_result_id
                                    WHERE g.version_id = %s""", (version,))
            cached = self._slots.put(key, colour_slots([r["id"] for r in rows],
                                                       np.array([[r["x"], r["y"]] for r in rows]).reshape(-1, 2)))
        return cached

    def _grid_slots(self, rows: list[dict[str, Any]], q: Query) -> None:
        slots = self.grid_slots(q.version)
        for row in rows:
            row["slot"] = slots.get(row.get("grid", row["fid"]))

    def _run_slots(self, kind: str, q: Query) -> dict[Any, int]:
        key = (kind, q.version, q.mv)
        cached = self._slots.get(key)
        if cached is None:
            if kind == "area":
                rows = self.db.query("""SELECT mv_grid_id AS id, ST_X(ST_PointOnSurface(geom)) AS x,
                                               ST_Y(ST_PointOnSurface(geom)) AS y
                                        FROM pylovo.mv_grid WHERE """ + RUN + " AND geom IS NOT NULL", q.params())
                keys = [r["id"] for r in rows]
            else:
                rows = self.db.query("""SELECT mv_grid_id, ring_id, ST_X(ST_Centroid(ST_Collect(geom))) AS x,
                                               ST_Y(ST_Centroid(ST_Collect(geom))) AS y
                                        FROM pylovo.mv_line WHERE """ + RUN + """ AND ring_id IS NOT NULL
                                          AND role <> 'tie' GROUP BY mv_grid_id, ring_id""", q.params())
                keys = [(r["mv_grid_id"], r["ring_id"]) for r in rows]
            xy = np.array([[r["x"], r["y"]] for r in rows], dtype=float).reshape(-1, 2)
            cached = self._slots.put(key, colour_slots(keys, xy))
        return cached

    def _area_slots(self, rows: list[dict[str, Any]], q: Query) -> None:
        slots = self._run_slots("area", q)
        for row in rows:
            row["slot"] = slots.get(row.get("area", row["fid"]))

    def _ring_slots(self, rows: list[dict[str, Any]], q: Query) -> None:
        slots = self._run_slots("ring", q)
        for row in rows:
            row["ring_slot"] = slots.get((row.get("area"), row.get("ring")))

    def feeder_maps(self, grids: set[int]) -> dict[int, tuple[dict[int, int], dict[int, int]]]:
        """``grid -> (feeder per cable id, feeder per vertex)``, cached per grid."""
        out, todo = {}, []
        for grid in grids:
            cached = self._feeders.get(grid)
            if cached is None:
                todo.append(grid)
            else:
                out[grid] = cached
        if todo:
            stations = {r["grid_result_id"]: r["ont_vertice_id"] for r in self.db.query(
                "SELECT grid_result_id, ont_vertice_id FROM pylovo.grid_result WHERE grid_result_id = ANY(%s)",
                (todo,))}
            edges: dict[int, list[tuple[int, int, int, float]]] = {g: [] for g in todo}
            for r in self.db.query("""SELECT grid_result_id AS g, id, from_bus, to_bus, length_km
                                      FROM pylovo.lines_result_cache WHERE grid_result_id = ANY(%s)""", (todo,)):
                edges[r["g"]].append((r["id"], r["from_bus"], r["to_bus"], r["length_km"]))
            for grid in todo:
                out[grid] = self._feeders.put(grid, feeders(stations.get(grid), edges[grid]))
        return out

    def _cable_feeders(self, rows: list[dict[str, Any]], q: Query) -> None:
        maps = self.feeder_maps({row["grid"] for row in rows if row.get("grid") is not None})
        for row in rows:
            row["feeder"] = maps.get(row["grid"], ({}, {}))[0].get(row["fid"])

    def _building_feeders(self, rows: list[dict[str, Any]], q: Query) -> None:
        maps = self.feeder_maps({row["grid"] for row in rows if row.get("grid") is not None})
        for row in rows:
            vertex = row.pop("vertice_id", None)
            row["feeder"] = maps.get(row["grid"], ({}, {}))[1].get(vertex)


GRID_DETAIL = f"""
SELECT {{grid_columns}}, tp.osm_id AS station_osm_id, tp.comment AS station_comment, tp.osm AS station_from_osm,
       tp.lod2 AS station_from_lod2,
       (SELECT count(*) FROM pylovo.buildings_result b WHERE b.grid_result_id = g.grid_result_id) AS buildings,
       (SELECT sum(b.households) FROM pylovo.buildings_result b WHERE b.grid_result_id = g.grid_result_id)
         AS households,
       (SELECT {num("sum(b.peak_load_in_kw)", 1)} FROM pylovo.buildings_result b
         WHERE b.grid_result_id = g.grid_result_id) AS peak_load_kw,
       (SELECT {num("sum(c.length_km * COALESCE(c.parallel, 1))", 3)} FROM pylovo.lines_result_cache c
         WHERE c.grid_result_id = g.grid_result_id AND c.feeder_section_id IS NOT NULL) AS feeder_cable_km,
       (SELECT {num("sum(c.length_km * COALESCE(c.parallel, 1))", 3)} FROM pylovo.lines_result_cache c
         WHERE c.grid_result_id = g.grid_result_id AND c.feeder_section_id IS NULL) AS service_cable_km
FROM pylovo.grid_result g LEFT JOIN pylovo.transformer_positions tp ON tp.grid_result_id = g.grid_result_id
WHERE g.grid_result_id = %(fid)s"""

