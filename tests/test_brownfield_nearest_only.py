"""The brownfield fill takes a building only to its nearest existing station (``position_brownfield_transformers``, no database)."""

import logging

import numpy as np
import pandas as pd

from pylovo import utils
from pylovo.lv_grid import station_planning
from pylovo.lv_grid.context import GenerationContext
from pylovo.lv_grid.station_planning import StationPlanner


class FakeDb:
    """Station 100 is nearest to consumers 1-6, station 200 to consumer 7; every consumer has 100 kW."""

    def __init__(self):
        self.costs = [(c, 100, 10.0 * c) for c in range(1, 7)] + [(c, 200, 300.0 + c) for c in range(1, 7)]
        self.costs += [(7, 100, 400.0), (7, 200, 50.0)]
        self.clusters, self.deleted = {}, []

    def get_buildings_from_kcid(self, kcid):
        return pd.DataFrame({"vertice_id": range(1, 8)})

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


def test_a_building_whose_nearest_station_is_full_is_left_for_greenfield(monkeypatch):
    monkeypatch.setattr(station_planning, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(utils, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(utils, "CoincidentLoads", FakeLoads)
    generator = StationPlanner(GenerationContext(plz=84000, dbc=FakeDb(),            # FakeLoads ignores the categories
                                                 logger=logging.getLogger("brownfield-nearest-only-test")))
    generator.assign_to_existing_stations(84000, 1, [100, 200])
    # station 100 is full after five consumers (600 kW / 0.8 > 630 kVA); 6 does not join station 200, which still
    # has capacity, as station 100 is its nearest: it is left for greenfield
    assert generator.dbc.clusters == {100: ([1, 2, 3, 4, 5], 630), 200: ([7], 250)}
