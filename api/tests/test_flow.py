"""Tests of the workflow helpers: pre-flight, chained jobs, per-PLZ outcomes, stored validation.

The database tests (``requires_db``) only read, or check that destructive endpoints refuse
without the typed confirmation; they never start a job that writes.
"""
from __future__ import annotations

import re
import sys
import time
from types import SimpleNamespace

import pytest
from conftest import requires_db
from pylovo_api import chain, job_outcomes, preflight
from pylovo_api.jobs import JobManager


# --------------------------------------------------------------------------- pure helpers
def test_next_free_version_id():
    assert preflight.next_free_version_id(["1", "2", "3"], "1") == "4"
    assert preflight.next_free_version_id([], "1") == "1"
    assert preflight.next_free_version_id(["base", "base_2"], "base") == "base_3"
    assert preflight.next_free_version_id(["1", "x"], "x") == "2"


def test_summarise_and_flags():
    diff = [{"key": "transformer_placement.use_open_transformer_positions", "stored": False, "config": True},
            {"key": "transformer_placement.transformer_mapping.1", "stored": [100], "config": [100, 160, 250]},
            {"key": "transformer_placement.transformer_mapping.2", "stored": [100], "config": [250, 400]},
            {"key": "transformer_placement.k_means_seed", "stored": 1, "config": 2}]
    assert preflight.summarise(diff) == "open positions on; k_means_seed 2; sizes 1:100-250 2:250-400"
    gp = {"transformer_placement": {"use_open_transformer_positions": True, "transformer_mapping": {"1": [100, 250]}},
          "residential_only_generation": True}
    assert preflight.version_flags(gp) == ["open positions", "residential only", "100–250 kVA"]
    assert preflight.version_flags({"transformer_placement": {}}) == ["greenfield"]


def _job(plz):
    return SimpleNamespace(kind="generate", params={"plz": plz}, plz_outcomes={}, progress=None, status="running")


def test_generate_outcomes_from_log():
    job = _job([85653, 85654, 85655])
    for line in ["-------------------- start 85653 ---------------------------",
                 "Cable installation progress: 5/10",
                 "2026-09-25 - GridGenerator - INFO - Grid for the postcode area 85653 has already been generated.",
                 "-------------------- end 85653 -----------------------------",
                 "-------------------- start 85654 ---------------------------",
                 "2026-09-25 - GridGenerator - ERROR - Error during grid generation for PLZ 85654: boom",
                 "2026-09-25 - GridGenerator - INFO - Skipped PLZ 85654 due to generation error.",
                 "-------------------- end 85654 -----------------------------",
                 "-------------------- start 85655 ---------------------------",
                 "Cable installation progress: 1/2"]:
        job_outcomes.observe(job, line)
    items = job.plz_outcomes["items"]
    assert items["85653"]["state"] == "exists"
    assert items["85654"] == {"state": "failed", "error": "boom"}
    assert items["85655"]["state"] == "running"
    assert job.plz_outcomes["done"] == 2 and abs(job.progress - (2 + 0.5) / 3) < 1e-9
    job.status = "cancelled"
    job_outcomes.finish(job)
    assert items["85655"]["state"] == "cancelled"


def test_generate_outcomes_when_the_job_fails_before_any_plz():
    job = _job([85653])
    job_outcomes.observe(job, "ValueError: Generation parameters differ from the stored snapshot for version 1.")
    job.status = "failed"
    job_outcomes.finish(job)
    item = job.plz_outcomes["items"]["85653"]
    assert item["state"] == "not_run" and "differ from the stored snapshot" in item["error"]


def test_chain_runs_in_order_and_stops_at_the_first_failure(tmp_path):
    ok = [sys.executable, "-c", "print('one')"]
    bad = [sys.executable, "-c", "import sys; print('two'); sys.exit(3)"]
    never = [sys.executable, "-c", "print('three')"]
    assert chain.split(chain.build(ok, bad)[3:]) == [ok, bad]
    manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
    job = manager.start("test", "chain", chain.build(ok, bad, never))
    for _ in range(200):
        if not job.active:
            break
        time.sleep(0.1)
    texts = [line["text"] for line in job.lines]
    assert job.status == "failed" and job.exit_code == 3
    assert "one" in texts and "two" in texts and "three" not in texts
    assert any(t.startswith("### Step 2/3") for t in texts) and not any(t.startswith("### Step 3/3") for t in texts)


# --------------------------------------------------------------------------- sandbox database
def _as_generated_before_the_station_voltage(project):
    """The sandbox grids predate LV_REFERENCE_VOLTAGE_PU: use the value they had."""
    path = project / "config" / "config_generation.yaml"
    path.write_text(re.sub(r"^LV_REFERENCE_VOLTAGE_PU:.*$", "LV_REFERENCE_VOLTAGE_PU: null", path.read_text(),
                           flags=re.MULTILINE))


@requires_db
def test_preflight_matches_the_stored_snapshot(client, project):
    _as_generated_before_the_station_voltage(project)
    state = client.get("/api/flow/generate-state", params={"plz": 85653}).json()
    assert state["error"] is None and state["version_id"] == "1"
    assert state["comparison"]["exists"] and state["comparison"]["matches"] is True
    assert state["next_free_version_id"] not in state["versions"]
    assert state["next_free_version_id"] == preflight.next_free_version_id(state["versions"], "1")
    candidate = client.post("/api/flow/config-preflight",
                            json={"changes": {"USE_OPEN_TRANSFORMER_POSITIONS": True}}).json()
    assert candidate["comparison"]["matches"] is False
    assert [d["key"] for d in candidate["comparison"]["differences"]] == \
        ["transformer_placement.use_open_transformer_positions"]
    assert "+USE_OPEN_TRANSFORMER_POSITIONS: True" in candidate["diff"]
    renamed = client.post("/api/flow/config-preflight", json={"changes": {
        "USE_OPEN_TRANSFORMER_POSITIONS": True, "VERSION_ID": candidate["next_free_version_id"]}}).json()
    assert renamed["comparison"]["exists"] is False and renamed["base_version_id"] == "1"
    assert renamed["suggested_comment"] == "open positions on"


@requires_db
def test_destructive_flow_endpoints_need_the_typed_confirmation(client, project):
    _as_generated_before_the_station_voltage(project)
    assert client.post("/api/flow/regenerate", json={"plz": 85653, "confirm": "1/1"}).status_code == 400
    assert client.post("/api/flow/reanalyse", json={"plz": 85653, "confirm": "85653"}).status_code == 400


@requires_db
def test_feeders_exclude_service_cables_at_the_station(client):
    for version in ("1", "2"):
        grids = client.get(f"/api/results/{version}/summary").json()["grids"]
        # pylovo's analysis counts the same branches (clustering_parameters.no_branches)
        assert [g["feeders"] for g in grids] == [g["pylovo_branches"] for g in grids]
    v1 = client.get("/api/results/1/summary").json()
    assert v1["kpis"]["feeders"] == 24 and v1["kpis"]["direct_connections"] == 4


@requires_db
def test_stored_validation_reproduces_the_on_demand_power_flow(client):
    summary = client.get("/api/results/1/summary").json()
    kpis = summary["kpis"]
    assert kpis["stations"] == kpis["grids"] == 4 and kpis["transformer_units"] == 5 and kpis["parallel_stations"] == 1
    grid = next(g for g in summary["grids"] if (g["kcid"], g["bcid"]) == (1, 3))
    stored = grid["validation"]
    assert stored["checks"] == {"solver": "ok", "voltage_band": "fail", "cable_thermal": "fail", "trafo_planning": "warn"}
    pf = client.post(f"/api/grids/{grid['grid_result_id']}/powerflow").json()
    assert stored["min_vm_pu"] == pytest.approx(pf["min_vm_pu"], abs=1e-4)
    assert stored["trafo_loading_percent"] == pytest.approx(pf["trafo"][0]["loading_percent"], abs=0.05)
    assert stored["max_line_loading_percent"] == pytest.approx(pf["max_line_loading_percent"], abs=0.05)
    budget = stored["budget"]
    assert budget["total_pct"] == pytest.approx(budget["trafo_pct"] + budget["feeder_pct"] + budget["service_pct"], abs=0.02)
    assert budget["lv_pct"] == pytest.approx(grid["max_total_drop_pct"], abs=0.01)  # the stored LV drop from the busbar
    detail = client.get(f"/api/grids/{grid['grid_result_id']}").json()
    assert detail["validation"]["min_vm_pu"] == stored["min_vm_pu"]
    first = detail["feeder_stats"][0]
    assert [m["std_type"] for m in first["cable_mix"]] == ["NAYY_4_300", "NAYY_4_240", "NAYY_4_150"]
    assert sum(f["cabinets"] for f in detail["feeder_stats"]) == len(detail["splits"]["features"])
    assert len(detail["direct_connections"]) == 1 and detail["direct_connections"][0]["address"]
    station = client.get("/api/results/1/85653/map").json()["stations"]["features"]
    assert "2 × 400 kVA" in {s["properties"]["size_label"] for s in station}
