"""Tests of the MV ring solver (savings, local search) with a small fake ring evaluator."""

import itertools
import json
import math
import time

import numpy as np
import pytest

from pylovo.mv_grid import solvers as S
from pylovo.mv_grid.model import RingCheck, RingLimits, SolverResult

INF = math.inf


class FakeEvaluator:
    """Ring evaluator with the structure of the real one (``RingEvaluatorProtocol``).

    A ring is feasible iff its member count is within the cap, every section is finite, the ring load is within
    ``max_load``, the (n-1) "drop" (Σ section length × load downstream, whole ring fed from either side) is within
    ``max_drop_n1``, and some open section gives both halves a drop within ``max_drop`` and a length within
    ``max_half_m``. The drops depend on the ring order, like the voltage drops of the real evaluator.
    """

    def __init__(self, dist, load, *, member=None, max_members=20, max_load=INF, max_drop=INF, max_drop_n1=INF,
                 max_half_m=INF, limits=False):
        self.dist = np.asarray(dist, dtype=float)
        self.load = np.asarray(load, dtype=float)
        self.member = np.ones(len(self.load), bool) if member is None else np.asarray(member, bool)
        self.max_members, self.max_load = max_members, max_load
        self.max_drop, self.max_drop_n1, self.max_half_m = max_drop, max_drop_n1, max_half_m
        if limits:
            self.limits = RingLimits(max_members=max_members, max_drop=0.05, max_drop_n1=0.1, utilization=0.6,
                                     n1_utilization=1.0, max_half_m=max_half_m)
        self.calls = 0
        self.seen = []
        self._memo = {}

    def member_count(self, seq):
        return int(self.member[list(seq)].sum())

    def ring_length(self, seq):
        return length([list(seq)], self.dist)

    def evaluate(self, seq):
        self.calls += 1
        seq = [int(t) for t in seq]
        self.seen.append(seq)
        key = min(tuple(seq), tuple(reversed(seq)))
        if key not in self._memo:
            self._memo[key] = self._check(list(key))
        return self._memo[key]

    @staticmethod
    def _result(feasible, reason, length_m=0.0, m=1):
        return RingCheck(feasible=feasible, open_section=m, nop_position=m, length_m=length_m, half_a_m=0.0,
                         half_b_m=0.0, drop_a=0.0, drop_b=0.0, drop_n1=0.0, util_normal=0.0, util_n1=0.0,
                         p_ring_kw=0.0, p_a_kw=0.0, p_b_kw=0.0, reason=reason)

    def _check(self, seq):
        k = len(seq)
        ext = [0, *seq, 0]
        sec = np.array([self.dist[a, b] for a, b in zip(ext, ext[1:])])
        loads = self.load[seq]
        if self.member_count(seq) > self.max_members:
            return self._result(False, "count")
        if not np.isfinite(sec).all():
            return self._result(False, "unreachable")
        total = float(sec.sum())
        if loads.sum() > self.max_load:
            return self._result(False, "current", total)
        n1_a = sum(sec[j] * loads[j:].sum() for j in range(k))
        n1_b = sum(sec[j] * loads[:j].sum() for j in range(1, k + 1))
        if max(n1_a, n1_b) > self.max_drop_n1:
            return self._result(False, "n1_drop", total)
        for m in range(1, k) if k >= 2 else [1]:
            drop_a = sum(sec[j] * loads[j:m].sum() for j in range(m))
            drop_b = sum(sec[j] * loads[m:j].sum() for j in range(m + 1, k + 1))
            if (max(drop_a, drop_b) <= self.max_drop and sec[:m].sum() <= self.max_half_m
                    and sec[m + 1:].sum() <= self.max_half_m):
                return self._result(True, None, total, m)
        return self._result(False, "drop", total)


def length(routes, dist):
    """Total ring length with both busbar sections (reference implementation)."""
    total = 0.0
    for r in routes:
        ext = [0, *r, 0]
        total += sum(dist[a, b] for a, b in zip(ext, ext[1:]))
    return total


