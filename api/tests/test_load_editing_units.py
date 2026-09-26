"""Load editing without a database: peak formula, validation, the replicated generation code, guards, lease.

The replication tests compare :mod:`pylovo.load_editing` with the generation code it mirrors
(``CableInstaller.create_consumer_bus_and_load`` on a real pandapower backend).
"""
from __future__ import annotations

import json
import logging
import math

import numpy as np
import pandas as pd
import pytest

import pylovo.load_editing as le
from pylovo.load_editing import (
    BuildingInputs,
    LoadParameters,
    component_peaks,
    validate_changes,
)

GP = {
    "electrical_backend": "pandapower",
    "residential_only_generation": False,
    "load_calculation": {
        "peak_load_household": 16.825, "default_power_factor": 0.95,
        "sim_factor": {"Commercial": 0.5, "Public": 0.6, "Residential": 0.07},
        "consumer_categories": [
            {"definition": "Commercial", "peak_load": None, "peak_load_per_m2": 79.0, "sim_factor": 0.5},
            {"definition": "Public", "peak_load": None, "peak_load_per_m2": 29.0, "sim_factor": 0.6},
            {"definition": "Residential", "peak_load": 16.825, "peak_load_per_m2": None, "sim_factor": 0.07},
        ],
    },
    "cable_dimensioning": {"vn": 400, "mv_direct_connection_load_threshold_kw": 100,
                           "max_service_design_voltage_drop_percent": 3},
    "power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1},
    "transformer_placement": {"transformer_planning_utilization": 0.8},
}


def params(**over) -> LoadParameters:
    gp = json.loads(json.dumps(GP))
    for key, value in over.items():
        gp[key] = value
    return LoadParameters.from_generation_parameters("t", gp)


def building(**kw) -> dict:
    row = {"objectid": "b1", "vertice_id": 7, "type": "MFH", "floor_area": 200.0, "floor_number": 2, "households": 6,
           "residential_floor_area": 400.0, "nonresidential_floor_area": 0.0, "nonresidential_use": None}
    row.update(kw)
    p = component_peaks(BuildingInputs.from_row(row), params(residential_only_generation=row.pop("_ro", False)))
    row.update(p.as_dict())
    return row


# --------------------------------------------------------------------------- parameters and peaks
def test_parameters_from_snapshot():
    p = params()
    assert p.residential_peak_kw == 16.825 and p.peak_load_per_m2_w == {"Commercial": 79.0, "Public": 29.0}
    assert p.sim_factor["Residential"] == 0.07 and p.vn_v == 400 and p.planning_utilization == 0.8
    df = p.consumer_categories_df()
    assert list(df.index) == ["Commercial", "Public", "Residential"] and "definition" in df.columns


def test_residential_peak_comes_from_the_category_record():
    gp = json.loads(json.dumps(GP))
    gp["load_calculation"]["consumer_categories"][2]["peak_load"] = 15.0
    p = LoadParameters.from_generation_parameters("t", gp)
    assert p.residential_peak_kw == 15.0 and p.notes


@pytest.mark.parametrize("change, reason", [
    (lambda gp: gp.update(electrical_backend="opendss"), "backend"),
    (lambda gp: gp["load_calculation"].pop("default_power_factor"), "power_factor"),
    (lambda gp: gp["cable_dimensioning"].pop("mv_direct_connection_load_threshold_kw"), "threshold"),
])
def test_not_editable_snapshots(change, reason):
    gp = json.loads(json.dumps(GP))
    change(gp)
    with pytest.raises(le.LoadEditConflict) as err:
        LoadParameters.from_generation_parameters("t", gp)
    assert err.value.code == "not_editable" and err.value.status == 409
    with pytest.raises(le.LoadEditConflict):
        LoadParameters.from_generation_parameters("t", None)


