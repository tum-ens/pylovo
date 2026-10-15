"""Tests of the coupled MV+LV checks (pylovo.mv_grid.coupling) on synthetic LV nets and districts; no database."""
from __future__ import annotations

import copy
import importlib
import json
import math
import sys
import time

import numpy as np
import pandapower as pp
import pandas as pd
import pytest
from shapely.geometry import LineString

import pylovo.mv_grid as mv_grid_package
from pylovo.mv_grid import coincidence, electrical
from pylovo.mv_grid.model import NODE_COLUMNS, DistrictInput, EdgeSet, LinePlan, RingPlan, TopologyPlan
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError

X0, Y0 = 700_000.0, 5_380_000.0
BUSBAR = 1
SIM = {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6}
TAN = math.tan(math.acos(0.95))
COUPLING = "pylovo.mv_grid.coupling"
coupling = None     # the module under test, loaded by _coupling_module for the tests of this file only


@pytest.fixture(scope="module", autouse=True)
def _coupling_module():
    """Load the coupling module for this file's tests and unload it afterwards.

    tests/test_mv_cli.py checks the message of a build without the module by setting its sys.modules entry to None;
    a module imported at collection time would leave the package attribute behind and let that CLI test run the
    real coupling driver against the configured database.
    """
    global coupling
    loaded = COUPLING in sys.modules
    coupling = importlib.import_module(COUPLING)
    yield coupling
    if not loaded:
        sys.modules.pop(COUPLING, None)
        if "coupling" in vars(mv_grid_package):
            delattr(mv_grid_package, "coupling")
    coupling = None


def _lv() -> LvVersionParameters:
    return LvVersionParameters(version_id="t", sim_factor=dict(SIM), power_factor=0.95, mv_direct_threshold_kw=300.0,
                               residential_only=False, lv_reference_voltage_pu=0.96, use_dso=False, use_open=False,
                               use_manual=False, electrical_backend="pandapower")


# ---------------------------------------------------------------------------------------------- LV nets
def make_lv_net(gid: int, households: float = 40.0, commercial_kw: float = 100.0, nodes: int = 4,
                length_km: float = 0.06, std_type: str = "0.4 MVA 20/0.4 kV", reference: float | None = 0.96
                ) -> pp.pandapowerNet:
    """A pylovo-like LV net: bus 0 'LVbus 1', bus 1 'MVbus 1' with the ext_grid, one station transformer, a feeder
    of ``nodes`` consumer buses with residential loads and one commercial load at its end; the loads carry pylovo's
    columns (category, load_units, max_p_mw, station-coincident p_mw) and the ext_grid holds the requirement that
    puts 'LVbus 1' at ``reference``."""
    net = pp.create_empty_network(name=f"PLZ1_kcid1_bcid{gid}")
    lv = pp.create_bus(net, 0.4, name="LVbus 1", index=0, geodata=(11.0, 49.0))
    mv = pp.create_bus(net, 20.0, name="MVbus 1", index=1, geodata=(11.0, 49.00015))
    pp.create_ext_grid(net, mv, vm_pu=1.0, name="External grid")
    pp.create_transformer(net, mv, lv, std_type=std_type, name="single 400 kva transformer")
    loads = []
    previous = lv
    for i in range(nodes):
        bus = pp.create_bus(net, 0.4, name=f"Consumer Nodebus {100 * gid + i}", geodata=(11.0 + 0.001 * (i + 1), 49.0))
        pp.create_line(net, previous, bus, length_km=length_km, std_type="NAYY 4x150 SE", name=f"Line to {i}")
        previous = bus
        units = households / nodes
        loads.append((bus, "Residential", 30.0 * units, units))
    if commercial_kw > 0:
        loads.append((previous, "Commercial", commercial_kw, 1.0))
    frame = pd.DataFrame(loads, columns=["bus", "category", "installed_kw", "units"])
    sums = frame.groupby("category")[["installed_kw", "units"]].sum()
    g = np.array([SIM[c] for c in sums.index])
    share = (g + (1 - g) * sums["units"].to_numpy() ** -0.75)          # coincident per installed kW
    frame["p_kw"] = frame["installed_kw"] * frame["category"].map(dict(zip(sums.index, share)))
    index = pp.create_loads(net, buses=frame["bus"].tolist(), p_mw=(frame["p_kw"] / 1000).to_numpy(),
                            q_mvar=(frame["p_kw"] / 1000 * TAN).to_numpy(),
                            name=[f"Load {b} {c}" for b, c in zip(frame["bus"], frame["category"])])
    net.load.loc[index, "category"] = frame["category"].to_numpy()
    net.load.loc[index, "load_units"] = frame["units"].to_numpy()
    net.load.loc[index, "max_p_mw"] = (frame["installed_kw"] / 1000).to_numpy()
    if reference is not None:                                          # pylovo's station voltage rule
        for _ in range(8):
            pp.runpp(net)
            net.ext_grid.at[0, "vm_pu"] += reference - float(net.res_bus.at[lv, "vm_pu"])
        pp.runpp(net)
    return net


def lv_stats(net: pp.pandapowerNet) -> dict[str, float]:
    """NODE_COLUMNS statistics of a station from the loads of its LV net."""
    out = {}
    for category, short in zip(("Residential", "Commercial", "Public", "Unknown"), ("res", "com", "pub", "unk")):
        part = net.load[net.load["category"] == category]
        out[f"inst_{short}"] = float(part["max_p_mw"].sum() * 1000)
        out[f"units_{short}"] = float(part["load_units"].sum())
    return out


