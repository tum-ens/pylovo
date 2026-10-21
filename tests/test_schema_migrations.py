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


def test_migration_on_violating_rows_stops_with_the_setup_message(monkeypatch):
    """A VALIDATE step that meets bad rows ends in "Migration X stopped" (setup prints it), not a traceback."""
    import psycopg2.errors

    from pylovo.database import database_constructor

    class Violation(psycopg2.errors.ForeignKeyViolation):
        diag = SimpleNamespace(
            message_primary='insert or update on table "pandapower_line" violates foreign key constraint '
                            '"fk_pp_line_from_bus"',
            message_detail='Key (grid_result_id, from_bus)=(5, 3) is not present in table "pandapower_bus".')

    def bad_rows(cur):
        raise Violation()

    monkeypatch.setattr(database_constructor, "PRE_SCHEMA_MIGRATIONS", (("0003_integrity", bad_rows),))
    conn = Connection()
    with pytest.raises(RuntimeError) as stopped:
        DatabaseConstructor(SimpleNamespace(conn=conn)).migrate_schema()
    assert str(stopped.value) == (
        'Migration 0003_integrity stopped, nothing was changed: insert or update on table "pandapower_line" '
        'violates foreign key constraint "fk_pp_line_from_bus". Key (grid_result_id, from_bus)=(5, 3) is not '
        'present in table "pandapower_bus". Repair or delete these rows, then run pylovo-setup again.')
    assert "ROLLBACK" in conn.log and not any("INSERT INTO pylovo.schema_migrations" in q for q in conn.log)
