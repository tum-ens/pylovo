"""GridGenerator.generate_grid runs the steps of pylovo.lv_grid in order, on the context of the postcode (no database)."""

import logging
import re
from pathlib import Path

import pytest

import pylovo
from pylovo.lv_grid.generator import GridGenerator, ResultExistsError
from pylovo.lv_grid.network_builder import NetworkBuilder
from pylovo.lv_grid.preparation import InputPreparation
from pylovo.lv_grid.station_planning import StationPlanner
from pylovo.lv_grid.street_clusters import StreetClustering

STEPS = (InputPreparation, StreetClustering, StationPlanner, NetworkBuilder)
LOGGER = logging.getLogger("generation-steps-test")


class Db:
    def __init__(self, calls, generated):
        self.calls, self.generated = calls, generated

    def is_grid_generated(self, plz):
        self.calls.append(("is_grid_generated", plz))
        return self.generated


def generator(monkeypatch, generated):
    calls = []
    for step in STEPS:
        monkeypatch.setattr(step, "run", lambda self, name=step.__name__: calls.append((name, self.context)))
    gg = GridGenerator.__new__(GridGenerator)
    gg.plz, gg.logger, gg.consumer_categories = 80331, LOGGER, "categories"
    gg.dbc, gg.inf_dbc = Db(calls, generated), "infdb"
    return gg, calls


def test_the_stages_run_in_order_on_one_context_after_the_check_for_existing_grids(monkeypatch):
    gg, calls = generator(monkeypatch, generated=False)
    gg.generate_grid()
    assert [name for name, _ in calls] == ["is_grid_generated", *(step.__name__ for step in STEPS)]
    contexts = [context for _, context in calls[1:]]
    assert all(context is contexts[0] for context in contexts)
    context = contexts[0]
    assert (context.plz, context.dbc, context.logger, context.consumer_categories, context.inf_dbc) == (
        80331, gg.dbc, LOGGER, "categories", "infdb")


def test_a_postcode_with_grids_of_the_version_stops_before_the_first_step(monkeypatch):
    gg, calls = generator(monkeypatch, generated=True)
    with pytest.raises(ResultExistsError):
        gg.generate_grid()
    assert calls == [("is_grid_generated", 80331)]


def test_a_stage_has_the_objects_of_its_context():
    from pylovo.lv_grid.context import GenerationContext

    context = GenerationContext(plz=80331, dbc="db", logger=LOGGER, consumer_categories="categories", inf_dbc="infdb")
    step = StationPlanner(context)
    assert (step.plz, step.dbc, step.logger, step.consumer_categories, step.inf_dbc) == (
        80331, "db", LOGGER, "categories", "infdb")
    assert step.areas.context is context          # the greenfield areas of the planner work on the same context
    builder = NetworkBuilder(context)
    assert builder.repair.context is context and builder.storage.context is context
    assert builder.repair.areas is builder.repair.stations.areas


@pytest.mark.parametrize("package, other", [("lv_grid", "mv_grid"), ("mv_grid", "lv_grid")])
def test_the_lv_and_the_mv_package_do_not_import_each_other(package, other):
    """MV works on the stored LV results; what both levels need lies outside the two packages."""
    imports = re.compile(rf"^\s*(?:from\s+pylovo\.{other}\b|import\s+pylovo\.{other}\b"
                         rf"|from\s+pylovo\s+import\s[^\n]*\b{other}\b)", re.M)
    files = sorted((Path(pylovo.__file__).parent / package).rglob("*.py"))
    assert files
    assert [path.name for path in files if imports.search(path.read_text(encoding="utf-8"))] == []
