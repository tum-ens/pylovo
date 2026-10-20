"""Tests of the MV electrical checks (pylovo.mv_grid.electrical) on synthetic districts; no database."""
from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import math

import networkx as nx
import numpy as np
import pandapower as pp
import pandas as pd
import pytest
from shapely.geometry import LineString

from pylovo.mv_grid import coincidence, electrical
from pylovo.mv_grid.model import (
    NODE_COLUMNS,
    CaseResult,
    DistrictInput,
    EdgeSet,
    LinePlan,
    RingPlan,
    TopologyPlan,
    Violation,
)
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

X0, Y0 = 700_000.0, 5_380_000.0
BUSBAR = 1
SUBSTATION = 7
SIM = {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6}


def _lv(reference: float | None = 0.96) -> LvVersionParameters:
    return LvVersionParameters(version_id="t", sim_factor=dict(SIM), power_factor=0.95, mv_direct_threshold_kw=300.0,
                               residential_only=False, lv_reference_voltage_pu=reference, use_dso=False,
                               use_open=False, use_manual=False, electrical_backend="pandapower")


def _row(kind: str, x: float, y: float, **values) -> dict:
    row = {column: 0.0 for column in NODE_COLUMNS}
    row.update(kind=kind, connection_hint="busbar" if kind == "busbar" else "ring",
               x=x, y=y, attach_edge_id=-1, attach_fraction=0.0, attach_offset_m=0.0, bridged=False,
               settlement_id=-1, urban=False, plz=0, plz_settlement_type=0, gen_level="", sn_kva=math.nan,
               grid_result_id=math.nan, lv_design_source_vm_pu=math.nan, lv_max_drop_pu=math.nan)
    row.update(values)
    return row


def _station(x: float, y: float, gid: int, households: float = 80.0, **values) -> dict:
    defaults = dict(inst_res=17.0 * households, units_res=households, inst_com=100.0, units_com=1.0, sn_kva=400.0,
                    grid_result_id=float(gid), lv_design_source_vm_pu=0.98, lv_max_drop_pu=0.05)
    defaults.update(values)
    return _row("station", x, y, **defaults)


class Builder:
    """Synthetic district: nodes, lines and rings of a TopologyPlan built step by step."""

    def __init__(self, params: MvParameters | None = None, lv: LvVersionParameters | None = None):
        self.params = params or MvParameters()
        self.lv = lv or _lv()
        self.rows: dict[int, dict] = {BUSBAR: _row("busbar", X0, Y0)}
        self.lines: list[LinePlan] = []
        self.rings: list[RingPlan] = []
        self.joints: dict[int, tuple[float, float]] = {}
        self.role: dict[int, str] = {BUSBAR: "busbar"}
        self.feeder: dict[int, int] = {}
        self.ring_of: dict[int, int] = {}
        self.position: dict[int, int] = {}
        self.stub_parent: dict[int, int] = {}
        self.next_key = 2

    def xy(self, key: int) -> tuple[float, float]:
        if key in self.joints:
            return self.joints[key]
        return self.rows[key]["x"], self.rows[key]["y"]

    def add_node(self, row: dict) -> int:
        key = self.next_key
        self.next_key += 1
        self.rows[key] = row
        return key

    def line(self, a: int, b: int, role: str, ring_id: int | None, *, kind: str = "cable", length_m: float | None = None,
             normally_open: bool = False, open_at: int | None = None, exit_: str | None = None, parallel: int = 1,
             feeder: int | None = None) -> LinePlan:
        geometry = LineString([self.xy(a), self.xy(b)])
        length = float(length_m if length_m is not None else max(geometry.length, 1.0))
        cable_m = length if kind == "cable" else (0.4 * length if kind == "mixed" else 0.0)
        names = {"cable": self.params.cable.name, "overhead": self.params.overhead.name,
                 "mixed": f"{self.params.cable.name} + {self.params.overhead.name}"}
        line = LinePlan(key=len(self.lines), from_key=a, to_key=b, role=role, kind=kind, feeder_id=feeder,
                        ring_id=ring_id, length_m=length, cable_m=cable_m, overhead_m=length - cable_m,
                        straight_m=geometry.length, bridge_m=0.0, geometry=geometry, std_type=names[kind],
                        parallel=parallel, normally_open=normally_open, open_at_key=open_at, exit=exit_)
        self.lines.append(line)
        return line

    def ring(self, k: int, angle_deg: float = 0.0, spacing: float = 500.0, nop: int | None = None,
             households: float = 80.0, set_exits: bool = False, kind: str = "cable", **station_values) -> int:
        """A hairpin ring of k stations: half A out along ``angle``, half B back; NOP after position ``nop``."""
        ring_id = len(self.rings) + 1
        theta = math.radians(angle_deg)
        direction, normal = np.array([math.cos(theta), math.sin(theta)]), np.array([-math.sin(theta), math.cos(theta)])
        half = (k + 1) // 2
        members = []
        for i in range(k):
            step = i + 1 if i < half else k - i
            offset = 50.0 if i < half else -50.0
            p = np.array([X0, Y0]) + direction * spacing * step + normal * offset
            gid = 1000 * ring_id + i
            members.append(self.add_node(_station(p[0], p[1], gid, households, **station_values)))
        m = nop if nop is not None else max(half, 1)
        fa, fb = 2 * ring_id - 1, 2 * ring_id
        sequence = [BUSBAR, *members, BUSBAR]
        for j in range(k + 1):
            a, b = sequence[j], sequence[j + 1]
            exit_ = ("a" if j == 0 else "b" if j == k else None) if set_exits else None
            is_open = (j == m) and k >= 2
            self.line(a, b, "ring", ring_id, kind=kind, normally_open=is_open, open_at=a if is_open else None,
                      exit_=exit_, feeder=fa if j < m else fb)
        for i, key in enumerate(members):
            self.role[key] = "ring"
            self.feeder[key] = fa if i < m else fb
            self.ring_of[key] = ring_id
            self.position[key] = i + 1
        self.rings.append(RingPlan(ring_id, "ring", list(members), members[m - 1], None, fa, fb))
        return ring_id

    def stub_on(self, ring_id: int, section: int, households: float = 30.0) -> tuple[int, int]:
        """Split the ring section after position ``section`` with a joint and hang one stub station on it."""
        ring = next(r for r in self.rings if r.ring_id == ring_id)
        a, b = ring.members[section - 1], ring.members[section]
        line = next(item for item in self.lines if {item.from_key, item.to_key} == {a, b})
        assert not line.normally_open
        joint = -(len(self.joints) + 1)
        xa, xb = self.xy(a), self.xy(b)
        self.joints[joint] = ((xa[0] + xb[0]) / 2, (xa[1] + xb[1]) / 2)
        self.lines.remove(line)
        self.line(a, joint, "ring", ring_id, feeder=line.feeder_id)
        self.line(joint, b, "ring", ring_id, feeder=line.feeder_id)
        stub = self.add_node(_station(self.joints[joint][0], self.joints[joint][1] + 400.0, 9000 + len(self.rows),
                                      households))
        self.line(joint, stub, "stub", ring_id, feeder=line.feeder_id)
        ring.members.insert(section, joint)
        self.role[joint], self.role[stub] = "joint", "stub"
        self.ring_of[joint] = self.ring_of[stub] = ring_id
        self.feeder[joint] = self.feeder[stub] = line.feeder_id
        self.stub_parent[stub] = joint
        self._renumber()
        return joint, stub

    def dedicated_customer(self, p_fixed_kw: float, distance: float = 3000.0, angle_deg: float = 180.0,
                           set_exits: bool = False) -> tuple[int, int]:
        theta = math.radians(angle_deg)
        key = self.add_node(_row("customer", X0 + distance * math.cos(theta), Y0 + distance * math.sin(theta),
                                 connection_hint="dedicated", p_fixed_kw=p_fixed_kw))
        ring_id = len(self.rings) + 1
        feeder = 100 + ring_id
        self.line(BUSBAR, key, "dedicated", ring_id, exit_="a" if set_exits else None, feeder=feeder)
        self.line(BUSBAR, key, "dedicated", ring_id, normally_open=True, open_at=key,
                  exit_="b" if set_exits else None, feeder=feeder)
        self.rings.append(RingPlan(ring_id, "dedicated", [key], key, None, feeder, None))
        self.role[key], self.ring_of[key], self.feeder[key] = "dedicated", ring_id, feeder
        return key, ring_id

    def tie(self, a: int, b: int) -> LinePlan:
        return self.line(a, b, "tie", None, normally_open=True, open_at=b)

    def _renumber(self) -> None:
        self.lines = [dataclasses.replace(line, key=i) for i, line in enumerate(self.lines)]

    def build(self, known_units_mva: tuple[float, ...] = ()) -> tuple[DistrictInput, TopologyPlan]:
        self._renumber()
        nodes = pd.DataFrame.from_dict(self.rows, orient="index")[list(NODE_COLUMNS)]
        nodes.index.name = "mv_node_id"
        edges = EdgeSet(*(np.array([], dtype=np.int64) for _ in range(4)), np.array([], dtype=bool),
                        np.array([], dtype=np.int64), np.array([], dtype=np.int64), np.zeros((0, 2)))
        inp = DistrictInput(mv_grid_id=3, substation_id=SUBSTATION, vn_kv=self.params.base_kv, nodes=nodes, edges=edges,
                            params=self.params, lv=self.lv, known_units_mva=known_units_mva,
                            edge_geometry=lambda ids: {})
        plan = TopologyPlan(rings=list(self.rings), lines=list(self.lines), joints=dict(self.joints),
                            node_role=dict(self.role), node_feeder=dict(self.feeder), node_ring=dict(self.ring_of),
                            node_position=dict(self.position), stub_parent=dict(self.stub_parent), diagnostics={})
        return inp, plan


