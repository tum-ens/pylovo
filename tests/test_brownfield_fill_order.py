"""The brownfield fill visits equal routed distances in a fixed order (``position_brownfield_transformers``, no database)."""

import logging

import numpy as np
import pandas as pd
import pytest

from pylovo import grid_generator, utils
from pylovo.grid_generator import GridGenerator


class FakeDb:
    """Consumers 1 and 2 are 10 m from both stations; a station takes only one of them (2 x 300 kW / 0.8 > 630 kVA)."""

    def __init__(self, costs):
        self.costs, self.clusters = costs, {}

    def get_buildings_from_kcid(self, kcid):
        return pd.DataFrame({"vertice_id": [1, 2]})

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
        pass

    def update_building_cluster(self, transformer_id, consumers, bcid, kcid, plz, rating):
        self.clusters[transformer_id] = sorted(consumers)


class FakeLoads:
    def __init__(self, buildings, categories):
        pass

    def simultaneous_peak_load(self, consumers):
        return 300.0 * len(consumers)


@pytest.mark.parametrize("costs", [
    [(2, 100, 10.0), (1, 100, 10.0), (1, 200, 10.0), (2, 200, 10.0)],
    [(2, 200, 10.0), (1, 200, 10.0), (1, 100, 10.0), (2, 100, 10.0)],
])
def test_equal_distances_are_visited_by_consumer_then_station(monkeypatch, costs):
    monkeypatch.setattr(grid_generator, "BROWNFIELD_NEAREST_ONLY", False)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(utils, "CoincidentLoads", FakeLoads)
    generator = GridGenerator.__new__(GridGenerator)
    generator.dbc, generator.logger = FakeDb(costs), logging.getLogger("brownfield-fill-order-test")
    generator.position_brownfield_transformers(84000, 1, [100, 200])
    # the same answer for every row order of the routing result: consumer 1 takes station 100 first
    assert generator.dbc.clusters == {100: [1], 200: [2]}