@pytest.mark.parametrize("households", [3, 6, 7, 9])
def test_component_peaks_residential(households):
    p = component_peaks(BuildingInputs(households, 400.0, 0.0, None), params())
    assert p.residential_peak_load_in_kw == households * 16.825  # same IEEE operation as the SQL
    assert p.peak_load_in_kw == p.residential_peak_load_in_kw and not p.nonresidential_mv_direct
    if households == 6:
        assert p.residential_peak_load_in_kw == 100.94999999999999


def test_component_peaks_mixed_and_mv_threshold():
    p = component_peaks(BuildingInputs(2, 150.0, 300.0, "Commercial"), params())
    assert p.nonresidential_peak_load_in_kw == 300.0 * 79.0 / 1000
    assert p.peak_load_in_kw == 2 * 16.825 + 300.0 * 79.0 / 1000
    low_threshold = params(cable_dimensioning={"vn": 400, "mv_direct_connection_load_threshold_kw": 79.0})
    exactly = component_peaks(BuildingInputs(1, 0.0, 1000.0, "Commercial"), low_threshold)
    assert exactly.nonresidential_peak_load_in_kw == 79.0 and not exactly.nonresidential_mv_direct  # exactly T stays LV
    assert exactly.peak_load_in_kw == 79.0
    above = component_peaks(BuildingInputs(1, 0.0, 1000.0000001, "Commercial"), low_threshold)
    assert above.nonresidential_mv_direct and above.peak_load_in_kw == 0
    mixed = component_peaks(BuildingInputs(2, 150.0, 1500.0, "Commercial"), params())  # 118.5 kW > 100 kW
    assert mixed.nonresidential_mv_direct and mixed.peak_load_in_kw == 2 * 16.825


def test_component_peaks_residential_only_and_no_area():
    ro = component_peaks(BuildingInputs(4, 300.0, 2000.0, "Commercial"), params(residential_only_generation=True))
    assert ro.nonresidential_peak_load_in_kw == 2000.0 * 79.0 / 1000  # computed, but not counted
    assert not ro.nonresidential_mv_direct and ro.peak_load_in_kw == 4 * 16.825
    none = component_peaks(BuildingInputs(1, 0.0, 0.0, None), params())
    assert none.residential_peak_load_in_kw == 0 and none.peak_load_in_kw == 0


# --------------------------------------------------------------------------- validation
def test_validation_ranges_and_types():
    p = params()
    b = building()
    for bad in (0, 5001, 1.5, True):
        check = validate_changes(b, {"households": bad}, p)
        assert not check.valid and check.errors[0]["field"] == "households"
    assert validate_changes(b, {"households": 4}, p).valid
    check = validate_changes(b, {"residential_floor_area": float("nan")}, p)
    assert check.errors[0]["code"] == "invalid_number"
    check = validate_changes(b, {"nonresidential_floor_area": 50.0}, p)
    assert check.errors[0]["code"] == "invalid_use"  # the use is required with an area (the UI preselects one)
    check = validate_changes(b, {"nonresidential_floor_area": 50.0, "nonresidential_use": "Public"}, p)
    assert check.valid and any(w["code"] == "split_mismatch" for w in check.warnings)  # a warning, not an error


def test_validation_keeps_stored_precision_and_detects_no_change():
    p = params()
    b = building(residential_floor_area=400.1234)
    check = validate_changes(b, {"residential_floor_area": 400.12}, p)  # the UI shows 2 decimals
    assert check.errors[0]["code"] == "no_change" and check.inputs.residential_floor_area == 400.1234


def test_validation_no_lv_load_for_mv_direct_commercial_building():
    p = params()
    b = building(type="Commercial", households=1, residential_floor_area=0.0, nonresidential_floor_area=1232.42,
                 nonresidential_use="Commercial")
    check = validate_changes(b, {"nonresidential_floor_area": 1300.0}, p)  # 102.7 kW > 100 kW
    assert [e["code"] for e in check.errors] == ["no_lv_load"]


