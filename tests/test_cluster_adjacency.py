"""The street-graph neighbourhood of greenfield clusters and the load and distance rules of the hierarchical split
(``ClusteringMixin._cluster_adjacency_from_street_edges``, ``load_constrained_hierarchical_clustering``), no database."""
import numpy as np
import pandas as pd
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform

from pylovo.database import clustering_mixin
from pylovo.database.clustering_mixin import ClusteringMixin

adjacency = ClusteringMixin._cluster_adjacency_from_street_edges


def street(*vertices):
    """Edges of a street through ``vertices`` in this order, 100 m each."""
    return [(a, b, 100.0) for a, b in zip(vertices, vertices[1:])]


def test_clusters_are_neighbours_when_an_edge_joins_their_territories():
    clusters = {"a": ([1, 2], 250), "b": ([3, 4], 250), "c": ([6], 250)}
    # vertex 5 is as close to b as to c: the cluster listed first (b) takes it, so b and c touch at the edge 5 - 6
    assert adjacency(clusters, street(1, 2, 3, 4, 5, 6)) == {frozenset("ab"), frozenset("bc")}


def test_clusters_that_are_far_apart_on_the_street_are_not_neighbours():
    clusters = {"a": ([1], 250), "b": ([3], 250), "c": ([5], 250)}
    assert adjacency(clusters, street(1, 2, 3, 4, 5)) == {frozenset("ab"), frozenset("bc")}    # never a and c


def test_a_street_component_without_a_cluster_and_unconnected_clusters_add_no_pair():
    clusters = {"a": ([1, 2], 250), "b": ([10, 11], 250)}
    assert adjacency(clusters, street(1, 2) + street(10, 11) + street(20, 21)) == set()


# --- the split rules -------------------------------------------------------------------------------------------------------
class Split(ClusteringMixin):
    pass


def split(monkeypatch, kw_by_vertex, x_by_vertex, capacities=(250, 400), limit=1000, cut=2):
    monkeypatch.setattr(clustering_mixin, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(clustering_mixin, "MAX_GREENFIELD_TRAFO_DISTANCE_STD", 0)
    vertices = sorted(x_by_vertex)
    x = np.array([x_by_vertex[v] for v in vertices], dtype=float)
    dist = np.abs(x[:, None] - x[None, :])
    localid2vid = dict(enumerate(vertices))
    buildings = pd.DataFrame({
        "vertice_id": vertices, "connection_point": vertices, "residential_peak_load_in_kw": [kw_by_vertex[v] for v in vertices],
        "households": 1.0, "nonresidential_peak_load_in_kw": 0.0, "nonresidential_use": None, "nonresidential_mv_direct": None})
    consumers = pd.DataFrame({"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]})
    invalid, valid, count = object.__new__(Split).load_constrained_hierarchical_clustering(
        linkage(squareform(dist), method="average"), cut, localid2vid, buildings, consumers, np.array(capacities),
        dist_mat=dist, vid2localid={v: i for i, v in localid2vid.items()}, max_transformer_distance=limit)
    return {k: sorted(v) for k, v in invalid.items()}, {k: (sorted(v), int(r)) for k, (v, r) in valid.items()}, count


def test_a_cluster_gets_the_smallest_rating_above_its_planned_load(monkeypatch):
    # two groups of three households of 100 kW 1 km apart: 3 x 100 kW coincide to 143 kW, 179 kVA planned
    positions = {1: 0.0, 2: 10.0, 3: 20.0, 11: 1000.0, 12: 1010.0, 13: 1020.0}
    invalid, valid, count = split(monkeypatch, {v: 100.0 for v in positions}, positions, limit=5000)

    assert invalid == {} and count == 2
    assert sorted(valid.values()) == [([1, 2, 3], 250), ([11, 12, 13], 250)]


def test_a_cluster_above_the_largest_rating_with_five_buildings_or_more_has_to_be_split_again(monkeypatch):
    positions = {v: 10.0 * v for v in range(1, 31)}          # 30 households of 100 kW: 534 kVA planned in one piece
    invalid, valid, _ = split(monkeypatch, {v: 100.0 for v in positions}, positions, cut=1)
    assert valid == {} and invalid == {0: sorted(positions)}


def test_a_small_cluster_above_the_catalogue_keeps_its_load_as_the_rating(monkeypatch):
    positions = {1: 0.0, 2: 10.0}                            # two 500 kW customers: more than 400 kVA but fewer than 5 buildings
    invalid, valid, _ = split(monkeypatch, {1: 500.0, 2: 500.0}, positions, cut=1)
    assert invalid == {} and valid[0][0] == [1, 2] and valid[0][1] > 400


def test_a_cluster_without_a_station_position_within_the_distance_limit_is_invalid(monkeypatch):
    positions = {1: 0.0, 2: 1500.0, 3: 3000.0}               # no point within 1000 m of all others
    invalid, valid, _ = split(monkeypatch, {v: 1.0 for v in positions}, positions, cut=1, limit=1000)
    assert valid == {} and invalid == {0: [1, 2, 3]}
    invalid, valid, _ = split(monkeypatch, {v: 1.0 for v in positions}, positions, cut=1, limit=1500)
    assert invalid == {} and valid[0][0] == [1, 2, 3]
