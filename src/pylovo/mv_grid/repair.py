"""The repair loop of one MV district: the actions of one round for the violations of the electrical check.

:func:`repair` runs one round: it sorts the violations by the feeder they hit (:func:`classify`), decides per kind
without changing the plan (:func:`decide_ring_voltage`, :func:`decide_ring_loading`, :func:`decide_feeders`) and
applies the decisions afterwards (:func:`apply_upgrades`, :func:`apply_parallels`, :func:`apply_structural`), with
the topology changes of :mod:`~pylovo.mv_grid.topology`. The planner
(:func:`~pylovo.mv_grid.planner.plan_district`) runs up to ``repair_rounds`` rounds and checks the plan again after
each. It is the MV counterpart of :mod:`pylovo.lv_grid.voltage_repair`.

Pure module: no database, configuration or plotting import; the topology is imported on first use.
"""
from __future__ import annotations

import importlib
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from pylovo.mv_grid import electrical, equipment
from pylovo.mv_grid.model import DistrictInput, LinePlan, LineType, RingPlan, TopologyPlan, Violation


def _topology() -> Any:
    """The topology module (imported on first use)."""
    return importlib.import_module("pylovo.mv_grid.topology")


# ---------------------------------------------------------------------------------------------- helpers
def _json(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    return value


def _record(round_no: int, name: str, violation: Violation | None, **extra: Any) -> dict:
    record: dict[str, Any] = {"round": round_no, "action": name}
    if violation is not None:
        record.update(case=violation.case, violation=violation.kind, element=violation.element,
                      key=int(violation.key), value=_json(violation.value), limit=_json(violation.limit))
    record.update({k: ([_json(v) for v in value] if isinstance(value, (list, tuple)) else _json(value))
                   for k, value in extra.items()})
    return record


def _member_count(inp: DistrictInput, members: Sequence[int]) -> int:
    nodes = inp.nodes
    return sum(1 for m in members if int(m) in nodes.index and nodes.at[int(m), "kind"] in ("station", "customer"))


def _parallel(plan: TopologyPlan) -> dict[int, int]:
    return {int(line.key): int(line.parallel) for line in plan.lines}


def _signature(plan: TopologyPlan) -> tuple:
    """What a structural repair changes: rings, lines (ends, role, parallels, open state) and node roles."""
    return (tuple((int(r.ring_id), r.kind, tuple(int(m) for m in r.members), r.nop_key) for r in plan.rings),
            tuple((int(line.key), int(line.from_key), int(line.to_key), line.role, int(line.parallel),
                   bool(line.normally_open), line.open_at_key) for line in plan.lines),
            tuple(sorted((int(k), str(v)) for k, v in plan.node_role.items())))


def path_lines(plan: TopologyPlan, busbar: int, target: int, case: str) -> list[int]:
    """Keys of the lines from the busbar to node ``target`` in the switching state of ``case``: the normally-open
    lines open in normal operation (``load``), closed in ``n1a``/``n1b`` with the A or B exits out.
    Empty if ``target`` is not reached."""
    out = electrical.N1_EXIT.get(case)
    exits = electrical.ring_exits(plan, busbar) if out is not None else {}
    neighbours: dict[int, list[tuple[int, int]]] = {}
    for line in electrical.net_lines(plan):
        if (line.normally_open and out is None) or (out is not None and exits.get(int(line.key)) == out):
            continue
        a, b = int(line.from_key), int(line.to_key)
        neighbours.setdefault(a, []).append((b, int(line.key)))
        neighbours.setdefault(b, []).append((a, int(line.key)))
    parent: dict[int, tuple[int, int] | None] = {busbar: None}
    queue = deque([busbar])
    while queue and target not in parent:
        node = queue.popleft()
        for other, key in sorted(neighbours.get(node, ())):
            if other not in parent:
                parent[other] = (node, key)
                queue.append(other)
    if target not in parent:
        return []
    keys = []
    node = target
    while parent[node] is not None:
        node, key = parent[node]
        keys.append(key)
    return keys[::-1]


def _upgradable(line: LinePlan, vn_kv: float) -> bool:
    """Whether a larger conductor of the same kind exists for some part of the line (no type name: no)."""
    for part in (line.std_type or "").split(" + "):
        try:
            if part.strip() and equipment.upgraded_line_type(part.strip(), vn_kv) is not None:
                return True
        except ValueError:
            continue
    return False


def _component_rings(plan: TopologyPlan, components: Mapping[int, int]) -> dict[int, int]:
    """Ring id per component label, from the lines that carry a ring id (the first one by line key)."""
    rings: dict[int, int] = {}
    for line in sorted(plan.lines, key=lambda item: item.key):
        if line.role == "tie" or line.ring_id is None:
            continue
        for key in (line.from_key, line.to_key):
            label = components.get(int(key))
            if label is not None:
                rings.setdefault(label, int(line.ring_id))
    return rings


# ---------------------------------------------------------------------------------------------- one round
@dataclass
class RepairRound:
    """One round of the repair loop: the plan as the actions change it, the step of the voltage ladder per ring,
    the requests of the decisions and the records of the round."""

    inp: DistrictInput
    ctx: Any                               # the district context of the topology
    ev: Any                                # the ring evaluator of the solver
    plan: TopologyPlan                     # changed by the actions
    round_no: int
    stage: dict[int, int]                  # ring id -> step of the voltage ladder reached
    vn_kv: float
    types: tuple[LineType, LineType]
    busbar: int
    rings: dict[int, RingPlan]             # of the plan the round starts from
    lines: dict[int, LinePlan]             # of the plan the round starts from
    exits: dict[int, str]                  # of the plan the round starts from
    cache: dict[str, LineType] = field(default_factory=dict)
    touched: set[int] = field(default_factory=set)        # rings with a decision in this round
    upgrades: list[tuple[list[int], int | None, Violation, str, dict[int, float] | None]] = field(default_factory=list)
    parallels: list[tuple[list[int], int | None, Violation, str]] = field(default_factory=list)
    structural: list[tuple[str, int, Violation]] = field(default_factory=list)   # ("move_nop" | "split_ring", ring)
    reports: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    @classmethod
    def of(cls, inp: DistrictInput, ctx: Any, ev: Any, plan: TopologyPlan) -> "RepairRound":
        busbar = electrical.busbar_key(inp)
        return cls(inp=inp, ctx=ctx, ev=ev, plan=plan, round_no=int(plan.diagnostics.get("repair_round", 0)) + 1,
                   stage={int(k): int(v) for k, v in (plan.diagnostics.get("repair_stage") or {}).items()},
                   vn_kv=float(inp.vn_kv), types=electrical.line_types(inp), busbar=busbar,
                   rings={int(r.ring_id): r for r in plan.rings}, lines={int(line.key): line for line in plan.lines},
                   exits=electrical.ring_exits(plan, busbar))

    def upgradable(self, keys: Iterable[int], current: TopologyPlan | None = None) -> list[int]:
        """The keys among ``keys`` whose line can get a larger conductor (lines of ``current``, else of the plan
        the round starts from)."""
        by_key = self.lines if current is None else {int(line.key): line for line in current.lines}
        return [int(k) for k in keys if int(k) in by_key and _upgradable(by_key[int(k)], self.vn_kv)]

    def needed_ka(self, key: int, v: Violation) -> float:
        """Ampacity per circuit that carries the current of an overloaded line within the limit."""
        return electrical.line_parameters(self.lines[key], self.types, self.cache)[3] * float(v.value) / max(
            float(v.limit), 1e-9)

    def exit_lines(self, ring_id: int) -> list[int]:
        """The two busbar sections of ring ``ring_id``."""
        return sorted(k for k, line in self.lines.items()
                      if line.ring_id is not None and int(line.ring_id) == ring_id and self.exits.get(k) in ("a", "b"))

    def report(self, name: str, v: Violation | None, **extra: Any) -> None:
        self.reports.append(_record(self.round_no, name, v, **extra))

    def act(self, name: str, v: Violation | None, **extra: Any) -> None:
        self.actions.append(_record(self.round_no, name, v, **extra))


def repair(inp: DistrictInput, ctx: Any, ev: Any, plan: TopologyPlan, violations: Sequence[Violation]
           ) -> tuple[TopologyPlan, list[dict]]:
    """One round of the repair loop: applies all non-conflicting actions, at most one per ring.

    Larger conductors come before topology changes, as in Ding0's reinforcement (``topology.upgrade_lines``, the
    pandapower conductors of the kind by ampacity, :func:`pylovo.mv_grid.equipment.upgrade_ladder`). The violations
    are sorted by the feeder they hit (:func:`classify`), each kind decides its actions without changing the plan
    (:func:`decide_ring_voltage`, :func:`decide_ring_loading`, :func:`decide_feeders`), and the actions are applied
    afterwards: larger conductors and parallel cables first, while the line keys are still valid
    (:func:`apply_upgrades`, :func:`apply_parallels`), then the open points and splits (:func:`apply_structural`).

    1. Voltage (``load``, ``n1a``, ``n1b``) on a node of ring r, a ladder over the rounds: move the NOP to the best
       feasible position (``topology.move_nop``); if it does not move (or moved in an earlier round and the ring
       still violates): the next larger conductor on every line of the path from the busbar to the node in that
       case (:func:`path_lines`), repeated while a line can be upgraded; then split the ring
       (``topology.split_ring``, rings with at least 2 members); then ``parallel`` + 1 on the first section of each
       half (the two exits). Rings made by a split continue at the last step.
    2. Loading (``load``, ``n1*``) on a section of ring r: the smallest conductor that carries the current within
       the limit; at the largest conductor, split the ring if the section is a busbar exit and the ring has at least
       4 members, else ``parallel`` + 1 (up to ``max_parallel_cables``).
    3. Loading or voltage on a dedicated feeder or a busbar radial: larger conductors (the overloaded line, or all
       lines of the feeder for voltage), then ``parallel`` + 1 on its lines (up to the cap), else the report
       ``dedicated_overload``.
    4. ``not_converged``: report, the topology is kept.

    Args:
        inp: The district input.
        ctx: Its context of the topology (:class:`pylovo.mv_grid.topology.DistrictContext`).
        ev: The ring evaluator of the solver.
        plan: The current plan.
        violations: The violations of :func:`pylovo.mv_grid.electrical.check` for ``plan``.

    Returns:
        ``(plan, actions)``: the repaired plan and one JSON-native record per applied action (``round``,
        ``action``, the triggering violation, ``ring_id``/``node_key``/``line_keys``). An empty list means nothing
        could be repaired. Reports of violations that are not repaired are kept in
        ``plan.diagnostics["repair_reports"]`` (the latest round), the ladder state per ring in
        ``plan.diagnostics["repair_stage"]`` and the round count in ``plan.diagnostics["repair_round"]``.
    """
    state = RepairRound.of(inp, ctx, ev, plan)
    ring_voltage, ring_loading, feeders = classify(state, violations)
    decide_ring_voltage(state, ring_voltage)
    decide_ring_loading(state, ring_loading)
    decide_feeders(state, feeders)
    apply_upgrades(state)
    apply_parallels(state)
    apply_structural(state)
    plan = state.plan
    plan.diagnostics["repair_round"] = state.round_no
    plan.diagnostics["repair_stage"] = {str(k): v for k, v in sorted(state.stage.items())}
    plan.diagnostics["repair_reports"] = state.reports
    return plan, state.actions


# ---------------------------------------------------------------------------------------------- classify
def classify(state: RepairRound, violations: Sequence[Violation]
             ) -> tuple[dict[int, Violation], dict[int, list[tuple[int, Violation]]], dict[int, Violation]]:
    """The violations by the feeder they hit.

    Returns:
        ``(ring_voltage, ring_loading, feeders)``: the first voltage violation per ring, the overloaded sections per
        ring (line key and violation) and the first violation per dedicated feeder or busbar radial. A case that did
        not converge and a violation on no feeder are reported (``not_converged``, ``unassigned``).
    """
    plan, busbar, rings = state.plan, state.busbar, state.rings
    n1 = electrical.components(electrical.bus_keys(state.inp, plan), busbar, electrical.net_lines(plan),
                               include_open=True)
    ring_of_component = _component_rings(plan, n1)

    def ring_of_node(key: int) -> int | None:
        ring = plan.node_ring.get(key)
        if ring is not None:
            return int(ring)
        label = n1.get(key)
        return ring_of_component.get(label) if label is not None else None

    def ring_of_line(line: LinePlan) -> int | None:
        if line.ring_id is not None:
            return int(line.ring_id)
        for key in (line.from_key, line.to_key):
            if int(key) != busbar:
                return ring_of_node(int(key))
        return None

    def feeder_kind(ring_id: int | None, line: LinePlan | None = None) -> str:
        if ring_id is not None and ring_id in rings:
            return rings[ring_id].kind
        if line is not None and line.role == "dedicated":
            return line.role
        return "radial"

    ring_voltage: dict[int, Violation] = {}
    ring_loading: dict[int, list[tuple[int, Violation]]] = {}
    feeders: dict[int, Violation] = {}                     # ring id (non-ring feeders) -> first violation
    for v in violations:
        if v.kind == "not_converged":
            state.report("not_converged", v)
            continue
        if v.element == "bus":
            ring_id = v.ring_id if v.ring_id is not None else ring_of_node(int(v.key))
            line = None
        else:
            line = state.lines.get(int(v.key))
            if line is None:
                continue
            ring_id = v.ring_id if v.ring_id is not None else ring_of_line(line)
        kind = feeder_kind(ring_id, line)
        if kind == "ring":
            if v.element == "bus":
                ring_voltage.setdefault(int(ring_id), v)
            else:
                ring_loading.setdefault(int(ring_id), []).append((int(v.key), v))
        elif ring_id is not None:
            feeders.setdefault(int(ring_id), v)
        else:
            state.report("unassigned", v)
    return ring_voltage, ring_loading, feeders


# ---------------------------------------------------------------------------------------------- decide
def decide_ring_voltage(state: RepairRound, ring_voltage: Mapping[int, Violation]) -> None:
    """The next step of the voltage ladder of every ring with a voltage violation (step 1 of :func:`repair`)."""
    for ring_id, v in sorted(ring_voltage.items()):
        ring = state.rings.get(ring_id)
        if ring is None:
            continue
        level = state.stage.get(ring_id, 0)
        if level < 1:
            state.stage[ring_id] = 1
            state.structural.append(("move_nop", ring_id, v))
            state.touched.add(ring_id)
            continue
        path = state.upgradable(path_lines(state.plan, state.busbar, int(v.key), v.case)) if level < 2 else []
        if path:
            state.upgrades.append((path, ring_id, v, "voltage", None))
            state.touched.add(ring_id)
            continue
        if level < 2 and _member_count(state.inp, ring.members) >= 2:
            state.stage[ring_id] = 2
            state.structural.append(("split_ring", ring_id, v))
            state.touched.add(ring_id)
            continue
        if level < 3:
            state.stage[ring_id] = 3
            state.parallels.append((state.exit_lines(ring_id), ring_id, v, "voltage"))
            state.touched.add(ring_id)
            continue
        state.report("unresolved_voltage", v, ring_id=ring_id)


def decide_ring_loading(state: RepairRound, ring_loading: Mapping[int, list[tuple[int, Violation]]]) -> None:
    """Larger conductors, a split or a parallel cable for the overloaded sections of every ring whose voltage
    needs no action (step 2 of :func:`repair`)."""
    for ring_id, items in sorted(ring_loading.items()):
        ring = state.rings.get(ring_id)
        if ring is None or ring_id in state.touched:
            continue
        larger = {k: state.needed_ka(k, v) for k, v in items if state.upgradable([k])}
        if larger:
            state.upgrades.append((list(larger), ring_id, items[0][1], "loading", larger))
            state.touched.add(ring_id)
            continue
        on_exit = [(k, v) for k, v in items if state.exits.get(k) in ("a", "b")]
        if on_exit and _member_count(state.inp, ring.members) >= 4:
            state.structural.append(("split_ring", ring_id, on_exit[0][1]))
            state.stage[ring_id] = max(state.stage.get(ring_id, 0), 2)
        else:
            state.parallels.append(([k for k, _ in items], ring_id, items[0][1], "loading"))
        state.touched.add(ring_id)


def decide_feeders(state: RepairRound, feeders: Mapping[int, Violation]) -> None:
    """Larger conductors or parallel cables for dedicated feeders and busbar radials (step 3 of :func:`repair`)."""
    for ring_id, v in sorted(feeders.items()):
        if ring_id in state.touched:
            continue
        keys_of_feeder = sorted(k for k, line in state.lines.items()
                                if line.ring_id is not None and int(line.ring_id) == ring_id)
        state.touched.add(ring_id)
        if v.element == "line":
            larger = {int(v.key): state.needed_ka(int(v.key), v)} if state.upgradable([int(v.key)]) else {}
        else:
            larger = {k: None for k in state.upgradable(keys_of_feeder)}
        if larger:
            state.upgrades.append((list(larger), ring_id, v, "dedicated", larger if v.element == "line" else None))
            continue
        state.parallels.append((keys_of_feeder, ring_id, v, "dedicated"))


# ---------------------------------------------------------------------------------------------- apply
def _upgrade(state: RepairRound, line_keys: list[int], ring_id: int | None, v: Violation, reason: str,
             need: dict[int, float] | None) -> bool:
    """Larger conductors on ``line_keys`` (``need``: the ampacity per line); whether any line changed."""
    state.plan, changed = _topology().upgrade_lines(state.plan, line_keys, state.vn_kv,
                                                    None if need is None else {k: x for k, x in need.items()
                                                                               if x is not None})
    if changed:
        names = sorted({line.std_type for line in state.plan.lines if int(line.key) in set(changed)})
        state.act("upgrade", v, ring_id=ring_id, line_keys=changed, reason=reason, std_types=names)
    return bool(changed)


def _add_parallel(state: RepairRound, line_keys: list[int]) -> list[int]:
    """One more cable on ``line_keys`` (up to ``max_parallel_cables``); the keys that got one."""
    before = _parallel(state.plan)
    if line_keys:
        state.plan, _ = _topology().add_parallel(state.plan, line_keys, int(state.inp.params.max_parallel_cables))
    after = _parallel(state.plan)
    return [k for k in line_keys if after.get(k, 0) > before.get(k, 0)]


def apply_upgrades(state: RepairRound) -> None:
    """The larger conductors decided in this round."""
    for line_keys, ring_id, v, reason, need in state.upgrades:
        if not _upgrade(state, line_keys, ring_id, v, reason, need):
            state.report("unresolved_" + reason, v, ring_id=ring_id, line_keys=line_keys)


def apply_parallels(state: RepairRound) -> None:
    """The parallel cables decided in this round."""
    for line_keys, ring_id, v, reason in state.parallels:
        changed = _add_parallel(state, line_keys)
        if changed:
            state.act("parallel", v, ring_id=ring_id, line_keys=changed, reason=reason)
        else:
            state.report("dedicated_overload" if reason == "dedicated" else "unresolved_" + reason, v,
                         ring_id=ring_id, line_keys=line_keys)


def _exit_parallel(state: RepairRound, target: int, v: Violation) -> None:
    """Last step of the voltage ladder: one more cable on the two exits of ring ``target``."""
    state.stage[target] = 3
    current_exits = electrical.ring_exits(state.plan, state.busbar)
    line_keys = sorted(int(line.key) for line in state.plan.lines if line.ring_id is not None
                       and int(line.ring_id) == target and current_exits.get(int(line.key)) in ("a", "b"))
    changed = _add_parallel(state, line_keys)
    if changed:
        state.act("parallel", v, ring_id=target, line_keys=changed, reason="voltage")
    else:
        state.report("unresolved_voltage", v, ring_id=target)


def apply_structural(state: RepairRound) -> None:
    """The open points and ring splits decided in this round; a NOP that does not move falls back to the next
    step of the voltage ladder, a split that changes nothing to the exit cables."""
    topology = _topology()
    for name, target, v in state.structural:
        ring_ids = {int(r.ring_id) for r in state.plan.rings}
        if name in ("move_nop", "split_ring") and target not in ring_ids:
            continue
        if name == "move_nop":
            new_plan, moved = topology.move_nop(state.ctx, state.plan, target)
            if moved:
                state.plan = new_plan
                state.act("move_nop", v, ring_id=target)
                continue
            path = state.upgradable(path_lines(state.plan, state.busbar, int(v.key), v.case), state.plan)
            if path and _upgrade(state, path, target, v, "voltage", None):
                continue                        # the open point is already at its best position
            ring = next(r for r in state.plan.rings if int(r.ring_id) == target)
            if _member_count(state.inp, ring.members) < 2:
                _exit_parallel(state, target, v)
                continue
            name = "split_ring"                 # the open point is already at its best position
        if name == "split_ring":
            before = _signature(state.plan)
            new_plan = topology.split_ring(state.ctx, state.ev, state.plan, target)
            if _signature(new_plan) == before:  # the ring cannot be split (fewer than two members)
                _exit_parallel(state, target, v)
                continue
            state.plan = new_plan
            new_ids = {int(r.ring_id) for r in state.plan.rings}
            for ring_id in new_ids - ring_ids:
                state.stage[ring_id] = 2
            if target in new_ids:
                state.stage[target] = 2
            else:
                state.stage.pop(target, None)
            state.act("split_ring", v, ring_id=target, new_ring_ids=sorted(new_ids - ring_ids))
