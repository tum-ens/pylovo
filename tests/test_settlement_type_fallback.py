"""Settlement type of a PLZ whose metrics cannot both be calculated (no database)."""

import pytest

from pylovo.database.preprocessing_mixin import PreprocessingMixin

THRESHOLDS = {"rural_max_households": 1.0, "urban_min_households": 3.0,
              "rural_min_distance": 50.0, "urban_max_distance": 20.0}


class Cursor:
    def __init__(self, row):
        self.row, self.stored = row, None

    def execute(self, query, params=None):
        if query.strip().startswith("UPDATE"):
            self.stored = params["stype"]

    def fetchone(self):
        return self.row


def classify(row):
    client = PreprocessingMixin.__new__(PreprocessingMixin)
    client.cur = Cursor(row)
    settlement_type = client.set_settlement_type_per_plz(80331, THRESHOLDS)
    assert client.cur.stored == settlement_type
    return settlement_type


@pytest.mark.parametrize("metrics, expected", [
    ((None, None), 1),        # a single commercial building: rural
    ((None, 10.0), 2),        # commercial buildings only, dense: the distance alone, at most semi-urban
    ((None, 60.0), 1),        # commercial buildings only, sparse
    ((5.0, None), 2),         # a single building with many households
    ((1.0, None), 1),         # a single house
    ((3.0, 10.0), 3),         # both metrics: unchanged
    ((1.0, 60.0), 1),
])
def test_a_missing_metric_counts_as_rural(metrics, expected):
    assert classify(metrics) == expected


def test_a_plz_without_postcode_result_row_is_an_error():
    with pytest.raises(ValueError, match="no postcode_result row"):
        classify(None)
