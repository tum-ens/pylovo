"""Explicit outcomes of generate_grid_for_single_plz / generate_grid_for_multiple_plz (no database)."""

import concurrent.futures
import logging
import re

import pandas as pd
import pytest

from pylovo import grid_generator
from pylovo.grid_generator import GenerationOutcome, GridGenerator, ResultExistsError


class FakeDb:
    def __init__(self, grids=((1, 1),)):
        self.grids, self.saved, self.logger = list(grids), False, logging.getLogger("outcome-test")

    def __getattr__(self, name):            # every other database call is a no-op
        return lambda *args, **kwargs: None

    def get_list_from_plz(self, plz):
        return self.grids

    def save_tables(self, plz):
        self.saved = True


def generator(db, generate):
    gg = GridGenerator.__new__(GridGenerator)
    gg.dbc, gg.logger = db, logging.getLogger("outcome-test")
    gg.generate_grid = generate
    return gg


def test_a_generated_plz_is_saved_and_reported():
    db = FakeDb()
    assert generator(db, lambda: None).generate_grid_for_single_plz(80331) == GenerationOutcome.GENERATED
    assert db.saved


def test_failures_empty_and_existing_results_are_reported_instead_of_hidden():
    def fail():
        raise RuntimeError("boom")

    def exists():
        raise ResultExistsError()

    assert generator(FakeDb(), fail).generate_grid_for_single_plz(80331) == GenerationOutcome.FAILED
    assert generator(FakeDb(grids=()), lambda: None).generate_grid_for_single_plz(80331) == GenerationOutcome.EMPTY
    assert generator(FakeDb(), exists).generate_grid_for_single_plz(80331) == GenerationOutcome.EXISTS


class RecordingDb(FakeDb):
    """Records the order of the database calls."""

    def __init__(self, grids=()):
        super().__init__(grids)
        self.calls = []

    def __getattr__(self, name):
        return lambda *args, **kwargs: self.calls.append(name)

    def get_list_from_plz(self, plz):
        self.calls.append("get_list_from_plz")
        return self.grids


def test_an_empty_plz_stores_its_exclusions_and_mv_customers_instead_of_rolling_back():
    db = RecordingDb()
    assert generator(db, lambda: None).generate_grid_for_single_plz(80331) == GenerationOutcome.EMPTY
    after_generation = db.calls[db.calls.index("get_list_from_plz"):]
    assert after_generation[:3] == ["get_list_from_plz", "save_building_exclusions", "commit_changes"]
    assert not db.saved


def test_a_plz_stored_without_grids_stays_empty_on_a_rerun():
    def exists():
        raise ResultExistsError()

    assert generator(FakeDb(grids=()), exists).generate_grid_for_single_plz(80331) == GenerationOutcome.EMPTY


def test_an_interrupt_still_raises():
    def interrupt():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        generator(FakeDb(), interrupt).generate_grid_for_single_plz(80331)


def test_sequential_runs_return_the_outcome_of_every_plz(monkeypatch):
    gg = generator(FakeDb(), lambda: None)
    results = {80331: GenerationOutcome.GENERATED, 80333: GenerationOutcome.FAILED}
    monkeypatch.setattr(gg, "generate_grid_for_single_plz", lambda plz, **kwargs: results[plz])
    assert gg.generate_grid_for_multiple_plz(pd.DataFrame({"plz": [80331, 80333]}), parallel=False) == results


def test_the_worker_returns_the_outcome(monkeypatch, tmp_path):
    class Stub:
        def __init__(self, **kwargs):
            self.dbc, self.logger = FakeDb(), logging.getLogger("outcome-test")

        def generate_grid_for_single_plz(self, plz, **kwargs):
            return GenerationOutcome.EMPTY

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(grid_generator, "GridGenerator", Stub)
    assert GridGenerator._worker(80331, False) == GenerationOutcome.EMPTY


def parallel_generator(monkeypatch, run_worker):
    # Threads instead of processes: the bookkeeping of the parent is the same.
    monkeypatch.setattr(grid_generator, "ProcessPoolExecutor", concurrent.futures.ThreadPoolExecutor)
    monkeypatch.setattr(grid_generator, "N_JOBS", 2)
    monkeypatch.setattr(GridGenerator, "_worker", staticmethod(lambda plz, analyze: run_worker(plz)))
    return generator(FakeDb(), lambda: None)


