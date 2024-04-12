from factor_analyzer import FactorAnalyzer

from classification.config_classification import NO_OF_CLUSTERS_ALLOWED
from classification.database_communication.DatabaseCommunication import DatabaseCommunication
from plotting.plotting_classification import get_parameters_for_clustering
from plotting.plotting_classification import plot_ch_index_for_clustering_algos


def print_parameters_for_clustering_for_classification_version() -> None:
    """ print optimal clustering parameter for grid data of classification version
    """
    # get grid data
    dc = DatabaseCommunication()
    df = dc.get_clustering_parameters_for_classification_version()

    # Dropping unnecessary columns
    df.drop(['version_id', 'plz', 'bcid', 'kcid', 'ratio', 'osm_trafo', 'house_distance_km', 'no_connection_buses',
             'resistance', 'reactance', 'simultaneous_peak_load_mw',
             'no_household_equ', 'max_power_mw'], axis=1, inplace=True)

    # Create factor analysis object and perform factor analysis
    fa = FactorAnalyzer()
    fa.fit(df)

    # Check Eigenvalues
    ev = fa.get_eigenvalues()

    # get the eigenvalues larger than 1.
    # --> This is the appropriate number of factors
    no_of_factors = (ev[0] > 1).sum()

    # print parameters
    get_parameters_for_clustering(df_grid_parameters=df, n_comps=no_of_factors)


def get_best_no_of_clusters_ch_index_for_classification_version() -> None:
    """print and plot best number of clusters for cluster algorithms determined with CH index"""
    # import the dateset of grid parameters
    dc = DatabaseCommunication()
    df_parameters_of_grids = dc.get_clustering_parameters_for_classification_version()

    plot_ch_index_for_clustering_algos(df_grid_parameters=df_parameters_of_grids,
                                       no_of_clusters_allowed=NO_OF_CLUSTERS_ALLOWED)
