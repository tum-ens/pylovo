"""Analytic check of open MV rings: member cap, half-ring lengths, voltage drops and currents with the best
normally-open point, and both (n-1) cases. Shared by all solvers, the stubs and the repairs.

An open ring ``R = (v_1, ..., v_k)`` starts and ends at the HV/MV busbar ``v_0 = v_{k+1}``. Its ``k + 1``
sections are ``s_j = (v_j, v_{j+1})``, ``j = 0 ... k``: ``s_0`` leaves the busbar at the A exit, ``s_k`` at the B
exit. In normal operation one section ``s_m`` is open (the normally-open point, NOP): half A ``v_1 ... v_m`` is fed
through ``s_0``, half B ``v_{m+1} ... v_k`` through ``s_k``. Positions are 1-based in :class:`RingCheck` and in
this text, 0-based in the arrays.

**Loads.** Every section carries the coincident load of the positions behind it (its downstream set): pylovo's
category law on their summed statistics plus their fixed loads (:func:`pylovo.mv_grid.coincidence.segment_loads`),
never a sum of station peaks. The law is not monotone in the non-residential terms, so every section load is
computed, never inferred from a neighbour. This is the MV analogue of pylovo's LV feeder planning.

**Electrics** (linearised, cable charging ignored; pandapower verifies the planned grid):

* voltage drop ``du = sum over the sections of w_s * P_s`` with ``w_s = (r_s + x_s * tan(phi)) * l_s / U**2``
  (p.u. per kW with ``l_s`` in m, ``r_s`` and ``x_s`` in Ohm/km, ``U`` the nominal voltage in V); with the section
  loads ``P_s`` of the open-section rule above:
  ``du_A(m) = sum_{j=0}^{m-1} w(s_j) * P[j+1, m]`` and ``du_B(m) = sum_{j=m+1}^{k} w(s_j) * P[m+1, j]``;
* current ``I_s = P_s / (sqrt(3) * U * cos(phi))``, utilization ``I_s / (I_max,s * parallel_s)``;
* (n-1): with ``s_k`` out the whole ring is fed from A (``du_n1A = sum_{j=0}^{k-1} w(s_j) * P[j+1, k]``), with
  ``s_0`` out from B (``du_n1B = sum_{j=1}^{k} w(s_j) * P[1, j]``).

**Feasible** iff (in this order; the first failing rule is the ``reason``):

1. ``count``: the weighted ring members (stations and MV customers; joints and stub nodes do not count) are at most ``RingLimits.max_members``. An urban member weighs 1, a rural member
   ``1 / rural_ring_factor``, so rural rings hold more members
   (:func:`~pylovo.mv_grid.model.member_weights`);
2. ``unreachable``: every section length is finite;
3. ``no_nop``: an *allowed* open section exists: ``1 <= m <= k - 1`` (``k = 1``: ``m = 1``, the B exit) with a
   switchgear node (station or customer) at ``v_m`` or ``v_{m+1}``, whose ring panel holds the open
   switch (at ``v_m`` if possible). A NOP never sits at a T-joint;
4. ``length``, ``current``, ``drop``: some allowed ``m`` has both half-ring lengths (busbar to the last node of the
   half; the open section belongs to neither half) at most ``max_half_m``, every section current of both halves at
   most ``utilization * I_max`` and both drops at most ``max_drop``;
5. ``n1_current``, ``n1_drop``: both (n-1) directions keep every section current at most
   ``n1_utilization * I_max`` and the drop at most ``max_drop_n1``.

**NOP choice** (voltage balance, Klabunde 2022): ``m* = argmin max(du_A(m), du_B(m))`` over the feasible allowed
``m``, ties by ``|P_A - P_B|``, then by the lower ``m``. If no allowed ``m`` is feasible, the same rule picks the
open section among all allowed ``m`` (the ring is reported infeasible with that NOP, so the topology can still build
and repair it). Cost: ``O(k * k * 4)``, about 0.1 ms for ``k = 20``.

* :func:`check_ring`: the vectorised check on explicit per-position and per-section arrays (rings with joints,
  parallel circuits, explicit sections after a split).
* :class:`RingEvaluator`: the check of terminal sequences on the district matrices, memoised per ring (both
  orientations share one result); the evaluator the solvers use
  (:class:`~pylovo.mv_grid.model.RingEvaluatorProtocol`).

Pure module: it imports numpy and the MV contracts only (no database, configuration or plotting module).
"""
from __future__ import annotations

