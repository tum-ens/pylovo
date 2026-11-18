"""Colour slots that keep neighbours apart, and feeder numbers of LV grids.

Colour slots: grids, MV supply areas and rings are coloured from a small palette (8 slots) so that
neighbours never share a colour. Items are taken in key order; each takes the lowest slot not used
by its nearest already-coloured neighbours.

Feeders: a cable of an LV grid belongs to the feeder (outgoing cable towards at least two houses) of the
station that supplies it, as in the structural metrics. The search starts at the station vertex
(``grid_result.ont_vertice_id``) and walks the cables (``from_bus``/``to_bus`` are routing vertices). Feeders are
numbered 1..n by descending cable length; a building takes the feeder of the cable that ends at its vertex.
"""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Hashable, Sequence

import numpy as np
from scipy.spatial import cKDTree

SLOTS = 8
NEIGHBOURS = 6
#: Cables from the station at most this long (km) belong to the station (``pylovo.analysis.comparison.STATION_LINK_KM``).
STATION_LINK_KM = 0.002


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
    """Feeder number per cable id and per vertex of one grid, by the rule of the structural metrics.

    The station is the station vertex with the vertices it reaches over cables of at most
    :data:`STATION_LINK_KM`. A cable end without a further cable is a house. A feeder is a cable that leaves the
    station towards at least two houses; a house on its own cable from the station belongs to no feeder
    (``pylovo.analysis.comparison.feeder_structure``, ``pylovo_api.topology.analyse``).

    Args:
        station: Routing vertex of the station busbar.
        edges: ``(cable id, from vertex, to vertex, length in km)`` of all cables of the grid.

    Returns:
        ``(feeder per cable id, feeder per vertex)``; cables that cannot be reached from the station, the station
        links and the cables of a single house at the station are left out.
    """
    if station is None or not edges:
        return {}, {}
    adjacent: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for cable, a, b, _ in edges:
        adjacent[a].append((cable, b))
        adjacent[b].append((cable, a))
    length = {cable: float(km or 0.0) for cable, _, _, km in edges}
    stations, stack = {station}, [station]
    while stack:
        vertex = stack.pop()
        for cable, other in adjacent[vertex]:
            if other not in stations and length[cable] <= STATION_LINK_KM:
                stations.add(other)
                stack.append(other)
    branch_of_cable: dict[int, int] = {}
    branch_of_vertex: dict[int, int] = {}
    branch_length: dict[int, float] = defaultdict(float)
    houses: dict[int, int] = defaultdict(int)
    branch = 0
    for root in [station, *sorted(stations - {station})]:
        for first, start in adjacent[root]:
            if first in branch_of_cable or start in stations:
                continue
            if start in branch_of_vertex:          # a loop back into a branch: the cable joins that branch
                branch_of_cable[first] = branch_of_vertex[start]
                branch_length[branch_of_vertex[start]] += length[first]
                continue
            branch += 1
            branch_of_cable[first] = branch
            branch_length[branch] += length[first]
            branch_of_vertex[start] = branch
            queue = deque([start])
            while queue:
                vertex = queue.popleft()
                houses[branch] += len(adjacent[vertex]) == 1
                for cable, other in adjacent[vertex]:
                    if cable in branch_of_cable or other in stations:
                        continue
                    branch_of_cable[cable] = branch
                    branch_length[branch] += length[cable]
                    if other not in branch_of_vertex:
                        branch_of_vertex[other] = branch
                        queue.append(other)
    ranked = sorted((b for b in branch_length if houses[b] >= 2), key=lambda b: (-branch_length[b], b))
    number = {branch: i for i, branch in enumerate(ranked, start=1)}
    cables = {cable: number[b] for cable, b in branch_of_cable.items() if b in number}
    vertices = {vertex: number[b] for vertex, b in branch_of_vertex.items() if b in number}
    return cables, vertices
