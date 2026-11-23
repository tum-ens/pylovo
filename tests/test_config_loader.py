"""Checks of the generation configuration at load time (no database)."""
import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from pylovo import config_loader
from pylovo.config_loader import (ConfigFile, MissingConfigKey, _check_cable_ampacities, _check_key_figure_names,
                                  _check_transformer_mapping, _consumer_categories_from_config, _version_id,
                                  _warn_about_defaulted_keys, _warn_about_former_generation_config, check_keys)


def categories(sim_factor) -> dict:
    return {"CONSUMER_CATEGORIES": [
        {"consumer_category_id": 3, "definition": "Residential", "peak_load": 16.8, "sim_factor": 0.07},
        {"consumer_category_id": 1, "definition": "Commercial", "peak_load": 30.0, "sim_factor": sim_factor},
    ]}


@pytest.mark.parametrize("sim_factor", [None, float("nan"), float("inf"), -0.1, 1.5])
def test_sim_factor_must_be_a_number_from_0_to_1(sim_factor):
    with pytest.raises(ValueError, match=r"sim_factor must be a number from 0 to 1.*Commercial"):
        _consumer_categories_from_config(categories(sim_factor), 16.8)


@pytest.mark.parametrize("sim_factor", [0, 0.5, 1])
def test_valid_sim_factors_are_kept(sim_factor):
    assert _consumer_categories_from_config(categories(sim_factor), 16.8)["sim_factor"].iloc[1] == sim_factor


def cables(*max_i_a) -> pd.DataFrame:
    return pd.DataFrame([{"name": f"C{i}", "max_i_a": value} for i, value in enumerate(max_i_a)])


@pytest.mark.parametrize("ampacities", [(0,), (142, 0), (-5,), (142, None), (142, np.nan)])
def test_every_cable_needs_an_ampacity_above_zero(ampacities):
    with pytest.raises(ValueError, match=r"FEEDER_CABLES: max_i_a must be a number above 0, check \['C"):
        _check_cable_ampacities(cables(*ampacities), "FEEDER_CABLES")


def test_a_catalogue_without_ampacity_is_named():
    with pytest.raises(ValueError, match="CONSUMER_CONNECTION_CABLES: max_i_a is missing"):
        _check_cable_ampacities(pd.DataFrame([{"name": "C0"}]), "CONSUMER_CONNECTION_CABLES")


def test_valid_cables_pass():
    _check_cable_ampacities(cables(142, 270), "FEEDER_CABLES")


def test_the_former_names_of_renamed_key_figures_are_rejected():
    _check_key_figure_names({"CLUSTERING_PARAMETERS": ["no_household_equ_per_branch"],
                             "LIST_OF_CLUSTERING_PARAMETERS": ["avg_trafo_dis"]})
    with pytest.raises(ValueError, match="LIST_OF_CLUSTERING_PARAMETERS of config_clustering.yaml lists renamed key "
                                         "figures: max_no_of_households_of_a_branch is now max_household_equ_of_a_branch"):
        _check_key_figure_names({"CLUSTERING_PARAMETERS": [],
                                 "LIST_OF_CLUSTERING_PARAMETERS": ["avg_trafo_dis", "max_no_of_households_of_a_branch"]})


def test_transformer_mapping_needs_ratings_of_the_catalogue():
    transformers = pd.DataFrame({"s_max_kva": [250, 400, 630]})
    _check_transformer_mapping({1: [250, 400], 2: [400, 630]}, transformers)
    with pytest.raises(ValueError, match=r"TRANSFORMER_MAPPING\[2\] lists \[500\] kVA, which TRANSFORMERS does not define"):
        _check_transformer_mapping({1: [250], 2: [400, 500]}, transformers)
    with pytest.raises(ValueError, match=r"TRANSFORMER_MAPPING\[3\] must be a list of ratings"):
        _check_transformer_mapping({3: []}, transformers)


