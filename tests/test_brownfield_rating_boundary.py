"""A brownfield station loaded exactly to the largest catalogue rating (``position_brownfield_transformers``, no database)."""

import logging

import numpy as np
import pandas as pd
import pytest

from pylovo import grid_generator, utils
from pylovo.grid_generator import GridGenerator


class FakeDb:
    """Station 100 is the nearest station of consumers 1 and 2; the ratings are 400 and 630 kVA."""

    def __init__(self, known=None):
        self.known, self.clusters = known or {}, {}

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
        return self.known

    def delete_transformers_from_buildings_tem(self, transformers):
        pass

    def update_building_cluster(self, transformer_id, consumers, bcid, kcid, plz, rating):
        self.clusters[transformer_id] = (sorted(consumers), rating)


class FakeLoads:
    """Every consumer has the same coincident load ``kw`` (no diversity)."""

    kw = 252.0

    def __init__(self, buildings, categories):
        pass

    def simultaneous_peak_load(self, consumers):
        return self.kw * len(consumers)


def fill(monkeypatch, kw, known=None):
    monkeypatch.setattr(grid_generator, "BROWNFIELD_NEAREST_ONLY", False)
    monkeypatch.setattr(grid_generator, "MAX_BROWNFIELD_TRAFO_DISTANCE", 1000)
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(FakeLoads, "kw", kw)
    monkeypatch.setattr(utils, "CoincidentLoads", FakeLoads)
    generator = GridGenerator.__new__(GridGenerator)
    generator.dbc, generator.logger = FakeDb(known), logging.getLogger("brownfield-rating-boundary-test")
    generator.position_brownfield_transformers(84000, 1, [100])
    return generator.dbc.clusters


def test_a_load_exactly_at_the_largest_rating_fills_the_station(monkeypatch):
    # 2 x 252 kW / 0.8 = 630 kVA: no catalogue rating lies strictly above it, so consumer 2 is left for
    # greenfield (it used to be accepted and then crashed the rating pick with an IndexError)
    assert fill(monkeypatch, 252.0) == {100: ([1], 400)}


def test_a_load_just_below_the_largest_rating_still_joins(monkeypatch):
    assert fill(monkeypatch, 250.0) == {100: ([1, 2], 630)}


def test_an_imported_rating_is_still_filled_up_to_the_rating(monkeypatch):
    # 2 x 252 kW = 504 kW on an imported 504 kVA station: equality is allowed there
    assert fill(monkeypatch, 252.0, known={100: 504}) == {100: ([1, 2], 504)}
    assert fill(monkeypatch, 252.0, known={100: 503}) == {100: ([1], 503)}
