"""The repair loop of one MV district: the voltage ladder, the loading rules and the paths of the switching states,
on the districts of the electrical tests with a fake topology."""
from __future__ import annotations

import dataclasses
import importlib
import math

import pytest
from test_mv_electrical import BUSBAR, Builder

from pylovo.mv_grid import repair
from pylovo.mv_grid.model import RingPlan, Violation


class FakeTopology:
    """Stubs of topology.move_nop, split_ring, upgrade_lines and add_parallel that record their calls."""

    def __init__(self, moved: bool = True):
        self.moved = moved
        self.calls: list[tuple] = []

    def move_nop(self, ctx, plan, ring_id):
        self.calls.append(("move_nop", ring_id))
        return plan, self.moved

    def split_ring(self, ctx, ev, plan, ring_id):
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
        monkeypatch.setattr(repair, "_topology", lambda: fake)
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
    plan, actions = repair.repair(inp, None, None, plan, [low])
    assert [a["action"] for a in actions] == ["move_nop"] and actions[0]["round"] == 1
    plan, actions = repair.repair(inp, None, None, plan, [low])
    assert [a["action"] for a in actions] == ["split_ring"] and actions[0]["new_ring_ids"] == [2]
    assert plan.diagnostics["repair_stage"] == {"1": 2, "2": 2}
    plan, actions = repair.repair(inp, None, None, plan, [low])
    exits = sorted(line.key for line in plan.lines if line.exit in ("a", "b"))
    assert [a["action"] for a in actions] == ["parallel"] and actions[0]["line_keys"] == exits
    assert all(line.parallel == 2 for line in plan.lines if line.key in exits)
    plan, actions = repair.repair(inp, None, None, plan, [low])
    assert actions == [] and plan.diagnostics["repair_reports"][0]["action"] == "unresolved_voltage"
    assert plan.diagnostics["repair_round"] == 4
    assert [c[0] for c in fake.calls] == ["move_nop", "split_ring", "add_parallel"]


def _types_of(plan, keys):
    by_key = {line.key: line.std_type for line in plan.lines}
    return [by_key[k] for k in keys]


def test_path_lines_follow_the_switching_state_of_the_case():
    b = Builder()
    b.ring(6, set_exits=True)
    inp, plan = b.build()
    m = plan.rings[0].members
    keys = {frozenset((ln.from_key, ln.to_key)): ln.key for ln in plan.lines}
    a_half = [keys[frozenset(p)] for p in [(BUSBAR, m[0]), (m[0], m[1]), (m[1], m[2])]]
    assert repair.path_lines(plan, BUSBAR, m[2], "load") == a_half
    # exit A out: m[2] is fed around the ring from exit B through the closed open point
    around = repair.path_lines(plan, BUSBAR, m[2], "n1a")
    assert around[0] == keys[frozenset((BUSBAR, m[5]))] and len(around) == 4
    assert repair.path_lines(plan, BUSBAR, 999, "load") == []


def test_repair_upgrades_an_overloaded_overhead_section_before_splitting(fake_topology):
    fake = fake_topology()
    b = Builder()
    r = b.ring(6, kind="overhead", set_exits=True)
    inp, plan = b.build()
    exit_key = next(line.key for line in plan.lines if line.exit == "a")
    load = _violation("load", "loading", "line", exit_key, r, 70.0, 60.0)
    plan, actions = repair.repair(inp, None, None, plan, [load])
    # 410 A x 70/60 = 478 A: the smallest overhead line that carries it is 184-AL1 (535 A)
    assert [a["action"] for a in actions] == ["upgrade"] and actions[0]["line_keys"] == [exit_key]
    assert actions[0]["std_types"] == ["184-AL1/30-ST1A 20.0"] and actions[0]["reason"] == "loading"
    plan, actions = repair.repair(inp, None, None, plan, [load])
    assert _types_of(plan, [exit_key]) == ["243-AL1/39-ST1A 20.0"]
    plan, actions = repair.repair(inp, None, None, plan, [load])    # the largest: split the ring
    assert [a["action"] for a in actions] == ["split_ring"]
    assert [c[0] for c in fake.calls] == ["upgrade_lines", "upgrade_lines", "split_ring"]


