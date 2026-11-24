"""Topology of one MV supply district: rings with their open points, rural stubs and dedicated feeders.

This module turns the rings of a solver (:mod:`pylovo.mv_grid.solvers`) into a
:class:`~pylovo.mv_grid.model.TopologyPlan`:

1. **Rings** from the solver, each link routed along the run graph (:func:`distance_model`).
2. **Rural stubs**: a rural station or MV customer leaves its ring for a T-off when that saves
   more ring length than the T-off costs (``saving > spur``, ``spur > stub_min_offset_m``), the stub tree stays
   within ``stub_max_kva`` and within the length of its load (``stub_max_length_m``, and load x length at most
   ``stub_max_kva_km``), and the shortened ring with the stub loads stays feasible.
   Later stubs may hang on earlier ones (stub trees). Urban nodes are always looped in. Then the **satellites**
   (rural stations outside settlements or alone in theirs, which the solver never saw) hang on a stub from the
   nearest ring within the same limits, nearest to the busbar first; a satellite that fits no stub goes into the
   ring where it costs the least length and stays feasible, else into a ring of its own (Ding0's satellites).
3. **Busbar radials**: a rural single-station ring of at most ``stub_max_kva`` within the stub length of its load
   (street) of the busbar becomes one radial line.
4. **Dedicated customers** (``connection_hint = 'dedicated'``): a 2-leg feeder from the busbar, leg 2 normally open
   at the customer, parallel cables by load.
5. The **normally-open point** of every ring at its voltage balance, only at a switchgear node
   (:func:`pylovo.mv_grid.rings.check_ring` on explicit sections, joints included).
6. **Geometries** along the run-graph pieces (straight only for bridges and connectors), and the line types: a line
   is an overhead line where its route runs on OSM MV lines and a cable elsewhere, ``cable``, ``overhead`` or
   ``mixed`` (length-weighted r and x, the smaller ampacity).

All length limits (``stub_max_length_m``) hold for the true route length: with ``osm_line_cost_factor`` below 1,
routes are chosen by their cost, but a stub or radial is accepted by its
length.

The plan is always built from its *decisions* (ring orders, stubs, T-offs, feeders), so the repair functions
(:func:`move_nop`, :func:`split_ring`, :func:`add_parallel`, :func:`upgrade_lines`)
change a decision and rebuild the joints and geometries from it. They return a new plan and never change the plan
they are given. A rebuilt plan keeps the parallel cables and the upgraded conductors of the old plan on the same
street pieces.

Paths, stub and T-off searches use SciPy's Dijkstra on ``RunGraph.csr`` directly (searches limited to the stub or
T-off length), keyed by the local vertices of the run graph (:mod:`~pylovo.mv_grid.routing`). This module keeps the
public functions, the :class:`DistrictContext` they share and the decisions; :mod:`~pylovo.mv_grid.network` lays
the decisions out and builds the plan from them. The module is pure: no database, configuration or plotting import.
The solvers and the coincidence law are imported on first use (the ring evaluator in :mod:`~pylovo.mv_grid.network`).
"""
from __future__ import annotations

import dataclasses
import importlib
import math
from typing import Any, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid import equipment
from pylovo.mv_grid.graph import terminal_lengths, terminal_overhead
from pylovo.mv_grid.model import (
    MEMBER_KINDS,
    DistanceModel,
    DistrictInput,
    RingCheck,
    RingLimits,
    RunGraph,
    TopologyPlan,
    member_weights,
)
from pylovo.mv_grid.network import (
    Decisions,
    Network,
    Struct,
    chain_feeder,
    children,
    choose_nop,
    layout,
    materialize,
    plan_chain,
    ring_feeders,
)
from pylovo.mv_grid.routing import Router, terminal_distances

INSTALLED_COLUMNS: tuple[str, ...] = ("inst_res", "inst_com", "inst_pub", "inst_unk")
UNIT_COLUMNS: tuple[str, ...] = ("units_res", "units_com", "units_pub", "units_unk")
#: Node kinds with a load (station statistics or the fixed customer load).
LOAD_KINDS: tuple[str, ...] = ("station", "customer")


def _solvers():
    """The ring solver module (imported on first use)."""
    return importlib.import_module("pylovo.mv_grid.solvers")


def _coincidence():
    """The coincidence module (imported on first use)."""
    return importlib.import_module("pylovo.mv_grid.coincidence")