def test_the_shipped_configuration_passes_its_checks():
    _check_cable_ampacities(config_loader.FEEDER_CABLES, "FEEDER_CABLES")
    _check_cable_ampacities(config_loader.CONSUMER_CONNECTION_CABLES, "CONSUMER_CONNECTION_CABLES")
    _check_transformer_mapping(config_loader.TRANSFORMER_MAPPING, config_loader.TRANSFORMERS)
    assert config_loader.SIM_FACTOR["Residential"] == 0.07


@pytest.mark.parametrize("value, expected", [("1", "1"), (2, "2"), ("a5", "a5"), ("a2-all", "a2-all"), ("v1.0_x", "v1.0_x"),
                                              ("0123456789", "0123456789")])
def test_version_id_is_text(value, expected):
    assert _version_id(value) == expected


@pytest.mark.parametrize("value", [None, True, 1.5, [1], "", "12345678901", "a b", "a/b", "ä"])
def test_invalid_version_ids_are_refused_with_the_key_named(value):
    with pytest.raises(ValueError, match="VERSION_ID must be"):
        _version_id(value)


def test_an_unquoted_numeric_version_id_loads_as_text(tmp_path):
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    config = tmp_path / "config" / "config_generation_lv.yaml"
    config.write_text(config.read_text().replace('VERSION_ID: "1"', "VERSION_ID: 2", 1))
    proc = subprocess.run([sys.executable, "-c", "import pylovo.config_loader as c; print(repr(c.VERSION_ID))"],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().splitlines()[-1] == "'2'"


LV, MV, FORMER = "config_generation_lv.yaml", "config_generation_mv.yaml", "config_generation.yaml"


def folder_with(tmp_path, name, *files) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    for file in files:
        (folder / file).write_text("")
    return folder


def warnings_for(config_file: Path, folders: list[Path]) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _warn_about_former_generation_config(config_file, folders)
    return [str(w.message) for w in caught]


def test_only_the_former_file_is_read_with_a_warning(tmp_path):
    here = folder_with(tmp_path, "here", FORMER)
    messages = warnings_for(here / FORMER, [here])
    assert len(messages) == 1
    assert "is the former name of config_generation_lv.yaml" in messages[0] and "config_generation_mv.yaml" in messages[0]


def test_a_former_file_in_a_folder_of_higher_priority_hides_the_current_files(tmp_path):
    first = folder_with(tmp_path, "first", FORMER)
    second = folder_with(tmp_path, "second", LV, MV)
    messages = warnings_for(first / FORMER, [first, second])
    assert len(messages) == 2 and "former name" in messages[0]
    assert str(second / LV) in messages[1] and str(second / MV) in messages[1] and "hides" in messages[1]


def test_the_former_file_is_not_reported_as_hiding_its_own_folder_or_a_resolved_alias(tmp_path):
    first = folder_with(tmp_path, "first", FORMER, MV)
    alias = tmp_path / "alias"
    alias.symlink_to(first)
    assert len(warnings_for(first / FORMER, [first, alias])) == 1          # only the former name


def test_both_names_in_one_folder_warn_that_the_former_one_is_not_read(tmp_path):
    here = folder_with(tmp_path, "here", LV, FORMER)
    messages = warnings_for(here / LV, [here])
    assert len(messages) == 1 and "is not read" in messages[0] and str(here / FORMER) in messages[0]


def test_current_files_give_no_warning_even_if_a_former_file_follows_in_another_folder(tmp_path):
    first = folder_with(tmp_path, "first", LV, MV)
    second = folder_with(tmp_path, "second", FORMER)
    assert warnings_for(first / LV, [first, second]) == []


# --- default values and unknown keys ---------------------------------------------------------------------------------
def messages_of(action) -> list[str]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        action()
    return [str(w.message) for w in caught]


def test_a_key_that_the_file_does_not_have_takes_the_value_of_the_default_configuration():
    config = ConfigFile(Path("project/lv.yaml"), {"VN": 230, "A": None},
                        {"VN": 400, "A": 1, "TRANSFORMER_MAPPING": {1: [250]}}, Path("shipped/lv.yaml"))
    assert config["VN"] == 230 and config["A"] is None           # the file wins, also with an explicit null
    assert config["TRANSFORMER_MAPPING"] == {1: [250]} and config.get("TRANSFORMER_MAPPING") == {1: [250]}
    config["TRANSFORMER_MAPPING"][1].append(400)                  # a copy: the defaults stay as shipped
    assert config.defaults["TRANSFORMER_MAPPING"] == {1: [250]}
    assert "TRANSFORMER_MAPPING" not in config and set(config) == {"VN", "A"}      # "in" asks the file alone
    assert config.defaulted == {"TRANSFORMER_MAPPING"}
    (message,) = messages_of(lambda: _warn_about_defaulted_keys(config))
    assert "project/lv.yaml does not have TRANSFORMER_MAPPING" in message and "shipped/lv.yaml" in message


def test_a_key_in_neither_file_is_an_error_that_names_both_files():
    config = ConfigFile(Path("project/lv.yaml"), {}, {"VN": 400}, Path("shipped/lv.yaml"))
    with pytest.raises(MissingConfigKey, match="VM is missing in project/lv.yaml and in pylovo's default "
                                               "configuration shipped/lv.yaml"):
        config["VM"]
    assert config.get("VM") is None and config.get("VM", 1) == 1
    with pytest.raises(KeyError, match="VN is missing in lv.yaml$"):          # the default file itself: no fallback
        ConfigFile(Path("lv.yaml"), {})["VN"]


def test_a_file_without_missing_keys_gives_no_warning():
    config = ConfigFile(Path("lv.yaml"), {"VN": 400}, {"VN": 400}, Path("shipped/lv.yaml"))
    config["VN"]
    assert messages_of(lambda: _warn_about_defaulted_keys(config)) == []


def test_an_unknown_key_is_an_error_with_the_closest_known_name():
    config = ConfigFile(Path("config/config_generation_lv.yaml"),
                        {"HOUSE_CONECTION": "cabinet", "OLD_KEY": 1, "VN": 400, "FEEDER": 1, "N_JOBS_PERCENT": 50},
                        {"HOUSE_CONNECTION": "joint"}, Path("shipped/lv.yaml"))
    config["VN"]                                     # every kind of look-up counts as a read
    config.get("N_JOBS_PERCENT")
    assert "FEEDER" in config
    with pytest.raises(ValueError) as error:
        check_keys(config)
    message = str(error.value)
    assert "config/config_generation_lv.yaml has keys that pylovo does not know" in message
    assert "HOUSE_CONECTION (did you mean HOUSE_CONNECTION?), OLD_KEY." in message
    assert "VN" not in message and "FEEDER" not in message


def test_a_key_of_an_earlier_version_gives_a_warning_only():
    config = ConfigFile(Path("lv.yaml"), {"BROWNFIELD_NEAREST_ONLY": True, "NETWORK_COLORS": {}, "VN": 400})
    config["VN"]
    (message,) = messages_of(lambda: check_keys(config))
    assert "lv.yaml has keys of earlier pylovo releases that are no longer read" in message
    assert "BROWNFIELD_NEAREST_ONLY (until 1.2.0), NETWORK_COLORS (until 1.2.0)" in message


def test_keys_that_are_read_some_other_way_are_known():
    config = ConfigFile(Path("lv.yaml"), {"MV_STANDARD_CABLE": "a", "VN": 400})
    config["VN"]
    check_keys(config, {"MV_STANDARD_CABLE", "MV_VARIANT_ID"})
    with pytest.raises(ValueError, match="MV_STANDARD_CABLE"):
        check_keys(config)


def test_a_file_whose_keys_were_all_read_passes():
    check_keys(ConfigFile(Path("lv.yaml"), {}))
    config = ConfigFile(Path("lv.yaml"), {"A": 1})
    config["A"]
    assert messages_of(lambda: check_keys(config)) == []


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def import_config(tmp_path, expression: str = "None") -> subprocess.CompletedProcess:
    """Import ``pylovo.config_loader`` in ``tmp_path``; stdout: the value of ``expression`` (JSON), then its warnings."""
    code = ("import json, warnings\n"
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            "    import pylovo.config_loader as cl\n"
            f"print(json.dumps({expression}))\n"
            "print(*[str(w.message) for w in caught if 'config' in str(w.message)], sep='\\n')\n")
    return subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=False)


