"""Tests of the MV grid metrics (generic format, feeder and ring detection, readers, literature ranges)."""

import json
import math

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import pandapower as pp
import pandapower.networks as pn
import pandapower.topology as top
import pytest
from pyproj import Transformer
from shapely.geometry import LineString, box

from pylovo.mv_grid import metrics as m


def _nodes(rows):
    return pd.DataFrame(rows, columns=["node_id", "kind", "x", "y"])


def _lines(rows):
    df = pd.DataFrame(rows, columns=["line_id", "from_node", "to_node", "length_km", "kind", "normally_open",
                                     "open_at_node"])
    return df


def _ring_toy():
    """One open ring with a joint, a stub station and a stub generator (hand-counted in the tests)."""
    nodes = _nodes([
        (0, "busbar", 0.0, 0.0),
        (1, "station", 1000.0, 0.0),
        (2, "station", 2000.0, 0.0),
        (3, "station", 3000.0, 1000.0),
        (4, "station", 0.0, 2000.0),
        (5, "joint", 2500.0, 500.0),
        (6, "station", 2500.0, 800.0),
        (7, "customer", 1500.0, 2000.0),
        (8, "generator", 2900.0, 400.0),
    ])
    lines = _lines([
        (10, 0, 1, 1.0, "cable", False, None),
        (11, 1, 2, 1.0, "cable", False, None),
        (12, 2, 5, 0.5, "cable", False, None),
        (13, 5, 3, 0.5, "cable", False, None),
        (14, 3, 7, 1.0, "cable", True, 3),          # NOP, open switch at station 3
        (15, 7, 4, 1.0, "cable", False, None),
        (16, 4, 0, 2.0, "overhead", False, None),
        (17, 5, 6, 0.3, "cable", False, None),      # stub station
        (18, 5, 8, 0.7, "cable", False, None),      # stub generator
    ])
    return nodes, lines


def test_toy_ring_feeders_and_ring():
    nodes, lines = _ring_toy()
    feeders = m.feeder_table(nodes, lines)
    assert list(feeders["kind"]) == ["ring", "ring"]
    a = feeders.set_index("feeder").loc[0]           # feeder of node 1 (lowest node id first)
    b = feeders.set_index("feeder").loc[1]
    assert a["n_members"] == 3 and a["n_stub_nodes"] == 2 and a["feeder_supply_points"] == 4
    assert a["feeder_km"] == pytest.approx(3.0) and a["stub_km"] == pytest.approx(1.0)
    # the NOP line counts with feeder B (its closed end is the customer 7)
    assert b["n_members"] == 2 and b["feeder_km"] == pytest.approx(4.0) and b["n_nops"] == 1
    rings = m.ring_table(nodes, lines)
    assert len(rings) == 1
    ring = rings.iloc[0]
    assert ring["kind"] == "ring" and ring["feeders"] == (0, 1)
    assert ring["ring_members"] == 5 and ring["ring_members_incl_stubs"] == 7
    assert ring["ring_km"] == pytest.approx(7.0) and ring["ring_km_incl_stubs"] == pytest.approx(8.0)
    roles = m.node_roles(nodes, lines).set_index("node_id")
    assert roles.loc[0, "position"] == "busbar"
    assert set(roles.index[roles["position"] == "stub"]) == {6, 8}
    assert roles.loc[5, "position"] == "main"


def test_toy_topology_metrics():
    nodes, lines = _ring_toy()
    tm = m.topology_metrics(nodes, lines)
    assert tm["n_busbars"] == 1 and tm["n_supply_points"] == 6
    assert tm["n_nops"] == 1 and tm["n_ties"] == 0 and tm["n_rings"] == 1
    assert tm["n_feeder_bays"] == 2 and tm["feeders_per_substation"] == 2.0
    assert tm["circuit_km"] == pytest.approx(8.0)
    assert tm["route_km"] == pytest.approx(8.0)
    assert tm["cable_share"] == pytest.approx(0.75)
    assert tm["km_per_supply_point"] == pytest.approx(8.0 / 6.0)
    assert tm["ring_members_mean"] == 5.0
    assert tm["mean_section_km"] == pytest.approx(7.0 / 6.0)
    assert tm["n1_secure_share"] == pytest.approx(5 / 6) and tm["stub_share"] == pytest.approx(1 / 6)
    assert tm["customer_share"] == pytest.approx(1 / 6)
    assert tm["station_spacing_m"] > 0
    json.dumps(tm, allow_nan=False)                     # stored as jsonb: no NaN