# ============================================================================================ public: terminals, model
def terminal_rows(inp: DistrictInput) -> np.ndarray:
    """Row positions of the terminals of a district: the busbar first, then every connected node by ``mv_node_id``.

    Terminals are the busbar, the stations and the MV customers; nodes with
    ``connection_hint = 'excluded'`` are not terminals.

    Raises:
        ValueError: If ``inp.nodes`` does not start with its only busbar row.
    """
    kind = inp.nodes["kind"].astype(str).to_numpy()
    hint = inp.nodes["connection_hint"].astype(str).to_numpy()
    busbar = np.flatnonzero(kind == "busbar")
    if len(busbar) != 1 or busbar[0] != 0:
        raise ValueError(f"district {inp.mv_grid_id}: the node table must start with its only busbar row")
    other = np.flatnonzero(np.isin(kind, MEMBER_KINDS) & (hint != "excluded"))
    order = np.argsort(np.asarray(inp.nodes.index, dtype=np.int64)[other], kind="stable")
    return np.concatenate([[0], other[order]]).astype(np.int64)


def distance_model(inp: DistrictInput, rg: RunGraph, terminals: np.ndarray) -> DistanceModel:
    """Design distances and link kinds between the terminals of a district.

    Every link follows the run graph: ``street_m`` is its routing cost (``inf`` where unreachable) and the solvers'
    ``dist_m``, ``length_m`` its true length and ``overhead_m`` the part of it on OSM MV lines
    (:func:`pylovo.mv_grid.graph.terminal_overhead`); ``overhead`` marks links that are mostly overhead. The OSM
    lines cost ``osm_line_cost_factor`` times their length, so the routes follow the existing corridors.
    ``straight_m`` is Euclidean. The busbar legs follow the run graph like every other link: feeders leave HV/MV
    substations in shared trenches, not as straight spokes.

    Ring candidates are the stations and the customers that are neither dedicated nor excluded, without the
    **satellites**: rural stations outside every settlement or alone in their settlement. They are connected after
    the rings are solved (:func:`build_plan`). A district whose candidates would all be satellites has none.
    """
    terminals = np.asarray(terminals, dtype=np.int64)
    nodes = inp.nodes
    xy = _numbers(nodes, ("x", "y"))[terminals]
    straight_m = np.hypot(xy[:, None, 0] - xy[None, :, 0], xy[:, None, 1] - xy[None, :, 1])
    street_m = terminal_distances(rg, terminals)
    settlement = _settlement_ids(nodes)[terminals]
    urban = _flags(nodes["urban"])[terminals]
    overhead_m = terminal_overhead(rg, terminals)
    length_m = terminal_lengths(rg, terminals)
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(np.isfinite(length_m) & (length_m > 0), overhead_m / length_m, 0.0)
    overhead = share > 0.5
    dist = street_m.copy()
    np.fill_diagonal(overhead, False)
    np.fill_diagonal(dist, 0.0)
    kind = nodes["kind"].astype(str).to_numpy()[terminals]
    hint = nodes["connection_hint"].astype(str).to_numpy()[terminals]
    candidate = np.isin(kind, LOAD_KINDS) & ~np.isin(hint, ("dedicated", "excluded"))
    candidate[0] = False
    counts = pd.Series(settlement[candidate & (settlement >= 0)]).value_counts()
    alone = np.array([s < 0 or counts.get(int(s), 0) <= 1 for s in settlement], dtype=bool)
    satellite = candidate & (kind == "station") & ~urban & alone
    if not (candidate & ~satellite).any():
        satellite[:] = False
    return DistanceModel(terminals=terminals, candidates=np.flatnonzero(candidate & ~satellite).astype(np.int64),
                         dist_m=dist, street_m=street_m, straight_m=straight_m, overhead=overhead, xy=xy,
                         satellite=satellite, overhead_m=overhead_m, length_m=length_m)


