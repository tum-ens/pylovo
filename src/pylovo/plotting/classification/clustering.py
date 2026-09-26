"""
Clustering analysis plotting functions.
This module contains functions for visualizing clustering results and quality metrics,
including CH/DB indices, cluster distributions, and 3D visualizations.
"""
from math import ceil, pi
from typing import Sequence, Tuple
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.figure import Figure
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from sklearn import preprocessing
from sklearn.cluster import KMeans
from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score
from sklearn.mixture import GaussianMixture

from pylovo.config_loader import TUMPalette, LIST_OF_CLUSTERING_PARAMETERS, REGIO7_REGIO5_GEM_DICT


def plot_radar_graph(
    representative_networks: pd.DataFrame,
    list_of_parameters: list,
    figsize: Tuple[int, int] = (10, 10)
) -> Figure:
    """
    Plot representative networks as radar graphs.

    One polar panel per representative grid (three per row); each parameter is
    normalized by its maximum over all representative grids.

    Args:
        representative_networks: Table of parameters of representative networks
            with a ``clusters`` column.
        list_of_parameters: Parameters to plot in radar graph.
        figsize: Figure size in inches (width, height). Default: (10, 10).

    Returns:
        matplotlib.figure.Figure: The Figure object containing the radar plots.
    """
    # Prepare data
    representative_networks = representative_networks.reset_index()
    list_of_parameters_copy = list_of_parameters.copy()
    list_of_parameters_copy.append('clusters')
    representative_networks = representative_networks[list_of_parameters_copy]

    # Normalize values
    max_values = representative_networks.max()
    representative_networks_normalized = representative_networks.copy()
    for column in representative_networks:
        representative_networks_normalized[column] = representative_networks[column] / max_values[column]

    # Move clusters column to first position
    first_column = representative_networks_normalized.pop('clusters')
    representative_networks_normalized.insert(0, 'clusters', first_column)

    def make_spider(row, title, color, ax):
        """Create a single radar plot."""
        categories = list(representative_networks_normalized)[1:]
        N = len(categories)
        angles = [n / float(N) * 2 * pi for n in range(N)]
        angles += angles[:1]

        ax.set_theta_offset(pi / 2)
        ax.set_theta_direction(-1)
        ax.set_xticks(angles[:-1], categories, color='grey', size=8)

        ax.set_rlabel_position(0)
        ax.set_yticks([0.33, 0.66, 1.0], ["33%", "66%", "100%"], color="grey", size=7)
        ax.set_ylim(0, 1.1)

        values = representative_networks_normalized.loc[row].drop('clusters').values.flatten().tolist()
        values += values[:1]
        ax.plot(angles, values, color=color, linewidth=2, linestyle='solid')
        ax.fill(angles, values, color=color, alpha=0.4)
        ax.set_title(title, size=11, color=color, y=1.1)

    # Create figure and subplots: three panels per row, at least two rows (the
    # former fixed 2 x 3 grid failed for 7 clusters, see NO_OF_CLUSTERS_ALLOWED)
    n_plots = len(representative_networks.index)
    n_rows = max(2, ceil(n_plots / 3))
    fig = plt.figure(figsize=figsize, dpi=96)

    for row in range(0, n_plots):
        ax = plt.subplot(n_rows, 3, row + 1, polar=True)
        make_spider(row=row, title=row, color=TUMPalette[row % len(TUMPalette)], ax=ax)

    return fig


def _clustering_columns(list_of_clustering_parameters: Sequence[str] | None) -> list[str]:
    """Return the given clustering columns or, if ``None``, the configured ones."""
    if list_of_clustering_parameters is None:
        return list(LIST_OF_CLUSTERING_PARAMETERS)
    return list(list_of_clustering_parameters)


