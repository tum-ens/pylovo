"""Clustering algorithms for the grid classification.

Each algorithm assigns the grids to clusters and picks one representative grid
per cluster: the real grid closest to the cluster center.

KMedoids clustering was removed to avoid the ``scikit-learn-extra`` dependency
(commit d66e7ed); the ``kmedoid_*`` columns of ``transformer_classified`` stay empty.
"""
import pandas as pd
from scipy.cluster.vq import vq
from sklearn import preprocessing
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture


def reindex_cluster_indices(
    df_parameters_of_grids: pd.DataFrame, representative_networks: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Renumber the clusters by the number of households of their representative grid.

    After renumbering, cluster 0 has the representative grid with the fewest households.

    Args:
        df_parameters_of_grids: Grid parameters with a ``clusters`` column.
        representative_networks: The representative grid of each cluster.

    Returns:
        Tuple ``(df_parameters_of_grids, representative_networks)`` with renumbered
        ``clusters``; ``representative_networks`` is sorted by cluster and its former
        index (the row label in ``df_parameters_of_grids``) is kept in column ``index``.
    """
    df_map = representative_networks.sort_values(by=['no_households'])['clusters'].reset_index()
    df_map['index'] = range(0, len(representative_networks))
    mapping = dict(df_map[['clusters', 'index']].values)
    df_parameters_of_grids = df_parameters_of_grids.replace({'clusters': mapping})
    representative_networks = representative_networks.replace({'clusters': mapping})
    representative_networks = representative_networks.sort_values(by=['clusters']).reset_index()

    return df_parameters_of_grids, representative_networks


def gmm_tied_clustering(
    df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cluster the grids with a Gaussian mixture model with tied covariance.

    The parameters are standardized first. The representative grid of a cluster
    is the grid closest to the component mean.

    Args:
        df_parameters_of_grids: Grid parameters; a ``clusters`` column is added in place.
        list_of_clustering_parameters: Columns used for clustering.
        n_clusters: Number of mixture components.

    Returns:
        Tuple ``(df_parameters_of_grids, representative_networks)``, see
        :func:`reindex_cluster_indices`.
    """
    # scaling and clustering
    X = df_parameters_of_grids[list_of_clustering_parameters]
    X = preprocessing.scale(X)
    gm = GaussianMixture(n_components=n_clusters, covariance_type='tied', random_state=1).fit(X)
    print('converged:', gm.converged_)
    print('no of iterations', gm.n_iter_)
    # we store the cluster labels
    labels = gm.predict(X)
    df_parameters_of_grids['clusters'] = labels

    # find representative networks (grids closest to the component means)
    centroids = gm.means_
    closest, _ = vq(centroids, X)
    representative_networks = df_parameters_of_grids.iloc[closest]

    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks


def kmeans_clustering(
    df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cluster the grids with k-means.

    The parameters are standardized first. The representative grid of a cluster
    is the grid closest to the centroid.

    Args:
        df_parameters_of_grids: Grid parameters; a ``clusters`` column is added in place.
        list_of_clustering_parameters: Columns used for clustering.
        n_clusters: Number of clusters.

    Returns:
        Tuple ``(df_parameters_of_grids, representative_networks)``, see
        :func:`reindex_cluster_indices`.
    """
    # scaling and clustering
    X = df_parameters_of_grids[list_of_clustering_parameters]
    X = preprocessing.scale(X)
    kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
    # we store the cluster labels
    labels = kmeans.labels_
    df_parameters_of_grids['clusters'] = labels

    # find representative networks (grids closest to the centroids)
    centroids = kmeans.cluster_centers_
    closest, _ = vq(centroids, X)
    representative_networks = df_parameters_of_grids.iloc[closest]
    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks
