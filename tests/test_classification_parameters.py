"""Parameter selection (factor analysis) and number of clusters (CH/DB index) on synthetic grid parameters."""
import os
import warnings

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("matplotlib")  # the selection helpers live in the plotting package (extra "plots")
pytest.importorskip("seaborn")
pytest.importorskip("plotly")
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

from sklearn import preprocessing  # noqa: E402
from sklearn.cluster import KMeans  # noqa: E402
from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score  # noqa: E402

import pylovo.classification.clustering.get_no_clusters_for_clustering as get_no  # noqa: E402
import pylovo.classification.clustering.get_parameters_for_clustering as get_params  # noqa: E402
from classification_helpers import synthetic_clustering_parameters  # noqa: E402
from pylovo.plotting.classification import (  # noqa: E402
    get_parameters_for_clustering as select_parameters,
    plot_ch_index_for_clustering_algos,
    plot_db_index_for_clustering_algos,
)

PARAMS = ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"]


@pytest.fixture(autouse=True)
def _close_figures():
    yield
    import matplotlib.pyplot as plt

    plt.close("all")


class FakeDC:
    frame: pd.DataFrame

    def get_clustering_parameters_for_classification_version(self):
        return FakeDC.frame.copy()


@pytest.fixture()
def fake_dc(monkeypatch):
    monkeypatch.setattr(get_params, "DatabaseCommunication", FakeDC)
    monkeypatch.setattr(get_no, "DatabaseCommunication", FakeDC)
    FakeDC.frame = synthetic_clustering_parameters()
    return FakeDC


def test_selected_parameters_are_candidate_columns_and_plain_strings(fake_dc, capsys):
    selected = get_params.get_parameters_for_clustering()
    candidates = set(fake_dc.frame.columns) - set(get_params.EXCLUDED_COLUMNS)
    assert 1 <= len(selected) <= len(candidates)
    assert set(selected) <= candidates
    assert all(type(name) is str for name in selected), "must be YAML-serialisable"
    assert selected == get_params.get_parameters_for_clustering(), "deterministic"


def test_perfectly_collinear_column_does_not_break_the_selection(fake_dc):
    fake_dc.frame["cable_length_twice"] = fake_dc.frame["cable_length_km"] * 2
    selected = get_params.get_parameters_for_clustering()
    assert not {"cable_length_km", "cable_length_twice"} <= set(selected)


def test_excluded_columns_never_reach_the_factor_analysis(fake_dc):
    for column in ("resistance", "reactance", "ratio", "max_power_mw", "osm_trafo"):
        assert column in get_params.EXCLUDED_COLUMNS
    assert set(get_params.get_parameters_for_clustering()).isdisjoint(get_params.EXCLUDED_COLUMNS)


def test_nan_parameter_is_rejected(fake_dc):
    fake_dc.frame.loc[3, "avg_trafo_dis"] = np.nan
    with pytest.raises(ValueError, match="NaN"):
        get_params.get_parameters_for_clustering()


def test_failing_factor_analysis_names_the_sample_and_the_way_out(fake_dc):
    fake_dc.frame = fake_dc.frame.head(8)  # one archetype: the transformer rating is the same in every grid
    with pytest.raises(ValueError, match=r"Automatic parameter selection failed for 8 grids and \d+ parameters") as exc:
        get_params.get_parameters_for_clustering()
    assert "LIST_OF_CLUSTERING_PARAMETERS" in str(exc.value) and exc.value.__cause__ is not None


def test_small_sample_that_can_be_analysed_is_not_rejected(fake_dc):
    fake_dc.frame = fake_dc.frame.sample(16, random_state=0).reset_index(drop=True)
    assert get_params.get_parameters_for_clustering()


def test_plotting_selector_returns_one_parameter_per_factor():
    frame = synthetic_clustering_parameters()[["no_house_connections", "avg_trafo_dis", "vsw_per_branch", "cable_length_km"]]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        for n in (1, 2, 3):
            assert len(select_parameters(df_plz_parameters=frame, n_comps=n)) == n


# ---------------------------------------------------------------------------------------- number of clusters
ALLOWED = [3, 4, 5, 6, 7]


def test_ch_index_table_contract_and_value():
    frame = synthetic_clustering_parameters()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        table = plot_ch_index_for_clustering_algos(frame, ALLOWED, PARAMS)
    assert list(table.columns) == ["algorithm", "no_clusters", "ch_index"]
    assert list(table["algorithm"]) == ["kmeans", "gmm_full", "gmm_diag", "gmm_tied", "gmm_sph"]
    assert table["no_clusters"].isin(ALLOWED).all() and (table["ch_index"] > 0).all()
    X = preprocessing.scale(frame[PARAMS])
    scores = {k: calinski_harabasz_score(X, KMeans(n_clusters=k, random_state=0).fit(X).labels_) for k in ALLOWED}
    kmeans_row = table[table["algorithm"] == "kmeans"].iloc[0]
    assert kmeans_row["no_clusters"] == max(scores, key=scores.get) and kmeans_row["ch_index"] == pytest.approx(max(scores.values()))


def test_db_index_picks_the_minimum():
    frame = synthetic_clustering_parameters()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        table = plot_db_index_for_clustering_algos(frame, ALLOWED, PARAMS)
    X = preprocessing.scale(frame[PARAMS])
    scores = {k: davies_bouldin_score(X, KMeans(n_clusters=k, random_state=0).fit(X).labels_) for k in ALLOWED}
    kmeans_row = table[table["algorithm"] == "kmeans"].iloc[0]
    assert kmeans_row["no_clusters"] == min(scores, key=scores.get)


def test_get_no_clusters_passes_parameters_and_allowed_numbers_through(fake_dc, monkeypatch):
    seen = {}

    def fake_plot(df_plz_parameters, no_of_clusters_allowed, list_of_clustering_parameters):
        seen.update(allowed=no_of_clusters_allowed, params=list_of_clustering_parameters)
        return pd.DataFrame({"algorithm": ["kmeans"], "no_clusters": [4], "ch_index": [1.0]})

    monkeypatch.setattr(get_no, "plot_ch_index_for_clustering_algos", fake_plot)
    out = get_no.get_no_clusters_for_clustering(["a", "b"])
    assert seen == {"allowed": get_no.NO_OF_CLUSTERS_ALLOWED, "params": ["a", "b"]} and len(out) == 1


def test_algorithm_names_used_by_the_cli_exist_in_the_ch_table():
    """pylovo-classify maps 'kmeans' and 'gmm_tied' to config keys; KMedoids has no row any more."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        names = set(plot_ch_index_for_clustering_algos(synthetic_clustering_parameters(), [3, 4], PARAMS)["algorithm"])
    assert {"kmeans", "gmm_tied"} <= names
    assert not names & {"KMedoids", "kmedoids", "kmedoid"}
