"""Tests of the MV topology build (pylovo.mv_grid.topology), DB-free.

The ring evaluator (rings.check_ring), the coincidence law and the solver are replaced by small fakes that follow
their contracts (DESIGN §4.3, §9.3 WP3/WP4/WP5), so these tests do not depend on those packages.
"""
from __future__ import annotations

import dataclasses
import math
import types

import numpy as np
import pandas as pd
import pytest
from scipy.sparse import csr_matrix
from shapely.geometry import LineString, Point

from pylovo.mv_grid import topology
from pylovo.mv_grid.model import NODE_COLUMNS, DistrictInput, EdgeSet, LineType, RingCheck, RunGraph, SolverResult
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

CABLE = LineType("NA2XS2Y 1x240 RM/25 12/20 kV", "cable", 20.0, 0.122, 0.112, 304.0, 0.421)
OVERHEAD = LineType("122-AL1/20-ST1A 20.0", "overhead", 20.0, 0.2376, 0.344, 10.3, 0.41)
LV = LvVersionParameters(version_id="t", sim_factor={"Residential": 0.07, "Commercial": 0.5, "Public": 0.6,
                                                     "Unknown": 0.6},
                         power_factor=0.95, mv_direct_threshold_kw=300.0, residential_only=False,
                         lv_reference_voltage_pu=0.96, use_dso=False, use_open=False, use_manual=False,
                         electrical_backend="pandapower")
BUSBAR = 1


# ============================================================================================ fakes of WP3, WP4, WP5
def fake_coincident_kw(installed, units, g):
    installed = np.asarray(installed, dtype=float)
    units = np.asarray(units, dtype=float)
    safe = np.where(units > 0, units, 1.0)
    term = installed * (g + (1.0 - g) * safe ** -0.75)
    return np.where((installed > 0) & (units > 0), term, 0.0).sum(axis=-1)


def fake_check_ring(installed, units, fixed_kw, section_m, section_r, section_x, section_imax, member, switchgear,
                    g, power_factor, vn_kv, limits):
    """Brute-force version of the ring check of DESIGN §4.3 (positions 1..k, sections s_0..s_k)."""
    k = len(fixed_kw)
    tan = math.tan(math.acos(power_factor))
    w = (section_r + section_x * tan) * section_m / 1000.0 * 1000.0 / (vn_kv * 1000.0) ** 2

    def load(a, b):
        if b < a:
            return 0.0
        return float(fake_coincident_kw(installed[a - 1:b].sum(axis=0), units[a - 1:b].sum(axis=0), g)
                     + fixed_kw[a - 1:b].sum())

    def amps(p_kw, j):
        return p_kw / (math.sqrt(3.0) * vn_kv * power_factor) / 1000.0 / section_imax[j]

    drop_n1a = sum(w[j] * load(j + 1, k) for j in range(k))
    drop_n1b = sum(w[j] * load(1, j) for j in range(1, k + 1))
    util_n1 = max([amps(load(j + 1, k), j) for j in range(k)] + [amps(load(1, j), j) for j in range(1, k + 1)])
    best = None
    for m in ([1] if k == 1 else range(1, k)):
        if not (switchgear[m - 1] or (m < k and switchgear[m])):
            continue
        da = sum(w[j] * load(j + 1, m) for j in range(m))
        db = sum(w[j] * load(m + 1, j) for j in range(m + 1, k + 1))
        half_a, half_b = float(section_m[:m].sum()), float(section_m[m + 1:].sum())
        util = max([amps(load(j + 1, m), j) for j in range(m)]
                   + [amps(load(m + 1, j), j) for j in range(m + 1, k + 1)] + [0.0])
        ok = (max(half_a, half_b) <= limits.max_half_m and max(da, db) <= limits.max_drop
              and util <= limits.utilization)
        rank = (not ok, max(da, db), abs(load(1, m) - load(m + 1, k)), m)
        if best is None or rank < best[0]:
            best = (rank, m, da, db, half_a, half_b, util, ok)
    if best is None:
        return RingCheck(False, 0, 0, float(section_m.sum()), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, "no_nop")
    _, m, da, db, half_a, half_b, util, ok = best
    nop = m if switchgear[m - 1] else m + 1
    reason = None
    if int(member.sum()) > limits.max_members:
        reason = "count"
    elif not ok:
        reason = "drop"
    elif max(drop_n1a, drop_n1b) > limits.max_drop_n1:
        reason = "n1_drop"
    elif util_n1 > limits.n1_utilization:
        reason = "n1_current"
    return RingCheck(reason is None, m, nop, float(section_m.sum()), half_a, half_b, da, db,
                     max(drop_n1a, drop_n1b), util, util_n1, load(1, k), load(1, m), load(m + 1, k), reason)


def fake_solve(name, ev, dist_m, candidates, xy, *, rounds=50, neighbours=15, time_guard_s=120.0):
    routes, current = [], []
    for c in sorted(int(c) for c in candidates):
        trial = current + [c]
        if current and not ev.evaluate(trial).feasible:
            routes.append(current)
            current = [c]
        else:
            current = trial
    if current:
        routes.append(current)
    return SolverResult(routes, 0.0, 0.0, 0, 0, False, 0.0)


class FakeEvaluator:
    """RingEvaluatorProtocol with a member cap only."""

    def __init__(self, cap: int = 20) -> None:
        self.cap = cap

    def member_count(self, seq):
        return len(seq)

    def ring_length(self, seq):
        return 0.0

    def evaluate(self, seq):
        ok = len(seq) <= self.cap
        return RingCheck(ok, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, None if ok else "count")


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(topology, "_rings", lambda: types.SimpleNamespace(check_ring=fake_check_ring))
    monkeypatch.setattr(topology, "_coincidence", lambda: types.SimpleNamespace(coincident_kw=fake_coincident_kw))
    monkeypatch.setattr(topology, "_solvers", lambda: types.SimpleNamespace(solve=fake_solve))
    topology._CONTEXT.clear()
    yield
    topology._CONTEXT.clear()


