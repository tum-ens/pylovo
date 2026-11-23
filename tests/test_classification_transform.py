"""The transform of the key figures before the factor analysis and the clustering (no database)."""
import numpy as np
import pandas as pd
import pytest
from scipy.stats import skew
from sklearn import preprocessing
from sklearn.cluster import KMeans

from classification_helpers import synthetic_clustering_parameters
from pylovo.classification.clustering import clustering_algorithms
from pylovo.classification.clustering.transform import transform_key_figures
from pylovo.config_loader import TRANSFORMS

PARAMS = ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"]


def long_tailed() -> pd.DataFrame:
    rng = np.random.default_rng(0)
    return pd.DataFrame({"km": rng.lognormal(-1.5, 0.8, 2000), "count": rng.lognormal(3, 0.8, 2000).round(),
                         "zeros": np.r_[np.zeros(200), rng.lognormal(0, 1, 1800)]})


def test_none_and_log1p_keep_index_and_columns():
    frame = long_tailed()
    assert transform_key_figures(frame, "none").equals(frame.astype(float))
    log = transform_key_figures(frame, "log1p")
    assert list(log.columns) == list(frame.columns) and log.index.equals(frame.index)
    assert np.allclose(log, np.log1p(frame))


def test_yeo_johnson_makes_every_key_figure_close_to_symmetric_and_keeps_its_exponents():
    frame = long_tailed()
    out = transform_key_figures(frame, "yeo-johnson")
    assert (abs(skew(frame)) > 1.5).all() and (abs(skew(out)) < 0.3).all()
    assert set(out.attrs["lambdas"]) == set(frame.columns)
    # log1p leaves a figure in km nearly as skewed as it was, the reason for the default
    assert abs(skew(transform_key_figures(frame, "log1p")["km"])) > 0.5 * abs(skew(frame["km"]))


def test_median_yeo_johnson_is_free_of_the_unit_of_a_key_figure():
    """S6: Yeo-Johnson of x >= 0 is Box-Cox of x + 1 and changes with the unit; divided by the median first, it does not."""
    frame = long_tailed()
    metres = frame.assign(km=frame["km"] * 1000)
    plain = transform_key_figures(frame, "yeo-johnson"), transform_key_figures(metres, "yeo-johnson")
    assert abs(plain[0].attrs["lambdas"]["km"] - plain[1].attrs["lambdas"]["km"]) > 0.3
    scaled = transform_key_figures(frame, "median-yeo-johnson"), transform_key_figures(metres, "median-yeo-johnson")
    assert np.allclose(scaled[0], scaled[1], rtol=1e-6, atol=1e-9)
    assert scaled[0].attrs["lambdas"]["km"] == pytest.approx(scaled[1].attrs["lambdas"]["km"], rel=1e-6)
    assert scaled[1].attrs["medians"]["km"] == pytest.approx(1000 * scaled[0].attrs["medians"]["km"])
    assert (abs(skew(scaled[0])) < 0.3).all()


def test_a_key_figure_with_median_zero_is_divided_by_its_mean():
    from pylovo.classification.clustering.transform import median_divisors

    frame = pd.DataFrame({"mostly_zero": [0, 0, 0, 2, 6], "all_zero": [0.0] * 5, "plain": [1, 2, 3, 4, 50]})
    medians, divisors, rules = median_divisors(frame)
    assert medians == {"mostly_zero": 0.0, "all_zero": 0.0, "plain": 3.0}
    assert divisors == {"mostly_zero": pytest.approx(1.6), "all_zero": 1.0, "plain": 3.0}
    assert rules == {"mostly_zero": "mean", "all_zero": "one", "plain": "median"}
    out = transform_key_figures(frame, "median-yeo-johnson")
    assert out.attrs["divisor_rules"] == rules and np.isfinite(out.to_numpy()).all()


def test_an_unknown_transform_is_rejected():
    assert TRANSFORMS == ("none", "log1p", "yeo-johnson", "median-yeo-johnson")
    with pytest.raises(ValueError, match="Unknown transform 'rank', use one of: none, log1p, yeo-johnson, median-yeo-johnson"):
        transform_key_figures(long_tailed(), "rank")


@pytest.mark.parametrize("transform", ["none", "log1p", "yeo-johnson", "median-yeo-johnson"])
def test_kmeans_clusters_the_transformed_and_standardised_parameters(transform):
    frame = synthetic_clustering_parameters()
    out, _ = clustering_algorithms.kmeans_clustering(frame.copy(), PARAMS, 4, transform=transform)
    X = preprocessing.scale(transform_key_figures(frame[PARAMS], transform))
    expected = KMeans(n_clusters=4, random_state=0, n_init=10).fit(X).labels_
    assert pd.crosstab(out["clusters"], expected).gt(0).sum(axis=1).eq(1).all(), "the same partition, renumbered"


def test_without_a_transform_the_configured_one_is_used(monkeypatch):
    seen = []
    monkeypatch.setattr(clustering_algorithms, "transform_key_figures", lambda df, transform: seen.append(transform) or df)
    monkeypatch.setattr(clustering_algorithms, "TRANSFORM", "log1p")
    clustering_algorithms.gmm_tied_clustering(synthetic_clustering_parameters(), PARAMS, 3)
    assert seen == ["log1p"]