def test_two_leg_radial_tie_island_and_mesh():
    nodes, lines = _ring_toy()
    nodes = pd.concat([nodes, _nodes([
        (20, "busbar", 9000.0, 0.0),
        (21, "station", 9000.0, 1000.0),
        (22, "station", 9000.0, 2000.0),
        (23, "customer", 10000.0, 0.0),
        (24, "station", 20000.0, 0.0),                  # no line: island
    ])], ignore_index=True)
    lines = pd.concat([lines, _lines([
        (30, 20, 21, 1.0, "cable", False, None),
        (31, 21, 22, 1.0, "cable", False, None),
        (32, 20, 23, 2.0, "cable", False, None),        # leg 1
        (33, 20, 23, 2.0, "cable", True, 23),           # leg 2, open at the customer
        (34, 22, 4, 1.5, "cable", True, 22),            # tie to the other substation
    ])], ignore_index=True)
    feeders = m.feeder_table(nodes, lines).set_index("feeder")
    kinds = dict(zip(feeders["busbar"].astype(str) + ":" + feeders["kind"], feeders["n_bays"]))
    assert kinds == {"0:ring": 1, "20:radial": 1, "20:two_leg": 2, "None:island": 0}
    assert feeders.loc[feeders["kind"] == "radial", "has_tie"].item()
    rings = m.ring_table(nodes, lines).set_index("kind")
    assert rings.loc["two_leg", "ring_km"] == pytest.approx(4.0)
    tm = m.topology_metrics(nodes, lines)
    assert tm["n_ties"] == 1 and tm["tie_km"] == pytest.approx(1.5)
    assert tm["circuit_km"] == pytest.approx(8.0 + 6.0)            # tie excluded
    assert tm["n_feeder_bays"] == 5 and tm["feeders_per_substation"] == 2.5
    assert tm["n_two_leg"] == 1 and tm["n_radial"] == 1 and tm["n_islands"] == 1
    assert tm["unsupplied_share"] == pytest.approx(1 / 10)
    # (n-1) secure: ring main paths (5) + two-leg customer (1); stub side: stub 6 + radial 21, 22
    assert tm["n1_secure_share"] == pytest.approx(6 / 10) and tm["stub_share"] == pytest.approx(3 / 10)

    # three feeders of one busbar joined by two NOP lines form an open mesh
    mesh_nodes = _nodes([(0, "busbar", 0, 0), (1, "station", 1, 0), (2, "station", 2, 0), (3, "station", 3, 0)])
    mesh_lines = _lines([(1, 0, 1, 1.0, "cable", False, None), (2, 0, 2, 1.0, "cable", False, None),
                         (3, 0, 3, 1.0, "cable", False, None), (4, 1, 2, 1.0, "cable", True, None),
                         (5, 2, 3, 1.0, "cable", True, None)])
    mesh = m.ring_table(mesh_nodes, mesh_lines)
    assert list(mesh["kind"]) == ["mesh"] and mesh.iloc[0]["ring_members"] == 3


def test_metrics_are_deterministic_and_independent_of_row_order():
    nodes, lines = _ring_toy()
    first = m.topology_metrics(nodes, lines)
    shuffled = m.topology_metrics(nodes.sample(frac=1.0, random_state=1), lines.sample(frac=1.0, random_state=2))
    assert first == shuffled
    pd.testing.assert_frame_equal(m.feeder_table(nodes, lines), m.feeder_table(nodes, lines))