# ============================================================================================ synthetic districts
def make_params(**changes) -> MvParameters:
    """The parameters of a test district; the tests of the straight-span rule run with MV_OVERHEAD_LINES rural."""
    params = MvParameters().replace(**{"overhead_lines": "rural", **changes})
    params._types.update({("cable", 20.0): CABLE, ("overhead", 20.0): OVERHEAD})       # the 20 kV district types
    return params


class District:
    """A synthetic district: streets (straight edges), nodes at street vertices or with connectors."""

    def __init__(self) -> None:
        self.xy: list[tuple[float, float]] = []
        self.edges: list[tuple[int, int]] = []
        self.edge_settlement: list[int] = []
        self.edge_overhead: list[bool] = []
        self.connectors: list[tuple[int, int]] = []
        self.nodes: list[dict] = []

    def vertex(self, x: float, y: float) -> int:
        point = (float(x), float(y))
        if point not in self.xy:
            self.xy.append(point)
        return self.xy.index(point)

    def street(self, *points, settlement: int | None = None, overhead: bool = False) -> None:
        """Edges through the points; ``overhead``: an OSM MV line instead of a street."""
        ids = [self.vertex(*p) for p in points]
        for a, b in zip(ids[:-1], ids[1:]):
            self.edges.append((a, b))
            self.edge_settlement.append(-1 if settlement is None else settlement)
            self.edge_overhead.append(overhead)

    def node(self, key: int, kind: str, x: float, y: float, *, hint: str = "ring", urban: bool = False,
             settlement: int | None = None, inst_res: float = 150.0, units_res: float = 50.0,
             p_fixed_kw: float = 0.0, gen_kw: float = 0.0, offset: tuple[float, float] | None = None) -> None:
        self.nodes.append(dict(key=key, kind=kind, hint=hint, x=float(x), y=float(y), urban=urban,
                               settlement=settlement, inst_res=inst_res if kind == "station" else 0.0,
                               units_res=units_res if kind == "station" else 0.0, p_fixed_kw=p_fixed_kw,
                               gen_kw=gen_kw, offset=offset))

    def build(self, **param_changes) -> tuple[DistrictInput, RunGraph]:
        terminal_vertex = []
        for n in self.nodes:
            if n["offset"] is None:
                terminal_vertex.append(self.vertex(n["x"], n["y"]))
            else:                                   # off-street node: own vertex plus a straight connector
                own = len(self.xy)
                self.xy.append((n["x"], n["y"]))
                self.connectors.append((own, self.vertex(*n["offset"])))
                terminal_vertex.append(own)
        xy = np.asarray(self.xy, dtype=float)
        rows = []
        for i, (a, b) in enumerate(self.edges + self.connectors):
            settlement = self.edge_settlement[i] if i < len(self.edges) else -1
            rows.append(dict(piece_id=i, u=a, v=b, length_m=float(np.hypot(*(xy[a] - xy[b]))),
                             edge_id=i if i < len(self.edges) else -1, f0=0.0, f1=1.0,
                             kind="street" if i < len(self.edges) else "connector", in_settlement=settlement >= 0,
                             settlement_id=settlement,
                             overhead=self.edge_overhead[i] if i < len(self.edges) else False))
        pieces = pd.DataFrame(rows, columns=["piece_id", "u", "v", "length_m", "edge_id", "f0", "f1", "kind",
                                             "in_settlement", "settlement_id", "overhead"])
        n = len(xy)
        csr = csr_matrix((np.r_[pieces.length_m, pieces.length_m], (np.r_[pieces.u, pieces.v],
                                                                    np.r_[pieces.v, pieces.u])), shape=(n, n))
        pair_piece = {(min(a, b), max(a, b)): int(i) for i, a, b in zip(pieces.piece_id, pieces.u, pieces.v)}
        rg = RunGraph(csr=csr, n_vertices=n, vertex_xy=xy, terminal_vertex=np.asarray(terminal_vertex),
                      pieces=pieces, pair_piece=pair_piece, n_bridges=0,
                      bridged_rows=np.zeros(len(self.nodes), dtype=bool))
        geoms = {i: LineString([xy[a], xy[b]]) for i, (a, b) in enumerate(self.edges)}
        self.geometry_calls = []

        def edge_geometry(ids):
            self.geometry_calls.append(list(ids))
            return {int(i): geoms[int(i)] for i in ids}

        records = []
        for nd in self.nodes:
            rec = {c: 0.0 for c in NODE_COLUMNS}
            rec.update(kind=nd["kind"], connection_hint=nd["hint"], x=nd["x"], y=nd["y"], attach_edge_id=-1,
                       attach_fraction=0.0, attach_offset_m=0.0, bridged=False, settlement_id=nd["settlement"],
                       urban=nd["urban"], plz=12345, plz_settlement_type=3, inst_res=nd["inst_res"],
                       units_res=nd["units_res"], p_fixed_kw=nd["p_fixed_kw"], gen_solar_kw=nd["gen_kw"],
                       gen_level="mv" if nd["kind"] == "generator" else "", sn_kva=400.0, grid_result_id=None,
                       lv_design_source_vm_pu=0.98, lv_max_drop_pu=0.03)
            records.append(rec)
        frame = pd.DataFrame(records, columns=list(NODE_COLUMNS),
                             index=pd.Index([nd["key"] for nd in self.nodes], name="mv_node_id"))
        edges = EdgeSet(*(np.zeros(0, dtype=np.int64) for _ in range(3)), np.zeros(0), np.zeros(0, dtype=bool),
                        np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros((0, 2)))
        inp = DistrictInput(mv_grid_id=1, substation_id=1, vn_kv=20.0, nodes=frame, edges=edges,
                            params=make_params(**param_changes), lv=LV, known_units_mva=(),
                            edge_geometry=edge_geometry)
        return inp, rg


