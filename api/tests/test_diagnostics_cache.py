"""The caches of the diagnostics are keyed by a cheap change key, not by an aggregate over all loads and lines (no database)."""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from pylovo_api import diagnostics, diagnostics_data, queries


class Database:
    """Answers the queries of the cache key and records every statement."""

    def __init__(self):
        self.statements: list[str] = []
        self.grids = [(11, 85653), (12, 85653), (13, 85654)]
        self.edits = "e0"
        self.parameters = "p0"
        self.row: dict = {}

    @contextmanager
    def cursor(self, *args, **kwargs):
        yield self

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))
        if "FROM pylovo.grid_result" in sql:
            ids = [g for g, _ in self.grids]
            self.row = {"n": len(ids), "last": max(ids), "total": sum(ids), "parameters": self.parameters}
        elif "to_regclass" in sql:
            self.row = {"ok": True}
        else:
            self.row = {"token": self.edits}

    def fetchone(self):
        return self.row

    def fetch_all(self, sql, params=None, cur=None):
        self.statements.append(" ".join(sql.split()))
        return [{"grid_result_id": g, "plz": p} for g, p in self.grids]


@pytest.fixture()
def database(monkeypatch):
    db = Database()
    monkeypatch.setattr(diagnostics_data.db, "cursor", db.cursor)
    monkeypatch.setattr(diagnostics_data.db, "fetch_all", db.fetch_all)
    monkeypatch.setattr(diagnostics_data.db, "fetch_one", lambda *args, **kwargs: None)
    monkeypatch.setattr(diagnostics_data, "generation_parameters", lambda version_id: {})
    monkeypatch.setattr(queries, "_grid_rows", lambda *args, **kwargs: [])
    for cache in (diagnostics_data._context_cache, diagnostics_data._batch_cache):
        cache.clear()
    return db


@pytest.fixture()
def loads(monkeypatch):
    calls = []

    def load_inputs(ids, light=False):
        calls.append(list(ids))
        return {}

    monkeypatch.setattr(diagnostics_data, "load_inputs", load_inputs)
    monkeypatch.setattr(diagnostics, "version_statistics", lambda inputs, gp: {})
    return calls


def test_the_version_context_is_keyed_without_scanning_loads_and_lines(database, loads):
    diagnostics_data.version_context("1")
    assert loads == [[11, 12, 13]]
    assert not any("pandapower_load" in s or "pandapower_line" in s for s in database.statements)
    database.statements.clear()
    diagnostics_data.version_context("1")  # a hit: only the key is read, no grid list and no statistics
    assert loads == [[11, 12, 13]] and len(database.statements) == 3
    assert not any("ORDER BY plz, kcid, bcid" in s for s in database.statements)


def test_generating_deleting_or_editing_changes_the_key_and_an_undo_restores_it(database, loads):
    diagnostics_data.version_context("1")
    database.edits = "e1"  # a load edit
    diagnostics_data.version_context("1")
    assert len(loads) == 2
    database.edits = "e0"  # its undo: the grids are as before, the first result is current again
    diagnostics_data.version_context("1")
    assert len(loads) == 2
    database.grids.append((14, 85654))  # a PLZ generated
    diagnostics_data.version_context("1")
    database.grids.pop()
    database.parameters = "p1"  # the version replaced after a reset
    diagnostics_data.version_context("1")
    assert len(loads) == 4


def test_the_batch_findings_are_cached_by_the_same_key(database, loads, monkeypatch):
    monkeypatch.setattr(diagnostics_data, "version_context", lambda *args, **kwargs: {})
    first = diagnostics_data.version_diagnostics("1", [85653])
    database.statements.clear()
    assert diagnostics_data.version_diagnostics("1", [85653]) is first
    assert len(database.statements) == 3 and not any("kcid" in s for s in database.statements)  # no grid list on a hit
    assert diagnostics_data.version_diagnostics("1", [85654]) is not first  # another PLZ is another entry
    database.edits = "e1"
    assert diagnostics_data.version_diagnostics("1", [85653]) is not first