def test_normalise_tables_defaults_and_errors():
    nodes = _nodes([(1, "busbar", 0, 0), (2, "station", 1, 0)])
    lines = pd.DataFrame({"from_node": [1], "to_node": [2], "length_km": [0.5], "kind": ["overhead"]})
    n, ln = m.normalise_tables(nodes, lines)
    assert ln.loc[0, "overhead_km"] == 0.5 and ln.loc[0, "cable_km"] == 0.0
    assert not ln.loc[0, "normally_open"] and ln.loc[0, "parallel"] == 1 and ln.loc[0, "open_at_node"] is None
    with pytest.raises(ValueError, match="unknown node kinds"):
        m.normalise_tables(_nodes([(1, "substation", 0, 0)]), lines)
    with pytest.raises(ValueError, match="mixed lines"):
        m.normalise_tables(nodes, lines.assign(kind="mixed"))
    with pytest.raises(ValueError, match="duplicate"):
        m.normalise_tables(pd.concat([nodes, nodes]), lines)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")    # pandapower: mv_oberrhein has no tap_dependency_table
def test_mv_oberrhein_hand_count():
    net = pn.mv_oberrhein()
    nodes, lines = m.net_to_tables(net)
    assert nodes["kind"].value_counts().to_dict() == {"station": 141, "joint": 16, "generator": 12,
                                                      "customer": 6, "busbar": 2}
    assert int(lines["normally_open"].sum()) == 6
    tm = m.topology_metrics(nodes, lines)
    assert tm["n_busbars"] == 2 and tm["n_hv_mv_transformers"] == 2
    assert tm["n_nops"] + tm["n_ties"] == 6
    # 3 NOPs join the feeders of the two substations, 2 close a ring per substation, 1 is inside one feeder
    assert tm["n_ties"] == 3 and tm["n_rings"] == 2
    assert tm["circuit_km"] + tm["tie_km"] == pytest.approx(net.line["length_km"].sum())
    assert tm["cable_km"] + tm["overhead_km"] == pytest.approx(tm["circuit_km"])
    # feeders = components without the busbars and the open switches (independent pandapower count)
    graph = top.create_nxgraph(net, respect_switches=True)
    graph.remove_nodes_from(list(net.trafo["lv_bus"]) + list(net.trafo["hv_bus"]))
    assert tm["n_feeders"] == nx.number_connected_components(graph) == 4
    assert tm["n_feeder_bays"] == 4
    # projected coordinates (EPSG:25832)
    assert nodes["x"].between(3e5, 7e5).all() and nodes["y"].between(5.2e6, 5.5e6).all()


def test_net_to_tables_stage_net_columns_switches_and_mixed_lines():
    net = pp.create_empty_network()
    hv = pp.create_bus(net, 110.0, name="HV busbar 1")
    bb = pp.create_bus(net, 20.0, name="MV busbar 1", type="b")
    b1 = pp.create_bus(net, 20.0, name="MV node 1")
    b2 = pp.create_bus(net, 20.0, name="MV node 2")
    b2b = pp.create_bus(net, 20.0, name="MV node 2 second bus")
    b3 = pp.create_bus(net, 20.0, name="MV node 3")
    net.bus["kind"] = ["busbar", "busbar", "station", "station", "joint", "customer"]
    pp.create_ext_grid(net, hv)
    pp.create_transformer(net, hv, bb, "25 MVA 110/20 kV")
    pp.create_transformer(net, hv, bb, "25 MVA 110/20 kV")
    r_c, r_o = 0.122, 0.2376
    l1 = pp.create_line_from_parameters(net, bb, b1, 2.0, 0.3 * r_c + 0.7 * r_o, 0.3, 100.0, 0.41)
    l2 = pp.create_line_from_parameters(net, b1, b2, 1.0, r_c, 0.112, 304.0, 0.421)
    l3 = pp.create_line_from_parameters(net, b3, bb, 1.5, r_c, 0.112, 304.0, 0.421)
    l4 = pp.create_line_from_parameters(net, b2b, b3, 0.8, r_c, 0.112, 304.0, 0.421)
    net.line["kind"] = ["mixed", "cable", "cable", "cable"]
    net.line["std_type_name"] = ["NA2XS2Y 1x240 RM/25 12/20 kV + 122-AL1/20-ST1A 20.0"] + [None] * 3
    pp.create_switch(net, b2, b2b, et="b", closed=True)          # fused into one node
    pp.create_switch(net, b3, l4, et="l", closed=False)          # NOP, open at the customer
    nodes, lines = m.net_to_tables(net, epsg=None)
    assert set(nodes["node_id"]) == {bb, b1, b2, b3}               # HV bus dropped, b2b fused into b2
    assert nodes.set_index("node_id").loc[bb, "n_transformers"] == 2
    assert nodes.set_index("node_id").loc[bb, "transformer_mva"] == 50.0
    lines = lines.set_index("line_id")
    assert lines.loc[l1, "cable_km"] == pytest.approx(0.6) and lines.loc[l1, "overhead_km"] == pytest.approx(1.4)
    assert lines.loc[l4, "normally_open"] and lines.loc[l4, "open_at_node"] == b3
    assert lines.loc[l4, "from_node"] == b2 and not lines.loc[l2, "normally_open"]
    assert lines.attrs["mixed_split_unknown"] == 0
    rings = m.ring_table(nodes, lines.reset_index())
    assert list(rings["kind"]) == ["ring"] and rings.iloc[0]["ring_km"] == pytest.approx(2.0 + 1.0 + 1.5 + 0.8)
    assert l3 in lines.index


