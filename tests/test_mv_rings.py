"""MV ring evaluator (pylovo.mv_grid.rings): open-point rule, limits, (n-1), memo, pandapower agreement."""
from __future__ import annotations

import dataclasses
import math
import time

import numpy as np
import pandas as pd
import pytest

from pylovo.mv_grid import coincidence, rings
from pylovo.mv_grid.model import NODE_COLUMNS, DistanceModel, DistrictInput, EdgeSet, RingCheck, RingLimits
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

#: Sim factors in CATEGORIES order (Residential, Commercial, Public, Unknown), as shipped.
G = np.array([0.07, 0.5, 0.6, 0.6])
#: NA2XS2Y 1x240 RM/25 12/20 kV (pandapower 3.4.0): r, x (Ohm/km), c (nF/km), max_i (kA).
CABLE = (0.122, 0.112, 304.0, 0.421)
LIMITS = RingLimits(max_members=20, max_drop=0.05, max_drop_n1=0.10, utilization=0.6, n1_utilization=1.0,
                    max_half_m=28000.0)
FLOATS = ("length_m", "half_a_m", "half_b_m", "drop_a", "drop_b", "drop_n1", "util_normal", "util_n1", "p_ring_kw",
          "p_a_kw", "p_b_kw")


def _check(fixed=None, lengths=None, *, installed=None, units=None, member=None, switchgear=None, r=0.1, x=0.1,
           imax=0.421, pf=0.95, vn_kv=20.0, limits=LIMITS, parallel=None) -> RingCheck:
    """check_ring with scalar defaults (fixed loads only, uniform sections, every position a station)."""
    k = len(fixed) if fixed is not None else len(installed)
    zeros = np.zeros((k, 4))
    return rings.check_ring(zeros if installed is None else installed, zeros if units is None else units,
                            np.zeros(k) if fixed is None else fixed,
                            np.full(k + 1, 1000.0) if lengths is None else lengths,
                            np.full(k + 1, r), np.full(k + 1, x), np.full(k + 1, imax),
                            np.ones(k, bool) if member is None else member,
                            np.ones(k, bool) if switchgear is None else switchgear,
                            G, pf, vn_kv, limits, parallel=parallel)


# ---------------------------------------------------------------------------------------------- brute force
def _law_kw(inst: list[float], units: list[float], g) -> float:
    total = 0.0
    for c in range(4):
        if inst[c] > 0 and units[c] > 0:
            total += inst[c] * (g[c] + (1.0 - g[c]) * units[c] ** (-0.75))
    return total


