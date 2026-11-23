"""GridGenerator.generate_grid runs the stages of pylovo.lv_grid in order, on the context of the postcode (no database)."""

import logging

import pytest

from pylovo.grid_generator import GridGenerator, ResultExistsError
from pylovo.lv_grid.network_builder import NetworkBuilder
from pylovo.lv_grid.preparation import InputPreparation
from pylovo.lv_grid.station_planning import StationPlanner
from pylovo.lv_grid.street_clusters import StreetClustering

STAGES = (InputPreparation, StreetClustering, StationPlanner, NetworkBuilder)
LOGGER = logging.getLogger("generation-stages-test")


class Db:
    def __init__(self, calls, generated):
        self.calls, self.generated = calls, generated

    def is_grid_generated(self, plz):
        self.calls.append(("is_grid_generated", plz))
        return self.generated


def generator(monkeypatch, generated):
    calls = []
    for stage in STAGES:
        monkeypatch.setattr(stage, "run", lambda self, name=stage.__name__: calls.append((name, self.context)))
    gg = GridGenerator.__new__(GridGenerator)
    gg.plz, gg.logger, gg.consumer_categories = 80331, LOGGER, "categories"
    gg.dbc, gg.inf_dbc = Db(calls, generated), "infdb"
    return gg, calls


def test_the_stages_run_in_order_on_one_context_after_the_check_for_existing_grids(monkeypatch):
    gg, calls = generator(monkeypatch, generated=False)
    gg.generate_grid()
    assert [name for name, _ in calls] == ["is_grid_generated", *(stage.__name__ for stage in STAGES)]
    contexts = [context for _, context in calls[1:]]
    assert all(context is contexts[0] for context in contexts)
    context = contexts[0]
    assert (context.plz, context.dbc, context.logger, context.consumer_categories, context.inf_dbc) == (
        80331, gg.dbc, LOGGER, "categories", "infdb")


def test_a_postcode_with_grids_of_the_version_stops_before_the_first_stage(monkeypatch):
    gg, calls = generator(monkeypatch, generated=True)
    with pytest.raises(ResultExistsError):
        gg.generate_grid()
    assert calls == [("is_grid_generated", 80331)]


def test_a_stage_has_the_objects_of_its_context():
    from pylovo.lv_grid.context import GenerationContext

    context = GenerationContext(plz=80331, dbc="db", logger=LOGGER, consumer_categories="categories", inf_dbc="infdb")
    stage = StationPlanner(context)
    assert (stage.plz, stage.dbc, stage.logger, stage.consumer_categories, stage.inf_dbc) == (
        80331, "db", LOGGER, "categories", "infdb")
    assert stage.areas.context is context          # the greenfield areas of the planner work on the same context
    builder = NetworkBuilder(context)
    assert builder.repair.context is context and builder.storage.context is context
    assert builder.repair.areas is builder.repair.stations.areas
