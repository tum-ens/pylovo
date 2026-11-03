"""update_transformer_rated_power: the rating a greenfield station keeps or gets (no database)."""
import logging

import numpy as np
import pytest

from pylovo.database.clustering_mixin import ClusteringMixin


class FakeDb:
    logger = logging.getLogger("rating-test")

    def __init__(self, stored, existing=None):
        self.stored, self.existing, self.set_to = stored, existing, None

    def get_existing_transformer_capacity_trafo_ui(self, plz, kcid, bcid):
        return self.existing

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_transformer_data(self, settlement_type):
        return np.array([250, 400, 630, 800]), {}

    def get_transformer_rated_power_from_bcid(self, plz, kcid, bcid):
        return self.stored

    def _set_transformer_rated_power(self, plz, kcid, bcid, rated_power):
        self.set_to = rated_power


@pytest.mark.parametrize("stored, expected", [
    (400, None),          # a standard capacity stays
    (1260, None),         # twice the third standard capacity (two parallel units) stays
    (1600, None),         # twice the fourth
    (700, 1260),          # anything else is rounded up to a multiple of 630 kVA
    (1300, 1890),
])
def test_the_stored_rating_is_kept_or_rounded_up_to_whole_630_kva_units(stored, expected):
    db = FakeDb(stored)
    ClusteringMixin.update_transformer_rated_power(db, 80331, 1, 0)
    assert db.set_to == expected
    # a plain int: psycopg2 cannot bind the NumPy number np.ceil returns (4eecfcba)
    assert expected is None or type(db.set_to) is int


def test_a_rated_transformer_in_the_cluster_wins():
    db = FakeDb(stored=700, existing=np.float64(160))
    ClusteringMixin.update_transformer_rated_power(db, 80331, 1, 0)
    assert db.set_to == 160 and type(db.set_to) is int