def _write_ding0_fixture(folder):
    topo = folder / "topology"
    topo.mkdir(parents=True)
    (topo / "buses.csv").write_text(
        "name,v_nom,x,y,mv_grid_id,lv_grid_id,in_building\n"
        "Busbar_mvgd_1_MV,20.0,11.0,48.0,1,,False\n"
        "BusBar_mvgd_1_lvgd_1_MV,20.0,11.01,48.0,1,,False\n"
        "BusBar_mvgd_1_lvgd_2_MV,20.0,11.02,48.01,1,,False\n"
        "virtual_BusBar_mvgd_1_lvgd_2_MV,20.0,11.02,48.01,1,,False\n"
        "BranchTee_mvgd_1_1,20.0,11.015,48.005,1,,False\n"
        "Bus_mvgd_1_gen_1,20.0,11.016,48.006,1,,False\n"
        "BusBar_mvgd_1_lvgd_1_LV,0.4,11.01,48.0,1,1,False\n"
    )
    (topo / "lines.csv").write_text(
        "name,bus0,bus1,length,x,r,b,s_nom,num_parallel,type_info,kind\n"
        "Branch_1,Busbar_mvgd_1_MV,BusBar_mvgd_1_lvgd_1_MV,1.0,0.1,0.1,0.0,14.0,1.0,NA2XS2Y 3x1x240,cable\n"
        "Branch_2,BusBar_mvgd_1_lvgd_1_MV,BranchTee_mvgd_1_1,0.5,0.1,0.1,0.0,14.0,1.0,122-AL1/20-ST1A,line\n"
        "Branch_3,BranchTee_mvgd_1_1,virtual_BusBar_mvgd_1_lvgd_2_MV,0.7,0.1,0.1,0.0,14.0,1.0,NA2XS2Y 3x1x240,cable\n"
        "Branch_4,BusBar_mvgd_1_lvgd_2_MV,Busbar_mvgd_1_MV,1.2,0.1,0.1,0.0,14.0,1.0,NA2XS2Y 3x1x240,cable\n"
        "Branch_5,BranchTee_mvgd_1_1,Bus_mvgd_1_gen_1,0.2,0.1,0.1,0.0,14.0,2.0,NA2XS2Y 3x1x240,cable\n"
    )
    (topo / "switches.csv").write_text(
        "name,bus_closed,bus_open,branch,type_info\n"
        "circuit_breaker_1,BusBar_mvgd_1_lvgd_2_MV,virtual_BusBar_mvgd_1_lvgd_2_MV,Branch_3,Switch Disconnector\n"
    )
    (topo / "transformers.csv").write_text(
        ",bus0,bus1,x,r,s_nom,type_info\n"
        "Transformer_lv_grid_1_1,BusBar_mvgd_1_lvgd_1_MV,BusBar_mvgd_1_lvgd_1_LV,0.04,0.01,0.4,0.4 MVA 20/0.4 kV\n"
        "Transformer_lv_grid_2_1,BusBar_mvgd_1_lvgd_2_MV,BusBar_mvgd_1_lvgd_2_LV,0.04,0.01,0.25,0.25 MVA 20/0.4 kV\n"
    )
    (topo / "transformers_hvmv.csv").write_text(
        "name,bus0,bus1,s_nom,r,x,type,type_info\n"
        "Transformer_mv_grid_1_1,Busbar_mvgd_1_HV,Busbar_mvgd_1_MV,40.0,,,40 MVA 110/20 kV,40 MVA 110/20 kV\n"
        "Transformer_mv_grid_1_2,Busbar_mvgd_1_HV,Busbar_mvgd_1_MV,40.0,,,40 MVA 110/20 kV,40 MVA 110/20 kV\n"
    )
    (topo / "generators.csv").write_text(
        "name,bus,p_nom,type,control,weather_cell_id,subtype,source_id,voltage_level\n"
        "Generator_mvgd_1_wind_1,Bus_mvgd_1_gen_1,1.5,wind,PQ,1.0,wind_onshore,X1,mv\n"
    )
    (topo / "loads.csv").write_text(
        "name,bus,p_set,building_id,type,annual_consumption,sector,number_households,voltage_level\n"
        "Load_lv_1,BusBar_mvgd_1_lvgd_1_LV,0.01,1,conventional_load,,residential,1.0,lv\n"
    )


