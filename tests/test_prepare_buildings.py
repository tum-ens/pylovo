"""Order and fallbacks of InputPreparation.prepare_buildings (no database)."""

import logging

import pytest

from pylovo.lv_grid import preparation
from pylovo.lv_grid.context import GenerationContext
from pylovo.lv_grid.preparation import InputPreparation


class Db:
    """Records every database call; ``raises`` names the calls that raise a ValueError."""

    def __init__(self, raises=()):
        self.calls, self.raises = [], set(raises)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            if name in self.raises:
                raise ValueError(f"{name} cannot be calculated.")
            return 1
        return call


def prepared(monkeypatch, raises=()):
    monkeypatch.setattr(preparation, "RESIDENTIAL_ONLY_GENERATION", False)
    db = Db(raises)                                          # the InfDB reads are recorded with the rest
    InputPreparation(GenerationContext(plz=80331, dbc=db, logger=logging.getLogger("prepare-test"),
                                       inf_dbc=db)).prepare_buildings()
    return db.calls


@pytest.mark.parametrize("missing", ["calculate_avg_households_per_building", "calculate_house_distance_metric"])
def test_a_metric_that_cannot_be_calculated_does_not_skip_the_settlement_type(monkeypatch, missing):
    calls = prepared(monkeypatch, raises=[missing])
    assert "set_settlement_type_per_plz" in calls


def test_mv_customers_are_stored_before_buildings_with_a_transformer_inside_leave_the_lv_input(monkeypatch):
    calls = prepared(monkeypatch)
    steps = ["remove_duplicate_buildings", "set_building_peak_load", "update_too_large_consumers_to_zero",
             "set_connection_levels", "save_mv_customers", "remove_non_residential_buildings_overlapping_transformers",
             "calculate_avg_households_per_building", "calculate_house_distance_metric", "set_settlement_type_per_plz"]
    assert [name for name in calls if name in steps] == steps
