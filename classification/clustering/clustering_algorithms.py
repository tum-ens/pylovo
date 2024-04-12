import pandas as pd
from scipy.cluster.vq import vq
from sklearn import preprocessing
from sklearn.cluster import KMeans
from sklearn.mixture import GaussianMixture
from sklearn_extra.cluster import KMedoids


def reindex_cluster_indices(df_parameters_of_grids: pd.DataFrame, representative_networks: pd.DataFrame) -> (
        pd.DataFrame, pd.DataFrame):
    """sort the cluster indices by the representatives networks number of households ascending.
    this means the representative grid of cluster index 0 has the least number of households

    :param df_parameters_of_grids: set of parameters for grids that are clustered and thus have a column named 'clusters'
    :type df_parameters_of_grids: pd.DataFrame

    :param representative_networks: set of representative grids
    :type representative_networks: pd.DataFrame

    :return: df_parameters_of_grids with re-indexed clusters
    :rtype: pd.DataFrame

    :return: representative_networks with re-indexed clusters
    :rtype: pd.DataFrame
    """
    df_map = representative_networks.sort_values(by=['no_households'])['clusters'].reset_index()
    df_map['index'] = range(0, len(representative_networks))
    mapping = dict(df_map[['clusters', 'index']].values)
    df_parameters_of_grids = df_parameters_of_grids.replace({'clusters': mapping})
    representative_networks = representative_networks.replace({'clusters': mapping})
    representative_networks = representative_networks.sort_values(by=['clusters']).reset_index()

    return df_parameters_of_grids, representative_networks


def kmedoids_clustering(df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int) -> (
        pd.DataFrame, pd.DataFrame):
    """
    Clustering the grids with kmedoids algorithm

    Parameters
    ----------
    df_parameters_of_grids : DataFrame
        Grids with parameters to be clustered
    list_of_clustering_parameters : list of strings
        Parameters used for clustering.
    n_clusters: int
        Number of clusters.

    Returns
    -------
    Dataframe:
        Grids that are attributed to a cluster.
    Dataframe:
        Grids that are medoids (cluster centers).
    """
    # scaling and clustering
    X = df_parameters_of_grids[list_of_clustering_parameters]
    X = preprocessing.scale(X)
    kmedoids = KMedoids(n_clusters=n_clusters, random_state=0).fit(X)

    # we store the cluster labels
    df_parameters_of_grids['clusters'] = kmedoids.labels_

    # find representative networks (medoids)
    medoid_indices = kmedoids.medoid_indices_
    representative_networks = df_parameters_of_grids.iloc[medoid_indices]

    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks


def gmm_tied_clustering(df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int) -> (
        pd.DataFrame, pd.DataFrame):
    """
    Clustering the grids with gmm tied algorithm

    Parameters
    ----------
    df_parameters_of_grids : DataFrame
        Grids with parameters to be clustered
    list_of_clustering_parameters : list of strings
        Parameters used for clustering.
    n_clusters: int
        Number of clusters.

    Returns
    -------
    Dataframe:
        Grids that are attributed to a cluster.
    Dataframe:
        Grids that are medoids (cluster centers).
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

    # find representative networks (centroids)
    centroids = gm.means_
    closest, distances = vq(centroids, X)
    representative_networks = df_parameters_of_grids.iloc[closest]

    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks


def kmeans_clustering(df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list, n_clusters: int) -> (
        pd.DataFrame, pd.DataFrame):
    """
    Clustering the grids with kmeans algorithm

    Parameters
    ----------
    df_parameters_of_grids : DataFrame
        Grids with parameters to be clustered
    list_of_clustering_parameters : list of strings
        Parameters used for clustering.
    n_clusters: int
        Number of clusters.

    Returns
    -------
    Dataframe:
        Grids that are attributed to a cluster.
    Dataframe:
        Grids that are centroids (cluster centers).
    """
    # scaling and clustering
    X = df_parameters_of_grids[list_of_clustering_parameters]
    X = preprocessing.scale(X)
    kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
    # print('converged:', gm.converged_)
    # print('no of iterations', gm.n_iter_)
    # we store the cluster labels
    labels = kmeans.labels_
    df_parameters_of_grids['clusters'] = labels

    # find representative networks (centroids)
    centroids = kmeans.cluster_centers_
    closest, distances = vq(centroids, X)
    representative_networks = df_parameters_of_grids.iloc[closest]
    # print(representative_networks[['clusters', 'plz']])
    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)

    return df_parameters_of_grids, representative_networks
