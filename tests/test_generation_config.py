"""Unit tests without a database: version snapshot rule, station ratings, transformer sources.

Run with ``uv run --with pytest python -m pytest tests``. They import pylovo, which reads the
configuration of this checkout, but none of them opens a database connection.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pandapower as pp
import pytest

from pylovo.version_snapshot import ADDED_LATER, compare_snapshots


def snapshot(**placement) -> dict:
    base = {"transformer_mapping": {"1": [100, 160]}, "use_open_transformer_positions": False,
            "use_dso_transformer_positions": False}
    base.update(placement)
    return {"electrical_backend": "pandapower", "equipment_data": [{"name": "Tr_100", "cost_eur": 3000}],
            "transformer_placement": base}


NEW_KEYS = {"use_manual_transformer_positions": False, "merge_greenfield_clusters": False,
            "greenfield_cluster_merge_transformer_kva": [400], "greenfield_cluster_merge_max_buildings": None}


def test_identical_snapshots_match():
    s = snapshot(**NEW_KEYS)
    assert compare_snapshots(s, copy.deepcopy(s)) == ([], [])


def test_old_snapshot_without_the_new_keys_does_not_block():
    stored = snapshot()                       # written before the keys were recorded
    differences, not_recorded = compare_snapshots(stored, snapshot(**NEW_KEYS))
    assert differences == []
    assert not_recorded == ["transformer_placement.merge_greenfield_clusters",
                            "transformer_placement.greenfield_cluster_merge_transformer_kva"]


def test_new_key_with_a_non_legacy_value_blocks_an_old_snapshot():
    expected = snapshot(**{**NEW_KEYS, "use_manual_transformer_positions": True})
    differences, _ = compare_snapshots(snapshot(), expected)
    assert differences == ["transformer_placement.use_manual_transformer_positions"]


def test_merge_size_limit_blocks_an_old_snapshot():
    expected = snapshot(**{**NEW_KEYS, "greenfield_cluster_merge_max_buildings": 4})
    differences, _ = compare_snapshots(snapshot(), expected)
    assert differences == ["transformer_placement.greenfield_cluster_merge_max_buildings"]


def test_recorded_keys_are_compared_normally():
    stored = snapshot(**NEW_KEYS)
    changed = snapshot(**{**NEW_KEYS, "merge_greenfield_clusters": True})
    assert compare_snapshots(stored, changed)[0] == ["transformer_placement.merge_greenfield_clusters"]
    other = snapshot(**NEW_KEYS)
    other["equipment_data"][0]["cost_eur"] = 3100
    other["transformer_placement"]["transformer_mapping"]["1"] = [100, 250]
    assert compare_snapshots(stored, other)[0] == ["equipment_data", "transformer_placement.transformer_mapping.1"]
    removed = snapshot(**NEW_KEYS)
    del removed["electrical_backend"]
    assert compare_snapshots(stored, removed)[0] == ["electrical_backend"]


def test_added_later_keys_are_in_the_snapshot_of_this_code():
    from pylovo.database.preprocessing_mixin import PreprocessingMixin

    current = PreprocessingMixin._generation_parameters_snapshot(object.__new__(PreprocessingMixin))
    for path in ADDED_LATER:
        node = current
        for key in path:
            node = node[key]


def test_version_insert_runs_no_ddl():
    """Each GridGenerator writes the version row; an ALTER TABLE there locks pylovo.version exclusively."""
    import logging

    from pylovo.database.preprocessing_mixin import PreprocessingMixin

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.queries, self.stored = [], None

        def execute(self, query, params=None):
            self.queries.append(" ".join(query.split()))
            if query.lstrip().startswith("INSERT"):
                self.stored = params[2]

        def fetchone(self):
            return (self.stored,)

    class Connection:
        def commit(self):
            pass

    client = object.__new__(PreprocessingMixin)
    client.cur, client.conn, client.logger = Cursor(), Connection(), logging.getLogger("version-insert-test")
    client.insert_version_if_not_exists()
    assert [query.split()[0] for query in client.cur.queries] == ["INSERT", "SELECT"]


def two_unit_net(parallel: int) -> pp.pandapowerNet:
    net = pp.create_empty_network()
    hv, lv = pp.create_bus(net, 20), pp.create_bus(net, 0.4)
    pp.create_transformer_from_parameters(net, hv, lv, sn_mva=0.4, vn_hv_kv=20, vn_lv_kv=0.4, vkr_percent=1.0,
                                          vk_percent=6.0, pfe_kw=0, i0_percent=0, parallel=parallel)
    return net


@pytest.mark.parametrize("parallel, expected", [(1, 0.4), (2, 0.8)])
def test_station_rating_counts_parallel_units(parallel, expected):
    from pylovo.analysis.grid_analysis import _get_transformer_mva
    from pylovo.analysis.parameter_calculation import station_mva

    net = two_unit_net(parallel)
    assert station_mva(net) == pytest.approx(expected)
    assert _get_transformer_mva(net) == pytest.approx(expected)
    assert str(int(round(station_mva(net) * 1000))) == str(int(expected * 1000))   # the plz_parameters key


def test_transformer_source_predicate():
    from pylovo.database.transformer_sources import SOURCE_ENABLED_SQL, source_params

    assert source_params(True, False, 1) == {"include_dso": True, "include_open": False, "include_manual": True}
    assert not any(source_params(False, False, False).values()) and any(source_params(False, False, True).values())
    for name in ("include_dso", "include_open", "include_manual"):
        assert f"%({name})s" in SOURCE_ENABLED_SQL
    assert "manual/%%" in SOURCE_ENABLED_SQL and "dso/%%" in SOURCE_ENABLED_SQL


def test_station_voltage_keys_of_older_snapshots():
    stored = {"power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1}}
    legacy = {"power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1, "lv_reference_voltage_pu": None}}
    assert compare_snapshots(stored, legacy) == ([], [])
    new = {"power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1, "lv_reference_voltage_pu": 0.96}}
    assert compare_snapshots(stored, new)[0] == ["power_flow_assessment.lv_reference_voltage_pu"]


def test_feeder_utilization_of_older_snapshots():
    stored = {"cable_dimensioning": {"vn": 400}}
    legacy = {"cable_dimensioning": {"vn": 400, "feeder_planning_utilization": 1.0}}
    assert compare_snapshots(stored, legacy) == ([], [])
    reserve = {"cable_dimensioning": {"vn": 400, "feeder_planning_utilization": 0.8}}
    assert compare_snapshots(stored, reserve)[0] == ["cable_dimensioning.feeder_planning_utilization"]


def test_service_utilization_of_older_snapshots():
    """A version stored before CABLE_PLANNING_UTILIZATION sized its service cables at full ampacity."""
    stored = {"cable_dimensioning": {"vn": 400, "feeder_planning_utilization": 0.8}}
    same = {"cable_dimensioning": {"vn": 400, "feeder_planning_utilization": 0.8, "service_planning_utilization": 1.0}}
    assert compare_snapshots(stored, same) == ([], [])
    reserve = {"cable_dimensioning": {"vn": 400, "feeder_planning_utilization": 0.8, "service_planning_utilization": 0.8}}
    assert compare_snapshots(stored, reserve)[0] == ["cable_dimensioning.service_planning_utilization"]


@pytest.mark.parametrize("keys, expected", [
    ("CABLE_PLANNING_UTILIZATION: 0.7\n", "0.7"),
    ("FEEDER_PLANNING_UTILIZATION: 0.6\n", "0.6"),                                  # the former name
    ("CABLE_PLANNING_UTILIZATION: 0.7\nFEEDER_PLANNING_UTILIZATION: 0.7\n", "0.7"),
    ("CABLE_PLANNING_UTILIZATION: 0.7\nFEEDER_PLANNING_UTILIZATION: 0.6\n", "former name"),
])
def test_cable_utilization_key_and_its_former_name(tmp_path, keys, expected):
    shutil.copytree(Path(__file__).resolve().parents[1] / "config", tmp_path / "config")
    config = tmp_path / "config" / "config_generation_lv.yaml"
    text = "".join(line for line in config.read_text().splitlines(keepends=True)
                   if not line.startswith(("CABLE_PLANNING_UTILIZATION:", "FEEDER_PLANNING_UTILIZATION:")))
    config.write_text(text.rstrip("\n") + "\n" + keys)
    proc = subprocess.run([sys.executable, "-c", "import pylovo.config_loader as c; print(c.CABLE_PLANNING_UTILIZATION)"],
                          cwd=tmp_path, capture_output=True, text=True, check=False)
    assert expected in (proc.stdout if proc.returncode == 0 else proc.stderr)


# --- the input data in the snapshot -----------------------------------------------------------------------------------
INPUT_DATA = {"use_infdb": True, "target_epsg": 25832, "infdb_source_schema": "basedata"}


def with_input_data(**changes) -> dict:
    return {**snapshot(**NEW_KEYS), "input_data": {**INPUT_DATA, **changes}}


def test_the_snapshot_records_the_input_data(monkeypatch):
    from pylovo.database import preprocessing_mixin

    monkeypatch.setattr(preprocessing_mixin, "USE_INFDB", False)
    monkeypatch.setattr(preprocessing_mixin, "TARGET_EPSG", 25833)
    monkeypatch.setattr(preprocessing_mixin, "INFDB_SOURCE_SCHEMA", None)
    mixin = object.__new__(preprocessing_mixin.PreprocessingMixin)
    monkeypatch.setattr(mixin, "_household_fallback_parameters", lambda: {}, raising=False)
    recorded = mixin._generation_parameters_snapshot()
    assert recorded["input_data"] == {"use_infdb": False, "target_epsg": 25833, "infdb_source_schema": None}
    json.dumps(recorded, allow_nan=False)                         # it is stored as JSON


def test_a_snapshot_stored_before_the_input_data_was_recorded_stays_usable():
    differences, not_recorded = compare_snapshots(snapshot(**NEW_KEYS), with_input_data())
    assert differences == []                                      # the version is not blocked ...
    assert sorted(not_recorded) == ["input_data.infdb_source_schema", "input_data.target_epsg",
                                    "input_data.use_infdb"]       # ... but the log says what is unknown


def test_the_same_input_data_matches():
    assert compare_snapshots(with_input_data(), copy.deepcopy(with_input_data())) == ([], [])


@pytest.mark.parametrize("key, value", [("use_infdb", False), ("target_epsg", 25833),
                                        ("infdb_source_schema", "basedata_copy")])
def test_other_input_data_than_the_stored_blocks_the_version(key, value):
    differences, _ = compare_snapshots(with_input_data(), with_input_data(**{key: value}))
    assert differences == [f"input_data.{key}"]