def plan_of(district: District, routes: list[list[int]], *, cap: int = 20, satellites: bool = True,
            **param_changes):
    """Build the plan of a district; ``routes`` hold node keys. ``satellites=False`` makes every satellite of the
    model an ordinary ring candidate (for tests of the ring, stub and radial rules)."""
    inp, rg = district.build(**param_changes)
    terminals = topology.terminal_rows(inp)
    model = topology.distance_model(inp, rg, terminals)
    if not satellites:
        model = dataclasses.replace(model, candidates=np.union1d(model.candidates, np.flatnonzero(model.satellite)),
                                    satellite=np.zeros_like(model.satellite))
    keys = list(inp.nodes.index[terminals])
    route_t = [[keys.index(k) for k in route] for route in routes]
    ev = FakeEvaluator(cap)
    plan = topology.build_plan(inp, rg, model, ev, route_t)
    return plan, (inp, rg, model, ev)


LOOP = [(0, 0), (500, 0), (1000, 0), (2000, 0), (2500, 0), (2600, 0), (3000, 0), (4000, 0), (4000, 500),
        (4000, 1000), (3000, 1000), (2000, 1000), (1000, 1000), (0, 1000), (0, 0)]
RING = [[10, 20, 30, 40, 50]]


def loop_district(side_m: float | None = 400.0, **d_kwargs) -> District:
    """A 4 km x 1 km street loop in one rural village (settlement 1) with the busbar at its south-west corner;
    A (10), C (30), E (40), F (50) on the loop; D (20) at the end of a side street leaving the loop at
    J = (2000, 0) to the south."""
    d = District()
    d.street(*LOOP, settlement=1)
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar", settlement=1)
    d.node(10, "station", 1000, 0, settlement=1)
    if side_m is not None:
        d.street((2000, 0), (2000, -side_m), settlement=1)
        d.node(20, "station", 2000, -side_m, **{"settlement": 1, **d_kwargs})
    d.node(30, "station", 3000, 0, settlement=1)
    d.node(40, "station", 3000, 1000, settlement=1)
    d.node(50, "station", 1000, 1000, settlement=1)
    return d


def line_between(plan, a, b):
    return [ln for ln in plan.lines if {ln.from_key, ln.to_key} == {a, b}]


def ring_of(plan, key):
    return next(r for r in plan.rings if key in r.members)


def node_point(plan, inp, key):
    if key < 0:
        return np.asarray(plan.joints[key])
    return inp.nodes.loc[key, ["x", "y"]].to_numpy(dtype=float)


def assert_plan_valid(plan, inp) -> None:
    """Roles, open points and geometries of a plan (the WP9 acceptance rules)."""
    keys = [int(k) for k in inp.nodes.index]
    assert plan.node_role[BUSBAR] == "busbar"
    for key in keys + list(plan.joints):
        assert key in plan.node_role, key
    assert set(plan.node_role) == set(keys) | set(plan.joints)
    # each connected non-busbar node is in exactly one place: a feeder structure or below one
    members = [k for r in plan.rings for k in r.members if k > 0]
    placed = members + [k for k, role in plan.node_role.items() if role in ("stub", "tee") and k not in members]
    connected = [k for k, role in plan.node_role.items() if role not in ("busbar", "joint", "excluded")]
    assert sorted(placed) == sorted(connected)
    assert len(placed) == len(set(placed))
    for ring in plan.rings:
        if ring.kind != "ring":
            continue
        sections = [ln for ln in plan.lines if ln.ring_id == ring.ring_id and ln.role == "ring"]
        opened = [ln for ln in sections if ln.normally_open]
        assert len(opened) == 1, ring.ring_id
        nop = opened[0].open_at_key
        assert nop == ring.nop_key and nop in (opened[0].from_key, opened[0].to_key)
        assert nop > 0 and inp.nodes.loc[nop, "kind"] in ("station", "customer", "generator")
        assert sum(ln.exit == "a" for ln in sections) == 1 and sum(ln.exit == "b" for ln in sections) == 1
    for ln in plan.lines:
        geom = ln.geometry
        assert isinstance(geom, LineString) and geom.is_valid and geom.length > 0
        start, end = np.asarray(geom.coords[0]), np.asarray(geom.coords[-1])
        assert np.hypot(*(start - node_point(plan, inp, ln.from_key))) <= 0.5, ln
        assert np.hypot(*(end - node_point(plan, inp, ln.to_key))) <= 0.5, ln
        assert ln.length_m >= 1.0
        assert ln.cable_m + ln.overhead_m == pytest.approx(ln.length_m)
        if ln.pieces:
            assert geom.length == pytest.approx(ln.length_m, abs=1.0)
        if ln.std_type == CABLE.name:
            assert ln.kind == "cable" and ln.overhead_m == 0.0
        elif ln.std_type == OVERHEAD.name:
            assert ln.kind == "overhead" and ln.cable_m == 0.0
        else:
            assert ln.kind == "mixed" and ln.std_type == f"{CABLE.name} + {OVERHEAD.name}"


# ============================================================================================ terminals and model
def test_terminal_rows_start_with_the_busbar_and_skip_excluded_nodes():
    d = District()
    d.street((0, 0), (1000, 0))
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    d.node(9, "station", 1000, 0)
    d.node(4, "customer", 500, 0)
    d.node(7, "customer", 0, 0, hint="excluded", p_fixed_kw=30000.0)
    d.node(5, "generator", 1000, 0, hint="tee", gen_kw=500.0)
    inp, _ = d.build()
    rows = topology.terminal_rows(inp)
    assert list(inp.nodes.index[rows]) == [BUSBAR, 4, 5, 9]


def test_terminal_rows_need_the_busbar_first():
    d = District()
    d.street((0, 0), (1000, 0))
    d.node(9, "station", 1000, 0)
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    inp, _ = d.build()
    with pytest.raises(ValueError, match="busbar"):
        topology.terminal_rows(inp)


def detour_district(**node_kwargs) -> District:
    """P and Q are 1 km apart, but the street between them is 5 km long; R is straight down the street from P."""
    d = District()
    d.street((0, 0), (1000, 0), (3000, 0), (3000, 1000), (1000, 1000))
    d.street((1000, 0), (1000, -1000))
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    d.node(2, "station", 1000, 0, **node_kwargs.get("p", {}))
    d.node(3, "station", 1000, 1000, **node_kwargs.get("q", {}))
    d.node(4, "station", 1000, -1000, **node_kwargs.get("r", {}))
    return d


