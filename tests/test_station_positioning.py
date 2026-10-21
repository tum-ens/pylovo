"""Greenfield station dimensioning and positioning (``GridGenerator.dimension_bcid_for_kcid``,
``position_greenfield_transformers``, ``position_all_transformers``) without a database.

The street is a line along the x axis; the database lookups are replaced by fixed data, the clustering rules
(hierarchical split, load and distance limits, merge) are the real ones.
"""

import logging
import math

import numpy as np
import pandas as pd
import pytest

from pylovo import grid_generator, utils
from pylovo.database import clustering_mixin
from pylovo.database.clustering_mixin import ClusteringMixin
from pylovo.grid_generator import GridGenerator

CONSUMERS = pd.DataFrame({"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]})
LOGGER = logging.getLogger("station-positioning-test")
UTILIZATION = 0.8


@pytest.fixture(autouse=True)
def pinned_configuration(monkeypatch):
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", UTILIZATION)
    monkeypatch.setattr(clustering_mixin, "TRANSFORMER_PLANNING_UTILIZATION", UTILIZATION)
    monkeypatch.setattr(clustering_mixin, "MAX_GREENFIELD_TRAFO_DISTANCE_STD", 0)
    monkeypatch.setattr(grid_generator, "MAX_GREENFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(grid_generator, "GREENFIELD_TRAFO_POSITION_TOLERANCE", 0)
    monkeypatch.setattr(grid_generator, "MERGE_GREENFIELD_CLUSTERS", True)
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA", [250, 400, 630])
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", None)


def generator_with(dbc):
    generator = GridGenerator.__new__(GridGenerator)
    generator.plz, generator.dbc, generator.logger = 80803, dbc, LOGGER
    return generator


def buildings_at(vertices, kw):
    """One household of ``kw`` kW per connection point (the vertex is its own connection point)."""
    return pd.DataFrame({
        "vertice_id": vertices, "connection_point": vertices, "residential_peak_load_in_kw": float(kw),
        "households": 1.0, "nonresidential_peak_load_in_kw": 0.0, "nonresidential_use": None,
        "nonresidential_mv_direct": None,
    })


def planned_kva(buildings, vertices):
    return utils.simultaneous_peak_load(buildings, CONSUMERS, vertices) / UTILIZATION


class Street(ClusteringMixin):
    """The lookups of one kcid on a street along the x axis (``positions``: vertex -> x in m)."""

    def __init__(self, positions, buildings, capacities=(250, 400)):
        self.positions, self.buildings, self.capacities, self.written = dict(positions), buildings, capacities, []

    def get_buildings_from_kcid(self, kcid):
        return self.buildings

    def get_consumer_categories(self):
        return CONSUMERS

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_transformer_data(self, settlement_type):
        return np.array(self.capacities), {}

    def get_distance_matrix_from_kcid(self, kcid):
        vertices = sorted(self.positions)
        x = np.array([self.positions[vertex] for vertex in vertices], dtype=float)
        localid2vid = dict(enumerate(vertices))
        return localid2vid, np.abs(x[:, None] - x[None, :]), {vertex: i for i, vertex in localid2vid.items()}

    def get_cluster_adjacency_from_street_graph(self, cluster_dict):
        vertices = sorted(self.positions)
        edges = [(a, b, abs(self.positions[b] - self.positions[a])) for a, b in zip(vertices, vertices[1:])]
        return self._cluster_adjacency_from_street_edges(cluster_dict, edges)

    def clear_grid_result_in_kmean_cluster(self, plz, kcid):
        self.written.append(("clear", plz, kcid))

    def upsert_bcid(self, plz, kcid, bcid, vertices, transformer_rated_power):
        self.written.append(("bcid", plz, kcid, bcid, sorted(int(vertex) for vertex in vertices),
                             int(transformer_rated_power)))


def dimension(positions, kw):
    street = Street(positions, buildings_at(sorted(positions), kw))
    generator_with(street).dimension_bcid_for_kcid(80803, 4)
    return street


def stations(street):
    """``[(bcid, vertices, rating)]`` written for the kcid."""
    assert street.written[0] == ("clear", 80803, 4)    # the old grid_result rows of the kcid go first
    return [(bcid, vertices, rating) for _, _, _, bcid, vertices, rating in street.written[1:]]


def test_buildings_that_fit_one_station_get_one_bcid_with_the_smallest_sufficient_rating():
    positions = {vertex: 10.0 * vertex for vertex in range(1, 7)}
    street = dimension(positions, 100.0)

    planned = planned_kva(street.buildings, list(positions))
    assert 200 < planned < 250
    assert stations(street) == [(1, [1, 2, 3, 4, 5, 6], 250)]


def test_an_overloaded_area_is_split_until_every_station_fits_the_largest_rating():
    positions = {vertex: 10.0 * vertex for vertex in range(1, 31)}
    street = dimension(positions, 100.0)
    result = stations(street)

    assert planned_kva(street.buildings, list(positions)) > 400
    assert [bcid for bcid, _, _ in result] == list(range(1, len(result) + 1)) and len(result) >= 2
    assert sorted(vertex for _, vertices, _ in result for vertex in vertices) == sorted(positions)   # a partition
    assert [min(vertices) for _, vertices, _ in result] == sorted(min(vertices) for _, vertices, _ in result)
    for _, vertices, rating in result:
        planned = planned_kva(street.buildings, vertices)
        assert planned < 400
        assert rating == min(capacity for capacity in (250, 400) if capacity > planned)


@pytest.mark.parametrize("merge", [False, True])
def test_every_station_has_a_position_within_the_greenfield_distance_limit(monkeypatch, merge):
    monkeypatch.setattr(grid_generator, "MERGE_GREENFIELD_CLUSTERS", merge)
    positions = {vertex: 500.0 * (vertex - 1) for vertex in range(1, 13)}   # 5.5 km, a load that fits one station
    result = stations(dimension(positions, 1.0))

    assert len(result) >= 3
    assert sorted(vertex for _, vertices, _ in result for vertex in vertices) == sorted(positions)
    for _, vertices, _ in result:
        reach = min(max(abs(positions[a] - positions[b]) for b in vertices) for a in vertices)
        assert reach <= 1000


def test_a_single_connection_point_becomes_one_bcid_without_clustering():
    street = dimension({7: 0.0}, 50.0)
    assert stations(street) == [(1, [7], 250)]

    # beyond the catalogue the planned load itself is the rating (create_transformer builds 630 kVA units from it)
    street = dimension({7: 0.0}, 1000.0)
    planned = planned_kva(street.buildings, [7])
    assert planned > 400
    assert stations(street) == [(1, [7], math.ceil(planned))]


def test_connection_points_without_a_routed_distance_are_skipped_with_a_warning(caplog):
    street = Street({7: 0.0}, buildings_at([7, 8], 50.0))    # two loaded points, the matrix knows one
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        generator_with(street).dimension_bcid_for_kcid(80803, 4)

    assert street.written == []
    assert any("Skipped BCID dimensioning" in message for message in caplog.messages)


# --- the station position ------------------------------------------------------------------------------------------
class Cluster:
    """One building cluster: connection points on the x axis (``x``: vertex -> m) with a load each."""

    greenfield_distance_limit = staticmethod(ClusteringMixin.greenfield_distance_limit)

    def __init__(self, x, loads=None, missing=()):
        self.x, self.loads, self.missing, self.selected = dict(x), loads or {}, list(missing), []

    def get_building_connection_points_from_bc(self, kcid, bcid):
        return sorted(self.x)

    def get_distance_matrix_from_bcid(self, kcid, bcid):
        vertices = sorted(self.x)
        positions = np.array([self.x[vertex] for vertex in vertices], dtype=float)
        localid2vid = dict(enumerate(vertices))
        return localid2vid, np.abs(positions[:, None] - positions[None, :]), {v: i for i, v in localid2vid.items()}

    def generate_load_vector_for_connection_points(self, kcid, bcid, points):
        return np.array([self.loads.get(point, 1.0) for point in points], dtype=float), self.missing

    def upsert_transformer_selection(self, plz, kcid, bcid, vertex):
        self.selected.append((plz, kcid, bcid, vertex))


def position(cluster, bcid=3):
    generator_with(cluster).position_greenfield_transformers(80803, 4, bcid)
    return cluster.selected


def test_a_cluster_of_one_connection_point_gets_its_station_there():
    cluster = Cluster({5: 0.0})
    cluster.get_distance_matrix_from_bcid = None   # no routing needed
    assert position(cluster) == [(80803, 4, 3, 5)]


def test_the_station_sits_at_the_connection_point_with_the_smallest_distance_times_load():
    # costs: vertex 10: 0 + 100 + 3000, vertex 20: 100 + 0 + 2000, vertex 30: 300 + 200 + 0
    cluster = Cluster({10: 0.0, 20: 100.0, 30: 300.0}, loads={10: 1.0, 20: 1.0, 30: 10.0})
    assert position(cluster) == [(80803, 4, 3, 30)]


def test_candidates_beyond_the_distance_limit_are_ignored_while_one_is_in_reach():
    # the heavy load at 1900 m makes vertex 3 the cheapest, but it is 1900 m from vertex 1
    cluster = Cluster({1: 0.0, 2: 900.0, 3: 1900.0}, loads={1: 1.0, 2: 1.0, 3: 100.0})
    assert position(cluster) == [(80803, 4, 3, 2)]


def test_without_any_candidate_in_reach_the_weighted_minimum_is_taken_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(grid_generator, "MAX_GREENFIELD_TRAFO_DISTANCE", 500)
    cluster = Cluster({1: 0.0, 2: 900.0, 3: 1900.0}, loads={1: 1.0, 2: 1.0, 3: 100.0})
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        assert position(cluster) == [(80803, 4, 3, 3)]
    assert any("falling back to weighted minimum" in message for message in caplog.messages)


def test_the_tolerance_draws_a_seeded_station_among_the_nearly_optimal_candidates(monkeypatch):
    monkeypatch.setattr(grid_generator, "GREENFIELD_TRAFO_POSITION_TOLERANCE", 0.3)
    x = {vertex: 100.0 * (vertex - 1) for vertex in range(1, 6)}   # costs 1000, 700, 600, 700, 1000
    first = position(Cluster(x))
    assert first == position(Cluster(x))                           # a rerun places the same station
    rng = np.random.default_rng([grid_generator.K_MEANS_SEED, 80803, 4, 3])
    assert first == [(80803, 4, 3, [2, 3, 4][int(rng.integers(3))])]   # vertices within 1.3 x the optimum (600)


def test_unroutable_load_points_are_reported_and_the_grid_goes_on(caplog):
    cluster = Cluster({10: 0.0, 20: 100.0, 30: 300.0}, missing=[99])
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        assert len(position(cluster)) == 1
    assert any("ignored 1 unroutable load connection point(s): 99" in message for message in caplog.messages)


def test_a_cluster_without_connection_points_is_an_error():
    cluster = Cluster({})
    with pytest.raises(ValueError, match="no active connection points"):
        position(cluster)


def test_a_cluster_without_a_distance_matrix_is_an_error():
    cluster = Cluster({1: 0.0, 2: 100.0})
    cluster.get_distance_matrix_from_bcid = lambda kcid, bcid: ({}, np.zeros((0, 0)), {})
    with pytest.raises(ValueError, match="no route distance matrix"):
        position(cluster)


def test_a_load_vector_that_does_not_match_the_matrix_is_an_error():
    cluster = Cluster({1: 0.0, 2: 100.0})
    cluster.generate_load_vector_for_connection_points = lambda kcid, bcid, points: (np.ones(1), [])
    with pytest.raises(ValueError, match="aligned load vector length 1"):
        position(cluster)


# --- the order of the steps per kcid -------------------------------------------------------------------------------
class Kcids:
    def __init__(self, transformers, greenfield_bcids, remaining, events):
        self.transformers, self.bcids, self.remaining, self.events = transformers, greenfield_bcids, remaining, events
        self.queue = iter(sorted(transformers))

    def get_kcid_length(self):
        return len(self.transformers)

    def get_next_unfinished_kcid(self, plz):
        return next(self.queue)

    def get_included_transformers(self, kcid):
        return self.transformers[kcid]

    def count_kmean_cluster_consumers(self, kcid):
        return self.remaining[kcid]

    def delete_isolated_building(self, plz, kcid):
        self.events.append(("delete isolated", kcid))

    def get_greenfield_bcids(self, plz, kcid):
        return self.bcids[kcid]

    def update_transformer_rated_power(self, plz, kcid, bcid, note):
        self.events.append(("rated", kcid, bcid, note))


def run_all(monkeypatch, remaining, merge=True):
    monkeypatch.setattr(grid_generator, "MERGE_GREENFIELD_CLUSTERS", merge)
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", 4)
    events = []
    kcids = Kcids({1: [], 2: [7, 8]}, {1: [1, 2], 2: [-1, -2, 1]}, {2: remaining}, events)
    generator = generator_with(kcids)
    generator.dimension_bcid_for_kcid = lambda plz, kcid: events.append(("dimension", kcid))
    generator.position_brownfield_transformers = lambda plz, kcid, transformers: events.append(
        ("brownfield", kcid, transformers))
    generator.merge_small_greenfield_into_brownfield = lambda plz, kcid: events.append(("merge", kcid))
    generator.position_greenfield_transformers = lambda plz, kcid, bcid: events.append(("position", kcid, bcid))
    generator.position_all_transformers()
    return events


def test_a_kcid_without_stations_is_dimensioned_and_each_greenfield_bcid_is_positioned_and_rated(monkeypatch):
    events = run_all(monkeypatch, remaining=3)

    assert events[:5] == [("dimension", 1), ("position", 1, 1), ("rated", 1, 1, 1), ("position", 1, 2),
                          ("rated", 1, 2, 1)]


def test_existing_stations_fill_first_then_the_rest_gets_greenfield_stations(monkeypatch):
    events = run_all(monkeypatch, remaining=3)

    # brownfield bcids (negative) are not positioned again; small greenfield clusters may join a station
    assert events[5:] == [("brownfield", 2, [7, 8]), ("dimension", 2), ("merge", 2), ("position", 2, 1),
                          ("rated", 2, 1, 1)]


def test_without_remaining_buildings_the_isolated_one_is_deleted_and_nothing_is_dimensioned(monkeypatch):
    events = run_all(monkeypatch, remaining=0)

    assert events[5:] == [("brownfield", 2, [7, 8]), ("delete isolated", 2), ("position", 2, 1), ("rated", 2, 1, 1)]


def test_the_merge_into_existing_stations_needs_the_merge_switch(monkeypatch):
    events = run_all(monkeypatch, remaining=3, merge=False)

    assert ("merge", 2) not in events and ("dimension", 2) in events


# --- LVGEN-03 ------------------------------------------------------------------------------------------------------
class BrownfieldDb:
    """Two consumers of 252 kW at one station: together 504 / 0.8 = 630 kVA, exactly the largest rating."""

    def __init__(self):
        self.clusters = []

    def get_buildings_from_kcid(self, kcid):
        return pd.DataFrame({"vertice_id": [1, 2]})

    def get_consumer_categories(self):
        return None

    def get_consumer_to_transformer_df(self, kcid, transformers):
        return pd.DataFrame([(1, 100, 10.0), (2, 100, 20.0)], columns=["start_vid", "end_vid", "agg_cost"])

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_transformer_data(self, settlement_type):
        return np.array([400, 630]), {}

    def get_brownfield_transformer_capacity_map(self, transformers):
        return {}

    def delete_transformers_from_buildings_tem(self, transformers):
        pass

    def update_building_cluster(self, transformer_id, consumers, bcid, kcid, plz, rating):
        self.clusters.append((sorted(consumers), rating))


class FixedLoads:
    def __init__(self, buildings, categories):
        pass

    def simultaneous_peak_load(self, consumers):
        return 252.0 * len(consumers)


@pytest.mark.xfail(strict=True, raises=IndexError,
                   reason="LVGEN-03: a station loaded exactly to the largest rating raises IndexError in the brownfield fill")
def test_a_station_loaded_exactly_to_its_largest_rating_does_not_fail_the_plz(monkeypatch):
    monkeypatch.setattr(grid_generator, "BROWNFIELD_NEAREST_ONLY", False)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(utils, "CoincidentLoads", FixedLoads)
    database = BrownfieldDb()
    generator_with(database).position_brownfield_transformers(84000, 1, [100])

    (consumers, rating), = database.clusters
    assert rating * UTILIZATION >= 252.0 * len(consumers)    # whichever consumers the station took, its rating covers them