def _full_district(**kwargs) -> tuple[Builder, DistrictInput, TopologyPlan, dict]:
    b = Builder(**kwargs)
    r1 = b.ring(6, angle_deg=0.0)
    r2 = b.ring(4, angle_deg=90.0)
    joint, stub = b.stub_on(r1, 1)
    customer, rd = b.dedicated_customer(1500.0)
    b.tie(b.rings[r2 - 1].members[-1], BUSBAR)
    inp, plan = b.build()
    return b, inp, plan, dict(r1=r1, r2=r2, joint=joint, stub=stub, customer=customer, rd=rd)


# ---------------------------------------------------------------------------------------------- design net
def test_design_net_shape_names_and_columns():
    b, inp, plan, ids = _full_district()
    net = electrical.build_design_net(inp, plan)
    n_nodes = len(inp.nodes) + len(plan.joints)
    assert len(net.bus) == n_nodes
    assert net.bus.at[0, "name"] == f"MV busbar {SUBSTATION}" and net.bus.at[0, "type"] == "b"
    assert net.bus["node_key"].tolist()[0] == BUSBAR
    assert set(net.bus["kind"]) == {"busbar", "station", "customer", "joint"}
    assert net.bus.loc[net.bus["node_key"] == ids["joint"], "name"].item() == f"MV node {ids['joint']}"
    assert np.isnan(net.bus.loc[net.bus["node_key"] == ids["joint"], "mv_node_id"].item())
    lon, lat = json.loads(net.bus.at[0, "geo"])["coordinates"]
    assert 11.0 < lon < 12.5 and 48.0 < lat < 49.0
    assert (net.bus["vn_kv"] == 20.0).all()
    assert len(net.ext_grid) == 1 and net.ext_grid.at[0, "vm_pu"] == pytest.approx(1.02)

    ties = [line for line in plan.lines if line.role == "tie"]
    assert len(ties) == 1 and len(net.line) == len(plan.lines) - 1
    assert set(net.line["line_key"]) == {line.key for line in plan.lines if line.role != "tie"}
    cable = inp.params.cable
    assert np.allclose(net.line["r_ohm_per_km"], cable.r_ohm_per_km)
    assert np.allclose(net.line["c_nf_per_km"], cable.c_nf_per_km)
    geo = json.loads(net.line.at[0, "geo"])
    assert geo["type"] == "LineString" and len(geo["coordinates"]) == 2

    # switches: the ring NOPs and the open leg of the dedicated feeder, open, at their open_at node
    open_lines = [line for line in plan.lines if line.normally_open and line.role != "tie"]
    assert len(net.switch) == len(open_lines) == 3
    assert not net.switch["closed"].any() and (net.switch["type"] == "LBS").all()
    for _, sw in net.switch.iterrows():
        line = next(item for item in open_lines if item.key == sw["line_key"])
        assert net.bus.at[sw["bus"], "node_key"] == line.open_at_key

    # exits: rings a/b, dedicated legs a (closed) and b (open)
    for ring in plan.rings:
        rows = net.line[(net.line["ring_id"] == ring.ring_id) & net.line["exit"].notna()]
        assert sorted(rows["exit"]) == ["a", "b"]
    legs = net.line[net.line["ring_id"] == ids["rd"]].set_index("exit")
    assert legs.loc["b", "line_key"] in {line.key for line in open_lines}

    # loads: one per station and customer, names
    stations = inp.nodes.index[inp.nodes["kind"] == "station"]
    assert len(net.load) == len(stations) + 1
    assert f"MV customer {ids['customer']}" in set(net.load["name"])
    assert "Station 1000" in set(net.load["name"])
    assert net.load.loc[net.load["node_key"] == ids["customer"], "p_mw"].item() == pytest.approx(1.5)
    assert net.sgen.empty