def model_of(district: District, **params):
    inp, rg = district.build(**params)
    return topology.distance_model(inp, rg, topology.terminal_rows(inp))


def test_overhead_links_between_rural_settlements_take_the_shorter_straight_span():
    model = model_of(detour_district(p=dict(settlement=1), q=dict(settlement=2), r=dict(settlement=3)))
    assert model.street_m[1, 2] == pytest.approx(5000.0)
    assert model.straight_m[1, 2] == pytest.approx(1000.0)
    assert model.overhead[1, 2] and model.straight[1, 2]
    assert model.dist_m[1, 2] == pytest.approx(1100.0)
    # a near-straight street keeps the street route, but the link is still an overhead line
    assert model.overhead[1, 3] and not model.straight[1, 3]
    assert model.dist_m[1, 3] == pytest.approx(1000.0)
    assert list(model.candidates) == [1, 2, 3]
    assert not model.overhead.diagonal().any() and not model.straight.diagonal().any()


def test_cable_inside_a_settlement_and_between_urban_areas():
    same = model_of(detour_district(p=dict(settlement=1), q=dict(settlement=1)))
    assert not same.overhead[1, 2] and not same.straight[1, 2]
    assert same.dist_m[1, 2] == pytest.approx(5000.0)
    urban = model_of(detour_district(p=dict(settlement=1, urban=True), q=dict(settlement=2, urban=True)))
    assert not urban.overhead[1, 2]
    into_town = model_of(detour_district(p=dict(settlement=1, urban=True), q=dict(settlement=2)))
    assert not into_town.overhead[1, 2] and not into_town.straight[1, 2]     # one urban end: cable along streets
    assert into_town.dist_m[1, 2] == pytest.approx(5000.0)
    open_field = model_of(detour_district())                 # two open-field ends count as different settlements
    assert open_field.overhead[1, 2] and open_field.straight[1, 2]
    no_overhead = model_of(detour_district(p=dict(settlement=1), q=dict(settlement=2)), overhead_lines="none")
    assert not no_overhead.overhead.any() and not no_overhead.straight.any()


def test_busbar_legs_are_cables_along_the_streets():
    d = detour_district(p=dict(settlement=1), q=dict(settlement=2), r=dict(settlement=3))
    model = model_of(d)
    assert not model.overhead[0].any() and not model.overhead[:, 0].any()
    assert not model.straight[0].any() and not model.straight[:, 0].any()
    # Q lies 1.4 km from the busbar in a straight line, 6 km along the streets: the leg takes the streets
    assert model.dist_m[0, 2] == pytest.approx(model.street_m[0, 2]) == pytest.approx(6000.0)
    assert model.overhead[1, 2] and model.straight[1, 2]           # between two settlements still a straight span
    crowfly = model_of(d, routing="crowfly")
    assert crowfly.straight[0, 2] and not crowfly.overhead[0, 2]     # crowfly: a stretched span, but a cable
    assert crowfly.dist_m[0, 2] == pytest.approx(1.3 * np.hypot(1000.0, 1000.0))


def satellite_district() -> District:
    """The loop village of :func:`loop_district` plus two farms in open field: F (80) 300 m south of the loop
    street at (2500, 0), G (81) 2.7 km further down the same field path."""
    d = loop_district()
    d.street((2500, 0), (2500, -300), (2500, -3000))
    d.node(80, "station", 2500, -300)
    d.node(81, "station", 2500, -3000)
    return d


def test_isolated_rural_stations_are_satellites_and_no_ring_candidates():
    model = model_of(satellite_district())
    inp, _ = satellite_district().build()
    keys = list(inp.nodes.index[model.terminals])
    assert [keys[t] for t in np.flatnonzero(model.satellite)] == [80, 81]
    assert [keys[t] for t in model.candidates] == [10, 20, 30, 40, 50]
    off = model_of(satellite_district(), stubs="none")
    assert not off.satellite.any() and [keys[t] for t in off.candidates] == [10, 20, 30, 40, 50, 80, 81]
    town = satellite_district()
    for node in town.nodes:
        if node["key"] in (80, 81):
            node["urban"] = True                                    # urban stations are never satellites
    assert not model_of(town).satellite.any()
    alone = detour_district(p=dict(settlement=1), q=dict(settlement=2), r=dict(settlement=3))
    assert not model_of(alone).satellite.any()                       # all would be satellites: none is


def test_satellites_hang_on_stubs_or_go_into_the_cheapest_ring():
    plan, (inp, *_) = plan_of(satellite_district(), RING)
    assert_plan_valid(plan, inp)
    assert plan.node_role[80] == "stub"
    joint = plan.stub_parent[80]
    assert joint < 0 and tuple(plan.joints[joint]) == (2500.0, 0.0)  # T-off where the field path leaves the loop
    stub = line_between(plan, joint, 80)[0]
    assert stub.role == "stub" and stub.length_m == pytest.approx(300.0)
    # G is 2.7 km from the nearest line, beyond stub_max_length_m: it joins the ring where it costs the least
    assert plan.node_role[81] == "ring" and 81 in ring_of(plan, 10).members
    assert "satellite_in_ring" in plan.diagnostics["node_flags"][81]
    assert plan.diagnostics["satellites"] == {"stub": 1, "ring": 1, "own_ring": 0}


def test_a_satellite_without_a_ring_gets_its_own():
    # the village ring is full: F can neither hang on it nor join it, so it gets its own ring, which G then joins
    plan, (inp, *_) = plan_of(satellite_district(), RING, max_stations_per_ring=3, rural_ring_factor=1.0, cap=3)
    assert_plan_valid(plan, inp)
    assert "satellite_own_ring" in plan.diagnostics["node_flags"][80]
    assert sorted(ring_of(plan, 80).members) == [80, 81] and plan.node_role[81] == "ring"
    assert "satellite_in_ring" in plan.diagnostics["node_flags"][81]
    assert plan.diagnostics["satellites"] == {"stub": 0, "ring": 1, "own_ring": 1}