# ============================================================================================ public: plan and repairs
def build_plan(ctx: DistrictContext, routes: list[list[int]]) -> TopologyPlan:
    """The topology of a district from the solver's rings (the steps of the module docstring).

    Args:
        ctx: The district context (input, run graph and the distance model of :func:`distance_model`).
        routes: Rings as terminal indices in ring order, busbar excluded. A ring candidate missing from every route
            becomes a single-station ring (flag ``unrouted``). The satellites of the model are connected after the
            stubs (:func:`_attach_satellites`).
    """
    model = ctx.model
    dec = Decisions()
    candidates = [int(model.terminals[t]) for t in model.candidates]
    candidate_set = set(candidates)
    seen: set[int] = set()
    for route in routes:
        rows = []
        for t in route:
            row = int(model.terminals[int(t)])
            if int(t) != 0 and row in candidate_set and row not in seen:
                rows.append(row)
                seen.add(row)
        if rows:
            dec.structs.append(Struct(dec.next_ring_id(), "ring", rows))
    for row in candidates:
        if row not in seen:
            dec.structs.append(Struct(dec.next_ring_id(), "ring", [row]))
            dec.flag(ctx, row, "unrouted")
    ring_ids = [st.ring_id for st in dec.structs]
    _decide_stubs(ctx, dec, ring_ids)
    satellites = _attach_satellites(ctx, dec)
    _decide_radials(ctx, dec, [st.ring_id for st in dec.structs])
    for row in ctx.terminals[1:]:
        row = int(row)
        if ctx.kind[row] == "customer" and ctx.hint[row] == "dedicated":
            dec.structs.append(Struct(dec.next_ring_id(), "dedicated", [row]))
    plan = materialize(ctx, dec)
    plan.diagnostics["satellites"] = satellites
    return plan


def move_nop(ctx: DistrictContext, plan: TopologyPlan, ring_id: int) -> tuple[TopologyPlan, bool]:
    """Move the open point of ring ``ring_id`` to its best allowed position for the plan's current sections.

    The best position is the voltage balance of :func:`pylovo.mv_grid.rings.check_ring` among the feasible allowed
    positions, or among all allowed positions if none is feasible. The section parallels of the plan are used.

    Returns:
        The new plan and whether the open point moved (the given plan unchanged and ``False`` otherwise).
    """
    ring = next((r for r in plan.rings if r.ring_id == ring_id and r.kind == "ring"), None)
    chain = plan_chain(plan, ring_id) if ring is not None else None
    if ring is None or not chain:
        return plan, False
    pos_keys = list(ring.members)
    kids = children(plan.stub_parent)
    sections = [(plan.lines[i].length_m, plan.lines[i].kind, plan.lines[i].parallel, plan.lines[i].overhead_m,
                 plan.lines[i].std_type) for i in chain]
    m, nop, check = choose_nop(ctx, pos_keys, sections, kids)
    current_m = next((j for j, i in enumerate(chain) if plan.lines[i].normally_open), None)
    current_nop = pos_keys.index(ring.nop_key) + 1 if ring.nop_key in pos_keys else None
    if (m, nop) == (current_m, current_nop):
        return plan, False
    lines = list(plan.lines)
    for i in chain:
        if lines[i].normally_open:
            lines[i] = dataclasses.replace(lines[i], normally_open=False, open_at_key=None)
    lines[chain[m]] = dataclasses.replace(lines[chain[m]], normally_open=True, open_at_key=pos_keys[nop - 1])
    node_feeder = dict(plan.node_feeder)
    feeders = ring_feeders(pos_keys, m, ring.feeder_a, ring.feeder_b, kids)
    node_feeder.update(feeders)
    for j, i in enumerate(chain):
        lines[i] = dataclasses.replace(lines[i], feeder_id=chain_feeder(j, m, nop, ring.feeder_a, ring.feeder_b))
    chain_set = set(chain)
    for i, line in enumerate(lines):
        if line.ring_id == ring_id and i not in chain_set and line.to_key in feeders:
            lines[i] = dataclasses.replace(line, feeder_id=feeders[line.to_key])
    rings = [dataclasses.replace(r, nop_key=pos_keys[nop - 1], check=check) if r.ring_id == ring_id else r
             for r in plan.rings]
    return dataclasses.replace(plan, rings=rings, lines=lines, node_feeder=node_feeder), True


