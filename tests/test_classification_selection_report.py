"""The selection report of step 6: the same choices as the pipeline, their margins and their bootstrap shares."""
import json
import logging
import warnings

import pytest

pytest.importorskip("matplotlib")  # the selection uses the factor analysis of the plotting module (extra "plots")

import pylovo.classification.clustering.get_parameters_for_clustering as get_params
import pylovo.classification.clustering.selection_report as sr
import pylovo.classification.database_communication.database_communication as dcm
from classification_helpers import FakeDbc, synthetic_clustering_parameters
from pylovo.classification.clustering.transform import transform_key_figures
from pylovo.plotting.classification import get_parameters_for_clustering as select_parameters
from pylovo.plotting.classification import plot_ch_index_for_clustering_algos

ALLOWED = [3, 4, 5]


@pytest.fixture(scope="module")
def frame():
    return synthetic_clustering_parameters()


@pytest.fixture(scope="module")
def report(frame):
    return sr.selection_report(frame, None, "yeo-johnson", resamples=4, no_of_clusters_allowed=ALLOWED)


@pytest.mark.parametrize("transform", ["none", "yeo-johnson", "median-yeo-johnson"])
def test_the_report_repeats_the_choices_of_the_pipeline(frame, transform):
    candidates = transform_key_figures(get_params.factor_analysis_input(frame), transform)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        picks = select_parameters(candidates, get_params.number_of_factors(candidates))
        ch = plot_ch_index_for_clustering_algos(frame, ALLOWED, picks, transform=transform)
    result = sr.selection_report(frame, None, transform, resamples=0, no_of_clusters_allowed=ALLOWED)
    assert [pick["pick"] for pick in result["picks"]] == picks == result["parameters"]
    assert result["factors"] == get_params.number_of_factors(candidates)
    for name in ("kmeans", "gmm_tied"):
        assert result["clusters"][name]["best_ch"] == ch.set_index("algorithm").at[name, "no_clusters"]
    assert result["bootstrap"] == {"resamples": 0, "failed": 0, "seed": 0}


def test_every_pick_names_its_runner_up_and_margin(report):
    for pick in report["picks"]:
        assert pick["pick"] != pick["runner_up"] and pick["loading"] >= pick["runner_up_loading"]
        assert pick["margin"] == pytest.approx(pick["loading"] - pick["runner_up_loading"], abs=1e-3)
        assert 0 <= pick["bootstrap_share"] <= 1


def test_the_bootstrap_shares_add_up_and_repeat_with_the_seed(frame, report):
    boot = report["bootstrap"]
    assert boot["resamples"] == 4 and sum(boot["factor_counts"].values()) == pytest.approx(1, abs=0.01)
    assert sum(boot["selections"].values()) == pytest.approx(1, abs=0.01)
    assert set(boot["best_k"]) == {"kmeans", "gmm_tied"} and set(boot["best_k"]["kmeans"]["ch"]) <= {"3", "4", "5"}
    assert [r["rank"] for r in boot["eigenvalue_ranges"]] == list(range(1, len(boot["eigenvalue_ranges"]) + 1))
    assert sr.selection_report(frame, None, "yeo-johnson", resamples=4, no_of_clusters_allowed=ALLOWED) == report


def test_a_failing_resample_is_counted_and_the_run_goes_on(frame, report, monkeypatch):
    """S7: a LinAlgError (or ValueError) in one resample costs that resample, not the run after its longest step."""
    import numpy as np

    selection, calls = sr._selection, []

    def flaky(df, transform):
        calls.append(len(df))
        if len(calls) == 3:                                  # the second resample (the first call is the sample)
            raise np.linalg.LinAlgError("Singular matrix")
        return selection(df, transform)

    monkeypatch.setattr(sr, "_selection", flaky)
    result = sr.selection_report(frame, None, "yeo-johnson", resamples=4, no_of_clusters_allowed=ALLOWED)
    boot = result["bootstrap"]
    assert (boot["resamples"], boot["failed"]) == (3, 1) and boot["failures"] == ["resample 2: LinAlgError: Singular matrix"]
    assert any("1 of 4 bootstrap resamples failed" in text for text in result["warnings"])
    assert sum(boot["factor_counts"].values()) == pytest.approx(1, abs=0.01)
    assert "4 PLZ bootstrap" not in sr.format_selection_report(result) and ", 1 failed)" in sr.format_selection_report(result)
    assert result["parameters"] == report["parameters"], "the sample itself is not resampled"


def test_every_close_call_has_a_warning(report):
    text = "\n".join(report["warnings"])
    for pick in report["picks"]:
        assert (f"{pick['pick']} leads {pick['runner_up']}" in text) is (pick["margin"] < sr.LOADING_MARGIN)
    for name, clusters in report["clusters"].items():
        assert (f"{name}: the DB index prefers" in text) is (clusters["best_db"] != clusters["best_ch"])
    eigenvalues, n = report["eigenvalues"], report["factors"]      # the last kept and the first dropped one decide
    assert ("is borderline" in text) is any(abs(eigenvalues[rank - 1] - 1) < sr.EIGENVALUE_MARGIN for rank in (n, n + 1))


def test_the_report_is_json_and_markdown(report):
    assert json.loads(json.dumps(report)) == report
    text = sr.format_selection_report(report, 9)
    assert text.startswith("# Selection report of classification 9\n")
    for heading in ("## Factor analysis", "## Number of clusters", "## Bootstrap", "## Warnings"):
        assert heading in text
    assert all(warning in text for warning in report["warnings"])


def test_the_report_names_the_medians_and_divisors_of_median_yeo_johnson(frame):
    result = sr.selection_report(frame, None, "median-yeo-johnson", resamples=0, no_of_clusters_allowed=ALLOWED)
    candidates = get_params.factor_analysis_input(frame)
    assert result["medians"] == {name: float(candidates[name].median()) for name in candidates}
    assert set(result["divisors"]) == set(candidates) and all(d["value"] > 0 for d in result["divisors"].values())
    assert json.loads(json.dumps(result)) == result
    assert "- Divisors before Yeo-Johnson" in sr.format_selection_report(result)
    assert sr.selection_report(frame, None, "yeo-johnson", resamples=0, no_of_clusters_allowed=ALLOWED)["divisors"] == {}


def test_the_chosen_parameters_set_the_curves(frame):
    result = sr.selection_report(frame, ["avg_trafo_dis", "no_households"], "none", resamples=0,
                                 no_of_clusters_allowed=ALLOWED)
    assert result["parameters"] == ["avg_trafo_dis", "no_households"]


def test_report_selection_prints_writes_stores_and_logs(frame, monkeypatch, tmp_path, capsys):
    class FakeDC:
        classification_id = 9
        stored = None

        def __init__(self):
            self.dbc = type("Dbc", (), {"logger": logging.getLogger("selection-report-test")})()

        def get_classification_choices(self):
            return {"transform": "log1p"}

        def get_clustering_parameters_for_classification_version(self, additional_filtering=None):
            FakeDC.filtering = additional_filtering
            return frame

        def save_selection_report(self, report):
            FakeDC.stored = report

    monkeypatch.setattr(sr, "DatabaseCommunication", FakeDC)
    report = sr.report_selection(["avg_trafo_dis", "no_households"], True, resamples=1, path=tmp_path / "r.md")
    assert FakeDC.stored == report and report["transform"] == "log1p" and FakeDC.filtering is True
    assert (tmp_path / "r.md").read_text() in capsys.readouterr().out


def test_the_report_is_stored_with_the_classification():
    dc = object.__new__(dcm.DatabaseCommunication)
    dc.dbc = FakeDbc()
    dc.save_selection_report({"factors": 3})
    ((query, params),) = dc.dbc.cur.executed
    assert query == ("UPDATE pylovo.classification_version SET selection_report = %(r)s::jsonb "
                     "WHERE classification_id = %(c)s")
    assert json.loads(params["r"]) == {"factors": 3} and params["c"] == dcm.CLASSIFICATION_VERSION
    assert dc.dbc.conn.commits == 1