import dataclasses
import math
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np

from pylovo.mv_grid import coincidence
from pylovo.mv_grid.model import DistanceModel, DistrictInput, RingCheck, RingLimits, member_weights

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import MvParameters

#: Terminal index of the busbar in the matrices of a :class:`RingEvaluator` (as in the distance model).
BUSBAR = 0
#: Node kinds with switchgear: their ring panel may hold the normally-open switch (never a T-joint).
SWITCHGEAR_KINDS: tuple[str, ...] = ("station", "customer")
#: Default number of memoised rings per evaluator (about 0.7 kB each; the memo is cleared when it is full).
DEFAULT_CACHE_SIZE = 50_000

#: Absolute tolerance of the limit comparisons (float noise must not decide a borderline ring).
_TOL = 1e-9
_SQRT3 = math.sqrt(3.0)


# ---------------------------------------------------------------------------------------------- limits
def _urban(rows: Any) -> np.ndarray:
    """The urban flags of node rows (missing column or values: rural)."""
    if "urban" not in rows.columns:
        return np.zeros(len(rows), dtype=bool)
    return np.array([bool(u) if u is not None and u == u else False for u in rows["urban"]], dtype=bool)


def ring_limits(params: "MvParameters") -> RingLimits:
    """The ring limits of an MV variant.

    ``max_members = MV_MAX_STATIONS_PER_RING``, ``max_drop = MV_MAX_VOLTAGE_DROP_PERCENT / 100``,
    ``max_drop_n1 = max_drop_n1_percent / 100``, ``utilization = MV_LINE_PLANNING_UTILIZATION``,
    ``n1_utilization`` and ``max_half_m = max_half_ring_length_m``.
    """
    return RingLimits(
        max_members=int(params.max_stations_per_ring),
        max_drop=float(params.max_drop_percent) / 100.0,
        max_drop_n1=float(params.max_drop_n1_percent) / 100.0,
        utilization=float(params.line_utilization),
        n1_utilization=float(params.n1_utilization),
        max_half_m=float(params.max_half_ring_length_m),
    )


# ---------------------------------------------------------------------------------------------- results
def _empty_check() -> RingCheck:
    return RingCheck(feasible=True, open_section=0, nop_position=0, length_m=0.0, half_a_m=0.0, half_b_m=0.0,
                     drop_a=0.0, drop_b=0.0, drop_n1=0.0, util_normal=0.0, util_n1=0.0, p_ring_kw=0.0,
                     p_a_kw=0.0, p_b_kw=0.0, reason=None)


def reverse_check(check: RingCheck, k: int) -> RingCheck:
    """The check of the same ring traversed in the other direction (position ``i`` becomes ``k + 1 - i``).

    Halves A and B swap, the open section ``m`` becomes ``k - m`` and the NOP position ``p`` becomes ``k + 1 - p``
    (0, i.e. none, stays 0). A ring of one member has one orientation (open section at the B exit) and is returned
    unchanged.

    Args:
        check: The check of the ring in one direction.
        k: Number of positions of the ring (members and joints).
    """
    if k <= 1:
        return check
    return dataclasses.replace(
        check,
        open_section=k - check.open_section if check.open_section else 0,
        nop_position=k + 1 - check.nop_position if check.nop_position else 0,
        half_a_m=check.half_b_m, half_b_m=check.half_a_m,
        drop_a=check.drop_b, drop_b=check.drop_a,
        p_a_kw=check.p_b_kw, p_b_kw=check.p_a_kw,
    )


# ---------------------------------------------------------------------------------------------- core
def _drop_coefficient(r_ohm_km, x_ohm_km, length_m, parallel, tan_phi: float, vn_kv: float):
    """``w`` in p.u. per kW: ``(r + x * tan(phi)) * l / (U**2 * parallel)`` with ``l`` in m and ``U`` in V."""
    return (r_ohm_km + x_ohm_km * tan_phi) * length_m / ((vn_kv * 1000.0) ** 2 * parallel)


def _capacity_kw(imax_ka, parallel, power_factor: float, vn_kv: float):
    """Active power (kW) at 100 % of the ampacity: ``sqrt(3) * U * cos(phi) * I_max * parallel``."""
    return _SQRT3 * vn_kv * power_factor * imax_ka * 1000.0 * parallel