def test_validation_households_require_residential_area():
    p = params()
    b = building(type="Commercial", households=1, residential_floor_area=0.0, nonresidential_floor_area=500.0,
                 nonresidential_use="Commercial")
    assert validate_changes(b, {"households": 3}, p).errors[0]["code"] == "households_without_area"
    check = validate_changes(b, {"residential_floor_area": 100.0}, p)
    assert check.valid  # households=1 is kept
    check = validate_changes({**b, "households": None}, {"residential_floor_area": 100.0}, p)
    assert check.errors[0]["code"] == "households_required"


def test_validation_residential_only_nonresidential_edit_is_allowed():
    p = params(residential_only_generation=True)
    b = building(_ro=True, type="AB", households=10, residential_floor_area=800.0, nonresidential_floor_area=466.96,
                 nonresidential_use="Commercial")
    check = validate_changes(b, {"nonresidential_floor_area": 200.0}, p)
    assert check.valid and any(w["code"] == "residential_only_nonres" for w in check.warnings)
    assert check.peaks.peak_load_in_kw == b["peak_load_in_kw"]


# --------------------------------------------------------------------------- replicated generation code
def _grid_buildings() -> pd.DataFrame:
    rows = []
    for i, (hh, res, nonres, use) in enumerate([(1, 150.0, 0.0, None), (6, 500.0, 0.0, None), (2, 120.0, 90.0, "Commercial"),
                                                (1, 0.0, 400.0, "Public"), (12, 1400.0, 0.0, None), (2, 150.0, 1500.0, "Commercial")]):
        b = building(objectid=f"b{i}", vertice_id=100 + i, households=hh, residential_floor_area=res,
                     nonresidential_floor_area=nonres, nonresidential_use=use, type="MFH")
        rows.append(b)
    return le.buildings_frame(rows)


def _installer_net(buildings: pd.DataFrame, p: LoadParameters):
    """The loads generation creates: CableInstaller.create_consumer_bus_and_load on a pandapower backend."""
    from pylovo import utils
    from pylovo.cable_installer import CableInstaller
    from pylovo.electrical_backend import create_backend

    backend = create_backend("pandapower", logger=logging.getLogger("test"))
    backend.initialize_circuit(name="t", source_bus="MVbus 1", primary_kv=20.0)
    consumer_list = list(dict.fromkeys(buildings.vertice_id.to_list()))
    coords = {v: (11.0 + v * 1e-5, 48.0) for v in consumer_list}
    installer = CableInstaller(backend, None, logging.getLogger("test"), [], pd.DataFrame(), pd.DataFrame(),
                               node_coordinates=coords)
    _, components = utils.allocate_consumer_simultaneous_loads(consumer_list, buildings, p.consumer_categories_df())
    installer.create_consumer_bus_and_load(consumer_list, components)
    return backend.net


def test_load_specs_identical_to_cable_installer():
    from pylovo import config_loader as cl

    p = params()
    assert p.power_factor == cl.DEFAULT_POWER_FACTOR  # the installer uses the config constant
    buildings = _grid_buildings()
    net = _installer_net(buildings, p)
    table = le.snapshot_loads(buildings, p)
    rows = table.rows
    assert len(rows) == len(net.load)
    for (i, gen), mine in zip(net.load.iterrows(), rows):
        assert gen["name"] == mine["name"] and gen["category"] == mine["category"]
        for key in ("p_mw", "q_mvar", "max_p_mw", "service_design_p_mw", "load_units"):
            assert gen[key] == mine[key], key  # bit-identical
        assert int(gen["consumer_vertex"]) == mine["consumer_vertex"]
    zones = {int(name.rsplit(" ", 1)[1]): zone for name, zone in zip(net.bus.name, net.bus.zone)}
    assert zones == table.zones
    assert "Load 105 Commercial" not in [r["name"] for r in rows]  # 118.5 kW: MV-direct, not in the LV model
    assert "Load 105 Residential" in [r["name"] for r in rows] and table.zones[102] == "Mixed"


