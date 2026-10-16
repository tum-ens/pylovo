"""Colour slots that keep neighbours apart, and feeder numbers of LV grids.

Colour slots: grids, MV supply areas and rings are coloured from a small palette (8 slots) so that
neighbours never share a colour. Items are taken in key order; each takes the lowest slot not used
by its nearest already-coloured neighbours (the same rule as the GridPlanner map).

Feeders: every cable of an LV grid belongs to the feeder (outgoing cable) of the station that
supplies it. The search starts at the station vertex (``grid_result.ont_vertice_id``) and walks
the cables (``from_bus``/``to_bus`` are routing vertices). Feeders are numbered 1..n by descending
cable length; a building takes the feeder of the cable that ends at its vertex.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable, Sequence

import numpy as np
from scipy.spatial import cKDTree

SLOTS = 8
NEIGHBOURS = 6


def colour_slots(keys: Sequence[Hashable], xy: np.ndarray, slots: int = SLOTS,
                 neighbours: int = NEIGHBOURS) -> dict[Hashable, int]:
    """Slot ``0..slots-1`` per key; neighbouring points (``xy`` in metres) get different slots."""
    n = len(keys)
    if n == 0:
        return {}
    order = sorted(range(n), key=lambda i: keys[i])
    k = min(n, 4 * neighbours + 1)
    _, near = cKDTree(np.asarray(xy, dtype=float)).query(xy, k=k)
    near = np.asarray(near).reshape(n, -1)
    slot = np.full(n, -1)
    for i in order:
        used: set[int] = set()
        for j in near[i]:
            if j != i and j < n and slot[j] >= 0:
                used.add(int(slot[j]))
                if len(used) >= neighbours:
                    break
        slot[i] = next((c for c in range(slots) if c not in used), i % slots)
    return {keys[i]: int(slot[i]) for i in range(n)}


def feeders(station: int | None, edges: Sequence[tuple[int, int, int, float]]) -> tuple[dict[int, int], dict[int, int]]:
    """Feeder number per cable id and per vertex of one grid.

    Args:
        station: Routing vertex of the station busbar.
        edges: ``(cable id, from vertex, to vertex, length)`` of all cables of the grid.

    Returns:
        ``(feeder per cable id, feeder per vertex)``; cables that cannot be reached from the station
        are left out.
    """
    if station is None or not edges:
        return {}, {}
    adjacent: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for cable, a, b, _ in edges:
        adjacent[a].append((cable, b))
        adjacent[b].append((cable, a))
    length = {cable: float(km or 0.0) for cable, _, _, km in edges}
    branch_of_cable: dict[int, int] = {}
    branch_of_vertex: dict[int, int] = {station: 0}
    branch_length: dict[int, float] = defaultdict(float)
    for branch, (first, start) in enumerate(adjacent.get(station, []), start=1):
        if first in branch_of_cable:
            continue
        branch_of_cable[first] = branch
        branch_length[branch] += length[first]
        if start in branch_of_vertex:
            continue
        branch_of_vertex[start] = branch
        queue = deque([start])
        while queue:
            vertex = queue.popleft()
            for cable, other in adjacent[vertex]:
                if cable in branch_of_cable:
                    continue
                branch_of_cable[cable] = branch
                branch_length[branch] += length[cable]
                if other not in branch_of_vertex:
                    branch_of_vertex[other] = branch
                    queue.append(other)
    ranked = sorted(branch_length, key=lambda b: (-branch_length[b], b))
    number = {branch: i for i, branch in enumerate(ranked, start=1)}
    cables = {cable: number[b] for cable, b in branch_of_cable.items()}
    vertices = {vertex: number[b] for vertex, b in branch_of_vertex.items() if b in number}
    return cables, vertices
