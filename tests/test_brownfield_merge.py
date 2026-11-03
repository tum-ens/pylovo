"""MERGE_GREENFIELD_CLUSTERS with existing stations: small greenfield clusters join the nearest station (no database)."""

import logging

import numpy as np
import pandas as pd

from pylovo import grid_generator, utils
from pylovo.grid_generator import GridGenerator


class FakeDb:
    """Stations -1 (vertex 100, rated 250 kVA) and -2 (vertex 200, unrated); greenfield clusters 1 to 3."""

    def __init__(self):
        self.members = {1: [11], 2: [21, 22], 3: [31, 32, 33], -1: [101], -2: [201]}
        self.costs = {201: {100: 400, 200: 0}, 101: {100: 0, 200: 400}, 11: {100: 300, 200: 500},
                      21: {100: 900, 200: 120}, 22: {100: 950, 200: 140},
                      31: {100: 50, 200: 60}, 32: {100: 55, 200: 65}, 33: {100: 52, 200: 70}}
        self.moves, self.ratings = [], {}

    def get_brownfield_stations(self, plz, kcid):
        return {-1: 100, -2: 200}

    def get_brownfield_transformer_capacity_map(self, vertices):
        return {100: 250}

    def get_transformer_data(self, settlement_type):
        return np.array([250, 400, 630]), {}

    def get_settlement_type_from_plz(self, plz):
        return 1

    def get_consumer_categories(self):
        return None

    def get_greenfield_bcids(self, plz, kcid):
        return [-2, -1, 1, 2, 3]   # like the database: the unfinished brownfield grids are listed too

    def get_buildings_from_bcid(self, plz, kcid, bcid):
        vertices = self.members[bcid]
        return pd.DataFrame({"vertice_id": vertices, "connection_point": vertices}, index=vertices)

    def get_connection_point_to_transformer_costs(self, points, transformers):
        return pd.DataFrame([dict(start_vid=p, end_vid=t, agg_cost=self.costs[p][t]) for p in points for t in transformers])

    def move_building_cluster(self, plz, kcid, bcid, target):
        self.moves.append((bcid, target))
        self.members[target] += self.members.pop(bcid)

    def _set_transformer_rated_power(self, plz, kcid, bcid, rating):
        self.ratings[bcid] = rating


def merged(monkeypatch, limit, distance=1000, node_kw=100.0):
    monkeypatch.setattr(grid_generator, "GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS", limit)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", distance)
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(utils, "planning_nodes", lambda buildings: buildings["connection_point"])
    monkeypatch.setattr(utils, "simultaneous_peak_load", lambda buildings, categories, nodes: node_kw * len(nodes))
    generator = GridGenerator.__new__(GridGenerator)
    generator.dbc, generator.logger = FakeDb(), logging.getLogger("brownfield-merge-test")
    count = generator.merge_small_greenfield_into_brownfield(84000, 1)
    return count, generator.dbc


def test_small_clusters_join_the_nearest_station_and_stations_never_move(monkeypatch):
    count, db = merged(monkeypatch, 2)
    assert count == 2 and db.moves == [(1, -1), (2, -2)]   # cluster 3 has three buildings
    # the rated station keeps 250 kVA; the unrated one is sized for 3 nodes: 300 kW / 0.8 = 375 -> 400
    assert db.ratings == {-2: 400}


def test_the_brownfield_distance_limit_holds(monkeypatch):
    count, db = merged(monkeypatch, 3, distance=100, node_kw=50.0)
    assert count == 1 and db.moves == [(3, -1)]


def test_a_station_takes_no_cluster_that_would_overload_it(monkeypatch):
    # cluster 3 (3 nodes) is nearest to -1, rated 250 kVA: with its own node 400 kW, so it joins -2 (400 kW <= 630)
    count, db = merged(monkeypatch, 3, distance=100)
    assert count == 1 and db.moves == [(3, -2)]
    assert db.ratings == {-2: 630}   # 400 kW / 0.8 = 500 -> 630


def test_unrated_stations_are_sized_again_up_to_the_largest_rating(monkeypatch):
    monkeypatch.setattr(FakeDb, "get_brownfield_transformer_capacity_map", lambda self, vertices: {})
    count, db = merged(monkeypatch, 3, node_kw=150.0)
    # -1: 2 nodes, 300 kW / 0.8 = 375 -> 400; -2: 3 nodes, 450 kW / 0.8 = 562.5 -> 630. Cluster 3 would bring -1
    # to 750 kW and -2 to 900 kW, both above the largest rating of 630 kVA: it stays a greenfield cluster.
    assert count == 2 and db.moves == [(1, -1), (2, -2)]
    assert db.ratings == {-1: 400, -2: 630}


def test_a_load_between_planning_and_full_rating_may_join(monkeypatch):
    # the planning reserve may be used: -1 (unrated) with cluster 1 reaches 2 x 300 = 600 kW <= 630 kVA,
    # above 630 x 0.8 = 504; the rating stays the largest one
    monkeypatch.setattr(FakeDb, "get_brownfield_transformer_capacity_map", lambda self, vertices: {})
    count, db = merged(monkeypatch, 1, node_kw=300.0)
    assert count == 1 and db.moves == [(1, -1)]
    assert db.ratings == {-1: 630}