def test_replace_net_loads_rebuilds_the_installer_table_exactly():
    import pandapower as pp

    p = params()
    buildings = _grid_buildings()
    net = _installer_net(buildings, p)
    stored = pp.to_json(net)
    parsed = pp.from_json_string(stored)
    assert le.replace_net_loads(parsed, le.snapshot_loads(buildings, p)) == []
    assert pp.to_json(parsed) == stored  # byte-identical network JSON


def test_parameters_are_not_read_from_the_config(monkeypatch):
    from pylovo import config_loader as cl

    buildings = _grid_buildings()
    before = le.snapshot_loads(buildings, params()).rows
    monkeypatch.setattr(cl, "DEFAULT_POWER_FACTOR", 0.5)
    monkeypatch.setattr(cl, "VN", 230)
    assert le.snapshot_loads(buildings, params()).rows == before
    other = le.snapshot_loads(buildings, params(load_calculation={**GP["load_calculation"], "default_power_factor": 0.9})).rows
    assert other[0]["p_mw"] == before[0]["p_mw"] and other[0]["q_mvar"] != before[0]["q_mvar"]


# --------------------------------------------------------------------------- guards
def _stored(table):
    return [{**r, "pp_index": i, "bus": None} for i, r in enumerate(table.rows)]


def test_reproduction_tiers():
    p = params()
    buildings = _grid_buildings()
    table = le.snapshot_loads(buildings, p)
    assert le.compare_loads(table, _stored(table)).tier == "exact"
    stored = _stored(table)
    stored[0]["p_mw"] = np.nextafter(stored[0]["p_mw"], 1)
    assert le.compare_loads(table, stored).tier == "tolerance"
    stored = _stored(table)
    stored[1]["p_mw"] *= 1 + 1e-9
    rep = le.compare_loads(table, stored)
    assert rep.tier == "drift" and rep.load_mismatches
    other = le.snapshot_loads(buildings, params(load_calculation={**GP["load_calculation"],
                                                                 "sim_factor": {**GP["load_calculation"]["sim_factor"],
                                                                                "Residential": 0.07 + 1e-9}}))
    assert le.compare_loads(other, _stored(table)).tier == "drift"
    fewer = le.snapshot_loads(buildings.iloc[1:], p)  # e.g. a duplicated objectid dropped by save_tables
    assert le.compare_loads(fewer, _stored(table)).tier == "drift"


def test_building_peak_check():
    p = params()
    buildings = _grid_buildings()
    assert le.check_building_peaks(buildings, p)[0] == []
    changed = buildings.copy()
    changed.iloc[0, changed.columns.get_loc("peak_load_in_kw")] += 1e-6
    assert le.check_building_peaks(changed, p)[0]


def test_storage_consistency():
    import pandapower as pp

    p = params()
    buildings = _grid_buildings()
    net = pp.from_json_string(pp.to_json(_installer_net(buildings, p)))
    sql_loads = [{"pp_index": i, **{k: (r[k].item() if hasattr(r[k], "item") else r[k]) for k in
                                    ("name", "category", "bus", "p_mw", "q_mvar", "max_p_mw", "service_design_p_mw",
                                     "load_units", "consumer_vertex")}} for i, r in net.load.iterrows()]
    sql_buses = [{"pp_index": int(i), "name": n} for i, n in net.bus.name.items()]
    assert le.check_storage_consistency(net, sql_loads, sql_buses) == []
    shifted = [dict(r) for r in sql_loads]
    shifted[0]["p_mw"] += 5e-16
    assert le.check_storage_consistency(net, shifted, sql_buses) == []  # JSON rounding
    shifted[0]["p_mw"] += 1e-9
    assert le.check_storage_consistency(net, shifted, sql_buses)
    assert le.check_storage_consistency(net, sql_loads[:-1], sql_buses)
    assert le.check_storage_consistency(net, sql_loads, [{**b, "pp_index": b["pp_index"] + 1} for b in sql_buses])


