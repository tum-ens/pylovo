"""Shared state of the query mixins that make up :class:`~pylovo.database.database_client.DatabaseClient`."""

import logging

import psycopg2.extensions
import sqlalchemy

# pgRouting edge query over the session view ways_tem, written as a SQL string literal so it can
# be embedded directly as the first argument of pgr_* functions.
WAYS_TEM_EDGES_SQL = "'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem'"


class BaseMixin:
    """Attributes every query mixin relies on.

    The mixins contain queries only. ``DatabaseClient`` opens the connection and sets these
    attributes in its constructor, before any mixin method can run.

    Unless a docstring says otherwise, mixin methods run inside the current transaction and do
    not commit; the caller decides when to call ``commit_changes()``.

    Attributes:
        conn: psycopg2 connection with ``search_path`` set to ``pylovo, public``.
        cur: Cursor on ``conn`` that the mixin queries share.
        logger: Logger of the owning client.
        sqla_engine: SQLAlchemy engine used by pandas and geopandas readers.
    """

    conn: psycopg2.extensions.connection
    cur: psycopg2.extensions.cursor
    logger: logging.Logger
    sqla_engine: sqlalchemy.Engine


def plz_table_name(base_name: str, plz: int) -> str:
    """Return the name of a PLZ-specific working table, e.g. ``buildings_tem_80805``.

    ``plz`` is converted with ``int()`` so the name is always a plain SQL identifier.
    """
    return f"{base_name}_{int(plz)}"
