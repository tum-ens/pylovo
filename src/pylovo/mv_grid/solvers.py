"""Ring solvers of the MV stage: Clarke-Wright savings, a granular local search and a sweep baseline.

The solvers form the open rings of one HV/MV supply district. A ring is an ordered list of terminal indices (rows
of the district's distance matrix); the busbar is terminal 0 and closes every ring at both ends, so the length of
a ring ``[a, b, c]`` is ``d[0, a] + d[a, b] + d[b, c] + d[c, 0]``. The objective of every solver is the total ring
length (both busbar sections included). Feasibility (member cap, voltage drop with the best open point, currents,
(n-1)) is decided only by the ring evaluator (:class:`~pylovo.mv_grid.model.RingEvaluatorProtocol`), never here.

Implemented from the published methods, no Ding0 code:

* ``savings``: parallel Clarke-Wright savings (Clarke & Wright 1964) with route reversal, a merge is kept only
  when the evaluator accepts the merged ring.
* :func:`local_search`: granular first-improvement local search (Toth & Vigo 2003; Bräysy & Gendreau 2005) with
  the operators relocate, exchange, 2-opt*, 2-opt and Or-opt on the 15 nearest neighbours of every node,
  don't-look bits, a pass limit and a wall-clock safety net.
* ``sweep``: the sweep heuristic (Gillett & Miller 1974) as a baseline, with eight start angles.
* ``ortools``: optional, in :mod:`pylovo.mv_grid.solvers_ortools` (extra ``mv``).

:func:`solve` runs the construction of the chosen solver and then the local search (``rounds = 0`` turns the local
search off). Every function is deterministic: no random numbers, nodes in index order, ties broken by index. Only
the wall-clock guard of the local search can make a result timing dependent, and it is flagged when it stops the
search. ``dist_m`` is assumed symmetric (undirected rings); ``inf`` marks an unreachable pair.

Rings whose single node is infeasible on its own (too heavy, too far, unreachable) stay singletons; the topology
build flags them (``infeasible_singleton``).
"""
from __future__ import annotations

import math
import time
from bisect import bisect_left
from typing import Callable, Iterator, Sequence

import numpy as np

from pylovo.mv_grid.model import RingEvaluatorProtocol, SolverResult

#: Terminal index of the busbar in every distance matrix.
BUSBAR = 0
#: Names accepted by :func:`solve` (``MV_RING_SOLVER``).
SOLVERS: tuple[str, ...] = ("savings", "sweep", "ortools")
#: Local search operators in the order they are tried for every node pair.
OPERATORS: tuple[str, ...] = ("relocate", "exchange", "two_opt_star", "two_opt", "or_opt")
#: Smallest length gain (m) for which a move is applied; smaller gains are float noise and could cycle.
MIN_GAIN_M = 1.0
#: Up to this many candidates the savings list holds every positive pair, above only near neighbours.
SAVINGS_ALL_PAIRS_MAX = 800
#: Neighbours per node whose pairs enter the savings list above :data:`SAVINGS_ALL_PAIRS_MAX` candidates.
SAVINGS_NEIGHBOURS = 40
#: Chain lengths of the Or-opt operator.
OR_OPT_CHAINS: tuple[int, ...] = (1, 2, 3)

_ORTOOLS_HINT = ("MV_RING_SOLVER ortools needs the optional extra 'mv' (OR-Tools): install it with "
                 "'uv sync --extra mv' or use MV_RING_SOLVER savings or sweep")

Routes = list[list[int]]
_Build = Callable[[], dict[int, list[int]]]


