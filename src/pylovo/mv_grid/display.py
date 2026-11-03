"""Display geometry of MV lines that share a route: lanes side by side and a busbar circle at the HV/MV substations.

MV lines that run along the same streets follow the same pieces of the run graph, so their stored geometry
(``mv_line.geom``) is identical there and every map draws them on top of each other. The display geometry
(``mv_line.display_geom``) shifts them sideways into lanes where they share the route, as the LV helpers of
``lines_result_helper`` do for LV lines; ``geom`` and every length stay as they are.

* **Segments**: the lines are noded (on a grid of :attr:`DisplayParameters.snap_m`) into segments; each segment
  carries the lines that run on it.
* **Lanes**: the n lines of a segment lie :attr:`~DisplayParameters.spacing_m` apart; the band is at most
  :attr:`~DisplayParameters.max_band_m` wide, so the spacing shrinks for more lines.
* **Order**: across a segment a line lies on the side where it leaves the bundle. Of two lines, the one that turns
  further left where they part lies left (looking along their common direction); lines that only part behind the
  segment are compared there, and lines that never part keep the order of their keys. Consecutive lines of one path
  (same supply area, ring and role, the only two of that path at a node that is no busbar) count as one line, so a
  ring keeps its lane through its stations.
* **Transitions**: where a line changes lane it moves over :attr:`~DisplayParameters.transition_m`.
* **Ends**: at an HV/MV substation (its busbar node) every line ends where its lane meets the busbar circle: the
  radius is half the widest band at the busbar plus :attr:`~DisplayParameters.busbar_margin_m`, at least
  :attr:`~DisplayParameters.busbar_min_radius_m`. Two consecutive lines of one path meet on their lane (the lower id
  keeps its lane, the other one moves). Every other end bends into its node point.

Lines that need no change get no display geometry. The module is pure (numpy, shapely); reading and writing the
lines is in ``MvResultMixin``.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Hashable, Mapping, Sequence

import numpy as np
import shapely
from shapely import LineString, Point, Polygon


@dataclass(frozen=True)
class DisplayParameters:
    """Lane layout of the display geometry (all in metres)."""

    #: Distance between neighbouring lanes.
    spacing_m: float = 2.0
    #: Widest band (centre of the outer lanes); more lines share it with a smaller spacing.
    max_band_m: float = 12.0
    #: Length over which a line moves to another lane.
    transition_m: float = 10.0
    #: Margin of the busbar circle beyond half the widest band at the busbar.
    busbar_margin_m: float = 2.0
    #: Smallest radius of a busbar circle.
    busbar_min_radius_m: float = 6.0
    #: Grid the lines are snapped to before they are noded.
    snap_m: float = 0.2

    def spacing(self, n: int) -> float:
        """Distance between neighbouring lanes of a segment with ``n`` lines."""
        return min(self.spacing_m, self.max_band_m / (n - 1)) if n > 1 else 0.0

    def band(self, n: int) -> float:
        """Width of the band of ``n`` lines (centre of the outer lanes)."""
        return (n - 1) * self.spacing(n) if n > 1 else 0.0


@dataclass(frozen=True)
class DisplayLine:
    """One MV line: id, stored geometry (metres), end nodes and the path it belongs to."""

    id: int
    geom: LineString
    from_node: Hashable
    to_node: Hashable
    #: Lines with the same key form paths through the nodes where only two of them meet ((area, ring, role)).
    path_key: Hashable | None = None


@dataclass
class DisplayResult:
    """Display geometry of the lines that change, and the busbar circle (x, y, radius) at each busbar node."""

    lines: dict[int, LineString] = field(default_factory=dict)
    busbars: dict[Hashable, tuple[float, float, float]] = field(default_factory=dict)

    def busbar_polygons(self) -> dict[Hashable, Polygon]:
        return {node: Point(x, y).buffer(r, quad_segs=8) for node, (x, y, r) in self.busbars.items()}


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.hypot(v[0], v[1]))
    return v / n if n > 0 else np.zeros(2)


def _left(d: np.ndarray) -> np.ndarray:
    return np.array([-d[1], d[0]])


def _turn(d_in: np.ndarray, d_out: np.ndarray | None) -> float:
    """Signed turn from ``d_in`` to ``d_out`` (radians, left positive); 0 for a line that ends."""
    if d_out is None:
        return 0.0
    return math.atan2(d_in[0] * d_out[1] - d_in[1] * d_out[0], d_in[0] * d_out[0] + d_in[1] * d_out[1])


def _mitre(v: np.ndarray, d1: np.ndarray, d2: np.ndarray, offset: float) -> list[np.ndarray]:
    """Offset point(s) at vertex ``v`` between edges of directions ``d1`` and ``d2`` (a bevel at sharp turns)."""
    if offset == 0:
        return [v]
    n1, n2 = _left(d1), _left(d2)
    denom = 1.0 + float(n1 @ n2)
    if denom > 0.3:
        return [v + offset * (n1 + n2) / denom]
    return [v + offset * n1, v + offset * n2]


class _Cursor:
    """A position on a path: a line, an index into its segment list and the walking direction (+1 along it)."""

    __slots__ = ("line", "idx", "step")

    def __init__(self, line: int, idx: int, step: int) -> None:
        self.line, self.idx, self.step = line, idx, step


class _Layout:
    def __init__(self, lines: Sequence[DisplayLine], busbars: Mapping[Hashable, tuple[float, float]],
                 params: DisplayParameters) -> None:
        self.p = params
        self.busbars = dict(busbars)
        self.info: dict[int, DisplayLine] = {}
        self.geom: dict[int, LineString] = {}
        for ln in lines:
            g = shapely.set_precision(ln.geom, params.snap_m)
            if not g.is_empty and g.geom_type == "LineString" and g.length > 0:
                self.info[ln.id], self.geom[ln.id] = ln, g
        self._segments()
        self._paths()
        self._sides: dict[tuple[int, int, int], int] = {}

    # ------------------------------------------------------------------------------------------------ segments
    def _segments(self) -> None:
        """Node the lines; per line its segments in order with their direction (``seq``)."""
        geoms = list(self.geom.values())
        noded = shapely.unary_union(geoms) if geoms else shapely.GeometryCollection()
        parts = [g for g in getattr(noded, "geoms", [noded]) if g.geom_type == "LineString" and g.length > 0]
        self.coords = [np.asarray(g.coords, dtype=float)[:, :2] for g in parts]
        tree = shapely.STRtree(parts)
        mids = [g.interpolate(0.5, normalized=True) for g in parts]
        self.seq: dict[int, list[tuple[int, bool]]] = {}
        self.members: dict[int, list[int]] = defaultdict(list)
        for i, g in self.geom.items():
            found = []
            for s in tree.query(g):
                if g.distance(mids[s]) > 1e-3:
                    continue
                c = self.coords[s]
                a, b = g.project(Point(c[0])), g.project(Point(c[-1]))
                found.append((min(a, b), int(s), a <= b, abs(b - a)))
            found.sort()
            # a line that runs over its own route twice cannot be ordered by projection: it keeps its geometry
            if not found or abs(sum(f[3] for f in found) - g.length) > max(1.0, 0.01 * g.length):
                continue
            self.seq[i] = [(s, fwd) for _, s, fwd, _ in found]
            for s, _ in self.seq[i]:
                self.members[s].append(i)

    def _dir(self, s: int, forward: bool, at_end: bool) -> np.ndarray:
        """Unit direction of segment ``s`` traversed forward (or backward), at its last (or first) edge."""
        c = self.coords[s] if forward else self.coords[s][::-1]
        return _unit(c[-1] - c[-2]) if at_end else _unit(c[1] - c[0])

    # --------------------------------------------------------------------------------------------------- paths
    def _paths(self) -> None:
        """``nb[(line, at_to_node)] = (neighbour, neighbour's end at that node is its to_node)``."""
        ends: dict[tuple[Hashable, Hashable], list[tuple[int, bool]]] = defaultdict(list)
        for i in self.seq:
            ln = self.info[i]
            if ln.path_key is None:
                continue
            for node, at_to in ((ln.from_node, False), (ln.to_node, True)):
                if node not in self.busbars:
                    ends[(node, ln.path_key)].append((i, at_to))
        self.nb: dict[tuple[int, bool], tuple[int, bool]] = {}
        for found in ends.values():
            if len(found) == 2 and found[0][0] != found[1][0]:
                self.nb[found[0]] = found[1]
                self.nb[found[1]] = found[0]

    def _seg(self, c: _Cursor) -> tuple[int, bool]:
        """Segment under the cursor and whether walking traverses it in its own direction."""
        s, fwd = self.seq[c.line][c.idx]
        return s, fwd if c.step > 0 else not fwd

    def _next(self, c: _Cursor) -> _Cursor | None:
        j = c.idx + c.step
        if 0 <= j < len(self.seq[c.line]):
            return _Cursor(c.line, j, c.step)
        other = self.nb.get((c.line, c.step > 0))
        if other is None or other[0] not in self.seq:
            return None
        line, at_to = other
        return _Cursor(line, len(self.seq[line]) - 1, -1) if at_to else _Cursor(line, 0, 1)

    # ---------------------------------------------------------------------------------------------------- order
    def side(self, a: int, b: int, s: int) -> int:
        """+1 if line ``a`` lies left of line ``b`` on segment ``s`` (looking along the segment), else -1."""
        hit = self._sides.get((a, b, s))
        if hit is not None:
            return hit
        ia = next(k for k, (t, _) in enumerate(self.seq[a]) if t == s)
        ib = next(k for k, (t, _) in enumerate(self.seq[b]) if t == s)
        same = self.seq[a][ia][1] == self.seq[b][ib][1]
        frame: list[tuple[int, bool]] = [(s, self.seq[a][ia][1])]     # shared segments, direction along a at s
        result = 0
        for sign in (1, -1):        # walk along a's direction at s, then against it
            ca, cb = _Cursor(a, ia, sign), _Cursor(b, ib, sign if same else -sign)
            while True:
                na, nb = self._next(ca), self._next(cb)
                sa = None if na is None else self._seg(na)
                sb = None if nb is None else self._seg(nb)
                if sa is not None and sa == sb and len(frame) < 100_000:
                    ca, cb = na, nb
                    frame.append((sa[0], sa[1] if sign > 0 else not sa[1]))
                    continue
                if result == 0:
                    d_in = self._dir(*self._seg(ca), at_end=True)
                    turn = (_turn(d_in, None if sa is None else self._dir(*sa, at_end=False))
                            - _turn(d_in, None if sb is None else self._dir(*sb, at_end=False)))
                    if abs(turn) > 1e-6:
                        result = (1 if turn > 0 else -1) * sign     # left looking backward is right looking ahead
                break
        if result == 0:
            result = 1 if a > b else -1
        for t, along in frame:      # the same answer holds on every shared segment (in the segment's direction)
            o = result if along else -result
            self._sides[(a, b, t)] = o
            self._sides[(b, a, t)] = -o
        return self._sides[(a, b, s)]

    def offsets(self) -> dict[tuple[int, int], float]:
        """Lane offset of every (line, segment), left of the segment's own direction positive."""
        out = {}
        for s, lines in self.members.items():
            n = len(lines)
            if n == 1:
                out[(lines[0], s)] = 0.0
                continue
            order = sorted(sorted(lines), key=cmp_to_key(lambda x, y, s=s: self.side(x, y, s)))
            gap = self.p.spacing(n)
            for rank, line in enumerate(order):
                out[(line, s)] = (rank - (n - 1) / 2) * gap
        return out


def layout(lines: Sequence[DisplayLine], busbars: Mapping[Hashable, tuple[float, float]],
           params: DisplayParameters | None = None) -> DisplayResult:
    """Display geometry of ``lines`` (metres) with the busbar circles at ``busbars`` (node -> x, y).

    Returns:
        The lines whose display geometry differs from their geometry, and every busbar circle.
    """
    p = params or DisplayParameters()
    lay = _Layout(lines, busbars, p)
    off = lay.offsets()

    def along(line: int) -> list[float]:
        """Offset per segment of ``line`` in its own direction."""
        return [off[(line, s)] if fwd else -off[(line, s)] for s, fwd in lay.seq[line]]

    poly: dict[int, tuple[np.ndarray, np.ndarray]] = {}     # line -> vertices, offset per edge
    for line, seq in lay.seq.items():
        pts: list[np.ndarray] = []
        eoff: list[float] = []
        for (s, fwd), o in zip(seq, along(line)):
            c = list(lay.coords[s] if fwd else lay.coords[s][::-1])
            if not pts:
                pts.extend(c)
                eoff.extend([o] * (len(c) - 1))
                continue
            if np.allclose(pts[-1], c[0]):
                c = c[1:]
            pts.extend(c)                               # each new point adds one edge (a gap edge included)
            eoff.extend([o] * len(c))
        poly[line] = (np.asarray(pts, dtype=float), np.asarray(eoff, dtype=float))

    def edge_dir(line: int, at_to: bool) -> tuple[np.ndarray, float]:
        """Direction (along the line) and offset of the line's edge at one of its ends."""
        v, o = poly[line]
        return (_unit(v[-1] - v[-2]), float(o[-1])) if at_to else (_unit(v[1] - v[0]), float(o[0]))

    # busbar circles: half the widest band leaving the busbar plus the margin
    radius = {node: p.busbar_min_radius_m for node in busbars}
    for line, seq in lay.seq.items():
        ln = lay.info[line]
        for node, (s, _) in ((ln.from_node, seq[0]), (ln.to_node, seq[-1])):
            if node in radius:
                radius[node] = max(radius[node], p.band(len(lay.members[s])) / 2 + p.busbar_margin_m)
    result = DisplayResult(busbars={node: (float(x), float(y), radius[node]) for node, (x, y) in busbars.items()})

    for line in lay.seq:
        v, o = poly[line]
        if len(v) < 2 or len(o) != len(v) - 1:
            continue
        ln = lay.info[line]
        dirs = [_unit(v[k + 1] - v[k]) for k in range(len(v) - 1)]
        ends = {}
        for at_to, node in ((False, ln.from_node), (True, ln.to_node)):
            own = float(o[-1] if at_to else o[0])
            if node in busbars:
                ends[at_to] = ("busbar", own, None)
            elif (line, at_to) in lay.nb:
                other, other_at_to = lay.nb[(line, at_to)]
                d_other, o_other = edge_dir(other, other_at_to)
                # in this line's direction: the neighbour ends where this line starts (or the other way round)
                flip = other_at_to == at_to
                o_other = -o_other if flip else o_other
                d_other = -d_other if flip else d_other
                joint = own if line < other else o_other            # the lower id keeps its lane
                d_in, d_out = (dirs[-1], d_other) if at_to else (d_other, dirs[0])
                ends[at_to] = ("joint", joint, _mitre(v[-1] if at_to else v[0], d_in, d_out, joint))
            else:
                ends[at_to] = ("node", 0.0, None)
        if all(abs(x) < 1e-9 for x in o) and ends[False][0] != "busbar" and ends[True][0] != "busbar" \
                and all(e[1] == 0 for e in ends.values()):
            continue
        out = _offset_polyline(v, o, dirs, ends, p.transition_m)
        for at_to, node in ((False, ln.from_node), (True, ln.to_node)):
            if ends[at_to][0] == "busbar":
                x, y, r = result.busbars[node]
                out = _cut_circle(out, np.array([x, y]), r, at_end=at_to)
        if len(out) >= 2:
            result.lines[line] = LineString(out)
    return result


def _offset_polyline(v: np.ndarray, o: np.ndarray, dirs: list[np.ndarray], ends: Mapping[bool, tuple],
                     transition: float) -> np.ndarray:
    """Vertices of the polyline ``v`` shifted by the offset ``o`` of each edge, with the lane changes and ends."""
    pts: list[np.ndarray] = []

    def at(k: int, dist: float, offset: float) -> np.ndarray:
        """Point ``dist`` along edge ``k``, shifted by ``offset``."""
        return v[k] + dirs[k] * dist + _left(dirs[k]) * offset

    def length(k: int) -> float:
        return float(np.hypot(*(v[k + 1] - v[k])))

    kind, start, joint = ends[False]
    if kind == "joint":
        pts.extend(joint)
    else:
        pts.append(v[0] + _left(dirs[0]) * start)
    if start != o[0]:
        pts.append(at(0, min(transition, 0.5 * length(0)), o[0]))
    for k in range(1, len(v) - 1):
        if o[k] == o[k - 1]:
            pts.extend(_mitre(v[k], dirs[k - 1], dirs[k], o[k]))
        else:
            pts.append(v[k] + _left(dirs[k - 1]) * o[k - 1])
            pts.append(at(k, min(transition, 0.5 * length(k)), o[k]))
    kind, end, joint = ends[True]
    last = len(v) - 2
    if end != o[last]:
        pts.append(at(last, max(length(last) - min(transition, 0.5 * length(last)), 0.0), o[last]))
    if kind == "joint":
        pts.extend(joint)
    else:
        pts.append(v[-1] + _left(dirs[last]) * end)
    arr = np.asarray(pts, dtype=float)
    keep = np.ones(len(arr), dtype=bool)
    keep[1:] = np.hypot(*(np.diff(arr, axis=0).T)) > 1e-6
    return arr[keep]


def _cut_circle(pts: np.ndarray, centre: np.ndarray, r: float, at_end: bool) -> np.ndarray:
    """The polyline from (to) where it leaves (enters) the circle; unchanged if it never leaves it."""
    seq = pts[::-1] if at_end else pts
    dist = np.hypot(*(seq - centre).T)
    outside = np.nonzero(dist >= r)[0]
    if len(outside) == 0 or outside[0] == 0:
        return pts
    k = int(outside[0])
    a, b = seq[k - 1], seq[k]
    d = b - a
    # |a + t d - c| = r, the root in [0, 1] with a inside the circle
    f = a - centre
    qa, qb, qc = float(d @ d), 2 * float(f @ d), float(f @ f) - r * r
    t = (-qb + math.sqrt(max(qb * qb - 4 * qa * qc, 0.0))) / (2 * qa) if qa > 0 else 0.0
    cut = np.vstack([a + min(max(t, 0.0), 1.0) * d, seq[k:]])
    return cut[::-1] if at_end else cut
