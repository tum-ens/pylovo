"""The pure district planner: composition with fake planning modules, the repair loop, the generic tables, and
(once every planning module is present) a synthetic district end to end."""
from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd
import pytest
import shapely

from pylovo.mv_grid import metrics, planner
from pylovo.mv_grid.model import (
    NODE_COLUMNS,
    CaseResult,
    DistrictInput,
    DistrictPlan,
    EdgeSet,
    LinePlan,
    RingPlan,
    SolverResult,
    TopologyPlan,
    Violation,
)
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

LV = LvVersionParameters(version_id="mv1", sim_factor={"Residential": 0.07, "Commercial": 0.5, "Public": 0.6,
                                                       "Unknown": 0.6},
                         power_factor=0.95, mv_direct_threshold_kw=300.0, residential_only=False,
                         lv_reference_voltage_pu=0.96, use_dso=False, use_open=False, use_manual=False,
                         electrical_backend="pandapower")


def node_table(rows: list[dict]) -> pd.DataFrame:
    base = {c: 0.0 for c in NODE_COLUMNS}
    base.update(kind="station", connection_hint="ring", attach_edge_id=1, attach_fraction=0.5, attach_offset_m=0.0,
                bridged=False, settlement_id=np.nan, urban=False, plz=80801, plz_settlement_type=2, gen_level="",
                sn_kva=400.0, grid_result_id=np.nan, lv_design_source_vm_pu=0.98, lv_max_drop_pu=0.03)
    frame = pd.DataFrame([{**base, **row} for row in rows]).set_index("mv_node_id")
    return frame[list(NODE_COLUMNS)]


def small_input(params: MvParameters | None = None) -> DistrictInput:
    nodes = node_table([
        {"mv_node_id": 10, "kind": "busbar", "connection_hint": "busbar", "x": 0.0, "y": 0.0, "sn_kva": np.nan},
        {"mv_node_id": 11, "x": 100.0, "y": 0.0, "inst_res": 100.0, "units_res": 20.0, "urban": True},
        {"mv_node_id": 12, "x": 100.0, "y": 100.0, "inst_com": 50.0, "units_com": 1.0},
        {"mv_node_id": 13, "x": 0.0, "y": 100.0, "kind": "customer", "p_fixed_kw": 200.0},
        {"mv_node_id": 14, "x": 900.0, "y": 900.0, "kind": "customer", "connection_hint": "excluded",
         "p_fixed_kw": 30000.0},
    ])
    edges = EdgeSet(edge_id=np.array([1]), source=np.array([1]), target=np.array([2]), length_m=np.array([10.0]),
                    in_settlement=np.array([False]), settlement_id=np.array([-1]), vertex_id=np.array([1, 2]),
                    vertex_xy=np.array([[0.0, 0.0], [10.0, 0.0]]))
    return DistrictInput(mv_grid_id=6, substation_id=5, vn_kv=20.0, nodes=nodes, edges=edges,
                         params=params or MvParameters(), lv=LV, edge_geometry=lambda ids: {})


def ring_topology(nop: int = 12) -> TopologyPlan:
    def ln(key, a, b, open_at=None, length=100.0):
        return LinePlan(key=key, from_key=a, to_key=b, role="ring", kind="cable", feeder_id=1, ring_id=1,
                        length_m=length, cable_m=length, overhead_m=0.0, straight_m=length, bridge_m=0.0,
                        geometry=shapely.LineString([(0, 0), (1, 1)]), std_type="cable", normally_open=open_at
                        is not None, open_at_key=open_at, pieces=(key,))
    lines = [ln(0, 10, 11), ln(1, 11, 12), ln(2, 12, 13, open_at=nop), ln(3, 13, 10)]
    return TopologyPlan(rings=[RingPlan(1, "ring", [11, 12, 13], nop, None)], lines=lines, joints={},
                        node_role={10: "busbar", 11: "ring", 12: "ring", 13: "ring", 14: "excluded"},
                        node_feeder={11: 1, 12: 1, 13: 2}, node_ring={11: 1, 12: 1, 13: 1},
                        node_position={11: 1, 12: 2, 13: 3}, stub_parent={}, diagnostics={})