def test_read_ding0_grid_fixture(tmp_path):
    _write_ding0_fixture(tmp_path / "1")
    nodes, lines = m.read_ding0_grid(tmp_path / "1")
    kinds = dict(zip(nodes["node_id"], nodes["kind"]))
    assert kinds == {"Busbar_mvgd_1_MV": "busbar", "BusBar_mvgd_1_lvgd_1_MV": "station",
                     "BusBar_mvgd_1_lvgd_2_MV": "station", "BranchTee_mvgd_1_1": "joint",
                     "Bus_mvgd_1_gen_1": "generator"}
    x, y = Transformer.from_crs(4326, 25832, always_xy=True).transform(11.0, 48.0)
    busbar = nodes.set_index("node_id").loc["Busbar_mvgd_1_MV"]
    assert busbar["x"] == pytest.approx(x) and busbar["y"] == pytest.approx(y)
    assert busbar["n_transformers"] == 2 and busbar["transformer_mva"] == 80.0
    assert nodes.set_index("node_id").loc["BusBar_mvgd_1_lvgd_2_MV", "sn_kva"] == 250.0
    lines = lines.set_index("line_id")
    nop = lines.loc["Branch_3"]
    assert nop["normally_open"] and nop["to_node"] == "BusBar_mvgd_1_lvgd_2_MV"
    assert nop["open_at_node"] == "BusBar_mvgd_1_lvgd_2_MV"
    assert lines.loc["Branch_2", "kind"] == "overhead" and lines.loc["Branch_5", "parallel"] == 2
    assert int(lines["normally_open"].sum()) == 1
    rings = m.ring_table(nodes, lines.reset_index())
    ring = rings[rings["kind"] == "ring"].iloc[0]
    assert ring["ring_members"] == 2 and ring["n_stub_nodes"] == 1
    assert ring["ring_km"] == pytest.approx(1.0 + 0.5 + 0.7 + 1.2)
    tm = m.topology_metrics(nodes, lines.reset_index())
    assert tm["n_hv_mv_transformers"] == 2
    assert tm["route_km"] == pytest.approx(3.6)
    assert tm["circuit_km"] == pytest.approx(3.8)  # Branch_5 is 0.2 km x 2
    assert tm["cable_share"] == pytest.approx(3.3 / 3.8)
    # the topology folder itself works too
    nodes2, _ = m.read_ding0_grid(tmp_path / "1" / "topology")
    pd.testing.assert_frame_equal(nodes, nodes2)
    with pytest.raises(FileNotFoundError):
        m.read_ding0_grid(tmp_path)