def test_parallel_run_counts_a_returned_failure_and_names_the_plz(monkeypatch, caplog):
    outcomes = {80331: GenerationOutcome.GENERATED, 80333: GenerationOutcome.FAILED, 80335: GenerationOutcome.EMPTY}
    gg = parallel_generator(monkeypatch, outcomes.__getitem__)
    with caplog.at_level(logging.INFO, logger="outcome-test"):
        result = gg.generate_grid_for_multiple_plz(pd.DataFrame({"plz": list(outcomes)}), parallel=True)

    assert result == outcomes
    messages = [record.getMessage() for record in caplog.records]
    assert "Parallel grid generation finished with 1 failed PLZ: 80333" in messages
    assert "Parallel grid generation finished with no failed PLZ." not in messages
    # the lines pylovo-api parses keep their wording, the outcome is appended
    assert any(re.fullmatch(r"Completed PLZ 80333 \(\d/3\): failed", m) for m in messages)
    assert any(re.fullmatch(r"Completed PLZ 80331 \(\d/3\): generated", m) for m in messages)


def test_parallel_run_counts_a_raising_worker_once(monkeypatch, caplog):
    def run_worker(plz):
        if plz == 80333:
            raise RuntimeError("connection lost")
        return GenerationOutcome.GENERATED

    gg = parallel_generator(monkeypatch, run_worker)
    with caplog.at_level(logging.INFO, logger="outcome-test"):
        result = gg.generate_grid_for_multiple_plz(pd.DataFrame({"plz": [80331, 80333]}), parallel=True)

    assert result[80333] == GenerationOutcome.FAILED
    messages = [record.getMessage() for record in caplog.records]
    assert any(re.fullmatch(r"PLZ 80333 generated an exception \(\d/2\): connection lost", m) for m in messages)
    assert "Parallel grid generation finished with 1 failed PLZ: 80333" in messages


def test_parallel_run_without_failures_says_so(monkeypatch, caplog):
    gg = parallel_generator(monkeypatch, lambda plz: GenerationOutcome.GENERATED)
    with caplog.at_level(logging.INFO, logger="outcome-test"):
        gg.generate_grid_for_multiple_plz(pd.DataFrame({"plz": [80331, 80333]}), parallel=True)
    assert "Parallel grid generation finished with no failed PLZ." in [r.getMessage() for r in caplog.records]


def test_sequential_run_names_failed_plz_too(monkeypatch, caplog):
    gg = generator(FakeDb(), lambda: None)
    results = {80331: GenerationOutcome.GENERATED, 80333: GenerationOutcome.FAILED}
    monkeypatch.setattr(gg, "generate_grid_for_single_plz", lambda plz, **kwargs: results[plz])
    with caplog.at_level(logging.INFO, logger="outcome-test"):
        gg.generate_grid_for_multiple_plz(pd.DataFrame({"plz": list(results)}), parallel=False)
    assert "Grid generation finished with 1 failed PLZ: 80333" in [r.getMessage() for r in caplog.records]


def test_an_interrupt_cancels_the_pending_plz_and_does_not_wait_for_a_grace_period(monkeypatch):
    class FakeFuture:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    class FakeExecutor:
        futures = []

        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, function, *args):
            FakeExecutor.futures.append(FakeFuture())
            return FakeExecutor.futures[-1]

    waits = []

    def interrupted(futures, timeout=None):
        waits.append(timeout)
        raise KeyboardInterrupt

    monkeypatch.setattr(grid_generator, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(grid_generator, "as_completed", interrupted)
    monkeypatch.setattr(grid_generator, "N_JOBS", 2)
    gg = generator(FakeDb(), lambda: None)
    with pytest.raises(KeyboardInterrupt, match="Grid generation interrupted by user"):
        gg.generate_grid_for_multiple_plz(pd.DataFrame({"plz": [80331, 80333]}), parallel=True)
    assert [future.cancelled for future in FakeExecutor.futures] == [True, True]
    assert waits == [None]          # the interrupted loop only; the with block waits for the running workers