def case(name: str) -> CaseResult:
    return CaseResult(name, True, pd.Series({10: 1.02, 11: 1.01}), pd.Series({0: 10.0}), 1.0, 0.1)


@pytest.fixture()
def fakes(monkeypatch):
    """Fake planning modules in ``sys.modules``; ``calls`` records the steps, ``state`` scripts the checks."""
    calls: list[str] = []
    state = {"violations": [[]], "actions": [[{"action": "move_nop", "ring_id": 1}]]}
    mods = {name: types.ModuleType(f"pylovo.mv_grid.{name}") for name in
            ("graph", "topology", "rings", "solvers", "electrical", "coincidence")}
    g, t, r, s, e, c = (mods[n] for n in ("graph", "topology", "rings", "solvers", "electrical", "coincidence"))

    def build_run_graph(edges, nodes, **kw):
        calls.append(f"run_graph:{kw['long_connector_m']:g}:{kw['bridge_detour']:g}")
        return types.SimpleNamespace(n_bridges=1, bridged_rows=np.array([False, False, False, True, False]))

    g.build_run_graph = build_run_graph
    g.trench_km = lambda rg, piece_lists: (calls.append(f"trench:{len(piece_lists)}") or (0.25, 2))
    t.terminal_rows = lambda inp: (calls.append("terminals") or np.arange(len(inp.nodes)))
    t.distance_model = lambda inp, rg, terminals: (
        calls.append("distances") or types.SimpleNamespace(dist_m=np.zeros((4, 4)), candidates=np.array([1, 2, 3]),
                                                           xy=np.zeros((4, 2))))
    r.RingEvaluator = type("RingEvaluator", (), {"from_model": classmethod(
        lambda cls, inp, model: (calls.append("evaluator") or cls()))})

    def solve(ev, dist, candidates, *, rounds, neighbours, time_guard_s):
        calls.append(f"solve:{rounds}:{neighbours}:{time_guard_s:g}")
        return SolverResult([[1, 2, 3]], 500.0, 400.0, 3, 7, False, 0.1, {"ls_gain_percent": 20.0})

    s.solve = solve
    t.build_plan = lambda inp, rg, model, ev, routes: (calls.append(f"plan:{routes}") or ring_topology())
    e.build_design_net = lambda inp, plan: (calls.append("net") or f"net-{plan.rings[0].nop_key}")
    e.run_cases = lambda net, inp: (calls.append(f"cases:{net}") or {
        "load": case("load"), "n1a": case("n1a"), "n1b": case("n1b")})

    def check(cases, inp, plan):
        calls.append("check")
        return state["violations"].pop(0) if state["violations"] else []

    def repair(inp, rg, model, ev, plan, violations):
        calls.append("repair")
        actions = state["actions"].pop(0) if state["actions"] else []
        return (ring_topology(nop=11) if actions else plan), actions

    e.check, e.repair = check, repair
    e.size_substation = lambda inp, plan: (calls.append("size") or (2, 25.0, "sized"))
    e.district_load_kw = lambda inp, plan: 1000.0 * sum(role == "ring" for role in plan.node_role.values())
    e.build_stored_net = lambda net, units, mva, inp: (calls.append(f"stored:{net}:{units}:{mva:g}") or ("stored", -2))
    e.station_budgets = lambda inp, plan, cases: (calls.append("budgets") or {})
    e.node_flags = lambda inp, plan, cases, budgets: {12: ["band_conflict"], 11: []}
    e.case_metrics = lambda cases: {"min_vm_load_pu": 1.01}
    e.power_flow_status = lambda cases, violations: "voltage_violation" if violations else "converged"
    c.coincident_kw = lambda installed, units, gf: float(np.sum(np.asarray(installed) * np.asarray(gf)))
    for name, module in mods.items():
        monkeypatch.setitem(sys.modules, f"pylovo.mv_grid.{name}", module)
    return calls, state


