"""A kcid that ends without grid_result rows must not be picked again by position_all_transformers (no database)."""

import logging

import numpy as np
import pandas as pd

from pylovo.grid_generator import GridGenerator


class FakeDb:
    """The kcid bookkeeping of ``buildings_tem`` and ``grid_result``: kcids 1-3, kcid 2 has no loaded building."""

    def __init__(self):
        self.kcids, self.graded = {1, 2, 3}, set()

    def get_kcid_length(self):
        return len(self.kcids)

    def get_next_unfinished_kcid(self, plz):
        unfinished = sorted(self.kcids - self.graded)
        if not unfinished:
            raise TypeError("'NoneType' object is not subscriptable")    # fetchone() is None
        return unfinished[0]

    def get_included_transformers(self, kcid):
        return []

    def get_greenfield_bcids(self, plz, kcid):
        return []

    # what dimension_bcid_for_kcid reads of a kcid without loaded buildings
    def get_buildings_from_kcid(self, kcid):
        return pd.DataFrame({"connection_point": pd.Series(dtype=float)})

    def get_consumer_categories(self):
        return None

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_transformer_data(self, settlement_type):
        return np.array([250, 400, 630]), {}

    def get_distance_matrix_from_kcid(self, kcid):
        return {}, np.zeros((0, 0)), {}

    def delete_isolated_building(self, plz, kcid):          # DELETE ... WHERE kcid = ... AND bcid ISNULL
        self.kcids.discard(kcid)


def test_a_skipped_kcid_does_not_hold_back_the_kcids_after_it(monkeypatch):
    dimensioned = []
    real_dimension = GridGenerator.dimension_bcid_for_kcid

    def dimension(self, plz, kcid):
        dimensioned.append(kcid)
        if kcid == 2:
            return real_dimension(self, plz, kcid)          # the "empty distance vector" branch
        self.dbc.graded.add(kcid)                           # every other kcid gets its grid

    monkeypatch.setattr(GridGenerator, "dimension_bcid_for_kcid", dimension)
    generator = GridGenerator.__new__(GridGenerator)
    generator.dbc, generator.logger, generator.plz = FakeDb(), logging.getLogger("kcid-without-grid-test"), 91301
    generator.position_all_transformers()
    assert dimensioned == [1, 2, 3]
