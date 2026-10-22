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


def external_basedata() -> bool:
    """Whether the InfDB source schemas (``basedata``, ``opendata``, ``citydb``) are in another database than pylovo's.

    That is ``USE_INFDB`` with ``USE_EXTERNAL_BASEDATA``; the other database is described by the ``INFDB_*`` settings.
    """
    from pylovo import config_loader as cl

    return bool(cl.USE_INFDB and cl.USE_EXTERNAL_BASEDATA)


def settings() -> dict[str, Any]:
    """Connection settings of the configured pylovo database (without the password).

    ``use_external_basedata`` says that the InfDB is another database, ``infdb_target`` is then its ``host:port/dbname``.
    """
    from pylovo import config_loader as cl

    external = external_basedata()
    return {
        "host": cl.HOST,
        "port": str(cl.PORT),
        "dbname": cl.DBNAME,
        "user": cl.DBUSER,
        "use_infdb": bool(cl.USE_INFDB),
        "use_external_basedata": external,
        "infdb_target": f"{cl.INFDB_HOST}:{cl.INFDB_PORT}/{cl.INFDB_DBNAME}" if external else None,
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


def connect_source(timeout_s: int = 5):
    """A read-only connection to the InfDB with the ``INFDB_*`` settings, made the way ``InfdbClient`` connects.

    No startup ``options`` are sent (a PgBouncer older than 1.20 refuses them), so the search path and the
    timeouts have to be set per transaction with ``SET LOCAL``. psycopg2 begins every transaction READ ONLY.

    Raises:
        psycopg2.OperationalError: If the InfDB cannot be reached.
    """
    from pylovo import config_loader as cl

    conn = psycopg2.connect(dbname=cl.INFDB_DBNAME, user=cl.INFDB_USER, password=cl.INFDB_PASSWORD,
                            host=cl.INFDB_HOST, port=cl.INFDB_PORT, connect_timeout=timeout_s)
    conn.set_session(readonly=True)
    return conn


@contextmanager
def source_cursor(cur=None, timeout_s: int = 5, statement_timeout_ms: int = 120_000) -> Iterator[psycopg2.extras.RealDictCursor]:
    """Yield a dict cursor for reading the InfDB source schemas (``basedata``, ``opendata``, ``citydb``).

    With the InfDB in the pylovo database that is ``cur`` (or a new :func:`cursor`). With
    ``USE_EXTERNAL_BASEDATA`` it is a cursor on the InfDB (:func:`connect_source`) in one read-only
    transaction with the source schema first on the search path; the pylovo connection is never used.
    """
    if not external_basedata():
        if cur is not None:
            yield cur
        else:
            with cursor(timeout_s=timeout_s, statement_timeout_ms=statement_timeout_ms) as own:
                yield own
        return
    from pylovo import config_loader as cl

    conn = connect_source(timeout_s)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as source:
            source.execute("SET LOCAL search_path = %s, public; SET LOCAL statement_timeout = %s",
                           (cl.INFDB_SOURCE_SCHEMA, statement_timeout_ms))
            yield source
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def fetch_all(sql: str, params: Any = None, cur=None) -> list[dict]:
    """Run a read query and return all rows as dicts (on ``cur`` if given, else on a connection of its own)."""
    if cur is None:
        with cursor() as own:
            return fetch_all(sql, params, own)
    cur.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def fetch_one(sql: str, params: Any = None, cur=None) -> dict | None:
    """Run a read query and return the first row or ``None`` (on ``cur`` if given, else on a connection of its own)."""
    if cur is None:
        with cursor() as own:
            return fetch_one(sql, params, own)
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