def test_validate_operating_point_classifies_like_save_net():
    class Fake:
        def __init__(self, converged, vmin=0.95, raises=False):
            self.converged, self.vmin, self.raises, self.net = converged, vmin, raises, None

        def solve_power_flow(self):
            if self.raises:
                raise RuntimeError("boom")
            return self.converged

        def get_circuit_metrics(self):
            return {"min_voltage_pu": self.vmin, "max_voltage_pu": 1.0}

    assert le.validate_operating_point(Fake(True), 0.9, 1.1).status == "converged"
    assert le.validate_operating_point(Fake(True, 0.89), 0.9, 1.1).status == "voltage_violation"
    assert le.validate_operating_point(Fake(False), 0.9, 1.1).status == "not_converged"
    failed = le.validate_operating_point(Fake(True, raises=True), 0.9, 1.1)
    assert failed.status == "not_converged" and "boom" in failed.error


# --------------------------------------------------------------------------- UI: lease, request models, CSV
def test_job_lease_rules(tmp_path):
    from pylovo_api.jobs import Job, JobConflict, JobManager

    jm = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
    with jm.db_edit_lease("1", 85653), pytest.raises(JobConflict):
        jm.start("delete", "Delete", ["true"], writes_db=True)
    for kind, params_, blocks in (("delete", {}, True), ("setup", {}, True), ("import", {}, True),
                                  ("generate", {"plz": [85653], "version_id": "1"}, False),
                                  ("analyze", {"plz": 85653, "version_id": "1"}, True),
                                  ("analyze", {"plz": 85653, "version_id": "2"}, False),
                                  ("analyze", {"plz": 80000, "version_id": "1"}, False)):
        jm._jobs = {"x": Job(id="x", kind=kind, title=kind, argv=[], writes_db=True, params=params_, status="running")}
        assert (jm.conflicting_writer("1", 85653) is not None) == blocks, (kind, params_)
        if blocks:
            with pytest.raises(JobConflict), jm.db_edit_lease("1", 85653):
                pass
    jm._jobs = {}


def test_request_models_reject_nan_infinity_and_extra_fields(client):
    for raw in ('{"objectid": "a", "changes": {"nonresidential_floor_area": NaN}}',
                '{"objectid": "a", "changes": {"residential_floor_area": Infinity}}',
                '{"objectid": "a", "changes": {"households": 1.5}}',
                '{"objectid": "a", "changes": {"households": 4}, "extra": 1}',
                '{"objectid": "a", "changes": {"peak_load_in_kw": 4}}'):
        res = client.post("/api/grids/1/load-edit/preview", content=raw, headers={"Content-Type": "application/json"})
        assert res.status_code == 422, raw
    res = client.post("/api/grids/1/load-edits", json={"objectid": "a", "changes": {}, "if_match": "x",
                                                       "acknowledge": ["something_else"]})
    assert res.status_code == 422
    res = client.post("/api/grids/1/load-edits", json={"objectid": "a", "changes": {"households": 2}, "if_match": "x"},
                      headers={"X-Pylovo-UI": ""})
    assert res.status_code == 403


def test_csv_export_escapes_formulas():
    from pylovo_api.load_edit_service import csv_safe, history_csv

    assert csv_safe("=cmd|' /C calc'!A0") == "'=cmd|' /C calc'!A0" and csv_safe("+1") == "'+1" and csv_safe("ok") == "ok"
    text = history_csv([{"load_edit_id": 1, "changes": {"households": [10, 4]}, "reason": "=HYPERLINK(1)",
                         "before_grid": {"power_flow_status": "converged"}, "after_grid": {"power_flow_status": "converged"}}])
    assert "'=HYPERLINK(1)" in text and "households,10,4" in text


def test_clean_makes_results_json_safe():
    out = le._clean({"a": np.float64("nan"), "b": [np.int64(3), math.inf], "c": np.bool_(True)})
    assert out == {"a": None, "b": [3, None], "c": True}
    json.dumps(out, allow_nan=False)