def euclid(xy, detour=1.0):
    xy = np.asarray(xy, dtype=float)
    return np.sqrt(((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1)) * detour


def settlement_instance(n_town, n_villages, per_village, seed):
    """Busbar at the origin, a town around it and villages around: the shape of a rural MV district."""
    rng = np.random.default_rng(seed)
    town = rng.normal([1000.0, 500.0], 1500.0, (n_town, 2))
    centres = rng.uniform(-12000.0, 12000.0, (n_villages, 2))
    villages = [rng.normal(c, 300.0, (per_village, 2)) for c in centres]
    xy = np.vstack([[[0.0, 0.0]], town, *villages])
    load = np.concatenate([[0.0], rng.uniform(100.0, 600.0, len(xy) - 1)])
    return xy, euclid(xy, 1.3), load


def uniform_instance(n, seed, size=20000.0):
    rng = np.random.default_rng(seed)
    xy = np.vstack([[[size / 2, size / 2]], rng.uniform(0.0, size, (n, 2))])
    load = np.concatenate([[0.0], rng.uniform(100.0, 600.0, n)])
    return xy, euclid(xy, 1.25), load


TIGHT = dict(max_members=12, max_load=4000.0, max_drop=4000.0 * 4000.0, max_drop_n1=8000.0 * 6000.0,
             max_half_m=15000.0)
REAL = dict(max_members=20, max_load=6000.0, max_drop=6000.0 * 6000.0, max_drop_n1=12000.0 * 8000.0,
            max_half_m=28000.0)


def assert_partition(routes, candidates):
    flat = [t for r in routes for t in r]
    assert sorted(flat) == sorted(candidates)
    assert all(isinstance(t, int) for t in flat)
    assert all(r for r in routes)


def assert_feasible(routes, dist, load, **limits):
    """Every ring is feasible, except singletons whose own ring is infeasible."""
    ev = FakeEvaluator(dist, load, **limits)
    for r in routes:
        assert ev.evaluate(r).feasible or (len(r) == 1 and not ev.evaluate(r).feasible), r


# --------------------------------------------------------------------------------------------------------------
# savings
# --------------------------------------------------------------------------------------------------------------
#: Six stations (km) around a busbar at the origin; with at most four members per ring the optimum needs a merge
#: of two two-station rings at their tails, i.e. a reversed ring (found by a seeded search, checked below).
SIX = [(-5, -4), (6, -8), (2, 5), (-6, -9), (-5, 3), (1, -7)]


def _brute_force_optimum(dist, n, cap):
    def partitions(items):
        if not items:
            yield []
            return
        first, rest = items[0], items[1:]
        for p in partitions(rest):
            yield [[first]] + p
            for k in range(len(p)):
                yield p[:k] + [[first] + p[k]] + p[k + 1:]

    def best_ring(block):
        return min(length([list(p)], dist) for p in itertools.permutations(block))

    return min(sum(best_ring(b) for b in p) for p in partitions(list(range(1, n + 1)))
               if all(len(b) <= cap for b in p))


def _savings_without_reversal(dist, nodes, cap):
    """Clarke-Wright that only concatenates rings (a tail to a head), as without route reversal."""
    routes = {t: [t] for t in nodes}
    route_of = {t: t for t in nodes}
    pairs = sorted(((dist[0, i] + dist[0, j] - dist[i, j], i, j) for i, j in itertools.combinations(nodes, 2)),
                   key=lambda s: (-s[0], s[1], s[2]))
    for s, i, j in pairs:
        ri, rj = route_of[i], route_of[j]
        if s <= 0 or ri == rj:
            continue
        a, b = routes[ri], routes[rj]
        if a[-1] == i and b[0] == j:
            merged = a + b
        elif b[-1] == j and a[0] == i:
            merged = b + a
        else:
            continue
        if len(merged) <= cap:
            routes[ri] = merged
            del routes[rj]
            for t in merged:
                route_of[t] = ri
    return list(routes.values())


def test_savings_reaches_the_known_optimum_of_six_nodes_with_route_reversal():
    xy = np.array([(0, 0), *SIX], dtype=float) * 1000.0
    dist = euclid(xy)
    optimum = _brute_force_optimum(dist, 6, cap=4)
    ev = FakeEvaluator(dist, np.ones(7), max_members=4)
    routes = S.solve_savings(ev, dist, range(1, 7))
    assert_partition(routes, range(1, 7))
    assert length(routes, dist) == pytest.approx(optimum, abs=1e-6)
    assert routes == [[1, 4, 6, 2], [3, 5]]
    # without reversal the same savings list ends 1.8 km longer
    assert length(_savings_without_reversal(dist, list(range(1, 7)), 4), dist) > optimum + 1000.0
    result = S.solve(FakeEvaluator(dist, np.ones(7), max_members=4), dist, range(1, 7))
    assert result.length_final_m == pytest.approx(optimum, abs=1e-6)


def test_savings_merges_a_line_into_one_ring_and_keeps_far_sides_apart():
    # two stations east and two west of the busbar: merging across the busbar saves nothing
    xy = np.array([(0, 0), (1000, 0), (2000, 0), (-1000, 0), (-2000, 0)], dtype=float)
    dist = euclid(xy)
    routes = S.solve_savings(FakeEvaluator(dist, np.ones(5)), dist, [4, 3, 2, 1])
    assert routes == [[1, 2], [3, 4]]


def test_savings_uses_only_finite_positive_pairs_and_keeps_infeasible_singletons():
    xy, dist, load = settlement_instance(10, 2, 5, seed=3)
    dist = dist.copy()
    dist[7, :] = dist[:, 7] = INF                      # station 7 cannot be reached
    dist[7, 7] = 0.0
    load[3] = 1e9                                       # station 3 is too heavy for any ring
    ev = FakeEvaluator(dist, load, **TIGHT)
    routes = S.solve_savings(ev, dist, range(1, len(xy)))
    assert_partition(routes, range(1, len(xy)))
    assert [7] in routes and [3] in routes
    assert_feasible(routes, dist, load, **TIGHT)


def test_savings_above_the_pair_limit_uses_near_neighbour_pairs(monkeypatch):
    xy, dist, load = settlement_instance(30, 4, 6, seed=9)
    nodes = list(range(1, len(xy)))
    all_i, all_j = S._savings_pairs(dist, nodes)
    monkeypatch.setattr(S, "SAVINGS_ALL_PAIRS_MAX", 10)
    monkeypatch.setattr(S, "SAVINGS_NEIGHBOURS", 5)
    near_i, near_j = S._savings_pairs(dist, nodes)
    near = set()
    for t in nodes:
        others = sorted((dist[t, o], o) for o in nodes if o != t)[:5]
        near |= {(min(t, o), max(t, o)) for _, o in others}
    assert set(zip(near_i, near_j)) == {p for p in zip(all_i, all_j) if p in near}
    # the processing order is the order of the full list restricted to these pairs
    assert list(zip(near_i, near_j)) == [p for p in zip(all_i, all_j) if p in near]
    routes = S.solve_savings(FakeEvaluator(dist, load, **REAL), dist, nodes)
    assert_partition(routes, nodes)
    assert_feasible(routes, dist, load, **REAL)


# --------------------------------------------------------------------------------------------------------------
# feasibility, local search
# --------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("make", [lambda s: settlement_instance(40, 6, 8, s), lambda s: uniform_instance(80, s)])
@pytest.mark.parametrize("seed", [0, 1])
def test_every_ring_respects_the_evaluator(make, seed):
    xy, dist, load = make(seed)
    candidates = list(range(1, len(xy)))
    ev = FakeEvaluator(dist, load, **TIGHT)
    result = S.solve(ev, dist, candidates)
    assert isinstance(result, SolverResult)
    assert_partition(result.routes, candidates)
    assert_feasible(result.routes, dist, load, **TIGHT)
    check = FakeEvaluator(dist, load, **TIGHT)
    infeasible = [r for r in result.routes if not check.evaluate(r).feasible]
    assert all(len(r) == 1 for r in infeasible)        # far villages beyond the half-ring limit
    assert result.diagnostics["solver_infeasible_rings"] == len(infeasible)
    assert result.length_final_m <= result.length_savings_m + 1e-6
    assert result.length_final_m == pytest.approx(length(result.routes, dist))
    # canonical form: first terminal below the last, rings sorted by their first terminal
    assert all(r[0] <= r[-1] for r in result.routes)
    assert [r[0] for r in result.routes] == sorted(r[0] for r in result.routes)


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_local_search_never_increases_the_length_and_keeps_every_node_once(seed):
    xy, dist, load = settlement_instance(30, 5, 6, seed)
    candidates = list(range(1, len(xy)))
    for start in (S.solve_savings(FakeEvaluator(dist, load, **TIGHT), dist, candidates),
                  [[t] for t in candidates]):
        ev = FakeEvaluator(dist, load, **TIGHT)
        routes, diagnostics = S.local_search(start, ev, dist)
        assert_partition(routes, candidates)
        assert_feasible(routes, dist, load, **TIGHT)
        assert length(routes, dist) <= length(start, dist) + 1e-6
        assert diagnostics["length_after_m"] == pytest.approx(length(routes, dist))
        assert diagnostics["moves"] == sum(diagnostics[f"moves_{op}"] for op in S.OPERATORS)
        assert diagnostics["ls_time_guard"] is False
    # from singletons the search has to merge rings: a large gain
    assert length(routes, dist) < 0.6 * length([[t] for t in candidates], dist)


def test_local_search_on_one_ring_only_reorders_it():
    xy, dist, load = settlement_instance(12, 0, 0, seed=5)
    ring = list(range(1, 13))                          # index order, far from the best order
    ev = FakeEvaluator(dist, load, max_members=12)
    routes, diagnostics = S.local_search([ring], ev, dist)
    assert len(routes) == 1 and sorted(routes[0]) == ring
    assert length(routes, dist) < length([ring], dist) - 1.0
    assert diagnostics["moves_exchange"] == diagnostics["moves_two_opt_star"] == 0


def test_move_deltas_equal_the_length_change_of_the_built_rings():
    rng = np.random.default_rng(7)
    for trial in range(60):
        n = int(rng.integers(2, 10))
        xy, dist, _ = uniform_instance(n, trial)
        order = [int(t) for t in rng.permutation(np.arange(1, n + 1))]
        cuts = sorted(int(c) for c in rng.choice(np.arange(1, n), size=min(2, n - 1), replace=False))
        routes = [order[a:b] for a, b in zip([0, *cuts], [*cuts, n]) if b > a]
        state = S._State(routes, n + 1)
        D = dist.tolist()
        for u, v in itertools.permutations(range(1, n + 1), 2):
            for op, delta, build in S._moves(state, D, u, v):
                changes = build()
                old = sum(length([state.routes[rid]], dist) for rid in changes)
                new = sum(length([r], dist) for r in changes.values())
                assert delta == pytest.approx(new - old, abs=1e-6), (op, routes, u, v)
                kept = [t for rid, r in enumerate(state.routes) if rid not in changes for t in r]
                assert sorted(kept + [t for r in changes.values() for t in r]) == list(range(1, n + 1))


# --------------------------------------------------------------------------------------------------------------
# solve(): switches, determinism, guard, size, errors
# --------------------------------------------------------------------------------------------------------------
def test_two_runs_are_identical():
    xy, dist, load = settlement_instance(60, 8, 10, seed=11)
    candidates = list(range(1, len(xy)))
    first = S.solve(FakeEvaluator(dist, load, **REAL), dist, candidates)
    second = S.solve(FakeEvaluator(dist, load, **REAL), dist, candidates[::-1])
    assert first.routes == second.routes
    assert first.length_final_m == second.length_final_m
    assert (first.passes, first.moves) == (second.passes, second.moves)
    timing = {k for k in first.diagnostics if k.endswith("_s")}
    assert ({k: v for k, v in first.diagnostics.items() if k not in timing}
            == {k: v for k, v in second.diagnostics.items() if k not in timing})


def test_rounds_zero_returns_the_construction():
    xy, dist, load = settlement_instance(40, 6, 8, seed=2)
    candidates = list(range(1, len(xy)))
    result = S.solve(FakeEvaluator(dist, load, **REAL), dist, candidates, rounds=0)
    assert result.routes == S.solve_savings(FakeEvaluator(dist, load, **REAL), dist, candidates)
    assert (result.passes, result.moves) == (0, 0)
    assert result.length_final_m == result.length_savings_m
    assert result.diagnostics["solver_gain_percent"] == 0.0


def test_time_guard_flags_and_keeps_a_feasible_solution():
    xy, dist, load = settlement_instance(30, 5, 6, seed=1)
    candidates = list(range(1, len(xy)))
    singletons = [[t] for t in candidates]
    routes, diagnostics = S.local_search(singletons, FakeEvaluator(dist, load, **TIGHT), dist, time_guard_s=1e-9)
    assert diagnostics["ls_time_guard"] is True
    assert diagnostics["passes"] == 1 and diagnostics["moves"] <= 1
    assert_partition(routes, candidates)
    assert_feasible(routes, dist, load, **TIGHT)
    result = S.solve(FakeEvaluator(dist, load, **TIGHT), dist, candidates, time_guard_s=1e-9)
    assert result.time_guard_hit is True and result.diagnostics["ls_time_guard"] is True
    assert_feasible(result.routes, dist, load, **TIGHT)
    assert S.solve(FakeEvaluator(dist, load, **TIGHT), dist, candidates).time_guard_hit is False


def test_300_nodes_savings_with_local_search_is_fast():
    # settlement-shaped random district: a town around the busbar and 15 villages of 12 stations
    xy, dist, load = settlement_instance(120, 15, 12, seed=0)
    candidates = list(range(1, len(xy)))
    assert len(candidates) == 300
    started = time.perf_counter()
    savings = S.solve(FakeEvaluator(dist, load, **REAL), dist, candidates)
    assert time.perf_counter() - started < 20.0
    assert savings.length_final_m < savings.length_savings_m    # the local search gains something
    assert_feasible(savings.routes, dist, load, **REAL)


class CappedEvaluator:
    """A wrapper that only lowers the member cap (as the ring split of the repair loop does)."""

    def __init__(self, ev, cap):
        self.ev, self.cap = ev, cap

    def evaluate(self, seq):
        check = self.ev.evaluate(seq)
        if self.ev.member_count(seq) > self.cap and check.feasible:
            return RingCheck(**{**check.__dict__, "feasible": False, "reason": "count"})
        return check

    def ring_length(self, seq):
        return self.ev.ring_length(seq)

    def member_count(self, seq):
        return self.ev.member_count(seq)


def test_member_cap_of_the_evaluator_and_of_a_capped_wrapper():
    xy, dist, load = settlement_instance(30, 4, 6, seed=6)
    candidates = list(range(1, len(xy)))
    result = S.solve(CappedEvaluator(FakeEvaluator(dist, load, **REAL), 4), dist, candidates)
    assert_partition(result.routes, candidates)
    assert max(len(r) for r in result.routes) <= 4
    # an evaluator with RingLimits is never asked about rings above its cap (cheap pre-check)
    ev = FakeEvaluator(dist, load, **{**REAL, "max_members": 5}, limits=True)
    result = S.solve(ev, dist, candidates)
    assert ev.seen and max(len(seq) for seq in ev.seen) <= 5
    assert max(len(r) for r in result.routes) <= 5
    # members are counted by the evaluator: non-members (e.g. joints) do not count for the cap
    member = np.ones(len(xy), bool)
    member[1::2] = False
    ev = FakeEvaluator(dist, load, **{**REAL, "max_members": 3, "max_load": INF}, member=member, limits=True)
    result = S.solve(ev, dist, candidates)
    assert max(len(r) for r in result.routes) > 3
    assert max(ev.member_count(r) for r in result.routes) <= 3


def test_unreachable_station_and_json_safe_diagnostics():
    xy, dist, load = settlement_instance(20, 2, 5, seed=8)
    dist = dist.copy()
    dist[5, :] = dist[:, 5] = INF
    dist[5, 5] = 0.0
    candidates = list(range(1, len(xy)))
    result = S.solve(FakeEvaluator(dist, load, **REAL), dist, candidates)
    assert [5] in result.routes
    assert result.length_final_m == INF
    assert result.diagnostics["solver_infeasible_rings"] == 1
    assert result.diagnostics["solver_length_final_m"] is None
    json.dumps(result.diagnostics, allow_nan=False)
    expected = {"solver", "solver_candidates", "solver_rings", "solver_infeasible_rings",
                "solver_length_construction_m", "solver_length_final_m", "solver_gain_percent", "solver_passes",
                "solver_moves", "solver_evaluations_construction", "solver_evaluations_local_search",
                "solver_construction_s", "solver_local_search_s", "solver_runtime_s", "ls_time_guard",
                *(f"solver_moves_{op}" for op in S.OPERATORS)}
    assert set(result.diagnostics) == expected


def test_empty_and_single_candidate():
    xy = np.array([[0.0, 0.0], [100.0, 0.0]])
    dist = euclid(xy)
    empty = S.solve(FakeEvaluator(dist, np.ones(2)), dist, [])
    assert empty.routes == [] and empty.length_final_m == 0.0
    single = S.solve(FakeEvaluator(dist, np.ones(2)), dist, [1])
    assert single.routes == [[1]] and single.length_final_m == pytest.approx(200.0)


def test_invalid_inputs_raise():
    xy, dist, load = settlement_instance(5, 0, 0, seed=0)
    ev = FakeEvaluator(dist, load)
    with pytest.raises(ValueError, match="busbar"):
        S.solve(ev, dist, [0, 1])
    with pytest.raises(ValueError, match="more than once"):
        S.solve_savings(ev, dist, [1, 1, 2])
    with pytest.raises(ValueError, match="NaN"):
        S.solve_savings(ev, np.where(dist > 2000, np.nan, dist), [1, 2])
    with pytest.raises(ValueError, match="square"):
        S.solve_savings(ev, dist[:, :-1], [1, 2])
    with pytest.raises(ValueError, match="more than once"):
        S.local_search([[1, 2], [2, 3]], ev, dist)
