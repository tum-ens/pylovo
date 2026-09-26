"""Small PostgreSQL helper for the UI's read queries.

Connection settings come from :mod:`pylovo.config_loader` (i.e. the project's ``.env``), so
the UI always talks to the same database as the CLI. Every request opens its own short-lived
connection: it is cheap on a local database, survives database restarts, and never keeps a
lock that could block a running ``pylovo-*`` job.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

APPLICATION_NAME = "pylovo-api"


class DatabaseUnavailable(RuntimeError):
    """Raised when the configured database cannot be reached."""


def settings() -> dict[str, Any]:
    """Connection settings of the configured pylovo database (without the password)."""
    from pylovo import config_loader as cl

    return {
        "host": cl.HOST,
        "port": str(cl.PORT),
        "dbname": cl.DBNAME,
        "user": cl.DBUSER,
        "use_infdb": bool(cl.USE_INFDB),
        "infdb_source_schema": cl.INFDB_SOURCE_SCHEMA,
        "infdb_opendata_schema": cl.INFDB_OPENDATA_SCHEMA,
        "target_epsg": int(cl.TARGET_EPSG),
    }


def _connect(readonly: bool, timeout_s: int = 5, statement_timeout_ms: int = 120_000):
    from pylovo import config_loader as cl

    try:
        conn = psycopg2.connect(
            dbname=cl.DBNAME,
            user=cl.DBUSER,
            password=cl.PASSWORD,
            host=cl.HOST,
            port=cl.PORT,
            connect_timeout=timeout_s,
            application_name=APPLICATION_NAME,
            options=f"-c search_path=pylovo,public -c statement_timeout={statement_timeout_ms}",
        )
    except psycopg2.OperationalError as exc:
        raise DatabaseUnavailable(str(exc).strip()) from exc
    conn.set_session(readonly=readonly)
    return conn


@contextmanager
def cursor(readonly: bool = True, **kwargs) -> Iterator[psycopg2.extras.RealDictCursor]:
    """Yield a dict cursor on a fresh connection; commit on success, roll back on error."""
    conn = _connect(readonly, **kwargs)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: Any = None) -> list[dict]:
    """Run a read query and return all rows as dicts."""
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def fetch_one(sql: str, params: Any = None) -> dict | None:
    """Run a read query and return the first row (or ``None``)."""
    with cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None


def existing_tables(cur, schema: str = "pylovo") -> set[str]:
    """Names of all tables, views and materialized views in ``schema``."""
    cur.execute(
        """SELECT c.relname AS name FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'p')""",
        (schema,),
    )
    return {row["name"] for row in cur.fetchall()}


@contextmanager
def database_client():
    """Yield a :class:`pylovo.database.database_client.DatabaseClient` (used for all edits).

    The transformer editor deliberately writes through pylovo's own ``*_trafo_ui`` methods so
    the UI and the library share one implementation of those edits.
    """
    from pylovo.database.database_client import DatabaseClient

    try:
        client = DatabaseClient()
    except psycopg2.OperationalError as exc:
        raise DatabaseUnavailable(str(exc).strip()) from exc
    try:
        yield client
    finally:
        client.close()
