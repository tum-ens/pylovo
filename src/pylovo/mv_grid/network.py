"""The network of one MV district built from the decisions of its topology: lines, open points, geometries.

:class:`Decisions` holds what :mod:`~pylovo.mv_grid.topology` decides (the feeder structures, stubs and flags).
:func:`layout` lays them out as a :class:`Network`: the lines of the rings, radials and dedicated feeders, the stubs
on their T-joints and their routes. :func:`materialize` turns a network into the
:class:`~pylovo.mv_grid.model.TopologyPlan`: the normally-open point of every ring at its voltage balance
(:func:`choose_nop`, only at a switchgear node), the feeders, the geometries along the run-graph pieces and the line
types, keeping the parallel cables and upgraded conductors of an earlier plan on the same street pieces.

Pure module: no database, configuration or plotting import; the ring evaluator is imported on first use.
"""
from __future__ import annotations

import dataclasses
import importlib
import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Sequence

import numpy as np
from shapely.geometry import LineString
from shapely.ops import substring

from pylovo.mv_grid import equipment
from pylovo.mv_grid.model import MEMBER_KINDS, LinePlan, RingCheck, RingPlan, TopologyPlan, line_kind
from pylovo.mv_grid.routing import Route, bridge_m, route_overhead_m, route_pieces, span, street

if TYPE_CHECKING:
    from pylovo.mv_grid.topology import DistrictContext

#: Shortest stored line (no zero-impedance lines between co-located nodes).
MIN_LINE_M: float = 1.0


def _rings():
    """The ring evaluator module (imported on first use)."""
    return importlib.import_module("pylovo.mv_grid.rings")



@dataclass
class Line:
    """A line while the plan is built (node keys, route, kind, role)."""

    a: int
    b: int
    route: Route
    kind: str
    role: str
    ring_id: int
    parallel: int = 1
    normally_open: bool = False
    open_at: int | None = None
    exit: str | None = None
    searchable: bool = True                # stubs and T-offs may attach to it
    std_type: str | None = None            # an upgraded conductor (None: the district's type of the kind)


@dataclass
class Attach:
    """The nearest attachment point of a node on a line."""

    dist: float                            # routing cost of the connection (m; OSM lines discounted)
    line: int
    pos: int                               # street: index in route.verts; -1 on a straight span
    foot: np.ndarray                       # the attachment point
    t: float                               # straight span: position 0..1
    path: np.ndarray | None = None         # street: vertices from the node to the attachment vertex
    cum: np.ndarray | None = None
    length: float = math.nan               # true length of the connection (m), checked against the limits


@dataclass
class Struct:
    """One feeder structure: a ring, a busbar radial or a dedicated customer feeder."""

    ring_id: int
    kind: str                              # ring | radial | dedicated
    rows: list[int]                        # ring order from the A exit; one row else


@dataclass
class Decisions:
    structs: list[Struct] = field(default_factory=list)
    stubs: list[tuple[int, int]] = field(default_factory=list)   # (row, ring_id), parents before children
    flags: dict[int, list[str]] = field(default_factory=dict)      # node key -> flags

    def next_ring_id(self) -> int:
        return max((s.ring_id for s in self.structs), default=-1) + 1

    def flag(self, ctx: DistrictContext, row: int, text: str) -> None:
        values = self.flags.setdefault(ctx.key(row), [])
        if text not in values:
            values.append(text)