def _unreachable(p_ring_kw: float, reason: str) -> RingCheck:
    inf = math.inf
    return RingCheck(feasible=False, open_section=0, nop_position=0, length_m=inf, half_a_m=inf, half_b_m=inf,
                     drop_a=inf, drop_b=inf, drop_n1=inf, util_normal=inf, util_n1=inf, p_ring_kw=p_ring_kw,
                     p_a_kw=0.0, p_b_kw=0.0, reason=reason)


def _check(loads: np.ndarray, length: np.ndarray, w: np.ndarray, cap: np.ndarray, n_members: float,
           switchgear: np.ndarray, limits: RingLimits) -> RingCheck:
    """The ring check on prepared arrays (``k >= 1``).

    Args:
        loads: (k, k) segment loads in kW (``loads[a, b]`` = positions a..b, 0-based), NaN below the diagonal.
        length: (k + 1,) section lengths in m.
        w: (k + 1,) drop coefficients in p.u. per kW.
        cap: (k + 1,) section capacities in kW at 100 % of the ampacity.
        n_members: Weighted number of ring members.
        switchgear: (k,) bool per position.
        limits: The ring limits.
    """
    k = loads.shape[0]
    P = np.triu(loads)                                   # zeros below the diagonal
    p_ring = float(P[0, k - 1])
    if not np.isfinite(length).all():
        return _unreachable(p_ring, "count" if n_members > limits.max_members + _TOL else "unreachable")
    # half A ending at position e (0-based) is fed through sections 0..e: section j carries P[j, e]
    drop_end = w[:k] @ P                                 # (k,): du of a half A ending at e
    util_end = (P / cap[:k, None]).max(axis=0)           # (k,): max utilization of a half A ending at e
    # half B starting at position s is fed through sections s+1..k: section e+1 carries P[s, e]
    drop_start = P @ w[1:]                               # (k,): du of a half B starting at s
    util_start = (P / cap[None, 1:]).max(axis=1)         # (k,)
    len_end = np.cumsum(length[:k])                      # sections 0..e
    len_start = np.cumsum(length[:0:-1])[::-1]           # sections s+1..k
    if k >= 2:                                           # open section m = 1..k-1: A ends at m-1, B starts at m
        drop_a, drop_b = drop_end[:-1], drop_start[1:]
        util_a, util_b = util_end[:-1], util_start[1:]
        half_a, half_b = len_end[:-1], len_start[1:]
        p_a, p_b = P[0, :-1], P[1:, k - 1]
        allowed = switchgear[:-1] | switchgear[1:]
    else:                                                # one position: open at the B exit (m = 1)
        zero = np.zeros(1)
        drop_a, drop_b = drop_end, zero
        util_a, util_b = util_end, zero
        half_a, half_b = len_end, zero
        p_a, p_b = P[0, :1], zero
        allowed = switchgear.copy()
    ok_length = (half_a <= limits.max_half_m + _TOL) & (half_b <= limits.max_half_m + _TOL)
    ok_current = (util_a <= limits.utilization + _TOL) & (util_b <= limits.utilization + _TOL)
    ok_drop = (drop_a <= limits.max_drop + _TOL) & (drop_b <= limits.max_drop + _TOL)
    feasible_m = allowed & ok_length & ok_current & ok_drop
    drop_n1 = float(max(drop_end[k - 1], drop_start[0]))
    util_n1 = float(max(util_end[k - 1], util_start[0]))

    if n_members > limits.max_members + _TOL:
        reason = "count"
    elif not allowed.any():
        reason = "no_nop"
    elif not (allowed & ok_length).any():
        reason = "length"
    elif not (allowed & ok_length & ok_current).any():
        reason = "current"
    elif not feasible_m.any():
        reason = "drop"
    elif util_n1 > limits.n1_utilization + _TOL:
        reason = "n1_current"
    elif drop_n1 > limits.max_drop_n1 + _TOL:
        reason = "n1_drop"
    else:
        reason = None

    pool = feasible_m if feasible_m.any() else (allowed if allowed.any() else np.ones_like(allowed))
    index = np.flatnonzero(pool)
    balance = np.maximum(drop_a, drop_b)[index]
    spread = np.abs(p_a - p_b)[index]
    best = int(index[np.lexsort((index, spread, balance))[0]])
    m = best + 1                                         # 1-based open section
    if allowed[best]:
        nop = m if switchgear[best] else m + 1           # the panel at v_m if it has switchgear, else at v_{m+1}
        open_section = m
    else:                                                # no_nop: values of the voltage-balanced section
        nop = open_section = 0
    return RingCheck(
        feasible=reason is None,
        open_section=open_section,
        nop_position=nop,
        length_m=float(length.sum()),
        half_a_m=float(half_a[best]),
        half_b_m=float(half_b[best]),
        drop_a=float(drop_a[best]),
        drop_b=float(drop_b[best]),
        drop_n1=drop_n1,
        util_normal=float(max(util_a[best], util_b[best])),
        util_n1=util_n1,
        p_ring_kw=p_ring,
        p_a_kw=float(p_a[best]),
        p_b_kw=float(p_b[best]),
        reason=reason,
    )