# --------------------------------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------------------------------
def solve(name: str, ev: RingEvaluatorProtocol, dist_m: np.ndarray, candidates: Sequence[int], xy: np.ndarray, *,
          rounds: int = 50, neighbours: int = 15, time_guard_s: float = 120.0) -> SolverResult:
    """Form the open rings of one district with a construction heuristic followed by the local search.

    Args:
        name: ``savings`` (Clarke-Wright), ``sweep`` (baseline) or ``ortools`` (needs the extra ``mv``).
        ev: Ring evaluator; its ``evaluate(seq).feasible`` decides every merge and move.
        dist_m: ``(k, k)`` design distances between the terminals in m (terminal 0 = busbar, ``inf`` =
            unreachable, symmetric).
        candidates: Terminal indices of the ring candidates (never 0, each once).
        xy: ``(k, 2)`` terminal coordinates (only the sweep uses them).
        rounds: Pass limit of the local search (``local_search_rounds``); 0 turns it off.
        neighbours: Neighbour list length of the local search (``neighbours``).
        time_guard_s: Wall-clock safety net of the local search (``ls_time_guard_s``).

    Returns:
        :class:`~pylovo.mv_grid.model.SolverResult` with the rings (terminal indices, busbar excluded, each ring
        oriented so that its first terminal is lower than its last, sorted by the first terminal). The lengths are
        sums of ``dist_m`` (``inf`` if a ring has an unreachable section). ``diagnostics`` is a flat, JSON-safe
        dict for the district metrics (keys ``solver_*`` and ``ls_time_guard``).

    Raises:
        ValueError: On an unknown solver name or invalid inputs.
        ImportError: For ``ortools`` without the optional extra ``mv`` (the message names it).
    """
    if name not in SOLVERS:
        raise ValueError(f"MV_RING_SOLVER must be one of {', '.join(SOLVERS)}, got {name!r}")
    d, nodes = _prepare(dist_m, candidates)
    D = d.tolist()
    feas = _Feasibility(ev)
    started = time.perf_counter()
    if name == "savings":
        routes = _savings(feas, d, nodes)
    elif name == "sweep":
        routes = _sweep(feas, D, nodes, _prepare_xy(xy, d.shape[0]))
    else:
        try:
            from pylovo.mv_grid.solvers_ortools import solve_ortools
        except ImportError as exc:
            raise ImportError(_ORTOOLS_HINT) from exc
        routes = [list(map(int, r)) for r in solve_ortools(ev, d, nodes) if len(r)]
        _check_cover(routes, nodes, "the ortools solver")
    construction_s = time.perf_counter() - started
    construction_evaluations = feas.evaluations
    routes, ls = _local_search(feas, d, D, routes, rounds=rounds, neighbours=neighbours, time_guard_s=time_guard_s)
    runtime_s = time.perf_counter() - started
    _check_cover(routes, nodes, "the local search")
    routes = _canonical(routes)
    length_construction, length_final = ls["length_before_m"], ls["length_after_m"]
    infeasible = sum(1 for r in routes if not feas(r))
    gain = (100.0 * (length_construction - length_final) / length_construction
            if math.isfinite(length_construction) and math.isfinite(length_final) and length_construction > 0
            else None)
    diagnostics = {
        "solver": name,
        "solver_candidates": len(nodes),
        "solver_rings": len(routes),
        "solver_infeasible_rings": infeasible,
        "solver_length_construction_m": _finite_or_none(length_construction),
        "solver_length_final_m": _finite_or_none(length_final),
        "solver_gain_percent": gain,
        "solver_passes": ls["passes"],
        "solver_moves": ls["moves"],
        **{f"solver_moves_{op}": ls[f"moves_{op}"] for op in OPERATORS},
        "solver_evaluations_construction": construction_evaluations,
        "solver_evaluations_local_search": ls["evaluations"],
        "solver_construction_s": construction_s,
        "solver_local_search_s": ls["runtime_s"],
        "solver_runtime_s": runtime_s,
        "ls_time_guard": ls["ls_time_guard"],
    }
    return SolverResult(routes=routes, length_savings_m=length_construction, length_final_m=length_final,
                        passes=ls["passes"], moves=ls["moves"], time_guard_hit=ls["ls_time_guard"],
                        runtime_s=runtime_s, diagnostics=diagnostics)


