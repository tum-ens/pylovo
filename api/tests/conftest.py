"""Fixtures for the pylovo-api tests.

Tests that touch the database only run when you opt in explicitly by naming the database the
project's ``.env`` points to::

    PYLOVO_API_TEST_DATABASE=<sandbox db> uv run --extra api pytest api/tests

Use a throwaway sandbox database: the transformer test inserts and deletes a manual
transformer. Nothing else is written; no job that changes the database is started.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from pylovo_api.settings import find_project_root

ROOT = find_project_root()
os.chdir(ROOT)  # pylovo.config_loader resolves config/ from the working directory


def _db_opt_in() -> str | None:
    wanted = os.getenv("PYLOVO_API_TEST_DATABASE")
    if not wanted:
        return "set PYLOVO_API_TEST_DATABASE=<sandbox db name> to run database tests"
    from pylovo import config_loader

    if config_loader.DBNAME != wanted:
        return f"PYLOVO_API_TEST_DATABASE={wanted} does not match the configured database {config_loader.DBNAME}"
    return None


requires_db = pytest.mark.skipif(bool(_db_opt_in()), reason=_db_opt_in() or "")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    """A private project root with a copy of config/ (config writes never touch the checkout)."""
    shutil.copytree(ROOT / "config", tmp_path / "config")
    return tmp_path


@pytest.fixture()
def client(project: Path):
    from fastapi.testclient import TestClient
    from pylovo_api.app import create_app

    app = create_app(project)
    with TestClient(app, headers={"X-Pylovo-UI": "1"}) as c:
        yield c
    os.chdir(ROOT)