def _brute(inst, units, fixed, length, r, x, imax, member, gear, g, pf, vn_kv, lim, par) -> RingCheck:
    """check_ring in plain Python: direct sums, explicit loops over the open sections and the sections."""
    k = len(fixed)
    tan = math.tan(math.acos(pf))
    u2 = (vn_kv * 1000.0) ** 2

    def load(a, b):              # coincident load of positions a..b (0-based, inclusive)
        pos = range(a, b + 1)
        i = [sum(max(inst[p][c], 0.0) for p in pos) for c in range(4)]
        n = [sum(max(units[p][c], 0.0) for p in pos) for c in range(4)]
        return _law_kw(i, n, g) + sum(fixed[p] for p in pos)

    def w(j):
        return (r[j] + x[j] * tan) * length[j] / (u2 * par[j])

    def cap(j):
        return math.sqrt(3.0) * vn_kv * pf * imax[j] * 1000.0 * par[j]

    if k == 0:
        return RingCheck(True, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None)
    members = sum(bool(m) for m in member)
    if not all(math.isfinite(v) for v in length):
        reason = "count" if members > lim.max_members else "unreachable"
        inf = math.inf
        return RingCheck(False, 0, 0, inf, inf, inf, inf, inf, inf, inf, inf, load(0, k - 1), 0.0, 0.0, reason)

    def half_a(m):               # v_1..v_m through s_0..s_{m-1}; s_j carries positions j..m-1
        loads = [load(j, m - 1) for j in range(m)]
        return (sum(w(j) * loads[j] for j in range(m)), max(loads[j] / cap(j) for j in range(m)),
                sum(length[j] for j in range(m)), load(0, m - 1))

    def half_b(m):               # v_{m+1}..v_k through s_{m+1}..s_k; s_j carries positions m..j-1
        if m >= k:
            return 0.0, 0.0, 0.0, 0.0
        sections = range(m + 1, k + 1)
        return (sum(w(j) * load(m, j - 1) for j in sections), max(load(m, j - 1) / cap(j) for j in sections),
                sum(length[j] for j in sections), load(m, k - 1))

    tol = 1e-9
    rows = []
    for m in (range(1, k) if k >= 2 else [1]):
        da, ua, la, pa = half_a(m)
        db, ub, lb, pb = half_b(m)
        allowed = bool(gear[m - 1]) or (m < k and bool(gear[m]))
        ok_len = la <= lim.max_half_m + tol and lb <= lim.max_half_m + tol
        ok_cur = ua <= lim.utilization + tol and ub <= lim.utilization + tol
        ok_drop = da <= lim.max_drop + tol and db <= lim.max_drop + tol
        rows.append(dict(m=m, da=da, db=db, ua=ua, ub=ub, la=la, lb=lb, pa=pa, pb=pb, allowed=allowed,
                         ok_len=ok_len, ok_cur=ok_cur, ok=allowed and ok_len and ok_cur and ok_drop))
    n1a = (sum(w(j) * load(j, k - 1) for j in range(k)), max(load(j, k - 1) / cap(j) for j in range(k)))
    n1b = (sum(w(j) * load(0, j - 1) for j in range(1, k + 1)), max(load(0, j - 1) / cap(j) for j in range(1, k + 1)))
    drop_n1, util_n1 = max(n1a[0], n1b[0]), max(n1a[1], n1b[1])
    if members > lim.max_members:
        reason = "count"
    elif not any(row["allowed"] for row in rows):
        reason = "no_nop"
    elif not any(row["allowed"] and row["ok_len"] for row in rows):
        reason = "length"
    elif not any(row["allowed"] and row["ok_len"] and row["ok_cur"] for row in rows):
        reason = "current"
    elif not any(row["ok"] for row in rows):
        reason = "drop"
    elif util_n1 > lim.n1_utilization + tol:
        reason = "n1_current"
    elif drop_n1 > lim.max_drop_n1 + tol:
        reason = "n1_drop"
    else:
        reason = None
    pool = [row for row in rows if row["ok"]] or [row for row in rows if row["allowed"]] or rows
    best = min(pool, key=lambda row: (max(row["da"], row["db"]), abs(row["pa"] - row["pb"]), row["m"]))
    m = best["m"]
    if best["allowed"]:
        nop = m if gear[m - 1] else m + 1
        open_section = m
    else:
        nop = open_section = 0
    return RingCheck(reason is None, open_section, nop, sum(length), best["la"], best["lb"], best["da"], best["db"],
                     drop_n1, max(best["ua"], best["ub"]), util_n1, load(0, k - 1), best["pa"], best["pb"], reason)


def _random_ring(rng: np.random.Generator, k: int) -> dict:
    present = rng.random((k, 4)) < 0.6
    installed = np.where(present, rng.uniform(5, 900, (k, 4)), 0.0)
    units = np.where(present, rng.integers(1, 150, (k, 4)), 0).astype(float)
    units[:, 1:] = np.where(present[:, 1:], rng.integers(1, 6, (k, 3)), 0)
    fixed = np.where(rng.random(k) < 0.2, rng.uniform(100, 3000, k), 0.0)
    joint = rng.random(k) < 0.2
    installed[joint], units[joint], fixed[joint] = 0.0, 0.0, 0.0
    overhead = rng.random(k + 1) < 0.3
    return dict(
        installed=installed, units=units, fixed_kw=fixed,
        section_m=rng.uniform(50, 6000, k + 1),
        section_r=np.where(overhead, 0.2376, 0.122), section_x=np.where(overhead, 0.344, 0.112),
        section_imax=np.where(overhead, 0.41, 0.421),
        member=~joint, switchgear=~joint & (rng.random(k) < 0.9),
        g=G, power_factor=float(rng.choice([0.9, 0.95, 1.0])), vn_kv=float(rng.choice([10.0, 20.0])),
        limits=RingLimits(max_members=int(rng.integers(3, 25)), max_drop=float(rng.uniform(0.002, 0.05)),
                          max_drop_n1=float(rng.uniform(0.005, 0.12)), utilization=float(rng.uniform(0.1, 0.8)),
                          n1_utilization=float(rng.uniform(0.3, 1.0)), max_half_m=float(rng.uniform(2000, 40000))),
        parallel=np.where(rng.random(k + 1) < 0.1, 2.0, 1.0),
    )


