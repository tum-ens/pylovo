"""PLZ working tables, transaction helpers and small lookups shared by the other mixins."""

import warnings

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

        Every table of ``TEMP_CREATE_QUERIES`` is created as ``pylovo.<name>_<plz>`` so that
        parallel workers on different PLZ do not collide. A ``TEMP VIEW <name>`` exposes the table
        under its base name for the rest of the session; that is why most queries simply use
        ``buildings_tem`` and ``ways_tem``. Existing working tables of the PLZ are dropped first.

        Args:
            plz: Postcode whose working tables are created.
        """
        self.drop_temp_tables(plz)
        for base_name, query in TEMP_CREATE_QUERIES.items():
            table_name = plz_table_name(base_name, plz)
            self.cur.execute(query.replace(base_name, table_name))
            # For debugging, a regular view (CREATE OR REPLACE VIEW) makes the table visible
            # under its base name in other sessions as well.
            self.cur.execute(
                sql.SQL("CREATE TEMP VIEW {} AS SELECT * FROM {}").format(
                    sql.Identifier(base_name), sql.Identifier("pylovo", table_name)
                )
            )

    def drop_temp_tables(self, plz: int) -> None:
        """Drop the working tables of one PLZ, its pgRouting vertices table and the session views.

        Args:
            plz: Postcode whose working tables are dropped.
        """
        for base_name in TEMP_CREATE_QUERIES:
            self.cur.execute(sql.SQL("DROP VIEW IF EXISTS {} CASCADE").format(sql.Identifier(base_name)))
            self.cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                    sql.Identifier("pylovo", plz_table_name(base_name, plz))
                )
            )
        self.cur.execute("DROP VIEW IF EXISTS ways_tem_vertices_pgr CASCADE")
        # Vertices table written by build_pgr_network_topology()
        self.cur.execute(
            sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
                sql.Identifier("pylovo", plz_table_name("ways_tem", plz) + "_vertices_pgr")
            )
        )

    def drop_orphaned_plz_temp_tables(self) -> None:
        """Drop PLZ working tables left behind by interrupted runs.

        Only PLZ-suffixed tables are removed (for example ``buildings_tem_80805``,
        ``ways_tem_80805`` and ``ways_tem_80805_vertices_pgr``). Call it only while no other
        generation process works on the same database: their working tables look the same.
        """
        query = """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = %(schema)s
              AND (
                  tablename ~ '^buildings_tem_[0-9]+$'
                  OR tablename ~ '^ways_tem_[0-9]+$'
                  OR tablename ~ '^ways_tem_[0-9]+_vertices_pgr$'
              )
            ORDER BY tablename;
        """
        self.cur.execute(query, {"schema": "pylovo"})
        table_names = [row[0] for row in self.cur.fetchall()]

        for table_name in table_names:
            self.cur.execute(
                sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier("pylovo", table_name))
            )

        if table_names:
            self.logger.info(
                f"Dropped {len(table_names)} orphaned PLZ temp tables from previous runs."
            )

    def refresh_materialized_views(self) -> None:
        """Refresh the materialized views of ``REFRESH_QUERIES`` so they reflect the result tables."""
        for query in REFRESH_QUERIES.values():
            self.cur.execute(query)

    def commit_changes(self) -> None:
        """Commit the current transaction."""
        self.conn.commit()

    def is_table_empty(self, table_name: str) -> bool:
        """Return whether a table has no rows.

        Args:
            table_name: Table name, optionally schema-qualified; unqualified names refer to ``pylovo``.

        Returns:
            True if the table is empty or does not exist.
        """
        schema, _, table = table_name.rpartition(".")
        identifier = sql.Identifier(schema or "pylovo", table)
        # to_regclass() returns NULL for a missing table instead of raising, so the
        # transaction stays usable.
        self.cur.execute("SELECT to_regclass(%s);", (identifier.as_string(self.cur),))
        if self.cur.fetchone()[0] is None:
            return True
        self.cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {});").format(identifier))
        return not self.cur.fetchone()[0]

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