def test_design_net_line_kinds_and_errors():
    b = Builder()
    b.ring(3, kind="overhead")
    inp, plan = b.build()
    net = electrical.build_design_net(inp, plan)
    assert (net.line["kind"] == "overhead").all() and (net.line["type"] == "ol").all()
    assert np.allclose(net.line["x_ohm_per_km"], inp.params.overhead.x_ohm_per_km)

    b = Builder()
    b.ring(3, kind="mixed")
    inp, plan = b.build()
    net = electrical.build_design_net(inp, plan)
    line = plan.lines[0]
    expected = 0.4 * inp.params.cable.r_ohm_per_km + 0.6 * inp.params.overhead.r_ohm_per_km
    assert net.line.at[0, "r_ohm_per_km"] == pytest.approx(expected)
    assert net.line.at[0, "max_i_ka"] == pytest.approx(min(inp.params.cable.max_i_ka, inp.params.overhead.max_i_ka))
    assert net.line.at[0, "cable_km"] == pytest.approx(line.cable_m / 1000)

    plan.lines.append(dataclasses.replace(plan.lines[0], key=99, to_key=12345))
    with pytest.raises(ValueError, match="12345"):
        electrical.build_design_net(inp, plan)


def test_loads_are_feeder_proportional():
    b, inp, plan, ids = _full_district()
    loads = electrical.node_loads(inp, plan)
    g = inp.lv.g()
    for ring in plan.rings:
        if ring.kind != "ring":
            continue
        members = [k for k in loads.index if plan.node_ring.get(k) == ring.ring_id]
        installed = inp.nodes.loc[members, list(coincidence.INSTALLED_COLUMNS)].to_numpy(dtype=float)
        units = inp.nodes.loc[members, list(coincidence.UNITS_COLUMNS)].to_numpy(dtype=float)
        whole = float(coincidence.coincident_kw(installed.sum(0), units.sum(0), g))
        assert loads.loc[members, "p_n1_kw"].sum() == pytest.approx(whole, rel=1e-12)
        for feeder in loads.loc[members, "feeder"].unique():
            part = [k for k in members if loads.at[k, "feeder"] == feeder]
            inst = inp.nodes.loc[part, list(coincidence.INSTALLED_COLUMNS)].to_numpy(dtype=float)
            cnt = inp.nodes.loc[part, list(coincidence.UNITS_COLUMNS)].to_numpy(dtype=float)
            assert loads.loc[part, "p_kw"].sum() == pytest.approx(
                float(coincidence.coincident_kw(inst.sum(0), cnt.sum(0), g)), rel=1e-12)
        # the stub hangs on the half of its joint
        if ring.ring_id == ids["r1"]:
            first = ring.members[0]
            assert loads.at[ids["stub"], "feeder"] == loads.at[first, "feeder"]
    assert loads.at[ids["customer"], "p_kw"] == pytest.approx(1500.0)
    assert loads.at[ids["customer"], "p_n1_kw"] == pytest.approx(1500.0)
    assert np.allclose(loads["q_kvar"], loads["p_kw"] * math.tan(math.acos(0.95)))
    # never the sum of station peaks: the district load is below the sum of the own peaks
    own = sum(float(coincidence.coincident_kw(inp.nodes.loc[k, list(coincidence.INSTALLED_COLUMNS)].to_numpy(float),
                                              inp.nodes.loc[k, list(coincidence.UNITS_COLUMNS)].to_numpy(float), g))
              for k in loads.index if inp.nodes.at[k, "kind"] == "station")
    assert electrical.district_load_kw(inp, plan) < own + 1500.0


# ---------------------------------------------------------------------------------------------- cases
def test_case_net_per_case():
    b, inp, plan, ids = _full_district()
    net = electrical.build_design_net(inp, plan)
    params = inp.params
    original = copy.deepcopy(net)

    load = electrical.case_net(net, "load", params)
    assert not load.switch["closed"].any() and load.line["in_service"].all()
    assert np.allclose(load.load["p_mw"], net.load["p_normal_mw"])

    for case, out in (("n1a", "a"), ("n1b", "b")):
        n1 = electrical.case_net(net, case, params)
        assert n1.switch["closed"].all()
        assert set(n1.line.index[~n1.line["in_service"]]) == set(n1.line.index[n1.line["exit"] == out])
        assert (~n1.line["in_service"]).sum() == len(plan.rings) - 0  # one exit per ring and per 2-leg feeder
        assert np.allclose(n1.load["p_mw"], net.load["p_n1_mw"])

    edge = electrical.case_net(net, "load", params, busbar_vm_pu=1.005)
    assert edge.ext_grid.at[0, "vm_pu"] == pytest.approx(1.005)
    # the base net is unchanged, and a case of a case net is not needed: the base columns stay
    pd.testing.assert_frame_equal(net.load, original.load)
    pd.testing.assert_frame_equal(net.line, original.line)
    with pytest.raises(ValueError, match="unknown MV case"):
        electrical.case_net(net, "winter", params)