def plot_ch_index_for_clustering_algos(
    df_plz_parameters: pd.DataFrame,
    no_of_clusters_allowed: Sequence[int] = range(3, 8),
    list_of_clustering_parameters: Sequence[str] | None = None,
) -> pd.DataFrame:
    """
    Plot Calinski-Harabasz index for different clustering algorithms.

    Args:
        df_plz_parameters: Set of parameters for grids.
        no_of_clusters_allowed: Cluster numbers to test. Default: range(3, 8).
        list_of_clustering_parameters: Columns used for clustering. Defaults to
            ``LIST_OF_CLUSTERING_PARAMETERS`` from ``config_clustering.yaml``.

    Returns:
        pd.DataFrame: Comparison table of optimal cluster numbers for each algorithm.
    """
    X = df_plz_parameters[_clustering_columns(list_of_clustering_parameters)]
    X = preprocessing.scale(X)

    df_ch_comparison = pd.DataFrame(columns=['algorithm', 'no_clusters', 'ch_index'])
    df_ch_index = pd.DataFrame({'no_clusters': list(no_of_clusters_allowed)})

    # Test different algorithms
    algorithms = [
        ('kmeans', lambda n: KMeans(n_clusters=n, random_state=0)),
        ('gmm_full', lambda n: GaussianMixture(n_components=n, covariance_type='full', random_state=1)),
        ('gmm_diag', lambda n: GaussianMixture(n_components=n, covariance_type='diag', random_state=1)),
        ('gmm_tied', lambda n: GaussianMixture(n_components=n, covariance_type='tied', random_state=1)),
        ('gmm_sph', lambda n: GaussianMixture(n_components=n, covariance_type='spherical', random_state=1))
    ]

    for algo_name, algo_func in algorithms:
        ch_list = []
        for n_clusters in no_of_clusters_allowed:
            model = algo_func(n_clusters).fit(X)
            labels = model.labels_ if hasattr(model, 'labels_') else model.predict(X)
            ch_index = calinski_harabasz_score(X, labels)
            ch_list.append(ch_index)

        df_ch_index[f'ch_index_{algo_name}'] = ch_list
        idxmax = df_ch_index[f'ch_index_{algo_name}'].idxmax()
        opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
        ch_index_opt = df_ch_index[f'ch_index_{algo_name}'].max()
        df_ch_comparison.loc[len(df_ch_comparison)] = [algo_name, opt_no_clusters, ch_index_opt]

    # Plot results
    ax = sns.lineplot(data=pd.melt(df_ch_index, ['no_clusters']),
                      y='value', x='no_clusters', hue='variable', palette=TUMPalette)
    handles, labels = ax.get_legend_handles_labels()
    labels = ['kmeans', 'GMM full', 'GMM diagonal', 'GMM tied', 'GMM spherical']
    ax.legend(handles, labels, title='Clustering Algorithmus')
    ax.set(xlabel='Anzahl Cluster', ylabel='CH Index')
    sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))

    return df_ch_comparison


