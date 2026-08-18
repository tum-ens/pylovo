import os
import sys
import pandas as pd
import psycopg2 as psy

# Determine the project's root directory and add to Python's module search path
PROJECT_ROOT = os.path.abspath(os.path.join(os.getcwd(), "../../.."))
sys.path.append(PROJECT_ROOT)

from pylovo.config_loader import *


def get_clustering_parameters_for_kmeans_cluster_0() -> pd.DataFrame:
    """
    Get clustering parameters for entries assigned to cluster 0 in transformer_classified.

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

    The average values of these parameters for entries in Cluster 0 are:
        - avg_trafo_dis: 0.115
        - no_house_connections: 14.332
        - vsw_per_branch: 0.258
        - no_households: 35.316

    :return: A DataFrame with clustering parameters for cluster 0 entries.
    """
    # Connect to the database
    conn = psy.connect(
        database=DBNAME, user=DBUSER, password=PASSWORD, host=HOST, port=PORT
    )

    try:
        # Run the query
        query = f"""
            SELECT cp.*
            FROM pylovo.clustering_parameters cp
            JOIN (
                SELECT version_id, plz, kcid, bcid
                FROM pylovo.transformer_classified
                WHERE kmeans_clusters = 0
                GROUP BY version_id, plz, kcid, bcid
            ) tc
            ON cp.version_id = tc.version_id
            AND cp.plz = tc.plz
            AND cp.kcid = tc.kcid
            AND cp.bcid = tc.bcid;
        """

        df = pd.read_sql_query(query, con=conn)

    finally:
        # Close the connection
        conn.close()

    return df

def calculate_average_clustering_parameters(df: pd.DataFrame, parameters: list) -> dict:
    """
    Calculate the average values for the given clustering parameters.

    :param df: DataFrame with clustering parameters.
    :param parameters: List of parameter names to calculate averages for.
    :return: Dictionary with average values.
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
