"""The InfDB source data of the MV stage, read in the pylovo database or through a separate read-only connection.

The MV stage reads the street graph, the address buildings, the OSM layers, the municipality areas and the eGon-data
substations of InfDB (``INFDB_SOURCE_SCHEMA``, ``INFDB_OSM_SCHEMA``, ``INFDB_OPENDATA_SCHEMA``). Where these schemas
are depends on the ``.env`` settings:

* **local** (``USE_EXTERNAL_BASEDATA`` off, or its ``INFDB_*`` settings name the pylovo database itself): the queries
  run on the pylovo connection, in the caller's transaction;
* **remote** (``USE_EXTERNAL_BASEDATA`` with another database): the queries run on a separate read-only connection
  with the rules of :class:`~pylovo.infdb.infdb_client.InfdbClient`: no startup ``options`` and no ``SET`` (so it
  works through PgBouncer), every query in its own read-only transaction that is closed right after its fetch.

:class:`SourceReader` hides the difference. Its queries are schema-qualified and never touch a pylovo table. Rows the
stage processes further in SQL are first staged into a temporary table of the pylovo connection
(:meth:`SourceReader.stage`): with ``CREATE TEMP TABLE ... AS`` in the local mode, through ``COPY`` in the remote
mode. All later SQL runs on the staged rows, so both modes compute the same results.
"""
from __future__ import annotations

import tempfile
from typing import Any, Callable, Sequence

import psycopg2
from psycopg2 import sql

SRID_SQL = sql.SQL("SELECT ST_SRID(geom) FROM {table} WHERE geom IS NOT NULL LIMIT 1")

# a probe that fails would abort the caller's transaction: check existence and privilege first
TABLE_EXISTS_SQL = ("SELECT c IS NOT NULL AND has_table_privilege(c, 'SELECT') "
                    "FROM (SELECT to_regclass(%s) AS c) AS r")

COLUMN_TYPES_SQL = """
    SELECT a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a
    WHERE a.attrelid = to_regclass(%(table)s) AND a.attnum > 0 AND NOT a.attisdropped AND a.attname = ANY(%(names)s)
"""


def regclass_text(schema: str, table: str) -> str:
    """``"schema"."table"`` for ``to_regclass`` (quoted, so mixed case and odd names stay exact)."""
    return ".".join('"' + part.replace('"', '""') + '"' for part in (schema, table))


def source_is_remote(config: Any) -> bool:
    """Whether the InfDB source schemas lie in another database than the schema ``pylovo``: ``USE_INFDB`` and
    ``USE_EXTERNAL_BASEDATA`` with ``INFDB_DBNAME``, ``INFDB_HOST`` or ``INFDB_PORT`` of another database.

    Args:
        config: The module :mod:`pylovo.config_loader` (or an object with its connection attributes).
    """
    if not getattr(config, "USE_INFDB", False) or not getattr(config, "USE_EXTERNAL_BASEDATA", False):
        return False
    source = (config.INFDB_DBNAME, config.INFDB_HOST, str(config.INFDB_PORT))
    return source != (config.DBNAME, config.HOST, str(config.PORT))


def connect_infdb(config: Any) -> Any:
    """A connection to the InfDB of the ``INFDB_*`` settings, read-only, without startup ``options``."""
    conn = psycopg2.connect(database=config.INFDB_DBNAME, user=config.INFDB_USER, password=config.INFDB_PASSWORD,
                            host=config.INFDB_HOST, port=config.INFDB_PORT)
    # BEGIN READ ONLY per transaction; with autocommit psycopg2 would SET it for the session, which a PgBouncer in
    # transaction mode can pass on to other clients
    conn.set_session(readonly=True)
    return conn


