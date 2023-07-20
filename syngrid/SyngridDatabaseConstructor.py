import warnings
import pandas as pd
import psycopg2 as pg
import time
from pathlib import Path
import subprocess
from sqlalchemy import create_engine

from syngrid.config_data import *
# from raw_data.import_building_data import OGR_FILE_LIST
# uncomment for automated building import of buildings in regiostar_samples


class SyngridDatabaseConstructor:
    """
    Constructs a ready to use syngrid database. Be careful about overwriting the tables.
    """

    def __init__(self):
        try:
            self.sqlalchemy_engine = create_engine(
                f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{DBNAME}"
            )
            self.con = pg.connect(
                dbname=DBNAME, user=USER, password=PASSWORD, host=HOST, port=PORT
            )
            print(f"Connection to {DBNAME} is established.")
        except pg.OperationalError as err:
            print(
                f"Connecting to {DBNAME} was not successful. Make sure, that you have established the SSH "
                f"connection with correct port mapping."
            )
            raise err

    def get_table_name_list(self):
        with self.con.cursor() as cur:
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
                with self.con.cursor() as cur:
                    for table_name, query in CREATE_QUERIES.items():
                        cur.execute(query)
                        print(f"CREATE TABLE {table_name}")
                self.con.commit()
            except (Exception, pg.DatabaseError) as error:
                raise error
        elif table_name in CREATE_QUERIES:
            try:
                with self.con.cursor() as cur:
                    cur.execute(CREATE_QUERIES[table_name])
                    print(f"CREATE TABLE {table_name}")
                self.con.commit()
            except (Exception, pg.DatabaseError) as error:
                raise error
        else:
            raise ValueError(
                f"Table name {table_name} is not a valid parameter value for the function create_table. See config.py"
            )

    def ogr_to_db(self, ogr_file_list):
        """
            OGR/GDAL is a translator library for raster and vector geospatial data formats
            inserts building data specified in OGR_FILE_LIST into syngrid_db
        """


        for file_dict in ogr_file_list:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists()
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            table_exists = self.table_exists(table_name=table_name)

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
                shell=True
            )
            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et-st)} s")

    def csv_to_db(self):

        for file_dict in CSV_FILE_LIST:
            st = time.time()
            file_path = Path(file_dict["path"])
            assert file_path.exists()
            file_name = file_path.stem
            table_name = file_dict.get("table_name", file_name)

            if self.table_exists(table_name=table_name):
                with self.con.cursor() as cur:
                    cur.execute(f"DELETE FROM {table_name}")
                    self.con.commit()
            # read and write
            df = pd.read_csv(file_path, index_col=False)
            df.to_sql(
                name=table_name,
                con=self.sqlalchemy_engine,
                if_exists="append",
                index=False,
                schema="public",
            )

            et = time.time()
            print(f"{file_name} is successfully imported to db in {int(et - st)} s")

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
        cur = self.con.cursor()
        cur.execute(query)
        self.con.commit()

        et = time.time()
        print(f"Ways are successfully imported to db in {int(et - st)} s")
