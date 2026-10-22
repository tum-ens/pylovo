"""Tests of the viewer's database access: read-only connections and query errors (no database)."""
from __future__ import annotations

from contextlib import contextmanager

import psycopg2.errors
import psycopg2.pool
import pytest
from pylovo_viewer import db as viewer_db
from pylovo_viewer.settings import Settings


def test_connections_start_read_only(monkeypatch):
    seen = {}

    class Pool:
        def __init__(self, minconn, maxconn, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", Pool)
    viewer_db.Database(Settings(dbname="db", user="u", host="localhost", port=5432, password="p"))
    assert seen["options"] == "-c default_transaction_read_only=on" and seen["dbname"] == "db"
    assert seen["application_name"] == "pylovo-viewer"


def test_missing_table_is_a_schema_error(monkeypatch):
    class Cursor:
        def execute(self, sql, params):
            raise psycopg2.errors.UndefinedTable('relation "pylovo.mv_line" does not exist\nLINE 1: ...')

    @contextmanager
    def cursor(self):
        yield Cursor()

    monkeypatch.setattr(psycopg2.pool, "ThreadedConnectionPool", lambda *a, **k: None)
    monkeypatch.setattr(viewer_db.Database, "cursor", cursor)
    db = viewer_db.Database(Settings(dbname="db", user="u", host="localhost", port=5432, password="p"))
    with pytest.raises(viewer_db.SchemaError, match=r'^relation "pylovo.mv_line" does not exist$'):
        db.query("SELECT * FROM pylovo.mv_line")