def split_ring(ctx: DistrictContext, ev: Any, plan: TopologyPlan, ring_id: int) -> TopologyPlan:
    """Split ring ``ring_id``: re-solve its stations and customers (its stubs included) with at most half of its
    weighted members per ring, ``(w + w_max) / 2`` (``ceil(k / 2)`` members when all weigh the same; ``savings`` with
    local search), and rebuild its stubs and radials.

    The first new ring keeps ``ring_id``; the others get new ids. A ring with fewer than two members is returned
    unchanged. Line parallels of the plan are kept where the new lines use the same street pieces.
    """
    model = ctx.model
    dec = _decisions_from_plan(ctx, plan)
    struct = next((s for s in dec.structs if s.ring_id == ring_id and s.kind == "ring"), None)
    if struct is None or len(struct.rows) < 2:
        return plan
    stub_rows = [row for row, rid in dec.stubs if rid == ring_id]
    ring_rows = [r for r in struct.rows if ctx.kind[r] in LOAD_KINDS] + stub_rows
    weights = [float(ctx.cap_weight[r]) for r in ring_rows] or [1.0]       # of every node that is solved again
    cap = (sum(weights) + max(weights)) / 2.0
    dec.structs = [s for s in dec.structs if s.ring_id != ring_id]
    dec.stubs = [(row, rid) for row, rid in dec.stubs if rid != ring_id]
    candidates = np.array(sorted(ctx.t_of_row[r] for r in ring_rows), dtype=np.int64)
    capped = _CappedEvaluator(ev, cap, ctx)
    result = _solvers().solve(capped, model.dist_m, candidates, rounds=ctx.p.local_search_rounds,
                              neighbours=ctx.p.neighbours, time_guard_s=ctx.p.ls_time_guard_s)
    new_ids = []
    seen: set[int] = set()
    for route in result.routes:
        rows = [int(model.terminals[int(t)]) for t in route if int(t) != 0]
        rows = [r for r in rows if r in set(ring_rows) and r not in seen]
        if not rows:
            continue
        seen.update(rows)
        rid = ring_id if not new_ids else dec.next_ring_id()
        new_ids.append(rid)
        dec.structs.append(Struct(rid, "ring", rows))
    for row in ring_rows:
        if row not in seen:
            rid = ring_id if not new_ids else dec.next_ring_id()
            new_ids.append(rid)
            dec.structs.append(Struct(rid, "ring", [row]))
    _decide_stubs(ctx, dec, new_ids)
    _decide_radials(ctx, dec, new_ids)
    return materialize(ctx, dec, old=plan)


def add_parallel(plan: TopologyPlan, line_keys: Sequence[int], max_parallel: int
                 ) -> tuple[TopologyPlan, list[int]]:
    """Add one parallel cable to each of ``line_keys`` that has fewer than ``max_parallel``.

    The ring checks stored in the plan are not recomputed (pandapower verifies the repaired plan).

    Returns:
        The new plan and the keys of the lines that got a cable (in the order given).
    """
    wanted = [int(k) for k in line_keys]
    lines = list(plan.lines)
    position = {line.key: i for i, line in enumerate(lines)}
    changed = []
    for key in wanted:
        i = position.get(key)
        if i is None or key in changed or lines[i].parallel >= max_parallel:
            continue
        lines[i] = dataclasses.replace(lines[i], parallel=lines[i].parallel + 1)
        changed.append(key)
    if not changed:
        return plan, []
    return dataclasses.replace(plan, lines=lines), changed


def upgrade_lines(plan: TopologyPlan, line_keys: Sequence[int], vn_kv: float,
                  needed_ka: dict[int, float] | None = None) -> tuple[TopologyPlan, list[int]]:
    """Give each of ``line_keys`` the next larger conductor of its kind, as Ding0's reinforcement does; with
    ``needed_ka[key]`` the smallest conductor that carries that current (one circuit). A mixed line upgrades both of
    its parts. Lines that already have the largest conductors keep them.

    The ring checks stored in the plan are not recomputed (pandapower verifies the repaired plan).

    Returns:
        The new plan and the keys of the lines that got a larger conductor (in the order given).
    """
    lines = list(plan.lines)
    position = {line.key: i for i, line in enumerate(lines)}
    changed = []
    for key in (int(k) for k in line_keys):
        i = position.get(key)
        if i is None or key in changed:
            continue
        need = None if needed_ka is None else needed_ka.get(key)
        parts = [part.strip() for part in (lines[i].std_type or "").split(" + ")]
        larger = [_upgraded(part, vn_kv, need) for part in parts]
        if any(name is not None for name in larger):
            name = " + ".join(new or old for new, old in zip(larger, parts))
            lines[i] = dataclasses.replace(lines[i], std_type=name)
            changed.append(key)
    if not changed:
        return plan, []
    return dataclasses.replace(plan, lines=lines), changed