# ---------------------------------------------------------------------------------------------- districts
def _row(kind: str, x: float, y: float, **values) -> dict:
    row = {column: 0.0 for column in NODE_COLUMNS}
    row.update(kind=kind, connection_hint="busbar" if kind == "busbar" else "ring", x=x, y=y, attach_edge_id=-1,
               attach_fraction=0.0, attach_offset_m=0.0, bridged=False, settlement_id=-1, urban=False, plz=0,
               plz_settlement_type=0, gen_level="", sn_kva=math.nan, grid_result_id=math.nan,
               lv_design_source_vm_pu=math.nan, lv_max_drop_pu=math.nan)
    row.update(values)
    return row


def make_district(lv_nets: dict[int, pp.pandapowerNet], rings: list[list[int]], *, params: MvParameters | None = None,
                  spacing: float = 700.0, generation: dict[int, dict[str, float]] | None = None,
                  ) -> tuple[DistrictInput, TopologyPlan]:
    """Hairpin open rings from the busbar over the stations of ``lv_nets`` (lists of grid_result_ids per ring);
    station node key = grid_result_id + 100, statistics and LV requirement from the LV nets."""
    params = params or MvParameters()
    generation = generation or {}
    rows = {BUSBAR: _row("busbar", X0, Y0)}
    lines: list[LinePlan] = []
    plans: list[RingPlan] = []
    role, feeder, ring_of, position = {BUSBAR: "busbar"}, {}, {}, {}
    for r, gids in enumerate(rings, start=1):
        theta = 2 * math.pi * r / (len(rings) + 1)
        d, n = np.array([math.cos(theta), math.sin(theta)]), np.array([-math.sin(theta), math.cos(theta)])
        k, half = len(gids), (len(gids) + 1) // 2
        members = []
        for i, gid in enumerate(gids):
            step = i + 1 if i < half else k - i
            p = np.array([X0, Y0]) + d * spacing * step + n * (60.0 if i < half else -60.0)
            net = lv_nets[gid]
            gen = generation.get(gid, {})
            key = gid + 100
            vm = net.res_bus["vm_pu"]
            rows[key] = _row("station", p[0], p[1], **lv_stats(net), sn_kva=400.0, grid_result_id=float(gid),
                             lv_design_source_vm_pu=float(net.ext_grid.at[0, "vm_pu"]),
                             lv_max_drop_pu=float(vm.iloc[0] - vm.drop(index=1).min()),
                             gen_solar_kw=gen.get("solar", 0.0), gen_wind_kw=gen.get("wind", 0.0),
                             gen_other_kw=gen.get("other", 0.0), gen_level="lv" if gen else "")
            members.append(key)
        m = max(half, 1)
        sequence = [BUSBAR, *members, BUSBAR]
        fa, fb = 2 * r - 1, 2 * r
        for j in range(k + 1):
            a, b = sequence[j], sequence[j + 1]
            geometry = LineString([(rows[a]["x"], rows[a]["y"]), (rows[b]["x"], rows[b]["y"])])
            is_open = j == m and k >= 2
            lines.append(LinePlan(key=len(lines), from_key=a, to_key=b, role="ring", kind="cable",
                                  feeder_id=fa if j < m else fb, ring_id=r, length_m=max(geometry.length, 1.0),
                                  cable_m=max(geometry.length, 1.0), overhead_m=0.0, straight_m=geometry.length,
                                  bridge_m=0.0, geometry=geometry, std_type=params.cable.name,
                                  normally_open=is_open, open_at_key=a if is_open else None))
        for i, key in enumerate(members):
            role[key], feeder[key], ring_of[key], position[key] = "ring", fa if i < m else fb, r, i + 1
        plans.append(RingPlan(r, "ring", list(members), members[m - 1], None, fa, fb))
    nodes = pd.DataFrame.from_dict(rows, orient="index")[list(NODE_COLUMNS)]
    nodes.index.name = "mv_node_id"
    edges = EdgeSet(*(np.array([], dtype=np.int64) for _ in range(4)), np.array([], dtype=bool),
                    np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.zeros((0, 2)))
    inp = DistrictInput(mv_grid_id=3, substation_id=7, vn_kv=params.vn_kv, nodes=nodes, edges=edges, params=params,
                        lv=_lv(), known_units_mva=(), edge_geometry=lambda ids: {})
    plan = TopologyPlan(rings=plans, lines=lines, joints={}, node_role=role, node_feeder=feeder, node_ring=ring_of,
                        node_position=position, stub_parent={}, diagnostics={})
    return inp, plan


def planned(inp: DistrictInput, plan: TopologyPlan):
    """Design net, cases, budgets and the stored net (2 x 25 MVA) of a synthetic district."""
    design = electrical.build_design_net(inp, plan)
    cases = electrical.run_cases(design, inp, plan)
    budgets = electrical.station_budgets(inp, plan, cases)
    stored, _ = electrical.build_stored_net(design, 2, 25.0, inp)
    return design, cases, budgets, stored