def _vector(values: Any, n: int, name: str, dtype=float) -> np.ndarray:
    array = np.asarray(values, dtype=dtype)
    if array.shape != (n,):
        raise ValueError(f"{name} must have the shape ({n},), got {array.shape}")
    return array


def check_ring(installed: np.ndarray, units: np.ndarray, fixed_kw: np.ndarray,
               section_m: np.ndarray, section_r: np.ndarray, section_x: np.ndarray,
               section_imax: np.ndarray, member: np.ndarray, switchgear: np.ndarray,
               g: np.ndarray, power_factor: float, vn_kv: float, limits: RingLimits, *,
               parallel: np.ndarray | None = None) -> RingCheck:
    """Check one open ring given by explicit per-position and per-section arrays (see the module docstring).

    Position ``i`` (0-based) is ring position ``v_{i+1}``; section ``j`` connects ``v_j`` and ``v_{j+1}`` (section 0
    starts at the busbar, section ``k`` ends there). Joints are positions with zero statistics, ``member`` False and
    ``switchgear`` False; a stub subtree is added to the statistics of its joint.

    Args:
        installed: Installed load per position and category in kW, shape (k, 4) in CATEGORIES order.
        units: Load units per position and category, shape (k, 4).
        fixed_kw: Fixed load per position in kW (MV customers, stub subtrees with customers), shape (k,).
        section_m: Section lengths in m, shape (k + 1,); ``inf`` or NaN: unreachable.
        section_r: Resistance per section in Ohm/km (of its kind; mixed lines length-weighted), shape (k + 1,).
        section_x: Reactance per section in Ohm/km, shape (k + 1,).
        section_imax: Ampacity per section and circuit in kA, shape (k + 1,).
        member: Weight of a position for the member cap (0 for joints; True/False count 1/0), shape (k,).
        switchgear: Whether a position's ring panel can hold the open switch, shape (k,).
        g: Sim factors, shape (4,).
        power_factor: cos(phi) of the loads (0 < cos(phi) <= 1).
        vn_kv: Nominal voltage in kV.
        limits: The ring limits (:func:`ring_limits`).
        parallel: Parallel circuits per section, shape (k + 1,) (default 1): ``r`` and ``x`` are divided by it,
            the ampacity is multiplied by it.

    Returns:
        The :class:`~pylovo.mv_grid.model.RingCheck`. ``open_section`` and ``nop_position`` are 0 when no open
        section exists (no positions, ``unreachable``, ``no_nop``); without an open section the half values
        describe the voltage-balanced section that no switchgear can open (``no_nop``), or are infinite
        (``unreachable``). An empty ring (``k = 0``) is feasible with zeros.

    Raises:
        ValueError: If a shape does not fit, a length is negative, ``r`` or ``x`` is negative or not finite, an
            ampacity or a parallel count is not positive, or the voltage or the power factor is invalid.
    """
    inst = np.asarray(installed, dtype=float)
    if inst.ndim != 2 or inst.shape[1] != 4:
        raise ValueError(f"installed must have the shape (k, 4), got {inst.shape}")
    k = inst.shape[0]
    cnt = np.asarray(units, dtype=float)
    if cnt.shape != inst.shape:
        raise ValueError(f"units must have the shape {inst.shape} of installed, got {cnt.shape}")
    fixed = _vector(fixed_kw, k, "fixed_kw")
    length = _vector(section_m, k + 1, "section_m")
    r = _vector(section_r, k + 1, "section_r")
    x = _vector(section_x, k + 1, "section_x")
    imax = _vector(section_imax, k + 1, "section_imax")
    members = _vector(member, k, "member", dtype=float)
    gear = _vector(switchgear, k, "switchgear", dtype=bool)
    par = np.ones(k + 1) if parallel is None else _vector(parallel, k + 1, "parallel")
    if (length < 0).any():
        raise ValueError(f"section lengths must not be negative, got {length.tolist()}")
    if not (np.isfinite(r).all() and np.isfinite(x).all() and (r >= 0).all() and (x >= 0).all()):
        raise ValueError("section_r and section_x must be finite and not negative")
    if not (np.isfinite(imax).all() and (imax > 0).all()):
        raise ValueError(f"section_imax must be positive, got {imax.tolist()}")
    if not (np.isfinite(par).all() and (par >= 1).all()):
        raise ValueError(f"parallel must be at least 1 per section, got {par.tolist()}")
    if not (math.isfinite(vn_kv) and vn_kv > 0):
        raise ValueError(f"vn_kv must be positive, got {vn_kv!r}")
    tan_phi = coincidence.tan_phi(power_factor)
    if k == 0:
        return _empty_check()
    loads = coincidence.segment_loads(inst, cnt, fixed, g)
    with np.errstate(invalid="ignore"):                  # inf lengths are reported as unreachable
        w = _drop_coefficient(r, x, length, par, tan_phi, vn_kv)
    cap = _capacity_kw(imax, par, power_factor, vn_kv)
    return _check(loads, length, w, cap, float(members.sum()), gear, limits)


