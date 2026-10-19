"""Tests of the display geometry of MV lines that share a route (pure shapely, no database)."""
from __future__ import annotations

import numpy as np
import pytest
from shapely import LineString, Point

from pylovo.mv_grid.display import DisplayLine, DisplayParameters, _cut_circle, layout

P = DisplayParameters(spacing_m=2.0, max_band_m=12.0, transition_m=10.0, busbar_margin_m=2.0,
                      busbar_min_radius_m=6.0, snap_m=0.2)
BUSBAR = {"bb": (0.0, 0.0)}


def line(i, coords, a, b, key=None):
    return DisplayLine(i, LineString(coords), a, b, key)


def mean_y(g, x0, x1):
    """Mean y of points along the display line with x0 < x < x1."""
    pts = np.array([g.interpolate(f, normalized=True).coords[0] for f in np.linspace(0, 1, 2001)])
    sel = pts[(pts[:, 0] > x0) & (pts[:, 0] < x1) & (np.abs(pts[:, 1]) < 50)]
    return float(sel[:, 1].mean())


def test_lines_lie_on_the_side_where_they_leave_the_bundle():
    # both leave the busbar east; A turns left (north) at x = 300, B turns right (south)
    a = line(1, [(0, 0), (300, 0), (300, 200)], "bb", "sa")
    b = line(2, [(0, 0), (300, 0), (300, -200)], "bb", "sb")
    out = layout([b, a], BUSBAR, P)
    assert mean_y(out.lines[1], 50, 250) == pytest.approx(1.0, abs=0.01)
    assert mean_y(out.lines[2], 50, 250) == pytest.approx(-1.0, abs=0.01)
    # both start on the busbar circle (radius max(6, 1 + 2) = 6) and end exactly at their stations
    assert out.busbars["bb"] == (0.0, 0.0, 6.0)
    for i, end in ((1, (300, 200)), (2, (300, -200))):
        g = out.lines[i]
        assert Point(g.coords[0]).distance(Point(0, 0)) == pytest.approx(6.0, abs=1e-6)
        assert g.coords[-1] == pytest.approx(end)


def test_the_band_is_capped_and_the_busbar_grows_with_it():
    lines = [line(i, [(0, 0), (500, 0), (500, 100 + 10 * i)], "bb", f"s{i}") for i in range(10)]
    out = layout(lines, BUSBAR, P)
    ys = sorted(mean_y(out.lines[i], 50, 450) for i in range(10))
    assert ys[-1] - ys[0] == pytest.approx(12.0, abs=0.05)                # 9 gaps of 12/9 m
    assert out.busbars["bb"][2] == pytest.approx(12.0 / 2 + 2.0)


def test_a_ring_keeps_its_lane_through_its_station():
    # ring lines R1 (busbar -> station S) and R2 (S -> north) form one path; B shares the street and goes south
    r1 = line(1, [(0, 0), (150, 0)], "bb", "S", key=(7, 1, "ring"))
    r2 = line(2, [(150, 0), (300, 0), (300, 200)], "S", "n", key=(7, 1, "ring"))
    b = line(3, [(0, 0), (300, 0), (300, -200)], "bb", "s", key=(7, 2, "ring"))
    out = layout([r1, r2, b], BUSBAR, P)
    assert mean_y(out.lines[1], 20, 140) == pytest.approx(1.0, abs=0.01)  # decided where R2 leaves, behind S
    assert mean_y(out.lines[2], 160, 290) == pytest.approx(1.0, abs=0.01)
    assert out.lines[1].coords[-1] == pytest.approx(out.lines[2].coords[0])  # they meet on their lane
    assert mean_y(out.lines[3], 20, 290) == pytest.approx(-1.0, abs=0.01)


def test_a_line_bends_into_its_station_and_a_lone_line_keeps_its_geometry():
    # A ends at station S on the shared street (no other line of its path there); C runs alone
    a = line(1, [(0, 0), (200, 0)], "bb", "S")
    b = line(2, [(0, 0), (400, 0), (400, -100)], "bb", "t")
    c = line(3, [(1000, 1000), (1200, 1000)], "x", "y")
    out = layout([a, b, c], BUSBAR, P)
    assert out.lines[1].coords[-1] == pytest.approx((200, 0))
    assert 3 not in out.lines


def test_a_line_from_the_busbar_alone_still_starts_on_the_circle():
    out = layout([line(1, [(0, 0), (100, 0)], "bb", "s")], BUSBAR, P)
    assert out.lines[1].coords[0] == pytest.approx((6.0, 0.0))
    assert out.busbars["bb"] == (0.0, 0.0, 6.0)


def test_cut_circle_at_either_end():
    pts = np.array([[0.0, 0.0], [10.0, 0.0], [20.0, 0.0]])
    assert _cut_circle(pts, np.array([0.0, 0.0]), 5.0, at_end=False)[0] == pytest.approx((5.0, 0.0))
    assert _cut_circle(pts, np.array([20.0, 0.0]), 5.0, at_end=True)[-1] == pytest.approx((15.0, 0.0))
    inside = np.array([[0.0, 0.0], [1.0, 0.0]])
    assert _cut_circle(inside, np.array([0.0, 0.0]), 5.0, at_end=False).tolist() == inside.tolist()