def _upgraded(name: str, vn_kv: float, need: float | None) -> str | None:
    """The larger conductor of :func:`pylovo.mv_grid.equipment.upgraded_line_type`; None for a type that pandapower
    does not know."""
    try:
        return equipment.upgraded_line_type(name, vn_kv, need) if name else None
    except ValueError:
        return None


# ============================================================================================ helpers: tables, context
def _numbers(frame: pd.DataFrame, columns: Sequence[str]) -> np.ndarray:
    return frame[list(columns)].apply(pd.to_numeric, errors="coerce").fillna(0.0).to_numpy(dtype=float)


def _flags(column: pd.Series) -> np.ndarray:
    return np.array([bool(v) if v is not None and v == v else False for v in column], dtype=bool)


def _settlement_ids(nodes: pd.DataFrame) -> np.ndarray:
    return pd.to_numeric(nodes["settlement_id"], errors="coerce").fillna(-1).to_numpy().astype(np.int64)


class DistrictContext:
    """The arrays of one district, shared by the build of its plan and the repairs.

    The planner creates it once per district (:func:`pylovo.mv_grid.planner.plan_district`) and passes it to
    :func:`build_plan`, :func:`move_nop`, :func:`split_ring` and the repair loop, so its router keeps the paths and
    the limited Dijkstra trees of the district between them.

    Args:
        inp: The district input.
        rg: Its run graph.
        model: Its distance model (:func:`distance_model`).
    """

    def __init__(self, inp: DistrictInput, rg: RunGraph, model: DistanceModel) -> None:
        self.inp, self.rg, self.model, self.p = inp, rg, model, inp.params
        nodes = inp.nodes
        self.keys = np.asarray(nodes.index, dtype=np.int64)
        self.row_of_key = {int(k): i for i, k in enumerate(self.keys)}
        self.kind = nodes["kind"].astype(str).to_numpy()
        self.hint = nodes["connection_hint"].astype(str).to_numpy()
        self.xy = _numbers(nodes, ("x", "y"))
        self.urban = _flags(nodes["urban"])
        self.cap_weight = member_weights(self.kind, self.urban, self.p.rural_ring_factor)
        loads = np.isin(self.kind, LOAD_KINDS)
        self.inst = _numbers(nodes, INSTALLED_COLUMNS) * loads[:, None]
        self.units = _numbers(nodes, UNIT_COLUMNS) * loads[:, None]
        self.fixed = _numbers(nodes, ("p_fixed_kw",))[:, 0] * loads
        self.vertex = np.asarray(rg.terminal_vertex, dtype=np.int64)
        self.vertex_xy = np.asarray(rg.vertex_xy, dtype=float)
        self.terminals = np.asarray(model.terminals, dtype=np.int64)
        self.t_of_row = {int(r): t for t, r in enumerate(self.terminals)}
        self.busbar_row = int(self.terminals[0])
        self.busbar_key = int(self.keys[self.busbar_row])
        self.busbar_vertex = int(self.vertex[self.busbar_row])
        self.g = np.asarray(inp.lv.g(), dtype=float)
        self.pf = float(inp.lv.power_factor)
        self.vn_kv = float(inp.vn_kv)
        self.cable = self.p.cable_at(self.vn_kv)            # the types of the district's nominal voltage
        self.overhead = self.p.overhead_at(self.vn_kv)
        self.costed = self.p.osm_line_cost_factor != 1.0   # router distances are costs, not lengths
        self.limits = RingLimits.of(self.p)
        self.relaxed = RingLimits(max_members=10 ** 9, max_drop=math.inf, max_drop_n1=math.inf,
                                  utilization=math.inf, n1_utilization=math.inf, max_half_m=math.inf)
        pieces = rg.pieces
        pid = pieces["piece_id"].to_numpy(dtype=np.int64)
        size = int(pid.max()) + 1 if len(pid) else 0
        self.piece_edge = np.full(size, -1, dtype=np.int64)
        self.piece_f0 = np.zeros(size)
        self.piece_f1 = np.ones(size)
        self.piece_bridge = np.zeros(size, dtype=bool)
        self.piece_overhead = np.zeros(size, dtype=bool)
        self.piece_length = np.zeros(size)
        if size:
            self.piece_edge[pid] = pd.to_numeric(pieces["edge_id"], errors="coerce").fillna(-1).astype(np.int64)
            self.piece_f0[pid] = pd.to_numeric(pieces["f0"], errors="coerce").fillna(0.0)
            self.piece_f1[pid] = pd.to_numeric(pieces["f1"], errors="coerce").fillna(1.0)
            self.piece_bridge[pid] = pieces["kind"].astype(str).to_numpy() == "bridge"
            if "overhead" in pieces:
                self.piece_overhead[pid] = _flags(pieces["overhead"])
            self.piece_length[pid] = pd.to_numeric(pieces["length_m"], errors="coerce").fillna(0.0)
        self.pair_piece = rg.pair_piece
        self.router = Router(rg.csr)
        self.geoms: dict[int, Any] = {}

    def key(self, row: int) -> int:
        return int(self.keys[row])

    def point(self, key: int, net: "Network | None" = None) -> np.ndarray:
        if key > 0:
            return self.xy[self.row_of_key[key]]
        return np.asarray(net.joint_xy[key], dtype=float)

    def stub_limit_m(self, load_kva: float) -> float:
        """Longest stub tree (true length) for its coincident load: ``stub_max_length_m``, shortened so that load x
        length stays within ``stub_max_kva_km`` (the outage energy of a stub grows with both)."""
        p = self.p
        if load_kva <= 0:
            return float(p.stub_max_length_m)
        return min(float(p.stub_max_length_m), 1000.0 * float(p.stub_max_kva_km) / float(load_kva))

    def coincident_kva(self, rows: Sequence[int]) -> float:
        """Coincident apparent power of a set of node rows (category law plus fixed customer loads)."""
        rows = list(rows)
        if not rows:
            return 0.0
        p = float(_coincidence().coincident_kw(self.inst[rows].sum(axis=0), self.units[rows].sum(axis=0), self.g))
        return (p + float(self.fixed[rows].sum())) / self.pf

    def piece(self, u: int, v: int) -> int:
        return int(self.pair_piece.get((u, v) if u < v else (v, u), -1))

    def line_type(self, kind: str):
        return self.cable if kind == "cable" else self.overhead

    def section_values(self, kind: str, length: float, overhead_m: float,
                       std_type: str | None = None) -> tuple[float, float, float]:
        """r and x in Ohm/km and the ampacity in kA of one circuit of a line; a mixed line is length-weighted with
        the smaller ampacity of its parts (as :func:`pylovo.mv_grid.equipment.mixed_parameters`). ``std_type`` is
        the conductor of the line (``'<cable> + <overhead>'`` for a mixed line; None: the district's types), so an
        upgraded line counts with its larger conductor."""
        cable, overhead = self.cable, self.overhead
        if std_type and std_type != self.std_type(kind):
            parts = [equipment.line_type(part.strip()) for part in std_type.split(" + ")]
            if kind == "mixed":
                cable, overhead = parts
            elif kind == "cable":
                cable = parts[0]
            else:
                overhead = parts[0]
        if kind != "mixed":
            lt = cable if kind == "cable" else overhead
            return float(lt.r_ohm_per_km), float(lt.x_ohm_per_km), float(lt.max_i_ka)
        share = min(1.0, max(0.0, float(overhead_m) / float(length))) if length > 0 else 0.0
        return (share * overhead.r_ohm_per_km + (1.0 - share) * cable.r_ohm_per_km,
                share * overhead.x_ohm_per_km + (1.0 - share) * cable.x_ohm_per_km,
                float(min(cable.max_i_ka, overhead.max_i_ka)))

    def std_type(self, kind: str) -> str:
        """The type name of a line: the cable or overhead type, ``'<cable> + <overhead>'`` for a mixed line."""
        if kind == "mixed":
            return f"{self.cable.name} + {self.overhead.name}"
        return self.line_type(kind).name