def test_run_cases_converge_and_n1_feeds_each_ring_from_one_side():
    b, inp, plan, ids = _full_district()
    net = electrical.build_design_net(inp, plan)
    cases = electrical.run_cases(net, inp, plan)
    assert set(cases) == {"load", "n1a", "n1b"}
    assert all(result.converged for result in cases.values())
    assert cases["load"].vm_pu.index[0] == BUSBAR and cases["load"].vm_pu.iloc[0] == pytest.approx(1.02)
    assert len(cases["load"].vm_pu) == len(net.bus)
    # (n-1): the out-of-service exit carries nothing, the other exit of the ring carries the whole ring
    exits = net.line.dropna(subset=["exit"]).set_index("line_key")
    for case, out, other in (("n1a", "a", "b"), ("n1b", "b", "a")):
        loading = cases[case].loading_percent
        for ring in plan.rings:
            ring_exits = exits[exits["ring_id"] == ring.ring_id]
            assert loading[ring_exits.index[ring_exits["exit"] == out]].item() == 0.0
            assert loading[ring_exits.index[ring_exits["exit"] == other]].item() > 0.0
    # every node is supplied in every case
    for result in cases.values():
        assert result.vm_pu.notna().all()
    # load case: the slack carries the coincident loads of the feeders (plus losses)
    assert cases["load"].slack_p_mw == pytest.approx(net.load["p_mw"].sum(), rel=0.02)
    violations = electrical.check(cases, inp, plan)
    assert violations == []
    assert electrical.power_flow_status(cases, violations) == "converged"
    metrics = electrical.case_metrics(cases)
    assert metrics["load_converged"] and 0 < metrics["max_voltage_drop_percent"] < 5
    assert metrics["max_line_loading_n1_percent"] >= metrics["max_line_loading_percent"]
    json.dumps(metrics)   # JSON-native


def test_long_heavy_ring_violates_voltage():
    b = Builder(MvParameters(cable_type="NA2XS2Y 1x240 RM/25 12/20 kV"))    # the hand value is for 1x240
    b.ring(10, spacing=6000.0, households=1500.0)       # about 9 MW per 30 km half: 5.4 % drop by hand
    inp, plan = b.build()
    cases = electrical.run_cases(electrical.build_design_net(inp, plan), inp, plan)
    violations = electrical.check(cases, inp, plan)
    assert any(v.case == "load" and v.kind == "voltage_low" for v in violations)
    assert all(v.ring_id == 1 for v in violations if v.element == "bus")
    assert electrical.power_flow_status(cases, violations) == "voltage_violation"


def _result(case: str, vm: dict[int, float], loading: dict[int, float] | None = None, converged: bool = True
            ) -> CaseResult:
    return CaseResult(case, converged, pd.Series(vm, dtype=float), pd.Series(loading or {}, dtype=float), 0.0, 0.0)


def test_check_criteria_and_status():
    b = Builder()
    r = b.ring(4)
    joint, stub = b.stub_on(r, 1)
    inp, plan = b.build()
    m = plan.rings[0].members                  # [s1, joint, s2, s3, s4]
    base = {BUSBAR: 1.02, **{k: 1.0 for k in [*m, stub]}}
    cases = {
        "load": _result("load", {**base, m[2]: 0.969, m[3]: 0.9701}, {0: 60.5, 1: 59.9}),
        "n1a": _result("n1a", {**base, stub: 0.90, m[4]: 0.919}, {0: 0.0, 2: 101.0}),
        "n1b": _result("n1b", base, {1: 99.0}),
    }
    violations = electrical.check(cases, inp, plan)
    found = {(v.case, v.kind, v.element, v.key) for v in violations}
    assert found == {("load", "voltage_low", "bus", m[2]), ("load", "loading", "line", 0),
                     ("n1a", "voltage_low", "bus", m[4]), ("n1a", "loading", "line", 2)}
    low = next(v for v in violations if v.case == "load" and v.kind == "voltage_low")
    assert low.limit == pytest.approx(0.97) and low.ring_id == r
    assert next(v for v in violations if v.case == "n1a" and v.kind == "voltage_low").limit == pytest.approx(0.92)
    assert electrical.power_flow_status(cases, violations) == "voltage_violation"
    only_loading = [v for v in violations if v.kind == "loading"]
    assert electrical.power_flow_status(cases, only_loading) == "loading_violation"
    # a case that does not converge is one violation at the busbar
    failed = {**cases, "n1b": _result("n1b", base, converged=False)}
    violations = electrical.check(failed, inp, plan)
    assert ("n1b", "not_converged", "bus", BUSBAR) in {(v.case, v.kind, v.element, v.key) for v in violations}
    assert electrical.power_flow_status(failed, violations) == "not_converged"
    # an unsupplied node (NaN) is no violation but is counted
    cases = {"load": _result("load", {**base, m[0]: math.nan})}
    assert electrical.check(cases, inp, plan) == []
    assert electrical.case_metrics(cases)["load_unsupplied_nodes"] == 1


