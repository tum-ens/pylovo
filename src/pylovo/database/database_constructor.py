"""Creation of the pylovo schema and import of the raw data (used by ``pylovo-setup``)."""

import os
import subprocess
import time
import warnings
from pathlib import Path

import pandas as pd
import psycopg2 as psy
import sqlparse
from psycopg2 import sql

import pylovo.database.database_client as dbc
from pylovo.config_loader import (
    DBNAME,
    DBUSER,
    HOST,
    PASSWORD,
    PORT,
    TARGET_EPSG,
    USE_INFDB,
)
from pylovo.data_import.import_transformers import (
    RELATION_ID,
    fetch_trafos,
    get_trafos_processed_target_geojson_path,
    process_trafos,
)

# Import table structure from packaged module (reliable for installed/editable usage)
from pylovo.database.config_table_structure import CREATE_QUERIES, INFDB_OPTIONAL_TABLES
from pylovo.database.migrations import PRE_SCHEMA_MIGRATIONS, POST_SCHEMA_MIGRATIONS, legacy_columns
from pylovo.infdb.infdb_client import InfdbClient
from pylovo.utils import get_user_data_dir


def _blocked_message(step: str, exc: psy.errors.DependentObjectsStillExist) -> str:
    """Name the database objects that keep a migration step from dropping or changing a view."""
    detail = "; ".join(line.strip() for line in (exc.diag.message_detail or "").splitlines() if line.strip())
    return (f"{step} stopped, nothing was changed: {exc.diag.message_primary}. {detail}. "
            "Drop or adapt these objects, then run pylovo-setup again.")