def _assert_same(got: RingCheck, expected: RingCheck, rel: float = 1e-9) -> None:
    assert (got.feasible, got.reason, got.open_section, got.nop_position) == \
        (expected.feasible, expected.reason, expected.open_section, expected.nop_position)
    for name in FLOATS:
        assert getattr(got, name) == pytest.approx(getattr(expected, name), rel=rel, abs=1e-12), name


def test_check_ring_agrees_with_brute_force_on_random_rings():
    rng = np.random.default_rng(20261015)
    reasons = set()
    for _ in range(200):
        case = _random_ring(rng, int(rng.integers(1, 23)))
        got = rings.check_ring(**case)
        expected = _brute(case["installed"].tolist(), case["units"].tolist(), case["fixed_kw"].tolist(),
                          case["section_m"].tolist(), case["section_r"].tolist(), case["section_x"].tolist(),
                          case["section_imax"].tolist(), case["member"].tolist(), case["switchgear"].tolist(),
                          G, case["power_factor"], case["vn_kv"], case["limits"], case["parallel"].tolist())
        _assert_same(got, expected)
        reasons.add(got.reason)
    # the random limits reach every rule
    assert {None, "count", "length", "current", "drop", "n1_current", "n1_drop"} <= reasons


# ---------------------------------------------------------------------------------------------- hand-computed
def test_hand_computed_three_node_ring():
    """Loads 1000, 2000, 1500 kW (fixed), four 1 km sections, r = x = 0.1 Ohm/km, cos(phi) 0.8, 20 kV.

    w = (0.1 + 0.1 * 0.75) * 1000 m / (20000 V)**2 = 4.375e-7 p.u./kW.
    m = 1: du_A = 1000 w, du_B = (3500 + 2000) w = 5500 w -> 5500 w
    m = 2: du_A = (3000 + 2000) w = 5000 w, du_B = 1500 w -> 5000 w (chosen, NOP at v_2)
    (n-1) A: (4500 + 3500 + 1500) w = 9500 w; B: (4500 + 3500 + 1000) w = 9000 w
    """
    check = _check([1000.0, 2000.0, 1500.0], [1000.0] * 4, pf=0.8)
    w = 175.0 / 4e8
    i_kw = math.sqrt(3) * 20.0 * 0.8 * 421.0                      # kW at the ampacity
    assert check == RingCheck(
        feasible=True, open_section=2, nop_position=2, length_m=4000.0, half_a_m=2000.0, half_b_m=1000.0,
        drop_a=pytest.approx(5000 * w), drop_b=pytest.approx(1500 * w), drop_n1=pytest.approx(9500 * w),
        util_normal=pytest.approx(3000 / i_kw), util_n1=pytest.approx(4500 / i_kw), p_ring_kw=4500.0,
        p_a_kw=3000.0, p_b_kw=1500.0, reason=None)
    # the other direction opens the same section (between the 2000 and the 1500 kW node)
    other = _check([1500.0, 2000.0, 1000.0], [1000.0] * 4, pf=0.8)
    assert (other.open_section, other.drop_a, other.drop_b) == (1, pytest.approx(1500 * w), pytest.approx(5000 * w))


def test_sections_carry_the_coincident_load_of_their_downstream_set():
    installed = np.array([[400.0, 0, 0, 0], [300.0, 200.0, 0, 0], [500.0, 0, 0, 0], [250.0, 0, 0, 100.0]])
    units = np.array([[40.0, 0, 0, 0], [25.0, 2, 0, 0], [60.0, 0, 0, 0], [20.0, 0, 0, 1]])
    check = _check(installed=installed, units=units)
    peaks = coincidence.coincident_kw(installed, units, G)
    whole = coincidence.coincident_kw(installed.sum(axis=0), units.sum(axis=0), G)
    assert check.p_ring_kw == pytest.approx(whole)
    assert check.p_ring_kw < peaks.sum()                          # station peaks are never added
    m = check.open_section
    assert check.p_a_kw == pytest.approx(coincidence.coincident_kw(installed[:m].sum(0), units[:m].sum(0), G))
    assert check.p_b_kw == pytest.approx(coincidence.coincident_kw(installed[m:].sum(0), units[m:].sum(0), G))


