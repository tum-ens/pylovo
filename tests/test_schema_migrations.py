"""DB-free checks of DatabaseConstructor.migrate_schema with a recording connection."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pylovo.database.database_constructor import DatabaseConstructor


class Cursor:
    """Records every statement; answers the server version query, finds no applied migration."""

    def __init__(self, conn):
        self.conn, self.last = conn, ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.last = " ".join(str(query).split())
        self.conn.log.append(self.last)

    def fetchone(self):
        return self.conn.server if "server_version_num" in self.last else None


class Connection:
    def __init__(self, server=(170006, "17.6")):
        self.server, self.log = server, []

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        self.log.append("ROLLBACK")


def test_setup_refuses_postgresql_before_15_before_any_change():
    """ON DELETE SET NULL (column) is PostgreSQL 15 syntax; older servers fail in the middle of the DDL."""
    conn = Connection(server=(140013, "14.13 (Ubuntu 14.13-0ubuntu0.22.04.1)"))
    with pytest.raises(RuntimeError, match=r"PostgreSQL 15 or newer, the server runs PostgreSQL 14\.13 "):
        DatabaseConstructor(SimpleNamespace(conn=conn)).migrate_schema()
    assert len(conn.log) == 1 and "server_version_num" in conn.log[0]
    DatabaseConstructor(SimpleNamespace(conn=Connection(server=(150000, "15.0")))).check_server_version()
