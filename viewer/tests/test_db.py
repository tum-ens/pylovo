"""Tests of the viewer's database access: every connection and transaction is read-only (no database)."""
from __future__ import annotations

import psycopg2.pool
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
