"""Clustering algorithms for the grid classification.

Each algorithm assigns the grids to clusters and picks one representative grid
per cluster: the member of the cluster closest to the cluster center.

KMedoids clustering was removed to avoid the ``scikit-learn-extra`` dependency
(commit d66e7ed).
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


#: Seeds and initialisations of the two algorithms of step 7 (also in the selection report and the CH/DB curves)
KMEANS_SETTINGS = {"random_state": 0, "n_init": 10}
GMM_SETTINGS = {"random_state": 1, "n_init": 5}


def scaling_record(df_parameters_of_grids: pd.DataFrame, list_of_clustering_parameters: list,
                   transform: str | None = None) -> dict:
    """Return the transform and the standardisation of :func:`scaled_key_figures` as JSON-ready values.

    ``transform`` holds the method and, for ``yeo-johnson``, the fitted exponent of every parameter at full precision
    (``median-yeo-johnson``: also the medians and the divisors applied before it, :func:`~pylovo.classification.clustering.transform.median_divisors`);
    ``scaler`` the mean and standard deviation of the transformed values (``(t - mean) / scale`` gives the rows that
    were clustered, to about 1e-15). With them a grid outside the clustered set can be placed in the same space
    (:func:`assign_clusters`).
    """
    method = TRANSFORM if transform is None else transform
    parameters = [str(name) for name in list_of_clustering_parameters]
    transformed = transform_key_figures(df_parameters_of_grids[parameters], method)
    values = transformed.to_numpy(dtype=float)
    record = {"method": method}
    for key in ("medians", "divisors", "divisor_rules", "lambdas"):
        if key in transformed.attrs:
            record[key] = {name: transformed.attrs[key][name] for name in parameters}
    return {"parameters": parameters, "transform": record,
            "scaler": {"mean": values.mean(axis=0).tolist(), "scale": values.std(axis=0).tolist()}}


def _yeo_johnson(values: np.ndarray, lmbda: float) -> np.ndarray:
    """The Yeo-Johnson transform with a given exponent (the formula of scikit-learn's ``PowerTransformer``)."""
    out = np.zeros_like(values, dtype=float)
    positive = values >= 0
    if abs(lmbda) < np.spacing(1.0):
        out[positive] = np.log1p(values[positive])
    else:
        out[positive] = (np.power(values[positive] + 1, lmbda) - 1) / lmbda
    if abs(lmbda - 2) > np.spacing(1.0):
        out[~positive] = -(np.power(-values[~positive] + 1, 2 - lmbda) - 1) / (2 - lmbda)
    else:
        out[~positive] = -np.log1p(-values[~positive])
    return out


def assign_clusters(df_parameters: pd.DataFrame, model: dict, algorithm: str = "kmeans") -> np.ndarray:
    """Return the cluster of every grid of ``df_parameters`` under a stored model (``classification_version.model``).

    The key figures are transformed and standardised with the stored values, then k-means takes the nearest centre and
    the tied GMM the most probable component, and the stored label map gives the cluster number of the
    classification. For the clustered grids this reproduces ``transformer_classified``.

    Args:
        df_parameters: Key figures with the columns of ``model["parameters"]``.
        model: The stored model (:meth:`DatabaseCommunication.save_transformers_with_classification_info`).
        algorithm: ``kmeans`` or ``gmm_tied``.
    """
    parameters = model["parameters"]
    values = df_parameters[parameters].to_numpy(dtype=float)
    transform = model["transform"]
    if "divisors" in transform:
        values = values / np.array([transform["divisors"][name] for name in parameters])
    if transform["method"] in ("yeo-johnson", "median-yeo-johnson"):
        values = np.column_stack([_yeo_johnson(values[:, i], transform["lambdas"][name])
                                  for i, name in enumerate(parameters)])
    elif transform["method"] == "log1p":
        values = np.log1p(values)
    X = (values - np.array(model["scaler"]["mean"])) / np.array(model["scaler"]["scale"])
    fitted = model[algorithm]
    centers = np.array(fitted["means"] if algorithm == "gmm_tied" else fitted["centers"])
    if algorithm == "gmm_tied":
        prec_chol = np.array(fitted["precisions_cholesky"])
        y = (X[:, None, :] - centers[None, :, :]) @ prec_chol
        log_prob = -0.5 * (y ** 2).sum(axis=2) + np.log(np.array(fitted["weights"]))
        labels = log_prob.argmax(axis=1)
    else:
        labels = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2).argmin(axis=1)
    label_map = {int(k): int(v) for k, v in fitted["label_map"].items()}
    return np.array([label_map[int(label)] for label in labels])


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
        index (the row label in ``df_parameters_of_grids``) is kept in column ``index``. Its
        ``attrs["label_map"]`` maps the label of the algorithm to the new number.
    """
    df_map = representative_networks.sort_values(by=['no_households'], kind='stable')['clusters'].reset_index()
    df_map['index'] = range(0, len(representative_networks))
    mapping = dict(df_map[['clusters', 'index']].values)
    df_parameters_of_grids = df_parameters_of_grids.replace({'clusters': mapping})
    representative_networks = representative_networks.replace({'clusters': mapping})
    representative_networks = representative_networks.sort_values(by=['clusters']).reset_index()
    representative_networks.attrs["label_map"] = {int(label): int(number) for label, number in mapping.items()}

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
        :func:`reindex_cluster_indices`. ``representative_networks.attrs["model"]`` holds the fitted model as
        JSON-ready values (centres or mixture parameters, settings and the label map).
    """
    # transform, scaling and clustering
    X = scaled_key_figures(df_parameters_of_grids, list_of_clustering_parameters, transform)
    gm = GaussianMixture(n_components=n_clusters, covariance_type='tied', **GMM_SETTINGS).fit(X)
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
    representative_networks.attrs["model"] = {
        "n_components": int(n_clusters), "covariance_type": "tied", **GMM_SETTINGS,
        "weights": gm.weights_.tolist(), "means": gm.means_.tolist(), "covariance": gm.covariances_.tolist(),
        "precisions_cholesky": gm.precisions_cholesky_.tolist(), "converged": bool(gm.converged_),
        "n_iter": int(gm.n_iter_), "lower_bound": float(gm.lower_bound_),
        "label_map": representative_networks.attrs["label_map"]}

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
        :func:`reindex_cluster_indices`. ``representative_networks.attrs["model"]`` holds the fitted model as
        JSON-ready values (centres or mixture parameters, settings and the label map).
    """
    # transform, scaling and clustering
    X = scaled_key_figures(df_parameters_of_grids, list_of_clustering_parameters, transform)
    kmeans = KMeans(n_clusters=n_clusters, **KMEANS_SETTINGS).fit(X)
    # we store the cluster labels
    labels = kmeans.labels_
    df_parameters_of_grids['clusters'] = labels

    # find representative networks (members closest to the centroids)
    closest = representatives_in_clusters(X, labels, kmeans.cluster_centers_)
    representative_networks = df_parameters_of_grids.iloc[closest]
    df_parameters_of_grids, representative_networks = reindex_cluster_indices(
        df_parameters_of_grids=df_parameters_of_grids, representative_networks=representative_networks)
    representative_networks.attrs["model"] = {
        "n_clusters": int(n_clusters), **KMEANS_SETTINGS, "centers": kmeans.cluster_centers_.tolist(),
        "inertia": float(kmeans.inertia_), "n_iter": int(kmeans.n_iter_),
        "label_map": representative_networks.attrs["label_map"]}

    return df_parameters_of_grids, representative_networks
