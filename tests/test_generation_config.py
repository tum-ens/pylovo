"""Unit tests without a database: version snapshot rule, station ratings, transformer sources.

Run with ``uv run --with pytest python -m pytest tests``. They import pylovo, which reads the
configuration of this checkout, but none of them opens a database connection.
"""
from __future__ import annotations

import copy

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
            "greenfield_cluster_merge_transformer_kva": [400]}


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
    from pylovo.database.transformer_sources import SOURCE_ENABLED_SQL, any_source, source_params

    assert source_params(True, False, 1) == {"include_dso": True, "include_open": False, "include_manual": True}
    assert not any_source(False, False, False) and any_source(False, False, True)
    for name in ("include_dso", "include_open", "include_manual"):
        assert f"%({name})s" in SOURCE_ENABLED_SQL
    assert "manual/%%" in SOURCE_ENABLED_SQL and "dso/%%" in SOURCE_ENABLED_SQL


def test_station_voltage_keys_of_older_snapshots():
    stored = {"power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1}}
    legacy = {"power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1, "lv_reference_voltage_pu": None}}
    assert compare_snapshots(stored, legacy) == ([], [])
    new = {"power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1, "lv_reference_voltage_pu": 0.96}}
    assert compare_snapshots(stored, new)[0] == ["power_flow_assessment.lv_reference_voltage_pu"]
