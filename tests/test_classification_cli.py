"""The interactive ``pylovo-classify`` flow with scripted answers, temporary config copies and stubbed pipeline steps."""
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

pytest.importorskip("matplotlib")  # pylovo-classify imports the plotting stack (extra "plots")
pytest.importorskip("seaborn")
pytest.importorskip("plotly")

import pylovo.cli.classify as cl  # noqa: E402

REPO_CONFIG = Path(cl.CONFIG_CLUSTERING_PATH).parent


@pytest.fixture()
def config_copy(tmp_path, monkeypatch):
    """Work on copies: save_yaml rewrites the config files."""
    for name in ("config_classification.yaml", "config_clustering.yaml"):
        shutil.copy(REPO_CONFIG / name, tmp_path / name)
    monkeypatch.setattr(cl, "CONFIG_CLASSIFICATION_PATH", str(tmp_path / "config_classification.yaml"))
    monkeypatch.setattr(cl, "CONFIG_CLUSTERING_PATH", str(tmp_path / "config_clustering.yaml"))
    return tmp_path


def answers(monkeypatch, *values):
    queue = iter(values)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(queue))


@pytest.fixture()
def pipeline(monkeypatch, config_copy):
    """Stub the long-running steps and record what the CLI hands to them."""
    calls = {}
    monkeypatch.setattr(sys, "argv", ["pylovo-classify"])
    monkeypatch.setattr(cl, "classification_version_exists", lambda: False)
    monkeypatch.setattr(cl, "prepare_data_for_clustering", lambda **kw: calls.__setitem__("prepare", kw) or [80002])
    monkeypatch.setattr(cl, "report_grid_counts", lambda **kw: calls.__setitem__("report", kw))

    def select_parameters(additional_filtering=None):
        calls["parameters_filtering"] = additional_filtering
        return ["no_branches", "avg_trafo_dis"]

    def select_clusters(params=None, additional_filtering=None):
        calls["clusters_filtering"] = additional_filtering
        return pd.DataFrame({"algorithm": ["kmeans", "gmm_full", "gmm_tied"], "no_clusters": [5, 3, 6],
                             "ch_index": [1.0, 1.0, 1.0]})

    monkeypatch.setattr(cl, "get_parameters_for_clustering", select_parameters)
    monkeypatch.setattr(cl, "get_no_clusters_for_clustering", select_clusters)
    monkeypatch.setattr(cl, "apply_clustering_for_visualisation", lambda **kw: calls.__setitem__("apply", kw))
    calls["config"] = config_copy / "config_clustering.yaml"
    return calls


def test_automatic_path_writes_the_computed_values_and_passes_them_on(monkeypatch, pipeline, capsys):
    answers(monkeypatch, "yes", "no", "no")
    cl.main()
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": False}
    assert pipeline["apply"] == {"list_of_clustering_parameters": ["no_branches", "avg_trafo_dis"],
                                 "n_clusters_kmeans": 5, "n_clusters_gmm": 6, "additional_filtering": True}
    assert pipeline["parameters_filtering"] is True and pipeline["clusters_filtering"] is True
    assert pipeline["report"] == {"additional_filtering": True, "plz_without_grids": [80002]}, "the summary closes the run"
    saved = yaml.safe_load(pipeline["config"].read_text())
    assert saved["LIST_OF_CLUSTERING_PARAMETERS"] == ["no_branches", "avg_trafo_dis"]
    assert (saved["N_CLUSTERS_KMEANS"], saved["N_CLUSTERS_GMM"]) == (5, 6)
    assert saved["ADDITIONAL_FILTERING"] is True, "the filters are applied when the grids are read: the answer is stored"
    assert "CLUSTERING_PARAMETERS" in saved and "THRESHOLD_MAX_TRAFO_DIS" in saved, "other keys survive the rewrite"
    assert "N_CLUSTERS_KMEDOID" not in saved
    assert "kmedoid" not in capsys.readouterr().out.lower(), "no warning about the removed KMedoids"


def test_manual_path_takes_parameters_and_two_cluster_numbers(monkeypatch, pipeline):
    answers(monkeypatch, "no", "yes", "yes", "no_branches, max_no_of_households_of_a_branch", "4,5")
    cl.main()
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": True}
    assert pipeline["apply"] == {"list_of_clustering_parameters": ["no_branches", "max_no_of_households_of_a_branch"],
                                 "n_clusters_kmeans": 4, "n_clusters_gmm": 5, "additional_filtering": False}
    saved = yaml.safe_load(pipeline["config"].read_text())
    assert (saved["N_CLUSTERS_KMEANS"], saved["N_CLUSTERS_GMM"]) == (4, 5) and "N_CLUSTERS_KMEDOID" not in saved


def test_all_three_questions_accept_the_short_answers(monkeypatch, pipeline):
    answers(monkeypatch, "y", "n", "N")
    cl.main()
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": False}
    assert pipeline["apply"]["additional_filtering"] is True


def test_confirmation_loops_until_yes_or_no(monkeypatch, capsys):
    answers(monkeypatch, "maybe", "", "YES ")
    assert cl.ask_yes_no("Really?") is True
    assert capsys.readouterr().out.count("Invalid input") == 2
    answers(monkeypatch, "Y")
    assert cl.ask_yes_no("Really?") is True
    answers(monkeypatch, "n")
    assert cl.ask_yes_no("Really?") is False
    answers(monkeypatch, " No ")
    assert cl.ask_yes_no("Really?") is False


def test_existing_classification_version_stops_the_run_before_any_question(monkeypatch, pipeline):
    monkeypatch.setattr(cl, "classification_version_exists", lambda: True)

    def no_question(prompt=""):
        raise AssertionError(f"nothing may be asked: {prompt}")

    monkeypatch.setattr("builtins.input", no_question)
    with pytest.raises(SystemExit) as exc:
        cl.main()
    assert "already exists" in str(exc.value) and "CLASSIFICATION_VERSION" in str(exc.value)
    assert "prepare" not in pipeline


def test_closed_standard_input_ends_the_run_with_a_message(monkeypatch, pipeline):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    with pytest.raises(SystemExit) as exc:
        cl.main()
    assert "No more input" in str(exc.value)
    assert "prepare" not in pipeline


def test_custom_cluster_numbers_validation(monkeypatch, config_copy, capsys):
    answers(monkeypatch, "4", "3,4,5", "4,9", "a,b", "3,7")
    assert cl.get_custom_cluster_numbers() == [3, 7]
    assert capsys.readouterr().out.count("Invalid input") == 4


def test_custom_parameters_reject_empty_input(monkeypatch, capsys):
    answers(monkeypatch, " , ", "no_branches,avg_trafo_dis")
    assert cl.get_custom_clustering_parameters() == ["no_branches", "avg_trafo_dis"]
    assert capsys.readouterr().out.count("Invalid input") == 1


def test_unknown_parameter_names_are_rejected(monkeypatch, capsys):
    answers(monkeypatch, "no_branchs,avg_trafo_dis", "no_branches,avg_trafo_dis")
    assert cl.get_custom_clustering_parameters() == ["no_branches", "avg_trafo_dis"]
    assert "Unknown parameter(s): no_branchs" in capsys.readouterr().out


def test_identifier_columns_are_no_clustering_parameters(monkeypatch):
    answers(monkeypatch, "plz,avg_trafo_dis", "kcid", "avg_trafo_dis")
    assert cl.get_custom_clustering_parameters() == ["avg_trafo_dis"]


def test_shipped_clustering_config_declares_additional_filtering_off():
    from pylovo import config_loader

    assert yaml.safe_load((REPO_CONFIG / "config_clustering.yaml").read_text())["ADDITIONAL_FILTERING"] is False
    assert config_loader.ADDITIONAL_FILTERING is False


def test_shipped_clustering_config_has_no_kmedoid_key():
    """The KMedoids clustering was removed: neither the shipped file nor the loader mention its cluster number."""
    from pylovo import config_loader

    assert "N_CLUSTERS_KMEDOID" not in yaml.safe_load((REPO_CONFIG / "config_clustering.yaml").read_text())
    assert not hasattr(config_loader, "N_CLUSTERS_KMEDOID")


def test_config_file_is_the_first_match_of_the_search_paths(tmp_path, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    for folder in (first, second):
        folder.mkdir()
        (folder / "x.yaml").write_text("a: 1")
    monkeypatch.setattr(cl, "get_config_search_paths", lambda: [tmp_path / "missing", first, second])
    assert cl._config_file("x.yaml") == str(first / "x.yaml")
    with pytest.raises(FileNotFoundError):
        cl._config_file("absent.yaml")


def test_help_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pylovo-classify", "--help"])
    with pytest.raises(SystemExit) as exc:
        cl.main()
    assert exc.value.code == 0 and "usage: pylovo-classify" in capsys.readouterr().out


@pytest.mark.xfail(strict=True, reason="CLASSIF-10 (c): save_yaml rewrites the file without the user's comments (decision pending)")
def test_saving_the_config_keeps_comments(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("# my note\nN_CLUSTERS_GMM: 4  # tied\n")
    cl.save_yaml(path, cl.load_yaml(path))
    assert "# my note" in path.read_text() and "# tied" in path.read_text()
