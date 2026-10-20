"""Tests of the version and MV run listing: retired runs (fake database, pylovo's snapshot rules)."""
from __future__ import annotations

import json

import pytest
from pylovo_viewer import regions
from pylovo_viewer.catalog import Catalog

CURRENT = {"generation": {"include_generation": False}, "sources": {"rebalance_max_iter": 0}}


def test_retired_options():
    assert regions.retired_options(CURRENT) == []
    assert regions.retired_options(json.dumps(CURRENT)) == []          # a json column comes as text
    old = {"topology": {"solver": "sweep"}, "sources": {"rebalance_max_iter": 0}}
    assert regions.retired_options(old) == ["generation.include_generation", "topology.solver"]
    assert "generation.include_generation" in regions.retired_options(None)   # no snapshot: the oldest behaviour


def test_retired_options_without_pylovo(monkeypatch):
    regions.version_snapshot.cache_clear()
    monkeypatch.setattr(regions.importlib.util, "find_spec", lambda name: None)
    try:
        assert regions.retired_options(CURRENT) is None
    finally:
        regions.version_snapshot.cache_clear()


@pytest.fixture()
def mv_catalog(fake_db) -> Catalog:
    catalog = Catalog(fake_db)
    catalog.columns["pylovo.mv_grid"] = {"mv_grid_id": "int8"}
    catalog.has_rows["pylovo.mv_variant"] = True
    return catalog


def test_versions_mark_retired_runs(fake_db, mv_catalog):
    fake_db.rows = {
        "FROM pylovo.mv_variant v": [
            {"version_id": "v1", "id": "new", "mv_parameters": CURRENT},
            {"version_id": "v1", "id": "old", "mv_parameters": {"topology": {"stubs": "none"}}}],
        "FROM pylovo.version v": [{"version_id": "v1"}, {"version_id": "v2"}],
    }
    rows = regions.versions(fake_db, mv_catalog)
    runs = {run["id"]: run for run in rows[0]["runs"]}
    assert runs["new"]["retired"] == [] and "topology.stubs" in runs["old"]["retired"]
    assert "mv_parameters" not in runs["new"] and rows[1]["runs"] == []