class Network:
    """The lines of a district while it is laid out from its decisions."""

    def __init__(self, ctx: DistrictContext) -> None:
        self.ctx = ctx
        self.lines: list[Line] = []
        self.structs: dict[int, Struct] = {}
        self.chain: dict[int, list[int]] = {}          # ring/radial id -> main line indices from the busbar
        self.feeder_lines: dict[int, list[int]] = {}   # dedicated feeder id -> its lines
        self.chain_of_line: dict[int, int] = {}
        self.parent: dict[int, int] = {}               # stub, T-off and tree-joint key -> parent key
        self.depth: dict[int, float] = {}              # stub tree length from the attachment on the main path
        self.tree_of: dict[int, int] = {}              # stub key / tree joint key -> tree id
        self.role: dict[int, str] = {}                 # stub | joint
        self.node_ring: dict[int, int] = {}
        self.joint_xy: dict[int, tuple[float, float]] = {}
        self.flags: dict[int, list[str]] = {}
        self._next_joint = -1

    # ------------------------------------------------------------------ building
    def overhead_m(self, line: Line) -> float:
        """Overhead length of a line: all of an overhead line, none of a cable, the OSM pieces of a mixed route."""
        if line.kind == "overhead":
            return float(line.route.length)
        if line.kind == "cable":
            return 0.0
        return route_overhead_m(self.ctx, line.route)

    def flag(self, key: int, text: str) -> None:
        values = self.flags.setdefault(int(key), [])
        if text not in values:
            values.append(text)

    def add_line(self, line: Line) -> int:
        self.lines.append(line)
        return len(self.lines) - 1

    def link(self, a: int, b: int, role: str, ring_id: int) -> Line:
        """The line between terminal rows ``a`` and ``b`` by the link rule of the distance model."""
        ctx = self.ctx
        ta, tb = ctx.t_of_row[a], ctx.t_of_row[b]
        found = ctx.router.path(ctx.vertex[a], ctx.vertex[b], float(ctx.model.street_m[ta, tb]))
        if found is None:
            route = span(ctx.xy[a], ctx.xy[b], ctx.p.bridge_detour)
            self.flag(ctx.key(b), "no_street_path")
        else:
            route = street(ctx, *found)
        kind = line_kind(route_overhead_m(ctx, route), route.length)
        return Line(ctx.key(a), ctx.key(b), route, kind, role, ring_id)

    def add_structure(self, struct: Struct) -> None:
        ctx = self.ctx
        rid = struct.ring_id
        self.structs[rid] = struct
        bb = ctx.busbar_row
        if struct.kind == "ring":
            seq = [bb, *struct.rows, bb]
            idx = []
            for j in range(len(seq) - 1):
                line = self.link(seq[j], seq[j + 1], "ring", rid)
                line.exit = "a" if j == 0 else ("b" if j == len(seq) - 2 else None)
                idx.append(self.add_line(line))
            self.chain[rid] = idx
            self.chain_of_line.update({i: rid for i in idx})
            for row in struct.rows:
                self.node_ring[ctx.key(row)] = rid
        elif struct.kind == "radial":
            i = self.add_line(self.link(bb, struct.rows[0], "stub", rid))
            self.chain[rid] = [i]
            self.chain_of_line[i] = rid
            self.node_ring[ctx.key(struct.rows[0])] = rid
        else:
            row = struct.rows[0]
            line = self.link(bb, row, "dedicated", rid)
            line.searchable = False
            line.parallel = self._feeder_parallel(row, line.kind)
            line.exit = "a"
            legs = [line, dataclasses.replace(line, exit="b", normally_open=True, open_at=ctx.key(row))]
            self.feeder_lines[rid] = [self.add_line(leg) for leg in legs]
            self.node_ring[ctx.key(row)] = rid

    def _feeder_parallel(self, row: int, kind: str) -> int:
        """Parallel cables of a dedicated feeder by the customer load at the planning utilization; capped by
        ``max_parallel_cables`` (flag ``dedicated_overload``)."""
        ctx, p = self.ctx, self.ctx.p
        s_th = math.sqrt(3.0) * ctx.vn_kv * ctx.section_values(kind, 1.0, 0.5)[2] * 1000.0
        need = ctx.fixed[row] / ctx.pf / (p.line_utilization * s_th)
        n = max(1, math.ceil(need - 1e-9))
        if n > p.max_parallel_cables:
            self.flag(ctx.key(row), "dedicated_overload")
            n = int(p.max_parallel_cables)
        return n

    def new_joint(self, xy: np.ndarray) -> int:
        key = self._next_joint
        self._next_joint -= 1
        self.joint_xy[key] = (float(xy[0]), float(xy[1]))
        self.role[key] = "joint"
        return key

    def split(self, i: int, att: Attach) -> int:
        """Split line ``i`` at the attachment point; returns the joint key."""
        line = self.lines[i]
        route = line.route
        if route.verts is not None:
            pos = att.pos
            first = Route(route.verts[:pos + 1], route.cum[:pos + 1], route.p0, self.ctx.vertex_xy[route.verts[pos]])
            second = Route(route.verts[pos:], route.cum[pos:] - route.cum[pos], first.p1, route.p1)
            xy = first.p1
        else:
            xy = np.asarray(att.foot, dtype=float)
            first = span(route.p0, xy, route.detour)
            second = span(xy, route.p1, route.detour)
        joint = self.new_joint(xy)
        self.node_ring[joint] = line.ring_id
        kinds = (line.kind, line.kind)
        if line.kind == "mixed":
            kinds = tuple(line_kind(route_overhead_m(self.ctx, part), part.length) for part in (first, second))
        self.lines[i] = dataclasses.replace(line, b=joint, route=first, kind=kinds[0],
                                            exit="a" if line.exit == "a" else None)
        j = self.add_line(dataclasses.replace(line, a=joint, route=second, kind=kinds[1],
                                              exit="b" if line.exit == "b" else None))
        rid = self.chain_of_line.get(i)
        if rid is not None:
            chain = self.chain[rid]
            chain.insert(chain.index(i) + 1, j)
            self.chain_of_line[j] = rid
        else:
            self.parent[joint] = line.a
            self.parent[line.b] = joint
            self.depth[joint] = self.depth.get(line.a, 0.0) + first.length
            if line.b in self.tree_of:
                self.tree_of[joint] = self.tree_of[line.b]
        return joint

    def attach(self, row: int, att: Attach) -> None:
        """Connect node ``row`` at the attachment point with a stub."""
        ctx = self.ctx
        line = self.lines[att.line]
        key = ctx.key(row)
        route = line.route
        if route.verts is not None:
            last = len(route.verts) - 1
            anchor = line.a if att.pos == 0 else (line.b if att.pos == last else None)
        else:
            anchor = line.a if att.t <= 0.0 else (line.b if att.t >= 1.0 else None)
        if anchor is None:
            anchor = self.split(att.line, att)
        anchor_xy = ctx.point(anchor, self) if anchor != ctx.busbar_key else ctx.xy[ctx.busbar_row]
        if route.verts is not None:
            verts = att.path[::-1].copy()
            cum = att.cum[-1] - att.cum[::-1]
            new_route = street(ctx, verts, cum)
        else:
            new_route = span(anchor_xy, ctx.xy[row], route.detour)
        new_kind = line.kind
        if new_route.verts is not None:
            new_kind = line_kind(route_overhead_m(ctx, new_route), new_route.length)
        self.add_line(Line(anchor, key, new_route, new_kind, "stub", line.ring_id))
        self.parent[key] = anchor
        self.node_ring[key] = line.ring_id
        self.role[key] = "stub"
        self.depth[key] = self.depth.get(anchor, 0.0) + new_route.length
        self.tree_of[key] = self.tree_of.get(anchor, key)

    # ------------------------------------------------------------------ searching
    def searchable(self) -> list[int]:
        return [i for i, line in enumerate(self.lines) if line.searchable]

    def nearest(self, row: int, limit: float, line_ids: Sequence[int]) -> Attach | None:
        """The nearest point of node ``row`` on the given lines whose connection is at most ``limit`` long (street:
        the cheapest route from the node, nearest by routing cost, with its true length within ``limit``; straight
        span: the stretched straight distance to the foot point); never the busbar."""
        ctx = self.ctx
        line_ids = [i for i in line_ids if self.lines[i].searchable]
        on_street = [i for i in line_ids if self.lines[i].route.verts is not None]
        best: Attach | None = None
        if on_street:
            verts = np.concatenate([self.lines[i].route.verts for i in on_street])
            owner = np.concatenate([np.full(len(self.lines[i].route.verts), i) for i in on_street])
            pos = np.concatenate([np.arange(len(self.lines[i].route.verts)) for i in on_street])
            keep = verts != ctx.busbar_vertex
            verts, owner, pos = verts[keep], owner[keep], pos[keep]
            if len(verts):
                tree = ctx.router.tree(int(ctx.vertex[row]), limit)    # costs never exceed true lengths
                dist = tree.distance(verts)
                ok = np.isfinite(dist) & (dist <= limit)
                sel = np.flatnonzero(ok)
                for j in sel[np.lexsort((pos[sel], owner[sel], dist[sel]))]:
                    target = int(verts[j])
                    path = tree.path(target)
                    cum = tree.distance(path)
                    # The length also enters the accumulated stub-tree depth below;
                    # a safe individual cost bound cannot replace the true length.
                    length = street(ctx, path, cum).length if ctx.costed else float(dist[j])
                    if length <= limit:
                        best = Attach(float(dist[j]), int(owner[j]), int(pos[j]), ctx.vertex_xy[target].copy(),
                                       -1.0, path, cum, length)
                        break
        src = ctx.xy[row]
        for i in line_ids:
            line = self.lines[i]
            route = line.route
            if route.verts is not None:
                continue
            vec = route.p1 - route.p0
            length2 = float(vec @ vec)
            t = 0.0 if length2 == 0.0 else min(1.0, max(0.0, float((src - route.p0) @ vec) / length2))
            if (t <= 0.0 and line.a == ctx.busbar_key) or (t >= 1.0 and line.b == ctx.busbar_key):
                continue
            foot = route.p0 + t * vec
            dist = route.detour * float(np.hypot(*(src - foot)))
            if dist <= limit and (best is None or dist < best.dist):
                best = Attach(dist, i, -1, foot, t, length=dist)
        return best

    def trees(self, ring_id: int) -> dict[int, list[int]]:
        """Stub lines of a ring or radial grouped by stub tree."""
        out: dict[int, list[int]] = {}
        for i, line in enumerate(self.lines):
            if line.ring_id == ring_id and line.role == "stub" and i not in self.chain_of_line:
                out.setdefault(self.tree_of.get(line.b, line.b), []).append(i)
        return out

    def depth_at(self, att: Attach) -> float:
        line = self.lines[att.line]
        base = self.depth.get(line.a, 0.0)
        if line.route.verts is not None:
            return base + float(line.route.cum[att.pos])
        return base + line.route.detour * float(np.hypot(*(att.foot - line.route.p0)))

    def stub_attachment(self, row: int, ring_id: int) -> Attach | None:
        """The nearest allowed attachment of a stub: on the main path of the ring (or radial) or on one of its stub
        trees, within the stub limits: the tree's coincident load at most ``stub_max_kva`` and every node of the tree
        within the length limit of that load (:meth:`DistrictContext.stub_limit_m`)."""
        ctx, p = self.ctx, self.ctx.p
        best: tuple[float, int, int, Attach] | None = None
        load = ctx.coincident_kva([row])
        if load <= p.stub_max_kva:
            att = self.nearest(row, ctx.stub_limit_m(load), self.chain.get(ring_id, []))
            if att is not None:
                best = (att.dist, 0, 0, att)
        for tree_id, line_ids in sorted(self.trees(ring_id).items()):
            keys = [k for k, t in self.tree_of.items() if t == tree_id and k > 0]
            load = ctx.coincident_kva([ctx.row_of_key[k] for k in keys] + [row])
            if load > p.stub_max_kva:
                continue
            lim = ctx.stub_limit_m(load)
            if max((self.depth.get(k, 0.0) for k in keys), default=0.0) > lim:
                continue                                # the heavier tree would be too long already
            att = self.nearest(row, lim, line_ids)
            if att is None or self.depth_at(att) + att.length > lim:
                continue
            if best is None or (att.dist, 1, tree_id) < best[:3]:
                best = (att.dist, 1, tree_id, att)
        return None if best is None else best[3]

    def add_stub(self, row: int, ring_id: int, enforce: bool) -> bool:
        """Attach stub ``row`` to its ring (or radial). With ``enforce`` a stub outside the limits fails; else it
        hangs on the nearest point of the main path (flag ``stub_limit``)."""
        if ring_id not in self.chain:
            return False
        att = self.stub_attachment(row, ring_id)
        if att is None:
            if enforce:
                return False
            att = self.nearest(row, math.inf, self.chain[ring_id])
            if att is None:
                return False
            self.flag(self.ctx.key(row), "stub_limit")
        self.attach(row, att)
        return True

    # ------------------------------------------------------------------ rings
    def chain_keys(self, ring_id: int) -> list[int]:
        idx = self.chain[ring_id]
        return [self.lines[idx[0]].a] + [self.lines[i].b for i in idx]

    def ring_check(self, ring_id: int, relaxed: bool = False) -> RingCheck:
        keys = self.chain_keys(ring_id)[1:-1]
        sections = [(self.lines[i].route.length, self.lines[i].kind, self.lines[i].parallel,
                     self.overhead_m(self.lines[i]), self.lines[i].std_type) for i in self.chain[ring_id]]
        return _ring_check(self.ctx, keys, sections, children(self.parent), relaxed)