# ---------------------------------------------------------------------------------------------- open-point rule
def test_open_point_never_at_a_joint():
    rng = np.random.default_rng(3)
    for _ in range(300):
        k = int(rng.integers(1, 15))
        gear = rng.random(k) < 0.4
        check = _check(rng.uniform(100, 800, k), rng.uniform(100, 3000, k + 1), switchgear=gear,
                       member=gear, limits=dataclasses.replace(LIMITS, max_members=50))
        if not gear.any():
            assert (check.reason, check.open_section, check.nop_position) == ("no_nop", 0, 0)
            continue
        m, nop = check.open_section, check.nop_position
        assert nop in (m, m + 1) and gear[nop - 1]                # the open switch sits at a switchgear node
        assert m == 1 if k == 1 else 1 <= m <= k - 1
        assert gear[m - 1] or gear[m]                             # the open section touches a switchgear node
        if gear[m - 1]:
            assert nop == m                                       # at v_m if possible


def test_open_point_next_to_the_only_switchgear_nodes():
    # stations at positions 1 and 5, joints between them: only m = 1 (NOP at v_1) and m = 4 (NOP at v_5) open
    gear = np.array([True, False, False, False, True])
    check = _check([300.0, 0.0, 0.0, 0.0, 300.0], switchgear=gear, member=gear)
    assert check.feasible
    assert (check.open_section, check.nop_position) in ((1, 1), (4, 5))
    # a ring of joints has no open point
    joints = np.zeros(3, bool)
    assert _check([0.0] * 3, switchgear=joints, member=joints).reason == "no_nop"
    # one member: open at the B exit, NOP at the member
    single = _check([500.0])
    assert (single.feasible, single.open_section, single.nop_position, single.half_b_m, single.p_b_kw) == \
        (True, 1, 1, 0.0, 0.0)
    assert single.half_a_m == 1000.0 and single.p_a_kw == 500.0


def test_open_point_balances_the_voltage_drop():
    rng = np.random.default_rng(11)
    loads, lengths = rng.uniform(100, 900, 12), rng.uniform(200, 2500, 13)
    check = _check(loads, lengths)
    others = []
    for m in range(1, 12):                                        # every NOP position by brute force
        a = sum(lengths[j] * loads[j:m].sum() for j in range(m))
        b = sum(lengths[j] * loads[m:j].sum() for j in range(m + 1, 13))
        others.append((max(a, b), m))
    assert check.open_section == min(others)[1]
    assert check.feasible


def test_member_cap_counts_members_not_joints():
    loads = np.full(21, 50.0)
    lengths = np.full(22, 100.0)
    assert _check(loads, lengths).reason == "count"
    member = np.ones(21, bool)
    member[[3, 9]] = False                                        # two joints: 19 members
    check = _check(loads, lengths, member=member, switchgear=member)
    assert check.feasible
    assert _check(loads[:20], lengths[:21]).feasible              # exactly 20 members


def test_n1_is_checked_from_both_sides():
    # a long B exit: normal operation and (n-1) from A are fine, (n-1) fed from B is not
    loads = np.array([1500.0, 1500.0, 1500.0, 1500.0])
    lengths = np.array([500.0, 500.0, 500.0, 500.0, 20000.0])
    check = _check(loads, lengths, limits=dataclasses.replace(LIMITS, max_drop_n1=0.02))
    w = (0.1 + 0.1 * math.tan(math.acos(0.95))) / 4e8
    n1_a = w * 500 * (6000 + 4500 + 3000 + 1500)
    n1_b = w * (20000 * 6000 + 500 * (4500 + 3000 + 1500))
    assert check.drop_n1 == pytest.approx(max(n1_a, n1_b)) == pytest.approx(n1_b)
    assert check.reason == "n1_drop"
    reverse = _check(loads, lengths[::-1], limits=dataclasses.replace(LIMITS, max_drop_n1=0.02))
    assert (reverse.reason, reverse.drop_n1) == ("n1_drop", pytest.approx(check.drop_n1))
    assert _check(loads, lengths, limits=dataclasses.replace(LIMITS, max_drop_n1=0.05)).feasible
    # each half below the normal utilization, the whole ring above the ampacity: an (n-1) current violation
    heavy = _check(np.full(4, 4000.0), np.full(5, 200.0))
    assert heavy.util_normal <= 0.6 < 1.0 < heavy.util_n1
    assert heavy.reason == "n1_current"