def test_region_metrics_clips_to_the_region():
    lines = gpd.GeoDataFrame({
        "length_km": [2.0, 1.3],
        "kind": ["cable", "overhead"],
        "geometry": [LineString([(0, 0), (2000, 0)]), LineString([(0, 1000), (1000, 1000)])],
    }, crs=25832)
    region = box(0, -500, 1000, 1500)
    nodes = _nodes([(1, "station", 500, 0), (2, "station", 1500, 0), (3, "customer", 100, 1000),
                    (4, "joint", 200, 200)])
    out = m.region_metrics(lines, region, nodes)
    assert out["region_area_km2"] == pytest.approx(2.0)
    assert out["circuit_km_in_region"] == pytest.approx(1.0 + 1.3)          # half of line 1, all of line 2
    assert out["km_per_km2"] == pytest.approx(2.3 / 2.0)
    assert out["cable_share"] == pytest.approx(1.0 / 2.3)
    assert out["n_supply_points_in_region"] == 2 and out["n_stations_in_region"] == 1
    assert out["km_per_supply_point"] == pytest.approx(2.3 / 2)
    series = gpd.GeoSeries([box(0, -500, 1000, 500), box(0, 500, 1000, 1500)], crs=25832)
    assert m.region_metrics(lines, series)["km_per_km2"] == pytest.approx(out["km_per_km2"])
    with pytest.raises(ValueError, match="projected"):
        m.region_metrics(lines.to_crs(4326), region)


def test_parallel_inventory_keeps_route_and_topology_distances():
    nodes, lines = _ring_toy()
    lines["parallel"] = 1
    lines.loc[lines["line_id"] == 10, "parallel"] = 2  # 1 km x 2 at an outlet
    lines.loc[lines["line_id"] == 14, "parallel"] = 3  # installed open section
    tm = m.topology_metrics(nodes, lines)
    assert tm["route_km"] == pytest.approx(8.0)
    assert tm["circuit_km"] == pytest.approx(11.0)
    assert tm["cable_km"] == pytest.approx(9.0)
    assert tm["overhead_km"] == pytest.approx(2.0)
    assert tm["cable_share"] == pytest.approx(9 / 11)
    feeders = m.feeder_table(nodes, lines)
    assert feeders["feeder_km"].tolist() == pytest.approx([3.0, 4.0])
    assert feeders["cable_km"].tolist() == pytest.approx([5.0, 4.0])
    assert feeders["overhead_km"].tolist() == pytest.approx([0.0, 2.0])
    ring = m.ring_table(nodes, lines).iloc[0]
    assert ring["ring_km"] == pytest.approx(7.0)
    assert ring["cable_km"] == pytest.approx(9.0)
    assert ring["overhead_km"] == pytest.approx(2.0)
    for invalid in (0, -1, 1.5, "bad"):
        with pytest.raises(ValueError, match="parallel must be a positive integer"):
            m.normalise_tables(nodes, lines.assign(parallel=invalid))


def test_parallel_external_tie_is_separate_from_district_inventory():
    nodes = _nodes([(0, "busbar", 0, 0), (1, "station", 1000, 0)])
    lines = _lines([(1, 0, 1, 1.0, "cable", False, None),
                    (2, 1, 99, 0.5, "overhead", True, 99)])
    lines["parallel"] = [2, 3]
    lines["role"] = ["ring", "tie"]
    tm = m.topology_metrics(nodes, lines)
    assert tm["route_km"] == pytest.approx(1.0)
    assert tm["circuit_km"] == pytest.approx(2.0)
    assert tm["tie_km"] == pytest.approx(1.5)


def test_region_parallel_inventory_uses_clipped_length():
    lines = gpd.GeoDataFrame({
        "length_km": [2.0, 1.3], "cable_km": [2.0, 0.0], "overhead_km": [0.0, 1.3],
        "parallel": [2, 3], "geometry": [LineString([(0, 0), (2000, 0)]),
                                        LineString([(0, 1000), (1000, 1000)])],
    }, crs=25832)
    out = m.region_metrics(lines, box(0, -500, 1000, 1500))
    assert out["route_km_in_region"] == pytest.approx(2.3)
    assert out["circuit_km_in_region"] == pytest.approx(5.9)
    assert out["cable_km_in_region"] == pytest.approx(2.0)
    assert out["overhead_km_in_region"] == pytest.approx(3.9)
    assert out["cable_share"] == pytest.approx(2 / 5.9)


