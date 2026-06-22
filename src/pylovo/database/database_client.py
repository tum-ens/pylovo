import warnings
import psycopg2 as psy
from sqlalchemy import create_engine
from typing import override

from pylovo import utils
from pylovo.config_loader import *
from pylovo.database.preprocessing_mixin import PreprocessingMixin
from pylovo.database.clustering_mixin import ClusteringMixin
from pylovo.database.grid_mixin import GridMixin
from pylovo.database.analysis_mixin import AnalysisMixin
from pylovo.database.utils_mixin import UtilsMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class DatabaseClient(PreprocessingMixin, ClusteringMixin, GridMixin, AnalysisMixin, UtilsMixin):
    """Main database client handling connections."""

    def __init__(self, dbname=DBNAME, user=DBUSER, pw=PASSWORD, host=HOST, port=PORT, **kwargs):
        self.logger = utils.create_logger(
            "DatabaseClient", log_file=kwargs.get("log_file", "log/log.txt"), log_level=LOG_LEVEL
        )
        self._connect_kwargs = {
            "database": dbname,
            "user": user,
            "password": pw,
            "host": host,
            "port": port,
            "options": "-c search_path=pylovo,public",
        }
        self.db_path = f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{dbname}"
        try:
            self._connect()
        except psy.OperationalError as err:
            self.logger.warning(
                f"Connecting to {dbname} was not successful. Make sure, that you have established the SSH connection with correct port mapping."
            )
            raise err

        # init supers after everything is set up
        super().__init__()

        self.logger.debug(f"DatabaseClient is constructed and connected to {self.db_path}.")

    def _connect(self) -> None:
        self.conn = psy.connect(**self._connect_kwargs)
        self.cur = self.conn.cursor()
        self.sqla_engine = create_engine(
            self.db_path,
            connect_args={"options": self._connect_kwargs["options"]},
        )

    def _close_handles(self) -> None:
        try:
            if hasattr(self, 'cur') and self.cur:
                self.cur.close()
        except Exception as e:
            print(f"Warning: Error closing cursor: {e}")

        try:
            if hasattr(self, 'conn') and self.conn:
                self.conn.close()
        except Exception as e:
            print(f"Warning: Error closing connection: {e}")

        try:
            if hasattr(self, 'sqla_engine') and self.sqla_engine:
                self.sqla_engine.dispose()
        except Exception as e:
            print(f"Warning: Error disposing SQLAlchemy engine: {e}")

    def _is_connection_usable(self) -> bool:
        if not hasattr(self, 'conn') or self.conn is None or self.conn.closed != 0:
            return False

        try:
            with self.conn.cursor() as probe_cursor:
                probe_cursor.execute("SELECT 1;")
                probe_cursor.fetchone()
            return True
        except psy.Error:
            return False

    def ensure_connection(self, force: bool = False) -> None:
        if not force and self._is_connection_usable():
            return

        self._close_handles()
        self._connect()
        self.logger.info("Re-established database connection.")

    def rollback_changes(self) -> None:
        try:
            if hasattr(self, 'conn') and self.conn and self.conn.closed == 0:
                self.conn.rollback()
        except (psy.InterfaceError, psy.OperationalError) as err:
            self.logger.warning(f"Rollback skipped because the database connection is unavailable: {err}")

    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures proper cleanup."""
        self.close()
    
    def close(self):
        """Explicitly close all database connections."""
        self._close_handles()
    
    def __del__(self):
        """Clean up database connections."""
        self.close()

    @override
    def get_connection(self):
        return self.conn

    @override
    def get_logger(self):
        return self.logger

    @override
    def get_sqla_engine(self):
        return self.sqla_engine

    def is_table_empty(self, table_name: str) -> bool:
        """
        Check if a table is empty (has no rows).

        Parameters
        ----------
        table_name : str
            Name of the table to check

        Returns
        -------
        bool
            True if table is empty or doesn't exist, False if it has data
        """
        try:
            qualified_table_name = table_name if "." in table_name else f"pylovo.{table_name}"
            query = f"SELECT COUNT(*) FROM {qualified_table_name};"
            self.cur.execute(query)
            count = self.cur.fetchone()[0]
            return count == 0
        except Exception as e:
            self.logger.warning(f"Could not check if table {table_name} is empty: {e}")
            return True  # Assume empty if we can't check

    def save_tables(self, plz: int):

        """Saves building and ways results from ZIP code-specific temporary tables to the permanent results tables.
           Removes duplicates from the temporary building table to avoid violating the unique constraint."""

        # suffixed table names for the current PLZ
        buildings_table = f"buildings_tem_{plz}"
        ways_table = f"ways_tem_{plz}"

        # finding duplicates that violate the buildings_result_pkey constraint
        # the key of building result is (version_id, objectid, plz)
        query = f"""
                DELETE
                FROM {buildings_table} a USING (SELECT MIN(ctid) as ctid, objectid, plz
                                                FROM {buildings_table}
                                                GROUP BY (objectid, plz)
                                                HAVING COUNT(*) > 1) b
                WHERE a.objectid = b.objectid
                  AND a.plz = b.plz
                  AND a.ctid <> b.ctid;"""
        self.cur.execute(query)

        # Save building results
        query = f"""
            INSERT INTO pylovo.buildings_result
                (version_id, objectid, grid_result_id, id, feature_id, height, floor_area, floor_number,
                 building_use, building_use_id, building_type, type, occupants, households, construction_year,
                 postcode, address_street_id, street, house_number, geom, centroid, gemeindeschluessel,
                 changelog_id, assigned_way_id, peak_load_in_kw, vertice_id, connection_point, agg_connection_point)
                SELECT '{VERSION_ID}' as version_id, objectid, gr.grid_result_id, id, feature_id, height,
                       floor_area, floor_number, building_use, building_use_id, building_type, type, occupants,
                       households, bt.construction_year, postcode, address_street_id, street, house_number,
                       geom, centroid, gemeindeschluessel, changelog_id, assigned_way_id, peak_load_in_kw,
                       vertice_id, bt.connection_point, bt.agg_connection_point
            FROM pylovo.{buildings_table} bt
            JOIN pylovo.grid_result gr
                ON bt.plz = gr.plz AND bt.kcid = gr.kcid AND bt.bcid = gr.bcid and gr.version_id = '{VERSION_ID}'
                WHERE peak_load_in_kw != 0 AND peak_load_in_kw != -1;"""
        self.cur.execute(query)

        # Save ways results
        query = f"""INSERT INTO pylovo.ways_result
                        SELECT '{VERSION_ID}' as version_id, clazz, source, target, cost, reverse_cost, geom, way_id,
                %(p)s as plz FROM pylovo.{ways_table};"""

        self.cur.execute(query, vars={"p": plz})

    def delete_plz_from_all_tables(self, plz: int, version_id: str) -> None:
        """
        Deletes all entries of corresponding networks in all tables for the given Version ID and plz.
        :param plz: Postal code
        :param version_id: Version ID
        """
        query = f"""DELETE
               FROM pylovo.postcode_result
                   WHERE version_id = %(v)s
                     AND postcode_result_plz = %(p)s;"""
        self.cur.execute(query, {"v": version_id, "p": int(plz)})
        self.refresh_materialized_views()
        self.conn.commit()
        self.logger.info(f"All data for PLZ {plz} and version {version_id} deleted")

    def delete_version_from_all_tables(self, version_id: str) -> None:
        """Delete all entries of the given version ID from all tables."""
        query = "DELETE FROM pylovo.version WHERE version_id = %(v)s;"
        self.cur.execute(query, {"v": version_id})
        self.refresh_materialized_views()
        self.conn.commit()
        self.logger.info(f"Version {version_id} deleted from all tables")

    def delete_classification_version_from_related_tables(self, classification_id: str) -> None:
        """
        Deletes all rows with the given classification_id from related tables:
        transformer_classified, sample_set, and classification_version.

        :param classification_id: ID of the classification version to delete
        """
        query = "DELETE FROM pylovo.classification_version WHERE classification_id = %(cid)s;"
        self.cur.execute(query, {"cid": classification_id})
        self.conn.commit()

        self.logger.info(f"Deleted classification ID {classification_id}.")

    def delete_plz_from_sample_set_table(self, classification_id: str, plz: int) -> None:
        """
        Deletes the row corresponding to the given classification ID and PLZ from the sample_set table.

        :param classification_id: ID of the classification version
        :param plz: Postal code to be removed
        """
        query = """
                DELETE
            FROM pylovo.sample_set
                WHERE classification_id = %(cid)s
                  AND plz = %(p)s; \
                """
        self.cur.execute(query, {"cid": classification_id, "p": plz})
        self.conn.commit()
        self.logger.info(f"Deleted PLZ {plz} for classification ID {classification_id} from sample_set table.")

    def delete_transformers(self) -> None:
        """all transformers are deleted from table transformers in database"""
        delete_query = "TRUNCATE TABLE pylovo.transformers;"
        self.cur.execute(delete_query)
        self.conn.commit()
        self.logger.info('Transformers deleted.')

    def write_ags_log(self, ags: int) -> None:
        """write ags log to database: the amtliche gemeindeschluessel of the municipalities of which the buildings
        have already been imported to the database
        :param ags:  ags to be added
        :rtype ags: numpy integer 64
         """
        query = f"""INSERT INTO pylovo.ags_log (ags)
                   VALUES (%(a)s); """
        self.cur.execute(query, {"a": int(ags), })
        self.conn.commit()
