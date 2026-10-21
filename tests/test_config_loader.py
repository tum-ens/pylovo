"""Checks of the generation configuration at load time (no database)."""
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from pylovo import config_loader
from pylovo.config_loader import (_check_cable_ampacities, _check_transformer_mapping, _consumer_categories_from_config,
                                  _version_id, _warn_about_former_generation_config)


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