def test_half_ring_length():
    tiny = np.full(4, 10.0)
    sections = np.full(5, 10000.0)                                # 50 km: only m = 2 gives 20 + 20 km
    check = _check(tiny, sections)
    assert (check.feasible, check.open_section, check.half_a_m, check.half_b_m) == (True, 2, 20000.0, 20000.0)
    assert _check(tiny, sections, limits=dataclasses.replace(LIMITS, max_half_m=15000.0)).reason == "length"
    # joints at v_2, v_3: the balanced section 2 cannot be opened, sections 1 and 3 leave a 30 km half
    gear = np.array([True, False, False, True])
    assert _check(tiny, sections, switchgear=gear, member=gear).reason == "length"


def test_normal_current_and_drop_reasons():
    over = _check(np.full(4, 4500.0), np.full(5, 200.0))          # 9 MW per half: 65 % of the ampacity
    assert (over.reason, over.util_normal) == ("current", pytest.approx(9000 / (math.sqrt(3) * 20 * 0.95 * 421)))
    sagging = _check(np.full(4, 2000.0), np.full(5, 9000.0), limits=dataclasses.replace(LIMITS, max_drop=0.01))
    assert (sagging.reason, sagging.open_section) == ("drop", 2)       # 1.8 % per half, 18 km halves
    assert sagging.drop_a == pytest.approx((0.1 + 0.1 * math.tan(math.acos(0.95))) * 9000 * 6000 / 4e8)


def test_parallel_circuits_halve_drop_and_utilization():
    loads, lengths = np.array([800.0, 1200.0, 600.0]), np.array([3000.0, 2000.0, 2500.0, 1500.0])
    single = _check(loads, lengths)
    double = _check(loads, lengths, parallel=np.full(4, 2.0))
    assert double.open_section == single.open_section
    for name in ("drop_a", "drop_b", "drop_n1", "util_normal", "util_n1"):
        assert getattr(double, name) == pytest.approx(getattr(single, name) / 2)


def test_unreachable_and_empty_rings():
    check = _check([100.0, 200.0], [500.0, math.inf, 300.0])
    assert (check.feasible, check.reason, check.open_section, check.nop_position) == (False, "unreachable", 0, 0)
    assert check.length_m == math.inf and check.p_ring_kw == 300.0
    assert _check([100.0, 200.0], [500.0, np.nan, 300.0]).reason == "unreachable"
    over_cap = _check([100.0, 200.0], [500.0, math.inf, 300.0], limits=dataclasses.replace(LIMITS, max_members=1))
    assert over_cap.reason == "count"
    empty = rings.check_ring(np.zeros((0, 4)), np.zeros((0, 4)), np.zeros(0), [0.0], [0.1], [0.1], [0.4],
                             np.zeros(0, bool), np.zeros(0, bool), G, 0.95, 20.0, LIMITS)
    assert empty.feasible and empty.length_m == 0.0 and empty.open_section == 0


def test_check_ring_rejects_invalid_inputs():
    with pytest.raises(ValueError, match="section_m"):
        _check([100.0, 200.0], [100.0, 200.0])
    with pytest.raises(ValueError, match="negative"):
        _check([100.0, 200.0], [100.0, -1.0, 100.0])
    with pytest.raises(ValueError, match="section_imax"):
        _check([100.0], [100.0, 100.0], imax=0.0)
    with pytest.raises(ValueError, match="power factor"):
        _check([100.0], [100.0, 100.0], pf=0.0)
    with pytest.raises(ValueError, match="parallel"):
        _check([100.0], [100.0, 100.0], parallel=[1.0, 0.0])
    with pytest.raises(ValueError, match="installed"):
        rings.check_ring(np.zeros((2, 3)), np.zeros((2, 3)), np.zeros(2), np.ones(3), np.ones(3), np.ones(3),
                         np.ones(3), np.ones(2, bool), np.ones(2, bool), G, 0.95, 20.0, LIMITS)