def solve_savings(ev: RingEvaluatorProtocol, dist_m: np.ndarray, candidates: Sequence[int]) -> Routes:
    """Parallel Clarke-Wright savings with route reversal (construction only, no local search).

    Every candidate starts as its own ring ``busbar -> i -> busbar``. The savings ``d[0, i] + d[0, j] - d[i, j]``
    of all pairs with finite distances and a positive saving (above :data:`SAVINGS_ALL_PAIRS_MAX` candidates only
    the pairs of the :data:`SAVINGS_NEIGHBOURS` nearest neighbours of every node) are processed in descending
    order (ties by ``i``, then ``j``). Two rings are merged at ``i`` and ``j`` when both are ring ends (a ring is
    reversed if needed) and the evaluator accepts the merged ring. Singletons are always kept, also infeasible
    ones.

    Args:
        ev: Ring evaluator.
        dist_m: ``(k, k)`` design distances in m, terminal 0 = busbar.
        candidates: Terminal indices of the ring candidates.

    Returns:
        The rings (terminal indices, canonical orientation and order as in :func:`solve`).
    """
    d, nodes = _prepare(dist_m, candidates)
    return _canonical(_savings(_Feasibility(ev), d, nodes))


def local_search(routes: Routes, ev: RingEvaluatorProtocol, dist_m: np.ndarray, *, rounds: int = 50,
                 neighbours: int = 15, time_guard_s: float = 120.0) -> tuple[Routes, dict]:
    """Improve rings by a granular first-improvement local search that keeps every changed ring feasible.

    For every node ``u`` (ascending index, skipping nodes with a set don't-look bit) and every ``v`` among the
    ``neighbours`` nearest nodes of ``u`` (by ``dist_m``, ties by index), the operators are tried in this order:
    relocate (``u`` next to ``v``, after and before it), exchange (``u`` and ``v`` of different rings), 2-opt*
    (the tails after ``u`` and after ``v`` exchanged, both reconnections), 2-opt (``u`` and ``v`` of one ring made
    adjacent by reversing the section between them) and Or-opt (the chain of 1, 2 or 3 nodes starting at ``u``
    moved behind ``v``). A move is applied when its length change, computed in O(1) from ``dist_m`` and confirmed
    on the changed rings, is below ``-MIN_GAIN_M`` and the evaluator accepts every changed non-empty ring. The
    first such move is applied and the don't-look bits of ``u``, ``v`` and every node whose ring neighbours
    changed are cleared; a node without a move gets its bit set. The search stops after a pass without a move or
    after ``rounds`` passes. If the wall clock exceeds ``time_guard_s``, it stops after the current node, keeps
    the (feasible) rings and sets ``ls_time_guard``; it never raises for that. Empty rings are dropped.

    Run on a single ring, the search changes only the order of that ring (intra-ring moves).

    Args:
        routes: Rings as lists of terminal indices (busbar excluded, every terminal once).
        ev: Ring evaluator.
        dist_m: ``(k, k)`` design distances in m, terminal 0 = busbar.
        rounds: Pass limit; 0 returns the rings unchanged (canonical orientation and order).
        neighbours: Neighbour list length.
        time_guard_s: Wall-clock safety net in s.

    Returns:
        The improved rings (canonical orientation and order as in :func:`solve`) and the diagnostics ``passes``,
        ``moves``, ``ls_time_guard``, ``moves_<operator>`` per operator, ``evaluations`` (evaluator calls),
        ``length_before_m``, ``length_after_m`` and ``runtime_s``.
    """
    d = _prepare_matrix(dist_m)
    nodes = [int(t) for r in routes for t in r]
    _check_nodes(nodes, d.shape[0], "routes")
    feas = _Feasibility(ev)
    improved, diagnostics = _local_search(feas, d, d.tolist(), [[int(t) for t in r] for r in routes if len(r)],
                                          rounds=rounds, neighbours=neighbours, time_guard_s=time_guard_s)
    return _canonical(improved), diagnostics


