"""PLZ working tables, transaction helpers and small lookups shared by the other mixins."""

import warnings
import re

import psycopg2 as psy

import pandas as pd
from psycopg2 import sql

from pylovo.config_loader import MUNICIPAL_REGISTER, VERSION_ID
from pylovo.database.base_mixin import BaseMixin, plz_table_name
from pylovo.database.config_table_structure import REFRESH_QUERIES, TEMP_CREATE_QUERIES

warnings.simplefilter(action='ignore', category=UserWarning)


class UtilsMixin(BaseMixin):
    """PLZ working tables, transaction helpers and small shared lookups."""

    def create_temp_tables(self, plz: int) -> None:
        """Create the working tables of one PLZ and session-local views on them.

        The working tables are session-local and disappear if the connection ends.
        A temporary view exposes each table under its base name to existing queries.
        Different sessions can use the same postcode without touching each other's
        staging data; the generation caller serializes result writes with an advisory lock.

        Args:
            plz: Postcode whose working tables are created.
        """
        self.drop_temp_tables(plz)
        for base_name, query in TEMP_CREATE_QUERIES.items():
            table_name = plz_table_name(base_name, plz)
            self.cur.execute(query.replace(base_name, table_name))
            # The view keeps existing unqualified SQL working on this session's table.
            self.cur.execute(
                sql.SQL("CREATE TEMP VIEW {} AS SELECT * FROM {}").format(
                    sql.Identifier(base_name), sql.Identifier("pg_temp", table_name)
                )
            )

    def drop_temp_tables(self, plz: int) -> None:
        """Drop the working tables of one PLZ, its pgRouting vertices table and the session views.

        Args:
            plz: Postcode whose working tables are dropped.
        """
        for base_name in TEMP_CREATE_QUERIES:
            self.cur.execute(sql.SQL("DROP VIEW IF EXISTS {}").format(sql.Identifier("pg_temp", base_name)))
            self.cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {}").format(
                    sql.Identifier("pg_temp", plz_table_name(base_name, plz))
                )
            )
        self.cur.execute("DROP VIEW IF EXISTS pg_temp.ways_tem_vertices_pgr")
        # Vertices table written by build_pgr_network_topology()
        self.cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {}").format(
                sql.Identifier("pg_temp", plz_table_name("ways_tem", plz) + "_vertices_pgr")
            )
        )

    def acquire_plz_lock(self, plz: int) -> None:
        """Serialize generation of one postcode across database sessions."""
        self.cur.execute("SELECT pg_advisory_lock(%s, %s)", (907361, int(plz)))

    def release_plz_lock(self, plz: int) -> None:
        """Release the session lock even after a failed generation transaction."""
        self.cur.execute("SELECT pg_advisory_unlock(%s, %s)", (907361, int(plz)))

    def drop_orphaned_plz_temp_tables(self) -> None:
        """Legacy entry point: session-local tables are reclaimed by PostgreSQL.

        Persistent staging tables from older versions require a separate, reviewed cleanup.
        """
        self.logger.info("Session-local staging requires no global orphan cleanup.")

    def refresh_materialized_views(self) -> None:
        """Refresh the materialized views of ``REFRESH_QUERIES`` so they reflect the result tables."""
        for query in REFRESH_QUERIES.values():
            self.cur.execute(query)

    def commit_changes(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    def is_table_empty(self, table_name: str) -> bool:
        """Check an allowlisted PyLovo table with EXISTS; propagate database errors."""
        schema, sep, table = table_name.rpartition(".")
        if not sep:
            schema, table = "pylovo", table_name
        if schema != "pylovo" or not re.fullmatch(r"[a-z][a-z0-9_]*", table):
            raise ValueError("Only simple pylovo table names are accepted")
        identifier = sql.Identifier("pylovo", table)
        try:
            self.cur.execute("SELECT to_regclass(%s)", (identifier.as_string(self.cur),))
            if self.cur.fetchone()[0] is None:
                raise ValueError(f"Table pylovo.{table} does not exist")
            self.cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(identifier))
            return not self.cur.fetchone()[0]
        except psy.Error:
            self.conn.rollback()
            raise

    def get_grid_result_id(self, plz: int, kcid: int, bcid: int, version_id: str | None = None) -> int | None:
        """Return the ``grid_result_id`` of one grid.

        Args:
            plz: Postcode of the grid.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            version_id: Version to look in; defaults to the configured ``VERSION_ID``.

        Returns:
            The ID, or ``None`` if the grid does not exist.
        """
        effective_version_id = VERSION_ID if version_id is None else str(version_id)
        query = """
            SELECT grid_result_id
            FROM pylovo.grid_result
            WHERE version_id = %s
              AND plz = %s
              AND kcid = %s
              AND bcid = %s
            LIMIT 1
        """
        self.cur.execute(query, vars=(effective_version_id, plz, kcid, bcid))
        result = self.cur.fetchone()
        if result is None:
            return None
        return int(result[0])

    def get_list_from_plz(self, plz: int) -> list:
        """Return the ``(kcid, bcid)`` pairs of all grids of ``plz`` in the configured version."""
        query = """SELECT DISTINCT kcid, bcid
                   FROM pylovo.grid_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s
                   ORDER BY kcid, bcid;"""
        self.cur.execute(query, {"p": plz, "v": VERSION_ID})
        cluster_list = self.cur.fetchall()

        return cluster_list

    def delete_transformers_from_buildings_tem(self, vertices: list) -> None:
        """
        Deletes selected transformers from buildings_tem
        :param vertices:
        :return:
        """
        query = """
                DELETE
                FROM buildings_tem
                WHERE vertice_id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def get_consumer_categories(self):
        """
        Returns: A dataframe with self-defined consumer categories and typical values
        """
        query = f"""SELECT *
                   FROM pylovo.consumer_categories"""
        cc_df = pd.read_sql_query(query, self.conn)
        cc_df.set_index("definition", drop=False, inplace=True)
        cc_df.sort_index(inplace=True)
        self.logger.debug("Consumer categories fetched.")
        return cc_df

    def get_municipal_register(self) -> pd.DataFrame:
        """Return the complete municipal register as a DataFrame with the ``MUNICIPAL_REGISTER`` columns."""
        query = """SELECT *
                   FROM pylovo.municipal_register;"""
        self.cur.execute(query)
        register = self.cur.fetchall()
        return pd.DataFrame(register, columns=MUNICIPAL_REGISTER)

    def get_municipal_register_for_plz(self, plz: int) -> pd.DataFrame:
        """Return the municipal register rows of one PLZ (several rows if it spans municipalities)."""
        query = """SELECT *
                   FROM pylovo.municipal_register
                   WHERE plz = %(p)s;"""
        self.cur.execute(query, {"p": int(plz)})
        register = self.cur.fetchall()
        return pd.DataFrame(register, columns=MUNICIPAL_REGISTER)