def osm_district() -> District:
    """P (2) and Q (3) in two villages; the street between them is 5 km long, an OSM MV line runs straight from P to
    Q (1 km); the busbar is 1 km west of P."""
    d = District()
    d.street((0, 0), (1000, 0), (3000, 0), (3000, 1000), (1000, 1000))
    d.street((1000, 0), (1000, 1000), overhead=True)
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    d.node(2, "station", 1000, 0, settlement=1)
    d.node(3, "station", 1000, 1000, settlement=2)
    return d


def test_osm_mode_takes_the_overhead_lines_of_the_graph_and_no_straight_spans():
    model = model_of(osm_district(), overhead_lines="osm")
    assert not model.straight.any()
    np.testing.assert_allclose(model.dist_m, model.street_m)
    assert model.dist_m[1, 2] == pytest.approx(1000.0) and model.overhead_m[1, 2] == pytest.approx(1000.0)
    # the busbar leg to Q: 1 km of street cable to P, then 1 km on the OSM line
    assert model.dist_m[0, 2] == pytest.approx(2000.0) and model.overhead_m[0, 2] == pytest.approx(1000.0)
    assert model.overhead_m[0, 1] == 0.0 and model.overhead[1, 2] and not model.overhead[0, 1]
    assert model_of(osm_district()).overhead_m is None                # rural: no overhead lengths


def test_osm_mode_plans_cable_overhead_and_mixed_lines():
    plan, (inp, *_) = plan_of(osm_district(), [[2, 3]], overhead_lines="osm", stubs="none")
    assert_plan_valid(plan, inp)
    leg_a, = line_between(plan, BUSBAR, 2)
    span, = line_between(plan, 2, 3)
    leg_b, = line_between(plan, 3, BUSBAR)
    assert (leg_a.kind, leg_a.overhead_m) == ("cable", 0.0) and leg_a.length_m == pytest.approx(1000.0)
    assert span.kind == "overhead" and span.overhead_m == pytest.approx(1000.0) and span.cable_m == 0.0
    assert leg_b.kind == "mixed" and leg_b.cable_m == pytest.approx(1000.0) and leg_b.overhead_m == pytest.approx(1000.0)
    assert leg_b.std_type == f"{CABLE.name} + {OVERHEAD.name}" and span.std_type == OVERHEAD.name


def test_crowfly_routing_uses_stretched_straight_spans():
    model = model_of(detour_district(p=dict(settlement=1), q=dict(settlement=1)), routing="crowfly")
    assert model.dist_m[1, 2] == pytest.approx(1300.0)
    assert model.straight[1, 2] and not model.overhead[1, 2]
    assert np.isfinite(model.street_m).all()


def test_unreachable_terminals_keep_inf():
    d = District()
    d.street((0, 0), (1000, 0))
    d.street((5000, 0), (6000, 0))
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    d.node(2, "station", 1000, 0, settlement=1, urban=True)
    d.node(3, "station", 6000, 0, settlement=2, urban=True)
    model = model_of(d)
    assert math.isinf(model.street_m[1, 2]) and math.isinf(model.dist_m[1, 2])
    assert model.street_m[0, 1] == pytest.approx(1000.0)


# ============================================================================================ rings and open points
def test_ring_plan_has_one_open_point_at_a_station_and_continuous_geometry():
    d = District()
    d.street((0, 0), (1000, 0), (2000, 0), (2000, 1000), (1000, 1000), (0, 1000), (0, 0))
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar", urban=True, settlement=1)
    for key, (x, y) in zip((11, 12, 13, 14), ((1000, 0), (2000, 0), (2000, 1000), (1000, 1000))):
        d.node(key, "station", x, y, urban=True, settlement=1)
    plan, (inp, *_) = plan_of(d, [[11, 12, 13, 14]])
    assert_plan_valid(plan, inp)
    ring = plan.rings[0]
    assert ring.kind == "ring" and ring.members == [11, 12, 13, 14]
    assert ring.nop_key in (12, 13)                         # voltage balance of a symmetric ring
    assert ring.check is not None and ring.check.feasible
    sections = [ln for ln in plan.lines if ln.role == "ring"]
    assert [(ln.from_key, ln.to_key) for ln in sections] == [(1, 11), (11, 12), (12, 13), (13, 14), (14, 1)]
    assert sections[0].exit == "a" and sections[-1].exit == "b"
    assert sum(ln.length_m for ln in sections) == pytest.approx(6000.0)
    assert {plan.node_feeder[11], plan.node_feeder[14]} == {ring.feeder_a, ring.feeder_b}
    assert all(ln.kind == "cable" and ln.std_type == CABLE.name for ln in plan.lines)
    assert plan.diagnostics["n_rings"] == 1 and plan.diagnostics["street_straight_ratio"] > 1.0
    assert len(d.geometry_calls) == 1                       # edge geometries fetched once


def test_single_station_ring_keeps_two_cables_and_opens_at_the_station():
    d = District()
    d.street((0, 0), (3000, 0))
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    d.node(5, "station", 3000, 0, urban=True, settlement=1)
    plan, (inp, *_) = plan_of(d, [[5]])
    assert_plan_valid(plan, inp)
    ring = plan.rings[0]
    assert ring.kind == "ring" and ring.members == [5] and ring.nop_key == 5
    assert len(plan.lines) == 2 and plan.lines[1].normally_open and plan.lines[1].open_at_key == 5


def test_unrouted_candidates_become_single_station_rings():
    plan, (inp, *_) = plan_of(loop_district(urban=True), [[10, 30, 40, 50]])
    assert_plan_valid(plan, inp)
    assert ring_of(plan, 20).members == [20]
    assert "unrouted" in plan.diagnostics["node_flags"][20]


def test_build_is_deterministic():
    first, _ = plan_of(loop_district(), RING)
    second, _ = plan_of(loop_district(), RING)
    assert [(ln.from_key, ln.to_key, ln.role, round(ln.length_m, 6)) for ln in first.lines] == \
        [(ln.from_key, ln.to_key, ln.role, round(ln.length_m, 6)) for ln in second.lines]
    assert first.node_role == second.node_role