# ---------------------------------------------------------------------------------------------- pandapower
def test_drops_agree_with_pandapower_on_a_20_kv_ring():
    """A 10-station 20 kV ring of the standard cable: the analytic drops and currents of the normal case and of
    both (n-1) cases are within 0.2 percentage points of a pandapower power flow (busbar at 1.02 p.u., cable
    charging included). Additive loads, so both sides see the same section loads."""
    pp = pytest.importorskip("pandapower")
    rng = np.random.default_rng(42)
    k, pf = 10, 0.95
    loads = rng.uniform(500.0, 1300.0, k)                         # kW, about 9 MW per ring
    lengths = rng.uniform(800.0, 2400.0, k + 1)                   # m, about 17 km per ring
    r, x, c, imax = CABLE
    check = _check(loads, lengths, r=r, x=x, imax=imax, pf=pf)
    assert check.feasible and check.drop_n1 > 0.03               # a stressed but feasible ring

    net = pp.create_empty_network()
    buses = pp.create_buses(net, k + 1, vn_kv=20.0)
    pp.create_ext_grid(net, buses[0], vm_pu=1.02)
    p_mw = loads / 1000.0
    pp.create_loads(net, buses[1:], p_mw=p_mw, q_mvar=p_mw * math.tan(math.acos(pf)))
    path = [0, *range(1, k + 1), 0]
    for j in range(k + 1):
        pp.create_line_from_parameters(net, buses[path[j]], buses[path[j + 1]], length_km=lengths[j] / 1000.0,
                                       r_ohm_per_km=r, x_ohm_per_km=x, c_nf_per_km=c, max_i_ka=imax)

    def flow(open_section: int) -> tuple[np.ndarray, float]:
        net.line["in_service"] = True
        net.line.loc[open_section, "in_service"] = False
        pp.runpp(net)
        return 1.02 - net.res_bus.vm_pu.to_numpy()[1:], float(net.res_line.loading_percent.max()) / 100.0

    m = check.open_section
    drop, loading = flow(m)
    tolerance = 0.002                                             # 0.2 percentage points
    assert check.drop_a == pytest.approx(drop[:m].max(), abs=tolerance)
    assert check.drop_b == pytest.approx(drop[m:].max(), abs=tolerance)
    assert check.util_normal == pytest.approx(loading, abs=0.01)
    drop_n1_a, loading_a = flow(k)                                # B exit out: fed from A
    drop_n1_b, loading_b = flow(0)                                # A exit out: fed from B
    assert check.drop_n1 == pytest.approx(max(drop_n1_a.max(), drop_n1_b.max()), abs=tolerance)
    assert check.util_n1 == pytest.approx(max(loading_a, loading_b), abs=0.01)


# ---------------------------------------------------------------------------------------------- evaluator
def _evaluator(rng: np.random.Generator, n: int, **kwargs) -> rings.RingEvaluator:
    xy = rng.uniform(0, 12000, (n, 2))
    dist = 1.3 * np.hypot(*(xy[:, None, :] - xy[None, :, :]).transpose(2, 0, 1))
    overhead = rng.random((n, n)) < 0.3
    overhead = overhead | overhead.T
    installed = np.where(rng.random((n, 4)) < 0.6, rng.uniform(5, 700, (n, 4)), 0.0)
    units = np.where(installed > 0, rng.integers(1, 80, (n, 4)), 0).astype(float)
    fixed = np.where(rng.random(n) < 0.15, rng.uniform(100, 2000, n), 0.0)
    installed[0], units[0], fixed[0] = 0.0, 0.0, 0.0                 # the busbar
    gear = rng.random(n) < 0.95
    defaults = dict(installed=installed, units=units, fixed_kw=fixed, member=np.ones(n, bool), switchgear=gear,
                    dist_m=dist, r_ohm_km=np.where(overhead, 0.2376, 0.122),
                    x_ohm_km=np.where(overhead, 0.344, 0.112), imax_ka=np.where(overhead, 0.41, 0.421), g=G,
                    power_factor=0.95, vn_kv=20.0, limits=LIMITS)
    return rings.RingEvaluator(**{**defaults, **kwargs})


def _explicit(ev: rings.RingEvaluator, seq) -> RingCheck:
    return rings.check_ring(**ev.position_arrays(seq), **ev.section_arrays(seq), g=ev.g,
                            power_factor=ev.power_factor, vn_kv=ev.vn_kv, limits=ev.limits)


def test_evaluator_equals_check_ring_in_both_orientations():
    rng = np.random.default_rng(5)
    ev = _evaluator(rng, 40)
    for _ in range(150):
        seq = [int(t) for t in rng.permutation(np.arange(1, 40))[:int(rng.integers(1, 21))]]
        reverse = seq[::-1]
        got, got_reverse = ev.evaluate(seq), ev.evaluate(reverse)
        canonical = min(tuple(seq), tuple(reverse))
        expected = _explicit(ev, canonical)
        if list(canonical) != seq:
            expected = rings.reverse_check(expected, len(seq))
        assert got == expected
        assert got_reverse == rings.reverse_check(got, len(seq))        # the same physical open point
        if got.nop_position:
            assert seq[got.nop_position - 1] == reverse[got_reverse.nop_position - 1]
        assert got.length_m == pytest.approx(ev.ring_length(seq)) or not got.feasible