def station_rows(inp: DistrictInput, cases, budgets) -> pd.DataFrame:
    """The mv_node rows of the stations as the coupling reads them (read_mv_layers 'nodes')."""
    feed = pd.concat([cases[c].vm_pu for c in electrical.FEED_IN_CASES], axis=1).max(axis=1)
    rows = []
    for key, node in inp.nodes.iterrows():
        if node["kind"] != "station":
            continue
        budget = budgets.get(int(key))
        rows.append({"mv_node_id": int(key), "kind": "station", "mv_grid_id": inp.mv_grid_id,
                     "grid_result_id": node["grid_result_id"], "vm_load_pu": cases["load"].vm_pu[key],
                     "vm_feed_in_pu": feed[key], "mv_lv_tap_pos": budget.tap_pos if budget else math.nan,
                     "gen_solar_kw": node["gen_solar_kw"], "gen_wind_kw": node["gen_wind_kw"],
                     "gen_other_kw": node["gen_other_kw"],
                     "lv_busbar_vm_pu": budget.lv_busbar_vm_pu if budget else math.nan,
                     "lv_min_vm_pu": budget.lv_min_vm_pu if budget else math.nan,
                     "lv_busbar_vm_feed_in_pu": budget.lv_busbar_vm_feed_in_pu if budget else math.nan})
    return pd.DataFrame(rows)


def frames_for(stored: pp.pandapowerNet, lv_nets: dict[int, pp.pandapowerNet]) -> dict:
    buses = coupling._node_buses(stored)
    return {buses[gid + 100]: (gid, coupling.lv_frames(net, gid)) for gid, net in lv_nets.items()}


@pytest.fixture(scope="module")
def small():
    """Two rings of three stations, one with LV-side PV."""
    lv_nets = {gid: make_lv_net(gid, households=30 + 5 * gid, commercial_kw=60.0 + 10 * gid) for gid in range(1, 7)}
    inp, plan = make_district(lv_nets, [[1, 2, 3], [4, 5, 6]], generation={2: {"solar": 250.0}})
    design, cases, budgets, stored = planned(inp, plan)
    return lv_nets, inp, plan, design, cases, budgets, stored


# ---------------------------------------------------------------------------------------------- frames, design point
def test_lv_frames_copy_the_four_tables_with_the_grid_id():
    net = make_lv_net(5)
    before = copy.deepcopy(net)
    frames = coupling.lv_frames(net, 5)
    assert set(frames) == {"bus", "line", "trafo", "load"}
    assert all((frame["grid_result_id"] == 5.0).all() for frame in frames.values())
    frames["bus"].loc[0, "name"] = "changed"
    assert net.bus.at[0, "name"] == "LVbus 1" and "grid_result_id" not in net.bus.columns
    assert net.load.equals(before.load)
    assert coupling.station_buses(net) == (1, 0)


def test_station_buses_needs_one_transformer():
    net = make_lv_net(1)
    net.trafo = net.trafo.iloc[0:0]
    with pytest.raises(ValueError, match="no station transformer"):
        coupling.station_buses(net)


