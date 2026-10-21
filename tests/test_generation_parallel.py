"""The run of several PLZ (``GridGenerator.generate_grid_for_multiple_plz``): which PLZ are generated how, and what the
parent process reports (no database; threads stand in for the worker processes, the bookkeeping is the same)."""
import concurrent.futures
import logging

import pandas as pd
import pytest

from pylovo import grid_generator
from pylovo.grid_generator import GenerationOutcome, GridGenerator

LOGGER = logging.getLogger("generation-parallel-test")


class Database:
    """Every database call is a no-op."""
    logger = LOGGER

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def generator():
    generator = GridGenerator.__new__(GridGenerator)
    generator.dbc, generator.logger = Database(), LOGGER
    return generator


def run(monkeypatch, outcomes, parallel, jobs=2):
    monkeypatch.setattr(grid_generator, "ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor)
    monkeypatch.setattr(grid_generator, "N_JOBS", jobs)
    monkeypatch.setattr(GridGenerator, "_worker", staticmethod(lambda plz, analyze: outcomes[plz]))
    return generator().generate_grid_for_multiple_plz(pd.DataFrame({"plz": list(outcomes)}), parallel=parallel)


def test_the_outcome_of_every_plz_is_returned_in_a_parallel_run(monkeypatch):
    outcomes = {80331: GenerationOutcome.GENERATED, 80333: GenerationOutcome.EMPTY, 80335: GenerationOutcome.EXISTS}
    assert run(monkeypatch, outcomes, parallel=True) == outcomes


def test_a_worker_that_raises_counts_as_a_failed_plz_and_the_others_still_run(monkeypatch, caplog):
    def worker(plz, analyze):
        if plz == 80333:
            raise RuntimeError("worker died")
        return GenerationOutcome.GENERATED

    monkeypatch.setattr(grid_generator, "ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor)
    monkeypatch.setattr(grid_generator, "N_JOBS", 2)
    monkeypatch.setattr(GridGenerator, "_worker", staticmethod(worker))
    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        result = generator().generate_grid_for_multiple_plz(pd.DataFrame({"plz": [80331, 80333]}), parallel=True)

    assert result == {80331: GenerationOutcome.GENERATED, 80333: GenerationOutcome.FAILED}
    assert any("finished with 1 failed PLZ: 80333" in message for message in caplog.messages)


@pytest.mark.parametrize("parallel, jobs, plz", [(False, 4, [80331, 80333]), (True, 1, [80331, 80333]), (True, 4, [80331])])
def test_without_parallelism_the_plz_are_generated_one_after_the_other_and_the_views_refreshed_once(
        monkeypatch, parallel, jobs, plz):
    calls, refreshed = [], []
    gen = generator()
    gen.dbc.refresh_materialized_views = lambda: refreshed.append(True)
    monkeypatch.setattr(grid_generator, "N_JOBS", jobs)
    monkeypatch.setattr(GridGenerator, "generate_grid_for_single_plz",
                        lambda self, plz, analyze_grids=False, refresh_mv=True: calls.append((plz, refresh_mv)) or
                        GenerationOutcome.GENERATED)

    result = gen.generate_grid_for_multiple_plz(pd.DataFrame({"plz": plz}), parallel=parallel)

    assert calls == [(value, False) for value in plz]        # the views are refreshed after all PLZ, not after each
    assert refreshed == [True] and set(result) == set(plz)


def test_a_plz_that_failed_in_a_worker_is_not_reported_as_free_of_failures(monkeypatch, caplog):
    outcomes = {80331: GenerationOutcome.GENERATED, 80333: GenerationOutcome.FAILED}
    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        result = run(monkeypatch, outcomes, parallel=True)

    assert result[80333] == GenerationOutcome.FAILED
    assert "Parallel grid generation finished with no failed PLZ." not in caplog.messages