def test_repair_voltage_ladder_upgrades_the_path_before_splitting(fake_topology):
    fake_topology(moved=True)
    b = Builder()
    r = b.ring(6, kind="overhead", set_exits=True)
    inp, plan = b.build()
    target = plan.rings[0].members[2]
    path = repair.path_lines(plan, BUSBAR, target, "load")
    low = _violation("load", "voltage_low", "bus", target, r, 0.96, 0.97)
    names = []
    for _ in range(4):
        plan, actions = repair.repair(inp, None, None, plan, [low])
        names.append([a["action"] for a in actions])
        if actions and actions[0]["action"] == "upgrade":
            assert actions[0]["line_keys"] == path and actions[0]["reason"] == "voltage"
    assert names == [["move_nop"], ["upgrade"], ["upgrade"], ["upgrade"]]
    assert set(_types_of(plan, path)) == {"243-AL1/39-ST1A 20.0"}
    plan, actions = repair.repair(inp, None, None, plan, [low])
    assert [a["action"] for a in actions] == ["split_ring"]


def test_repair_upgrades_the_path_at_once_when_the_nop_cannot_move(fake_topology):
    fake = fake_topology(moved=False)
    b = Builder()
    r = b.ring(6, kind="overhead")
    inp, plan = b.build()
    target = plan.rings[0].members[1]
    low = _violation("load", "voltage_low", "bus", target, r, 0.96, 0.97)
    plan, actions = repair.repair(inp, None, None, plan, [low])
    assert [c[0] for c in fake.calls] == ["move_nop", "upgrade_lines"]
    assert [a["action"] for a in actions] == ["upgrade"]
    assert plan.diagnostics["repair_stage"] == {"1": 1}


def test_repair_splits_at_once_when_the_nop_cannot_move(fake_topology):
    fake = fake_topology(moved=False)
    b = Builder()
    r = b.ring(6)
    inp, plan = b.build()
    low = _violation("n1a", "voltage_low", "bus", plan.rings[0].members[0], r, 0.91, 0.92)
    plan, actions = repair.repair(inp, None, None, plan, [low])
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
    plan2, actions = repair.repair(inp, None, None, plan, [
        _violation("load", "loading", "line", exit_key, r, 70.0, 60.0),
        _violation("n1a", "loading", "line", legs[1], rd, 120.0, 100.0)])
    assert sorted(a["action"] for a in actions) == ["parallel", "split_ring"]
    assert fake.calls == [("add_parallel", tuple(legs)), ("split_ring", r)]   # parallel cables before structure
    parallel = next(a for a in actions if a["action"] == "parallel")
    assert parallel["line_keys"] == legs and parallel["reason"] == "dedicated"
    # an overloaded middle section gets a parallel cable, up to the cap
    plan3, actions = repair.repair(inp, None, None, plan, [
        _violation("load", "loading", "line", middle_key, r, 70.0, 60.0)])
    assert [a["action"] for a in actions] == ["parallel"] and actions[0]["line_keys"] == [middle_key]
    capped = dataclasses.replace(plan, lines=[dataclasses.replace(line, parallel=2) for line in plan.lines],
                                 diagnostics={})
    plan4, actions = repair.repair(inp, None, None, capped, [
        _violation("load", "loading", "line", middle_key, r, 70.0, 60.0),
        _violation("load", "loading", "line", legs[0], rd, 70.0, 60.0)])
    assert actions == []
    assert sorted(item["action"] for item in plan4.diagnostics["repair_reports"]) == [
        "dedicated_overload", "unresolved_loading"]
    # non-convergence is only reported
    plan5, actions = repair.repair(inp, None, None, plan, [
        _violation("n1b", "not_converged", "bus", BUSBAR, None, math.nan, math.nan)])
    assert actions == [] and plan5.diagnostics["repair_reports"][0]["action"] == "not_converged"


def test_repair_falls_back_to_exit_cables_when_a_split_changes_nothing(fake_topology):
    fake = fake_topology(moved=False)
    fake.split_ring = lambda ctx, ev, plan, ring_id: (fake.calls.append(("split_ring", ring_id)), plan)[1]
    b = Builder()
    r = b.ring(6, set_exits=True)
    inp, plan = b.build()
    low = _violation("load", "voltage_low", "bus", plan.rings[0].members[-1], r, 0.96, 0.97)
    plan, actions = repair.repair(inp, None, None, plan, [low])
    assert [c[0] for c in fake.calls] == ["move_nop", "split_ring", "add_parallel"]
    assert [a["action"] for a in actions] == ["parallel"] and plan.diagnostics["repair_stage"] == {"1": 3}
