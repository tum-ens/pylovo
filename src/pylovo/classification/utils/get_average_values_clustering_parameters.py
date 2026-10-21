"""Derive the lower filter thresholds from k-means cluster 0 of one classification.

Run it after a classification without additional filtering: ``python -m
pylovo.classification.utils.get_average_values_clustering_parameters [--classification-id N]``. It prints the
four ``THRESHOLD_*`` values of ``config_clustering.yaml`` that the additional filtering uses, and the means of every
k-means cluster, so you can check that cluster 0 holds the small filling grids.
"""
import argparse
import sys

import pandas as pd

from pylovo.classification.database_communication.database_communication import LOWER_FILTER_THRESHOLDS
from pylovo.config_loader import CLASSIFICATION_VERSION, LIST_OF_CLUSTERING_PARAMETERS
from pylovo.database.database_client import DatabaseClient


def get_clustering_parameters_for_kmeans_cluster_0(classification_id: int | None = None) -> pd.DataFrame:
    """Get the clustering parameters of the grids in k-means cluster 0 of one classification.

    Allocation of buildings to a transformer within predefined system boundaries (postcodes)
    can lead to isolated building clusters, depending on the greenfield or brownfield placement
    assumptions. As a consequence, some unrealistically small grids might be generated.

    Cluster 0 is the cluster whose representative grid has the fewest households. It is meant to hold
    these filling grids, which the additional filtering removes. The thresholds are the means of
    the four parameters of ``LOWER_FILTER_THRESHOLDS`` over cluster 0 of one classification that
    clustered with exactly these four parameters and without additional filtering:

    - avg_trafo_dis
    - no_house_connections
    - vsw_per_branch
    - no_households

    The shipped values (0.115, 14.332, 0.258 and 35.316) come from one such classification over 100
    postcodes on grids generated in May 2025 (the values ship since pylovo 0.3.0). The generator has changed
    since (transformer sizing, cable catalogue, connection aggregation), pylovo 1.0.0 counts parallel cables in
    ``vsw_per_branch`` and households in ``no_households``, and those grids are not stored, so a classification of
    today's grids gives other values. Check that cluster 0 holds the small grids before you use its means: on
    pylovo 1.0.0 grids of three Bavarian test regions, k-means with k = 4 put long rural grids (mean
    ``avg_trafo_dis`` 0.45 km) into cluster 0, whose means would filter 99 % of the grids (see the classification
    user guide). An earlier version of this helper averaged the parameters of
    ``LIST_OF_CLUSTERING_PARAMETERS`` (three in the shipped file, without ``no_households``) over
    cluster 0 of all classifications, which cannot reproduce them.

    Args:
        classification_id: The classification. ``None`` uses ``CLASSIFICATION_VERSION``.

    Returns:
        pd.DataFrame: Clustering parameters of the grids in k-means cluster 0 of that classification.
    """
    query = """
        SELECT cp.*
        FROM pylovo.clustering_parameters cp
        JOIN pylovo.transformer_classified tc ON tc.grid_result_id = cp.grid_result_id
        WHERE tc.classification_id = %(c)s AND tc.kmeans_clusters = 0;
    """
    if classification_id is None:
        classification_id = CLASSIFICATION_VERSION
    with DatabaseClient() as dbc:
        return pd.read_sql_query(query, con=dbc.sqla_engine, params={"c": classification_id})


def get_clustering_parameters_by_kmeans_cluster(classification_id: int | None = None) -> pd.DataFrame:
    """Get the clustering parameters of all grids of one classification with their k-means cluster.

    Args:
        classification_id: The classification. ``None`` uses ``CLASSIFICATION_VERSION``.

    Returns:
        pd.DataFrame: Clustering parameters with the k-means cluster in the column ``kmeans_clusters``.
    """
    query = """
        SELECT tc.kmeans_clusters, cp.*
        FROM pylovo.clustering_parameters cp
        JOIN pylovo.transformer_classified tc ON tc.grid_result_id = cp.grid_result_id
        WHERE tc.classification_id = %(c)s;
    """
    if classification_id is None:
        classification_id = CLASSIFICATION_VERSION
    with DatabaseClient() as dbc:
        return pd.read_sql_query(query, con=dbc.sqla_engine, params={"c": classification_id})


def cluster_means(df: pd.DataFrame, parameters: list) -> pd.DataFrame:
    """Number of grids and mean of each parameter per k-means cluster, rounded to 3 decimals.

    Args:
        df: Clustering parameters with the column ``kmeans_clusters``.
        parameters: Parameter names to average.

    Returns:
        pd.DataFrame: One row per cluster: ``grids`` and the means of ``parameters``.
    """
    clusters = df.groupby("kmeans_clusters")
    table = clusters[parameters].mean().round(3)
    table.insert(0, "grids", clusters.size())
    return table


def calculate_average_clustering_parameters(df: pd.DataFrame, parameters: list) -> dict:
    """Calculate the average values for the given clustering parameters.

    Args:
        df: DataFrame with clustering parameters.
        parameters: Parameter names to average.

    Returns:
        dict: Average value per parameter, rounded to 3 decimals.
    """
    avg_values = {}

    for field in parameters:
        avg = df[field].mean()
        avg_values[field] = round(avg, 3)  # Rounded to 3 decimals
    return avg_values


def main():
    parser = argparse.ArgumentParser(
        description="Print the lower filter thresholds as the means of k-means cluster 0 of one classification.")
    parser.add_argument("--classification-id", type=int, default=CLASSIFICATION_VERSION,
                        help="classification to read (default: CLASSIFICATION_VERSION of the configuration)")
    args = parser.parse_args()

    df_clustering_parameters = get_clustering_parameters_for_kmeans_cluster_0(args.classification_id)
    if df_clustering_parameters.empty:
        sys.exit(f"Classification {args.classification_id} has no grids in k-means cluster 0 "
                 "(unknown id, or not classified yet).")
    averages = calculate_average_clustering_parameters(df_clustering_parameters, list(LOWER_FILTER_THRESHOLDS))

    print(f"Means over {len(df_clustering_parameters)} grids of k-means cluster 0 of classification "
          f"{args.classification_id}:")
    for parameter, avg in averages.items():
        print(f"THRESHOLD_{parameter.upper()}: {avg}")
    clusters = get_clustering_parameters_by_kmeans_cluster(args.classification_id)
    if not clusters.empty:
        print("Means of every k-means cluster. Cluster 0 is not always the cluster of the small filling grids (short "
              "avg_trafo_dis); use the means of that cluster:")
        print(cluster_means(clusters, list(LOWER_FILTER_THRESHOLDS)).to_string())
    if set(LIST_OF_CLUSTERING_PARAMETERS) != set(LOWER_FILTER_THRESHOLDS):
        print("Note: cluster 0 depends on the parameters of the clustering. The shipped values come from a clustering "
              f"with {', '.join(LOWER_FILTER_THRESHOLDS)}, the configuration has {', '.join(LIST_OF_CLUSTERING_PARAMETERS)}.")


if __name__ == "__main__":
    main()
