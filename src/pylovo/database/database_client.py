"""Connection to the pylovo database, composed of the query mixins of this package."""

import warnings

import psycopg2 as psy
from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from pylovo import utils
from pylovo.config_loader import DBNAME, DBUSER, HOST, LOG_LEVEL, PASSWORD, PORT
from pylovo.database.analysis_mixin import AnalysisMixin
from pylovo.database.clustering_mixin import ClusteringMixin
from pylovo.database.grid_mixin import GridMixin
from pylovo.database.load_edit_mixin import LoadEditMixin
from pylovo.database.preprocessing_mixin import PreprocessingMixin
from pylovo.database.results_mixin import ResultsMixin
from pylovo.database.transformer_ui_mixin import TransformerUiMixin
from pylovo.database.utils_mixin import UtilsMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class DatabaseClient(
    PreprocessingMixin,
    ClusteringMixin,
    GridMixin,
    AnalysisMixin,
    ResultsMixin,
    TransformerUiMixin,
    UtilsMixin,
    LoadEditMixin,
):
    """Connection to the pylovo database with all query methods.

    The query methods come from the mixins:

    * ``PreprocessingMixin``: version snapshot, configuration tables, and loading buildings,
      transformers and ways into the PLZ working tables.
    * ``ClusteringMixin``: connected components, k-means and building clusters, transformer sizing.
    * ``GridMixin``: routing and line queries for cable installation and visualisation rows.
    * ``AnalysisMixin``: pandapower persistence, analysis parameters and GeoDataFrame readers.
    * ``ResultsMixin``: saving working tables as results and deleting results.
    * ``TransformerUiMixin``: queries of the transformer map UI.
    * ``UtilsMixin``: PLZ working tables, transactions and shared lookups.
    * ``LoadEditMixin``: manual load edits of stored grids and their audit table (:mod:`pylovo.load_editing`).

    All queries share one psycopg2 connection (``search_path`` ``pylovo, public``) and cursor.
    Most methods do not commit; call ``commit_changes()`` or ``rollback_changes()``. Use the
    client as a context manager or call ``close()`` when done.

    Args:
        dbname: Database name; defaults to ``DBNAME`` from the ``.env`` file.
        user: Database user; defaults to ``DBUSER``.
        pw: Password; defaults to ``PASSWORD``.
        host: Host; defaults to ``HOST``.
        port: Port; defaults to ``PORT``.
        **kwargs: ``log_file``: log file path (default ``log/log.txt``).

    Raises:
        psycopg2.OperationalError: If the database cannot be reached.
    """

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
        self.db_path = URL.create(
            "postgresql+psycopg2", username=user, password=pw,
            host=host, port=int(port), database=dbname,
        )
        try:
            self._connect()
        except psy.OperationalError as err:
            self.logger.warning(
                f"Connecting to {dbname} was not successful. Make sure, that you have established the SSH connection with correct port mapping."
            )
            raise err

        self.logger.debug("DatabaseClient connected to %s.", self.db_path.render_as_string(hide_password=True))

    def _connect(self) -> None:
        """Open the psycopg2 connection, its shared cursor and the SQLAlchemy engine."""
        self.conn = psy.connect(**self._connect_kwargs)
        self.cur = self.conn.cursor()
        self.sqla_engine = create_engine(
            self.db_path,
            connect_args={"options": self._connect_kwargs["options"]},
        )

    def _close_handles(self) -> None:
        """Close cursor, connection and engine; safe on a partly constructed or closed client."""
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
        """Return whether the connection is open and answers a trivial query."""
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
        """Reconnect if the connection is closed or broken.

        Uncommitted work of the old connection is lost.

        Args:
            force: Reconnect even if the current connection still works.
        """
        if not force and self._is_connection_usable():
            return

        self._close_handles()
        self._connect()
        self.logger.info("Re-established database connection.")

    def rollback_changes(self) -> None:
        """Roll back the current transaction; does nothing if the connection is already gone."""
        try:
            if hasattr(self, 'conn') and self.conn and self.conn.closed == 0:
                self.conn.rollback()
        except (psy.InterfaceError, psy.OperationalError) as err:
            self.logger.warning(f"Rollback skipped because the database connection is unavailable: {err}")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: close the connection without committing."""
        self.close()

    def close(self):
        """Close cursor, connection and engine. Uncommitted changes are discarded."""
        self._close_handles()

    def __del__(self):
        """Close the connection when the client is garbage collected."""
        self.close()
