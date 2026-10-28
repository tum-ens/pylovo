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
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": False, "parallel": None}
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
    out = capsys.readouterr().out
    assert "kmedoid" not in out.lower(), "no warning about the removed KMedoids"
    assert "Selected clustering parameters: no_branches, avg_trafo_dis" in out
    assert "gmm_tied" in out and "ch_index" in out, "the Calinski-Harabasz table is printed"
    assert "N_CLUSTERS_KMEANS=5" in out and "N_CLUSTERS_GMM=6" in out, "and the chosen numbers"


def test_manual_path_takes_parameters_and_two_cluster_numbers(monkeypatch, pipeline):
    answers(monkeypatch, "no", "yes", "yes", "no_branches, max_no_of_households_of_a_branch", "4,5")
    cl.main()
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": True, "parallel": None}
    assert pipeline["apply"] == {"list_of_clustering_parameters": ["no_branches", "max_no_of_households_of_a_branch"],
                                 "n_clusters_kmeans": 4, "n_clusters_gmm": 5, "additional_filtering": False}
    saved = yaml.safe_load(pipeline["config"].read_text())
    assert (saved["N_CLUSTERS_KMEANS"], saved["N_CLUSTERS_GMM"]) == (4, 5) and "N_CLUSTERS_KMEDOID" not in saved


def test_all_three_questions_accept_the_short_answers(monkeypatch, pipeline):
    answers(monkeypatch, "y", "n", "N")
    cl.main()
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": False, "parallel": None}
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