def solve_sweep(ev: RingEvaluatorProtocol, dist_m: np.ndarray, candidates: Sequence[int], xy: np.ndarray, *,
                n_starts: int = 8) -> Routes:
    """Sweep heuristic (construction only): rings filled in polar-angle order around the busbar.

    The candidates are sorted by their polar angle around the busbar (``xy[0]``; ties by index). For each start
    angle (the first node after the largest angular gap, then ``n_starts - 1`` rotations by ``360 / n_starts``
    degrees), nodes are appended in angle order to the current ring while the evaluator accepts it, otherwise a
    new ring is opened. The ring order is the nearest-neighbour order from the busbar improved by intra-ring 2-opt
    and Or-opt. The start with the shortest total length is kept (rings with an unreachable section first count
    as worse).

    Args:
        ev: Ring evaluator.
        dist_m: ``(k, k)`` design distances in m, terminal 0 = busbar.
        candidates: Terminal indices of the ring candidates.
        xy: ``(k, 2)`` terminal coordinates, ``xy[0]`` = busbar.
        n_starts: Number of start angles.

    Returns:
        The rings (canonical orientation and order as in :func:`solve`).
    """
    d, nodes = _prepare(dist_m, candidates)
    return _canonical(_sweep(_Feasibility(ev), d.tolist(), nodes, _prepare_xy(xy, d.shape[0]),
                             n_starts=n_starts))


# --------------------------------------------------------------------------------------------------------------
# input checks and small helpers
# --------------------------------------------------------------------------------------------------------------
def _prepare_matrix(dist_m: np.ndarray) -> np.ndarray:
    d = np.asarray(dist_m, dtype=float)
    if d.ndim != 2 or d.shape[0] != d.shape[1] or d.shape[0] < 1:
        raise ValueError(f"dist_m must be a square (k, k) matrix with k >= 1, got shape {d.shape}")
    if np.isnan(d).any():
        raise ValueError("dist_m contains NaN; unreachable pairs must be inf")
    return d


def _check_nodes(nodes: list[int], k: int, what: str) -> None:
    if len(set(nodes)) != len(nodes):
        raise ValueError(f"{what} contain a terminal more than once")
    bad = [t for t in nodes if t <= BUSBAR or t >= k]
    if bad:
        raise ValueError(f"{what} must be terminal indices 1..{k - 1} (0 is the busbar), got {bad[:5]}")


def _prepare(dist_m: np.ndarray, candidates: Sequence[int]) -> tuple[np.ndarray, list[int]]:
    d = _prepare_matrix(dist_m)
    nodes = [int(t) for t in candidates]
    _check_nodes(nodes, d.shape[0], "candidates")
    return d, sorted(nodes)


def _prepare_xy(xy: np.ndarray, k: int) -> list[tuple[float, float]]:
    xy = np.asarray(xy, dtype=float)
    if xy.shape != (k, 2):
        raise ValueError(f"xy must have shape ({k}, 2), got {xy.shape}")
    return [(float(x), float(y)) for x, y in xy]


def _check_cover(routes: Routes, nodes: list[int], what: str) -> None:
    found = sorted(t for r in routes for t in r)
    if found != nodes:
        raise RuntimeError(f"{what} returned rings that do not hold every candidate exactly once")


def _canonical(routes: Routes) -> Routes:
    """Rings without empty ones, each oriented with its first terminal lower than its last, sorted by the first."""
    out = [list(r) if r[0] <= r[-1] else list(reversed(r)) for r in routes if r]
    out.sort(key=lambda r: r[0])
    return out


def _route_length(D: list[list[float]], route: Sequence[int]) -> float:
    if not route:
        return 0.0
    length = D[BUSBAR][route[0]] + D[route[-1]][BUSBAR]
    for a, b in zip(route, route[1:]):
        length += D[a][b]
    return length


def _routes_length(D: list[list[float]], routes: Routes) -> float:
    return math.fsum(_route_length(D, r) for r in routes)


def _finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


class _Feasibility:
    """Feasibility of a ring: the member cap first (cheap), then the evaluator (counted).

    The cap is read from ``ev.limits.max_members`` when the evaluator has :class:`~pylovo.mv_grid.model.RingLimits`
    (the real evaluator and capped wrappers do). It is only a pre-check: the evaluator rejects such rings anyway,
    but checking the count first saves evaluations and keeps them out of the evaluator's cache.
    """

    def __init__(self, ev: RingEvaluatorProtocol) -> None:
        self.ev = ev
        cap = getattr(getattr(ev, "limits", None), "max_members", None)
        self.cap = int(cap) if isinstance(cap, (int, np.integer)) and not isinstance(cap, bool) else None
        self.evaluations = 0

    def within_cap(self, seq: Sequence[int]) -> bool:
        return self.cap is None or self.ev.member_count(seq) <= self.cap

    def __call__(self, seq: Sequence[int]) -> bool:
        if not seq:
            return True
        if not self.within_cap(seq):
            return False
        self.evaluations += 1
        return bool(self.ev.evaluate(seq).feasible)


