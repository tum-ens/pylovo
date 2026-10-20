"""Read-only database access of the viewer.

Every connection starts read-only (``default_transaction_read_only=on`` in its startup options),
every transaction is read-only as well (psycopg2 ``readonly=True``, i.e. ``BEGIN READ ONLY``) and gets
a statement timeout, so the viewer cannot change the database even by mistake. Connections are
pooled; a connection that broke (for example after a database restart) is dropped and the query is
retried once on a fresh one.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

from pylovo_viewer.settings import Settings

APPLICATION_NAME = "pylovo-viewer"
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"


class DatabaseError(RuntimeError):
    """The database cannot be reached, or a query failed."""


class SchemaError(DatabaseError):
    """A query needs a table or column that the database does not have (an older or partial pylovo schema)."""


def first_line(exc: BaseException) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0]


class Database:
    """A small pool of read-only connections to the database of :class:`Settings`."""

    def __init__(self, settings: Settings, *, max_connections: int = 6, statement_timeout_ms: int = 60_000,
                 connect_timeout_s: int = 5) -> None:
        self.settings = settings
        self.statement_timeout_ms = statement_timeout_ms
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            0, max_connections, dbname=settings.dbname, user=settings.user, password=settings.password,
            host=settings.host, port=settings.port, connect_timeout=connect_timeout_s,
            application_name=APPLICATION_NAME, options=READ_ONLY_OPTIONS)
        self._slots = threading.BoundedSemaphore(max_connections)   # the pool raises instead of waiting

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        """A dict cursor in a read-only transaction that is rolled back afterwards."""
        with self._slots:
            try:
                conn = self._pool.getconn()
            except psycopg2.Error as exc:
                raise DatabaseError(f"cannot connect to {self.settings.describe()}: {first_line(exc)}") from exc
            try:
                if conn.readonly is not True:
                    conn.set_session(readonly=True, autocommit=False)
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SET LOCAL statement_timeout = %s", (self.statement_timeout_ms,))
                    yield cur
            finally:
                if not conn.closed:
                    try:
                        conn.rollback()
                    except psycopg2.Error:
                        pass
                self._pool.putconn(conn, close=bool(conn.closed))

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        """All rows of ``sql``; a dropped connection is retried once."""
        for attempt in (1, 2):
            try:
                with self.cursor() as cur:
                    cur.execute(sql, params)
                    return list(cur.fetchall()) if cur.description else []
            except psycopg2.errors.QueryCanceled as exc:
                raise DatabaseError(f"the query took longer than {self.statement_timeout_ms // 1000} s") from exc
            except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn) as exc:
                raise SchemaError(first_line(exc)) from exc
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                if attempt == 1 and getattr(exc, "pgcode", None) is None:
                    continue   # connection-level failure (no SQLSTATE): try a fresh connection
                raise DatabaseError(first_line(exc)) from exc
            except psycopg2.Error as exc:
                raise DatabaseError(first_line(exc)) from exc
        raise AssertionError("unreachable")

    def one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self._pool.closeall()