# ============================================================================================ helpers: decisions
class _CappedEvaluator:
    """A ring evaluator whose rings hold at most ``cap`` members (ring splits)."""

    def __init__(self, ev: Any, cap: float, ctx: DistrictContext) -> None:
        self._ev = ev
        self._cap = float(cap)
        base = getattr(ev, "limits", ctx.limits)
        self.limits = dataclasses.replace(base, max_members=min(float(base.max_members), self._cap))

    def evaluate(self, seq: Sequence[int]) -> RingCheck:
        check = self._ev.evaluate(seq)
        if self._ev.member_count(seq) > self._cap + 1e-9 and check.feasible:
            return dataclasses.replace(check, feasible=False, reason="count")
        return check

    def ring_length(self, seq: Sequence[int]) -> float:
        return self._ev.ring_length(seq)

    def member_count(self, seq: Sequence[int]) -> float:
        return self._ev.member_count(seq)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ev, name)


def _decisions_from_plan(ctx: DistrictContext, plan: TopologyPlan) -> Decisions:
    """The decisions a plan was built from (ring orders, stubs in build order, T-offs, feeders)."""
    dec = Decisions(flags={int(k): list(v) for k, v in plan.diagnostics.get("node_flags", {}).items()})
    radial_members = set()
    for ring in plan.rings:
        rows = [ctx.row_of_key[k] for k in ring.members if k > 0 and k in ctx.row_of_key]
        if ring.kind == "radial":
            radial_members.update(k for k in ring.members if k > 0)
            rows = rows[-1:]
        dec.structs.append(Struct(ring.ring_id, ring.kind, rows))
    stub_keys = [k for k, role in plan.node_role.items() if role == "stub" and k not in radial_members]
    order = {int(k): n for n, k in enumerate(plan.diagnostics.get("stub_order", []))}
    lines_order = {line.to_key: n for n, line in enumerate(plan.lines)}
    stub_keys.sort(key=lambda k: (order.get(k, len(order)), lines_order.get(k, 0), k))
    dec.stubs = [(ctx.row_of_key[k], int(plan.node_ring[k])) for k in stub_keys]
    return dec