def layout(ctx: DistrictContext, dec: Decisions) -> Network:
    """Lay out all decisions: structures, then stubs (in build order)."""
    net = Network(ctx)
    for struct in sorted(dec.structs, key=lambda s: s.ring_id):
        net.add_structure(struct)
    for row, rid in dec.stubs:
        if not net.add_stub(row, rid, enforce=False):
            net.flag(ctx.key(row), "stub_unattached")
    return net


# ============================================================================================ helpers: open points
def children(parent: dict[int, int]) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    for child, par in parent.items():
        kids.setdefault(int(par), []).append(int(child))
    return kids


def _below(key: int, kids: dict[int, list[int]]) -> list[int]:
    """Keys hanging below ``key`` (stubs, T-offs, tree joints), depth first."""
    out, stack = [], list(kids.get(key, []))
    while stack:
        k = stack.pop()
        out.append(k)
        stack.extend(kids.get(k, []))
    return out


def _ring_check(ctx: DistrictContext, pos_keys: Sequence[int], sections: Sequence[tuple[float, str, int]],
                kids: dict[int, list[int]], relaxed: bool = False) -> RingCheck:
    """:func:`pylovo.mv_grid.rings.check_ring` for explicit ring positions (joints included); the
    loads of stubs and T-offs are added to the position they hang on."""
    k = len(pos_keys)
    inst, units, fixed = np.zeros((k, 4)), np.zeros((k, 4)), np.zeros(k)
    member = np.zeros(k, dtype=bool)
    weight = np.zeros(k)
    for j, key in enumerate(pos_keys):
        rows = [ctx.row_of_key[x] for x in [key, *_below(key, kids)] if x > 0]
        if rows:
            inst[j] = ctx.inst[rows].sum(axis=0)
            units[j] = ctx.units[rows].sum(axis=0)
            fixed[j] = ctx.fixed[rows].sum()
        member[j] = key > 0 and ctx.kind[ctx.row_of_key[key]] in MEMBER_KINDS
        weight[j] = ctx.cap_weight[ctx.row_of_key[key]] if key > 0 else 0.0
    length = np.array([max(MIN_LINE_M, s[0]) for s in sections], dtype=float)
    values = np.array([ctx.section_values(s[1], s[0], s[3] if len(s) > 3 else 0.0, s[4] if len(s) > 4 else None)
                       for s in sections], dtype=float).reshape(-1, 3)
    parallel = np.array([max(1, int(s[2])) for s in sections], dtype=float)
    r = values[:, 0] / parallel
    x = values[:, 1] / parallel
    imax = values[:, 2] * parallel
    return _rings().check_ring(inst, units, fixed, length, r, x, imax, weight, member, ctx.g, ctx.pf,
                               ctx.vn_kv, ctx.relaxed if relaxed else ctx.limits)


