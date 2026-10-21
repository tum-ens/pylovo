"""Repairs of the MV topology with the real ring evaluator, solver, coincidence and equipment modules: a ring split
re-lays out the whole district, and the parallel cables and larger conductors of earlier repairs must stay on the
lines they were made for (``tests/test_mv_topology.py`` fakes these modules)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from shapely.geometry import LineString

from pylovo.mv_grid import rings, topology
from pylovo.mv_grid.model import NODE_COLUMNS, DistrictInput, EdgeSet, RunGraph
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

LV = LvVersionParameters(version_id="t", sim_factor={"Residential": 0.07, "Commercial": 0.5, "Public": 0.6,
                                                     "Unknown": 0.6},
                         power_factor=0.95, mv_direct_threshold_kw=200.0, residential_only=False,
                         lv_reference_voltage_pu=0.96, use_dso=False, use_open=False, use_manual=False,
                         electrical_backend="pandapower")
BUSBAR = 1


class District:
    """Straight streets and nodes at street vertices (busbar first)."""

    def __init__(self) -> None:
        self.xy: list[tuple[float, float]] = []
        self.edges: list[tuple[int, int, int, bool]] = []
        self.nodes: list[dict] = []

    def vertex(self, x: float, y: float) -> int:
        if (float(x), float(y)) not in self.xy:
            self.xy.append((float(x), float(y)))
        return self.xy.index((float(x), float(y)))

    def street(self, *points, settlement: int = 1, overhead: bool = False) -> None:
        ids = [self.vertex(*p) for p in points]
        self.edges += [(a, b, settlement, overhead) for a, b in zip(ids[:-1], ids[1:])]

    def node(self, key: int, kind: str, x: float, y: float, *, urban: bool = True, hint: str = "ring") -> None:
        self.nodes.append(dict(key=key, kind=kind, x=float(x), y=float(y), urban=urban,
                               hint="busbar" if kind == "busbar" else hint))

    def build(self) -> tuple[DistrictInput, RunGraph]:
        terminal_vertex = [self.vertex(n["x"], n["y"]) for n in self.nodes]
        xy = np.asarray(self.xy, dtype=float)
        pieces = pd.DataFrame([dict(piece_id=i, u=a, v=b, length_m=float(np.hypot(*(xy[a] - xy[b]))), edge_id=i,
                                    f0=0.0, f1=1.0, kind="street", in_settlement=True, settlement_id=s,
                                    overhead=o) for i, (a, b, s, o) in enumerate(self.edges)])
        params = MvParameters()
        pieces["cost_m"] = np.where(pieces.overhead, params.osm_line_cost_factor, 1.0) * pieces.length_m
        n = len(xy)
        csr = csr_matrix((np.r_[pieces.cost_m, pieces.cost_m], (np.r_[pieces.u, pieces.v], np.r_[pieces.v, pieces.u])),
                         shape=(n, n))
        pair_piece = {(min(a, b), max(a, b)): int(i) for i, a, b in zip(pieces.piece_id, pieces.u, pieces.v)}
        rg = RunGraph(csr=csr, n_vertices=n, vertex_xy=xy, terminal_vertex=np.asarray(terminal_vertex), pieces=pieces,
                      pair_piece=pair_piece, n_bridges=0, bridged_rows=np.zeros(len(self.nodes), dtype=bool))
        geoms = {i: LineString([xy[a], xy[b]]) for i, (a, b, _, _) in enumerate(self.edges)}
        records = []
        for nd in self.nodes:
            station = nd["kind"] == "station"
            rec = {c: 0.0 for c in NODE_COLUMNS}
            rec.update(kind=nd["kind"], connection_hint=nd["hint"], x=nd["x"], y=nd["y"], attach_edge_id=-1,
                       attach_fraction=0.0, attach_offset_m=0.0, bridged=False, settlement_id=1, urban=nd["urban"],
                       plz=12345, plz_settlement_type=3, inst_res=150.0 if station else 0.0,
                       units_res=50.0 if station else 0.0, p_fixed_kw=0.0, gen_level="", sn_kva=400.0,
                       grid_result_id=None, lv_design_source_vm_pu=0.98, lv_max_drop_pu=0.03)
            records.append(rec)
        frame = pd.DataFrame(records, columns=list(NODE_COLUMNS),
                             index=pd.Index([nd["key"] for nd in self.nodes], name="mv_node_id"))
        edges = EdgeSet(*(np.zeros(0, dtype=np.int64) for _ in range(3)), np.zeros(0), np.zeros(0, dtype=bool),
                        np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64), np.zeros((0, 2)))
        inp = DistrictInput(mv_grid_id=1, substation_id=1, vn_kv=20.0, nodes=frame, edges=edges, params=params,
                            lv=LV, edge_geometry=lambda ids: {int(i): geoms[int(i)] for i in ids})
        return inp, rg


def plan_with_routes(district: District, routes: list[list[int]]):
    """The plan of the given rings (node keys) by the real topology and ring evaluator, and its inputs."""
    topology._CONTEXT.clear()
    inp, rg = district.build()
    terminals = topology.terminal_rows(inp)
    model = topology.distance_model(inp, rg, terminals)
    ev = rings.RingEvaluator.from_model(inp, model)
    keys = list(inp.nodes.index[terminals])
    plan = topology.build_plan(inp, rg, model, ev, [[keys.index(k) for k in route] for route in routes])
    return plan, (inp, rg, model, ev)


def ring_lines(plan, member: int):
    ring = next(r for r in plan.rings if member in r.members)
    return [line for line in plan.lines if line.ring_id == ring.ring_id]


def exit_line(plan, member: int, exit_: str):
    return next(line for line in ring_lines(plan, member) if line.exit == exit_)


def three_rings(overhead: bool = False) -> District:
    """Rings A (north) and B (south) share the street busbar-J (500 m); ring C runs west."""
    d = District()
    d.street((0, 0), (500, 0), overhead=overhead)
    d.street((500, 0), (500, 1000), (1500, 1000))
    d.street((500, 0), (500, -1000), (1500, -1000))
    d.street((0, 0), (-1000, 0), (-1000, 1000), (-2000, 1000))
    d.node(BUSBAR, "busbar", 0, 0)
    for key, xy in ((11, (500, 1000)), (12, (1500, 1000)), (21, (500, -1000)), (22, (1500, -1000)),
                    (31, (-1000, 1000)), (32, (-2000, 1000))):
        d.node(key, "station", *xy)
    return d


def split(plan, inputs, member: int):
    ring = next(r for r in plan.rings if member in r.members)
    return topology.split_ring(*inputs, plan, ring.ring_id)


def test_a_split_keeps_a_parallel_cable_only_on_the_line_that_got_it():
    plan, inputs = plan_with_routes(three_rings(), [[11, 12], [21, 22], [31, 32]])
    exit_a = exit_line(plan, 11, "a")
    plan, changed = topology.add_parallel(plan, [exit_a.key], max_parallel=2)
    assert changed == [exit_a.key]
    after = split(plan, inputs, 31)
    assert exit_line(after, 11, "a").parallel == 2                          # ring A's exit keeps its second cable
    assert exit_line(after, 11, "b").parallel == 1                          # its other exit shares only a trench
    assert all(line.parallel == 1 for line in ring_lines(after, 21))        # ring B shares only busbar-J


def test_a_split_keeps_a_larger_conductor_only_on_the_line_that_got_it():
    plan, inputs = plan_with_routes(three_rings(overhead=True), [[11, 12], [21, 22], [31, 32]])
    exit_a = exit_line(plan, 11, "a")
    assert exit_a.kind == "mixed"
    plan, changed = topology.upgrade_lines(plan, [exit_a.key], 20.0)
    upgraded = plan.lines[exit_a.key].std_type
    assert changed == [exit_a.key] and upgraded != exit_a.std_type
    after = split(plan, inputs, 31)
    assert exit_line(after, 11, "a").std_type == upgraded
    assert all(line.std_type != upgraded for line in ring_lines(after, 21))


def test_a_split_does_not_pass_a_parallel_cable_between_exits_that_end_at_joints():
    """Every line from the busbar to a joint has the busbar as its only node end; that is no common line."""
    d = District()
    d.street((0, 0), (0, 500), (0, 2000), (1000, 2000))
    d.street((0, 500), (-400, 500))
    d.street((0, 0), (0, -500), (0, -2000), (1000, -2000))
    d.street((0, -500), (-400, -500))
    d.street((0, 0), (-1000, 0), (-1000, 1000), (-2000, 1000))
    d.node(BUSBAR, "busbar", 0, 0, urban=False)
    d.node(10, "station", -400, 500, urban=False)                           # rural: a stub on ring A
    d.node(20, "station", -400, -500, urban=False)                          # rural: a stub on ring B
    for key, xy in ((11, (0, 2000)), (12, (1000, 2000)), (21, (0, -2000)), (22, (1000, -2000)),
                    (31, (-1000, 1000)), (32, (-2000, 1000))):
        d.node(key, "station", *xy)
    plan, inputs = plan_with_routes(d, [[10, 11, 12], [20, 21, 22], [31, 32]])
    exit_a, exit_b = exit_line(plan, 11, "a"), exit_line(plan, 21, "a")
    assert plan.node_role[10] == "stub" and exit_a.to_key < 0 and exit_b.to_key < 0
    plan, _ = topology.add_parallel(plan, [exit_a.key], max_parallel=2)
    after = split(plan, inputs, 31)
    assert exit_line(after, 11, "a").parallel == 2 and exit_line(after, 21, "a").parallel == 1


def test_a_split_of_a_ring_with_stubs_halves_all_nodes_it_solves_again():
    """Three rural ring members and five stub stations: the cap of the split is half of all eight nodes."""
    d = District()
    d.street(*[(x, 0) for x in range(0, 4001, 500)])
    d.node(BUSBAR, "busbar", 0, 0, urban=False)
    for i, x in enumerate((1000, 2000, 3000, 4000)):
        d.node(11 + i, "station", x, 0, urban=False)
    for i, x in enumerate((1500, 2500, 3500, 500)):
        d.street((x, 0), (x, 300))
        d.node(21 + i, "station", x, 300, urban=False)
    plan, inputs = plan_with_routes(d, [[24, 11, 21, 12, 22, 13, 23, 14]])
    assert [r.kind for r in plan.rings] == ["ring"] and sum(role == "stub" for role in plan.node_role.values()) >= 4
    after = split(plan, inputs, 11)
    assert len(after.rings) == 2                       # two halves (both short enough for busbar radials here)