# ---------------------------------------------------------------------------------------------- repair
class FakeTopology:
    """Stubs of topology.move_nop, split_ring, upgrade_lines and add_parallel that record their calls."""

    def __init__(self, moved: bool = True):
        self.moved = moved
        self.calls: list[tuple] = []

    def move_nop(self, inp, rg, model, ev, plan, ring_id):
        self.calls.append(("move_nop", ring_id))
        return plan, self.moved

    def split_ring(self, inp, rg, model, ev, plan, ring_id):
        self.calls.append(("split_ring", ring_id))
        ring = next(r for r in plan.rings if r.ring_id == ring_id)
        new_id = max(r.ring_id for r in plan.rings) + 1
        half = len(ring.members) // 2
        rings = [r for r in plan.rings if r.ring_id != ring_id]
        rings += [dataclasses.replace(ring, members=ring.members[:half]),
                  RingPlan(new_id, "ring", ring.members[half:], None, None)]
        return dataclasses.replace(plan, rings=rings)

    def upgrade_lines(self, plan, line_keys, vn_kv, needed_ka=None):
        self.calls.append(("upgrade_lines", tuple(line_keys)))
        return importlib.import_module("pylovo.mv_grid.topology").upgrade_lines(plan, line_keys, vn_kv, needed_ka)

    def add_parallel(self, plan, line_keys, max_parallel):
        self.calls.append(("add_parallel", tuple(line_keys)))
        lines, changed = [], []
        for line in plan.lines:
            if line.key in line_keys and line.parallel < max_parallel:
                lines.append(dataclasses.replace(line, parallel=line.parallel + 1))
                changed.append(line.key)
            else:
                lines.append(line)
        return dataclasses.replace(plan, lines=lines), changed


@pytest.fixture
def fake_topology(monkeypatch):
    def install(moved: bool = True) -> FakeTopology:
        fake = FakeTopology(moved)
        monkeypatch.setattr(electrical, "_topology", lambda: fake)
        return fake
    return install


def _violation(case, kind, element, key, ring_id=None, value=0.0, limit=0.0):
    return Violation(case, kind, element, key, value, limit, ring_id)


def test_repair_voltage_ladder_moves_the_nop_then_splits_then_adds_parallel(fake_topology):
    fake = fake_topology(moved=True)
    b = Builder()
    r = b.ring(6, set_exits=True)
    inp, plan = b.build()
    last = plan.rings[0].members[-1]
    low = _violation("load", "voltage_low", "bus", last, r, 0.96, 0.97)
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert [a["action"] for a in actions] == ["move_nop"] and actions[0]["round"] == 1
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert [a["action"] for a in actions] == ["split_ring"] and actions[0]["new_ring_ids"] == [2]
    assert plan.diagnostics["repair_stage"] == {"1": 2, "2": 2}
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    exits = sorted(line.key for line in plan.lines if line.exit in ("a", "b"))
    assert [a["action"] for a in actions] == ["parallel"] and actions[0]["line_keys"] == exits
    assert all(line.parallel == 2 for line in plan.lines if line.key in exits)
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert actions == [] and plan.diagnostics["repair_reports"][0]["action"] == "unresolved_voltage"
    assert plan.diagnostics["repair_round"] == 4
    assert [c[0] for c in fake.calls] == ["move_nop", "split_ring", "add_parallel"]


def _types_of(plan, keys):
    by_key = {line.key: line.std_type for line in plan.lines}
    return [by_key[k] for k in keys]


def test_path_lines_follow_the_switching_state_of_the_case():
    b = Builder()
    r = b.ring(6, set_exits=True)
    inp, plan = b.build()
    m = plan.rings[0].members
    keys = {frozenset((ln.from_key, ln.to_key)): ln.key for ln in plan.lines}
    a_half = [keys[frozenset(p)] for p in [(BUSBAR, m[0]), (m[0], m[1]), (m[1], m[2])]]
    assert electrical.path_lines(plan, BUSBAR, m[2], "load") == a_half
    # exit A out: m[2] is fed around the ring from exit B through the closed open point
    around = electrical.path_lines(plan, BUSBAR, m[2], "n1a")
    assert around[0] == keys[frozenset((BUSBAR, m[5]))] and len(around) == 4
    assert electrical.path_lines(plan, BUSBAR, 999, "load") == []


def test_repair_upgrades_an_overloaded_overhead_section_before_splitting(fake_topology):
    fake = fake_topology()
    b = Builder()
    r = b.ring(6, kind="overhead", set_exits=True)
    inp, plan = b.build()
    exit_key = next(line.key for line in plan.lines if line.exit == "a")
    load = _violation("load", "loading", "line", exit_key, r, 70.0, 60.0)
    plan, actions = electrical.repair(inp, None, None, None, plan, [load])
    # 410 A x 70/60 = 478 A: the smallest overhead line that carries it is 184-AL1 (535 A)
    assert [a["action"] for a in actions] == ["upgrade"] and actions[0]["line_keys"] == [exit_key]
    assert actions[0]["std_types"] == ["184-AL1/30-ST1A 20.0"] and actions[0]["reason"] == "loading"
    plan, actions = electrical.repair(inp, None, None, None, plan, [load])
    assert _types_of(plan, [exit_key]) == ["243-AL1/39-ST1A 20.0"]
    plan, actions = electrical.repair(inp, None, None, None, plan, [load])    # the largest: split the ring
    assert [a["action"] for a in actions] == ["split_ring"]
    assert [c[0] for c in fake.calls] == ["upgrade_lines", "upgrade_lines", "split_ring"]


def test_repair_voltage_ladder_upgrades_the_path_before_splitting(fake_topology):
    fake = fake_topology(moved=True)
    b = Builder()
    r = b.ring(6, kind="overhead", set_exits=True)
    inp, plan = b.build()
    target = plan.rings[0].members[2]
    path = electrical.path_lines(plan, BUSBAR, target, "load")
    low = _violation("load", "voltage_low", "bus", target, r, 0.96, 0.97)
    names = []
    for _ in range(4):
        plan, actions = electrical.repair(inp, None, None, None, plan, [low])
        names.append([a["action"] for a in actions])
        if actions and actions[0]["action"] == "upgrade":
            assert actions[0]["line_keys"] == path and actions[0]["reason"] == "voltage"
    assert names == [["move_nop"], ["upgrade"], ["upgrade"], ["upgrade"]]
    assert set(_types_of(plan, path)) == {"243-AL1/39-ST1A 20.0"}
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert [a["action"] for a in actions] == ["split_ring"]


def test_repair_upgrades_the_path_at_once_when_the_nop_cannot_move(fake_topology):
    fake = fake_topology(moved=False)
    b = Builder()
    r = b.ring(6, kind="overhead")
    inp, plan = b.build()
    target = plan.rings[0].members[1]
    low = _violation("load", "voltage_low", "bus", target, r, 0.96, 0.97)
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert [c[0] for c in fake.calls] == ["move_nop", "upgrade_lines"]
    assert [a["action"] for a in actions] == ["upgrade"]
    assert plan.diagnostics["repair_stage"] == {"1": 1}


def test_repair_splits_at_once_when_the_nop_cannot_move(fake_topology):
    fake = fake_topology(moved=False)
    b = Builder()
    r = b.ring(6)
    inp, plan = b.build()
    low = _violation("n1a", "voltage_low", "bus", plan.rings[0].members[0], r, 0.91, 0.92)
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert [c[0] for c in fake.calls] == ["move_nop", "split_ring"]
    assert [a["action"] for a in actions] == ["split_ring"]


def test_repair_loading_rules(fake_topology):
    fake = fake_topology()
    b = Builder()
    r = b.ring(6)
    customer, rd = b.dedicated_customer(9000.0)
    inp, plan = b.build()
    exit_key = next(line.key for line in plan.lines if line.ring_id == r and BUSBAR in (line.from_key, line.to_key))
    middle_key = next(line.key for line in plan.lines if line.ring_id == r and BUSBAR not in (line.from_key, line.to_key)
                      and not line.normally_open)
    legs = sorted(line.key for line in plan.lines if line.ring_id == rd)
    # an overloaded busbar exit of a ring with >= 4 members splits the ring; a dedicated leg gets a parallel cable
    plan2, actions = electrical.repair(inp, None, None, None, plan, [
        _violation("load", "loading", "line", exit_key, r, 70.0, 60.0),
        _violation("n1a", "loading", "line", legs[1], rd, 120.0, 100.0)])
    assert sorted(a["action"] for a in actions) == ["parallel", "split_ring"]
    assert fake.calls == [("add_parallel", tuple(legs)), ("split_ring", r)]   # parallel cables before structure
    parallel = next(a for a in actions if a["action"] == "parallel")
    assert parallel["line_keys"] == legs and parallel["reason"] == "dedicated"
    # an overloaded middle section gets a parallel cable, up to the cap
    plan3, actions = electrical.repair(inp, None, None, None, plan, [
        _violation("load", "loading", "line", middle_key, r, 70.0, 60.0)])
    assert [a["action"] for a in actions] == ["parallel"] and actions[0]["line_keys"] == [middle_key]
    capped = dataclasses.replace(plan, lines=[dataclasses.replace(line, parallel=2) for line in plan.lines],
                                 diagnostics={})
    plan4, actions = electrical.repair(inp, None, None, None, capped, [
        _violation("load", "loading", "line", middle_key, r, 70.0, 60.0),
        _violation("load", "loading", "line", legs[0], rd, 70.0, 60.0)])
    assert actions == []
    assert sorted(item["action"] for item in plan4.diagnostics["repair_reports"]) == [
        "dedicated_overload", "unresolved_loading"]
    # non-convergence is only reported
    plan5, actions = electrical.repair(inp, None, None, None, plan, [
        _violation("n1b", "not_converged", "bus", BUSBAR, None, math.nan, math.nan)])
    assert actions == [] and plan5.diagnostics["repair_reports"][0]["action"] == "not_converged"


# ---------------------------------------------------------------------------------------------- HV/MV and OLTC
def test_size_substation_on_district_statistics():
    b = Builder()
    b.ring(10, households=400.0)
    b.ring(10, households=400.0, angle_deg=180.0)
    inp, plan = b.build()
    s_load = electrical.district_load_kw(inp, plan) / 1000.0 / 0.95
    assert 4.0 < s_load < 25.0
    assert electrical.substation_load(inp, plan) == pytest.approx(s_load)
    assert electrical.size_substation(inp, plan) == (2, 25.0, "sized")
    inp_osm = dataclasses.replace(inp, known_units_mva=(40.0, 40.0))
    assert electrical.size_substation(inp_osm, plan) == (2, 40.0, "osm_rating")


@pytest.mark.parametrize("unit_mva, households", [(25.0, 80.0), (31.5, 400.0), (20.0, 200.0)])
def test_stored_net_oltc_holds_the_busbar_setpoint(unit_mva, households):
    b = Builder()
    b.ring(10, households=households)
    b.ring(10, households=households, angle_deg=120.0)
    inp, plan = b.build()
    design = electrical.build_design_net(inp, plan)
    stored, tap = electrical.build_stored_net(design, 2, unit_mva, inp)
    busbar = int(stored.bus.index[stored.bus["kind"] == "busbar"][0])
    assert abs(stored.res_bus.at[busbar, "vm_pu"] - 1.02) <= 0.0075
    assert -9 <= tap <= 0 and (stored.trafo["tap_pos"] == tap).all()
    assert stored.trafo["name"].tolist() == ["HV/MV transformer 1", "HV/MV transformer 2"]
    assert (stored.trafo["sn_mva"] == unit_mva).all() and (stored.trafo["tap_changer_type"] == "Ratio").all()
    hv = stored.bus.index[stored.bus["kind"] == "hv_busbar"]
    assert len(hv) == 1 and stored.bus.at[hv[0], "vn_kv"] == 110.0
    assert pd.isna(stored.bus.at[hv[0], "etrago_bus_id"])                     # the test district has no eTraGo bus
    linked, _ = electrical.build_stored_net(design, 2, unit_mva, dataclasses.replace(inp, hv_bus_id=33316))
    assert linked.bus.at[hv[0], "etrago_bus_id"] == 33316
    assert stored.ext_grid["bus"].tolist() == [hv[0]] and len(design.ext_grid) == 1   # design net unchanged
    std_name = stored.trafo.at[stored.trafo.index[0], "std_type"]
    assert std_name in stored.std_types["trafo"]
    # a plain power flow of the JSON round trip reproduces the stored load case without a controller
    again = pp.from_json_string(pp.to_json(stored))
    pp.runpp(again)
    assert again.res_bus.at[busbar, "vm_pu"] == pytest.approx(stored.res_bus.at[busbar, "vm_pu"], abs=1e-9)
    # the case logic works on the stored net (HV ext_grid untouched)
    n1 = electrical.case_net(stored, "n1a", inp.params)
    assert n1.ext_grid["vm_pu"].tolist() == [1.0]
    nodes, lines = __import__("pylovo.analysis.mv_validation", fromlist=["net_to_tables"]).net_to_tables(stored)
    assert (nodes["kind"] == "busbar").sum() == 1 and nodes.loc[nodes["kind"] == "busbar", "n_transformers"].item() == 2


