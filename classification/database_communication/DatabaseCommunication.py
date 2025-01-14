import geopandas as gpd
import pandas as pd
import psycopg2 as pg
from geoalchemy2 import Geometry, WKTElement
from sqlalchemy import create_engine

from classification.clustering.clustering_algorithms import *
from classification.clustering.config_clustering import *
from classification.config_classification import *
from pylovo.config_data import *
from pylovo.config_version import *


class DatabaseCommunication:
    """
    This class is the interface with the database. Functions communicating with the database
    are listed under this class.
    """

    def __init__(self, **kwargs):
        self.conn = pg.connect(
            database=DBNAME, user=USER, password=PASSWORD, host=HOST, port=PORT
        )
        self.cur = self.conn.cursor()
        self.db_path = f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{DBNAME}"
        self.sqla_engine = create_engine(self.db_path)

        print("Database connection is constructed. ")

    def __del__(self):
        self.cur.close()
        self.conn.close()
        print("Database connection closed.")

    def get_clustering_parameters_for_plz(self, plz: str) -> pd.DataFrame:
        """get clustering parameter for a specific classification version indicated in config classification

        :return: a table with all grid parameters for all grids for PLZ included in the classification version
        :rtype: pd.DataFrame
        """
        query = """
                SELECT * 
                FROM public.clustering_parameters 
                WHERE version_id = %(v)s AND plz = %(p)s;"""
        params = {"v": VERSION_ID, "p": plz}
        df_query = pd.read_sql_query(query, con=self.conn, params=params, )
        columns = CLUSTERING_PARAMETERS
        df_parameter = pd.DataFrame(df_query, columns=columns)
        return df_parameter

    def get_clustering_parameters_for_classification_version(self) -> pd.DataFrame:
        """get clustering parameter for a specific classification version indicated in config classification

        :return: a table with all grid parameters for all grids for PLZ included in the classification version
        :rtype: pd.DataFrame
        """
        query = """
                WITH plz_table(plz) AS (
                    SELECT plz
                    FROM public.sample_set
                    WHERE classification_id= %(c)s
                    ),
                clustering AS(
                    SELECT * 
                    FROM public.clustering_parameters 
                    WHERE version_id = %(v)s AND filtered = false
                    )
                SELECT c.* 
                FROM clustering c
                JOIN plz_table p
                ON c.plz = p.plz;"""
        params = {"v": VERSION_ID, "c": str(CLASSIFICATION_VERSION)}
        df_query = pd.read_sql_query(query, con=self.conn, params=params, )
        columns = CLUSTERING_PARAMETERS
        df_parameter = pd.DataFrame(df_query, columns=columns)
        return df_parameter

    def municipal_register_with_clustering_parameters_for_classification_version(self) -> pd.DataFrame:
        """get full information about a samples set indicated by a classification version
        Information about:
        - clustering parameter
        - regiostar data
        - population, area, population density

        ...
        :return: a table with all grid parameters for all grids for PLZ included in the classification version
        :rtype: pd.DataFrame
        """
        query = """
                WITH plz_table(plz) AS (
                    plz, pop, area, lat, lon, ags, name, regio7, regio5, pop_den
                    FROM public.sample_set
                    WHERE classification_id= %(c)s
                    ),
                clustering AS(
                    SELECT * 
                    FROM public.clustering_parameters 
                    WHERE version_id = %(v)s AND filtered = false
                    )
                SELECT c.*, p.pop, p.area, p.lat, p.lon, p.ags, p.name, p.regio7, p.regio5, p.pop_den
                FROM clustering c
                JOIN plz_table p
                ON c.plz = p.plz;"""
        params = {"v": VERSION_ID, "c": str(CLASSIFICATION_VERSION)}
        df_query = pd.read_sql_query(query, con=self.conn, params=params, )
        return df_query

    def create_wkt_element(self, geom):
        """transform geometry entry so that it can be imported to database"""
        return WKTElement(geom.wkt, srid=3035)

    def save_transformers_with_classification_info(self) -> None:
        """write clusters of algorithms kmedoid, kmeans, gmm tied to database table transformer classified,
        set clustering parameters in config_clustering"""
        # retrieve clustering parameters
        df_parameters_of_grids = self.get_clustering_parameters_for_classification_version()

        # load transformer positions from database, preserve geo-datatype of geom column
        query = """
                SELECT version_id, plz, kcid, bcid, geom
                FROM public.transformer_positions
                WHERE version_id=%(v)s;"""
        params = {"v": VERSION_ID}
        df_transformer_positions = gpd.read_postgis(query, con=self.sqla_engine, params=params, )
        df_transformer_positions['geom'] = df_transformer_positions['geom'].apply(self.create_wkt_element)

        # calculate the clusters
        # KMEDOIDS
        df_parameters_of_grids, representative_networks_kmedoid = kmedoids_clustering(
            df_parameters_of_grids=df_parameters_of_grids,
            list_of_clustering_parameters=LIST_OF_CLUSTERING_PARAMETERS,
            n_clusters=N_CLUSTERS_KMEDOID)
        df_parameters_of_grids.rename(mapper={'clusters': 'kmedoid_clusters'}, axis=1, inplace=True)
        df_parameters_of_grids['kmedoid_representative_grid'] = False
        for i in list(representative_networks_kmedoid['index']):
            df_parameters_of_grids.at[i, 'kmedoid_representative_grid'] = True
        df_parameters_of_grids['kmedoid_clusters'] = df_parameters_of_grids[
            'kmedoid_clusters'].astype('int')

        # KMEANS
        df_parameters_of_grids, representative_networks_kmeans = kmeans_clustering(
            df_parameters_of_grids=df_parameters_of_grids,
            list_of_clustering_parameters=LIST_OF_CLUSTERING_PARAMETERS,
            n_clusters=N_CLUSTERS_KMEANS)
        df_parameters_of_grids.rename(mapper={'clusters': 'kmeans_clusters'}, axis=1, inplace=True)
        df_parameters_of_grids['kmeans_representative_grid'] = False
        for i in list(representative_networks_kmeans['index']):
            df_parameters_of_grids.at[i, 'kmeans_representative_grid'] = True
        df_parameters_of_grids['kmeans_clusters'] = df_parameters_of_grids[
            'kmeans_clusters'].astype('int')

        # GMM TIED
        df_parameters_of_grids, representative_networks_gmm = gmm_tied_clustering(
            df_parameters_of_grids=df_parameters_of_grids,
            list_of_clustering_parameters=LIST_OF_CLUSTERING_PARAMETERS,
            n_clusters=N_CLUSTERS_GMM)
        df_parameters_of_grids.rename(mapper={'clusters': 'gmm_clusters'}, axis=1, inplace=True)
        df_parameters_of_grids['gmm_representative_grid'] = False
        for i in list(representative_networks_gmm['index']):
            df_parameters_of_grids.at[i, 'gmm_representative_grid'] = True
        df_parameters_of_grids['gmm_clusters'] = df_parameters_of_grids[
            'gmm_clusters'].astype('int')

        # reduce columns and convert datatypes
        df_parameters_of_grids = df_parameters_of_grids[['version_id', 'plz', 'kcid', 'bcid',
                                                         'kmedoid_clusters', 'kmedoid_representative_grid',
                                                         'kmeans_clusters', 'kmeans_representative_grid',
                                                         'gmm_clusters', 'gmm_representative_grid']]
        df_parameters_of_grids['version_id'] = df_parameters_of_grids['version_id'].astype('string')
        df_parameters_of_grids['plz'] = df_parameters_of_grids['plz'].astype('int')

        # merge transformer positions with cluster information
        df_transformers_classified = pd.merge(df_transformer_positions, df_parameters_of_grids, how='right',
                                              left_on=['version_id', 'plz', 'kcid', 'bcid'],
                                              right_on=['version_id', 'plz', 'kcid', 'bcid'])
        df_transformers_classified.drop(columns=['plz', 'kcid', 'bcid'], inplace=True)

        # add classification id
        df_transformers_classified['classification_id'] = CLASSIFICATION_VERSION
        # write transformer data with cluster info to database
        df_transformers_classified.to_sql(name='transformer_classified', con=self.sqla_engine,
                                          if_exists='append',
                                          index=False, dtype={'geom': Geometry(geometry_type='POINT', srid=3035)})
        print(self.cur.statusmessage)
        self.conn.commit()

    def apply_max_trafo_dis_threshold(self) -> None:
        """apply maximum transformer distance threshold on clustering parameter table
        by indicating if the threshold is surpassed in the filtered column
        """
        query = """UPDATE public.clustering_parameters
                SET filtered = true
                WHERE max_trafo_dis > %(t)s;"""
        self.cur.execute(query, {"t": THRESHOLD_MAX_TRAFO_DIS})
        print(self.cur.statusmessage)
        self.conn.commit()

    def apply_households_per_building_threshold(self) -> None:
        """apply maximum households per building threshold on clustering parameter table
        by indicating if the threshold is surpassed in the filtered column
        """
        query = """WITH buildings(version_id, plz, bcid, kcid) AS (
                        SELECT version_id, plz, bcid, kcid
                        FROM public.buildings_result
                        WHERE houses_per_building > %(h)s)
                        
            UPDATE public.clustering_parameters c
            SET filtered = true
            FROM buildings b
            WHERE c.version_id = b.version_id AND 
                c.plz = b.plz AND 
                c.kcid = b.kcid AND
                c.bcid = b.bcid;"""
        self.cur.execute(query, {"h": THRESHOLD_HOUSEHOLDS_PER_BUILDING})
        print(self.cur.statusmessage)
        self.conn.commit()

    def set_remaining_filter_values_false(self) -> None:
        """setting filtered value to false for grids that should not be filtered according to their parameters
        """
        query = """UPDATE public.clustering_parameters 
            SET filtered = false
            WHERE filtered IS NULL;"""
        self.cur.execute(query)
        print(self.cur.statusmessage)
        self.conn.commit()

    def get_ags_for_plz(df_plz: pd.DataFrame) -> pd.DataFrame:
        """get the AGS for the PLZ in a dataframe

        :param df_plz: table with plz column,
        :type df_plz: pd.DataFrame

        :return: table with plz and ags column
        :rtype: pd.DataFrame"""