def test_the_shipped_configuration_has_every_key_and_no_unknown_one(tmp_path):
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    proc = import_config(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert [line for line in proc.stdout.splitlines() if line] == ["null"]   # no warning: nothing unknown or missing


def test_the_loader_has_no_default_values_of_its_own():
    """Every look-up of a YAML key is ``CONFIG[...]``: a ``.get(key, default)`` would be a second place of defaults."""
    import ast

    tree = ast.parse(Path(config_loader.__file__).read_text(encoding="utf-8"))
    with_default = [ast.unparse(node) for node in ast.walk(tree)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id.startswith("CONFIG_")
                    and node.func.value.id != "CONFIG_MV"]      # the MV keys: defaults of pylovo.mv_grid.options
    assert with_default == []


def test_a_project_file_without_newer_keys_gets_the_shipped_values_and_one_warning(tmp_path):
    """An older project configuration: three keys are missing, among them one whose shipped value is not the
    behaviour from before the key existed (TRANSFORMER_PLANNING_UTILIZATION 0.8, not 1.0)."""
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    path = tmp_path / "config" / "config_generation_lv.yaml"
    missing = ("TRANSFORMER_PLANNING_UTILIZATION", "MAX_VOLTAGE_REPAIRS", "TRANSFORMER_MAPPING")
    shipped = yaml.safe_load(path.read_text(encoding="utf-8"))
    path.write_text(yaml.safe_dump({k: v for k, v in shipped.items() if k not in missing}, sort_keys=False),
                    encoding="utf-8")
    proc = import_config(tmp_path, "[cl.TRANSFORMER_PLANNING_UTILIZATION, cl.MAX_VOLTAGE_REPAIRS, "
                                   "cl.TRANSFORMER_MAPPING[1], cl.VERSION_ID]")
    assert proc.returncode == 0, proc.stderr
    values, *messages = proc.stdout.splitlines()
    assert json.loads(values) == [shipped["TRANSFORMER_PLANNING_UTILIZATION"], shipped["MAX_VOLTAGE_REPAIRS"],
                                  shipped["TRANSFORMER_MAPPING"][1], str(shipped["VERSION_ID"])]
    assert len(messages) == 1 and "config_generation_lv.yaml does not have " + ", ".join(sorted(missing)) in messages[0]
    assert str(CONFIG_DIR / "config_generation_lv.yaml") in messages[0]


@pytest.mark.parametrize("file, line, key", [
    ("config_generation_lv.yaml", "HOUSE_CONECTION: cabinet\n", "HOUSE_CONECTION (did you mean HOUSE_CONNECTION?)"),
    ("config_analysis.yaml", "PLOT_COLORS: {}\n", "PLOT_COLORS"),
    ("config_classification.yaml", "N_SAMPELS: 5\n", "N_SAMPELS (did you mean N_SAMPLES?)"),
    ("config_clustering.yaml", "N_CLUSTER_GMM: 3\n", "N_CLUSTER_GMM (did you mean N_CLUSTERS_GMM?)")])
def test_a_misspelled_key_of_each_file_stops_the_import_and_names_the_key(tmp_path, file, line, key):
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    path = tmp_path / "config" / file
    path.write_text(path.read_text() + line)
    proc = import_config(tmp_path)
    assert proc.returncode != 0
    error = proc.stderr.strip().splitlines()[-1]
    assert error.startswith("ValueError: ") and file in error and key in error and "does not know" in error


def test_keys_of_earlier_releases_in_project_files_do_not_stop_the_import(tmp_path):
    """A project configuration carried along since 0.8.0 (as GridPlanner's) keeps loading, with one warning per file."""
    shutil.copytree(CONFIG_DIR, tmp_path / "config")
    former = {"config_generation_lv.yaml": "BROWNFIELD_NEAREST_ONLY: true\nV_BAND_LOW: 0.9\n",
              "config_analysis.yaml": "COLORS: {}\nPLOT_DEFAULTS: {}\nNETWORK_COLORS: {}\n",
              "config_classification.yaml": "REGIOSTAR7_DICT: {}\n", "config_clustering.yaml": "N_CLUSTERS_KMEDOID: 5\n"}
    for file, lines in former.items():
        path = tmp_path / "config" / file
        path.write_text(path.read_text() + lines)
    proc = import_config(tmp_path)
    assert proc.returncode == 0, proc.stderr
    _, *messages = proc.stdout.splitlines()
    assert len(messages) == 4 and all("no longer read" in message for message in messages)
    assert "BROWNFIELD_NEAREST_ONLY (until 1.2.0), V_BAND_LOW (until 0.8.0)" in messages[0]


def test_every_retired_key_is_gone_from_the_shipped_files():
    shipped = set()
    for path in CONFIG_DIR.glob("*.yaml"):
        shipped |= set(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    assert not shipped & set(config_loader.RETIRED_KEYS)


# --- connection variables replaced by the .env ---------------------------------------------------------------------
CONNECTION = {"DBNAME": "env_db", "DBUSER": "env_user", "HOST": "env.host", "PORT": "1111", "PASSWORD": "env-secret"}


def replaced_by_env_file(tmp_path, environment: dict[str, str], env_file: str | None) -> tuple[tuple[str, ...], list[str]]:
    """``ENV_FILE_REPLACED`` and the warnings about it of a fresh import of the module (``None``: no .env at all)."""
    path = tmp_path / ".env"
    path.write_text(env_file or "")
    code = ("import json, warnings\n"
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            "    import pylovo.config_loader as c\n"
            "print(json.dumps(c.ENV_FILE_REPLACED))\n"
            "print(*[str(w.message) for w in caught if '.env' in str(w.message)], sep='\\n')\n")
    env = {**os.environ, **CONNECTION, **environment, "PYLOVO_ENV_FILE": str(path) if env_file is not None else ""}
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    first, *messages = proc.stdout.splitlines()
    return tuple(json.loads(first)), [line for line in messages if line]


ENV_FILE = "DBNAME=env_db\nDBUSER=env_user\nHOST=file.host\nPORT=1111\nPASSWORD=file-secret\n"


def test_an_env_file_that_changes_connection_variables_of_the_environment_is_reported_without_values(tmp_path):
    replaced, messages = replaced_by_env_file(tmp_path, {}, ENV_FILE)
    assert replaced == ("HOST", "PASSWORD")
    assert len(messages) == 1 and "HOST, PASSWORD" in messages[0] and "PYLOVO_ENV_FILE=" in messages[0]
    for value in ("file.host", "env.host", "file-secret", "env-secret"):
        assert value not in messages[0]


def test_equal_values_give_no_report(tmp_path):
    same = "".join(f"{name}={value}\n" for name, value in CONNECTION.items())
    assert replaced_by_env_file(tmp_path, {}, same) == ((), [])


def test_variables_that_the_environment_did_not_set_give_no_report(tmp_path):
    env = {name: value for name, value in os.environ.items() if name not in CONNECTION}
    env["PYLOVO_ENV_FILE"] = str(tmp_path / ".env")
    (tmp_path / ".env").write_text(ENV_FILE)
    code = "import pylovo.config_loader as c; print(c.ENV_FILE_REPLACED, c.HOST)"
    proc = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=env,
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "() file.host"


def test_without_an_env_file_nothing_is_replaced(tmp_path):
    assert replaced_by_env_file(tmp_path, {}, None) == ((), [])