# ---------------------------------------------------------------------------------------------- budgets
def _budget_district(lv=None, **station_values):
    b = Builder(lv=lv)
    b.ring(2, **station_values)
    inp, plan = b.build()
    return inp, plan, plan.rings[0].members


def _budget_cases(vm_load: dict) -> dict:
    return {"load": _result("load", {BUSBAR: 1.02, **vm_load}),
            "n1a": _result("n1a", {BUSBAR: 1.02, **vm_load}),
            "n1b": _result("n1b", {BUSBAR: 1.02, **vm_load})}


def _margins(inp, budget_key, cases):
    """Brute force of the tap rule for one station (reference implementation of DESIGN §5.6)."""
    p = inp.params
    row = inp.nodes.loc[budget_key]
    lv_ref = inp.lv.lv_reference_voltage_pu
    taps = np.arange(p.mvlv_tap_min, p.mvlv_tap_max + 1)
    k = 1 / (1 + taps * p.mvlv_tap_step_percent / 100)
    d_t = row["lv_design_source_vm_pu"] - lv_ref
    lv_bus = (cases["load"].vm_pu[budget_key] - p.busbar_band_pu) * k - d_t
    return taps, lv_bus - (lv_ref - p.lv_design_tolerance_pu), lv_bus


def test_station_budget_near_busbar_keeps_tap_zero():
    inp, plan, (s1, s2) = _budget_district()
    cases = _budget_cases({s1: 1.019, s2: 1.0185})
    budgets = electrical.station_budgets(inp, plan, cases)
    assert set(budgets) == {s1, s2}
    budget = budgets[s1]
    assert budget.tap_pos == 0 and not budget.band_conflict and not budget.tap_limit
    taps, low, lv_bus = _margins(inp, s1, cases)
    assert low[list(taps).index(0)] >= 0
    assert budget.lv_busbar_vm_pu == pytest.approx(lv_bus[list(taps).index(0)])
    assert budget.lv_min_vm_pu == pytest.approx(budget.lv_busbar_vm_pu - 0.05)
    assert budget.vm_load_pu == 1.019 and budget.vm_n1_pu == 1.019
    assert math.isnan(budget.vm_feed_in_pu) and math.isnan(budget.lv_busbar_vm_feed_in_pu)   # no feed-in case


def test_station_budget_far_station_raises_the_tap_up_to_the_tap_limit():
    inp, plan, (s1, s2) = _budget_district(lv_design_source_vm_pu=0.985)
    cases = _budget_cases({s1: 0.973, s2: 0.972})
    budget = electrical.station_budgets(inp, plan, cases)[s1]
    taps, low, lv_bus = _margins(inp, s1, cases)
    best = list(taps).index(budget.tap_pos)
    feasible = [int(t) for t, margin in zip(taps, low) if margin >= 0]
    assert budget.tap_pos == min(feasible, key=lambda t: (abs(t), t)) == -2    # tap 0 and -1 miss the design point
    assert not budget.band_conflict and not budget.tap_limit
    assert budget.lv_busbar_vm_pu == pytest.approx(lv_bus[best])
    # a station that needs more than the tap range at the design point hits the tap limit
    cases = _budget_cases({s1: 0.935, s2: 0.935})
    budget = electrical.station_budgets(inp, plan, cases)[s1]
    assert budget.tap_limit and budget.band_conflict and budget.tap_pos == -2


def test_station_drop_ratio_of_an_upsized_station():
    sin = math.sqrt(1 - 0.95 ** 2)
    expected = ((1.206 * 0.95 + math.sqrt(36 - 1.206 ** 2) * sin) / 630) / (
        (1.425 * 0.95 + math.sqrt(36 - 1.425 ** 2) * sin) / 400)
    assert electrical.station_drop_ratio(400.0, 630.0, 0.95) == pytest.approx(expected) == pytest.approx(0.596, abs=1e-3)
    assert electrical.station_drop_ratio(630.0, 1260.0, 0.95) == pytest.approx(0.5)   # two units of the same type
    for lv_sn, sn in ((400.0, 400.0), (math.nan, 630.0), (0.0, 630.0), (400.0, math.nan)):
        assert electrical.station_drop_ratio(lv_sn, sn, 0.95) == 1.0


def test_station_budget_of_an_upsized_station_scales_the_transformer_drop():
    """A station upsized in the MV model (sn_kva 630 for an LV grid with 400 kVA) keeps the stored requirement of
    its LV grid, but its transformer drop at the LV design load shrinks: the LV busbar is higher."""
    inp, plan, (s1, s2) = _budget_district(lv_design_source_vm_pu=0.975)
    cases = _budget_cases({s1: 1.0, s2: 1.0})
    before = electrical.station_budgets(inp, plan, cases)[s1]
    inp.nodes.loc[s1, ["sn_kva", "lv_sn_kva"]] = [630.0, 400.0]
    after = electrical.station_budgets(inp, plan, cases)[s1]
    ratio = electrical.station_drop_ratio(400.0, 630.0, inp.lv.power_factor)
    assert after.tap_pos == before.tap_pos == 0
    assert after.lv_busbar_vm_pu - before.lv_busbar_vm_pu == pytest.approx((0.975 - 0.96) * (1 - ratio))


