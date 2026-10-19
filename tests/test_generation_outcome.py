"""Explicit outcomes of generate_grid_for_single_plz / generate_grid_for_multiple_plz (no database)."""

import logging

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
