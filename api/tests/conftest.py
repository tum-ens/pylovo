"""Fixtures for the pylovo-api tests.

Tests that touch the database (``requires_db``) only run when you opt in explicitly by naming the database
pylovo's configuration (``.env`` or environment) points to::

    PYLOVO_API_TEST_DATABASE=<test db> uv run --extra api pytest api/tests

Use the toy-town test database built by ``tests/db/build_test_database.py`` (section "Tests" of the
development guide). The tests derive their expectations from that database (any database with generated
grids of versions 1 to 3, version 2 with existing transformer positions and version 3 residential-only,
works), but they WRITE: the transformer test inserts and deletes a manual transformer, the load-edit tests
apply and undo load edits and leave their audit rows. Never point them at a database you want to keep.
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
        return "set PYLOVO_API_TEST_DATABASE=<test db name> to run database tests"
    from pylovo import config_loader

    if config_loader.DBNAME != wanted:
        return f"PYLOVO_API_TEST_DATABASE={wanted} does not match the configured database {config_loader.DBNAME}"
    return None


requires_db = pytest.mark.skipif(bool(_db_opt_in()), reason=_db_opt_in() or "")


@pytest.fixture(scope="module")
def conn():
    """Autocommit connection to the test database (search path ``pylovo, public``, exact float digits)."""
    import psycopg2

    from pylovo import config_loader as cl

    c = psycopg2.connect(dbname=cl.DBNAME, user=cl.DBUSER, password=cl.PASSWORD, host=cl.HOST, port=cl.PORT,
                         options="-c search_path=pylovo,public -c extra_float_digits=3")
    c.autocommit = True
    yield c
    c.close()


def q(conn, query, params=None) -> list[tuple] | None:
    """Rows of ``query`` (None for a statement without result)."""
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def region(conn) -> dict:
    """The postcodes of the database tests, found by query.

    ``plz``: the first postcode with grids of version 1 (99001 in the toy town); ``no_geometry``: a registered
    postcode with the same first four digits but without a polygon (99003), which the region gate blocks.
    """
    from psycopg2 import sql

    from pylovo import config_loader as cl

    plz = q(conn, "SELECT min(plz) FROM pylovo.grid_result WHERE version_id = '1'")[0][0]
    if plz is None:
        pytest.skip("the test database has no grids of version 1")
    no_polygon = sql.SQL("""
        SELECT mr.plz FROM pylovo.municipal_register mr
        WHERE left(mr.plz::text, 4) = left(%(plz)s::text, 4)
          AND NOT EXISTS (SELECT 1 FROM pylovo.postcode p WHERE p.plz = mr.plz)
          AND NOT EXISTS (SELECT 1 FROM {postcodes} o WHERE o.plz = mr.plz)
        ORDER BY mr.plz LIMIT 1""").format(postcodes=sql.Identifier(cl.INFDB_OPENDATA_SCHEMA, "postcodes_germany"))
    rows = q(conn, no_polygon, {"plz": plz})
    return {"plz": plz, "no_geometry": rows[0][0] if rows else None}


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