def test_traffic_lights_and_literature_comparison():
    assert m.traffic_light(15, 5, 30) == ("green", 0.0)
    status, dev = m.traffic_light(4.0, 5, 30)
    assert status == "amber" and dev == pytest.approx(0.2)
    assert m.traffic_light(2.0, 5, 30)[0] == "red"
    assert m.traffic_light(25, None, 20)[0] == "amber"
    assert m.traffic_light(15, None, None, 15) == ("green", 0.0)
    assert m.traffic_light(float("nan"), 5, 30)[0] == "n/a"
    frame = pd.DataFrame({"ring_members": [15.0, 38.0], "area_class": ["urban", "urban"]},
                         index=pd.Index(["g1", "g2"], name="mv_grid_id"))
    table = m.compare_to_literature(frame)
    assert set(table["area_class"].dropna()) == {"urban"}
    first = table[(table["mv_grid_id"] == "g1") & (table["low"] == 5) & (table["high"] == 30)]
    assert first["status"].item() == "green"
    second = table[(table["mv_grid_id"] == "g2") & (table["low"] == 5) & (table["high"] == 30)]
    assert second["status"].item() == "amber"
    rural = m.compare_to_literature({"ring_members": 25}, area_class="rural")
    assert set(rural["area_class"]) == {"rural"} and set(rural["status"]) == {"amber", "red"}


def test_literature_ranges_are_consistent():
    metrics_from_tables = set(m.topology_metrics(*_ring_toy())) | {"ring_members", "ring_km", "feeder_km",
                                                                   "feeder_supply_points", "km_per_km2"}
    external = {"substation_peak_mva", "hv_mv_unit_mva", "peak_to_station_rating", "max_line_loading_percent",
                "open_ring_grid_share", "open_meshed_share", "length_ratio_to_real"}
    for rng in m.LITERATURE_RANGES:
        assert rng.metric in metrics_from_tables | external, rng.metric
        assert rng.area_class in (None, "rural", "urban")
        assert any(v is not None for v in (rng.low, rng.high, rng.typical))
        if rng.low is not None and rng.high is not None:
            assert rng.low <= rng.high
        assert rng.label and rng.citation
    assert set(m.feeder_table(*_ring_toy()).columns) >= {"feeder_km", "feeder_supply_points"}
    assert set(m.ring_table(*_ring_toy()).columns) >= {"ring_members", "ring_km"}
    assert len(m.literature_ranges("cable_share", "urban")) == 2


def test_earth_fault_current():
    assert m.earth_fault_current_a(1.0, 0.0, 20) == pytest.approx(3.31, abs=0.01)
    assert m.earth_fault_current_a(0.0, 10.0, 20) == pytest.approx(0.544, abs=0.001)
    assert m.earth_fault_current_a(1.0, 0.0, 10) == pytest.approx(math.sqrt(3) * 2 * math.pi * 50 * 10e3 * 456e-9)
    with pytest.raises(ValueError):
        m.earth_fault_current_a(1.0, 0.0, 30)
    assert m.earth_fault_current_a(1.0, 0.0, 30, cable_c_nf_per_km=200.0) > 0


def test_wasserstein_table_and_ring_membership_ari():
    table = m.wasserstein_table({"a": [1, 2, 3], "b": [2, 3, 4, np.nan], "c": []}, reference="a").set_index("variant")
    assert table.loc["a", "wasserstein"] == 0.0
    assert table.loc["b", "wasserstein"] == pytest.approx(1.0) and table.loc["b", "n"] == 3
    assert table.loc["b", "wasserstein_rel"] == pytest.approx(0.5)
    assert np.isnan(table.loc["c", "wasserstein"])
    with pytest.raises(KeyError):
        m.wasserstein_table({"a": [1]}, reference="x")
    a = {1: "r1", 2: "r1", 3: "r2", 4: "r2"}
    assert m.ring_membership_ari(a, {1: 7, 2: 7, 3: 9, 4: 9, 5: 9}) == pytest.approx(1.0)
    assert m.ring_membership_ari(a, {1: 0, 2: 1, 3: 0, 4: 1}) < 0.5
    assert np.isnan(m.ring_membership_ari(a, {1: 0}))