def _valid_nop(check: RingCheck, member: Sequence[bool]) -> bool:
    k = len(member)
    m, n = int(check.open_section), int(check.nop_position)
    if not ((k == 1 and m == 1) or (k >= 2 and 1 <= m <= k - 1)):
        return False
    return n in (m, m + 1) and 1 <= n <= k and bool(member[n - 1])


def choose_nop(ctx: DistrictContext, pos_keys: Sequence[int], sections: Sequence[tuple[float, str, int]],
                kids: dict[int, list[int]]) -> tuple[int, int, RingCheck]:
    """Open section ``m`` and open-point position of a ring, and its (strict) check.

    The strict voltage balance if the ring is feasible, else the balance without limits, else the allowed section
    that halves the ring length best.
    """
    member = [k > 0 and ctx.kind[ctx.row_of_key[k]] in MEMBER_KINDS for k in pos_keys]
    check = _ring_check(ctx, pos_keys, sections, kids)
    if check.feasible and _valid_nop(check, member):
        return int(check.open_section), int(check.nop_position), check
    relaxed = _ring_check(ctx, pos_keys, sections, kids, relaxed=True)
    if _valid_nop(relaxed, member):
        return int(relaxed.open_section), int(relaxed.nop_position), check
    k = len(pos_keys)
    cum = np.cumsum([s[0] for s in sections])
    best = None
    for m in ([1] if k == 1 else range(1, k)):
        nop = m if member[m - 1] else (m + 1 if m < k and member[m] else None)
        if nop is None:
            continue
        score = (abs(cum[m - 1] - (cum[-1] - cum[m])), m)
        if best is None or score < best[0]:
            best = (score, m, nop)
    if best is None:
        raise ValueError(f"ring with positions {list(pos_keys)} has no switchgear node for its open point")
    return best[1], best[2], check


