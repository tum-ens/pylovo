"""Database access for the grid classification (sample set, clustering parameters, results)."""
import geopandas as gpd
import pandas as pd
from geoalchemy2 import Geometry, WKTElement
from sqlalchemy import text
import pylovo.database.database_client as dbc

from pylovo.config_loader import (
    ADDITIONAL_FILTERING,
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

# Lower bounds of the additional filtering: clustering parameter -> threshold of config_clustering.yaml. A grid below
# one of them is a small "filling" grid, see classification/utils/get_average_values_clustering_parameters.py.
LOWER_FILTER_THRESHOLDS = {
    "avg_trafo_dis": THRESHOLD_AVG_TRAFO_DIS,
    "no_house_connections": THRESHOLD_NO_HOUSE_CONNECTIONS,
    "vsw_per_branch": THRESHOLD_VSW_PER_BRANCH,
    "no_households": THRESHOLD_NO_HOUSEHOLDS,
}

# Columns of ``classification_version`` that store the choices of step 7, in the order of ``config_clustering.yaml``.
CLASSIFICATION_CHOICES = ("list_of_clustering_parameters", "n_clusters_kmeans", "n_clusters_gmm", "additional_filtering")

# Values of the placeholders in the conditions of ``filter_reasons``.
FILTER_PARAMETERS = {
    "max_trafo_dis": THRESHOLD_MAX_TRAFO_DIS,
    "households_per_building": THRESHOLD_HOUSEHOLDS_PER_BUILDING,
    **LOWER_FILTER_THRESHOLDS,
}


def filter_reasons(additional_filtering: bool | None = None) -> dict[str, str]:
    """Return the filters that apply as ``label -> SQL condition`` on ``pylovo.clustering_parameters`` (alias ``cp``).

    A grid is left out of the clustering if one condition holds. A missing value never filters. The two upper
    limits always apply, the lower bounds only with additional filtering. The placeholders take their values from
    ``FILTER_PARAMETERS``.

    Args:
        additional_filtering: Also apply the four lower bounds. ``None`` uses ``ADDITIONAL_FILTERING``.
    """
    if additional_filtering is None:
        additional_filtering = ADDITIONAL_FILTERING
    reasons = {
        f"max_trafo_dis > {THRESHOLD_MAX_TRAFO_DIS}": "cp.max_trafo_dis > %(max_trafo_dis)s",
        f"a building with more than {THRESHOLD_HOUSEHOLDS_PER_BUILDING} households": (
            "EXISTS (SELECT 1 FROM pylovo.buildings_result br WHERE br.grid_result_id = cp.grid_result_id "
            "AND br.households > %(households_per_building)s)"),
    }
    if additional_filtering:
        for parameter, threshold in LOWER_FILTER_THRESHOLDS.items():
            reasons[f"{parameter} < {threshold}"] = f"cp.{parameter} < %({parameter})s"
    return reasons


def _keep_condition(additional_filtering: bool | None) -> str:
    """Return the SQL condition that keeps the grids which no filter matches."""
    return " AND ".join(f"NOT COALESCE(({condition}), false)"
                        for condition in filter_reasons(additional_filtering).values())


class DatabaseCommunication:
    """Database interface of the classification workflow.

    Wraps its own :class:`~pylovo.database.database_client.DatabaseClient`. Queries
    use ``VERSION_ID`` (grid generation) and ``CLASSIFICATION_VERSION`` from the
    configuration files.

    Note:
        The grids that enter the clustering are selected when they are read: the readers apply the filters
        of :func:`filter_reasons` to the key figures of the sampled grids of ``VERSION_ID``. Nothing is
        stored per grid, so classifications that share grids do not influence each other. The column
        ``clustering_parameters.filtered`` is no longer used. Step 7 stores the parameters, the numbers of
        clusters and the filter choice with the classification, and ``None`` arguments default to them
        (:meth:`get_classification_choices`).
    """

    def __init__(self):
        self.dbc = dbc.DatabaseClient()

        print("Database connection is constructed. ")

    def __del__(self):
        self.dbc.cur.close()
        self.dbc.conn.close()
        print("Database connection closed.")

    def get_classification_choices(self) -> dict:
        """Return the choices that ``CLASSIFICATION_VERSION`` was clustered with.

        Step 7 stores them in ``classification_version``. A classification without stored choices (clustered
        before migration ``0014_classification_choices``, or not clustered yet) gets the values of
        ``config_clustering.yaml``, which hold the choices of the last ``pylovo-classify`` run.

        Returns:
            dict: ``list_of_clustering_parameters``, ``n_clusters_kmeans``, ``n_clusters_gmm`` and
            ``additional_filtering``.
        """
        self.dbc.cur.execute(f"SELECT {', '.join(CLASSIFICATION_CHOICES)} FROM pylovo.classification_version "
                             "WHERE classification_id = %(c)s", {"c": CLASSIFICATION_VERSION})
        stored = self.dbc.cur.fetchone() or (None,) * len(CLASSIFICATION_CHOICES)
        configured = (LIST_OF_CLUSTERING_PARAMETERS, N_CLUSTERS_KMEANS, N_CLUSTERS_GMM, ADDITIONAL_FILTERING)
        return {name: default if value is None else value
                for name, value, default in zip(CLASSIFICATION_CHOICES, stored, configured)}

    def _additional_filtering(self, additional_filtering: bool | None) -> bool:
        """Return ``additional_filtering``, ``None`` replaced by the choice of the classification."""
        if additional_filtering is None:
            return self.get_classification_choices()["additional_filtering"]
        return additional_filtering

    def get_clustering_parameters_for_classification_version(self, additional_filtering: bool | None = None) -> pd.DataFrame:
        """Return the unfiltered clustering parameters of all grids in the sample set.

        Only grids of ``VERSION_ID`` whose PLZ belong to the sample set of
        ``CLASSIFICATION_VERSION`` are read, and the grids that a filter matches are left out
        (:func:`filter_reasons`).

        Args:
            additional_filtering: Also leave out the grids below the four lower thresholds.
                ``None`` uses the choice of the classification (:meth:`get_classification_choices`).

        Returns:
            pd.DataFrame: One row per grid with the ``CLUSTERING_PARAMETERS`` columns, ordered by
            ``plz``, ``kcid`` and ``bcid``: the clusters depend on the row order, so it must not be
            left to the database.
        """
        query = f"""
                WITH plz_table(plz) AS (
                    SELECT plz
                    FROM pylovo.sample_set
                    WHERE classification_id= %(c)s
                ),
                clustering AS (
                    SELECT version_id, plz, kcid, bcid, cp.*
                    FROM pylovo.clustering_parameters cp 
                    JOIN pylovo.grid_result gr ON cp.grid_result_id = gr.grid_result_id
                    WHERE gr.version_id = %(v)s AND {_keep_condition(self._additional_filtering(additional_filtering))}
                )
                SELECT c.* 
                FROM clustering c
                JOIN plz_table p
                ON c.plz = p.plz
                ORDER BY c.plz, c.kcid, c.bcid;"""
        params = {"v": VERSION_ID, "c": CLASSIFICATION_VERSION, **FILTER_PARAMETERS}
        df_query = pd.read_sql_query(query, con=self.dbc.conn, params=params, )
        columns = CLUSTERING_PARAMETERS
        df_parameter = pd.DataFrame(df_query, columns=columns)
        return df_parameter

    def municipal_register_with_clustering_parameters_for_classification_version(
            self, additional_filtering: bool | None = None) -> pd.DataFrame:
        """Return the clustering parameters of the sample set joined with municipal register data.

        Same grid selection and order as :meth:`get_clustering_parameters_for_classification_version`,
        plus population, area, coordinates, AGS, city name and RegioStaR classes of the PLZ.

        Args:
            additional_filtering: Also leave out the grids below the four lower thresholds.
                ``None`` uses the choice of the classification (:meth:`get_classification_choices`).

        Returns:
            pd.DataFrame: One row per grid.
        """
        query = f"""
                WITH plz_table AS (
                    SELECT ss.plz, mr.pop, mr.area, mr.lat, mr.lon, ss.ags, mr.name_city, mr.regio7, mr.regio5, mr.pop_den
                    FROM pylovo.sample_set ss
                    JOIN pylovo.municipal_register mr ON ss.plz = mr.plz AND ss.ags = mr.ags
                    WHERE ss.classification_id = %(c)s
                ),
                clustering AS (
                    SELECT version_id, plz, kcid, bcid, cp.*
                    FROM pylovo.clustering_parameters cp 
                    JOIN pylovo.grid_result gr ON cp.grid_result_id = gr.grid_result_id
                    WHERE gr.version_id = %(v)s AND {_keep_condition(self._additional_filtering(additional_filtering))}
                )
                SELECT c.*, p.pop, p.area, p.lat, p.lon, p.ags, p.name_city, p.regio7, p.regio5, p.pop_den
                FROM clustering c
                JOIN plz_table p
                ON c.plz = p.plz
                ORDER BY c.plz, c.kcid, c.bcid;"""
        params = {"v": VERSION_ID, "c": CLASSIFICATION_VERSION, **FILTER_PARAMETERS}
        df_query = pd.read_sql_query(query, con=self.dbc.conn, params=params, )
        return df_query

    def get_grid_status_of_sample_set(self, additional_filtering: bool | None = None) -> pd.DataFrame:
        """Return what became of every grid of the sampled PLZ, for the summary of a classification run.

        One row per grid of ``VERSION_ID`` in a PLZ of the sample set, or one row with an empty
        ``grid_result_id`` for a sampled PLZ without grids. Columns: ``plz``, ``grid_result_id``,
        ``plz_analysed`` (the PLZ has a ``plz_parameters`` row), ``analysed`` (the grid has key figures),
        ``clustered`` (the grid is in ``transformer_classified`` of ``CLASSIFICATION_VERSION``) and one
        boolean column per filter of :func:`filter_reasons`, named by its label.

        Args:
            additional_filtering: Also check the four lower thresholds. ``None`` uses the choice of the
                classification (:meth:`get_classification_choices`).

        Returns:
            pd.DataFrame: The status, ordered by ``plz``, ``kcid`` and ``bcid``.
        """
        reasons = filter_reasons(self._additional_filtering(additional_filtering))
        flags = "".join(f",\n                       COALESCE(({condition}), false) AS filter_{i}"
                        for i, condition in enumerate(reasons.values()))
        query = f"""
                SELECT ss.plz, gr.grid_result_id,
                       EXISTS (SELECT 1 FROM pylovo.plz_parameters pp
                               WHERE pp.version_id = %(v)s AND pp.plz = ss.plz) AS plz_analysed,
                       cp.grid_result_id IS NOT NULL AS analysed,
                       tc.grid_result_id IS NOT NULL AS clustered{flags}
                FROM pylovo.sample_set ss
                LEFT JOIN pylovo.grid_result gr ON gr.plz = ss.plz AND gr.version_id = %(v)s
                LEFT JOIN pylovo.clustering_parameters cp ON cp.grid_result_id = gr.grid_result_id
                LEFT JOIN pylovo.transformer_classified tc
                       ON tc.grid_result_id = gr.grid_result_id AND tc.classification_id = %(c)s
                WHERE ss.classification_id = %(c)s
                ORDER BY ss.plz, gr.kcid, gr.bcid;"""
        params = {"v": VERSION_ID, "c": CLASSIFICATION_VERSION, **FILTER_PARAMETERS}
        status = pd.read_sql_query(query, con=self.dbc.conn, params=params, )
        return status.rename(columns={f"filter_{i}": label for i, label in enumerate(reasons)})

    def create_wkt_element(self, geom):
        """Wrap a shapely geometry as ``WKTElement`` in ``TARGET_EPSG`` for writing with SQLAlchemy."""
        return WKTElement(geom.wkt, srid=TARGET_EPSG)

    def save_transformers_with_classification_info(
        self,
        list_of_clustering_parameters: list | None = None,
        n_clusters_kmeans: int | None = None,
        n_clusters_gmm: int | None = None,
        additional_filtering: bool | None = None,
    ) -> None:
        """Cluster the sample grids and write the result to ``pylovo.transformer_classified``.

        Runs k-means and tied GMM and stores, per grid, the transformer position,
        the cluster of each algorithm and whether the grid is the representative
        of its cluster. The KMedoids columns stay empty. The rows of
        ``CLASSIFICATION_VERSION`` are replaced in one transaction, so the step can be repeated
        and a failing write keeps the previous result. The same transaction stores the four
        values used in ``classification_version``. ``None`` uses the stored value of the
        classification (:meth:`get_classification_choices`, ``config_clustering.yaml`` if none is stored).

        Args:
            list_of_clustering_parameters: Columns used for clustering.
            n_clusters_kmeans: Number of k-means clusters.
            n_clusters_gmm: Number of GMM components.
            additional_filtering: Also leave out the grids below the four lower thresholds.
        """
        choices = self.get_classification_choices()
        if list_of_clustering_parameters is None:
            list_of_clustering_parameters = choices["list_of_clustering_parameters"]
        if n_clusters_kmeans is None:
            n_clusters_kmeans = choices["n_clusters_kmeans"]
        if n_clusters_gmm is None:
            n_clusters_gmm = choices["n_clusters_gmm"]
        if additional_filtering is None:
            additional_filtering = choices["additional_filtering"]

        # retrieve clustering parameters
        df_parameters_of_grids = self.get_clustering_parameters_for_classification_version(additional_filtering)

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
        # write transformer data with cluster info to database: replace this classification's rows in one transaction
        # and store the choices of the result with the classification
        with self.dbc.sqla_engine.begin() as connection:
            connection.execute(text("DELETE FROM pylovo.transformer_classified WHERE classification_id = :c"),
                               {"c": CLASSIFICATION_VERSION})
            df_transformers_classified.to_sql(name='transformer_classified', con=connection, if_exists='append',
                                              index=False,
                                              dtype={'geom': Geometry(geometry_type='POINT', srid=TARGET_EPSG)})
            connection.execute(
                text("UPDATE pylovo.classification_version SET "
                     + ", ".join(f"{name} = :{name}" for name in CLASSIFICATION_CHOICES)
                     + " WHERE classification_id = :c"),
                {"c": CLASSIFICATION_VERSION,
                 "list_of_clustering_parameters": [str(name) for name in list_of_clustering_parameters],
                 "n_clusters_kmeans": int(n_clusters_kmeans), "n_clusters_gmm": int(n_clusters_gmm),
                 "additional_filtering": bool(additional_filtering)})
        self.dbc.refresh_materialized_views()
        print(self.dbc.cur.statusmessage)
        self.dbc.conn.commit()
