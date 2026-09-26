"""Database access for the grid classification (sample set, clustering parameters, results)."""
import geopandas as gpd
import pandas as pd
from geoalchemy2 import Geometry, WKTElement
import pylovo.database.database_client as dbc

from pylovo.config_loader import (
    CLASSIFICATION_VERSION,
    CLUSTERING_PARAMETERS,
    LIST_OF_CLUSTERING_PARAMETERS,
    N_CLUSTERS_GMM,
    N_CLUSTERS_KMEANS,
    TARGET_EPSG,
    THRESHOLD_AVG_TRAFO_DIS,
    THRESHOLD_HOUSEHOLDS_PER_BUILDING,
    THRESHOLD_MAX_TRAFO_DIS,
    THRESHOLD_NO_HOUSE_CONNECTIONS,
    THRESHOLD_NO_HOUSEHOLDS,
    THRESHOLD_VSW_PER_BRANCH,
    VERSION_ID,
)
from pylovo.classification.clustering.clustering_algorithms import gmm_tied_clustering, kmeans_clustering


class DatabaseCommunication:
    """Database interface of the classification workflow.

    Wraps its own :class:`~pylovo.database.database_client.DatabaseClient`. Queries
    use ``VERSION_ID`` (grid generation) and ``CLASSIFICATION_VERSION`` from the
    configuration files.

    Note:
        The ``apply_*_threshold*`` methods and :meth:`set_remaining_filter_values_false`
        update ``pylovo.clustering_parameters`` for all grid versions, and a grid once
        marked as ``filtered`` stays filtered when thresholds change later.
    """

    def __init__(self, **kwargs):
        self.dbc = dbc.DatabaseClient()

        print("Database connection is constructed. ")

    def __del__(self):
        self.dbc.cur.close()
        self.dbc.conn.close()
        print("Database connection closed.")

    def get_clustering_parameters_for_classification_version(self) -> pd.DataFrame:
        """Return the unfiltered clustering parameters of all grids in the sample set.

        Only grids of ``VERSION_ID`` whose PLZ belong to the sample set of
        ``CLASSIFICATION_VERSION`` and with ``filtered = false`` are returned.

        Returns:
            pd.DataFrame: One row per grid with the ``CLUSTERING_PARAMETERS`` columns.
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
        """Return the clustering parameters of the sample set joined with municipal register data.

        Same grid selection as :meth:`get_clustering_parameters_for_classification_version`,
        plus population, area, coordinates, AGS, city name and RegioStaR classes of the PLZ.

        Returns:
            pd.DataFrame: One row per grid.
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
        """Wrap a shapely geometry as ``WKTElement`` in ``TARGET_EPSG`` for writing with SQLAlchemy."""
        return WKTElement(geom.wkt, srid=TARGET_EPSG)

    def save_transformers_with_classification_info(
        self,
        list_of_clustering_parameters: list | None = None,
        n_clusters_kmeans: int | None = None,
        n_clusters_gmm: int | None = None,
    ) -> None:
        """Cluster the sample grids and append the result to ``pylovo.transformer_classified``.

        Runs k-means and tied GMM and stores, per grid, the transformer position,
        the cluster of each algorithm and whether the grid is the representative
        of its cluster. The KMedoids columns stay empty. The defaults below are the
        values of ``config_clustering.yaml`` at import time.

        Args:
            list_of_clustering_parameters: Columns used for clustering. Defaults to
                ``LIST_OF_CLUSTERING_PARAMETERS``.
            n_clusters_kmeans: Number of k-means clusters. Defaults to ``N_CLUSTERS_KMEANS``.
            n_clusters_gmm: Number of GMM components. Defaults to ``N_CLUSTERS_GMM``.
        """
        if list_of_clustering_parameters is None:
            list_of_clustering_parameters = LIST_OF_CLUSTERING_PARAMETERS
        if n_clusters_kmeans is None:
            n_clusters_kmeans = N_CLUSTERS_KMEANS
        if n_clusters_gmm is None:
            n_clusters_gmm = N_CLUSTERS_GMM

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

        # calculate the clusters (KMedoids is disabled, see clustering_algorithms)
        # KMEANS
        df_parameters_of_grids, representative_networks_kmeans = kmeans_clustering(
            df_parameters_of_grids=df_parameters_of_grids,
            list_of_clustering_parameters=list_of_clustering_parameters,
            n_clusters=n_clusters_kmeans)
        df_parameters_of_grids.rename(mapper={'clusters': 'kmeans_clusters'}, axis=1, inplace=True)
        df_parameters_of_grids['kmeans_representative_grid'] = False
        for i in list(representative_networks_kmeans['index']):
            df_parameters_of_grids.at[i, 'kmeans_representative_grid'] = True
        df_parameters_of_grids['kmeans_clusters'] = df_parameters_of_grids[
            'kmeans_clusters'].astype('int')

        # GMM TIED
        df_parameters_of_grids, representative_networks_gmm = gmm_tied_clustering(
            df_parameters_of_grids=df_parameters_of_grids,
            list_of_clustering_parameters=list_of_clustering_parameters,
            n_clusters=n_clusters_gmm)
        df_parameters_of_grids.rename(mapper={'clusters': 'gmm_clusters'}, axis=1, inplace=True)
        df_parameters_of_grids['gmm_representative_grid'] = False
        for i in list(representative_networks_gmm['index']):
            df_parameters_of_grids.at[i, 'gmm_representative_grid'] = True
        df_parameters_of_grids['gmm_clusters'] = df_parameters_of_grids[
            'gmm_clusters'].astype('int')

        # KMedoids is disabled: keep the table columns, but leave them empty
        df_parameters_of_grids['kmedoid_clusters'] = pd.NA
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
        """Mark grids with ``max_trafo_dis > THRESHOLD_MAX_TRAFO_DIS`` as filtered."""
        query = """UPDATE pylovo.clustering_parameters
                SET filtered = true
                WHERE max_trafo_dis > %(t)s;"""
        self.dbc.cur.execute(query, {"t": THRESHOLD_MAX_TRAFO_DIS})
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()

    def apply_households_per_building_threshold(self) -> None:
        """Mark grids with a building of more than ``THRESHOLD_HOUSEHOLDS_PER_BUILDING`` households as filtered."""
        query = """WITH buildings(grid_result_id) AS (
                       SELECT DISTINCT grid_result_id
                       FROM pylovo.buildings_result
                       WHERE households > %(h)s
                   )
                   
                   UPDATE pylovo.clustering_parameters c
                   SET filtered = true
                   FROM buildings b
                   WHERE c.grid_result_id = b.grid_result_id;"""
        self.dbc.cur.execute(query, {"h": THRESHOLD_HOUSEHOLDS_PER_BUILDING})
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()
    
    def apply_list_of_clustering_parameters_thresholds(self) -> None:
        """Mark grids as filtered if any of the four parameters is below its threshold.

        Parameters and thresholds: ``avg_trafo_dis`` (``THRESHOLD_AVG_TRAFO_DIS``),
        ``no_house_connections`` (``THRESHOLD_NO_HOUSE_CONNECTIONS``), ``vsw_per_branch``
        (``THRESHOLD_VSW_PER_BRANCH``) and ``no_households`` (``THRESHOLD_NO_HOUSEHOLDS``).
        This removes the small "filling" grids of k-means cluster 0, see
        ``classification/utils/get_average_values_clustering_parameters.py``.
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
        """Set ``filtered = false`` for all grids that no threshold has marked yet."""
        query = """UPDATE pylovo.clustering_parameters 
            SET filtered = false
            WHERE filtered IS NULL;"""
        self.dbc.cur.execute(query)
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()
