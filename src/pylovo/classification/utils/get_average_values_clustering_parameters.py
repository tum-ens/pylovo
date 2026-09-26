"""Average clustering parameters of k-means cluster 0 (the source of the lower filter thresholds).

Run as a script after a classification: ``python -m
pylovo.classification.utils.get_average_values_clustering_parameters``.
"""
import pandas as pd

from pylovo.config_loader import LIST_OF_CLUSTERING_PARAMETERS
from pylovo.database.database_client import DatabaseClient


def get_clustering_parameters_for_kmeans_cluster_0() -> pd.DataFrame:
    """Get clustering parameters for entries assigned to cluster 0 in transformer_classified.

    Allocation of buildings to a transformer within predefined system boundaries (postcodes)
    can lead to isolated building clusters, depending on the greenfield or brownfield placement
    assumptions. As a consequence, some unrealistically small grids might be generated.

    Cluster 0 consists of filling grids that mainly arise due to these methodological
    limitations. To address this, additional filtering steps are applied in the clustering
    methodology.

    The current clustering algorithm is applied to grids within 100 postcodes. The selected
    clustering parameters are:

    - avg_trafo_dis
    - no_house_connections
    - vsw_per_branch
    - no_households

    The average values of these parameters for entries in Cluster 0 are the
    ``THRESHOLD_*`` values in ``config_clustering.yaml``:

    - avg_trafo_dis: 0.115
    - no_house_connections: 14.332
    - vsw_per_branch: 0.258
    - no_households: 35.316

    Returns:
        pd.DataFrame: Clustering parameters of all grids in k-means cluster 0 (all versions
        and classification ids).
    """
    query = """
        SELECT cp.*
        FROM pylovo.clustering_parameters cp
        JOIN (
            SELECT DISTINCT grid_result_id
            FROM pylovo.transformer_classified
            WHERE kmeans_clusters = 0
        ) tc
        ON cp.grid_result_id = tc.grid_result_id;
    """
    with DatabaseClient() as dbc:
        return pd.read_sql_query(query, con=dbc.sqla_engine)


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
    # Get all clustering parameters
    df_clustering_parameters = get_clustering_parameters_for_kmeans_cluster_0()

    # Calculate average values using LIST_OF_CLUSTERING_PARAMETERS
    averages = calculate_average_clustering_parameters(df_clustering_parameters, LIST_OF_CLUSTERING_PARAMETERS)

    # Print the average values
    print("Average Clustering Parameter Values:")
    for param, avg in averages.items():
        print(f"{param}: {avg}")


if __name__ == "__main__":
    main()