# --------------------------------------------------------------------------------------------------------------
# savings
# --------------------------------------------------------------------------------------------------------------
def _savings_pairs(d: np.ndarray, nodes: list[int]) -> tuple[list[int], list[int]]:
    """Candidate pairs ``(i, j)``, ``i < j``, in processing order (saving descending, then ``i``, then ``j``)."""
    n = len(nodes)
    idx = np.asarray(nodes, dtype=np.int64)
    if n <= SAVINGS_ALL_PAIRS_MAX:
        pi, pj = np.triu_indices(n, k=1)
    else:
        sub = d[np.ix_(idx, idx)]
        np.fill_diagonal(sub, np.inf)
        near = np.argsort(sub, axis=1, kind="stable")[:, :SAVINGS_NEIGHBOURS]
        rows = np.repeat(np.arange(n, dtype=np.int64), near.shape[1])
        cols = near.ravel().astype(np.int64)
        codes = np.unique(np.minimum(rows, cols) * n + np.maximum(rows, cols))
        pi, pj = codes // n, codes % n
        keep = pi != pj
        pi, pj = pi[keep], pj[keep]
    ti, tj = idx[pi], idx[pj]
    with np.errstate(invalid="ignore"):
        saving = d[BUSBAR, ti] + d[BUSBAR, tj] - d[ti, tj]
    keep = np.isfinite(saving) & (saving > 0)
    ti, tj, saving = ti[keep], tj[keep], saving[keep]
    order = np.lexsort((tj, ti, -saving))
    return ti[order].tolist(), tj[order].tolist()


def _savings(feas: _Feasibility, d: np.ndarray, nodes: list[int]) -> Routes:
    routes: dict[int, list[int]] = {t: [t] for t in nodes}
    route_of = {t: t for t in nodes}
    for i, j in zip(*_savings_pairs(d, nodes)):
        ri, rj = route_of[i], route_of[j]
        if ri == rj:
            continue
        a, b = routes[ri], routes[rj]
        if a[-1] != i:
            if a[0] != i:
                continue
            a = a[::-1]
        if b[0] != j:
            if b[-1] != j:
                continue
            b = b[::-1]
        merged = a + b
        if not feas(merged):
            continue
        routes[ri] = merged
        del routes[rj]
        for t in b:
            route_of[t] = ri
    return list(routes.values())


# --------------------------------------------------------------------------------------------------------------
# local search
# --------------------------------------------------------------------------------------------------------------
class _State:
    """Rings with the ring and position of every node."""

    def __init__(self, routes: Routes, k: int) -> None:
        self.routes = [list(r) for r in routes]
        self.route_of = [-1] * k
        self.pos_of = [-1] * k
        for rid in range(len(self.routes)):
            self._index(rid)

    def _index(self, rid: int) -> None:
        route_of, pos_of = self.route_of, self.pos_of
        for pos, t in enumerate(self.routes[rid]):
            route_of[t] = rid
            pos_of[t] = pos

    def apply(self, changes: dict[int, list[int]]) -> None:
        for rid, route in changes.items():
            self.routes[rid] = route
        for rid in changes:
            self._index(rid)


def _neighbour_lists(d: np.ndarray, nodes: list[int], count: int) -> dict[int, list[int]]:
    """The ``count`` nearest other nodes of every node by ``d`` (finite distances only, ties by index)."""
    if count <= 0 or len(nodes) < 2:
        return {t: [] for t in nodes}
    idx = np.asarray(nodes, dtype=np.int64)          # ascending, so a stable sort breaks ties by index
    sub = d[np.ix_(idx, idx)]
    np.fill_diagonal(sub, np.inf)
    count = min(count, len(nodes) - 1)
    near = np.argsort(sub, axis=1, kind="stable")[:, :count]
    finite = np.isfinite(np.take_along_axis(sub, near, axis=1))
    return {t: idx[near[row][finite[row]]].tolist() for row, t in enumerate(nodes)}