VIOLATION = Violation("load", "voltage_low", "bus", 12, 0.96, 0.97, 1)


def test_plan_district_composes_the_steps(fakes):
    calls, state = fakes
    params = MvParameters(local_search_rounds=7, neighbours=9, ls_time_guard_s=30.0,
                          long_connector_m=250.0, bridge_detour=1.4)
    plan = planner.plan_district(small_input(params))
    assert isinstance(plan, DistrictPlan)
    assert calls == ["run_graph:250:1.4", "terminals", "distances", "evaluator", "solve:7:9:30",
                     "plan:[[1, 2, 3]]", "net", "cases:net-12", "check", "size", "stored:net-12:2:25", "budgets",
                     "trench:4"]
    assert (plan.hv_mv_units, plan.hv_mv_unit_mva, plan.hv_mv_sizing, plan.oltc_tap) == (2, 25.0, "sized", -2)
    assert plan.net == "stored" and plan.power_flow_status == "converged" and plan.repairs == []
    # district peak: the load of the HV/MV sizing (electrical.district_load_kw of the final plan: 3 ring nodes)
    assert plan.peak_load_mw == pytest.approx(3.0)
    assert plan.peak_feed_in_mw == 0.0                                              # no feed-in case
    m = plan.metrics
    assert m["n_stations"] == 2 and m["n_customers"] == 1 and m["n_rings"] == 1 and m["n_nops"] == 1
    assert m["min_vm_load_pu"] == 1.01 and m["ls_gain_percent"] == 20.0 and m["length_final_m"] == 400.0
    assert m["bridged_nodes"] == [13] and m["n_bridges"] == 1
    assert m["trench_km"] == 0.25 and m["max_circuits_per_piece"] == 2
    assert m["hv_mv_loading_n0_percent"] == pytest.approx(100 * plan.peak_load_mw / 0.95 / 50.0)
    assert m["hv_mv_loading_n1_percent"] == pytest.approx(100 * plan.peak_load_mw / 0.95 / 25.0)
    assert plan.topology.diagnostics["node_flags"] == {12: ["band_conflict"]}     # electrical flags for mv_node


def test_node_flags_of_topology_and_electrical_are_merged():
    merged = planner.merged_node_flags({3: ["stub_limit"], 4: []}, {3: ["not_n1_secure", "stub_limit"]},
                                       None, {"5": ("tap_limit",)})
    assert merged == {3: ["stub_limit", "not_n1_secure"], 5: ["tap_limit"]}


def test_repairs_are_re_evaluated_until_clean(fakes):
    calls, state = fakes
    state["violations"] = [[VIOLATION], [VIOLATION], []]
    state["actions"] = [[{"action": "move_nop", "ring_id": 1}], [{"action": "split_ring", "ring_id": 1}]]
    plan = planner.plan_district(small_input())
    assert calls.count("check") == 3 and calls.count("repair") == 2
    assert [r["round"] for r in plan.repairs] == [1, 2] and plan.repairs[1]["action"] == "split_ring"
    assert plan.violations == [] and plan.power_flow_status == "converged"
    assert plan.topology.rings[0].nop_key == 11 and "stored:net-11:2:25" in calls
    assert plan.metrics["repair_rounds_used"] == 2 and plan.metrics["n_repairs"] == 2


def test_repair_loop_stops_without_actions_and_after_the_last_round(fakes):
    calls, state = fakes
    state["violations"] = [[VIOLATION]] * 10
    state["actions"] = [[]]
    plan = planner.plan_district(small_input())
    assert calls.count("check") == 1 and calls.count("repair") == 1
    assert plan.violations == [VIOLATION] and plan.power_flow_status == "voltage_violation"

    calls.clear()
    state["violations"] = [[VIOLATION]] * 10
    state["actions"] = [[{"action": "parallel"}]] * 10
    plan = planner.plan_district(small_input(MvParameters(repair_rounds=2)))
    assert calls.count("check") == 3 and calls.count("repair") == 2 and len(plan.repairs) == 2
    assert plan.metrics["n_violations"] == 1