def _decide_stubs(ctx: DistrictContext, dec: Decisions, ring_ids: Sequence[int]) -> None:
    """Step 2: rural stations and customers that leave their ring for a T-off (stub trees allowed)."""
    for struct in [s for s in dec.structs if s.ring_id in set(ring_ids) and s.kind == "ring"]:
        candidates = [r for r in struct.rows if ctx.kind[r] in LOAD_KINDS and not ctx.urban[r]]
        if len(struct.rows) < 2 or not candidates:
            continue
        scores = []
        for row in candidates:
            _accepted, spur, saving = _stub_trial(ctx, struct, struct.rows, [], row)
            scores.append((-(saving - spur) if math.isfinite(spur) else math.inf, ctx.key(row), row))
        accepted_rows: list[int] = []
        for _, _, row in sorted(scores):
            if len(struct.rows) < 2:
                break
            ok, _, _ = _stub_trial(ctx, struct, struct.rows, accepted_rows, row)
            if ok:
                struct.rows = [r for r in struct.rows if r != row]
                accepted_rows.append(row)
        dec.stubs.extend((row, struct.ring_id) for row in accepted_rows)


def _stub_trial(ctx: DistrictContext, struct: Struct, rows: list[int], stubs: list[int], row: int
                ) -> tuple[bool, float, float]:
    """Whether ``row`` may leave ring ``rows`` as a stub (with the ring's accepted ``stubs``): (ok, spur, saving)."""
    p, d, t = ctx.p, ctx.model.dist_m, ctx.t_of_row
    i = rows.index(row)
    prev = rows[i - 1] if i > 0 else ctx.busbar_row
    nxt = rows[i + 1] if i < len(rows) - 1 else ctx.busbar_row
    saving = float(d[t[prev], t[row]] + d[t[row], t[nxt]] - d[t[prev], t[nxt]])
    if not math.isfinite(saving):
        return False, math.inf, saving
    net = Network(ctx)
    net.add_structure(Struct(struct.ring_id, "ring", [r for r in rows if r != row]))
    for stub in stubs:
        if not net.add_stub(stub, struct.ring_id, enforce=True):
            return False, math.inf, saving
    att = net.stub_attachment(row, struct.ring_id)
    if att is None:
        return False, math.inf, saving
    spur = float(att.dist)
    if not (spur > p.stub_min_offset_m and saving > spur):
        return False, spur, saving
    net.attach(row, att)
    check = net.ring_check(struct.ring_id)
    return bool(check.feasible), spur, saving


