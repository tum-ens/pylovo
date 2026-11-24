"""The region step of the MV stage without a database: attachment of the nodes, substations, nodes and districts,
with fake planning modules."""
from __future__ import annotations

import logging
import sys
import types

import numpy as np
import pandas as pd
import pytest
import shapely

from pylovo.mv_grid import preparation
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

LV_SNAPSHOT = {
    "load_calculation": {"sim_factor": {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6},
                         "consumer_categories": [{"definition": "Commercial", "peak_load_per_m2": 79.0},
                                                 {"definition": "Public", "peak_load_per_m2": 29.0},
                                                 {"definition": "Unknown", "peak_load_per_m2": 29.0}],
                         "default_power_factor": 0.95},
    "cable_dimensioning": {"mv_direct_connection_load_threshold_kw": 300},
    "power_flow_assessment": {"lv_reference_voltage_pu": 0.96},
}
LV = LvVersionParameters.from_generation_parameters("mv1", LV_SNAPSHOT)


def test_attachment_moves_unmapped_customers_to_the_street():
    rows = pd.DataFrame({"mv_node_id": [1, 2, 3, 4], "kind": ["busbar", "station", "customer", "customer"],
                         "mapped_station": [None, None, False, True], "x": [0.0, 1.0, 2.0, 3.0],
                         "y": [0.0, 0.0, 50.0, 60.0]})
    attach = pd.DataFrame({"key": pd.array([4, 3, 2, 1], dtype="Int64"),        # as MvGraphMixin.attach_points
                           "edge_id": pd.array([7, 6, 5, 4], dtype="Int64"), "component": 1,
                           "in_settlement": [True, True, False, True],
                           "settlement_id": pd.array([9, 9, None, 2], dtype="Int64"),
                           "settlement_urban": [False, True, True, False], "fraction": [0.1, 0.2, 0.3, 0.0],
                           "offset_m": [61.0, 48.0, 0.0, 400.0], "ax": [3.0, 2.0, 1.0, 0.0],
                           "ay": [0.0, 2.0, 0.0, 1.0]})
    out = preparation._attachment_updates(rows, attach, MvParameters(), logging.getLogger("t"))
    assert out["mv_node_id"].tolist() == [1, 2, 3, 4]
    assert out["attach_edge_id"].tolist() == [4, 5, 6, 7]
    assert out["area_class"].tolist() == ["rural", "rural", "urban", "rural"]   # station 2: open field
    assert out["settlement_id"].isna().tolist() == [False, True, False, False]
    assert out.loc[2, ["x", "y", "attach_offset_m"]].tolist() == [2.0, 2.0, 0.0]   # transfer point
    assert out.loc[3, "attach_offset_m"] == 61.0 and np.isnan(out.loc[3, "x"])   # mapped customer stays
    assert out["flags"].tolist() == [[], [], ["transfer_point"], []]


class RegionDb:
    """Fake client for the region step: ids from 1, three sites, two stations and a customer."""

    def __init__(self):
        self.calls: dict[str, list] = {}
        self.cur = types.SimpleNamespace(execute=lambda sql, *a: self.calls.setdefault("sql", []).append(sql))
        self.inserted_nodes = None

    def _record(self, name, *args):
        self.calls.setdefault(name, []).append(args)

    def egon_available(self):
        return True, "opendata"

    def egon_substations(self, region, buffer_m):
        return pd.DataFrame({"bus_id": [1], "x": [0.0], "y": [0.0]})

    def osm_substation_candidates(self, region, buffer_m):
        return pd.DataFrame({"source_id": ["way/1", "way/2", "way/3"]})

    def osm_hv_lines(self, region, reach_m):
        self._record("osm_hv_lines", reach_m)
        return pd.DataFrame({"subtype": ["line"], "tags": [{"voltage": "110000"}], "wkb": [None]})

    def insert_substations(self, version_id, variant_id, sites):
        self._record("insert_substations", sites.copy())
        return np.array([11, 12, 13])

    def station_nodes(self, version_id, plz):
        return pd.DataFrame({"grid_result_id": [7, 8]})

    def station_load_stats(self, version_id, plz):
        return pd.DataFrame()

    def pandapower_load_check(self, version_id, plz):
        return pd.DataFrame({"grid_result_id": [7, 8], "p_kw": [10.0, 20.0]})

    def mv_customer_rows(self, version_id, plz, lv):
        return pd.DataFrame()

    def insert_nodes(self, version_id, variant_id, rows):
        self.inserted_nodes = rows.copy()
        return np.arange(101, 101 + len(rows))

    def attach_points(self, graph_id, points):
        offsets = {101: 50.0, 102: 2500.0, 103: 10.0, 104: 0.0, 105: 0.0, 106: 40.0}
        keys = points["key"].to_numpy()
        return pd.DataFrame({"key": pd.array(keys, dtype="Int64"), "edge_id": pd.array(keys + 1000, dtype="Int64"),
                             "component": 1, "in_settlement": True,
                             "settlement_id": pd.array([5] * len(keys), dtype="Int64"),
                             "settlement_urban": False, "fraction": 0.5,
                             "offset_m": [offsets[int(k)] for k in keys], "ax": points["x"] + 1.0, "ay": 0.0})

    def update_node_attachments(self, rows):
        self._record("update_node_attachments", rows.copy())

    def flag_substations(self, ids, flag):
        self._record("flag_substations", list(ids), flag)

    def variant_node_table(self, version_id, variant_id):
        rows = self.inserted_nodes.assign(mv_node_id=np.arange(101, 101 + len(self.inserted_nodes)))
        return rows.set_index("mv_node_id")[["kind", "connection_hint", "x", "y"]].assign(
            attach_edge_id=lambda f: f.index + 1000)

    def region_graph_arrays(self, graph_id):
        ids = np.arange(1100, 1108)
        return ids, ids * 10, ids * 10 + 1, np.full(len(ids), 100.0)

    def edge_municipalities(self, graph_id):
        ids = np.arange(1100, 1108)
        return pd.Series(["09000001"] * len(ids), index=pd.Index(ids, name="edge_id"), dtype=object, name="ags")

    def create_grids(self, version_id, variant_id, substation_ids, vn_kv):
        self._record("create_grids", list(substation_ids), vn_kv)
        return {int(s): 900 + i for i, s in enumerate(substation_ids)}

    def set_node_grids(self, assignment):
        self._record("set_node_grids", assignment.copy())

    def write_grid_areas(self, version_id, variant_id, plz):
        self._record("write_grid_areas", list(plz))

    def grid_areas(self, version_id, variant_id):
        return {900: 0.25}

    def municipality_areas(self, ags, region_wkb):
        self._record("municipality_areas", list(ags))
        return {"09000001": 0.25}

    def set_grid_voltages(self, version_id, variant_id, voltages, records):
        self._record("set_grid_voltages", voltages.copy(), records)


