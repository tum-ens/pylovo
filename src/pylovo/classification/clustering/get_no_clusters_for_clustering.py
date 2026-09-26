"""Recommend the number of clusters per algorithm with the Calinski-Harabasz index."""
import warnings

import pandas as pd

from pylovo.config_loader import NO_OF_CLUSTERS_ALLOWED
from pylovo.classification.database_communication.database_communication import DatabaseCommunication
from pylovo.plotting.classification import plot_ch_index_for_clustering_algos


def get_no_clusters_for_clustering(list_of_clustering_parameters: list | None = None) -> pd.DataFrame:
    """Plot the CH index over ``NO_OF_CLUSTERS_ALLOWED`` and return the best cluster count per algorithm.

    Warnings (e.g. convergence warnings of the tested algorithms) are suppressed.

    Args:
        list_of_clustering_parameters: Columns used for clustering. Defaults to
            ``LIST_OF_CLUSTERING_PARAMETERS`` as loaded at start-up.

    Returns:
        pd.DataFrame: Columns ``algorithm``, ``no_clusters`` and ``ch_index``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dc = DatabaseCommunication()
        df_parameters_of_grids = dc.get_clustering_parameters_for_classification_version()
        return plot_ch_index_for_clustering_algos(df_plz_parameters=df_parameters_of_grids,
                                                  no_of_clusters_allowed=NO_OF_CLUSTERS_ALLOWED,
                                                  list_of_clustering_parameters=list_of_clustering_parameters)


def main() -> None:
    df_no = get_no_clusters_for_clustering()
    print(df_no)


if __name__ == "__main__":
    main()