def test_design_point_check_matches_the_analytic_budget(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    params = inp.params
    feed = pd.concat([cases[c].vm_pu for c in electrical.FEED_IN_CASES], axis=1).max(axis=1)
    band = params.busbar_band_pu
    for gid, net in lv_nets.items():
        key = gid + 100
        budget = budgets[key]
        state = (float(net.ext_grid.at[0, "vm_pu"]), float(net.trafo.at[0, "tap_pos"]), net.load["p_mw"].copy())
        gen = {"solar": inp.nodes.at[key, "gen_solar_kw"]}
        check = coupling.design_point_check(net, cases["load"].vm_pu[key] - band, feed[key] + band, budget.tap_pos,
                                            gen, params)
        assert check["design_converged"] == 1.0 and check["feed_in_converged"] == 1.0
        # the analytic budget (superposition) and the LV power flow agree at the design point
        assert check["coupled_lv_busbar_vm_pu"] == pytest.approx(budget.lv_busbar_vm_pu, abs=0.002)
        assert check["coupled_lv_busbar_vm_feed_in_pu"] == pytest.approx(budget.lv_busbar_vm_feed_in_pu, abs=0.003)
        assert check["coupled_lv_min_vm_pu"] <= check["coupled_lv_busbar_vm_pu"]
        assert check["coupled_lv_max_vm_pu"] >= check["coupled_lv_busbar_vm_feed_in_pu"] - 1e-12
        # the stored LV net is not changed
        assert (float(net.ext_grid.at[0, "vm_pu"]), float(net.trafo.at[0, "tap_pos"])) == state[:2]
        assert net.load["p_mw"].equals(state[2]) and not len(net.sgen)
    # PV raises the LV busbar in feed-in above the station without PV
    assert budgets[102].lv_busbar_vm_feed_in_pu > budgets[101].lv_busbar_vm_feed_in_pu


def test_design_point_check_tap_and_generation_rules():
    net = make_lv_net(3)
    params = MvParameters()
    r = float(net.ext_grid.at[0, "vm_pu"])
    neutral = coupling.design_point_check(net, r, math.nan, 0, 0.0, params)
    assert neutral["coupled_lv_busbar_vm_pu"] == pytest.approx(0.96, abs=1e-5)       # the stored requirement
    assert math.isnan(neutral["coupled_lv_busbar_vm_feed_in_pu"]) and math.isnan(neutral["feed_in_converged"])
    raised = coupling.design_point_check(net, r, math.nan, -1, 0.0, params)
    assert raised["coupled_lv_busbar_vm_pu"] - 0.96 == pytest.approx(r / (1 - 0.025) - r, abs=0.002)
    # a number is solar; a mapping gives kW per technology group (wind uses the wind factor of each case)
    solar = coupling.design_point_check(net, math.nan, 1.03, 0, 200.0, params)
    as_map = coupling.design_point_check(net, math.nan, 1.03, 0, {"solar": 200.0}, params)
    wind = coupling.design_point_check(net, math.nan, 1.03, 0, {"wind": 200.0}, params)
    assert solar["coupled_lv_busbar_vm_feed_in_pu"] == pytest.approx(as_map["coupled_lv_busbar_vm_feed_in_pu"])
    assert wind["coupled_lv_busbar_vm_feed_in_pu"] > solar["coupled_lv_busbar_vm_feed_in_pu"]   # 1.0 > 0.95
    assert math.isnan(solar["coupled_lv_busbar_vm_pu"])
    with pytest.raises(ValueError, match="unknown technology groups"):
        coupling.design_point_check(net, 1.0, 1.0, 0, {"hydro": 1.0}, params)


def test_design_point_check_with_an_under_excited_lv_generator():
    net = make_lv_net(4)
    unity = coupling.design_point_check(net, math.nan, 1.03, 0, 300.0, MvParameters())
    pv95 = coupling.design_point_check(net, math.nan, 1.03, 0, 300.0, MvParameters(lv_pv_cos_phi=0.95))
    assert pv95["coupled_lv_busbar_vm_feed_in_pu"] < unity["coupled_lv_busbar_vm_feed_in_pu"]


# ---------------------------------------------------------------------------------------------- merge
def test_mv_part_removes_the_hv_side(small):
    *_, stored = small
    part = coupling.mv_part(stored, vm_pu=1.005)
    assert "hv_busbar" not in set(part.bus["kind"]) and not len(part.trafo)
    assert len(part.ext_grid) == 1 and part.bus.at[int(part.ext_grid.at[part.ext_grid.index[0], "bus"]),
                                                   "kind"] == "busbar"
    assert part.ext_grid["vm_pu"].iloc[0] == 1.005 and not len(part.res_bus)
    assert len(stored.trafo) == 2 and "hv_busbar" in set(stored.bus["kind"])          # unchanged


def test_couple_fuses_the_lv_nets_at_their_mv_nodes(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    frames = frames_for(stored, lv_nets)
    net = coupling.couple_lv_nets(stored, frames, inp.vn_kv)
    buses = coupling._node_buses(stored)
    assert len(net.ext_grid) == 1 and net.bus.at[int(net.ext_grid["bus"].iloc[0]), "kind"] == "busbar"
    assert "MVbus 1" not in set(net.bus["name"]) and (net.bus["name"] == "LVbus 1").sum() == len(lv_nets)
    lv_trafo = net.trafo[net.trafo["grid_result_id"].notna()]
    assert sorted(lv_trafo["hv_bus"].astype(int)) == sorted(buses[g + 100] for g in lv_nets)
    assert (net.bus.loc[lv_trafo["lv_bus"].astype(int), "name"] == "LVbus 1").all()
    # station equivalents removed, LV loads present, MV indices kept
    assert not net.load["name"].astype(str).str.startswith("Station").any()
    assert len(net.load) == sum(len(n.load) for n in lv_nets.values())
    assert net.bus.loc[stored.bus.index.drop(stored.bus.index[stored.bus["kind"] == "hv_busbar"]), "name"].equals(
        stored.bus.loc[net.bus.index[net.bus["kind"] != "lv"], "name"])
    # the LV-side generation of station 2 moved to its LV busbar
    sgen = net.sgen[net.sgen["level"] == "lv"]
    assert len(sgen) == 1 and net.bus.at[int(sgen["bus"].iloc[0]), "name"] == "LVbus 1"
    assert net.bus.at[int(sgen["bus"].iloc[0]), "grid_result_id"] == 2.0
    # dtypes and indices
    for table in ("bus", "line", "trafo", "load"):
        assert net[table].index.is_unique and net[table].index.dtype == np.int64
    assert net.bus["in_service"].dtype == bool and net.line["in_service"].dtype == bool
    assert net.line["from_bus"].dtype == stored.line["from_bus"].dtype
    assert net.load["bus"].dtype == stored.load["bus"].dtype and net.trafo["hv_bus"].dtype == np.uint32
    assert not len(net.res_bus) and len(stored.trafo) == 2                              # no results; input kept


def test_couple_rejects_unknown_buses(small):
    lv_nets, inp, *_, stored = small
    with pytest.raises(ValueError, match="not in the MV net"):
        coupling.couple_lv_nets(stored, {9999: (1, coupling.lv_frames(lv_nets[1], 1))}, inp.vn_kv)
    frames = coupling.lv_frames(lv_nets[1], 1)
    frames["line"].loc[frames["line"].index[0], "to_bus"] = 77
    with pytest.raises(ValueError, match="unknown bus"):
        coupling.couple_lv_nets(stored, {coupling._node_buses(stored)[101]: (1, frames)}, inp.vn_kv)


def _lv_bus_voltages(net: pp.pandapowerNet, gid: int) -> np.ndarray:
    rows = net.bus.index[net.bus["grid_result_id"] == gid]
    return net.res_bus.loc[rows, "vm_pu"].to_numpy()


def test_forty_lv_nets_merge_fast_and_equal_separate_lv_flows():
    lv_nets = {gid: make_lv_net(gid, households=20 + gid % 7 * 5, commercial_kw=40.0 + gid % 5 * 15)
               for gid in range(1, 41)}
    inp, plan = make_district(lv_nets, [list(range(1, 21)), list(range(21, 41))], spacing=150.0)
    design = electrical.build_design_net(inp, plan)
    frames = frames_for(design, lv_nets)
    start = time.perf_counter()
    net = coupling.couple_lv_nets(design, frames, inp.vn_kv)
    assert time.perf_counter() - start < 1.0
    coupling.set_lv_taps(net, {gid: (-1 if gid % 3 == 0 else 0) for gid in lv_nets})
    pp.runpp(net)
    assert net.converged
    for gid, lv_net in lv_nets.items():
        trafo = net.trafo[net.trafo["grid_result_id"] == gid]
        mv_bus = int(trafo["hv_bus"].iloc[0])
        alone = copy.deepcopy(lv_net)
        alone.ext_grid.at[0, "vm_pu"] = float(net.res_bus.at[mv_bus, "vm_pu"])
        alone.trafo["tap_pos"] = float(trafo["tap_pos"].iloc[0])
        pp.runpp(alone)
        merged = _lv_bus_voltages(net, gid)
        separate = alone.res_bus.loc[alone.bus.index != 1, "vm_pu"].to_numpy()
        assert np.abs(merged - separate).max() < 1e-6


def test_fuse_equals_a_closed_bus_switch(small):
    lv_nets, inp, plan, design, *_ = small
    fused = coupling.couple_lv_nets(design, frames_for(design, lv_nets), inp.vn_kv)
    pp.runpp(fused)
    # the same nets with the LV 'MVbus 1' kept and joined to the MV node by a closed bus-bus switch
    switched = coupling.mv_part(design)
    buses = coupling._node_buses(design)
    station = switched.load["grid_result_id"].notna()
    switched.load = switched.load[~station]
    lv_rows = {}
    for gid, lv_net in lv_nets.items():
        lookup = {int(b): pp.create_bus(switched, lv_net.bus.at[b, "vn_kv"], name=lv_net.bus.at[b, "name"])
                  for b in lv_net.bus.index}
        lv_rows[gid] = [lookup[b] for b in lv_net.bus.index if b != 1]
        pp.create_switch(switched, buses[gid + 100], lookup[1], et="b", closed=True)
        for _, line in lv_net.line.iterrows():
            pp.create_line_from_parameters(switched, lookup[int(line.from_bus)], lookup[int(line.to_bus)],
                                           line.length_km, line.r_ohm_per_km, line.x_ohm_per_km, line.c_nf_per_km,
                                           line.max_i_ka)
        trafo = lv_net.trafo.iloc[0]
        pp.create_transformer(switched, lookup[int(trafo.hv_bus)], lookup[int(trafo.lv_bus)], std_type=trafo.std_type)
        for _, load in lv_net.load.iterrows():
            pp.create_load(switched, lookup[int(load.bus)], load.p_mw, load.q_mvar)
    switched.sgen.loc[switched.sgen["level"] == "lv", "bus"] = lv_rows[2][0]     # LV PV at LVbus 1 of grid 2
    pp.runpp(switched)
    for gid in lv_nets:
        assert np.abs(_lv_bus_voltages(fused, gid) - switched.res_bus.loc[lv_rows[gid], "vm_pu"].to_numpy()).max() \
            < 1e-9
    mv = design.bus.index
    assert np.abs(fused.res_bus.loc[mv, "vm_pu"] - switched.res_bus.loc[mv, "vm_pu"]).max() < 1e-9


def test_ten_kv_variant_retypes_the_lv_transformers():
    lv_nets = {gid: make_lv_net(gid) for gid in (1, 2, 3)}
    params = MvParameters(vn_kv=10.0)
    inp, plan = make_district(lv_nets, [[1, 2, 3]], params=params, spacing=400.0)
    design = electrical.build_design_net(inp, plan)
    net = coupling.couple_lv_nets(design, frames_for(design, lv_nets), 10.0)
    lv_trafo = net.trafo[net.trafo["grid_result_id"].notna()]
    assert (lv_trafo["vn_hv_kv"] == 10.0).all() and (lv_trafo["vk_percent"] == 6.0).all()
    pp.runpp(net)
    for gid, lv_net in lv_nets.items():
        mv_bus = int(lv_trafo.loc[lv_trafo["grid_result_id"] == gid, "hv_bus"].iloc[0])
        alone = copy.deepcopy(lv_net)
        alone.ext_grid.at[0, "vm_pu"] = float(net.res_bus.at[mv_bus, "vm_pu"])
        pp.runpp(alone)
        assert np.abs(_lv_bus_voltages(net, gid) - alone.res_bus.loc[alone.bus.index != 1, "vm_pu"].to_numpy()
                      ).max() < 1e-6
        assert _lv_bus_voltages(net, gid)[0] > 0.95                       # LVbus 1, not 0.43 p.u. (20 kV ratio)


# ---------------------------------------------------------------------------------------------- scaling
def test_coupled_scaling_reproduces_the_coincident_feeder_load():
    rng = np.random.default_rng(3)
    rows = []
    for gid in range(1, 10):
        for category in ("Residential", "Commercial", "Public"):
            for _ in range(int(rng.integers(1, 5))):
                installed = float(rng.uniform(5, 80))
                rows.append({"grid_result_id": float(gid), "category": category, "max_p_mw": installed / 1000,
                             "load_units": float(rng.integers(1, 6)) if category == "Residential" else 1.0,
                             "p_mw": installed * float(rng.uniform(0.1, 0.9)) / 1000})
    rows.append({"grid_result_id": 99.0, "category": "Residential", "max_p_mw": 0.01, "load_units": 1, "p_mw": 0.004})
    rows.append({"grid_result_id": 1.0, "category": "Mixed", "max_p_mw": 0.01, "load_units": 1, "p_mw": 0.004})
    loads = pd.DataFrame(rows)
    feeder_of_grid = {gid: gid % 3 for gid in range(1, 10)}
    scaling = coupling.coupled_scaling(loads, feeder_of_grid, SIM)
    assert scaling[-2] == 1.0 and scaling[-1] == 1.0                   # other grid, unknown category
    g = np.array([SIM[c] for c in ("Residential", "Commercial", "Public", "Unknown")])
    known = loads.iloc[:-2].copy()
    known["feeder"] = known["grid_result_id"].astype(int).map(feeder_of_grid)
    known["p_scaled"] = known["p_mw"] * scaling[:-2]
    for feeder, part in known.groupby("feeder"):
        installed = np.array([part.loc[part.category == c, "max_p_mw"].sum() * 1000
                              for c in ("Residential", "Commercial", "Public", "Unknown")])
        units = np.array([part.loc[part.category == c, "load_units"].sum()
                          for c in ("Residential", "Commercial", "Public", "Unknown")])
        expected = float(coincidence.coincident_kw(installed, units, g))
        assert part["p_scaled"].sum() * 1000 == pytest.approx(expected, rel=1e-9, abs=1e-9)
    assert (scaling[:-2] < 1.0).any()


def test_scaled_lv_loads_equal_the_design_feeder_loads(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    net = coupling.couple_lv_nets(stored, frames_for(stored, lv_nets), inp.vn_kv)
    coupling.scale_lv_loads(net, SIM)
    feeders = coupling.coupled_feeders(net)
    assert len(set(feeders.values())) == 4                             # two half-rings per ring
    lv_load = net.load[net.load["grid_result_id"].notna()]
    scaled = (lv_load["p_mw"] * lv_load["scaling"]).groupby(lv_load["grid_result_id"].map(feeders)).sum()
    station = design.load
    design_p = station["p_normal_mw"].groupby(station["grid_result_id"].map(feeders)).sum()
    assert np.abs(scaled.sort_index().to_numpy() - design_p.sort_index().to_numpy()).max() < 1e-9
    # (n-1) feeder sets: whole rings
    ring_feeders = coupling.coupled_feeders(net, include_open=True)
    assert len(set(ring_feeders.values())) == 2
    coupling.scale_lv_loads(net, SIM, include_open=True)
    lv_load = net.load[net.load["grid_result_id"].notna()]
    scaled = (lv_load["p_mw"] * lv_load["scaling"]).groupby(lv_load["grid_result_id"].map(ring_feeders)).sum()
    design_p = station["p_n1_mw"].groupby(station["grid_result_id"].map(ring_feeders)).sum()
    assert np.abs(scaled.sort_index().to_numpy() - design_p.sort_index().to_numpy()).max() < 1e-9


def test_mv_feeders_follow_the_open_points(small):
    *_, design, cases, budgets, stored = small
    normal = coupling.mv_feeders(stored)
    whole = coupling.mv_feeders(stored, include_open=True)
    assert normal.nunique() == 4 and whole.nunique() == 2
    assert set(normal.index) == set(stored.bus.index[stored.bus["kind"] == "station"])


# ---------------------------------------------------------------------------------------------- operating point
def test_run_coupled_cases_reports_load_and_feed_in(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    taps = {gid: budgets[gid + 100].tap_pos for gid in lv_nets}
    net = coupling.build_coupled_net(stored, frames_for(stored, lv_nets), taps, inp.lv, inp.vn_kv)
    result = coupling.run_coupled_cases(net, inp.params)
    assert list(result.columns) == list(coupling.OPERATING_COLUMNS) and list(result.index) == sorted(lv_nets)
    assert all(result.attrs["converged"].values()) and result.notna().all().all()
    assert (result["op_lv_min_vm_pu"] <= result["op_lv_busbar_vm_pu"]).all()
    assert (result["op_mv_vm_pu"] <= inp.params.busbar_vm_pu - inp.params.busbar_band_pu + 1e-9).all()
    assert result.at[2, "op_lv_busbar_vm_feed_in_pu"] > result.at[1, "op_lv_busbar_vm_feed_in_pu"]   # PV
    slack = result.attrs["slack_p_mw"]
    assert slack["load"] > 0 and slack["feed_in_pv"] < 0.2 * slack["load"]   # loads x 0.1, PV feeds in
    assert result.attrs["mv_vm_pu"].index.isin(stored.bus.index).all()
    with pytest.raises(ValueError, match="operating-point cases"):
        coupling.run_coupled_cases(net, inp.params, cases=("n1a",))


def test_couple_district_verifies_the_budget(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    stations = station_rows(inp, cases, budgets)
    result = coupling.couple_district(stored, stations, iter(lv_nets.items()), inp.params, inp.lv, vn_kv=inp.vn_kv,
                                      keep_net=True)
    assert result.status == "done" and result.metrics["not_coupled"] == []
    rows = result.rows.set_index("mv_node_id")
    assert list(result.rows.columns) == ["mv_node_id", *coupling.COUPLED_COLUMNS]
    assert rows.notna().all().all()
    analytic = stations.set_index("mv_node_id")
    diff = (rows["coupled_lv_busbar_vm_pu"] - analytic["lv_busbar_vm_pu"]).abs()
    assert (diff < coupling.AGREEMENT_PU).all()
    assert result.metrics["design_point"]["busbar"]["share_within"] == 1.0
    op = result.metrics["operating_point"]
    assert all(op["converged"].values())
    assert op["mv_vm_vs_mv_only"]["max_abs_diff_pu"] < 0.005                # LV losses only
    # less load at feeder coincidence than at the LV design point: the operating point is higher
    assert (rows["coupled_op_lv_busbar_vm_pu"] > rows["coupled_lv_busbar_vm_pu"] - 0.003).all()
    assert result.net is not None and (result.net.bus["kind"] == "lv").any()
    json.dumps(result.metrics, allow_nan=False)                         # JSON-native, no NaN


def test_couple_district_keeps_the_equivalent_of_a_station_without_lv_net(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    stations = station_rows(inp, cases, budgets)
    some = {gid: net for gid, net in lv_nets.items() if gid != 5}
    result = coupling.couple_district(stored, stations, iter(some.items()), inp.params, inp.lv, vn_kv=inp.vn_kv,
                                      keep_net=True)
    assert result.status == "partial" and result.metrics["not_coupled"] == [105]
    rows = result.rows.set_index("mv_node_id")
    assert rows.loc[105].isna().all() and rows.drop(index=105).notna().all().all()
    names = result.net.load["name"].astype(str)
    assert (names == "Station 5").sum() == 1 and not names.isin(["Station 1", "Station 6"]).any()
    assert result.net.load.loc[names == "Station 5", "scaling"].iloc[0] == 1.0


def test_couple_district_survives_a_broken_lv_net_and_unknown_grids(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    stations = station_rows(inp, cases, budgets)
    broken = copy.deepcopy(lv_nets[3])
    broken.trafo = broken.trafo.iloc[0:0]
    nets = [(gid, broken if gid == 3 else net) for gid, net in lv_nets.items()] + [(77, lv_nets[1])]
    result = coupling.couple_district(stored, stations, iter(nets), inp.params, inp.lv, vn_kv=inp.vn_kv,
                                      merged=False)
    assert result.status == "partial" and result.metrics["lv_error"] == [103]
    assert result.metrics["unknown_lv_grid"] == [77] and "operating_point" not in result.metrics
    assert result.rows["coupled_op_lv_busbar_vm_pu"].isna().all()


def test_couple_district_leaves_a_non_converging_lv_net_out_of_the_merged_flow(small):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    stations = station_rows(inp, cases, budgets)
    diverging = copy.deepcopy(lv_nets[4])
    diverging.load["p_mw"] = diverging.load["p_mw"] * 1000.0          # no power-flow solution on its own
    nets = [(gid, diverging if gid == 4 else net) for gid, net in lv_nets.items()]
    result = coupling.couple_district(stored, stations, iter(nets), inp.params, inp.lv, vn_kv=inp.vn_kv,
                                      keep_net=True)
    assert result.metrics["design_not_converged"] == [104] and result.metrics["not_merged"] == [104]
    assert result.status == "partial"
    op = result.metrics["operating_point"]
    assert all(op["converged"].values())                               # the other LV nets are merged and solved
    rows = result.rows.set_index("mv_node_id")
    assert np.isnan(rows.at[104, "coupled_op_lv_busbar_vm_pu"])
    assert rows.drop(index=104)["coupled_op_lv_busbar_vm_pu"].notna().all()
    names = result.net.load["name"].astype(str)
    assert (names == "Station 4").sum() == 1                           # the station keeps its MV-level load


def test_couple_district_needs_the_station_columns():
    with pytest.raises(ValueError, match="no column"):
        coupling.couple_district(None, pd.DataFrame({"mv_node_id": [1]}), [], MvParameters(), _lv(), vn_kv=20.0)


# ---------------------------------------------------------------------------------------------- drivers
class FakeClient:
    """In-memory stand-in for DatabaseClient with the methods the coupling drivers use."""

    def __init__(self, state: dict):
        self.state = state

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def ensure_grid_persistence_schema(self):
        self.state["schema_checked"] = True

    def try_lock_variant(self, version_id, variant_id):
        self.state.setdefault("locks", []).append((version_id, variant_id))
        return self.state.get("lock_free", True)

    def unlock_variant(self, version_id, variant_id):
        self.state.setdefault("unlocks", []).append((version_id, variant_id))

    def commit_changes(self):
        pass

    def rollback_changes(self):
        pass

    def close(self):
        self.state["closed"] = self.state.get("closed", 0) + 1

    def variant_row(self, version_id, variant_id):
        return self.state["variant"]

    def lv_snapshot(self, version_id):
        raise AssertionError("the variant row has the LV parameters")

    def read_mv_layers(self, version_id, variant_id, mv_grid_id=None):
        grids, nodes = self.state["grids"], self.state["nodes"]
        if mv_grid_id is not None:
            grids = grids[grids["mv_grid_id"] == mv_grid_id]
            nodes = nodes[nodes["mv_grid_id"] == mv_grid_id]
        return {"grids": grids.assign(geom=None), "nodes": nodes.assign(geom=None)}

    def read_mv_grid_net(self, mv_grid_id):
        return copy.deepcopy(self.state["nets"][mv_grid_id])

    def iter_lv_nets(self, version_id, grid_result_ids, chunk=50):
        self.state.setdefault("chunks", []).append(chunk)
        for gid in grid_result_ids:
            if gid in self.state["lv_nets"]:
                yield gid, copy.deepcopy(self.state["lv_nets"][gid])

    def save_coupled_results(self, mv_grid_id, rows, status, metrics):
        self.state.setdefault("saved", {})[mv_grid_id] = (rows.copy(), status, metrics)


@pytest.fixture()
def fake_db(small, monkeypatch, tmp_path):
    lv_nets, inp, plan, design, cases, budgets, stored = small
    stations = station_rows(inp, cases, budgets)
    nodes = pd.concat([stations, pd.DataFrame([{"mv_node_id": 1, "kind": "busbar", "mv_grid_id": 3}])],
                      ignore_index=True)
    state = {
        "variant": {"mv_variant_comment": "c", "mv_parameters": inp.params.to_snapshot(),
                    "lv_parameters": inp.lv.as_json()},
        "grids": pd.DataFrame({"mv_grid_id": [3, 4], "status": ["done", "failed"], "vn_kv": [20.0, 20.0]}),
        "nodes": nodes, "nets": {3: stored}, "lv_nets": lv_nets,
    }
    monkeypatch.setattr(coupling, "_database_client", lambda log_file: FakeClient(state))
    monkeypatch.setattr(coupling, "_worker_count", lambda jobs, n: 1)
    return state, str(tmp_path / "log" / "log.txt")


def test_run_coupling_couples_the_done_grids(fake_db, capsys):
    state, log_file = fake_db
    summary = coupling.run_coupling("t", "d1", jobs=2, log_file=log_file)
    assert summary.status == "done" and (summary.grids, summary.done, summary.failed) == (1, 1, 0)
    rows, status, metrics = state["saved"][3]
    assert status == "done" and len(rows) == 6 and rows[list(coupling.COUPLED_COLUMNS)].notna().all().all()
    assert metrics["design_point"]["busbar"]["share_within"] == 1.0 and "worker_runtime_s" in metrics
    assert state["locks"] == state["unlocks"] == [("t", "d1")] and state["schema_checked"]
    assert state["chunks"] == [coupling.LV_NET_CHUNK]
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("✓ MV coupling on ") and out[0].endswith("version t, variant d1")
    assert "✓ Coupling" in summary.describe()


def test_run_coupling_reports_requested_grids_that_are_not_done(fake_db):
    state, log_file = fake_db
    summary = coupling.run_coupling("t", "d1", [3, 4, 9], merged=False, log_file=log_file)
    assert (summary.grids, summary.done, summary.failed, summary.status) == (3, 1, 2, "partial")
    assert any("not done" in w for w in summary.warnings) and any("are not in variant" in w for w in summary.warnings)
    assert "operating_point" not in state["saved"][3][2]
    assert summary.describe().startswith("✗ Coupling of MV variant d1 of version t: partial")


def test_run_coupling_marks_a_failed_grid(fake_db, monkeypatch):
    state, log_file = fake_db
    state["nets"] = {}                                                 # read_mv_grid_net fails
    summary = coupling.run_coupling("t", "d1", log_file=log_file)
    assert summary.status == "failed" and summary.failed == 1
    rows, status, metrics = state["saved"][3]
    assert status == "failed" and len(rows) == 0 and "KeyError" in metrics["error"]


def test_run_coupling_stops_when_the_variant_is_locked(fake_db):
    state, log_file = fake_db
    state["lock_free"] = False
    with pytest.raises(MvStageError, match="another MV run of variant d1"):
        coupling.run_coupling("t", "d1", log_file=log_file)
    assert "unlocks" not in state and state["closed"] == 1


def test_run_coupling_needs_the_variant(fake_db):
    state, log_file = fake_db
    state["variant"] = None
    with pytest.raises(MvStageError, match="does not exist"):
        coupling.run_coupling("t", "d9", log_file=log_file)
    assert state["unlocks"] == [("t", "d9")]


def test_run_coupling_in_a_fork_pool(fake_db, monkeypatch, tmp_path):
    state, log_file = fake_db
    state["grids"] = pd.DataFrame({"mv_grid_id": [3, 5], "status": ["done", "done"], "vn_kv": [20.0, 20.0]})
    other = state["nodes"].copy()
    other["mv_grid_id"] = 5
    state["nodes"] = pd.concat([state["nodes"], other], ignore_index=True)
    state["nets"] = {3: state["nets"][3], 5: state["nets"][3]}
    out_dir = tmp_path / "saved"
    out_dir.mkdir()

    def save(self, mv_grid_id, rows, status, metrics):          # runs in the worker process
        (out_dir / f"{mv_grid_id}.json").write_text(json.dumps({"status": status, "n": len(rows)}))

    monkeypatch.setattr(FakeClient, "save_coupled_results", save)
    monkeypatch.setattr(coupling, "_worker_count", lambda jobs, n: 2)
    summary = coupling.run_coupling("t", "d1", jobs=2, log_file=log_file)
    assert summary.status == "done" and summary.done == 2
    assert {p.stem: json.loads(p.read_text())["status"] for p in out_dir.iterdir()} == {"3": "done", "5": "done"}


def test_export_coupled_net_writes_the_case(fake_db, tmp_path):
    state, _ = fake_db
    path = coupling.export_coupled_net("t", "d1", 3, tmp_path / "out" / "coupled.json", case="feed_in_pv")
    net = pp.from_json(str(path))
    assert net.converged and (net.bus["kind"] == "lv").sum() > 0 and len(net.ext_grid) == 1
    params = MvParameters()
    assert net.ext_grid["vm_pu"].iloc[0] == pytest.approx(params.busbar_vm_pu + params.busbar_band_pu)
    assert (net.trafo["grid_result_id"].notna()).sum() == len(state["lv_nets"])
    n1 = pp.from_json(str(coupling.export_coupled_net("t", "d1", 3, tmp_path / "n1.json", case="n1a")))
    assert (~n1.line["in_service"]).sum() == 2                          # the A exit of both rings
    with pytest.raises(ValueError, match="case must be one of"):
        coupling.export_coupled_net("t", "d1", 3, tmp_path / "x.json", case="peak")
    with pytest.raises(LookupError, match="does not belong"):
        coupling.export_coupled_net("t", "d1", 8, tmp_path / "x.json")


def test_operating_busbar_voltage_uses_the_band_edges():
    params = MvParameters()
    assert coupling.operating_busbar_vm("load", params) == pytest.approx(1.005)
    assert coupling.operating_busbar_vm("n1b", params) == pytest.approx(1.005)
    assert coupling.operating_busbar_vm("feed_in_wind", params) == pytest.approx(1.035)