def _moves(st: _State, D: list[list[float]], u: int, v: int) -> Iterator[tuple[str, float, _Build]]:
    """Candidate moves for the node pair ``(u, v)`` in operator order: (operator, O(1) length change, builder).

    The builder returns the changed rings ``{ring id: new ring}`` (a ring may become empty). The length changes
    assume a symmetric ``D``; the caller confirms them on the built rings.
    """
    ru, rv = st.route_of[u], st.route_of[v]
    Ru, Rv = st.routes[ru], st.routes[rv]
    i, j = st.pos_of[u], st.pos_of[v]
    pu = Ru[i - 1] if i else BUSBAR
    nu = Ru[i + 1] if i + 1 < len(Ru) else BUSBAR
    pv = Rv[j - 1] if j else BUSBAR
    nv = Rv[j + 1] if j + 1 < len(Rv) else BUSBAR
    Du, Dv = D[u], D[v]
    same = ru == rv
    removal = D[pu][nu] - D[pu][u] - Du[nu]

    # relocate: u next to v, after and before it
    if not same:
        yield ("relocate", removal + Dv[u] + Du[nv] - Dv[nv],
               lambda: {ru: Ru[:i] + Ru[i + 1:], rv: Rv[:j + 1] + [u] + Rv[j + 1:]})
        yield ("relocate", removal + D[pv][u] + Du[v] - D[pv][v],
               lambda: {ru: Ru[:i] + Ru[i + 1:], rv: Rv[:j] + [u] + Rv[j:]})
    else:
        jj = j if j < i else j - 1                       # position of v once u is taken out
        if j != i - 1:                                   # after v (no-op if v is right before u)
            yield ("relocate", removal + Dv[u] + Du[nv] - Dv[nv], _reinsert_builder(Ru, ru, i, 1, jj + 1))
        if j != i + 1:                                   # before v (no-op if v is right after u)
            yield ("relocate", removal + D[pv][u] + Du[v] - D[pv][v], _reinsert_builder(Ru, ru, i, 1, jj))

    if not same:
        # exchange: u and v swap their rings
        yield ("exchange",
               D[pu][v] + Dv[nu] - D[pu][u] - Du[nu] + D[pv][u] + Du[nv] - D[pv][v] - Dv[nv],
               lambda: {ru: Ru[:i] + [v] + Ru[i + 1:], rv: Rv[:j] + [u] + Rv[j + 1:]})
        # 2-opt*: tails after u and after v exchanged; then heads joined at u-v and tails at their first nodes
        yield ("two_opt_star", Du[nv] + Dv[nu] - Du[nu] - Dv[nv],
               lambda: {ru: Ru[:i + 1] + Rv[j + 1:], rv: Rv[:j + 1] + Ru[i + 1:]})
        yield ("two_opt_star", Du[v] + D[nu][nv] - Du[nu] - Dv[nv],
               lambda: {ru: Ru[:i + 1] + Rv[:j + 1][::-1], rv: Ru[i + 1:][::-1] + Rv[j + 1:]})
    else:
        # 2-opt: reverse the section between u and v so that they become adjacent
        if j > i + 1:
            yield ("two_opt", Du[v] + D[nu][nv] - Du[nu] - Dv[nv],
                   lambda: {ru: Ru[:i + 1] + Ru[i + 1:j + 1][::-1] + Ru[j + 1:]})
        elif j < i - 1:
            yield ("two_opt", D[pv][pu] + Dv[u] - D[pv][v] - D[pu][u],
                   lambda: {ru: Ru[:j] + Ru[j:i][::-1] + Ru[i:]})

    # Or-opt: the chain of 1, 2 or 3 nodes starting at u, moved behind v
    for length in OR_OPT_CHAINS:
        if i + length > len(Ru):
            break
        if same and (i <= j < i + length or j == i - 1):
            continue                                     # v inside the chain, or the chain already follows v
        last = Ru[i + length - 1]
        after = Ru[i + length] if i + length < len(Ru) else BUSBAR
        delta = D[pu][after] - D[pu][u] - D[last][after] + Dv[u] + D[last][nv] - Dv[nv]
        if not same:
            yield ("or_opt", delta, _chain_builder(Ru, Rv, ru, rv, i, j, length))
        else:                                            # v's next node is the same once the chain is out
            yield ("or_opt", delta, _reinsert_builder(Ru, ru, i, length, (j if j < i else j - length) + 1))


