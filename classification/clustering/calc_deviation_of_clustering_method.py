import math

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns


def calc_deviation_of_clustering_method(df_parameters_of_grids: pd.DataFrame, representative_networks: pd.DataFrame,
                                        n_clusters: int) -> float:
    """
    Calculate the overall deviation of a cluster-analysis. E.g. use to compare different clustering approaches 
    Calculate the deviation of all values from the medioid.
    see also examples_clustering/kmedoids_calc_deviation

    Parameters
    ----------
    df_parameters_of_grids : DataFrame
        Contains all parameters and the cluster of grids.
    representative_networks : DataFrame
        The representative grids or medoids.
    n_clusters : int

    Returns
    -------
    float
        The mean deviation of a clustering method of all parameters and clusters.
    """
    no_parameters = df_parameters_of_grids.shape[1] - 1  # no of columns - 1
    deviation_of_clusters = []
    for cluster in range(0, n_clusters):

        # get current cluster and its medoid
        current_cluster = df_parameters_of_grids.groupby('clusters').get_group(cluster)
        representative_grid = representative_networks.iloc[cluster]

        # no grids in cluster
        no_grids_in_cluster = len(current_cluster)

        # for each grid calculate the normalized deviation from medoid
        deviation_from_medoid = current_cluster
        for column in df_parameters_of_grids:
            deviation_from_medoid[column] = ((current_cluster[column] - representative_grid[column]) /
                                             representative_grid[
                                                 column]) ** 2

        # for each parameter (column) add up all deviations and divide by no of grids in cluster
        deviation_of_parameters = representative_grid
        deviation_of_parameters = deviation_of_parameters.to_frame().T.reset_index()
        for column in df_parameters_of_grids:
            deviation_of_parameters.at[0, column] = math.sqrt(deviation_from_medoid[column].sum() / no_grids_in_cluster)
        deviation_of_parameters = deviation_of_parameters.drop(['index', 'clusters'], axis=1)

        # plot deviation
        fig, ax = plt.subplots()
        print('--- cluster ', cluster, ' ---')
        ax = sns.barplot(deviation_of_parameters)
        ax.set_ylim(0, 5)
        ax.tick_params(axis='x', rotation=90)
        fig

        # calculate mean over all parameters
        deviation_of_curr_cluster = deviation_of_parameters.to_numpy().sum() / no_parameters
        deviation_of_clusters.append(deviation_of_curr_cluster)

    print('deviation of individual clusters:')
    print(deviation_of_clusters)
    # calculate mean deviation over all clusters
    mean_deviation_of_all_clusters = sum(deviation_of_clusters) / n_clusters

    return mean_deviation_of_all_clusters