def test_saving_the_config_keeps_comments(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("# my note\nN_CLUSTERS_GMM: 4  # tied\n")
    cl.save_yaml(path, cl.load_yaml(path))
    assert "# my note" in path.read_text() and "# tied" in path.read_text()


# ---------------------------------------------------------------------------------------- save_yaml: in-place edit
EXAMPLE = """# my note on the parameters
LIST_OF_CLUSTERING_PARAMETERS:  # chosen by hand
  # first the size
  - no_branches
  - avg_trafo_dis

# clusters
N_CLUSTERS_KMEANS: 4  # elbow
N_CLUSTERS_GMM: 4  # tied
OTHER: [a, b]  # flow style
LAST: 1
"""


def saved(tmp_path, text, **changes):
    path = tmp_path / "c.yaml"
    path.write_text(text)
    data = cl.load_yaml(path)
    data.update(changes)
    cl.save_yaml(path, data)
    return path.read_text()


def test_an_unchanged_config_is_not_touched(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(EXAMPLE)
    before = path.stat().st_mtime_ns
    cl.save_yaml(path, cl.load_yaml(path))
    assert path.read_text() == EXAMPLE and path.stat().st_mtime_ns == before


def test_a_changed_scalar_keeps_its_comment_and_every_other_line(tmp_path):
    text = saved(tmp_path, EXAMPLE, N_CLUSTERS_GMM=5)
    assert text == EXAMPLE.replace("N_CLUSTERS_GMM: 4  # tied", "N_CLUSTERS_GMM: 5  # tied")


def test_a_changed_list_keeps_the_key_comment_the_indentation_and_the_text_around_it(tmp_path):
    text = saved(tmp_path, EXAMPLE, LIST_OF_CLUSTERING_PARAMETERS=["no_households", "vsw_per_branch", "max_trafo_dis"])
    assert text.startswith("# my note on the parameters\nLIST_OF_CLUSTERING_PARAMETERS:  # chosen by hand\n"
                           "  - no_households\n  - vsw_per_branch\n  - max_trafo_dis\n\n# clusters\nN_CLUSTERS_KMEANS: 4  # elbow\n")
    assert text.endswith("OTHER: [a, b]  # flow style\nLAST: 1\n") and "first the size" not in text


def test_a_list_without_indentation_stays_without(tmp_path):
    text = saved(tmp_path, "A:\n- x\nB: 1\n", A=["y", "z"])
    assert text == "A:\n- y\n- z\nB: 1\n"


def test_a_flow_style_value_is_replaced_and_keeps_its_comment(tmp_path):
    text = saved(tmp_path, EXAMPLE, OTHER=["c"])
    assert "OTHER:  # flow style\n- c\nLAST: 1\n" in text
    assert yaml.safe_load(text)["OTHER"] == ["c"] and yaml.safe_load(text)["LAST"] == 1


def test_a_key_that_is_missing_is_appended(tmp_path):
    assert saved(tmp_path, "A: 1", B=True) == "A: 1\nB: true\n"
    assert saved(tmp_path, "A: 1\n", B=[1, 2]) == "A: 1\nB:\n- 1\n- 2\n"


def test_a_quoted_key_raises_and_leaves_the_file_alone(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text('"A": 1\nB: 2\n')
    with pytest.raises(ValueError, match="not a plain top-level key"):
        cl.save_yaml(path, {"A": 3})
    assert path.read_text() == '"A": 1\nB: 2\n'


def test_an_unsupported_layout_raises_and_leaves_the_file_alone(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("A:\n  nested: 1\nB: 2\n")
    with pytest.raises(ValueError, match="unsupported layout"):
        cl.save_yaml(path, {"A": 3})
    assert path.read_text() == "A:\n  nested: 1\nB: 2\n"


def test_the_shipped_config_keeps_its_comments_when_the_three_values_and_the_filter_choice_are_written(config_copy):
    path = config_copy / "config_clustering.yaml"
    before = path.read_text()
    data = cl.load_yaml(path)
    data.update(LIST_OF_CLUSTERING_PARAMETERS=["no_branches", "avg_trafo_dis"], N_CLUSTERS_KMEANS=6, N_CLUSTERS_GMM=3,
                ADDITIONAL_FILTERING=True)
    cl.save_yaml(path, data)
    after = path.read_text()
    assert yaml.safe_load(after) == data
    assert [line for line in after.splitlines() if line.startswith("#")] == [line for line in before.splitlines() if line.startswith("#")]
    assert after.count("\n") == before.count("\n") - 1, "one list item fewer, nothing else"


# ---------------------------------------------------------------------------------------- scripted runs
def no_question(prompt=""):
    raise AssertionError(f"nothing may be asked: {prompt}")


def run_with(monkeypatch, *flags):
    monkeypatch.setattr(sys, "argv", ["pylovo-classify", *flags])
    monkeypatch.setattr("builtins.input", no_question)
    cl.main()


def test_a_fully_scripted_manual_run_asks_nothing(monkeypatch, pipeline):
    run_with(monkeypatch, "--no-additional-filtering", "--restrict-to-generated", "--manual",
             "--parameters", "no_branches,avg_trafo_dis", "--clusters", "4,5")
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": True, "parallel": None}
    assert pipeline["apply"] == {"list_of_clustering_parameters": ["no_branches", "avg_trafo_dis"],
                                 "n_clusters_kmeans": 4, "n_clusters_gmm": 5, "additional_filtering": False}
    saved_config = yaml.safe_load(pipeline["config"].read_text())
    assert saved_config["LIST_OF_CLUSTERING_PARAMETERS"] == ["no_branches", "avg_trafo_dis"]
    assert (saved_config["N_CLUSTERS_KMEANS"], saved_config["N_CLUSTERS_GMM"], saved_config["ADDITIONAL_FILTERING"]) == (4, 5, False)
    assert pipeline["report"] == {"additional_filtering": False, "plz_without_grids": [80002]}


def test_a_fully_scripted_automatic_run_asks_nothing_and_prints_the_choice(monkeypatch, pipeline, capsys):
    run_with(monkeypatch, "--additional-filtering", "--no-restrict-to-generated", "--auto")
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": False, "parallel": None}
    assert pipeline["apply"]["additional_filtering"] is True and pipeline["apply"]["n_clusters_gmm"] == 6
    assert pipeline["parameters_filtering"] is True and pipeline["clusters_filtering"] is True
    assert "N_CLUSTERS_KMEANS=5" in capsys.readouterr().out


def test_options_answer_only_their_question(monkeypatch, pipeline):
    answers(monkeypatch, "yes")  # the remaining question: restrict to postcode_result
    monkeypatch.setattr(sys, "argv", ["pylovo-classify", "--no-additional-filtering", "--auto"])
    cl.main()
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": True, "parallel": None}
    assert pipeline["apply"]["additional_filtering"] is False


@pytest.mark.parametrize("flags, message", [
    (["--parameters", "no_branchs", "--clusters", "4,5"], "Unknown parameter(s): no_branchs"),
    (["--parameters", "no_branches", "--clusters", "4,9"], "NO_OF_CLUSTERS_ALLOWED"),
    (["--parameters", "no_branches", "--clusters", "4"], "exactly 2"),
    (["--parameters", "no_branches", "--clusters", "a,b"], "numeric"),
    (["--parameters", "plz", "--clusters", "4,5"], "Unknown parameter(s): plz"),
])
def test_scripted_values_are_checked_before_anything_runs(monkeypatch, pipeline, capsys, flags, message):
    """A typo must stop the command before the version check and the hours of grid generation: a usage error."""
    monkeypatch.setattr(cl, "classification_version_exists", no_question)
    with pytest.raises(SystemExit) as exc:
        run_with(monkeypatch, "--manual", *flags)
    err = capsys.readouterr().err
    assert exc.value.code == 2 and message in err and "usage: pylovo-classify" in err and "prepare" not in pipeline


@pytest.mark.parametrize("flags, message", [
    (["--manual"], "--manual needs --parameters and --clusters"),
    (["--manual", "--parameters", "no_branches"], "--manual needs --parameters and --clusters"),
    (["--parameters", "no_branches"], "need --manual"),
    (["--clusters", "4,5"], "need --manual"),
    (["--auto", "--manual", "--parameters", "no_branches", "--clusters", "4,5"], "not allowed with"),
])
def test_inconsistent_options_are_rejected_by_the_parser(monkeypatch, pipeline, capsys, flags, message):
    with pytest.raises(SystemExit) as exc:
        run_with(monkeypatch, *flags)
    assert exc.value.code == 2 and message in capsys.readouterr().err and "prepare" not in pipeline


def test_help_lists_the_options(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pylovo-classify", "--help"])
    with pytest.raises(SystemExit):
        cl.main()
    out = capsys.readouterr().out
    for option in ("--additional-filtering", "--no-additional-filtering", "--restrict-to-generated",
                   "--no-restrict-to-generated", "--auto", "--manual", "--parameters", "--clusters", "--parallel",
                   "--no-parallel"):
        assert option in out


@pytest.mark.parametrize("flag, parallel", [("--parallel", True), ("--no-parallel", False)])
def test_the_parallel_option_reaches_the_generation_and_the_key_figures(monkeypatch, pipeline, flag, parallel):
    run_with(monkeypatch, "--no-additional-filtering", "--restrict-to-generated", "--auto", flag)
    assert pipeline["prepare"] == {"sample_from_postcode_result_only": True, "parallel": parallel}


def test_parallel_and_no_parallel_exclude_each_other(monkeypatch, pipeline, capsys):
    monkeypatch.setattr(sys, "argv", ["pylovo-classify", "--parallel", "--no-parallel"])
    with pytest.raises(SystemExit) as exc:
        cl.main()
    assert exc.value.code == 2 and "not allowed with argument" in capsys.readouterr().err and "prepare" not in pipeline