def _chain_builder(Ru: list[int], Rv: list[int], ru: int, rv: int, i: int, j: int, length: int) -> _Build:
    """Move ``Ru[i:i + length]`` of ring ``ru`` behind position ``j`` of another ring ``rv``."""
    def build() -> dict[int, list[int]]:
        return {ru: Ru[:i] + Ru[i + length:], rv: Rv[:j + 1] + Ru[i:i + length] + Rv[j + 1:]}
    return build


def _reinsert_builder(Ru: list[int], ru: int, i: int, length: int, at: int) -> _Build:
    """Take ``Ru[i:i + length]`` out of ring ``ru`` and insert it at position ``at`` of the remaining ring."""
    def build() -> dict[int, list[int]]:
        rest = Ru[:i] + Ru[i + length:]
        return {ru: rest[:at] + Ru[i:i + length] + rest[at:]}
    return build


def _adjacency(route: list[int]) -> dict[int, tuple[int, int]]:
    ext = [BUSBAR, *route, BUSBAR]
    return {t: (min(a, b), max(a, b)) for a, t, b in zip(ext, ext[1:], ext[2:])}


def _improve_node(st: _State, D: list[list[float]], feas: _Feasibility, u: int, near: list[int],
                  dont_look: list[bool]) -> str | None:
    """Apply the first improving feasible move of ``u``; return its operator or None."""
    for v in near:
        for op, delta, build in _moves(st, D, u, v):
            if not delta < -MIN_GAIN_M:                  # also rejects NaN (inf - inf)
                continue
            changes = build()
            old = [st.routes[rid] for rid in changes]
            gain = sum(_route_length(D, r) for r in changes.values()) - sum(_route_length(D, r) for r in old)
            if not gain < -MIN_GAIN_M:
                continue
            if not all(feas(r) for r in changes.values()):
                continue
            before: dict[int, tuple[int, int]] = {}
            for r in old:
                before.update(_adjacency(r))
            after: dict[int, tuple[int, int]] = {}
            for r in changes.values():
                after.update(_adjacency(r))
            st.apply(changes)
            for t in {u, v, *(t for t in before if before[t] != after.get(t))}:
                dont_look[t] = False
            return op
    return None


def _local_search(feas: _Feasibility, d: np.ndarray, D: list[list[float]], routes: Routes, *, rounds: int,
                  neighbours: int, time_guard_s: float) -> tuple[Routes, dict]:
    started = time.perf_counter()
    evaluations = feas.evaluations
    st = _State(routes, d.shape[0])
    nodes = sorted(t for r in st.routes for t in r)
    length_before = _routes_length(D, st.routes)
    passes = moves = 0
    by_op = dict.fromkeys(OPERATORS, 0)
    guard = False
    if rounds > 0 and len(nodes) > 1:
        near = _neighbour_lists(d, nodes, neighbours)
        dont_look = [False] * d.shape[0]
        while passes < rounds and not guard:
            passes += 1
            moved = False
            for u in nodes:
                if dont_look[u]:
                    continue
                op = _improve_node(st, D, feas, u, near[u], dont_look)
                if op is None:
                    dont_look[u] = True
                else:
                    moved = True
                    moves += 1
                    by_op[op] += 1
                if time.perf_counter() - started > time_guard_s:
                    guard = True
                    break
            if not moved:
                break
    result = [r for r in st.routes if r]
    diagnostics = {
        "passes": passes,
        "moves": moves,
        "ls_time_guard": guard,
        **{f"moves_{op}": count for op, count in by_op.items()},
        "evaluations": feas.evaluations - evaluations,
        "length_before_m": length_before,
        "length_after_m": _routes_length(D, result),
        "runtime_s": time.perf_counter() - started,
    }
    return result, diagnostics