def test_evaluator_memo():
    rng = np.random.default_rng(8)
    ev = _evaluator(rng, 12)
    first = ev.evaluate([3, 5, 7])
    assert ev.evaluate([3, 5, 7]) is first
    assert ev.evaluate([7, 5, 3]) == rings.reverse_check(first, 3)
    assert (ev.n_computed, ev.n_cached) == (1, 2)
    small = _evaluator(np.random.default_rng(8), 12, cache_size=2)
    for seq in ([1, 2], [3, 4], [5, 6], [1, 2]):
        assert small.evaluate(seq) == ev.evaluate(seq)
    assert len(small._cache) <= 2
    uncached = _evaluator(np.random.default_rng(8), 12, cache_size=0)
    assert uncached.evaluate([3, 5, 7]) == first and not uncached._cache


def test_evaluator_length_count_and_input_checks():
    rng = np.random.default_rng(9)
    member = np.ones(10, bool)
    member[[4, 6]] = False
    ev = _evaluator(rng, 10, member=member)
    d = ev.dist_m
    assert ev.ring_length([2, 4, 6]) == pytest.approx(d[0, 2] + d[2, 4] + d[4, 6] + d[6, 0])
    assert ev.ring_length([]) == 0.0 and ev.member_count([]) == 0
    assert ev.member_count([2, 4, 6, 8]) == 2
    assert ev.evaluate([]).feasible
    for bad in ([0, 2], [2, 2], [10]):
        with pytest.raises(ValueError, match="distinct terminals"):
            ev.evaluate(bad)
    dist = d.copy()
    dist[2, 3] = dist[3, 2] = np.inf
    unreachable = _evaluator(np.random.default_rng(9), 10, dist_m=dist)
    assert unreachable.evaluate([2, 3]).reason == "unreachable"
    assert unreachable.ring_length([2, 3]) == math.inf
    with pytest.raises(ValueError, match="dist_m"):
        _evaluator(np.random.default_rng(9), 10, dist_m=np.zeros((9, 9)))
    with pytest.raises(ValueError, match="imax_ka"):
        _evaluator(np.random.default_rng(9), 10, imax_ka=np.zeros((10, 10)))
    with pytest.raises(ValueError, match="installed"):
        ev.with_loads(np.zeros((9, 4)), np.zeros((9, 4)), np.zeros(9))


def test_with_loads_gives_a_new_evaluator():
    rng = np.random.default_rng(12)
    ev = _evaluator(rng, 15)
    before = ev.evaluate([1, 2, 3])
    installed, units, fixed = ev.installed.copy(), ev.units.copy(), ev.fixed_kw.copy()
    fixed[2] += 2500.0                                           # a stub subtree with an MV customer on terminal 2
    heavier = ev.with_loads(installed, units, fixed)
    after = heavier.evaluate([1, 2, 3])
    assert after.p_ring_kw == pytest.approx(before.p_ring_kw + 2500.0)
    assert ev.evaluate([1, 2, 3]) is before                       # the original keeps its loads and memo
    assert heavier.limits is ev.limits and heavier.dist_m is ev.dist_m
    assert (heavier.n_computed, heavier.n_cached) == (1, 0)


def test_evaluator_is_fast_enough():
    """One evaluation of a 20-member ring (no memo hit) takes less than 200 microseconds."""
    rng = np.random.default_rng(13)
    n = 80
    ev = _evaluator(rng, n, cache_size=0)
    seqs = [[int(t) for t in rng.permutation(np.arange(1, n))[:20]] for _ in range(200)]
    best = math.inf
    for _ in range(5):
        start = time.perf_counter()
        for seq in seqs:
            ev.evaluate(seq)
        best = min(best, (time.perf_counter() - start) / len(seqs))
    assert best < 200e-6, f"{best * 1e6:.0f} us per evaluation"


