"""Routes on the run graph of one MV district, for the topology and its network.

:class:`Router` finds the shortest paths of the district's run graph (``RunGraph.csr``, SciPy's Dijkstra) and keeps
the paths per vertex pair and the trees limited to a stub or T-off length per source. A :class:`Route` is the route
of one line: a vertex path along the streets (:func:`street`) or a straight span (:func:`span`).
:func:`terminal_distances` gives the street distances between the terminals of the distance model;
:func:`route_pieces`, :func:`route_overhead_m` and :func:`bridge_m` read the run-graph pieces of a route.

Used by :mod:`~pylovo.mv_grid.topology` and :mod:`~pylovo.mv_grid.network`. Pure module: no database,
configuration or plotting import.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from scipy.sparse.csgraph import dijkstra

from pylovo.mv_grid.model import RunGraph

if TYPE_CHECKING:
    from pylovo.mv_grid.topology import DistrictContext

#: Limited Dijkstra trees kept per district (stub and T-off searches reuse them).
_TREE_CACHE_SIZE: int = 256


def terminal_distances(rg: RunGraph, rows: np.ndarray, chunk: int = 64) -> np.ndarray:
    """Street distances between the terminal rows (``inf`` where unreachable, every row kept)."""
    vertices = np.asarray(rg.terminal_vertex, dtype=np.int64)[rows]
    unique, inverse = np.unique(vertices, return_inverse=True)
    out = np.full((len(unique), len(unique)), np.inf)
    for start in range(0, len(unique), chunk):
        sources = unique[start:start + chunk]
        dist = dijkstra(rg.csr, directed=False, indices=sources)
        out[start:start + len(sources)] = np.atleast_2d(dist)[:, unique]
    return out[np.ix_(inverse, inverse)]


@dataclass
class _Tree:
    """A limited shortest-path tree, stored for the reached vertices only."""

    src: int
    reached: np.ndarray
    dist: np.ndarray
    pred: np.ndarray

    def distance(self, vertices: np.ndarray) -> np.ndarray:
        vertices = np.asarray(vertices, dtype=np.int64)
        if not len(self.reached):
            return np.full(vertices.shape, np.inf)
        i = np.minimum(np.searchsorted(self.reached, vertices), len(self.reached) - 1)
        return np.where(self.reached[i] == vertices, self.dist[i], np.inf)

    def path(self, target: int) -> np.ndarray | None:
        """Vertices from the source to ``target`` (None if it was not reached)."""
        out = [int(target)]
        v = int(target)
        while v != self.src:
            i = int(np.searchsorted(self.reached, v))
            if i >= len(self.reached) or self.reached[i] != v:
                return None
            v = int(self.pred[i])
            if v < 0:
                return None
            out.append(v)
        return np.asarray(out[::-1], dtype=np.int64)


class Router:
    """Shortest paths on the run graph (paths cached per vertex pair, limited trees per source and limit)."""

    def __init__(self, csr: Any) -> None:
        self.csr = csr
        self._paths: dict[tuple[int, int], tuple[np.ndarray | None, np.ndarray | None]] = {}
        self._trees: dict[tuple[int, float], _Tree] = {}

    def tree(self, src: int, limit: float) -> _Tree:
        key = (int(src), float(limit))
        tree = self._trees.get(key)
        if tree is None:
            dist, pred = dijkstra(self.csr, directed=False, indices=int(src), limit=float(limit),
                                  return_predecessors=True)
            reached = np.flatnonzero(np.isfinite(dist))
            tree = _Tree(int(src), reached, dist[reached], pred[reached])
            if len(self._trees) >= _TREE_CACHE_SIZE:
                self._trees.pop(next(iter(self._trees)))
            self._trees[key] = tree
        return tree

    def path(self, a: int, b: int, hint_m: float = math.inf) -> tuple[np.ndarray, np.ndarray] | None:
        """Vertex path from ``a`` to ``b`` and the cost from ``a`` at each vertex; None if unreachable.

        ``hint_m`` (the known distance) limits the search.
        """
        a, b = int(a), int(b)
        if a == b:
            return np.array([a], dtype=np.int64), np.zeros(1)
        key = (a, b) if a < b else (b, a)
        hit = self._paths.get(key)
        if hit is None:
            hit = (None, None)
            limits = [hint_m * (1.0 + 1e-6) + 1.0, math.inf] if math.isfinite(hint_m) else [math.inf]
            for limit in limits:
                dist, pred = dijkstra(self.csr, directed=False, indices=key[0], limit=limit,
                                      return_predecessors=True)
                if math.isfinite(dist[key[1]]):
                    verts = [key[1]]
                    while verts[-1] != key[0]:
                        verts.append(int(pred[verts[-1]]))
                    verts = np.asarray(verts[::-1], dtype=np.int64)
                    hit = (verts, dist[verts].astype(float))
                    break
            self._paths[key] = hit
        verts, cum = hit
        if verts is None:
            return None
        if a == key[0]:
            return verts, cum
        return verts[::-1].copy(), cum[-1] - cum[::-1]


@dataclass
class Route:
    """The route of one line: a run-graph vertex path (street) or a straight span."""

    verts: np.ndarray | None               # street: local vertices from the start to the end
    cum: np.ndarray | None                 # street: cost from the start at each vertex (m)
    p0: np.ndarray
    p1: np.ndarray
    detour: float = 1.0                    # straight span: length = detour x straight

    @property
    def length(self) -> float:
        if self.verts is not None:
            return float(self.cum[-1])
        return self.detour * float(np.hypot(*(self.p1 - self.p0)))


def street(ctx: DistrictContext, verts: np.ndarray, cum: np.ndarray) -> Route:
    """A run-graph route; with routing costs (``osm_line_cost_factor``) ``cum`` is replaced by the true lengths."""
    verts = np.asarray(verts, dtype=np.int64)
    cum = np.asarray(cum, dtype=float)
    if ctx.costed and len(verts) > 1:
        pieces = [ctx.piece(int(a), int(b)) for a, b in zip(verts[:-1], verts[1:])]
        steps = np.array([ctx.piece_length[q] if q >= 0 else c for q, c in zip(pieces, np.diff(cum))])
        cum = np.concatenate([[0.0], np.cumsum(steps)])
    return Route(verts, cum, ctx.vertex_xy[int(verts[0])].copy(), ctx.vertex_xy[int(verts[-1])].copy())


def span(p0: Any, p1: Any, detour: float) -> Route:
    return Route(None, None, np.asarray(p0, dtype=float).copy(), np.asarray(p1, dtype=float).copy(), float(detour))


def route_pieces(ctx: DistrictContext, route: Route) -> list[int]:
    if route.verts is None:
        return []
    return [ctx.piece(int(u), int(v)) for u, v in zip(route.verts[:-1], route.verts[1:])]


def route_overhead_m(ctx: DistrictContext, route: Route) -> float:
    """Length of a run-graph route on OSM MV lines (0 for a straight span)."""
    if route.verts is None or len(route.verts) < 2:
        return 0.0
    steps = np.diff(route.cum)
    flags = np.array([p >= 0 and ctx.piece_overhead[p] for p in route_pieces(ctx, route)], dtype=bool)
    return float(steps[flags].sum())


def bridge_m(ctx: DistrictContext, route: Route) -> float:
    if route.verts is None or len(route.verts) < 2:
        return 0.0
    steps = np.diff(route.cum)
    flags = np.array([p >= 0 and ctx.piece_bridge[p] for p in route_pieces(ctx, route)], dtype=bool)
    return float(steps[flags].sum())