class SourceReader:
    """Read-only queries on the InfDB source schemas, local or remote (see the module docstring).

    Args:
        local_cursor: Returns the cursor of the pylovo connection (called on every use, so a reconnected client
            hands out its new cursor).
        remote_connect: Opens the separate connection to the InfDB; None for the local mode. The connection is
            opened on first use and again after :meth:`close`.
        describe: Where the remote InfDB is, for messages (``dbname@host:port``).
    """

    def __init__(self, local_cursor: Callable[[], Any], remote_connect: Callable[[], Any] | None = None,
                 describe: str = "") -> None:
        self._local_cursor = local_cursor
        self._remote_connect = remote_connect
        self._conn: Any = None
        self._srid: dict[tuple[str, str], int | None] = {}
        self._types: dict[tuple[str, str], dict[str, str]] = {}
        self.where = describe if remote_connect is not None else "the pylovo database"

    @classmethod
    def from_config(cls, local_cursor: Callable[[], Any], config: Any = None) -> "SourceReader":
        """The reader of the ``.env`` settings: remote with ``USE_EXTERNAL_BASEDATA`` and another database."""
        if config is None:
            from pylovo import config_loader as config
        if not source_is_remote(config):
            return cls(local_cursor)
        return cls(local_cursor, lambda: connect_infdb(config),
                   f"{config.INFDB_DBNAME}@{config.INFDB_HOST}:{config.INFDB_PORT}")

    @property
    def remote(self) -> bool:
        """Whether the source schemas lie in another database (separate connection)."""
        return self._remote_connect is not None

    def close(self) -> None:
        """Close the separate connection (it is opened again on the next query); nothing to do in the local mode."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def _remote(self) -> Any:
        if self._conn is None or self._conn.closed:
            self._conn = self._remote_connect()
        return self._conn

    # ------------------------------------------------------------------------------------------------ queries
    def fetchall(self, query: Any, params: Any = None) -> list[tuple]:
        """All rows of a query on the source schemas (local: in the current transaction of the pylovo connection)."""
        if not self.remote:
            cur = self._local_cursor()
            cur.execute(query, params)
            return cur.fetchall()
        conn = self._remote()
        with conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()

    def fetchone(self, query: Any, params: Any = None) -> tuple | None:
        """The first row of a query on the source schemas (None without rows)."""
        rows = self.fetchall(query, params)
        return rows[0] if rows else None

    def table_exists(self, schema: str | None, table: str) -> bool:
        """Whether ``schema.table`` exists in the source database and may be read."""
        if not schema:
            return False
        row = self.fetchone(TABLE_EXISTS_SQL, (regclass_text(schema, table),))
        return bool(row and row[0])

    def srid(self, schema: str, table: str) -> int | None:
        """The SRID of the geometries of a source table, read from a row (``geometry_columns`` may say 0); None
        without geometries with an SRID. Cached per table."""
        key = (schema, table)
        if key not in self._srid:
            row = self.fetchone(SRID_SQL.format(table=sql.Identifier(schema, table)))
            self._srid[key] = int(row[0]) if row and row[0] else None
        return self._srid[key]

    def column_types(self, schema: str, table: str, names: Sequence[str]) -> list[tuple[str, str]]:
        """``(name, type)`` of the given columns of a source table (``format_type``), in the order of ``names``.

        Raises:
            LookupError: A column does not exist.
        """
        key = (schema, table)
        known = self._types.setdefault(key, {})
        wanted = [n for n in names if n not in known]
        if wanted:
            known.update({str(n): str(t) for n, t in self.fetchall(
                COLUMN_TYPES_SQL, {"table": regclass_text(schema, table), "names": list(wanted)})})
        missing = [n for n in names if n not in known]
        if missing:
            raise LookupError(f"{schema}.{table} has no column {', '.join(missing)}")
        return [(n, known[n]) for n in names]

    def stage(self, temp_table: str, query: sql.Composable, params: Any,
              columns: Sequence[tuple[str, str]] | Callable[[], Sequence[tuple[str, str]]]) -> int:
        """Stage the rows of a query on the source schemas into a temporary table of the pylovo connection.

        The table is created ``ON COMMIT DROP`` in the current transaction of the pylovo connection (a table of the
        same name is dropped first). Local: ``CREATE TEMP TABLE ... AS`` the query; remote: the table is created with
        ``columns`` and filled through ``COPY`` (text format; geometries keep their exact EWKB), buffered in a
        temporary file.

        Args:
            temp_table: Name of the temporary table.
            query: A ``SELECT`` on the source schemas whose output columns match ``columns``.
            params: Its parameters.
            columns: ``(name, type)`` of the output columns (the types of the source columns, see
                :meth:`column_types`), or a callable that returns them (called in the remote mode only).

        Returns:
            The number of staged rows.
        """
        local = self._local_cursor()
        table = sql.Identifier(temp_table)
        local.execute(sql.SQL("DROP TABLE IF EXISTS pg_temp.{}").format(table))
        if not self.remote:
            local.execute(sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS ").format(table) + query, params)
        else:
            columns = columns() if callable(columns) else columns
            local.execute(sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                table, sql.SQL(", ").join(sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(t)) for n, t in columns)))
            conn = self._remote()
            with tempfile.TemporaryFile() as buffer:
                with conn, conn.cursor() as cur:
                    select = cur.mogrify(query, params).decode()
                    cur.copy_expert(f"COPY ({select}) TO STDOUT", buffer)
                buffer.seek(0)
                local.copy_expert(sql.SQL("COPY {} FROM STDIN").format(table), buffer)
        local.execute(sql.SQL("SELECT count(*) FROM {}").format(table))
        return int(local.fetchone()[0])