# ---------------------------------------------------------------------------------------------- evaluator
def _square(values: Any, n: int, name: str, dtype=float) -> np.ndarray:
    array = np.array(values, dtype=dtype)                # a copy: the evaluator owns its matrices
    if array.shape != (n, n):
        raise ValueError(f"{name} must have the shape ({n}, {n}), got {array.shape}")
    return array


def _frozen(array: np.ndarray) -> np.ndarray:
    array.flags.writeable = False
    return array


class RingEvaluator:
    """Ring checks of terminal sequences on the matrices of one district (implements
    :class:`~pylovo.mv_grid.model.RingEvaluatorProtocol`).

    Terminal 0 is the busbar (:data:`BUSBAR`); a ring is a sequence of other terminals, each at most once. The
    sections of a ring ``[a, b, c]`` are the links ``(0, a)``, ``(a, b)``, ``(b, c)``, ``(c, 0)`` with their
    length, r, x and ampacity from the per-link matrices (kind-dependent: cable or overhead line, mixed lines
    length-weighted). Results are memoised per ring: a sequence and its reverse share one computation (on the
    lexicographically smaller orientation), so both orientations describe the same physical open point.

    Attributes:
        limits: The ring limits (the solvers read ``limits.max_members`` for a cheap pre-check).
        n_terminals: Number of terminals.
        n_computed: Rings computed so far (memo misses).
        n_cached: Evaluations answered from the memo.
    """

    def __init__(self, *, installed: np.ndarray, units: np.ndarray, fixed_kw: np.ndarray,
                 member: np.ndarray, switchgear: np.ndarray, dist_m: np.ndarray, r_ohm_km: np.ndarray,
                 x_ohm_km: np.ndarray, imax_ka: np.ndarray, g: np.ndarray, power_factor: float, vn_kv: float,
                 limits: RingLimits, cache_size: int = DEFAULT_CACHE_SIZE, length_m: np.ndarray | None = None) -> None:
        """Create the evaluator.

        Args:
            installed: Installed load per terminal and category in kW, shape (n, 4).
            units: Load units per terminal and category, shape (n, 4).
            fixed_kw: Fixed load per terminal in kW, shape (n,).
            member: Weight of a terminal for the member cap (0: not counted; True/False count 1/0), shape (n,).
            switchgear: Whether a terminal can hold the open switch, shape (n,).
            dist_m: Design link lengths in m, shape (n, n); ``inf`` = unreachable.
            r_ohm_km: Resistance per link in Ohm/km, shape (n, n).
            x_ohm_km: Reactance per link in Ohm/km, shape (n, n).
            imax_ka: Ampacity per link in kA, shape (n, n).
            g: Sim factors, shape (4,).
            power_factor: cos(phi) of the loads.
            vn_kv: Nominal voltage in kV.
            limits: The ring limits (:func:`ring_limits`).
            cache_size: Most memoised rings; the memo is cleared when it is full (0: no memo).
            length_m: True link lengths in m for the electrics and the half-ring limit, shape (n, n), where
                ``dist_m`` is a routing cost (default: ``dist_m``); ``dist_m`` stays the ring length of the solvers.

        Raises:
            ValueError: If a shape or value does not fit (as in :func:`check_ring`).
        """
        installed, units, fixed_kw = self._loads(installed, units, fixed_kw, None)
        n = installed.shape[0]
        if n < 1:
            raise ValueError("a ring evaluator needs at least the busbar terminal")
        self.n_terminals = n
        self.installed, self.units, self.fixed_kw = installed, units, fixed_kw
        self.member = _frozen(_vector(member, n, "member", dtype=float).copy())
        if not (np.isfinite(self.member).all() and (self.member >= 0).all()):
            raise ValueError("member weights must be finite and not negative")
        self.switchgear = _frozen(_vector(switchgear, n, "switchgear", dtype=bool).copy())
        dist = _square(dist_m, n, "dist_m")
        r = _square(r_ohm_km, n, "r_ohm_km")
        x = _square(x_ohm_km, n, "x_ohm_km")
        imax = _square(imax_ka, n, "imax_ka")
        dist[np.isnan(dist)] = np.inf
        if (dist < 0).any():
            raise ValueError("dist_m must not be negative")
        length = dist.copy() if length_m is None else _square(length_m, n, "length_m")
        length[np.isnan(length)] = np.inf
        if (length < 0).any():
            raise ValueError("length_m must not be negative")
        if not (np.isfinite(r).all() and np.isfinite(x).all() and (r >= 0).all() and (x >= 0).all()):
            raise ValueError("r_ohm_km and x_ohm_km must be finite and not negative")
        if not (np.isfinite(imax).all() and (imax > 0).all()):
            raise ValueError("imax_ka must be positive")
        if not (math.isfinite(vn_kv) and vn_kv > 0):
            raise ValueError(f"vn_kv must be positive, got {vn_kv!r}")
        tan_phi = coincidence.tan_phi(power_factor)
        self.g = _frozen(np.array(g, dtype=float))
        if self.g.shape != (4,):
            raise ValueError(f"g must have the shape (4,), got {self.g.shape}")
        self.power_factor = float(power_factor)
        self.vn_kv = float(vn_kv)
        self.limits = limits
        self.dist_m = _frozen(dist)
        self.length_m = _frozen(length)
        self.r_ohm_km, self.x_ohm_km, self.imax_ka = _frozen(r), _frozen(x), _frozen(imax)
        finite = np.isfinite(length)
        self._w = _frozen(np.where(finite, _drop_coefficient(r, x, np.where(finite, length, 0.0), 1.0, tan_phi,
                                                             self.vn_kv), np.inf))
        self._cap = _frozen(_capacity_kw(imax, 1.0, self.power_factor, self.vn_kv))
        self.cache_size = int(cache_size)
        self._cache: dict[tuple[int, ...], RingCheck] = {}
        self.n_computed = 0
        self.n_cached = 0

    # ------------------------------------------------------------------ construction
    @staticmethod
    def _loads(installed, units, fixed_kw, n: int | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        inst = np.array(installed, dtype=float)
        cnt = np.array(units, dtype=float)
        if inst.ndim != 2 or inst.shape[1] != 4 or (n is not None and inst.shape[0] != n):
            raise ValueError(f"installed must have the shape ({n if n is not None else 'n'}, 4), got {inst.shape}")
        if cnt.shape != inst.shape:
            raise ValueError(f"units must have the shape {inst.shape} of installed, got {cnt.shape}")
        fixed = np.array(_vector(fixed_kw, inst.shape[0], "fixed_kw"), dtype=float)
        # NaN and negative statistics are no load components (as in coincidence); a missing fixed load is 0
        inst = np.where(inst > 0, inst, 0.0)
        cnt = np.where(cnt > 0, cnt, 0.0)
        fixed = np.where(np.isnan(fixed), 0.0, fixed)
        return _frozen(inst), _frozen(cnt), _frozen(fixed)

    @classmethod
    def from_model(cls, inp: DistrictInput, model: DistanceModel, *,
                   cache_size: int = DEFAULT_CACHE_SIZE) -> "RingEvaluator":
        """The evaluator of a district: terminal statistics from ``inp.nodes``, links from the distance model.

        * loads: :func:`pylovo.mv_grid.coincidence.node_stats` of the terminal rows (``model.terminals``,
          positions in ``inp.nodes``); member weights by node kind and urban flag
          (:func:`~pylovo.mv_grid.model.member_weights`),
          switchgear by node kind (:data:`SWITCHGEAR_KINDS`);
        * links: length ``model.dist_m``; r, x and ampacity of the overhead line type of the district's voltage
          (``inp.params.overhead_at(inp.vn_kv)``) where ``model.overhead`` is set, else of its standard cable
          (``inp.params.cable_at(inp.vn_kv)``); with ``model.overhead_m`` (links on OSM MV lines) r and x are
          length-weighted by the overhead share of each link and the ampacity is the smaller one of its parts;
        * ``g`` and the power factor of the LV version (``inp.lv``), the nominal voltage ``inp.vn_kv`` and the
          limits of :func:`ring_limits`.
        """
        rows = inp.nodes.iloc[np.asarray(model.terminals, dtype=np.int64)]
        installed, units, fixed = coincidence.node_stats(rows)
        kinds = rows["kind"].astype(str).to_numpy()
        params = inp.params
        cable, overhead = params.cable_at(inp.vn_kv), params.overhead_at(inp.vn_kv)
        n = len(rows)
        oh = np.asarray(model.overhead, dtype=bool)
        if oh.shape != (n, n):
            raise ValueError(f"model.overhead must have the shape ({n}, {n}), got {oh.shape}")
        if model.overhead_m is not None:                 # links on OSM MV lines: length-weighted mixed links
            true = np.asarray(model.length_m if model.length_m is not None else model.dist_m, dtype=float)
            with np.errstate(invalid="ignore", divide="ignore"):
                share = np.where(np.isfinite(true) & (true > 0), np.asarray(model.overhead_m) / true, 0.0)
            share = np.clip(np.nan_to_num(share, nan=0.0, posinf=1.0), 0.0, 1.0)
            both = min(overhead.max_i_ka, cable.max_i_ka)
            imax = np.where(share >= 1.0 - 1e-9, overhead.max_i_ka, np.where(share <= 1e-9, cable.max_i_ka, both))
            return cls(
                installed=installed, units=units, fixed_kw=fixed,
                member=member_weights(kinds, _urban(rows), params.rural_ring_factor),
                switchgear=np.isin(kinds, SWITCHGEAR_KINDS), dist_m=model.dist_m,
                r_ohm_km=share * overhead.r_ohm_per_km + (1.0 - share) * cable.r_ohm_per_km,
                x_ohm_km=share * overhead.x_ohm_per_km + (1.0 - share) * cable.x_ohm_per_km,
                imax_ka=imax, g=inp.lv.g(), power_factor=inp.lv.power_factor, vn_kv=inp.vn_kv,
                limits=ring_limits(params), cache_size=cache_size, length_m=model.length_m,
            )
        return cls(
            installed=installed, units=units, fixed_kw=fixed,
            member=member_weights(kinds, _urban(rows), params.rural_ring_factor),
            switchgear=np.isin(kinds, SWITCHGEAR_KINDS),
            dist_m=model.dist_m,
            r_ohm_km=np.where(oh, overhead.r_ohm_per_km, cable.r_ohm_per_km),
            x_ohm_km=np.where(oh, overhead.x_ohm_per_km, cable.x_ohm_per_km),
            imax_ka=np.where(oh, overhead.max_i_ka, cable.max_i_ka),
            g=inp.lv.g(), power_factor=inp.lv.power_factor, vn_kv=inp.vn_kv, limits=ring_limits(params),
            cache_size=cache_size,
        )

    def with_loads(self, installed: np.ndarray, units: np.ndarray, fixed_kw: np.ndarray) -> "RingEvaluator":
        """A new evaluator with other terminal loads (e.g. stub subtrees added to their attachment) and the same
        links, flags and limits; it has its own empty memo.

        Args:
            installed: Installed load per terminal and category in kW, shape (n, 4).
            units: Load units per terminal and category, shape (n, 4).
            fixed_kw: Fixed load per terminal in kW, shape (n,).
        """
        installed, units, fixed_kw = self._loads(installed, units, fixed_kw, self.n_terminals)
        new = object.__new__(type(self))
        new.__dict__.update(self.__dict__)
        new.installed, new.units, new.fixed_kw = installed, units, fixed_kw
        new._cache = {}
        new.n_computed = 0
        new.n_cached = 0
        return new

    # ------------------------------------------------------------------ protocol
    def evaluate(self, seq: Sequence[int]) -> RingCheck:
        """The :class:`~pylovo.mv_grid.model.RingCheck` of the ring ``seq`` (terminal indices, busbar excluded),
        oriented as given: position 1 is ``seq[0]``, half A is fed through the link ``(0, seq[0])``.

        Raises:
            ValueError: If a terminal is the busbar, out of range or repeated.
        """
        key = tuple(int(i) for i in seq)
        if len(key) <= 1:
            return self._lookup(key)
        reverse = key[::-1]
        if reverse < key:
            return reverse_check(self._lookup(reverse), len(key))
        return self._lookup(key)

    def ring_length(self, seq: Sequence[int]) -> float:
        """Length of the ring in m: both busbar sections and every link between consecutive terminals
        (``inf`` if a link is unreachable, 0 for an empty ring)."""
        if len(seq) == 0:
            return 0.0
        path = np.fromiter((BUSBAR, *seq, BUSBAR), dtype=np.int64, count=len(seq) + 2)
        return float(self.dist_m[path[:-1], path[1:]].sum())

    def member_count(self, seq: Sequence[int]) -> float:
        """Weighted number of ring members among the terminals of ``seq`` (joints and the busbar do not count)."""
        if len(seq) == 0:
            return 0.0
        return float(self.member[np.fromiter(seq, dtype=np.int64, count=len(seq))].sum())

    # ------------------------------------------------------------------ arrays for explicit checks
    def position_arrays(self, seq: Sequence[int]) -> dict[str, np.ndarray]:
        """The per-position inputs of :func:`check_ring` for ``seq``: ``installed``, ``units``, ``fixed_kw``,
        ``member`` and ``switchgear`` (copies, to edit for joints and stub subtrees)."""
        idx = np.fromiter(seq, dtype=np.int64, count=len(seq))
        return {"installed": self.installed[idx], "units": self.units[idx], "fixed_kw": self.fixed_kw[idx],
                "member": self.member[idx], "switchgear": self.switchgear[idx]}

    def section_arrays(self, seq: Sequence[int]) -> dict[str, np.ndarray]:
        """The per-section inputs of :func:`check_ring` for ``seq``: ``section_m``, ``section_r``,
        ``section_x`` and ``section_imax`` of the ``len(seq) + 1`` links from the busbar back to the busbar."""
        path = np.fromiter((BUSBAR, *seq, BUSBAR), dtype=np.int64, count=len(seq) + 2)
        a, b = path[:-1], path[1:]
        return {"section_m": self.length_m[a, b], "section_r": self.r_ohm_km[a, b],
                "section_x": self.x_ohm_km[a, b], "section_imax": self.imax_ka[a, b]}

    # ------------------------------------------------------------------ memo
    def _lookup(self, key: tuple[int, ...]) -> RingCheck:
        check = self._cache.get(key)
        if check is not None:
            self.n_cached += 1
            return check
        check = self._compute(key)
        self.n_computed += 1
        if self.cache_size > 0:
            if len(self._cache) >= self.cache_size:
                self._cache.clear()
            self._cache[key] = check
        return check

    def _compute(self, key: tuple[int, ...]) -> RingCheck:
        k = len(key)
        if k == 0:
            return _empty_check()
        if len(set(key)) != k or min(key) <= BUSBAR or max(key) >= self.n_terminals:
            raise ValueError(f"a ring must hold distinct terminals 1..{self.n_terminals - 1} (the busbar {BUSBAR} "
                             f"closes it), got {list(key)}")
        idx = np.array(key, dtype=np.int64)
        path = np.empty(k + 2, dtype=np.int64)
        path[0] = path[-1] = BUSBAR
        path[1:-1] = idx
        a, b = path[:-1], path[1:]
        loads = coincidence.segment_loads(self.installed[idx], self.units[idx], self.fixed_kw[idx], self.g)
        return _check(loads, self.length_m[a, b], self._w[a, b], self._cap[a, b], float(self.member[idx].sum()),
                      self.switchgear[idx], self.limits)