@pytest.fixture()
def region_modules(monkeypatch):
    """Fake substations, inputs, districts and graph modules for the region step."""
    recorded: dict[str, object] = {}
    subs = types.ModuleType("pylovo.mv_grid.substations")
    sites = [types.SimpleNamespace(key=f"way/{i}") for i in (1, 2, 3)]
    subs.select_osm_sites = lambda candidates, params: types.SimpleNamespace(sites=sites, describe=lambda: "3 sites")
    subs.match_egon = lambda s, egon, max_m: [(1, 0.0), (None, None), (None, None)]
    subs.HV_LINE_END_M = 50.0
    subs.hv_line_ends = lambda s, lines: [True, False, True] if len(lines) else [False] * len(s)

    def sites_frame(s, region_wkb=None, matches=None, hv_lines=None):
        recorded["hv_lines"] = hv_lines
        return pd.DataFrame({
            "source": "osm", "source_ids": [[x.key] for x in s],
            "egon_bus_id": pd.array([1, None, None], dtype="Int64"), "in_region": [True, False, True],
            "osm_ratings_mva": None, "flags": [[], [], []], "x": [0.0, 5000.0, 900.0], "y": [0.0, 0.0, 0.0],
            "footprint_wkb": None})

    subs.sites_frame = sites_frame
    inputs = types.ModuleType("pylovo.mv_grid.inputs")
    inputs.station_table = lambda st, stats, lv: pd.DataFrame({
        "kind": "station", "connection_hint": "ring", "grid_result_id": [7, 8], "x": [10.0, 20.0], "y": 0.0,
        "p_own_kw": [10.0, 20.0], "sn_kva": [400.0, 630.0]})
    inputs.customer_table = lambda rows, lv, params: pd.DataFrame({
        "kind": ["customer"], "connection_hint": ["ring"], "x": [30.0], "y": [40.0], "mapped_station": [False],
        "installed_kw": [950.0]})

    def busbar_customers(stations, customers, params):
        recorded["split"] = len(customers)
        return stations, customers

    inputs.busbar_customers = busbar_customers

    def upsize(stations, lv):
        recorded["upsize_lv"] = lv
        out = stations.copy()
        out.attrs["n_upsized"] = 1
        return out

    inputs.upsize_busbar_stations = upsize
    inputs.describe_customers = lambda attrs, params: f"MV customers, {attrs.get('n_upsized_stations')} upsized"
    def node_rows(stations, customers, sites_frame):
        recorded["stations"] = stations
        busbars = pd.DataFrame({"kind": "busbar", "connection_hint": "busbar",
                                "substation_id": sites_frame["substation_id"], "x": sites_frame["x"], "y": 0.0})
        return pd.concat([busbars, stations, customers], ignore_index=True)

    inputs.node_rows = node_rows
    from pylovo.mv_grid import districts as real_districts

    districts = types.ModuleType("pylovo.mv_grid.districts")
    for name in ("installed_density", "district_voltages", "voltage_record", "describe_voltages",
                 "municipal_density", "dense_share", "node_installed_kva"):
        setattr(districts, name, getattr(real_districts, name))
    districts.site_capacity_mva = lambda sites, params: (recorded.__setitem__("sites", sites) or np.full(len(sites),
                                                                                                         63.0))

    def routed(csr, vertex_index, edge_len, sites, nodes, g, pf, capacity):
        recorded["nodes"] = nodes
        return types.SimpleNamespace(site_of_node=np.zeros(len(nodes), dtype=np.int64), overloaded=[0],
                                     bridged=np.array([False, True, False]))

    districts.routed_assignment = routed
    graph = types.ModuleType("pylovo.mv_grid.graph")
    graph.region_csr = lambda source, target, length: ("csr", {})
    for name, module in (("substations", subs), ("inputs", inputs), ("districts", districts), ("graph", graph)):
        monkeypatch.setitem(sys.modules, f"pylovo.mv_grid.{name}", module)
    return recorded


