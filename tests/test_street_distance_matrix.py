"""ClusteringMixin.street_distance_matrix follows the pgr_dijkstraCostMatrix conventions of the kcid matrix."""

import heapq
import logging

import numpy as np

from pylovo.database.clustering_mixin import ClusteringMixin

# (source, target, cost, reverse_cost): parallel edges 1-2, an edge usable in one direction only (3-4),
# an unusable edge (4-5), a zero-length edge (5-6), a self loop (7-7) and a second component (8-9-10).
EDGES = [
    (1, 2, 10.7, 10.7), (1, 2, 3.3, 3.3), (2, 3, 4.45, 4.45), (3, 4, 2.2, -1.0), (4, 5, -1.0, -1.0),
    (3, 5, 7.9, 7.9), (5, 6, 0.0, 0.0), (7, 7, 1.0, 1.0), (6, 7, 1.9999, 1.9999), (8, 9, 5.5, 5.5), (9, 10, 0.75, 0.75),
    (11, 12, 3.0, 3.0),
]


class _Cursor:
    def execute(self, query, params=None):
        self.rows = EDGES

    def fetchall(self):
        return self.rows


def _reference(points):
    """pgRouting semantics: undirected, an edge costs min of its non-negative costs; whole-metre int costs."""
    graph = {}
    for s, t, cost, reverse_cost in EDGES:
        usable = [c for c in (cost, reverse_cost) if c >= 0]
        if usable and s != t:
            graph.setdefault(s, []).append((t, min(usable)))
            graph.setdefault(t, []).append((s, min(usable)))

    def distances(start):
        dist, queue = {start: 0.0}, [(0.0, start)]
        while queue:
            d, node = heapq.heappop(queue)
            if d > dist[node]:
                continue
            for neighbour, weight in graph.get(node, []):
                if d + weight < dist.get(neighbour, np.inf):
                    dist[neighbour] = d + weight
                    heapq.heappush(queue, (d + weight, neighbour))
        return dist

    points = sorted(p for p in set(points) if p in graph)
    rows = {s: {t: d for t, d in distances(s).items() if t in points and t != s} for s in points}
    kept = [s for s in points if rows[s]]
    matrix = np.zeros((len(kept), len(kept)))
    for i, s in enumerate(kept):
        for j, t in enumerate(kept):
            if t in rows[s]:
                matrix[i, j] = int(rows[s][t])
    return kept, matrix


def test_street_distance_matrix_matches_the_pgrouting_conventions():
    client = object.__new__(ClusteringMixin)
    client.cur = _Cursor()
    client.logger = logging.getLogger("street-distance-test")
    for points in ([1, 2, 3, 4, 5, 6, 7], [7, 5, 1, 99], [8, 10, 1, 6], [11, 1], [99], []):
        localid2vid, matrix, vid2localid = client.street_distance_matrix(points, chunk_size=2)
        expected_vertices, expected_matrix = _reference(points)
        assert list(localid2vid.values()) == expected_vertices
        assert all(isinstance(v, np.int32) for v in localid2vid.values())
        assert vid2localid == {v: i for i, v in localid2vid.items()}
        np.testing.assert_array_equal(matrix, expected_matrix)
        assert matrix.dtype == np.float64