def chain_feeder(j: int, m: int, nop: int, fa: int, fb: int) -> int:
    """Feeder of ring section ``j``: A before the open section, B after it; the open section belongs to the half
    its closed end is in."""
    if j < m:
        return fa
    if j > m:
        return fb
    return fb if nop == m else fa


def ring_feeders(pos_keys: Sequence[int], m: int, fa: int, fb: int,
                  kids: dict[int, list[int]]) -> dict[int, int]:
    out = {}
    for j, key in enumerate(pos_keys):
        feeder = fa if j < m else fb
        out[key] = feeder
        for below in _below(key, kids):
            out[below] = feeder
    return out


def plan_chain(plan: TopologyPlan, ring_id: int) -> list[int] | None:
    """Indices in ``plan.lines`` of the ring sections of a ring, from the A exit to the B exit."""
    sections = [i for i, line in enumerate(plan.lines) if line.ring_id == ring_id and line.role == "ring"]
    start = next((i for i in sections if plan.lines[i].exit == "a"), None)
    if start is None:
        return None
    chain, used = [start], {start}
    while plan.lines[chain[-1]].exit != "b":
        current = plan.lines[chain[-1]].to_key
        nxt = next((i for i in sections if i not in used and plan.lines[i].from_key == current), None)
        if nxt is None:
            return None
        chain.append(nxt)
        used.add(nxt)
    return chain