# ============================================================================================ stubs
def test_a_rural_dead_end_station_becomes_a_stub_at_a_joint():
    plan, (inp, *_) = plan_of(loop_district(), RING)
    assert_plan_valid(plan, inp)
    assert plan.node_role[20] == "stub"
    joint = plan.stub_parent[20]
    assert joint < 0 and plan.node_role[joint] == "joint"
    assert plan.joints[joint] == pytest.approx((2000.0, 0.0))
    ring = ring_of(plan, 10)
    assert ring.members == [10, joint, 30, 40, 50]
    assert ring.nop_key > 0                                 # never the joint
    stub = line_between(plan, joint, 20)[0]
    assert stub.role == "stub" and stub.length_m == pytest.approx(400.0) and stub.kind == "cable"
    assert plan.node_feeder[20] == plan.node_feeder[joint]
    assert plan.node_ring[20] == ring.ring_id
    assert plan.diagnostics["n_stub_nodes"] == 1 and plan.diagnostics["n_joints"] == 1


@pytest.mark.parametrize("case", ["urban", "too_far", "too_close", "too_large", "stubs_off"])
def test_stub_rules_keep_the_station_looped_in(case):
    kwargs, params, side = {}, {}, 400.0
    if case == "urban":
        kwargs = dict(urban=True)
    elif case == "too_far":
        side = 2500.0                                       # spur above stub_max_length_m
    elif case == "too_close":
        side = 80.0                                         # spur below stub_min_offset_m
    elif case == "too_large":
        kwargs = dict(inst_res=0.0, units_res=0.0, p_fixed_kw=1200.0)
    else:
        params = dict(stubs="none")
    d = loop_district(side, **kwargs)
    if case == "too_large":
        d.nodes[2]["kind"] = "customer"                     # 1.2 MW / 0.95 > 1 MVA
    plan, (inp, *_) = plan_of(d, RING, **params)
    assert_plan_valid(plan, inp)
    assert plan.node_role[20] == "ring"
    assert ring_of(plan, 20).members == [10, 20, 30, 40, 50]
    assert not plan.joints




def test_a_station_on_the_way_stays_in_the_ring():
    d = loop_district()
    d.nodes[2].update(x=2000.0, y=0.0)                     # D on the loop street itself
    plan, (inp, *_) = plan_of(d, RING)
    assert_plan_valid(plan, inp)
    assert plan.node_role[20] == "ring" and not plan.joints


def tree_district(load_kw: float | None = None) -> District:
    """The loop with a side street J = (2000, 0) - K = (2000, -200) - D1 = (2000, -400) and a branch K - D2."""
    d = loop_district(side_m=None)
    d.street((2000, 0), (2000, -200), (2000, -400), settlement=1)
    d.street((2000, -200), (2300, -200), settlement=1)
    kind, kwargs = ("station", {}) if load_kw is None else ("customer", dict(p_fixed_kw=load_kw))
    d.node(21, kind, 2000, -400, settlement=1, **kwargs)                   # D1
    d.node(22, kind, 2300, -200, settlement=1, **kwargs)                   # D2
    return d


def test_stub_trees_hang_later_stubs_on_earlier_ones():
    """D2 leaves the ring first (larger gain), then D1 hangs on D2's stub at K (closer than the ring at J)."""
    plan, (inp, *_) = plan_of(tree_district(), [[10, 21, 22, 30, 40, 50]])
    assert_plan_valid(plan, inp)
    assert plan.node_role[21] == "stub" and plan.node_role[22] == "stub"
    k = plan.stub_parent[21]
    assert k == plan.stub_parent[22] and k < 0
    assert plan.joints[k] == pytest.approx((2000.0, -200.0))
    j = plan.stub_parent[k]
    assert j < 0 and plan.joints[j] == pytest.approx((2000.0, 0.0))
    assert ring_of(plan, 10).members == [10, j, 30, 40, 50]
    assert plan.node_role[k] == "joint" and k not in ring_of(plan, 10).members
    assert plan.diagnostics["stub_order"] == [22, 21]
    assert plan.node_feeder[21] == plan.node_feeder[j]


def test_stub_tree_load_limit_sends_the_second_stub_to_the_ring():
    plan, (inp, *_) = plan_of(tree_district(load_kw=600.0), [[10, 21, 22, 30, 40, 50]])
    assert_plan_valid(plan, inp)
    # together 1.26 MVA: D1 may not join D2's tree; 400 m from the ring it still leaves (saving 800 m)
    assert plan.node_role[22] == "stub" and plan.node_role[21] == "stub"
    members = ring_of(plan, 10).members
    assert plan.stub_parent[21] in members and plan.stub_parent[22] in members
    assert (2000.0, -200.0) not in [tuple(xy) for xy in plan.joints.values()]


# ============================================================================================ radials, dedicated
def radial_district() -> District:
    d = District()
    d.street((0, 0), (0, 800))
    d.street((0, 0), (0, -2500))
    d.street((0, 0), (900, 0))
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar")
    d.node(40, "station", 0, 800)                           # rural, 800 m: radial
    d.node(41, "station", 0, -2500)                         # rural, 2.5 km: two-cable ring
    d.node(42, "station", 900, 0, urban=True, settlement=1)  # urban: ring
    return d


def test_small_rural_single_station_rings_near_the_busbar_become_radials():
    plan, (inp, *_) = plan_of(radial_district(), [[40], [41], [42]], satellites=False)
    assert_plan_valid(plan, inp)
    radial = ring_of(plan, 40)
    assert radial.kind == "radial" and radial.nop_key is None and radial.feeder_b is None
    lines = line_between(plan, BUSBAR, 40)
    assert len(lines) == 1 and lines[0].role == "stub" and lines[0].exit is None
    assert plan.node_role[40] == "stub" and plan.stub_parent[40] == BUSBAR
    assert ring_of(plan, 41).kind == "ring" and len(line_between(plan, BUSBAR, 41)) == 2
    assert ring_of(plan, 42).kind == "ring"
    assert plan.diagnostics["n_radials"] == 1