def plot_db_index_for_clustering_algos(
    df_plz_parameters: pd.DataFrame,
    no_of_clusters_allowed: Sequence[int] = range(3, 8),
    list_of_clustering_parameters: Sequence[str] | None = None,
) -> pd.DataFrame:
    """
    Plot Davies-Bouldin index for different clustering algorithms.

    Args:
        df_plz_parameters: Set of parameters for grids.
        no_of_clusters_allowed: Cluster numbers to test. Default: range(3, 8).
        list_of_clustering_parameters: Columns used for clustering. Defaults to
            ``LIST_OF_CLUSTERING_PARAMETERS`` from ``config_clustering.yaml``.

    Returns:
        pd.DataFrame: Comparison table of optimal cluster numbers for each algorithm.
    """
    X = df_plz_parameters[_clustering_columns(list_of_clustering_parameters)]
    X = preprocessing.scale(X)

    df_db_comparison = pd.DataFrame(columns=['algorithm', 'no_clusters', 'db_index'])
    df_db_index = pd.DataFrame({'no_clusters': list(no_of_clusters_allowed)})

    # same seeds as plot_ch_index_for_clustering_algos, so the results are reproducible
    algorithms = [
        ('kmeans', lambda n: KMeans(n_clusters=n, random_state=0)),
        ('gmm_full', lambda n: GaussianMixture(n_components=n, covariance_type='full', random_state=1)),
        ('gmm_diag', lambda n: GaussianMixture(n_components=n, covariance_type='diag', random_state=1)),
        ('gmm_tied', lambda n: GaussianMixture(n_components=n, covariance_type='tied', random_state=1)),
        ('gmm_sph', lambda n: GaussianMixture(n_components=n, covariance_type='spherical', random_state=1))
    ]

    for algo_name, algo_func in algorithms:
        db_list = []
        for n_clusters in no_of_clusters_allowed:
            model = algo_func(n_clusters).fit(X)
            labels = model.labels_ if hasattr(model, 'labels_') else model.predict(X)
            db_index = davies_bouldin_score(X, labels)
            db_list.append(db_index)

        df_db_index[f'db_index_{algo_name}'] = db_list
        idxmin = df_db_index[f'db_index_{algo_name}'].idxmin()
        opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
        db_index_opt = df_db_index[f'db_index_{algo_name}'].min()
        df_db_comparison.loc[len(df_db_comparison)] = [algo_name, opt_no_clusters, db_index_opt]

    # Plot results
    ax = sns.lineplot(data=pd.melt(df_db_index, ['no_clusters']),
                      y='value', x='no_clusters', hue='variable', palette=TUMPalette)
    handles, labels = ax.get_legend_handles_labels()
    labels = ['kmeans', 'GMM full', 'GMM diagonal', 'GMM tied', 'GMM spherical']
    ax.legend(handles, labels, title='Clustering Algorithmus')
    ax.set(xlabel='Anzahl Cluster', ylabel='DB Index')
    sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))

    return df_db_comparison


def plot_percentage_of_clusters(df_plz_parameters: pd.DataFrame) -> Figure:
    """
    Plot distribution of clusters as a bar chart.

    Args:
        df_plz_parameters: Set of parameters for clustered grids with 'clusters' column.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the cluster distribution plot.
    """
    len_grids = len(df_plz_parameters)
    clusters_perc = df_plz_parameters['clusters'].value_counts() / len_grids
    clusters_perc = pd.DataFrame(clusters_perc)
    clusters_perc['count'] = clusters_perc['count'] * 100
    clusters_perc = clusters_perc.sort_index().reset_index()

    fig = plt.figure(figsize=(8, 6))
    sns.barplot(data=clusters_perc, y='count', x='clusters')
    plt.ylabel('Anteil in %')
    plt.xlabel('Index des Clusters')

    return fig


def plot_stacked_distribution_of_clusters_per_regio_5(
    df_plz_parameters: pd.DataFrame
) -> Figure:
    """
    Plot stacked bar chart of cluster distribution for each regio 5 class.

    Args:
        df_plz_parameters: Set of parameters for clustered grids with 'clusters' and 'regio7' columns.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the stacked distribution plot.
    """
    df_plz_parameters = df_plz_parameters.copy()
    df_plz_parameters['regio7'] = df_plz_parameters['regio7'].map(REGIO7_REGIO5_GEM_DICT)
    df_matrix = df_plz_parameters.pivot_table(index='regio7', columns='clusters', aggfunc='size')
    df_matrix = df_matrix.div(df_matrix.sum(axis=1), axis=0).reset_index()

    ax = df_matrix.plot(x='regio7', kind='bar', stacked=True,
                        title='Verteilung der Cluster über Regio5 Gem')
    plt.xlabel('Regionalstatistischer Gemeindetyp 5')
    plt.ylabel('Anteil je Klasse (normiert)')
    sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))

    return ax.get_figure()


def plot_bar_distribution_of_clusters_per_regio_5(df_plz_parameters: pd.DataFrame) -> None:
    """
    Plot bar chart of cluster distribution for each regio 5 class.

    Args:
        df_plz_parameters: Set of parameters for clustered grids with 'clusters' and 'regio7' columns.
    """
    x, y = 'regio7', 'clusters'
    (df_plz_parameters
     .groupby(x)[y]
     .value_counts(normalize=True)
     .mul(100)
     .rename('percent')
     .reset_index()
     .pipe((sns.catplot, 'data'), x=x, y='percent', hue=y, kind='bar'))