# ============================================================================================ helpers: the plan
def materialize(ctx: DistrictContext, dec: Decisions, old: TopologyPlan | None = None) -> TopologyPlan:
    """The plan of the decisions: layout, kept parallels, open points, feeders, geometries."""
    net = layout(ctx, dec)
    pieces = {i: route_pieces(ctx, line.route) for i, line in enumerate(net.lines)}
    if old is not None:
        _keep_parallels(net, pieces, old)
        _keep_types(ctx, net, pieces, old)
    geoms = _edge_geometries(ctx, pieces)

    lines_out: list[LinePlan] = []
    rings_out: list[RingPlan] = []
    node_role: dict[int, str] = {ctx.busbar_key: "busbar"}
    node_feeder: dict[int, int] = {}
    node_ring: dict[int, int] = {}
    node_position: dict[int, int] = {}
    stub_parent: dict[int, int] = {}
    kids = children(net.parent)
    feeder = 0

    def emit(i: int, feeder_id: int) -> None:
        line = net.lines[i]
        p0, p1 = ctx.point(line.a, net), ctx.point(line.b, net)
        length = max(MIN_LINE_M, line.route.length)
        overhead_m = min(length, net.overhead_m(line) * length / max(line.route.length, 1e-9))
        lines_out.append(LinePlan(
            key=len(lines_out), from_key=int(line.a), to_key=int(line.b), role=line.role, kind=line.kind,
            feeder_id=feeder_id, ring_id=line.ring_id, length_m=length,
            cable_m=length - overhead_m, overhead_m=overhead_m,
            straight_m=float(np.hypot(*(p1 - p0))), bridge_m=bridge_m(ctx, line.route),
            geometry=_geometry(ctx, line.route, pieces[i], geoms, p0, p1),
            std_type=line.std_type or ctx.std_type(line.kind), parallel=int(line.parallel),
            normally_open=bool(line.normally_open), open_at_key=None if line.open_at is None else int(line.open_at),
            exit=line.exit, pieces=tuple(int(x) for x in pieces[i])))

    for rid in sorted(net.structs):
        struct = net.structs[rid]
        if struct.kind == "ring":
            chain = net.chain[rid]
            pos_keys = net.chain_keys(rid)[1:-1]
            sections = [(net.lines[i].route.length, net.lines[i].kind, net.lines[i].parallel,
                         net.overhead_m(net.lines[i]), net.lines[i].std_type) for i in chain]
            m, nop, check = choose_nop(ctx, pos_keys, sections, kids)
            net.lines[chain[m]].normally_open = True
            net.lines[chain[m]].open_at = pos_keys[nop - 1]
            fa, fb = feeder, feeder + 1
            feeder += 2
            feeders = ring_feeders(pos_keys, m, fa, fb, kids)
            node_feeder.update(feeders)
            for j, key in enumerate(pos_keys):
                node_position[key] = j + 1
                node_ring[key] = rid
                if key < 0:
                    node_role[key] = "joint"
                else:
                    node_role[key] = "ring"
            for j, i in enumerate(chain):
                emit(i, chain_feeder(j, m, nop, fa, fb))
            rings_out.append(RingPlan(rid, "ring", list(pos_keys), pos_keys[nop - 1], check, fa, fb))
        elif struct.kind == "radial":
            chain = net.chain[rid]
            keys = net.chain_keys(rid)
            for j, key in enumerate(keys[1:]):
                node_position[key] = j + 1
                node_ring[key] = rid
                node_feeder[key] = feeder
                node_role[key] = "joint" if key < 0 else "stub"
                stub_parent[key] = keys[j]
                for below in _below(key, kids):
                    node_feeder[below] = feeder
            for i in chain:
                emit(i, feeder)
            rings_out.append(RingPlan(rid, "radial", list(keys[1:]), None, None, feeder, None))
            feeder += 1
        else:
            key = ctx.key(struct.rows[0])
            legs = net.feeder_lines[rid]
            node_position[key] = 1
            node_ring[key] = rid
            node_role[key] = "dedicated"
            node_feeder[key] = feeder
            for n, i in enumerate(legs):
                emit(i, feeder + n)
            rings_out.append(RingPlan(rid, struct.kind, [key], key if len(legs) > 1 else None, None, feeder,
                                      feeder + 1 if len(legs) > 1 else None))
            feeder += len(legs)
        for i, line in enumerate(net.lines):
            if line.ring_id == rid and i not in net.chain_of_line and line.role == "stub" \
                    and line.searchable:
                emit(i, node_feeder.get(line.b, node_feeder.get(line.a)))
    for key, role in net.role.items():
        node_role[key] = role
        node_ring.setdefault(key, net.node_ring.get(key))
        if key in net.parent:
            stub_parent[key] = int(net.parent[key])
    flags = {k: list(v) for k, v in dec.flags.items()}
    for k, values in net.flags.items():
        flags.setdefault(k, [])
        flags[k].extend(v for v in values if v not in flags[k])
    for row, key in enumerate(ctx.keys):
        key = int(key)
        if key not in node_role:
            node_role[key] = "excluded"
            if ctx.hint[row] != "excluded":
                flags.setdefault(key, []).append("unconnected")
    node_ring = {k: int(v) for k, v in node_ring.items() if v is not None}
    straight_total = sum(line.straight_m for line in lines_out)
    diagnostics = {
        "n_rings": sum(r.kind == "ring" for r in rings_out),
        "n_radials": sum(r.kind == "radial" for r in rings_out),
        "n_dedicated_customers": sum(r.kind == "dedicated" for r in rings_out),
        "n_stub_nodes": sum(role == "stub" for role in node_role.values()),
        "n_joints": len(net.joint_xy),
        "n_excluded": sum(role == "excluded" for role in node_role.values()),
        "street_straight_ratio": (sum(line.length_m for line in lines_out) / straight_total
                                  if straight_total > 0 else None),
        "node_flags": {int(k): v for k, v in flags.items() if v},
        "stub_order": [ctx.key(row) for row, _ in dec.stubs],
    }
    if old is not None:                             # keep what other steps stored (e.g. the repair ladder)
        diagnostics = {**{k: v for k, v in old.diagnostics.items() if k not in diagnostics}, **diagnostics}
    return TopologyPlan(rings=rings_out, lines=lines_out, joints=dict(net.joint_xy), node_role=node_role,
                        node_feeder={int(k): int(v) for k, v in node_feeder.items()}, node_ring=node_ring,
                        node_position=node_position, stub_parent=stub_parent, diagnostics=diagnostics)