@pytest.mark.parametrize("p_kw, parallel, overload", [(5000.0, 1, False), (12000.0, 2, False),
                                                      (20000.0, 2, True)])
def test_dedicated_customer_feeder_has_two_legs_sized_by_load(p_kw, parallel, overload):
    d = loop_district()
    d.street((1000, 0), (1000, -300), settlement=1)
    d.node(60, "customer", 1000, -300, hint="dedicated", p_fixed_kw=p_kw, settlement=1)
    plan, (inp, *_) = plan_of(d, RING)
    assert_plan_valid(plan, inp)
    feeder = ring_of(plan, 60)
    assert feeder.kind == "dedicated" and feeder.members == [60] and feeder.nop_key == 60
    legs = line_between(plan, BUSBAR, 60)
    assert [leg.exit for leg in legs] == ["a", "b"]
    assert [leg.normally_open for leg in legs] == [False, True] and legs[1].open_at_key == 60
    assert all(leg.role == "dedicated" and leg.parallel == parallel for leg in legs)
    assert legs[0].pieces == legs[1].pieces and legs[0].length_m == pytest.approx(1300.0)
    assert {legs[0].feeder_id, legs[1].feeder_id} == {feeder.feeder_a, feeder.feeder_b}
    assert plan.node_role[60] == "dedicated"
    assert ("dedicated_overload" in plan.diagnostics["node_flags"].get(60, [])) == overload


# ============================================================================================ generators
def generator_district() -> District:
    d = loop_district(side_m=None)                          # no dead end
    d.street((4000, 500), (4500, 500))                      # rural open-field generator: T-off
    d.street((2500, 0), (2500, 50), settlement=2)           # urban generator: loop-in
    d.street((4000, 0), (10000, 0))                         # generator 6 km from the lines
    d.node(60, "generator", 4500, 500, hint="tee", gen_kw=2000.0)
    d.node(61, "generator", 2500, 50, hint="tee", gen_kw=500.0, urban=True, settlement=2)
    d.node(62, "generator", 10000, 0, hint="tee", gen_kw=1500.0)
    d.node(63, "generator", 2000, 1000, hint="tee", gen_kw=8000.0, settlement=1)     # above dedicated_feeder_kw
    d.node(64, "generator", 500, 0, hint="dedicated", gen_kw=900.0, settlement=1)     # HV/MV level
    return d


GEN_RING = [[10, 30, 40, 50]]


def test_generators_connect_by_t_off_loop_in_or_dedicated_feeder():
    plan, (inp, *_) = plan_of(generator_district(), GEN_RING)
    assert_plan_valid(plan, inp)
    roles = {k: plan.node_role[k] for k in (60, 61, 62, 63, 64)}
    assert roles == {60: "tee", 61: "loop_in", 62: "dedicated", 63: "dedicated", 64: "dedicated"}
    # T-off at the street junction on the ring; rural open field: overhead line
    joint = plan.stub_parent[60]
    assert joint < 0 and plan.joints[joint] == pytest.approx((4000.0, 500.0))
    tee = line_between(plan, joint, 60)[0]
    assert tee.role == "generator" and tee.kind == "overhead" and tee.length_m == pytest.approx(500.0)
    assert plan.node_feeder[60] == plan.node_feeder[joint]
    # loop-in: a ring member between 10 and 30, counted in the ring
    ring = ring_of(plan, 10)
    assert ring.members[:3] == [10, 61, 30] and ring.kind == "ring"
    assert plan.node_ring[61] == ring.ring_id
    # dedicated feeders: 62 (nothing within 5 km), 63 (8 MW), 64 (HV/MV level); one circuit, no open leg
    for key in (62, 63, 64):
        feeder = ring_of(plan, key)
        assert feeder.kind == "generator" and feeder.nop_key is None
        lines = line_between(plan, BUSBAR, key)
        assert len(lines) == 1 and lines[0].role == "generator" and not lines[0].normally_open
    assert "no_line_within_tee_limit" in plan.diagnostics["node_flags"][62]
    assert line_between(plan, BUSBAR, 63)[0].parallel == 1                # 8.4 MVA < 14.6 MVA
    assert plan.diagnostics["n_tee_generators"] == 1 and plan.diagnostics["n_loop_in_generators"] == 1


def test_loop_in_falls_back_to_a_t_off_when_the_ring_is_full():
    plan, (inp, *_) = plan_of(generator_district(), GEN_RING, max_stations_per_ring=4, rural_ring_factor=1.0)
    assert_plan_valid(plan, inp)
    assert plan.node_role[61] == "tee"
    assert "loop_in_infeasible" in plan.diagnostics["node_flags"][61]
    tee = line_between(plan, plan.stub_parent[61], 61)[0]
    assert tee.kind == "cable"                              # urban generator inside its settlement
    assert tee.length_m == pytest.approx(50.0)


def test_large_generator_feeder_gets_parallel_cables():
    d = generator_district()
    d.nodes[-2]["gen_kw"] = 16000.0                         # 16.8 MVA > 14.6 MVA of one cable
    plan, (inp, *_) = plan_of(d, GEN_RING)
    assert line_between(plan, BUSBAR, 63)[0].parallel == 2


def test_off_street_generator_t_off_runs_through_its_connector():
    d = loop_district(urban=True)
    d.node(70, "generator", 2600, 400, hint="tee", gen_kw=300.0, offset=(2600, 0))
    plan, (inp, *_) = plan_of(d, RING)
    assert_plan_valid(plan, inp)
    assert plan.node_role[70] == "tee"
    tee = line_between(plan, plan.stub_parent[70], 70)[0]
    assert tee.length_m == pytest.approx(400.0)
    assert Point(2600, 400).distance(Point(tee.geometry.coords[-1])) < 1e-6


# ============================================================================================ repairs
def test_add_parallel_respects_the_cap_and_returns_the_changed_keys():
    plan, _ = plan_of(loop_district(urban=True), RING)
    new, changed = topology.add_parallel(plan, [0, 1, 1, 99], max_parallel=2)
    assert changed == [0, 1]
    assert [ln.parallel for ln in new.lines[:3]] == [2, 2, 1]
    assert plan.lines[0].parallel == 1                      # the given plan is unchanged
    again, changed = topology.add_parallel(new, [0], max_parallel=2)
    assert changed == [] and again is new