def get_min_max_data_for_clusters(n_clusters: int, df_networks: pd.DataFrame) -> pd.DataFrame:
    """
    Get minimum and maximum values for each cluster and parameter.

    Args:
        n_clusters: Number of clusters.
        df_networks: Network parameters with 'clusters' column.

    Returns:
        pd.DataFrame: Min/max per cluster of attribute 1 ``no_house_connections``,
        attribute 2 ``cable_len_per_house`` and attribute 3 ``transformer_mva``.
    """
    df_min_max = pd.DataFrame(
        columns=['attribute1_min', 'attribute1_max', 'attribute2_min', 'attribute2_max',
                 'attribute3_min', 'attribute3_max'])

    for n in range(0, n_clusters):
        networks_cluster_n = df_networks.groupby('clusters').get_group(n)
        df_min_max.at[n, 'attribute1_min'] = networks_cluster_n['no_house_connections'].min()
        df_min_max.at[n, 'attribute1_max'] = networks_cluster_n['no_house_connections'].max()
        df_min_max.at[n, 'attribute2_min'] = networks_cluster_n['cable_len_per_house'].min()
        df_min_max.at[n, 'attribute2_max'] = networks_cluster_n['cable_len_per_house'].max()
        df_min_max.at[n, 'attribute3_min'] = networks_cluster_n['transformer_mva'].min()
        df_min_max.at[n, 'attribute3_max'] = networks_cluster_n['transformer_mva'].max()

    return df_min_max


def plot_clusters_3D(df_min_max: pd.DataFrame, df_centroids: pd.DataFrame) -> Figure:
    """
    Plot clusters as 3D boxes with centroids.

    Args:
        df_min_max: Min/max values per cluster from :func:`get_min_max_data_for_clusters`.
        df_centroids: Cluster centroids with columns ``0``, ``1``, ``2`` in the attribute
            order of ``df_min_max``; an optional ``color`` column in ``df_min_max`` sets
            the box colors.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the 3D cluster plot.
    """
    fig = plt.figure(figsize=(10, 10))
    ax = fig.add_subplot(111, projection='3d')

    for index, row in df_min_max.iterrows():
        Z = np.array([[row['attribute1_min'], row['attribute2_min'], row['attribute3_min']],
                      [row['attribute1_max'], row['attribute2_min'], row['attribute3_min']],
                      [row['attribute1_max'], row['attribute2_max'], row['attribute3_min']],
                      [row['attribute1_min'], row['attribute2_max'], row['attribute3_min']],
                      [row['attribute1_min'], row['attribute2_min'], row['attribute3_max']],
                      [row['attribute1_max'], row['attribute2_min'], row['attribute3_max']],
                      [row['attribute1_max'], row['attribute2_max'], row['attribute3_max']],
                      [row['attribute1_min'], row['attribute2_max'], row['attribute3_max']]])
        ax.scatter3D(Z[:, 0], Z[:, 1], Z[:, 2])
        verts = [[Z[0], Z[1], Z[2], Z[3]],
                 [Z[4], Z[5], Z[6], Z[7]],
                 [Z[0], Z[1], Z[5], Z[4]],
                 [Z[2], Z[3], Z[7], Z[6]],
                 [Z[1], Z[2], Z[6], Z[5]],
                 [Z[4], Z[7], Z[3], Z[0]]]
        color = row.get('color', 'blue')
        ax.add_collection3d(Poly3DCollection(verts, facecolors=color, linewidths=1,
                                             edgecolors='r', alpha=.20))

    ax.scatter(df_centroids[0], df_centroids[1], df_centroids[2], color='black', marker='*')
    ax.set_xlabel('No of house connections')
    ax.set_ylabel('Cable length per house (km)')
    ax.set_zlabel('Trafo Size (MVA)')
    ax.zaxis.labelpad = -0.7

    return fig