def _runs_on(own: set[int], a: int, b: int, old: LinePlan) -> bool:
    """Whether a new line (street pieces ``own``, ends ``a`` and ``b``) runs on the old line: all its street pieces
    lie on it (e.g. a part of it cut off by a new joint), or, without street pieces, it joins the same two nodes."""
    if own:
        return own <= {p for p in old.pieces if p >= 0}
    return a > 0 and b > 0 and {a, b} == {old.from_key, old.to_key}


def _keep_parallels(net: Network, pieces: dict[int, list[int]], old: TopologyPlan) -> None:
    """Keep the parallel cables of the old plan on the new lines of the same role that run on the line that got them
    (:func:`_runs_on`); lines that only share a trench piece or an end with it keep their own."""
    repaired = [line for line in old.lines if line.parallel > 1]
    if not repaired:
        return
    for i, line in enumerate(net.lines):
        own = {p for p in pieces[i] if p >= 0}
        for o in repaired:
            if o.role == line.role and _runs_on(own, line.a, line.b, o):
                line.parallel = max(line.parallel, o.parallel)


def _keep_types(ctx: DistrictContext, net: Network, pieces: dict[int, list[int]], old: TopologyPlan) -> None:
    """Keep the upgraded conductors of the old plan on the new lines of the same role and kind that run on the line
    that got them (:func:`_runs_on`); of several, the largest."""
    upgraded = [line for line in old.lines if line.std_type and line.std_type != ctx.std_type(line.kind)]
    if not upgraded:
        return
    for i, line in enumerate(net.lines):
        own = {p for p in pieces[i] if p >= 0}
        for o in upgraded:
            if o.role == line.role and o.kind == line.kind and _runs_on(own, line.a, line.b, o) and (
                    line.std_type is None or equipment.type_rank(o.std_type) > equipment.type_rank(line.std_type)):
                line.std_type = o.std_type


