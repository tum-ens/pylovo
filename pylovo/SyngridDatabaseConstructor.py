import subprocess
import time
import warnings
from pathlib import Path

import pandas as pd
import psycopg2 as pg
import sqlparse
from sqlalchemy import create_engine

from pylovo.config_data import *
from pylovo.pgReaderWriter import PgReaderWriter

# uncomment for automated building import of buildings in regiostar_samples
# from raw_data.import_building_data import OGR_FILE_LIST



class SyngridDatabaseConstructor:
    """
    Constructs a ready to use pylovo database. Be careful about overwriting the tables.
    It uses pgReaderWriter to connect to the database and create tables and import data.
    """

    def __init__(self, pgr=None):
        if pgr:
            self.pgr = pgr 
        else:
            self.pgr = PgReaderWriter()

        with self.pgr.conn.cursor() as cur:
            # create extension if not exists for recognition of geom datatypes
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis;")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgRouting;")

    def get_table_name_list(self):
        with self.pgr.conn.cursor() as cur:
            cur.execute(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = 'public'"""
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

        if table_name == "all":
            try:
                with self.pgr.conn.cursor() as cur:
                    for table_name, query in CREATE_QUERIES.items():
                        cur.execute(query)
                        print(f"CREATE TABLE {table_name}")
                self.pgr.conn.commit()
            except (Exception, pg.DatabaseError) as error:
                raise error
        elif table_name in CREATE_QUERIES:
            try:
                with self.pgr.conn.cursor() as cur:
                    cur.execute(CREATE_QUERIES[table_name])
                    print(f"CREATE TABLE {table_name}")
                self.pgr.conn.commit()
            except (Exception, pg.DatabaseError) as error:
                raise error
        else:
            raise ValueError(
                f"Table name {table_name} is not a valid parameter value for the function create_table. See config.py"
            )

    def ogr_to_db(self, ogr_file_list):
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
            subprocess.run(
                [
                    "ogr2ogr",
                    "-append" if table_exists else "-overwrite",
                    "-progress",
                    "-f",
                    "PostgreSQL",
                    f"PG:dbname={DBNAME} user={USER} password={PASSWORD} host={HOST} port={PORT}",
                    file_path,
                    "-nln",
                    table_name,
                    "-nlt",
                    # "MULTIPOLYGON",
                    "PROMOTE_TO_MULTI",
                    "-t_srs",
                    "EPSG:3035",
                    "-lco",
                    "geometry_name=geom",
                ],
                shell=False            
            )
            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")

    def transformers_to_db(self, sgc):
        in_file = os.path.join(".", "raw_data", "transformer_data", "substations_bayern_processed.geojson")
        out_file = os.path.join(".", "raw_data", "transformer_data", "substations_bayern_processed_3035.geojson")

        # Convert the GeoJSON file to EPSG:3035 and write to a new file
        subprocess.run(
            [
                "ogr2ogr",
                "-f", "GeoJSON",
                "-s_srs", "EPSG:32633",
                "-t_srs", "EPSG:3035",
                out_file,  # output
                in_file  # input
            ],
            shell=False
        )

        trafo_dict = [
            {
                "path": out_file,
                "table_name": "transformers"
            }
        ]
        sgc.ogr_to_db(trafo_dict)

    def csv_to_db(self):

        for file_dict in CSV_FILE_LIST:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists(), file_path
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            if self.table_exists(table_name=table_name):
                with self.pgr.conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table_name}")
                    self.pgr.conn.commit()
            # read and write
            df = pd.read_csv(file_path, index_col=False)
            df.to_sql(
                name=table_name,
                con=self.pgr.sqla_engine,
                if_exists="append",
                index=False,
                schema="public",
            )

            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")


    def create_public_2po_table(self):
        """
        Reads the large SQL file in 10% chunks, executes complete statements on-the-fly,
        and defers incomplete statements until the next chunk.
        """
        cur = self.pgr.conn.cursor()

        # Path to your SQL file
        sc_path = os.path.join(os.getcwd(), "raw_data", "ways", "ways_public_2po_4pgr.sql")
        file_size = os.path.getsize(sc_path)

        # We read 10% at a time.  (Or pick a chunk size in bytes that works for your environment.)
        chunk_size = max(1, file_size // 100)

        leftover = ""  # Holds any partial statement that didn't end with a semicolon

        with open(sc_path, 'r', encoding='utf-8') as sc_file:
            while True:
                # Read next chunk
                data = sc_file.read(chunk_size)
                if not data:
                    # No more data to read
                    break

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
                            self.pgr.conn.commit()

                    # Check if the last statement ends with a semicolon or not
                    last_stmt = statements[-1].strip()
                    if last_stmt.endswith(';'):
                        # It's a complete statement
                        cur.execute(last_stmt)
                        self.pgr.conn.commit()
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
                            self.pgr.conn.commit()
                            leftover = ""
                        else:
                            # It's incomplete, keep it
                            leftover = stmt
                    else:
                        # No statements found. This can happen if combined was empty or whitespace.
                        # Just continue reading next chunk
                        pass
        print("Inserted all ways into ways_public_2po_4pgr table.")

    def ways_to_db(self):
        """This function transform the output of osm2po to the ways table, refer to the issue
        https://github.com/TongYe1997/Connector-syn-grid/issues/19"""

        st = time.time()

        # Transform to ways table
        query = """INSERT INTO ways
            SELECT  clazz,
                    source,
                    target,
                    cost,
                    reverse_cost,
                    ST_Transform(geom_way, 3035) as geom,
                    id
            FROM public_2po_4pgr"""
        cur = self.pgr.conn.cursor()
        cur.execute(query)
        self.pgr.conn.commit()

        et = time.time()
        print(f"Ways are successfully imported to db in {int(et - st)} s")

    def dump_functions(self):
        """
        Creates the SQL functions that are needed for the app to operate
        """
        cur = self.pgr.conn.cursor()
        sc_path = os.path.join(os.getcwd(), "pylovo", "dump_functions.sql")
        with open(sc_path, 'r') as sc_file:
            print("Executing dump_functions.sql script.")
            cur.execute(sc_file.read())
            self.pgr.conn.commit()