def balanced_ring() -> District:
    """Three 2 MW customers on a street east of the busbar; the ring returns along the same street (3 km)."""
    d = District()
    d.street((0, 0), (1000, 0), (2000, 0), (3000, 0), settlement=1)
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar", urban=True, settlement=1)
    for key, x in zip((11, 12, 13), (1000, 2000, 3000)):
        d.node(key, "customer", x, 0, p_fixed_kw=2000.0, urban=True, settlement=1)
    return d


def test_move_nop_follows_stronger_sections_and_reassigns_feeders():
    plan, (inp, rg, model, ev) = plan_of(balanced_ring(), [[11, 12, 13]])
    ring = plan.rings[0]
    unchanged, moved = topology.move_nop(inp, rg, model, ev, plan, ring.ring_id)
    assert not moved and unchanged is plan
    assert ring.nop_key == 12 and plan.node_feeder[12] == ring.feeder_a
    # four cables on the B half (12 -> 13 -> busbar): the B half takes 12 and 13, the open point moves to 11
    b_half = [ln.key for ln in plan.lines if ln.role == "ring" and ln.from_key in (12, 13)]
    stronger = plan
    for _ in range(3):
        stronger, _ = topology.add_parallel(stronger, b_half, max_parallel=4)
    new, moved = topology.move_nop(inp, rg, model, ev, stronger, ring.ring_id)
    assert moved
    assert new.rings[0].nop_key == 11
    assert_plan_valid(new, inp)
    assert new.node_feeder[12] == new.rings[0].feeder_b and new.node_feeder[11] == new.rings[0].feeder_a
    opened = next(ln for ln in new.lines if ln.normally_open)
    assert (opened.from_key, opened.to_key, opened.feeder_id) == (11, 12, new.rings[0].feeder_b)
    assert plan.rings[0].nop_key == 12                      # the given plan is unchanged


def test_split_ring_halves_the_ring_and_reconnects_its_generators():
    d = District()
    d.street(*[(x, 0) for x in (0, 1000, 2000, 3000, 3500, 4000, 5000, 6000)], settlement=1)
    d.node(BUSBAR, "busbar", 0, 0, hint="busbar", urban=True, settlement=1)
    for key, x in zip((11, 12, 13, 14, 15, 16), range(1000, 7000, 1000)):
        d.node(key, "station", x, 0, urban=True, settlement=1)
    d.street((3500, 0), (3500, 400))
    d.node(70, "generator", 3500, 400, hint="tee", gen_kw=400.0)
    plan, (inp, rg, model, ev) = plan_of(d, [[11, 12, 13, 14, 15, 16]])
    assert plan.node_role[70] == "tee"
    plan2 = topology.split_ring(inp, rg, model, ev, plan, plan.rings[0].ring_id)
    assert_plan_valid(plan2, inp)
    rings = [r for r in plan2.rings if r.kind == "ring"]
    assert len(rings) == 2 and all(len([k for k in r.members if k > 0]) <= 3 for r in rings)
    assert rings[0].ring_id == plan.rings[0].ring_id
    assert plan2.node_role[70] == "tee" and plan2.node_ring[70] == ring_of(plan2, 14).ring_id
    assert sorted(k for r in rings for k in r.members if k > 0) == [11, 12, 13, 14, 15, 16]


def test_split_ring_keeps_single_member_rings():
    plan, (inp, rg, model, ev) = plan_of(radial_district(), [[40], [41], [42]], satellites=False)
    assert topology.split_ring(inp, rg, model, ev, plan, ring_of(plan, 41).ring_id) is plan


def test_convert_t_off_generator_to_dedicated_feeder_removes_its_joint():
    plan, (inp, rg, model, ev) = plan_of(generator_district(), GEN_RING)
    joint = plan.stub_parent[60]
    new = topology.convert_to_dedicated(inp, rg, model, plan, 60)
    assert_plan_valid(new, inp)
    assert new.node_role[60] == "dedicated" and ring_of(new, 60).kind == "generator"
    assert (4000.0, 500.0) not in [tuple(xy) for xy in new.joints.values()]
    assert len(new.joints) == len(plan.joints) - 1 and joint in plan.joints
    assert new.node_role[61] == "loop_in"                   # the other decisions are kept
    assert topology.convert_to_dedicated(inp, rg, model, new, 60) is new
    with pytest.raises(ValueError, match="not a generator"):
        topology.convert_to_dedicated(inp, rg, model, new, 10)


def test_convert_loop_in_generator_and_keep_parallel_cables():
    plan, (inp, rg, model, ev) = plan_of(generator_district(), GEN_RING)
    exit_a = next(ln.key for ln in plan.lines if ln.exit == "a" and ln.role == "ring")
    plan, changed = topology.add_parallel(plan, [exit_a], max_parallel=2)
    assert changed == [exit_a]
    new = topology.convert_to_dedicated(inp, rg, model, plan, 61)
    assert_plan_valid(new, inp)
    assert new.node_role[61] == "dedicated"
    assert 61 not in ring_of(new, 10).members
    new_exit = next(ln for ln in new.lines if ln.exit == "a" and ln.role == "ring")
    assert new_exit.parallel == 2


# ============================================================================================ crowfly
def test_crowfly_plan_uses_straight_spans_and_stubs_on_them():
    plan, (inp, *_) = plan_of(loop_district(1500.0), RING, routing="crowfly", overhead_lines="none")
    assert_plan_valid(plan, inp)
    for ln in plan.lines:
        assert len(ln.geometry.coords) == 2 and not ln.pieces
        assert ln.length_m == pytest.approx(1.3 * ln.straight_m)
    assert plan.node_role[20] == "stub"
    joint = plan.stub_parent[20]
    assert plan.joints[joint] == pytest.approx((2000.0, 0.0))
    assert line_between(plan, joint, 20)[0].length_m == pytest.approx(1950.0)