def _edge_geometries(ctx: DistrictContext, pieces: dict[int, list[int]]) -> dict[int, Any]:
    """Edge geometries of all pieces (fetched once per district through ``DistrictInput.edge_geometry``)."""
    wanted = set()
    for ids in pieces.values():
        for piece in ids:
            if piece >= 0 and ctx.piece_edge[piece] >= 0:
                wanted.add(int(ctx.piece_edge[piece]))
    missing = sorted(wanted - set(ctx.geoms))
    if missing:
        ctx.geoms.update({int(k): v for k, v in dict(ctx.inp.edge_geometry(missing)).items()})
    return ctx.geoms


def _geometry(ctx: DistrictContext, route: Route, pieces: Sequence[int], geoms: dict[int, Any], p0: np.ndarray,
              p1: np.ndarray) -> LineString:
    """The line geometry: run-graph pieces cut from their edges (connectors and bridges straight), or a span."""
    coords: list[tuple[float, float]] = []
    if route.verts is None:
        coords = [tuple(route.p0), tuple(route.p1)]
    else:
        coords = [tuple(ctx.vertex_xy[int(route.verts[0])])]
        for (u, v), piece in zip(zip(route.verts[:-1], route.verts[1:]), pieces):
            a, b = ctx.vertex_xy[int(u)], ctx.vertex_xy[int(v)]
            edge = int(ctx.piece_edge[piece]) if piece >= 0 else -1
            part = [tuple(a), tuple(b)]
            if edge >= 0 and edge in geoms and geoms[edge] is not None:
                sub = substring(geoms[edge], float(ctx.piece_f0[piece]), float(ctx.piece_f1[piece]), normalized=True)
                pts = list(sub.coords)
                if len(pts) >= 2:
                    if np.hypot(*(np.asarray(pts[-1]) - a)) < np.hypot(*(np.asarray(pts[0]) - a)):
                        pts.reverse()
                    part = pts
            for pt in part:
                if pt != coords[-1]:
                    coords.append((float(pt[0]), float(pt[1])))
        # end exactly at the node points (terminals snapped to an edge end or split point lie up to ~1 m off)
        start, end = (float(p0[0]), float(p0[1])), (float(p1[0]), float(p1[1]))
        if np.hypot(coords[0][0] - start[0], coords[0][1] - start[1]) > 1e-6:
            coords.insert(0, start)
        if np.hypot(coords[-1][0] - end[0], coords[-1][1] - end[1]) > 1e-6:
            coords.append(end)
    if len(coords) < 2 or len(set(coords)) < 2:
        start = (float(p0[0]), float(p0[1]))
        end = (float(p1[0]), float(p1[1]))
        if start == end:
            end = (end[0] + 0.01, end[1])
        coords = [start, end]
    return LineString(coords)