def test_plan_tables_are_the_generic_format():
    nodes, lines = planner.plan_tables(small_input(), ring_topology())
    assert nodes["node_id"].tolist() == [10, 11, 12, 13]          # the excluded HV customer is left out
    assert nodes["kind"].tolist() == ["busbar", "station", "station", "customer"]
    assert nodes["area_class"].tolist() == ["rural", "urban", "rural", "rural"]
    assert lines["length_km"].tolist() == [0.1] * 4 and lines["normally_open"].tolist() == [False, False, True, False]
    assert lines["open_at_node"].tolist()[2] == 12
    topo = metrics.topology_metrics(nodes, lines)
    assert topo["n_rings"] == 1 and topo["n_nops"] == 1 and topo["ring_members_max"] == 3
    assert topo["circuit_km"] == pytest.approx(0.4)

    plan = ring_topology()
    plan.joints = {-1: (50.0, 0.0)}
    plan.lines[0] = LinePlan(0, 10, -1, "ring", "cable", 1, 1, 50.0, 50.0, 0.0, 50.0, 0.0, None, "cable")
    plan.lines.append(LinePlan(4, -1, 11, "ring", "cable", 1, 1, 50.0, 50.0, 0.0, 50.0, 0.0, None, "cable"))
    nodes, lines = planner.plan_tables(small_input(), plan)
    assert nodes.iloc[-1].to_dict()["node_id"] == -1 and nodes.iloc[-1]["kind"] == "joint"
    assert metrics.topology_metrics(nodes, lines)["n_joints"] == 1


def test_planner_is_pure():
    source = open(planner.__file__, encoding="utf-8").read()
    for forbidden in ("pylovo.database", "pylovo.config_loader", "pylovo.plotting", "psycopg2"):
        assert forbidden not in source


# ---------------------------------------------------------------------------------------------- end to end


def synthetic_district() -> DistrictInput:
    """Grid streets (200 m blocks), 30 stations, 2 customers, 1 dedicated customer, one island."""
    n = 7
    vertex = {}
    xy = []
    for i in range(n):
        for j in range(n):
            vertex[(i, j)] = len(xy) + 1
            xy.append((200.0 * i, 200.0 * j))
    island = {(0, 0): len(xy) + 1, (1, 0): len(xy) + 2}            # a street island east of the grid
    xy += [(2200.0, 600.0), (2400.0, 600.0)]
    src, dst = [], []
    for (i, j), v in vertex.items():
        if i + 1 < n:
            src.append(v), dst.append(vertex[(i + 1, j)])
        if j + 1 < n:
            src.append(v), dst.append(vertex[(i, j + 1)])
    src.append(island[(0, 0)]), dst.append(island[(1, 0)])
    xy_arr = np.array(xy)
    length = np.hypot(*(xy_arr[np.array(src) - 1] - xy_arr[np.array(dst) - 1]).T)
    edge_id = np.arange(1, len(src) + 1)
    geoms = {int(e): shapely.LineString([xy_arr[a - 1], xy_arr[b - 1]]) for e, a, b in zip(edge_id, src, dst)}
    edges = EdgeSet(edge_id=edge_id, source=np.array(src), target=np.array(dst), length_m=length,
                    in_settlement=np.ones(len(src), dtype=bool), settlement_id=np.ones(len(src), dtype=np.int64),
                    vertex_id=np.arange(1, len(xy) + 1), vertex_xy=xy_arr)
    first = xy_arr[src[0] - 1]                                       # the busbar sits on the first vertex
    rows = [{"mv_node_id": 1, "kind": "busbar", "connection_hint": "busbar", "x": float(first[0]),
             "y": float(first[1]), "attach_edge_id": int(edge_id[0]), "attach_fraction": 0.0, "sn_kva": np.nan}]
    horizontal = [int(e) for e, a, b in zip(edge_id, src, dst) if xy_arr[a - 1][1] == xy_arr[b - 1][1]
                  and e != edge_id[-1]]
    for k in range(30):
        e = horizontal[(7 * k) % len(horizontal)]
        a, b = src[e - 1], dst[e - 1]
        x, y = (xy_arr[a - 1] + xy_arr[b - 1]) / 2
        rows.append({"mv_node_id": 100 + k, "x": x, "y": y, "attach_edge_id": e, "attach_fraction": 0.5,
                     "inst_res": 150.0, "units_res": 40.0, "inst_com": 30.0, "units_com": 2.0, "settlement_id": 1,
                     "urban": True, "grid_result_id": 1000 + k})
    rows[1].update(attach_edge_id=int(edge_id[-1]), x=2300.0, y=600.0)              # station on the island
    for k, (kw, hint) in enumerate([(300.0, "ring"), (400.0, "ring"), (6000.0, "dedicated")]):
        e = horizontal[(11 * k + 3) % len(horizontal)]
        rows.append({"mv_node_id": 200 + k, "kind": "customer", "connection_hint": hint, "p_fixed_kw": kw,
                     "x": float(xy_arr[src[e - 1] - 1][0]), "y": float(xy_arr[src[e - 1] - 1][1]) + 30.0,
                     "attach_edge_id": e, "attach_fraction": 0.0, "attach_offset_m": 30.0, "sn_kva": np.nan})
    return DistrictInput(mv_grid_id=1, substation_id=1, vn_kv=20.0, nodes=node_table(rows), edges=edges,
                         params=MvParameters(stub_min_offset_m=1e9),   # no stubs
                         lv=LV,
                         edge_geometry=lambda ids: {int(i): geoms[int(i)] for i in ids})