def _attach_satellites(ctx: DistrictContext, dec: Decisions) -> dict[str, int]:
    """Step 2b: the satellites of the distance model, nearest to the busbar (street) first.

    Each satellite hangs on a stub from the line nearest to it within ``stub_max_length_m`` when its ring (or
    radial) takes the stub within the stub limits and stays feasible. Otherwise it goes into the ring position that
    adds the least length and keeps the ring feasible (flag ``satellite_in_ring``), else into a ring of its own (flag
    ``satellite_own_ring``).

    Returns:
        How many satellites became stubs, went into a ring or got their own ring.
    """
    satellite = ctx.model.satellite
    stats = {"stub": 0, "ring": 0, "own_ring": 0}
    if satellite is None or not np.any(satellite):
        return stats
    rows = [int(ctx.terminals[t]) for t in np.flatnonzero(satellite)]
    rows.sort(key=lambda r: (float(ctx.model.street_m[0, ctx.t_of_row[r]]), ctx.key(r)))
    net = layout(ctx, dec)
    for row in rows:
        att = net.nearest(row, ctx.p.stub_max_length_m, net.searchable())
        struct = None if att is None else next((s for s in dec.structs
                                                 if s.ring_id == net.lines[att.line].ring_id), None)
        if struct is not None and struct.kind == "ring":         # radials are decided after the satellites
            stub = net.stub_attachment(row, struct.ring_id)
            if stub is not None:
                net.attach(row, stub)
                if net.ring_check(struct.ring_id).feasible:
                    dec.stubs.append((row, struct.ring_id))
                    stats["stub"] += 1
                    continue
                net = layout(ctx, dec)                 # drop the trial stub
        insertion = _cheapest_insertion(ctx, dec, row)
        if insertion is not None:
            insertion[0].rows = insertion[1]
            dec.flag(ctx, row, "satellite_in_ring")
            stats["ring"] += 1
        else:
            dec.structs.append(Struct(dec.next_ring_id(), "ring", [row]))
            dec.flag(ctx, row, "satellite_own_ring")
            stats["own_ring"] += 1
        net = layout(ctx, dec)
    return stats


def _cheapest_insertion(ctx: DistrictContext, dec: Decisions, row: int) -> tuple[Struct, list[int]] | None:
    """The ring and its new order with ``row`` inserted where it adds the least length and the ring (with its
    stubs) stays feasible; None if no ring takes it."""
    d, t = ctx.model.dist_m, ctx.t_of_row
    options = []
    for struct in dec.structs:
        if struct.kind != "ring":
            continue
        seq = [ctx.busbar_row, *struct.rows, ctx.busbar_row]
        for i in range(len(seq) - 1):
            a, b = seq[i], seq[i + 1]
            delta = float(d[t[a], t[row]] + d[t[row], t[b]] - d[t[a], t[b]])
            if math.isfinite(delta):
                options.append((delta, struct.ring_id, i, struct))
    for _, rid, i, struct in sorted(options, key=lambda o: o[:3]):
        rows = struct.rows[:i] + [row] + struct.rows[i:]
        trial = Network(ctx)
        trial.add_structure(Struct(rid, "ring", rows))
        for stub, stub_ring in dec.stubs:
            if stub_ring == rid:
                trial.add_stub(stub, rid, enforce=False)
        if trial.ring_check(rid).feasible:
            return struct, rows
    return None


def _decide_radials(ctx: DistrictContext, dec: Decisions, ring_ids: Sequence[int]) -> None:
    """Step 3: small rural single-member rings near the busbar become busbar radials.

    A radial has no reserve, so the node and the stubs on its ring form one stub tree: its coincident load at most
    ``stub_max_kva`` and every node of the tree within the stub length of that load from the busbar, a stub at the
    radial length plus its depth on the radial (a bound: it may branch off before the node).
    """
    p = ctx.p
    lengths = ctx.model.length_m if ctx.model.length_m is not None else ctx.model.street_m
    for struct in dec.structs:
        if struct.ring_id not in set(ring_ids) or struct.kind != "ring" or len(struct.rows) != 1:
            continue
        row = struct.rows[0]
        if ctx.urban[row] or ctx.kind[row] not in LOAD_KINDS:
            continue
        stubs = [r for r, rid in dec.stubs if rid == struct.ring_id]
        load = ctx.coincident_kva([row] + stubs)
        limit = ctx.stub_limit_m(load)
        reach = float(lengths[0, ctx.t_of_row[row]])
        if load > p.stub_max_kva or reach > limit:
            continue
        if stubs:
            net = Network(ctx)
            net.add_structure(Struct(struct.ring_id, "radial", [row]))
            for stub in stubs:
                net.add_stub(stub, struct.ring_id, enforce=False)
            if reach + max(net.depth.get(ctx.key(r), math.inf) for r in stubs) > limit:
                continue
        struct.kind = "radial"