def test_station_budget_skips_and_flags():
    inp, plan, (s1, s2) = _budget_district()
    inp.nodes.loc[s2, "lv_design_source_vm_pu"] = math.nan
    cases = _budget_cases({s1: 1.019, s2: 1.0185})
    budgets = electrical.station_budgets(inp, plan, cases)
    assert set(budgets) == {s1}
    flags = electrical.node_flags(inp, plan, cases, budgets)
    assert flags.get(s2) == ["no_lv_requirement"] and s1 not in flags
    # with the load case only: the same tap, no (n-1) voltage
    partial = {"load": cases["load"]}
    budget = electrical.station_budgets(inp, plan, partial)[s1]
    assert budget.tap_pos == 0 and math.isnan(budget.vm_n1_pu)
    # LV versions without a reference voltage get no budgets
    inp_none, plan_none, (t1, _) = _budget_district(lv=_lv(None))
    cases = _budget_cases({t1: 1.019})
    assert electrical.station_budgets(inp_none, plan_none, cases) == {}
    assert electrical.node_flags(inp_none, plan_none, cases, {})[t1] == ["no_lv_reference"]


def test_node_flags_n1_security_and_unsupplied():
    b, inp, plan, ids = _full_district()
    cases = {"load": _result("load", {BUSBAR: 1.02, ids["stub"]: math.nan})}
    flags = electrical.node_flags(inp, plan, cases, {})
    assert "unsupplied" in flags[ids["stub"]] and "not_n1_secure" in flags[ids["stub"]]
    assert "not_n1_secure" not in flags.get(ids["customer"], [])
    assert ids["stub"] not in electrical.n1_nodes(inp, plan) and ids["joint"] in electrical.n1_nodes(inp, plan)


# ---------------------------------------------------------------------------------------------- mv_oberrhein
@pytest.mark.filterwarnings("ignore:tap_dependency_table is missing:DeprecationWarning")
def test_mv_oberrhein_cases_reproduce_the_pandapower_load_case():
    import pandapower.networks as pn
    import pandapower.topology as top

    net = pn.mv_oberrhein()
    params = MvParameters()
    load = electrical.case_net(net, "load", params)
    pp.runpp(load)
    vm = load.res_bus.vm_pu[load.bus.vn_kv < 60]
    assert vm.min() == pytest.approx(0.9756, abs=1e-3) and vm.max() == pytest.approx(1.0288, abs=1e-3)
    assert load.ext_grid["vm_pu"].tolist() == net.ext_grid["vm_pu"].tolist()   # HV slack kept
    graph = top.create_nxgraph(load, respect_switches=True, include_trafos=False)
    # NOPs open: radial (a forest: edges = nodes - components)
    assert graph.number_of_edges() == graph.number_of_nodes() - nx.number_connected_components(graph)
    closed = top.create_nxgraph(electrical.case_net(net, "load", params), respect_switches=False,
                                include_trafos=False)
    assert closed.number_of_edges() > closed.number_of_nodes() - nx.number_connected_components(closed)
    for case in ("n1a", "n1b"):
        current = electrical.case_net(net, case, params)
        assert electrical._runpp(current), case


def test_end_to_end_design_net_is_deterministic():
    b, inp, plan, _ = _full_district()
    first = electrical.run_cases(electrical.build_design_net(inp, plan), inp, plan)
    second = electrical.run_cases(electrical.build_design_net(inp, plan), inp, plan)
    for case in first:
        pd.testing.assert_series_equal(first[case].vm_pu, second[case].vm_pu)


def test_electrical_cases_ignore_stored_generation():
    b = Builder()
    b.ring(2, gen_solar_kw=250.0)
    inp, plan = b.build()
    net = electrical.build_design_net(inp, plan)
    assert net.sgen.empty
    cases = electrical.run_cases(net, inp, plan)
    assert tuple(cases) == ("load", "n1a", "n1b")
    # static generators of a net (e.g. of merged LV nets) are switched off in every case
    pp.create_sgen(net, int(net.bus.index[-1]), p_mw=0.5)
    for case in cases:
        assert (electrical.case_net(net, case, inp.params).sgen["scaling"] == 0).all(), case
    with pytest.raises(ValueError, match="unknown MV case"):
        electrical.case_net(net, "feed_in_pv", inp.params)


def test_unconnected_station_is_unsupplied_not_a_crash():
    b = Builder()
    station = b.add_node(_station(X0 + 800.0, Y0, 77))
    b.role[station] = "ring"
    inp, plan = b.build()
    net = electrical.build_design_net(inp, plan)
    assert len(net.bus) == 2 and len(net.line) == 0 and len(net.load) == 1
    cases = electrical.run_cases(net, inp, plan)
    assert all(result.converged for result in cases.values())
    assert math.isnan(cases["load"].vm_pu[station])
    assert electrical.check(cases, inp, plan) == []
    assert electrical.node_flags(inp, plan, cases, electrical.station_budgets(inp, plan, cases))[station] == [
        "unsupplied", "not_n1_secure"]
    assert electrical.station_budgets(inp, plan, cases) == {}
    units, unit_mva, sizing = electrical.size_substation(inp, plan)
    stored, tap = electrical.build_stored_net(net, units, unit_mva, inp)
    assert (units, unit_mva, sizing) == (2, 25.0, "sized") and abs(tap) <= 2


def test_repair_falls_back_to_exit_cables_when_a_split_changes_nothing(fake_topology):
    fake = fake_topology(moved=False)
    fake.split_ring = lambda inp, rg, model, ev, plan, ring_id: (fake.calls.append(("split_ring", ring_id)), plan)[1]
    b = Builder()
    r = b.ring(6, set_exits=True)
    inp, plan = b.build()
    low = _violation("load", "voltage_low", "bus", plan.rings[0].members[-1], r, 0.96, 0.97)
    plan, actions = electrical.repair(inp, None, None, None, plan, [low])
    assert [c[0] for c in fake.calls] == ["move_nop", "split_ring", "add_parallel"]
    assert [a["action"] for a in actions] == ["parallel"] and plan.diagnostics["repair_stage"] == {"1": 3}