def test_plan_district_end_to_end_on_a_synthetic_district():
    inp = synthetic_district()
    plan = planner.plan_district(inp)
    topo = plan.topology
    for key in inp.nodes.index[1:]:
        assert topo.node_role.get(int(key)) not in (None, "excluded"), key        # every node connected
    for ring in topo.rings:
        if ring.kind == "ring":
            nops = [ln for ln in topo.lines if ln.ring_id == ring.ring_id and ln.normally_open]
            assert len(nops) == 1                                                  # one NOP per ring
    assert plan.violations == [] and plan.power_flow_status == "converged"
    assert plan.metrics["bridged_nodes"] == [100]   # the island station
    again = planner.plan_district(synthetic_district())
    assert [r.members for r in again.topology.rings] == [r.members for r in topo.rings]   # deterministic


def test_a_ten_kv_district_of_an_auto_variant_uses_its_own_types():
    """MV_NOMINAL_VOLTAGE_KV auto: the district voltage decides the cables, the nets and the HV/MV transformers."""
    import dataclasses

    base = synthetic_district()
    assert base.params.auto_voltage
    plans = {vn: planner.plan_district(dataclasses.replace(synthetic_district(), vn_kv=vn)) for vn in (20.0, 10.0)}
    for vn, plan in plans.items():
        cable = base.params.cable_at(vn)
        names = {ln.std_type for ln in plan.topology.lines}
        assert names == {cable.name}, vn                                   # cables only (overhead lines off)
        net = plan.net
        mv = net.bus[net.bus["kind"].astype(str) != "hv_busbar"]
        assert set(mv["vn_kv"].astype(float)) == {vn}
        assert set(net.line["max_i_ka"].astype(float)) == {cable.max_i_ka}
        assert set(net.trafo["vn_lv_kv"].astype(float)) == {vn} and set(net.trafo["vn_hv_kv"]) == {110.0}
        assert net.trafo["std_type"].str.endswith(f"110/{vn:g} kV").all()
        assert plan.power_flow_status in ("converged", "voltage_violation", "loading_violation")
    assert "6/10 kV" in plans[10.0].topology.lines[0].std_type and "12/20 kV" in plans[20.0].topology.lines[0].std_type
    # half the voltage, about four times the drop per kW and half the ampacity: at least as many rings
    rings_of = {vn: sum(r.kind == "ring" for r in plan.topology.rings) for vn, plan in plans.items()}
    assert rings_of[10.0] >= rings_of[20.0]
