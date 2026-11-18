"""Supply areas in one piece (``pylovo.supply_areas``): greenfield areas among the buildings of other grids (no database)."""

import logging

import numpy as np
import pandas as pd
import pytest

from pylovo import grid_generator, supply_areas, utils
from pylovo.database import clustering_mixin
from pylovo.database.clustering_mixin import ClusteringMixin
from pylovo.grid_generator import GridGenerator
from pylovo.supply_areas import AreaContiguity, is_contiguous
from pylovo.version_snapshot import compare_snapshots

LOGGER = logging.getLogger("greenfield-contiguity-test")
COLUMNS = ["point", "bcid", "x", "y"]
CONSUMERS = pd.DataFrame({"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]})


def blob(x, y, size=5.0):
    """Three buildings around (x, y)."""
    return np.array([[x - size, y - size], [x + size, y - size], [x, y + size]])


# a row of buildings from y = -60 to 60 at x = 100: it separates whatever lies left and right of it
UTM = np.array([691_000.0, 5_335_000.0])   # projected coordinates (EPSG:25832) in Munich
WALL = np.array([[100.0 + (5 if i % 2 else -5), y] for i, y in enumerate(range(-60, 61, 15))], dtype=float)


def generator_with(dbc):
    generator = GridGenerator.__new__(GridGenerator)
    generator.plz, generator.dbc, generator.logger = 80803, dbc, LOGGER
    generator.consumer_categories = CONSUMERS
    return generator


def test_a_row_of_other_buildings_between_two_groups_separates_them():
    assert not is_contiguous(np.vstack([blob(0, 0), blob(200, 0)]), WALL)


def test_empty_land_between_two_groups_does_not_separate_them():
    assert is_contiguous(np.vstack([blob(0, 0), blob(200, 0)]), WALL + [0.0, 500.0])


@pytest.mark.parametrize("members, blockers", [
    (blob(0, 0), np.zeros((0, 2))),                                     # no other grid
    (np.array([[0.0, 0.0]]), WALL),                                     # a single building
])
def test_trivial_cases_are_contiguous(members, blockers):
    assert is_contiguous(members, blockers)


@pytest.mark.parametrize("blocker_x, contiguous", [(100.0, False), (300.0, True)])
def test_on_one_line_only_a_blocker_between_the_members_separates_them(blocker_x, contiguous):
    # all points on one line cannot be triangulated
    assert is_contiguous(np.array([[0.0, 0.0], [200.0, 0.0]]) + UTM, np.array([[blocker_x, 0.0]]) + UTM) == contiguous


@pytest.mark.parametrize("gap_m", [0.0, 0.01, 0.03, 0.15, 1.0])
def test_a_second_building_next_to_another_one_keeps_the_area_in_one_piece(gap_m):
    # at projected coordinates Qhull drops a point a few cm from another one; that must not split the area
    rng = np.random.default_rng(7)
    for _ in range(20):
        members, blockers = rng.uniform(0, 100, (15, 2)), rng.uniform(150, 250, (15, 2))
        assert is_contiguous(np.vstack([members, members[0] + [gap_m, 0.0]]) + UTM, blockers + UTM)
    assert not is_contiguous(np.vstack([blob(0, 0), blob(200, 0), blob(0, 0)[:1] + [gap_m, 0.0]]) + UTM, WALL + UTM)


def test_a_member_at_the_position_of_a_blocker_stays_a_member():
    # as members, the wall's buildings bridge the two groups; as blockers they would separate them
    assert is_contiguous(np.vstack([blob(0, 0), blob(200, 0), WALL]), WALL)


def test_area_contiguity_checks_the_buildings_of_the_planning_nodes_among_the_grids():
    rows = [(1, np.nan, *xy) for xy in blob(0, 0)] + [(2, np.nan, *xy) for xy in blob(200, 0)]
    rows += [(3, np.nan, *xy) for xy in blob(0, 20)]
    check = AreaContiguity(pd.DataFrame(rows + [(9, -1.0, *xy) for xy in WALL], columns=COLUMNS))
    assert check([1, 3]) and check([2]) and not check([1, 2])
    assert AreaContiguity(pd.DataFrame(rows, columns=COLUMNS))([1, 2])   # no grid yet: no blockers


class Street(ClusteringMixin):
    """One kcid along the x axis: twelve buildings without a grid (vertex 1-6 at x 0-50 m, 7-12 at
    x 300-350 m, alternating at y -10/10 m) and the area of an existing station (bcid -1) between them."""

    def __init__(self):
        self.x = {v: 10.0 * (v - 1) for v in range(1, 7)} | {v: 300.0 + 10.0 * (v - 7) for v in range(7, 13)}
        self.written = []

    def get_buildings_from_kcid(self, kcid):
        vertices = sorted(self.x)
        return pd.DataFrame({
            "vertice_id": vertices, "connection_point": vertices, "residential_peak_load_in_kw": 1.0,
            "households": 1.0, "nonresidential_peak_load_in_kw": 0.0, "nonresidential_use": None,
            "nonresidential_mv_direct": None})

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_transformer_data(self, settlement_type):
        return np.array([250, 400]), {}

    def get_distance_matrix_from_kcid(self, kcid):
        vertices = sorted(self.x)
        x = np.array([self.x[v] for v in vertices])
        return dict(enumerate(vertices)), np.abs(x[:, None] - x[None, :]), {v: i for i, v in enumerate(vertices)}

    def get_cluster_adjacency_from_street_graph(self, cluster_dict):
        vertices = sorted(self.x)
        edges = [(a, b, self.x[b] - self.x[a]) for a, b in zip(vertices, vertices[1:])]
        return self._cluster_adjacency_from_street_edges(cluster_dict, edges)

    def get_consumer_positions(self, kcid):
        rows = [(v, np.nan, x, 10.0 if v % 2 else -10.0) for v, x in self.x.items()]
        rows += [(100 + i, -1.0, x, y) for i, x in enumerate(range(100, 251, 30)) for y in (-10.0, 10.0)]
        return pd.DataFrame(rows, columns=COLUMNS)

    def clear_grid_result_in_kmean_cluster(self, plz, kcid):
        pass

    def upsert_bcid(self, plz, kcid, bcid, vertices, transformer_rated_power):
        self.written.append(sorted(int(v) for v in vertices))


def test_the_gaps_on_both_sides_of_an_existing_area_get_one_station_each(monkeypatch):
    monkeypatch.setattr(utils, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(clustering_mixin, "MAX_GREENFIELD_TRAFO_DISTANCE_STD", 0)
    monkeypatch.setattr(grid_generator, "MAX_GREENFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(grid_generator, "MERGE_GREENFIELD_CLUSTERS", True)
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA", [250, 400])
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", None)
    street = Street()
    generator_with(street).dimension_bcid_for_kcid(80803, 4)
    # load and distance would allow one area across the existing station's area: one area on each side, also
    # after the merge
    assert street.written == [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12]]


def test_an_area_that_load_and_distance_allow_is_split_when_it_lies_in_pieces(monkeypatch):
    # the cut of the voltage repair for buildings an existing station gave up: one area if it is feasible
    monkeypatch.setattr(utils, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(clustering_mixin, "MAX_GREENFIELD_TRAFO_DISTANCE_STD", 0)
    monkeypatch.setattr(grid_generator, "MAX_GREENFIELD_TRAFO_DISTANCE", 1000)
    street = Street()
    generator_with(street).dimension_bcid_for_kcid(80803, 4, first_bcid=5, merge=False, replace=False, first_cut=1)
    assert street.written == [[1, 2, 3, 4, 5, 6], [7, 8, 9, 10, 11, 12]]


class Stations:
    """Existing stations -1 (vertex 100) and -2 (vertex 200) and a one-building greenfield cluster 1, which
    the streets put nearest to -1; -2's area is a row of buildings between -1's area and the cluster."""

    def __init__(self):
        self.members = {1: [11], -1: [101], -2: [201]}
        self.moves = []

    def get_brownfield_stations(self, plz, kcid):
        return {-1: 100, -2: 200}

    def get_brownfield_transformer_capacity_map(self, vertices):
        return {100: 250, 200: 250}

    def get_transformer_data(self, settlement_type):
        return np.array([250, 400, 630]), {}

    def get_settlement_type_from_plz(self, plz):
        return 1

    def get_greenfield_bcids(self, plz, kcid):
        return [-2, -1, 1]

    def get_buildings_from_bcid(self, plz, kcid, bcid):
        vertices = self.members[bcid]
        return pd.DataFrame({"vertice_id": vertices, "connection_point": vertices}, index=vertices)

    def get_connection_point_to_transformer_costs(self, points, transformers):
        return pd.DataFrame([dict(start_vid=11, end_vid=100, agg_cost=300), dict(start_vid=11, end_vid=200, agg_cost=500)])

    def get_consumer_positions(self, kcid):
        groups = {-1: blob(0, 0), -2: WALL, 1: blob(200, 0)}
        return pd.DataFrame([(0, float(b), *xy) for b, points in groups.items() for xy in points], columns=COLUMNS)

    def move_building_cluster(self, plz, kcid, bcid, target):
        self.moves.append((bcid, target))


def test_a_small_cluster_joins_no_station_across_another_grid(monkeypatch):
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", 1)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(utils, "planning_nodes", lambda buildings: buildings["connection_point"])
    monkeypatch.setattr(utils, "simultaneous_peak_load", lambda buildings, categories, nodes: 100.0 * len(nodes))
    generator = generator_with(Stations())
    assert generator.merge_small_greenfield_into_brownfield(84000, 1) == 1
    # -1 is the nearest station by street, but its area and the cluster would be two pieces with -2's area between
    assert generator.dbc.moves == [(1, -2)]


@pytest.mark.parametrize("existing_stations, repairs, recorded", [
    (False, 0, {"brownfield_nearest_only": False, "greenfield_contiguous_areas": False}),   # the rules change nothing
    (False, 5, {"brownfield_nearest_only": False, "greenfield_contiguous_areas": True}),    # the voltage repair
    (True, 0, {"brownfield_nearest_only": True, "greenfield_contiguous_areas": True}),
])
def test_the_snapshot_records_where_the_rules_act(existing_stations, repairs, recorded):
    assert supply_areas.snapshot(existing_stations, repairs) == recorded


@pytest.mark.parametrize("existing_stations, extended", [(False, True), (True, False)])
def test_only_a_version_with_existing_stations_from_before_the_rules_is_not_extended(existing_stations, extended):
    # stored before the rules: without both entries, or with the former switch off
    for rules_before in ({}, {"brownfield_nearest_only": False}):
        stored = {"transformer_placement": {"max_brownfield_trafo_distance": 1000, **rules_before}}
        expected = {"transformer_placement": {"max_brownfield_trafo_distance": 1000,
                                              **supply_areas.snapshot(existing_stations, 0)}}
        differences, _ = compare_snapshots(stored, expected)
        assert (differences == []) == extended


class Chain(Stations):
    """Station -1 (vertex 100) with clusters 1 and 2 behind each other on its street, station -2 (vertex 200) far
    to the north: cluster 2 lies in one piece with -1's area only once cluster 1 belongs to it."""

    def __init__(self):
        self.members = {1: [11], 2: [21], -1: [101], -2: [201]}
        self.moves = []

    def get_brownfield_transformer_capacity_map(self, vertices):
        return {100: 1000, 200: 1000}

    def get_greenfield_bcids(self, plz, kcid):
        return [-2, -1, 1, 2]

    def get_connection_point_to_transformer_costs(self, points, transformers):
        costs = {11: {100: 30, 200: 600}, 21: {100: 60, 200: 500}}
        return pd.DataFrame([dict(start_vid=p, end_vid=t, agg_cost=costs[p][t]) for p in points for t in transformers])

    def get_consumer_positions(self, kcid):
        groups = {-1: blob(0, 0), 1: blob(30, 0), 2: blob(60, 0), -2: blob(60, 500)}
        return pd.DataFrame([(0, float(b), *xy) for b, points in groups.items() for xy in points], columns=COLUMNS)

    def move_building_cluster(self, plz, kcid, bcid, target):
        self.moves.append((bcid, target))
        self.members[target] += self.members.pop(bcid)


def test_a_cluster_that_joined_a_station_counts_as_its_area_for_the_next_cluster(monkeypatch):
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", 1)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(utils, "planning_nodes", lambda buildings: buildings["connection_point"])
    monkeypatch.setattr(utils, "simultaneous_peak_load", lambda buildings, categories, nodes: 100.0 * len(nodes))
    generator = generator_with(Chain())
    assert generator.merge_small_greenfield_into_brownfield(84000, 1) == 2
    assert generator.dbc.moves == [(1, -1), (2, -1)]
