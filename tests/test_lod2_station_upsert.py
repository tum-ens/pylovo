"""LoD2 stations become shared transformer candidates only for runs that use them, in a transaction of their own."""

import logging

import pytest

from pylovo import grid_generator
from pylovo.grid_generator import GridGenerator

STEPS = ("prepare_data_from_config", "prepare_postcodes", "prepare_buildings", "prepare_transformers",
         "prepare_ways", "apply_kmeans_clustering", "position_all_transformers", "install_cables")


class Recorder:
    def __init__(self, calls):
        self.calls = calls

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            return False if name == "is_grid_generated" else [("lod2-building",)]
        return call


def generated_steps(monkeypatch, open_positions, manual_positions):
    monkeypatch.setattr(grid_generator, "USE_INFDB", True)
    monkeypatch.setattr(grid_generator, "USE_OPEN_TRANSFORMER_POSITIONS", open_positions)
    monkeypatch.setattr(grid_generator, "USE_MANUAL_TRANSFORMER_POSITIONS", manual_positions)
    calls = []
    gg = GridGenerator.__new__(GridGenerator)
    gg.plz, gg.logger = 80331, logging.getLogger("lod2-test")
    gg.dbc = gg.inf_dbc = Recorder(calls)
    for step in STEPS:
        setattr(gg, step, lambda step=step: calls.append(step))
    gg.generate_grid()
    return list(calls)          # before the destructor of gg records close()


@pytest.mark.parametrize("open_positions, manual_positions", [(True, False), (False, True)])
def test_the_lod2_stations_are_committed_before_the_plz_work_when_positions_use_them(
        monkeypatch, open_positions, manual_positions):
    calls = generated_steps(monkeypatch, open_positions, manual_positions)
    assert calls[:5] == ["is_grid_generated", "fetch_transformer_station_buildings_from_infdb",
                         "upsert_lod2_transformer_stations", "commit_changes", "prepare_data_from_config"]


def test_a_run_without_open_or_manual_positions_writes_no_lod2_stations(monkeypatch):
    calls = generated_steps(monkeypatch, False, False)
    assert calls == ["is_grid_generated", *STEPS]


def test_preparing_the_buildings_no_longer_writes_the_shared_candidates(monkeypatch):
    monkeypatch.setattr(grid_generator, "USE_INFDB", True)
    monkeypatch.setattr(grid_generator, "RESIDENTIAL_ONLY_GENERATION", False)
    calls = []
    gg = GridGenerator.__new__(GridGenerator)
    gg.plz, gg.logger = 80331, logging.getLogger("lod2-test")
    gg.inf_dbc = Recorder(calls)

    class Db(Recorder):
        def __getattr__(self, name):
            return lambda *args, **kwargs: calls.append(name) or 1

    gg.dbc = Db(calls)
    gg.prepare_buildings()
    assert "upsert_lod2_transformer_stations" not in calls
    assert "fetch_transformer_station_buildings_from_infdb" not in calls
