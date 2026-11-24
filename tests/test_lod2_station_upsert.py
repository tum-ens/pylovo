"""LoD2 stations become shared transformer candidates only for runs that use them, in a transaction of their own."""

import logging

import pytest

from pylovo.lv_grid import preparation
from pylovo.lv_grid.context import GenerationContext
from pylovo.lv_grid.preparation import InputPreparation

STEPS = ("prepare_data_from_config", "prepare_postcodes", "prepare_buildings", "prepare_transformers", "prepare_ways")
LOGGER = logging.getLogger("lod2-test")


class Recorder:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return False if name == "is_grid_generated" else [("lod2-building",)]
        return call


def prepared_steps(monkeypatch, open_positions, manual_positions):
    """The calls of the input preparation of a postcode, with the steps after the LoD2 stations replaced by their names."""
    monkeypatch.setattr(preparation, "USE_OPEN_TRANSFORMER_POSITIONS", open_positions)
    monkeypatch.setattr(preparation, "USE_MANUAL_TRANSFORMER_POSITIONS", manual_positions)
    calls = []
    recorder = Recorder(calls)
    preparation_step = InputPreparation(GenerationContext(plz=80331, dbc=recorder, logger=LOGGER, inf_dbc=recorder))
    for step in STEPS:
        setattr(preparation_step, step, lambda step=step: calls.append(step))
    preparation_step.run()
    return calls


@pytest.mark.parametrize("open_positions, manual_positions", [(True, False), (False, True)])
def test_the_lod2_stations_are_committed_before_the_plz_work_when_positions_use_them(
        monkeypatch, open_positions, manual_positions):
    calls = prepared_steps(monkeypatch, open_positions, manual_positions)
    assert calls[:4] == ["fetch_transformer_station_buildings_from_infdb", "upsert_lod2_transformer_stations",
                         "commit_changes", "prepare_data_from_config"]


def test_a_run_without_open_or_manual_positions_writes_no_lod2_stations(monkeypatch):
    calls = prepared_steps(monkeypatch, False, False)
    assert calls == [*STEPS]


def test_preparing_the_buildings_no_longer_writes_the_shared_candidates(monkeypatch):
    monkeypatch.setattr(preparation, "RESIDENTIAL_ONLY_GENERATION", False)
    calls = []

    class Db(Recorder):
        def __getattr__(self, name):
            return lambda *args, **kwargs: calls.append(name) or 1

    InputPreparation(GenerationContext(plz=80331, dbc=Db(calls), logger=LOGGER,
                                       inf_dbc=Recorder(calls))).prepare_buildings()
    assert "upsert_lod2_transformer_stations" not in calls
    assert "fetch_transformer_station_buildings_from_infdb" not in calls
