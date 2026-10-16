"""The map layers: what each one shows, where its data comes from and how it is filtered.

Layers are grouped by voltage level, plus regions and generation. A substation belongs to the level
of its higher voltage: HV/MV substations to the HV grid, MV/LV substations to the MV grids. Every
layer is one :class:`Layer` with a SELECT that returns ``fid``, ``geometry`` (GeoJSON text in
EPSG:4326) and the few properties the map styles and tooltips need; the full row of a clicked
feature comes from its ``detail`` query. A layer whose tables are missing in the connected database
is listed as unavailable, with the reason. A layer with a ``parent`` (open points, cable cabinets,
substation sites) is shown and hidden together with it.

Scopes (which request parameters a layer needs):

* ``version``: all features of one LV version (postcode and grid areas, MV/LV substations, which
  add their MV data when an MV run is given).
* ``view``: LV detail (cables, cabinets, buildings) of one version in the map view (``bbox``).
* ``run``: one MV run (``version`` + ``mv``).
* ``bbox``: the map view, independent of the version (MaStR units).
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

# Group id -> (title, description, on by default), in panel order.
GROUPS = {
    "regions": ("Regions", "Postcode areas of the version", True),
    "hv": ("HV grid", "110-380 kV lines (eTraGo) and the HV/MV substations", True),
    "mv": ("MV grids", "MV lines, MV/LV substations and supply areas of the MV run", True),
    "lv": ("LV grids", "Grid areas; cables and buildings when zoomed in", True),
    "generation": ("Generation", "Generating units and storage of the Marktstammdatenregister", False),
}
DROP = {"geom", "centroid", "footprint", "geometry", "geo", "net", "grid"}   # never sent as attributes

MASTR_LEVEL_SQL = """CASE WHEN m.voltage_level ILIKE 'Niederspannung%%' THEN 'LV'
                          WHEN m.voltage_level ILIKE '%%Mittelspannung/Niederspannung%%' THEN 'MV/LV'
                          WHEN m.voltage_level ILIKE 'Mittelspannung%%' THEN 'MV'
                          WHEN m.voltage_level ILIKE '%%Hochspannung/Mittelspannung%%' THEN 'HV/MV'
                          WHEN m.voltage_level ILIKE 'Hochspannung%%' THEN 'HV'
                          WHEN m.voltage_level ILIKE '%%Höchstspannung%%' THEN 'EHV' END"""

# The MV data of an MV/LV substation in the selected MV run (left out without MV tables or run).
STATION_MV_JOIN = """LEFT JOIN pylovo.mv_node n ON n.version_id = g.version_id AND n.mv_variant_id = %(mv)s
                       AND n.grid_result_id = g.grid_result_id AND n.kind = 'station'"""


def run(alias: str = "") -> str:
    """Filter on one MV run (``alias.`` prefixed when given)."""
    a = f"{alias}." if alias else ""
    return f"{a}version_id = %(version)s AND {a}mv_variant_id = %(mv)s"


RUN = run()


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


# Grid ids of a region (postcode, MV supply area, grid), for the key figures.
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
    sql: str = ""                     # placeholders: {env} (map view), {mv_cols} {mv_join} (station MV data)
    detail: str = ""                  # one feature, parameter %(fid)s
    requires: tuple[str, ...] = ()    # "schema.table" or "schema.table(col, col)"
    rows: tuple[str, ...] = ()        # tables that must hold rows
    parent: str | None = None         # shown and hidden with this layer
    with_run: bool = False            # a version layer that adds data of the MV run when one is given
    default: bool = True
    min_zoom: float = 0.0
    cap: int = 60_000
    attribution: str | None = None
    post: tuple[str, ...] = ()        # post-processing steps, see LayerService._post


def build_layers() -> list[Layer]:
    """All layers in panel order."""
    in_view = "{env}"
    return [
        # ---------------------------------------------------------------------------------- regions
        Layer("postcodes", "regions", "Postcode areas",
              "Postcode areas of the version, with the number of grids. Click one to select it.",
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
              requires=("pylovo.postcode_result", "pylovo.postcode")),
        # --------------------------------------------------------------------------------- HV grid
        Layer("hv_lines", "hv", "HV and EHV lines",
              "The 110, 220 and 380 kV grid of Germany from eGon-data (eTraGo, PyPSA format). Download it "
              "once with: pylovo-viewer fetch etrago",
              "line", "file", "eGon-data eTraGo (Open Energy Platform)",
              attribution="eGon-data eTraGo grid (osmTGmod), © OpenStreetMap contributors (ODbL)"),
        Layer("hv_substations", "hv", "HV/MV substations",
              "The HV/MV substations of the MV run that supply the MV grids; candidates the run did not use "
              "are drawn hollow. Their site outlines show from zoom 12.",
              "point", "run", "pylovo.mv_substation",
              sql=f"""SELECT s.substation_id AS fid, s.source, s.name, s.operator, s.voltage_tag AS voltage,
                             s.egon_bus_id AS etrago_bus, s.n_transformers AS transformers, s.transformer_mva AS mva,
                             {num("s.loading_n0_percent", 1)} AS loading, {num("s.loading_n1_percent", 1)} AS loading_n1,
                             array_to_string(s.flags, ', ') AS flags, s.in_region,
                             EXISTS (SELECT 1 FROM pylovo.mv_grid g WHERE g.substation_id = s.substation_id) AS used,
                             {geo("s.geom")} AS geometry
                      FROM pylovo.mv_substation s WHERE {run('s')}""",
              detail="SELECT * FROM pylovo.mv_substation WHERE substation_id = %(fid)s",
              requires=("pylovo.mv_substation", "pylovo.mv_grid"), rows=("pylovo.mv_variant",)),
        Layer("hv_sites", "hv", "Substation sites", "Site outlines of the HV/MV substations.",
              "polygon", "run", "pylovo.mv_substation",
              sql=f"""SELECT s.substation_id AS fid, s.name, s.source, {geo("s.footprint")} AS geometry
                      FROM pylovo.mv_substation s WHERE {run('s')} AND s.footprint IS NOT NULL""",
              detail="SELECT * FROM pylovo.mv_substation WHERE substation_id = %(fid)s",
              requires=("pylovo.mv_substation(footprint)",), rows=("pylovo.mv_variant",),
              parent="hv_substations", min_zoom=12),
        # -------------------------------------------------------------------------------- MV grids
        Layer("mv_lines", "mv", "MV lines",
              "MV cables and overhead lines of the run: open rings, stubs, dedicated feeders and ties between "
              "supply areas, with the normally-open points.",
              "line", "run", "pylovo.mv_line",
              sql=f"""SELECT l.mv_line_id AS fid, l.mv_grid_id AS area, l.role, l.kind, l.ring_id AS ring,
                             l.feeder_id AS feeder, l.std_type, COALESCE(l.parallel, 1) AS parallel,
                             {num("l.length_km", 3)} AS length_km, {num("l.cable_km", 3)} AS cable_km,
                             {num("l.overhead_km", 3)} AS overhead_km, l.normally_open AS open,
                             {num("l.loading_load_percent", 1)} AS loading, {num("l.loading_n1_percent", 1)} AS loading_n1,
                             {num("l.loading_feed_in_percent", 1)} AS loading_feed_in,
                             {geo("ST_Simplify(l.geom, 1)")} AS geometry
                      FROM pylovo.mv_line l WHERE {run('l')}""",
              detail="SELECT * FROM pylovo.mv_line WHERE mv_line_id = %(fid)s",
              requires=("pylovo.mv_line",), rows=("pylovo.mv_variant",), post=("area_slots", "ring_slots")),
        Layer("mv_osm_lines", "mv", "OSM MV lines",
              "The MV overhead lines mapped in OpenStreetMap, as corridors in the routing graph of the run "
              "(MV_OVERHEAD_LINES osm): an MV line is an overhead line where it runs on them, else a cable.",
              "line", "run", "pylovo.mv_graph_edge",
              sql=f"""SELECT e.edge_id AS fid, e.src_id AS osm_way, e.objektart AS osm_type,
                             {num("e.length_m / 1000", 3)} AS length_km, {geo("ST_Simplify(e.geom, 2)")} AS geometry
                      FROM pylovo.mv_graph_edge e
                      WHERE e.graph_id = (SELECT v.graph_id FROM pylovo.mv_variant v
                                          WHERE v.version_id = %(version)s AND v.mv_variant_id = %(mv)s)
                        AND e.klasse = 'osm_mv_line'""",
              detail="""SELECT e.edge_id, e.src_id AS osm_way, e.objektart AS osm_type, e.length_m, e.graph_id
                        FROM pylovo.mv_graph_edge e
                        WHERE e.graph_id = (SELECT v.graph_id FROM pylovo.mv_variant v
                                            WHERE v.version_id = %(version)s AND v.mv_variant_id = %(mv)s)
                          AND e.edge_id = %(fid)s""",
              requires=("pylovo.mv_graph_edge", "pylovo.mv_variant"), rows=("pylovo.mv_variant",), default=False,
              attribution="© OpenStreetMap contributors (ODbL)"),
        Layer("mv_open_points", "mv", "Normally-open points",
              "Where each open ring is opened, drawn next to the node it opens at.",
              "point", "run", "pylovo.mv_line",
              sql=f"""SELECT l.mv_line_id AS fid, l.mv_grid_id AS area, l.ring_id AS ring, n.mv_node_id AS node,
                             {geo('''ST_LineInterpolatePoint(l.geom, CASE
                                 WHEN COALESCE(l.open_at_node, l.to_node) = l.from_node
                                   THEN LEAST(0.5, 12 / NULLIF(ST_Length(l.geom), 0))
                                 ELSE GREATEST(0.5, 1 - 12 / NULLIF(ST_Length(l.geom), 0)) END)''')} AS geometry
                      FROM pylovo.mv_line l JOIN pylovo.mv_node n ON n.mv_node_id = COALESCE(l.open_at_node, l.to_node)
                      WHERE {run('l')} AND l.normally_open AND l.role <> 'tie' AND ST_Length(l.geom) > 0""",
              detail="SELECT * FROM pylovo.mv_line WHERE mv_line_id = %(fid)s",
              requires=("pylovo.mv_line", "pylovo.mv_node"), rows=("pylovo.mv_variant",),
              parent="mv_lines", min_zoom=11),
        Layer("mv_stations", "mv", "MV/LV substations",
              "The transformer station of every LV grid: size, power-flow result of its LV grid and, with an "
              "MV run, its MV ring and voltage.",
              "point", "version", "pylovo.grid_result + transformer_positions (+ mv_node)",
              sql=f"""SELECT g.grid_result_id AS fid, g.kcid, g.bcid, g.plz, g.transformer_rated_power AS kva,
                             g.power_flow_status AS status,
                             CASE WHEN tp.lod2 THEN 'lod2' WHEN tp.osm THEN 'osm' ELSE 'new' END AS origin
                             {{mv_cols}}, {geo("tp.geom")} AS geometry
                      FROM pylovo.grid_result g
                      JOIN pylovo.transformer_positions tp ON tp.grid_result_id = g.grid_result_id
                      {{mv_join}}
                      WHERE g.version_id = %(version)s""",
              detail="grid", requires=("pylovo.grid_result", "pylovo.transformer_positions"),
              with_run=True, post=("ring_slots",)),
        Layer("mv_areas", "mv", "Supply areas",
              "Area supplied by each HV/MV substation (MV grid district), with its voltage and status. Areas cut "
              "by the border of the planning region have a dashed outline.",
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
              requires=("pylovo.mv_grid", "pylovo.mv_substation"), rows=("pylovo.mv_variant",),
              post=("area_slots",)),
        Layer("mv_customers", "mv", "MV customers",
              "Buildings connected directly to the MV grid (large non-residential loads).",
              "polygon", "version", "pylovo.mv_customers",
              sql=f"""SELECT c.objectid AS fid, c.building_use AS use, c.nonresidential_use AS nonres_use,
                             {num("COALESCE(c.nonresidential_peak_load_in_kw, 0) + COALESCE(c.residential_peak_load_in_kw, 0)", 1)}
                               AS peak_kw, c.households, {geo("c.geom")} AS geometry
                      FROM pylovo.mv_customers c WHERE c.version_id = %(version)s""",
              detail="SELECT * FROM pylovo.mv_customers WHERE version_id = %(version)s AND objectid = %(fid)s",
              requires=("pylovo.mv_customers",), rows=("pylovo.mv_customers",), min_zoom=12),
        # -------------------------------------------------------------------------------- LV grids
        Layer("lv_areas", "lv", "Grid areas",
              "Area of each LV grid (convex hull of its buildings), coloured so that neighbours differ.",
              "polygon", "version", "pylovo.grid_result + buildings_result",
              sql=f"""SELECT g.grid_result_id AS fid, g.kcid, g.bcid, g.plz, g.transformer_rated_power AS kva,
                             g.power_flow_status AS status, count(*) AS buildings,
                             {geo("ST_SimplifyPreserveTopology(ST_Buffer(ST_ConvexHull(ST_Collect(b.centroid)), 8, 2), 2)")}
                               AS geometry
                      FROM pylovo.grid_result g JOIN pylovo.buildings_result b ON b.grid_result_id = g.grid_result_id
                      WHERE g.version_id = %(version)s GROUP BY g.grid_result_id""",
              detail="grid", requires=("pylovo.grid_result", "pylovo.buildings_result"), post=("grid_slots",)),
        Layer("lv_cables", "lv", "Cables",
              "LV feeder cables along the streets and service cables to the buildings, with the cable cabinets. "
              "Shown when zoomed in.",
              "line", "view", "pylovo.lines_result_cache",
              sql=f"""SELECT c.id AS fid, c.grid_result_id AS grid, c.std_type, COALESCE(c.parallel, 1) AS parallel,
                             {num("c.length_km * 1000", 1)} AS length_m,
                             CASE WHEN c.feeder_section_id IS NULL THEN 'service' ELSE 'feeder' END AS role,
                             NULLIF(substring(c.std_type from '([0-9]+)$'), '')::int AS mm2,
                             {geo("c.geom")} AS geometry
                      FROM pylovo.lines_result_cache c WHERE c.version_id = %(version)s AND c.geom && {in_view}""",
              detail="""SELECT c.*, l.pp_index, l.max_i_ka, l.r_ohm_per_km, l.x_ohm_per_km, l.feeder_sizing_basis,
                               l.service_sizing_basis, l.service_selected_voltage_drop_percent,
                               l.total_design_voltage_drop_percent
                        FROM pylovo.lines_result_cache c
                        LEFT JOIN pylovo.pandapower_line l ON l.grid_result_id = c.grid_result_id
                         AND c.line_name = regexp_replace(l.name, '^Line to ', 'L')
                        WHERE c.id = %(fid)s""",
              requires=("pylovo.lines_result_cache(feeder_section_id, version_id)", "pylovo.pandapower_line"),
              min_zoom=13, post=("grid_slots", "cable_feeders")),
        Layer("lv_cabinets", "lv", "Cable cabinets",
              "Cable distribution cabinets where feeders split, with the number of outgoing cables.",
              "point", "view", "pylovo.split_points",
              sql=f"""SELECT sp.split_point_id AS fid, sp.grid_result_id AS grid, sp.split_type,
                             sp.outgoing_count AS outgoing, {geo("sp.geom")} AS geometry
                      FROM pylovo.split_points sp
                      WHERE sp.geom && {in_view} AND sp.grid_result_id IN
                            (SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(version)s)""",
              detail="SELECT * FROM pylovo.split_points WHERE split_point_id = %(fid)s",
              requires=("pylovo.split_points",), parent="lv_cables", min_zoom=15, post=("grid_slots",)),
        Layer("lv_buildings", "lv", "Buildings",
              "Buildings supplied by the LV grids, with type, households and peak load. Shown when zoomed in.",
              "polygon", "view", "pylovo.buildings_result",
              sql=f"""SELECT b.objectid AS fid, b.grid_result_id AS grid, b.type, b.building_use AS use,
                             b.households, {num("b.peak_load_in_kw", 1)} AS peak_kw, {num("b.height", 1)} AS height,
                             b.floor_number AS floors, b.nonresidential_mv_direct AS mv_direct, b.vertice_id,
                             {geo("b.geom")} AS geometry
                      FROM pylovo.buildings_result b WHERE b.version_id = %(version)s AND b.geom && {in_view}""",
              detail="SELECT * FROM pylovo.buildings_result WHERE version_id = %(version)s AND objectid = %(fid)s",
              requires=("pylovo.buildings_result",), min_zoom=13, post=("grid_slots", "building_feeders")),
        # ------------------------------------------------------------------------------ generation
        Layer("mastr", "generation", "MaStR units",
              "Generating units and storage from the Marktstammdatenregister, by technology and grid level.",
              "point", "bbox", "pylovo.mastr_units",
              sql=f"""SELECT m.mastr_id AS fid, m.technology, {num("m.net_power_kw", 1)} AS kw, {MASTR_LEVEL_SQL} AS level,
                             m.status, extract(year FROM m.commissioning_date)::int AS year, {geo("m.geom")} AS geometry
                      FROM pylovo.mastr_units m WHERE m.geom && {in_view}""",
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
        self.layers = {layer.id: layer for layer in build_layers()}
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
            out.append({"id": layer.id, "group": layer.group, "title": layer.title, "description": layer.description,
                        "geometry": layer.geometry, "scope": layer.scope, "source": layer.source,
                        "parent": layer.parent, "with_run": layer.with_run, "default": layer.default,
                        "min_zoom": layer.min_zoom,
                        "attribution": layer.attribution, "available": reason is None, "reason": reason})
        return out

    @staticmethod
    def groups() -> list[dict[str, Any]]:
        return [{"id": gid, "title": title, "description": text, "default": on}
                for gid, (title, text, on) in GROUPS.items()]

    def clear(self) -> None:
        self._slots.clear()
        self._feeders.clear()

    # -------------------------------------------------------------------------------------- data
    def _check(self, layer: Layer, q: Query) -> None:
        needs = {"version": ("version",), "view": ("version", "bbox"), "run": ("version", "mv"),
                 "bbox": ("bbox",)}.get(layer.scope, ())
        missing = [name for name in needs if getattr(q, name) is None]
        if missing:
            raise ValueError(f"layer {layer.id} needs {', '.join(missing)}")

    def station_mv(self, q: Query) -> bool:
        """Whether the MV/LV substations can carry MV data (MV tables and a run)."""
        return q.mv is not None and self.catalog.has("pylovo.mv_node", "grid_result_id", "ring_id")

    def sql(self, layer: Layer, q: Query) -> str:
        epsg = self.db.settings.target_epsg
        env = f"ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326), {epsg})"
        mv = self.station_mv(q)
        cols = (f", n.mv_grid_id AS area, n.ring_id AS ring, n.feeder_id AS feeder, n.connection AS mv_connection, "
                f"{num('n.vm_load_pu', 4)} AS vm, {num('n.lv_busbar_vm_pu', 4)} AS lv_vm") if mv else ""
        return (layer.sql.replace("{env}", env).replace("{mv_cols}", cols)
                .replace("{mv_join}", STATION_MV_JOIN if mv else ""))

    def geojson(self, layer_id: str, q: Query) -> str:
        layer = self.layers[layer_id]
        self._check(layer, q)
        if layer.scope == "file":
            from pylovo_viewer import etrago
            blob = etrago.read(self.data_dir, "lines")
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
            mv = self.station_mv(q)
            sql = (GRID_DETAIL.replace("{grid_columns}", self._columns("pylovo.grid_result", "g"))
                   .replace("{mv_cols}", STATION_MV_COLUMNS if mv else "")
                   .replace("{mv_join}", STATION_MV_JOIN if mv else ""))
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
        if q.mv is None or not self.catalog.has("pylovo.mv_line"):
            return
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
       {{mv_cols}}
FROM pylovo.grid_result g LEFT JOIN pylovo.transformer_positions tp ON tp.grid_result_id = g.grid_result_id
{{mv_join}}
WHERE g.grid_result_id = %(fid)s"""
STATION_MV_COLUMNS = """, n.mv_node_id, n.mv_grid_id AS mv_supply_area, n.ring_id AS mv_ring, n.feeder_id AS mv_feeder,
       n.connection AS mv_connection, n.vm_load_pu AS mv_vm_load_pu, n.vm_n1_pu AS mv_vm_n1_pu, n.lv_busbar_vm_pu,
       n.mv_lv_tap_pos, array_to_string(n.flags, ', ') AS mv_flags"""

