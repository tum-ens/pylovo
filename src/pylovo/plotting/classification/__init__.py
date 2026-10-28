"""
Classification workflow plotting functions.

This module provides plotting functions for classification, clustering, and feature analysis.
"""

from .clustering import (
    cluster_color,
    plot_radar_graph,
    plot_ch_index_for_clustering_algos,
    plot_db_index_for_clustering_algos,
    plot_percentage_of_clusters,
    plot_clusters_per_regiostar_class,
    plot_stacked_distribution_of_clusters_per_regio_5,
    plot_bar_distribution_of_clusters_per_regio_5,
    plot_clusters_in_parameter_space,
    plot_representative_grids,
    get_min_max_data_for_clusters,
    plot_clusters_3D,
)

from .features import (
    REGIOSTAR7_NAMES,
    regiostar7_color,
    plot_correlation_matrix,
    plot_samples_per_regiostarclass,
    plot_samples_on_map,
    plot_sample_map,
    plot_key_figure_distributions,
    plot_factor_analysis,
    get_parameters_for_clustering,
    varimax_factor_analysis,
    plot_eigendecomposition,
)

__all__ = [
    # Clustering
    'cluster_color',
    'plot_radar_graph',
    'plot_ch_index_for_clustering_algos',
    'plot_db_index_for_clustering_algos',
    'plot_percentage_of_clusters',
    'plot_clusters_per_regiostar_class',
    'plot_stacked_distribution_of_clusters_per_regio_5',
    'plot_bar_distribution_of_clusters_per_regio_5',
    'plot_clusters_in_parameter_space',
    'plot_representative_grids',
    'get_min_max_data_for_clusters',
    'plot_clusters_3D',
    # Features
    'REGIOSTAR7_NAMES',
    'regiostar7_color',
    'plot_correlation_matrix',
    'plot_samples_per_regiostarclass',
    'plot_samples_on_map',
    'plot_sample_map',
    'plot_key_figure_distributions',
    'plot_factor_analysis',
    'get_parameters_for_clustering',
    'varimax_factor_analysis',
    'plot_eigendecomposition',
]