def test_region_step_writes_substations_nodes_and_districts(region_modules):
    db = RegionDb()
    warnings: list[str] = []
    info = preparation.region_step(db, "mv1", "d0", [1, 2], shapely.box(0, 0, 1, 1).wkb, "g1",
                              MvParameters(), LV, logging.getLogger("t"), warnings)
    assert region_modules["stations"]["grid_result_id"].tolist() == [7, 8]
    assert region_modules["split"] == 1                                   # the small customers are split off
    assert region_modules["upsize_lv"] is LV                              # then overloaded stations are upsized
    # the OSM HV lines are read within the search buffer plus the end distance and checked at the sites
    assert db.calls["osm_hv_lines"] == [(5050.0,)] and region_modules["hv_lines"] == [True, False, True]
    nodes = db.inserted_nodes
    assert nodes["kind"].tolist() == ["busbar"] * 3 + ["station", "station", "customer"]


    # the customer moves to its street transfer point
    update = db.calls["update_node_attachments"][0][0].set_index("mv_node_id")
    assert update.loc[106, ["x", "y", "attach_offset_m"]].tolist() == [31.0, 0.0, 0.0]
    assert update.loc[106, "flags"] == ["transfer_point"] and update.loc[104, "flags"] == []
    assert update["attach_edge_id"].dtype == np.float64 and update.loc[101, "area_class"] == "rural"
    # substation 12 is 2.5 km from the streets: no supply area; 13 gets no node
    flags = {flag: ids for ids, flag in db.calls["flag_substations"]}
    assert flags["long_attach"] == [12] and flags["no_nodes"] == [13] and flags["over_capacity"] == [11]
    assert region_modules["sites"]["substation_id"].tolist() == [11, 13]
    assert {"edge_source", "edge_target", "edge_length_m", "attach_edge_id"} <= set(region_modules["sites"].columns)
    assert region_modules["sites"]["edge_source"].tolist() == [11010, 11030]    # edges 1101, 1103
    assert region_modules["nodes"]["kind"].tolist() == ["station", "station", "customer"]
    assert db.calls["create_grids"] == [([11], 20.0)]
    grids = db.calls["set_node_grids"][0][0]
    assert grids["mv_node_id"].tolist() == [104, 105, 106] and grids["mv_grid_id"].tolist() == [900] * 3
    assert grids["bridged"].tolist() == [False, True, False]
    assert db.calls["write_grid_areas"] == [([1, 2],)]
    # nominal voltage of the district: (400 + 630 kVA + 950 kW / 0.95) / 0.25 km² = 8.12 MVA/km² -> 10 kV (auto)
    voltages, records = db.calls["set_grid_voltages"][0]
    assert voltages.loc[900, "density_mva_km2"] == pytest.approx(8.12) and voltages.loc[900, "vn_kv"] == 10.0
    assert records[900]["rule"] == "auto" and records[900]["threshold_mva_km2"] == 5.0
    # the municipality of the nodes' street edges holds all of the district's capacity: 8.12 MVA/km² -> dense
    assert db.calls["municipality_areas"] == [(["09000001"],)]
    assert records[900]["scope"] == "municipality" and records[900]["dense_share"] == pytest.approx(1.0)
    assert (records[900]["n_stations"], records[900]["n_customers"]) == (2, 1)
    assert info == {"n_sites": 3, "n_grids": 1, "kinds": {"station": 2, "customer": 1}}
    # the substation that the street graph does not reach is a warning of the summary, which names the likely cause
    assert warnings == ["1 of 3 HV/MV substations are more than 2000 m from the street graph and supply nothing "
                        "(long_attach), so the supply areas go to the other 2; check that the InfDB has the streets "
                        "of the municipalities within 5 km of the region"]
    assert any("SAVEPOINT" in s for s in db.calls["sql"])


def test_region_step_without_long_attach_has_no_warning(region_modules, monkeypatch):
    db = RegionDb()
    attach = db.attach_points
    monkeypatch.setattr(db, "attach_points", lambda graph_id, points: attach(graph_id, points).assign(
        offset_m=lambda f: f["offset_m"].clip(upper=1000.0)))
    warnings: list[str] = []
    preparation.region_step(db, "mv1", "d0", [1, 2], shapely.box(0, 0, 1, 1).wkb, "g1", MvParameters(), LV,
                       logging.getLogger("t"), warnings)
    flags = {flag: ids for ids, flag in db.calls["flag_substations"]}
    assert flags["long_attach"] == [] and warnings == []
