"""Clustering algorithms for the grid classification.

Each algorithm assigns the grids to clusters and picks one representative grid
per cluster: the member of the cluster closest to the cluster center.

KMedoids clustering was removed to avoid the ``scikit-learn-extra`` dependency
(commit d66e7ed); the ``kmedoid_*`` columns of ``transformer_classified`` stay empty.
"""
import numpy as np
import pandas as pd
from sklearn import preprocessing
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture

from pylovo.classification.clustering.transform import transform_key_figures
from pylovo.config_loader import TRANSFORM


def scaled_key_figures(df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list,
                       transform: str | None = None) -> np.ndarray:
    """Return the clustering parameters transformed (:func:`transform_key_figures`) and standardised.

    Args:
        df_parameters_of_grids: Grid parameters.
        list_of_clustering_parameters: Columns used for clustering.
        transform: Transform of the key figures; ``None`` uses ``TRANSFORM`` of ``config_clustering.yaml``.
    """
    transformed = transform_key_figures(df_parameters_of_grids[list(list_of_clustering_parameters)],
                                        TRANSFORM if transform is None else transform)
    return preprocessing.scale(transformed)


def representatives_in_clusters(X: np.ndarray, labels: np.ndarray, centers: np.ndarray) -> np.ndarray:
    """Return the row position of the representative grid of every cluster.

    The representative of cluster ``i`` is the member of cluster ``i`` closest to
    ``centers[i]``. Searching all grids is not enough: with overlapping mixture
    components the grid closest to a mean can belong to another component.

    Args:
        X: Standardized parameters, one row per grid.
        labels: Cluster of every grid.
        centers: Cluster centers, row ``i`` belongs to cluster ``i``.

    Returns:
        Row positions in ``X``, one per cluster in the order of ``centers``.

    Raises:
        ValueError: If a cluster has no member.
    """
    positions = []
    for cluster, center in enumerate(centers):
        members = np.flatnonzero(labels == cluster)
        if members.size == 0:
            raise ValueError(f"Cluster {cluster} contains no grid, use fewer clusters.")
        positions.append(members[np.argmin(((X[members] - center) ** 2).sum(axis=1))])
    return np.array(positions)


def reindex_cluster_indices(
    df_parameters_of_grids: pd.DataFrame, representative_networks: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Renumber the clusters by the number of households of their representative grid.

    After renumbering, cluster 0 has the representative grid with the fewest households;
    clusters with the same number of households keep their order.

    Args:
        df_parameters_of_grids: Grid parameters with a ``clusters`` column.
        representative_networks: The representative grid of each cluster.

    Returns:
        Tuple ``(df_parameters_of_grids, representative_networks)`` with renumbered
        ``clusters``; ``representative_networks`` is sorted by cluster and its former
        index (the row label in ``df_parameters_of_grids``) is kept in column ``index``.
    """
    df_map = representative_networks.sort_values(by=['no_households'], kind='stable')['clusters'].reset_index()
    df_map['index'] = range(0, len(representative_networks))
    mapping = dict(df_map[['clusters', 'index']].values)
    df_parameters_of_grids = df_parameters_of_grids.replace({'clusters': mapping})
    representative_networks = representative_networks.replace({'clusters': mapping})
    representative_networks = representative_networks.sort_values(by=['clusters']).reset_index()

    return df_parameters_of_grids, representative_networks


def gmm_tied_clustering(
    df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int,
    transform: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cluster the grids with a Gaussian mixture model with tied covariance.

    The parameters are transformed and standardized first (:func:`scaled_key_figures`). The representative grid of a
    cluster is the member closest to the component mean.

    Args:
        df_parameters_of_grids: Grid parameters; a ``clusters`` column is added in place.
        list_of_clustering_parameters: Columns used for clustering.
        n_clusters: Number of mixture components.
        transform: Transform of the key figures; ``None`` uses ``TRANSFORM`` of ``config_clustering.yaml``.

    Returns:
        Tuple ``(df_parameters_of_grids, representative_networks)``, see
        :func:`reindex_cluster_indices`.
    """
    # transform, scaling and clustering
    X = scaled_key_figures(df_parameters_of_grids, list_of_clustering_parameters, transform)
    gm = GaussianMixture(n_components=n_clusters, covariance_type='tied', random_state=1).fit(X)
    print('converged:', gm.converged_)
    print('no of iterations', gm.n_iter_)
    # we store the cluster labels
    labels = gm.predict(X)
    df_parameters_of_grids['clusters'] = labels

    # find representative networks (members closest to the component means)
    closest = representatives_in_clusters(X, labels, gm.means_)
    representative_networks = df_parameters_of_grids.iloc[closest]

    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks


def kmeans_clustering(
    df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int,
    transform: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cluster the grids with k-means.

    The parameters are transformed and standardized first (:func:`scaled_key_figures`). The representative grid of a
    cluster is the member closest to the centroid.

    Args:
        df_parameters_of_grids: Grid parameters; a ``clusters`` column is added in place.
        list_of_clustering_parameters: Columns used for clustering.
        n_clusters: Number of clusters.
        transform: Transform of the key figures; ``None`` uses ``TRANSFORM`` of ``config_clustering.yaml``.

    Returns:
        Tuple ``(df_parameters_of_grids, representative_networks)``, see
        :func:`reindex_cluster_indices`.
    """
    # transform, scaling and clustering
    X = scaled_key_figures(df_parameters_of_grids, list_of_clustering_parameters, transform)
    kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
    # we store the cluster labels
    labels = kmeans.labels_
    df_parameters_of_grids['clusters'] = labels

    # find representative networks (members closest to the centroids)
    closest = representatives_in_clusters(X, labels, kmeans.cluster_centers_)
    representative_networks = df_parameters_of_grids.iloc[closest]
    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks
