"""Merging of greenfield clusters after the split (``GridGenerator._merge_feasible_greenfield_clusters``)."""

import logging

import numpy as np
import pandas as pd
import pytest

from pylovo import grid_generator
from pylovo.database.clustering_mixin import ClusteringMixin

CONSUMER_CATEGORIES = pd.DataFrame(
    {"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]}
)
# Four clusters along one street, one building per connection point:
# a (10 buildings) - b (2) - c (10) - d (3); b and d are closer to c than to a.
POSITIONS = {
    "a": [10 * i for i in range(10)],
    "b": [200, 210],
    "c": [260 + 10 * i for i in range(10)],
    "d": [400, 410, 420],
}
NEIGHBOURS = [("a", "b"), ("b", "c"), ("c", "d")]


class StreetGraph:
    """The two database lookups of the merge step on a fixed street."""

    cluster_has_feasible_transformer_position = staticmethod(
        ClusteringMixin.cluster_has_feasible_transformer_position
    )
    greenfield_distance_limit = staticmethod(ClusteringMixin.greenfield_distance_limit)

    def __init__(self, cluster_ids):
        self.cluster_ids = cluster_ids

    def get_cluster_adjacency_from_street_graph(self, cluster_dict):
        return {frozenset((self.cluster_ids[left], self.cluster_ids[right])) for left, right in NEIGHBOURS}


def merge(monkeypatch, max_buildings, distance_limit=None):
    monkeypatch.setattr(grid_generator, "MERGE_GREENFIELD_CLUSTERS", True)
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA", [400, 630])
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", max_buildings)
    monkeypatch.setattr(grid_generator, "MAX_GREENFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)

    vertices, offset = {}, 100
    for name, positions in POSITIONS.items():
        vertices[name] = list(range(offset, offset + len(positions)))
        offset += len(positions)
    x = np.array([p for positions in POSITIONS.values() for p in positions], dtype=float)
    dist_mat = np.abs(x[:, None] - x[None, :])
    vid2localid = {vid: i for i, vid in enumerate(v for name in POSITIONS for v in vertices[name])}
    all_vertices = list(vid2localid)
    buildings = pd.DataFrame({
        "vertice_id": all_vertices,
        "connection_point": all_vertices,
        "residential_peak_load_in_kw": 10.0,
        "households": 1.0,
        "nonresidential_peak_load_in_kw": 0.0,
        "nonresidential_use": None,
        "nonresidential_mv_direct": None,
    }).set_index("vertice_id", drop=False)
    cluster_ids = {name: i for i, name in enumerate(POSITIONS)}
    clusters = {cluster_ids[name]: (vertices[name], 400) for name in POSITIONS}

    generator = object.__new__(grid_generator.GridGenerator)
    generator.dbc = StreetGraph(cluster_ids)
    if distance_limit is not None:
        generator.dbc.greenfield_distance_limit = distance_limit
    generator.logger = logging.getLogger(__name__)
    merged = generator._merge_feasible_greenfield_clusters(
        clusters, buildings, CONSUMER_CATEGORIES, np.array([250, 400, 630]), dist_mat, vid2localid, 1, 1
    )
    names = {v: name for name, vids in vertices.items() for v in vids}
    return sorted("".join(sorted({names[v] for v in vids})) for vids, _ in merged.values())


@pytest.mark.parametrize("max_buildings, expected", [
    (None, ["abcd"]),          # any size: everything fits one transformer
    (10, ["abcd"]),            # a (10 buildings) may still join
    (4, ["a", "bcd"]),         # only the fragments b and d join their nearest neighbour c
    (2, ["a", "bc", "d"]),     # d has 3 buildings
    (1, ["a", "b", "c", "d"]),
])
def test_merge_limited_to_pairs_with_a_small_cluster(monkeypatch, max_buildings, expected):
    assert merge(monkeypatch, max_buildings) == expected


def test_a_merged_cluster_must_satisfy_its_own_drawn_distance_limit(monkeypatch):
    # The mean limit (1000 m) allows every union; the union's own draw (here 150 m) does not allow a + bcd,
    # whose best station position is 210 m from the farthest building.
    seen = []

    def drawn_limit(connection_points, mean_limit):
        seen.append((len(connection_points), mean_limit))
        return 150.0

    assert merge(monkeypatch, None, drawn_limit) == ["a", "bcd"]
    assert (25, 1000) in seen          # the union of all four clusters was checked against its draw