# --------------------------------------------------------------------------------------------------------------
# sweep
# --------------------------------------------------------------------------------------------------------------
def _improve_order(D: list[list[float]], route: list[int]) -> list[int]:
    """Intra-ring 2-opt and Or-opt (chains of 1-3, kept orientation) on the length only, first improvement."""
    r = list(route)
    n = len(r)
    for _ in range(100):                                  # a safety bound; small rings converge in a few rounds
        improved = False
        for a in range(n - 1):                            # 2-opt: reverse r[a..b]
            p = r[a - 1] if a else BUSBAR
            for b in range(a + 1, n):
                q = r[b + 1] if b + 1 < n else BUSBAR
                if D[p][r[b]] + D[r[a]][q] - D[p][r[a]] - D[r[b]][q] < -MIN_GAIN_M:
                    r[a:b + 1] = r[a:b + 1][::-1]
                    improved = True
                    break
            if improved:
                break
        if not improved:
            for length in OR_OPT_CHAINS:                  # Or-opt: move r[a..a+length-1] behind another node
                for a in range(n - length + 1):
                    first, last = r[a], r[a + length - 1]
                    p = r[a - 1] if a else BUSBAR
                    q = r[a + length] if a + length < n else BUSBAR
                    removal = D[p][q] - D[p][first] - D[last][q]
                    rest = r[:a] + r[a + length:]
                    ext = [BUSBAR, *rest, BUSBAR]
                    for pos in range(len(ext) - 1):
                        x, y = ext[pos], ext[pos + 1]
                        if pos == a:                      # the original position
                            continue
                        if removal + D[x][first] + D[last][y] - D[x][y] < -MIN_GAIN_M:
                            r = rest[:pos] + r[a:a + length] + rest[pos:]
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
        if not improved:
            break
    return r


def _order_ring(D: list[list[float]], nodes: list[int]) -> list[int]:
    """Nearest-neighbour order from the busbar (ties by index), improved by intra-ring 2-opt and Or-opt."""
    rest = sorted(nodes)
    route: list[int] = []
    current = BUSBAR
    while rest:
        row = D[current]
        best = min(rest, key=lambda t: (row[t], t))
        route.append(best)
        rest.remove(best)
        current = best
    return _improve_order(D, route)


def _sweep_once(feas: _Feasibility, D: list[list[float]], order: list[int]) -> Routes:
    routes: Routes = []
    current: list[int] = []
    for t in order:
        if current:
            grown = current + [t]
            if feas.within_cap(grown):
                grown = _order_ring(D, grown)
                if feas(grown):
                    current = grown
                    continue
            routes.append(current)
        current = [t]
    if current:
        routes.append(current)
    return routes


def _sweep(feas: _Feasibility, D: list[list[float]], nodes: list[int], xy: list[tuple[float, float]], *,
           n_starts: int = 8) -> Routes:
    if not nodes:
        return []
    x0, y0 = xy[BUSBAR]
    two_pi = 2.0 * math.pi
    angle = {t: math.atan2(xy[t][1] - y0, xy[t][0] - x0) % two_pi for t in nodes}
    order = sorted(nodes, key=lambda t: (angle[t], t))
    angles = [angle[t] for t in order]
    n = len(order)
    gaps = [angles[g + 1] - angles[g] for g in range(n - 1)] + [angles[0] + two_pi - angles[-1]]
    widest = max(range(n), key=lambda g: (gaps[g], -g))
    first = (widest + 1) % n
    starts = [first]
    count = max(1, int(n_starts))
    for m in range(1, count):
        start = bisect_left(angles, (angles[first] + m * two_pi / count) % two_pi) % n
        if start not in starts:
            starts.append(start)
    best: Routes = []
    best_key: tuple[int, float] | None = None
    for start in starts:
        routes = _sweep_once(feas, D, order[start:] + order[:start])
        lengths = [_route_length(D, r) for r in routes]
        key = (sum(1 for x in lengths if not math.isfinite(x)), math.fsum(x for x in lengths if math.isfinite(x)))
        if best_key is None or key < best_key:
            best, best_key = routes, key
    return best
