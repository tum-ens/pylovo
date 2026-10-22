"""BROWNFIELD_NEAREST_ONLY in the brownfield fill (``GridGenerator.position_brownfield_transformers``, no database)."""

import logging

import numpy as np
import pandas as pd
import pytest

from pylovo import grid_generator, utils
from pylovo.grid_generator import GridGenerator


class FakeDb:
    """Station 100 is nearest to consumers 1-6, station 200 to consumer 7; every consumer has 100 kW."""

    def __init__(self):
        self.costs = [(c, 100, 10.0 * c) for c in range(1, 7)] + [(c, 200, 300.0 + c) for c in range(1, 7)]
        self.costs += [(7, 100, 400.0), (7, 200, 50.0)]
        self.clusters, self.deleted = {}, []

    def get_buildings_from_kcid(self, kcid):
        return pd.DataFrame({"vertice_id": range(1, 8)})

    def get_consumer_categories(self):
        return None

    def get_consumer_to_transformer_df(self, kcid, transformers):
        return pd.DataFrame(self.costs, columns=["start_vid", "end_vid", "agg_cost"])

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_transformer_data(self, settlement_type):
        return np.array([250, 400, 630]), {}

    def get_brownfield_transformer_capacity_map(self, transformers):
        return {}

    def delete_transformers_from_buildings_tem(self, transformers):
        self.deleted += list(transformers)

    def update_building_cluster(self, transformer_id, consumers, bcid, kcid, plz, rating):
        self.clusters[transformer_id] = (sorted(consumers), rating)


class FakeLoads:
    def __init__(self, buildings, categories):
        pass

    def simultaneous_peak_load(self, consumers):
        return 100.0 * len(consumers)


@pytest.mark.parametrize("nearest_only, station_200", [
    (False, [6, 7]),     # station 100 is full after five consumers (600 kW / 0.8 > 630 kVA): 6 joins station 200
    (True, [7]),         # 6 may only join its nearest station, which is full: it is left for greenfield
])
def test_overflow_joins_a_farther_station_only_without_nearest_only(monkeypatch, nearest_only, station_200):
    monkeypatch.setattr(grid_generator, "BROWNFIELD_NEAREST_ONLY", nearest_only)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(utils, "CoincidentLoads", FakeLoads)
    generator = GridGenerator.__new__(GridGenerator)
    generator.dbc, generator.logger = FakeDb(), logging.getLogger("brownfield-nearest-only-test")
    generator.position_brownfield_transformers(84000, 1, [100, 200])
    assert generator.dbc.clusters[100][0] == [1, 2, 3, 4, 5]
    assert generator.dbc.clusters[200][0] == station_200
