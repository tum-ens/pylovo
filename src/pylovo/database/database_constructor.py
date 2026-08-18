import subprocess
import sys
import time
import warnings
from pathlib import Path

import psycopg2 as psy
import sqlparse
import pandas as pd

from pylovo.config_loader import *

# Import table structure from packaged module (reliable for installed/editable usage)
from pylovo.database.config_table_structure import CREATE_QUERIES, INFDB_OPTIONAL_TABLES

import pylovo.database.database_client as dbc
from pylovo.infdb.infdb_client import InfdbClient
from pylovo.data_import.import_transformers import process_trafos, get_trafos_processed_target_geojson_path, \
    fetch_trafos, RELATION_ID, EPSG
from pylovo.utils import get_user_data_dir


class DatabaseConstructor:
    """
    Constructs a ready to use src database. Be careful about overwriting the tables.
    It uses databaseClient to connect to the database and create tables and import data.
    """

    def __init__(self, dbc_obj=None):
        self.extensions_added = False

        if dbc_obj:
            self.dbc = dbc_obj
        else:
            self.dbc = dbc.DatabaseClient()

    def create_schema(self):
        """
        Creates the target schema if it doesn't exist.
        """
        try:
            with self.dbc.conn.cursor() as cur:
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS pylovo")
                self.dbc.conn.commit()
        except (Exception, psy.DatabaseError) as error:
            print(f"Error creating schema: {error}")
            raise error

    def get_table_name_list(self):
        with self.dbc.conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = %s""", ("pylovo",)
            )
            table_name_list = [tup[0] for tup in cur.fetchall()]

        return table_name_list

    def table_exists(self, table_name):
        if table_name in self.get_table_name_list():
            warnings.warn(f"{table_name} table is overwritten!")
            return True
        else:
            return False

    def create_table(self, table_name):
        # create extension if not exists for recognition of geom datatypes
        if not self.extensions_added:
            with self.dbc.conn.cursor() as cur:
                # create extension if not exists for recognition of geom datatypes
                cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
                print("CREATE EXTENSION postgis")
                cur.execute("CREATE EXTENSION IF NOT EXISTS pgRouting;")
                print("CREATE EXTENSION pgRouting")
                self.dbc.conn.commit()
                self.extensions_added = True

        if table_name == "all":
            try:
                with self.dbc.conn.cursor() as cur:
                    create_queries = CREATE_QUERIES
                    if USE_INFDB:
                        create_queries = {
                            name: query
                            for name, query in CREATE_QUERIES.items()
                            if name not in INFDB_OPTIONAL_TABLES
                        }

                    for table_name, query in create_queries.items():
                        cur.execute(query)
                        print(f"CREATE TABLE {table_name}")
                self.dbc.conn.commit()
            except (Exception, psy.DatabaseError) as error:
                raise error
        elif table_name in CREATE_QUERIES:
            try:
                with self.dbc.conn.cursor() as cur:
                    cur.execute(CREATE_QUERIES[table_name])
                    print(f"CREATE TABLE {table_name}")
                self.dbc.conn.commit()
            except (Exception, psy.DatabaseError) as error:
                raise error
        else:
            raise ValueError(
                f"Table name {table_name} is not a valid parameter value for the function create_table. See config.py"
            )

    def ogr_to_db(self, ogr_file_list, skip_failures: bool = False):
        """
            OGR/GDAL is a translator library for raster and vector geospatial data formats
            inserts building data specified into database
        """

        for file_dict in ogr_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists(), file_path
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            table_exists = self.table_exists(table_name=table_name)
            print("ogr working for table", table_name)
            command = [
                    "ogr2ogr",
                    "-append" if table_exists else "-overwrite",
                    "-progress",
                    "-f",
                    "PostgreSQL",
                    f"PG:dbname={DBNAME} user={DBUSER} password={PASSWORD} host={HOST} port={PORT}",
                    file_path,
                    "-nln",
                    f"pylovo.{table_name}",  # explicitly tells ogr2ogr where to append (for the case of table already existing)
                    "-nlt",
                    # "MULTIPOLYGON",
                    "PROMOTE_TO_MULTI",
                    "-t_srs",
                    f"EPSG:{TARGET_EPSG}",
                    "-lco",
                    "geometry_name=geom",
                    "-lco", f"SCHEMA=pylovo", # ensures creation happens in correct schema
            ]
            if skip_failures:
                command.append("-skipfailures")

            result = subprocess.run(command, check=True, shell=False, stderr=subprocess.PIPE if skip_failures else None)
            if skip_failures:
                error_list = result.stderr.decode().replace("\r", "").split("\n")
                error_list = [e[e.find("ERROR: "):e.find("DETAIL: ")] for e in error_list]
                error_list = [e.strip("\n") for e in error_list if "ERROR: " in e]
                error_set = set(error_list)
                
                print(f"Warning: Error(s) occurred while processing {file_name}:")
                for error in error_set:
                    print("\t" + error)
                    if "duplicate key value violates unique constraint" in error:
                        print("\tThis is likely due to importing already existing data.")

            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")


    def transformers_to_db(self, clear_existing: bool = True):
        """Call the overpass api for transformer data and populate the transformers table.
        Delete data/transformer_data/processed_trafos/*_trafos_processed_<epsg>.geojson to
        fetch fresh data from OSM.

        If clear_existing=True (default), all existing Transformer datasets are deleted before import
        to avoid duplicate primary keys.
        """
        # clear existing data to avoid duplicate primary keys
        if clear_existing and self.table_exists(table_name="transformers"):
            with self.dbc.conn.cursor() as cur:
                cur.execute(f"DELETE FROM pylovo.transformers;")
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

        for file_dict in csv_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists(), file_path
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            if self.table_exists(table_name=table_name):
                with self.dbc.conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table_name}")
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
        """
        Load postcode data from InfDB and insert into local 'postcode' table in pylovo.
        """
        st = time.time()

        # Create InfdbClient instance to connect to remote InfDB
        infdb_client = InfdbClient()

        # Fetch postcode data from InfDB
        rows = infdb_client.fetch_all_postcodes_from_infdb()

        if not rows:
            raise ValueError("No postcode data retrieved from InfDB")

        # Optional: Clear existing data from postcode table
        if self.table_exists(table_name="postcode"):
            with self.dbc.conn.cursor() as cur:
                cur.execute("DELETE FROM pylovo.postcode")
                self.dbc.conn.commit()

        # Insert rows into pylovo postcode table using executemany
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
        """
        Reads the large SQL file in 10% chunks, executes complete statements on-the-fly,
        and defers incomplete statements until the next chunk.
        """
        cur = self.dbc.conn.cursor()

        # Path to your SQL file, which includes creation of the table
        user_data = get_user_data_dir()
        sc_path = str(user_data / "ways" / "ways_public_2po_4pgr.sql")
        file_size = os.path.getsize(sc_path)

        # We read 10% at a time.  (Or pick a chunk size in bytes that works for your environment.)
        chunk_size = max(1, file_size // 100)
        chars_read = 0

        leftover = ""  # Holds any partial statement that didn't end with a semicolon

        print("\nStart inserting ways into public_2po_4pgr table.")
        with open(sc_path, 'r', encoding='utf-8') as sc_file:
            while True:
                # Read next chunk
                data = sc_file.read(chunk_size)
                if not data:
                    # No more data to read
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
        """This function transform the output of osm2po to the ways table, refer to the issue
        https://github.com/TongYe1997/Connector-syn-grid/issues/19"""

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
        """
        Loads and executes SQL function definitions into the database.

        The SQL files are grouped under two categories:
        1. Utility functions (e.g., spatial helpers, geometry splitting)
        2. Core functions (e.g., building-to-way connection logic, intersection segmentation)

        SQL files are loaded from:
            - src/ways_preprocessing/utils/
            - src/ways_preprocessing/core/
        """
        cur = self.dbc.conn.cursor()

        # Print once at the beginning
        print(f"Loading ways preprocessing functions into schema 'pylovo'.")

        # Get the path to the ways_preprocessing_functions directory within the package
        package_dir = Path(__file__).parent.parent  # Go up to pylovo package directory
        function_paths = [
            package_dir / "ways_preprocessing_functions" / "utils",
            package_dir / "ways_preprocessing_functions" / "core"
        ]

        try:
            for path in function_paths:
                filename = None  # Initialize to avoid potential reference before assignment

                for filename in sorted(os.listdir(path)):
                    if filename.endswith(".sql"):
                        full_file_path = path / filename
                        with open(full_file_path, 'r') as f:
                            sql = f.read()
                            cur.execute(sql)

            self.dbc.conn.commit()

        except Exception as e:
            error_msg = f"[ERROR] Failed while loading SQL functions"
            if filename:
                error_msg += f" from file '{filename}'"
            error_msg += f": {e}"
            print(error_msg)
            self.dbc.conn.rollback()
            raise

    def drop_all_tables(self):
        """
        Drops all tables in the database
        """
        cur = self.dbc.conn.cursor()
        cur.execute(f"DROP SCHEMA pylovo CASCADE")
        self.dbc.conn.commit()