def test_from_model_uses_the_variant_line_types_and_limits():
    params = MvParameters()
    lv = LvVersionParameters(version_id="t", sim_factor=dict(zip(("Residential", "Commercial", "Public", "Unknown"),
                                                                    G.tolist())),
                             power_factor=0.95, mv_direct_threshold_kw=300.0, residential_only=False,
                             lv_reference_voltage_pu=0.96, use_dso=False, use_open=False, use_manual=False,
                             electrical_backend="pandapower")
    nodes = pd.DataFrame(0.0, index=pd.Index([100, 5, 7, 9, 11, 13], name="mv_node_id"), columns=list(NODE_COLUMNS))
    nodes["kind"] = ["busbar", "station", "station", "customer", "generator", "station"]
    nodes["connection_hint"] = ["busbar", "ring", "ring", "ring", "tee", "excluded"]
    nodes.loc[5, ["inst_res", "units_res"]] = [600.0, 50.0]
    nodes.loc[7, ["inst_res", "units_res", "inst_com", "units_com"]] = [300.0, 20.0, 150.0, 1.0]
    nodes.loc[9, "p_fixed_kw"] = 1200.0
    nodes.loc[7, "inst_pub"] = np.nan                                 # missing statistics count as 0
    terminals = np.array([0, 1, 2, 3, 4])                             # rows of nodes, busbar first
    k = len(terminals)
    dist = np.full((k, k), 1500.0)
    np.fill_diagonal(dist, 0.0)
    overhead = np.zeros((k, k), bool)
    overhead[1, 2] = overhead[2, 1] = True
    model = DistanceModel(terminals=terminals, candidates=np.array([1, 2, 3]), dist_m=dist, street_m=dist,
                          straight_m=dist, overhead=overhead, straight=np.zeros((k, k), bool),
                          xy=np.zeros((k, 2)))
    empty = np.zeros(0)
    inp = DistrictInput(mv_grid_id=1, substation_id=1, vn_kv=20.0, nodes=nodes,
                        edges=EdgeSet(empty.astype(np.int64), empty.astype(np.int64), empty.astype(np.int64), empty,
                                      empty.astype(bool), empty.astype(np.int64), empty.astype(np.int64),
                                      np.zeros((0, 2))),
                        params=params, lv=lv, known_units_mva=(), edge_geometry=lambda ids: {})
    ev = rings.RingEvaluator.from_model(inp, model)
    assert ev.limits == RingLimits(max_members=20, max_drop=0.05, max_drop_n1=0.10, utilization=0.6,
                                   n1_utilization=1.0, max_half_m=28000.0) == rings.ring_limits(params)
    assert ev.r_ohm_km[1, 2] == params.overhead.r_ohm_per_km and ev.imax_ka[1, 2] == params.overhead.max_i_ka
    assert ev.r_ohm_km[0, 1] == params.cable.r_ohm_per_km and ev.x_ohm_km[2, 3] == params.cable.x_ohm_per_km
    assert ev.member.tolist() == [False, True, True, True, True]
    assert ev.switchgear.tolist() == [False, True, True, True, True]
    assert ev.vn_kv == 20.0 and ev.power_factor == 0.95 and ev.g.tolist() == G.tolist()
    check = ev.evaluate([1, 2, 3])
    installed, units, fixed = coincidence.node_stats(nodes.iloc[[1, 2, 3]])
    assert check.p_ring_kw == pytest.approx(coincidence.pooled_kw(installed, units, fixed, G))
    assert check.feasible and check.length_m == 6000.0
    cable, overhead = params.cable, params.overhead                   # the default types of the variant
    sections = (cable, overhead, cable, cable)                         # section 1-2 is an overhead line
    expected = rings.check_ring(installed, units, fixed, np.full(4, 1500.0),
                                [t.r_ohm_per_km for t in sections], [t.x_ohm_per_km for t in sections],
                                [t.max_i_ka for t in sections], np.ones(3, bool), np.ones(3, bool), G, 0.95, 20.0,
                                ev.limits)
    _assert_same(check, expected)
    # a 10 kV district of the same (auto) variant: the 10 kV types and the voltage of the district
    ten = rings.RingEvaluator.from_model(dataclasses.replace(inp, vn_kv=10.0), model)
    assert ten.vn_kv == 10.0 and ten.imax_ka[0, 1] == params.cable_at(10.0).max_i_ka == 0.416
    assert ten.x_ohm_km[0, 1] == params.cable_at(10.0).x_ohm_per_km and ten.imax_ka[1, 2] == params.overhead_at(
        10.0).max_i_ka
    check_10 = ten.evaluate([1, 2, 3])
    assert check_10.drop_a > 3.5 * check.drop_a                         # (20/10)² with nearly the same impedances
