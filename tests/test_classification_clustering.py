"""Pure-function tests of ``classification/clustering/clustering_algorithms.py`` (synthetic frames, no database)."""
import numpy as np
import pandas as pd
import pytest

from classification_helpers import synthetic_clustering_parameters
from pylovo.classification.clustering.clustering_algorithms import (
    gmm_tied_clustering,
    kmeans_clustering,
    reindex_cluster_indices,
    representatives_in_clusters,
)

PARAMS = ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"]
ALGORITHMS = [kmeans_clustering, gmm_tied_clustering]


def heavy_tailed(n: int, seed: int) -> pd.DataFrame:
    """Log-normal grid parameters like real data (many small grids, few very large ones)."""
    rng = np.random.default_rng(seed)
    a = rng.lognormal(3.0, 0.9, n)
    df = pd.DataFrame({"a": a, "b": a * rng.lognormal(0.0, 0.5, n), "c": rng.lognormal(0.0, 0.7, n)})
    df["no_households"] = (a * 2).round()
    return df


def assert_consistent(out: pd.DataFrame, reps: pd.DataFrame, k: int) -> None:
    assert sorted(out["clusters"].unique()) == list(range(k)), "cluster ids must be exactly 0..k-1"
    assert list(reps["clusters"]) == list(range(k)), "exactly one representative per cluster, sorted by cluster"
    assert reps["index"].is_unique
    assert reps["no_households"].is_monotonic_increasing, "clusters are numbered by the households of their representative"
    for row_label, cluster in zip(reps["index"], reps["clusters"]):
        assert out.loc[row_label, "clusters"] == cluster, "a representative lies in its own cluster"


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda f: f.__name__)
def test_wrapper_contract_on_separated_archetypes(algorithm):
    df = synthetic_clustering_parameters()
    out, reps = algorithm(df_parameters_of_grids=df, list_of_clustering_parameters=PARAMS, n_clusters=4)
    assert_consistent(out, reps, 4)
    assert len(out) == len(df)


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda f: f.__name__)
def test_results_are_deterministic(algorithm):
    first = algorithm(synthetic_clustering_parameters(), PARAMS, 4)
    second = algorithm(synthetic_clustering_parameters(), PARAMS, 4)
    pd.testing.assert_frame_equal(first[0], second[0])
    pd.testing.assert_frame_equal(first[1], second[1])


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda f: f.__name__)
def test_input_frame_gets_a_clusters_column_in_place(algorithm):
    """Documented behaviour: the caller's frame is extended in place (the returned frame is a copy)."""
    df = synthetic_clustering_parameters()
    out, _ = algorithm(df, PARAMS, 4)
    assert "clusters" in df.columns
    assert out is not df


def test_reindex_orders_clusters_by_representative_households():
    df = pd.DataFrame({"clusters": [0, 1, 2, 0, 1, 2], "no_households": [50, 10, 30, 51, 11, 31]})
    reps = df.iloc[[0, 1, 2]]
    out, new_reps = reindex_cluster_indices(df.copy(), reps)
    # representative households were 50, 10, 30 for the old clusters 0, 1, 2 -> new ids 2, 0, 1
    assert list(out["clusters"]) == [2, 0, 1, 2, 0, 1]
    assert list(new_reps["clusters"]) == [0, 1, 2]
    assert list(new_reps["no_households"]) == [10, 30, 50]
    assert list(new_reps["index"]) == [1, 2, 0]


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda f: f.__name__)
def test_nan_in_a_parameter_is_rejected(algorithm):
    df = synthetic_clustering_parameters()
    df.loc[3, "avg_trafo_dis"] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        algorithm(df, PARAMS, 4)


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda f: f.__name__)
def test_more_clusters_than_grids_is_rejected(algorithm):
    with pytest.raises(ValueError):
        algorithm(synthetic_clustering_parameters().head(3), PARAMS, 4)


def test_gmm_representative_lies_in_its_own_component():
    """CLASSIF-02: with seed 12 the grid closest to the mean of component 5 belongs to component 0."""
    df = heavy_tailed(168, seed=12)
    out, reps = gmm_tied_clustering(df, ["a", "b", "c"], 6)
    assert_consistent(out, reps, 6)


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda f: f.__name__)
@pytest.mark.parametrize("seed", range(20))
def test_representatives_are_consistent_on_heavy_tailed_data(algorithm, seed):
    df = heavy_tailed(60, seed)
    out, reps = algorithm(df, ["a", "b", "c"], 4)
    assert_consistent(out, reps, 4)


def test_representative_is_searched_among_the_members_of_the_cluster():
    X = np.array([[2.0], [0.4], [3.0], [3.2]])
    labels = np.array([0, 1, 1, 1])
    # the grid closest to the center of cluster 0 (0.5) is grid 1, but that one belongs to cluster 1
    assert list(representatives_in_clusters(X, labels, np.array([[0.5], [3.0]]))) == [0, 2]


def test_empty_cluster_is_reported():
    with pytest.raises(ValueError, match="Cluster 1 contains no grid"):
        representatives_in_clusters(np.array([[0.0], [1.0]]), np.array([0, 0]), np.array([[0.0], [1.0]]))


def test_clusters_with_equal_households_keep_their_order():
    n = 40
    df = pd.DataFrame({"clusters": np.arange(n), "no_households": [100, 100, 100, 50] * 10})
    out, _ = reindex_cluster_indices(df.copy(), df)
    assert list(out["clusters"]) == list(df["no_households"].rank(method="first").astype(int) - 1)


def test_gmm_mismatch_exists_in_default_setting():
    """Evidence for CLASSIF-02: at k=4 (shipped default) a few percent of heavy-tailed data sets trigger it."""
    from scipy.cluster.vq import vq
    from sklearn import preprocessing
    from sklearn.mixture import GaussianMixture

    bad = 0
    for seed in range(60):
        X = preprocessing.scale(heavy_tailed(120, seed)[["a", "b", "c"]])
        gm = GaussianMixture(n_components=4, covariance_type="tied", random_state=1).fit(X)
        closest, _ = vq(gm.means_, X)
        bad += not (gm.predict(X)[closest] == np.arange(4)).all()
    assert bad >= 1
