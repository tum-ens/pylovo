"""Tests of the version and MV run listing: runs of an older MV stage version (fake database, pylovo's options)."""
from __future__ import annotations

import json

import pytest
from pylovo_viewer import regions
from pylovo_viewer.catalog import Catalog

CURRENT = regions.mv_options().MV_STAGE_VERSION


def test_stage_version():
    assert regions.stage_version({"mv_stage_version": CURRENT}) == CURRENT
    assert regions.stage_version(json.dumps({"mv_stage_version": 9})) == 9     # a json column comes as text
    assert regions.stage_version({"topology": {}}) is None and regions.stage_version(None) is None


def test_mv_options_without_pylovo(monkeypatch):
    regions.mv_options.cache_clear()
    monkeypatch.setattr(regions.importlib.util, "find_spec", lambda name: None)
    try:
        assert regions.mv_options() is None
    finally:
        regions.mv_options.cache_clear()


@pytest.fixture()
def mv_catalog(fake_db) -> Catalog:
    catalog = Catalog(fake_db)
    catalog.columns["pylovo.mv_grid"] = {"mv_grid_id": "int8"}
    catalog.has_rows["pylovo.mv_variant"] = True
    return catalog


def test_versions_mark_runs_of_an_older_stage_version(fake_db, mv_catalog):
    fake_db.rows = {
        "FROM pylovo.mv_variant v": [
            {"version_id": "v1", "id": "new", "mv_parameters": {"mv_stage_version": CURRENT}},
            {"version_id": "v1", "id": "old", "mv_parameters": json.dumps({"mv_stage_version": CURRENT - 1})},
            {"version_id": "v1", "id": "unrecorded", "mv_parameters": None}],
        "FROM pylovo.version v": [{"version_id": "v1"}, {"version_id": "v2"}],
    }
    rows = regions.versions(fake_db, mv_catalog)
    runs = {run["id"]: run for run in rows[0]["runs"]}
    assert runs["new"]["older"] is False and runs["new"]["stage_version"] == CURRENT
    assert runs["old"]["older"] is True and runs["old"]["stage_version"] == CURRENT - 1
    assert runs["unrecorded"]["older"] is True and runs["unrecorded"]["stage_version"] is None
    assert "mv_parameters" not in runs["new"] and rows[1]["runs"] == []


def test_versions_mark_nothing_without_pylovo(fake_db, mv_catalog, monkeypatch):
    monkeypatch.setattr(regions, "mv_options", lambda: None)
    fake_db.rows = {"FROM pylovo.mv_variant v": [{"version_id": "v1", "id": "old", "mv_parameters": {}}],
                    "FROM pylovo.version v": [{"version_id": "v1"}]}
    assert regions.versions(fake_db, mv_catalog)[0]["runs"][0]["older"] is False