class DatabaseConstructor:
    """Create the ``pylovo`` schema, its tables and SQL functions, and import the raw data.

    Several methods delete or replace existing data; they are meant for ``pylovo-setup`` and
    the raw-data import, not for normal generation runs.

    Two raw-data paths exist. With ``USE_INFDB=True`` (default) buildings and ways are read
    from InfDB during generation, and setup only imports postcodes from InfDB. With
    ``USE_INFDB=False`` setup imports postcodes from CSV (``csv_to_db``) and the street
    network from the osm2po SQL file (``create_public_2po_table`` and ``ways_to_db``); building
    shapefiles are imported per municipality with ``ogr_to_db``.

    Args:
        dbc_obj: Existing ``DatabaseClient`` to use; a new one is created if omitted.
    """

    def __init__(self, dbc_obj=None):
        self.extensions_added = False

        if dbc_obj:
            self.dbc = dbc_obj
        else:
            self.dbc = dbc.DatabaseClient()

    def create_schema(self):
        """Create the ``pylovo`` schema if it does not exist, and commit."""
        try:
            with self.dbc.conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS pylovo")
                self.dbc.conn.commit()
        except (Exception, psy.DatabaseError) as error:
            print(f"Error creating schema: {error}")
            raise error

    def get_table_name_list(self):
        """Return the names of all tables and views in the ``pylovo`` schema."""
        with self.dbc.conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = %s""", ("pylovo",)
            )
            table_name_list = [tup[0] for tup in cur.fetchall()]

        return table_name_list

    def table_exists(self, table_name):
        """Return whether a table or view of this name exists in the ``pylovo`` schema."""
        return table_name in self.get_table_name_list()

    def create_table(self, table_name):
        """Create one table of ``CREATE_QUERIES``, or all of them, and commit.

        Also creates the extensions ``postgis`` and ``pgRouting`` in schema ``public`` on the first
        call (never in ``pylovo``, which ``reset_schema`` drops). With
        ``table_name="all"`` the queries run in the order of ``CREATE_QUERIES``; with
        ``USE_INFDB=True`` the file-based input tables (``INFDB_OPTIONAL_TABLES``) are skipped.

        Args:
            table_name: Key of ``CREATE_QUERIES``, or ``"all"``.

        Raises:
            ValueError: If ``table_name`` is not a key of ``CREATE_QUERIES`` or ``"all"``.
        """
        # create extension if not exists for recognition of geom datatypes
        if not self.extensions_added:
            with self.dbc.conn.cursor() as cur:
                # Explicit schema: without it PostgreSQL uses the first search_path entry, which is
                # ``pylovo``; DROP SCHEMA pylovo CASCADE would then drop the extension as well.
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public;")
                print("CREATE EXTENSION postgis")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgrouting SCHEMA public;")
                print("CREATE EXTENSION pgRouting")
                self.dbc.conn.commit()
                self.extensions_added = True

        if table_name == "all":
            with self.dbc.conn.cursor() as cur:
                create_queries = CREATE_QUERIES
                if USE_INFDB:
                    create_queries = {
                        name: query
                        for name, query in CREATE_QUERIES.items()
                        if name not in INFDB_OPTIONAL_TABLES
                    }

                for name, query in create_queries.items():
                    cur.execute(query)
                    print(f"CREATE TABLE {name}")
            self.dbc.conn.commit()
        elif table_name in CREATE_QUERIES:
            with self.dbc.conn.cursor() as cur:
                cur.execute(CREATE_QUERIES[table_name])
                print(f"CREATE TABLE {table_name}")
            self.dbc.conn.commit()
        else:
            raise ValueError(
                f"Table name {table_name} is not a valid parameter value for the function create_table. "
                "See config_table_structure.py"
            )

    def migrate_schema(self) -> None:
        """Install the baseline and apply each outstanding migration exactly once.

        A session advisory lock serializes concurrent setup processes. A migration
        and its ledger row commit together; failure leaves that step unapplied.
        """
        with self.dbc.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s, %s)", (907360, 0))
        self.dbc.conn.commit()
        try:
            self.create_schema()
            # Legacy geometry columns are added before baseline tables and views.
            with self.dbc.conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis SCHEMA public")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgrouting SCHEMA public")
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS pylovo.schema_migrations (
                        name text PRIMARY KEY,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )
                """)
            self.dbc.conn.commit()
            self.extensions_added = True

            applied = []

            def apply(name, action):
                with self.dbc.conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pylovo.schema_migrations WHERE name = %s", (name,))
                    if cur.fetchone():
                        self.dbc.conn.commit()
                        return
                    try:
                        action(cur)
                    except psy.errors.DependentObjectsStillExist as exc:
                        raise RuntimeError(_blocked_message(f"Migration {name}", exc)) from exc
                    cur.execute("INSERT INTO pylovo.schema_migrations (name) VALUES (%s)", (name,))
                    applied.append(name)
                self.dbc.conn.commit()

            for name, action in PRE_SCHEMA_MIGRATIONS:
                apply(name, action)
            apply("0002_legacy_columns", lambda cur: legacy_columns(cur, TARGET_EPSG))
            try:
                self.create_table("all")
            except psy.errors.DependentObjectsStillExist as exc:
                raise RuntimeError(_blocked_message("Creating the baseline tables and views", exc)) from exc
            for name, action in POST_SCHEMA_MIGRATIONS:
                apply(name, action)
            if applied:
                self.dbc.refresh_materialized_views()
            self.assert_schema()
            self.dbc.conn.commit()
        except Exception:
            self.dbc.conn.rollback()
            raise
        finally:
            with self.dbc.conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s, %s)", (907360, 0))
            self.dbc.conn.commit()

    def assert_schema(self) -> None:
        """Detect missing or unvalidated key constraints after migration."""
        required = {
            "fk_grid_result_transformer_equipment",
            "fk_tp_grid_version",
            "fk_tp_osm_id",
            "fk_pp_line_from_bus",
            "fk_pp_line_to_bus",
            "fk_pp_load_bus",
            "fk_pp_trafo_hv_bus",
            "fk_pp_trafo_lv_bus",
            "fk_lines_result_helper_source_line",
            "fk_lines_result_view_source_line",
            "uq_lines_result_grid_id",
            "fk_buildings_result_grid_result",
        }
        with self.dbc.conn.cursor() as cur:
            cur.execute("""
                SELECT conname, convalidated, pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE connamespace = 'pylovo'::regnamespace
                  AND conname = ANY(%s)
            """, (list(required),))
            found = {name: (validated, definition) for name, validated, definition in cur.fetchall()}
            missing = required - found.keys()
            invalid = {name for name, (validated, _) in found.items() if not validated}
            if missing or invalid:
                raise RuntimeError(
                    f"PyLovo schema is incomplete: missing={sorted(missing)}, "
                    f"not_validated={sorted(invalid)}"
                )
            if "SET NULL (transformer_equipment_name)" not in found[
                "fk_grid_result_transformer_equipment"
            ][1]:
                raise RuntimeError("PyLovo schema has unsafe equipment delete action")
            if "SET NULL" not in found["fk_tp_osm_id"][1]:
                raise RuntimeError("PyLovo schema has unsafe raw transformer delete action")
            cur.execute("SELECT to_regclass('pylovo.buildings_result_with_grid')")
            if cur.fetchone()[0] is None:
                raise RuntimeError("PyLovo schema is missing buildings_result_with_grid")
            cur.execute("""
                SELECT relkind FROM pg_class
                WHERE oid = 'pylovo.buildings_result_with_grid'::regclass
            """)
            if cur.fetchone()[0] != "v":
                raise RuntimeError("PyLovo building layer must be a regular view")
            cur.execute("SELECT to_regclass('pylovo.lines_result_cache'), "
                        "to_regclass('pylovo.lines_result_view')")
            if any(item is None for item in cur.fetchone()):
                raise RuntimeError("PyLovo line cache or compatibility view is missing")

    def ogr_to_db(self, ogr_file_list, skip_failures: bool = False):
        """Import geodata files into ``pylovo`` tables with ``ogr2ogr`` (GDAL).

        Used for the transformer GeoJSON and, with ``USE_INFDB=False``, for building
        shapefiles. Rows are appended if the table exists; otherwise ``ogr2ogr`` creates it.
        Geometries are promoted to multi-geometries and transformed to ``TARGET_EPSG``.

        Args:
            ogr_file_list: Dicts with ``path`` and optionally ``table_name`` (default: file stem).
            skip_failures: Pass ``-skipfailures`` to ``ogr2ogr`` and print the distinct errors.

        Raises:
            FileNotFoundError: If a file does not exist.
            subprocess.CalledProcessError: If ``ogr2ogr`` fails.
        """
        for file_dict in ogr_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            if not file_path.exists():
                raise FileNotFoundError(file_path)
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            table_exists = self.table_exists(table_name=table_name)
            print(f"ogr working for table {table_name} ({'append' if table_exists else 'create'})")
            command = [
                    "ogr2ogr",
                    "-append" if table_exists else "-overwrite",
                    "-progress",
                    "-f",
                    "PostgreSQL",
                    f"PG:dbname={DBNAME} user={DBUSER} host={HOST} port={PORT}",
                    file_path,
                    "-nln",
                    f"pylovo.{table_name}",  # explicitly tells ogr2ogr where to append (for the case of table already existing)
                    "-nlt",
                    "PROMOTE_TO_MULTI",
                    "-t_srs",
                    f"EPSG:{TARGET_EPSG}",
                    "-lco",
                    "geometry_name=geom",
                    "-lco", "SCHEMA=pylovo",  # ensures creation happens in correct schema
            ]
            if skip_failures:
                command.append("-skipfailures")

            result = subprocess.run(
                command, check=True, shell=False,
                stderr=subprocess.PIPE if skip_failures else None,
                env={**os.environ, "PGPASSWORD": PASSWORD},
            )
            if skip_failures:
                error_list = result.stderr.decode().replace("\r", "").split("\n")
                error_list = [e[e.find("ERROR: "):e.find("DETAIL: ")] for e in error_list]
                error_list = [e.strip("\n") for e in error_list if "ERROR: " in e]
                error_set = set(error_list)

                if error_set:
                    print(f"Warning: Error(s) occurred while processing {file_name}:")
                for error in error_set:
                    print("\t" + error)
                    if "duplicate key value violates unique constraint" in error:
                        print("\tThis is likely due to importing already existing data.")

            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")

    def transformers_to_db(self, clear_existing: bool = True):
        """Import the OSM transformers of ``RELATION_ID`` into ``pylovo.transformers`` and commit.

        The processed GeoJSON in ``data/transformer_data/processed_trafos/`` is reused if it
        exists; otherwise the transformers are fetched from the Overpass API and processed first
        (can take more than 30 minutes). Delete the GeoJSON to force a fresh download.

        Args:
            clear_existing: Replace OSM source rows only. Stored transformer positions
                retain their geometry and lose only their raw osm_id reference.
        """
        if clear_existing and self.table_exists(table_name="transformers"):
            warnings.warn("transformers table is overwritten!")
            with self.dbc.conn.cursor() as cur:
                cur.execute("DELETE FROM pylovo.transformers WHERE osm IS TRUE;")
            self.dbc.conn.commit()

        trafos_processed_target_geojson_path = get_trafos_processed_target_geojson_path(RELATION_ID)

        update_trafos = not os.path.isfile(trafos_processed_target_geojson_path)

        if update_trafos:
            print(f"{trafos_processed_target_geojson_path} does not exist -> fetch transformer data from API and process it")
            fetch_trafos(RELATION_ID)
            process_trafos(RELATION_ID)

        trafo_dict = [
            {
                "path": trafos_processed_target_geojson_path,
                "table_name": "transformers"
            }
        ]
        self.ogr_to_db(trafo_dict)

    def csv_to_db(self, csv_file_list):
        """Replace the content of ``pylovo`` tables with CSV files (file-based input, ``USE_INFDB=False``).

        Existing rows are deleted (and committed) first. The columns ``einwohner`` and ``gid``
        are renamed to ``population`` and ``postcode_id``.

        Args:
            csv_file_list: Dicts with ``path`` and optionally ``table_name`` (default: file stem),
                normally ``CSV_FILE_LIST``.

        Raises:
            FileNotFoundError: If a file does not exist.
        """
        for file_dict in csv_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            if not file_path.exists():
                raise FileNotFoundError(file_path)
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            if self.table_exists(table_name=table_name):
                warnings.warn(f"{table_name} table is overwritten!")
                with self.dbc.conn.cursor() as cur:
                    cur.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier("pylovo", table_name)))
                    self.dbc.conn.commit()
            # read and write
            df = pd.read_csv(file_path, index_col=False)
            df = df.rename(columns={"einwohner": "population", "gid": "postcode_id"})
            df.to_sql(
                name=table_name,
                con=self.dbc.sqla_engine,
                if_exists="append",
                index=False,
            )

            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")

    def load_postcode_from_infdb(self):
        """Replace the local ``postcode`` table with all postcodes of InfDB and commit.

        Raises:
            ValueError: If InfDB returns no postcodes.
        """
        st = time.time()

        infdb_client = InfdbClient()
        rows = infdb_client.fetch_all_postcodes_from_infdb()

        if not rows:
            raise ValueError("No postcode data retrieved from InfDB")

        if self.table_exists(table_name="postcode"):
            warnings.warn("postcode table is overwritten!")
            with self.dbc.conn.cursor() as cur:
                cur.execute("DELETE FROM pylovo.postcode")
                self.dbc.conn.commit()

        insert_query = f"""
            INSERT INTO pylovo.postcode (plz, note, qkm, population, geom)
            VALUES (%s, %s, %s, %s, ST_Transform(%s::geometry, {TARGET_EPSG}))
        """
        with self.dbc.conn.cursor() as cur:
            cur.executemany(insert_query, rows)
            self.dbc.conn.commit()

        et = time.time()
        print(f"Postcode data imported from InfDB in {int(et - st)} s")

    def create_public_2po_table(self):
        """Run the osm2po street SQL file ``<data dir>/ways/ways_public_2po_4pgr.sql`` (file-based input).

        The file creates and fills ``public_2po_4pgr``; ``ways_to_db`` converts it afterwards.
        It is read in chunks of 1 % of its size; complete statements are executed and committed
        immediately, an incomplete last statement is kept for the next chunk.
        """
        cur = self.dbc.conn.cursor()

        user_data = get_user_data_dir()
        sc_path = str(user_data / "ways" / "ways_public_2po_4pgr.sql")
        file_size = os.path.getsize(sc_path)

        chunk_size = max(1, file_size // 100)
        chars_read = 0

        leftover = ""  # Holds any partial statement that didn't end with a semicolon

        print("\nStart inserting ways into public_2po_4pgr table.")
        with open(sc_path, 'r', encoding='utf-8') as sc_file:
            while True:
                data = sc_file.read(chunk_size)
                if not data:
                    break

                chars_read += len(data)
                progress = round(chars_read * 100 / file_size)
                print(f"\rProgress: {progress}%", end="", flush=True)

                # Combine leftover from previous read with current chunk
                combined = leftover + data

                # Use sqlparse to split out complete statements
                statements = sqlparse.split(combined)

                # If sqlparse.split() returns multiple statements, the last one
                # might be incomplete. We’ll keep it as leftover if needed.
                if len(statements) > 1:
                    # Execute all statements except possibly the last
                    for stmt in statements[:-1]:
                        stmt = stmt.strip()
                        if stmt:
                            cur.execute(stmt)
                            self.dbc.conn.commit()

                    # Check if the last statement ends with a semicolon or not
                    last_stmt = statements[-1].strip()
                    if last_stmt.endswith(';'):
                        # It's a complete statement
                        cur.execute(last_stmt)
                        self.dbc.conn.commit()
                        leftover = ""
                    else:
                        leftover = last_stmt
                else:
                    # 0 or 1 statements from sqlparse
                    if len(statements) == 1:
                        # Could be complete or incomplete
                        stmt = statements[0].strip()
                        if stmt.endswith(';'):
                            # It's complete, execute it
                            cur.execute(stmt)
                            self.dbc.conn.commit()
                            leftover = ""
                        else:
                            # It's incomplete, keep it
                            leftover = stmt
                    else:
                        # No statements found. This can happen if combined was empty or whitespace.
                        # Just continue reading next chunk
                        pass
        print("\nInserted all ways into public_2po_4pgr table.")

    def ways_to_db(self):
        """Copy the osm2po ways from ``public_2po_4pgr`` into ``pylovo.ways``, drop the source table and commit.

        File-based input (``USE_INFDB=False``). Background:
        https://github.com/TongYe1997/Connector-syn-grid/issues/19
        """
        st = time.time()

        cur = self.dbc.conn.cursor()

        # Transform to ways table
        query = f"""INSERT INTO pylovo.ways
            SELECT  clazz,
                    source,
                    target,
                    cost,
                    reverse_cost,
                ST_Transform(geom_way, {TARGET_EPSG}) as geom,
                    id AS way_id
            FROM public_2po_4pgr"""
        cur.execute(query)

        # Drop public_2po_4pgr table, as it is not needed anymore
        query = "DROP TABLE public_2po_4pgr"
        cur.execute(query)

        self.dbc.conn.commit()

        et = time.time()
        print(f"Ways are successfully imported to db in {int(et - st)} s")

    def load_ways_preprocessing_functions(self):
        """Create or replace the SQL functions of ``pylovo/ways_preprocessing_functions`` and commit.

        All ``.sql`` files of ``utils/`` and then ``core/`` are executed in alphabetical order;
        ``PreprocessingMixin.preprocess_ways`` calls the ``core`` functions. On an error the
        transaction is rolled back and the error re-raised.
        """
        cur = self.dbc.conn.cursor()

        print("Loading ways preprocessing functions into schema 'pylovo'.")

        package_dir = Path(__file__).parent.parent  # Go up to pylovo package directory
        function_paths = [
            package_dir / "ways_preprocessing_functions" / "utils",
            package_dir / "ways_preprocessing_functions" / "core"
        ]

        filename = None
        try:
            for path in function_paths:
                for filename in sorted(os.listdir(path)):
                    if filename.endswith(".sql"):
                        full_file_path = path / filename
                        with open(full_file_path, 'r') as f:
                            sql_text = f.read()
                            cur.execute(sql_text)

            self.dbc.conn.commit()

        except Exception as e:
            error_msg = "[ERROR] Failed while loading SQL functions"
            if filename:
                error_msg += f" from file '{filename}'"
            error_msg += f": {e}"
            print(error_msg)
            self.dbc.conn.rollback()
            raise

    def reset_schema(self):
        """Drop only the PyLovo schema, aborting if another schema would lose objects.

        PostgreSQL CASCADE can remove views, functions, types or foreign keys in
        other schemas. Snapshot those catalog objects and roll the whole transaction
        back if any disappears. Extensions housed in PyLovo are never reset.
        """
        inventory = """
            SELECT 'relation', c.oid::text, c.oid::regclass::text FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname <> 'pylovo' AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
            UNION ALL
            SELECT 'procedure', p.oid::text, p.oid::regprocedure::text FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname <> 'pylovo' AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
            UNION ALL
            SELECT 'type', t.oid::text, NULL FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE n.nspname <> 'pylovo' AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
            UNION ALL
            SELECT 'constraint', c.oid::text,
                   c.conname || ' on ' || COALESCE(NULLIF(c.conrelid, 0)::regclass::text, c.contypid::regtype::text)
            FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname <> 'pylovo' AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
            UNION ALL
            SELECT 'trigger', t.oid::text,
                   CASE WHEN NOT t.tgisinternal THEN t.tgname || ' on ' || t.tgrelid::regclass::text END
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname <> 'pylovo' AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
            UNION ALL
            SELECT 'schema', n.oid::text, 'schema ' || n.nspname FROM pg_namespace n
            WHERE n.nspname <> 'pylovo' AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname <> 'information_schema'
            UNION ALL
            SELECT 'extension', e.oid::text, 'extension ' || e.extname FROM pg_extension e
        """
        try:
            with self.dbc.conn.cursor() as cur:
                cur.execute(
                    "SELECT extname FROM pg_extension "
                    "WHERE extnamespace = to_regnamespace('pylovo') ORDER BY 1"
                )
                extensions = [row[0] for row in cur.fetchall()]
                if extensions:
                    raise RuntimeError(
                        "Reset refused: extensions are installed in pylovo: "
                        + ", ".join(extensions)
                        + ". Move them to another schema before resetting."
                    )
                cur.execute(inventory)
                before = {(kind, oid): label for kind, oid, label in cur.fetchall()}
                cur.execute("DROP SCHEMA IF EXISTS pylovo CASCADE")
                cur.execute(inventory)
                lost = before.keys() - {(kind, oid) for kind, oid, _ in cur.fetchall()}
                if lost:
                    names = sorted(before[key] for key in lost if before[key])  # types follow their objects
                    shown = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
                    raise RuntimeError(
                        f"Reset refused: CASCADE would remove {len(lost)} object(s) "
                        f"outside pylovo ({shown}). The transaction was rolled back."
                    )
            self.dbc.conn.commit()
        except Exception:
            self.dbc.conn.rollback()
            raise
