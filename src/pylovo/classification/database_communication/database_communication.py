import geopandas as gpd
import pandas as pd
from geoalchemy2 import Geometry, WKTElement
import pylovo.database.database_client as dbc

from pylovo.config_loader import *
from pylovo.classification.clustering.clustering_algorithms import gmm_tied_clustering, kmeans_clustering#, kmedoids_clustering


class DatabaseCommunication:
    """
    This class is the interface with the database. Functions communicating with the database
    are listed under this class.
    """

    def __init__(self, **kwargs):
        self.dbc = dbc.DatabaseClient()

        print("Database connection is constructed. ")

    def __del__(self):
        self.dbc.cur.close()
        self.dbc.conn.close()
        print("Database connection closed.")

    def get_clustering_parameters_for_classification_version(self) -> pd.DataFrame:
        """get clustering parameter for a specific classification version indicated in config classification

        :return: a table with all grid parameters for all grids for PLZ included in the classification version
        :rtype: pd.DataFrame
        """
        query = """
                WITH plz_table(plz) AS (
                    SELECT plz
                    FROM pylovo.sample_set
                    WHERE classification_id= %(c)s
                ),
                clustering AS (
                    SELECT version_id, plz, kcid, bcid, cp.*
                    FROM pylovo.clustering_parameters cp 
                    JOIN pylovo.grid_result gr ON cp.grid_result_id = gr.grid_result_id
                    WHERE gr.version_id = %(v)s AND cp.filtered = false
                )
                SELECT c.* 
                FROM clustering c
                JOIN plz_table p
                ON c.plz = p.plz;"""
        params = {"v": VERSION_ID, "c": CLASSIFICATION_VERSION}
        df_query = pd.read_sql_query(query, con=self.dbc.conn, params=params, )
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
                    SELECT ss.plz, mr.pop, mr.area, mr.lat, mr.lon, ss.ags, mr.name_city, mr.regio7, mr.regio5, mr.pop_den
                    FROM pylovo.sample_set ss
                    JOIN pylovo.municipal_register mr ON ss.plz = mr.plz AND ss.ags = mr.ags
                    WHERE ss.classification_id = %(c)s
                ),
                clustering AS (
                    SELECT version_id, plz, kcid, bcid, cp.*
                    FROM pylovo.clustering_parameters cp 
                    JOIN pylovo.grid_result gr ON cp.grid_result_id = gr.grid_result_id
                    WHERE gr.version_id = %(v)s AND cp.filtered = false
                )
                SELECT c.*, p.pop, p.area, p.lat, p.lon, p.ags, p.name_city, p.regio7, p.regio5, p.pop_den
                FROM clustering c
                JOIN plz_table p
                ON c.plz = p.plz;"""
        params = {"v": VERSION_ID, "c": CLASSIFICATION_VERSION}
        df_query = pd.read_sql_query(query, con=self.dbc.conn, params=params, )
        return df_query

    def create_wkt_element(self, geom):
        """transform geometry entry so that it can be imported to database"""
        return WKTElement(geom.wkt, srid=TARGET_EPSG)

    def save_transformers_with_classification_info(self) -> None:
        """write clusters of algorithms kmedoid, kmeans, gmm tied to database table transformer classified,
        set clustering parameters in config_clustering"""
        # retrieve clustering parameters
        df_parameters_of_grids = self.get_clustering_parameters_for_classification_version()

        # load transformer positions from database, preserve geo-datatype of geom column
        query = """
                SELECT gr.version_id, gr.plz, gr.kcid, gr.bcid, tp.geom
                FROM pylovo.transformer_positions tp
                JOIN pylovo.grid_result gr
                  ON tp.grid_result_id = gr.grid_result_id
                WHERE gr.version_id=%(v)s;"""
        params = {"v": VERSION_ID}
        df_transformer_positions = gpd.read_postgis(query, con=self.dbc.sqla_engine, params=params, )
        df_transformer_positions['geom'] = df_transformer_positions['geom'].apply(self.create_wkt_element)

        # calculate the clusters
        # # KMEDOIDS
        # df_parameters_of_grids, representative_networks_kmedoid = kmedoids_clustering(
        #     df_parameters_of_grids=df_parameters_of_grids,
        #     list_of_clustering_parameters=LIST_OF_CLUSTERING_PARAMETERS,
        #     n_clusters=N_CLUSTERS_KMEDOID)
        # df_parameters_of_grids.rename(mapper={'clusters': 'kmedoid_clusters'}, axis=1, inplace=True)
        # df_parameters_of_grids['kmedoid_representative_grid'] = False
        # for i in list(representative_networks_kmedoid['index']):
        #     df_parameters_of_grids.at[i, 'kmedoid_representative_grid'] = True
        # df_parameters_of_grids['kmedoid_clusters'] = df_parameters_of_grids[
        #     'kmedoid_clusters'].astype('int')

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

        if 'kmedoid_clusters' not in df_parameters_of_grids.columns:
            df_parameters_of_grids['kmedoid_clusters'] = pd.NA
        if 'kmedoid_representative_grid' not in df_parameters_of_grids.columns:
            df_parameters_of_grids['kmedoid_representative_grid'] = False

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
        
        query = """
                SELECT grid_result_id, version_id, plz, kcid, bcid
                FROM pylovo.grid_result
                WHERE version_id=%(v)s;"""
        params = {"v": VERSION_ID}
        df_grid_result = pd.read_sql_query(query, con=self.dbc.sqla_engine, params=params)

        df_transformers_classified  = pd.merge(df_grid_result, df_transformers_classified, how='right',
                                               left_on=['version_id', 'plz', 'kcid', 'bcid'],
                                               right_on=['version_id', 'plz', 'kcid', 'bcid'])

        df_transformers_classified.drop(columns=['version_id', 'plz', 'kcid', 'bcid'], inplace=True)

        # add classification id
        df_transformers_classified['classification_id'] = CLASSIFICATION_VERSION
        # write transformer data with cluster info to database
        df_transformers_classified.to_sql(name='transformer_classified', con=self.dbc.sqla_engine,
                                          if_exists='append',
                                          index=False, dtype={'geom': Geometry(geometry_type='POINT', srid=TARGET_EPSG)})
        self.dbc.refresh_materialized_views()
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()

    def apply_max_trafo_dis_threshold(self) -> None:
        """apply maximum transformer distance threshold on clustering parameter table
        by indicating if the threshold is surpassed in the filtered column
        """
        query = """UPDATE pylovo.clustering_parameters
                SET filtered = true
                WHERE max_trafo_dis > %(t)s;"""
        self.dbc.cur.execute(query, {"t": THRESHOLD_MAX_TRAFO_DIS})
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()

    def apply_households_per_building_threshold(self) -> None:
        """apply maximum households per building threshold on clustering parameter table
        by indicating if the threshold is surpassed in the filtered column
        """
        query = """WITH buildings(grid_result_id) AS (
                       SELECT DISTINCT grid_result_id
                       FROM pylovo.buildings_result
                       WHERE households_per_building > %(h)s
                   )
                   
                   UPDATE pylovo.clustering_parameters c
                   SET filtered = true
                   FROM buildings b
                   WHERE c.grid_result_id = b.grid_result_id;"""
        self.dbc.cur.execute(query, {"h": THRESHOLD_HOUSEHOLDS_PER_BUILDING})
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()
    
    def apply_list_of_clustering_parameters_thresholds(self) -> None:
        """
        Apply thresholds on selected clustering parameters.
        If a parameter less than its threshold, set filtered = true.
        """

        query = """
            UPDATE pylovo.clustering_parameters
            SET filtered = true
            WHERE avg_trafo_dis < %(avg_trafo_dis)s
            OR no_house_connections < %(no_house_connections)s
            OR vsw_per_branch < %(vsw_per_branch)s
            OR no_households < %(no_households)s;
        """

        params = {
            "avg_trafo_dis": THRESHOLD_AVG_TRAFO_DIS,
            "no_house_connections": THRESHOLD_NO_HOUSE_CONNECTIONS,
            "vsw_per_branch": THRESHOLD_VSW_PER_BRANCH,
            "no_households": THRESHOLD_NO_HOUSEHOLDS
        }

        self.dbc.cur.execute(query, params)
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()

    def set_remaining_filter_values_false(self) -> None:
        """setting filtered value to false for grids that should not be filtered according to their parameters
        """
        query = """UPDATE pylovo.clustering_parameters 
            SET filtered = false
            WHERE filtered IS NULL;"""
        self.dbc.cur.execute(query)
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()

    def get_ags_for_plz(df_plz: pd.DataFrame) -> pd.DataFrame:
        """get the AGS for the PLZ in a dataframe

        :param df_plz: table with plz column,
        :type df_plz: pd.DataFrame

        :return: table with plz and ags column
        :rtype: pd.DataFrame"""
