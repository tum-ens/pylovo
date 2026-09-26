"""Tests without a database: config editing, topology, job runner, request guards."""
from __future__ import annotations

import sys
import time

import yaml
from pylovo_api import config_io, topology
from pylovo_api.jobs import JobManager, classify_line


def test_form_edit_keeps_comments_and_layout(client, project):
    text = (project / "config" / "config_generation.yaml").read_text()
    values = yaml.safe_load(text)
    unchanged = config_io.set_top_level_values(text, {k: values[k] for k in config_io.FORM_FIELDS if k in values})
    assert unchanged == text
    new = config_io.set_top_level_values(text, {"VERSION_ID": "42", "USE_DSO_TRANSFORMER_POSITIONS": True,
                                               "TRANSFORMER_MAPPING": {1: [100], 2: [250, 400], 3: [630]}})
    parsed = yaml.safe_load(new)
    assert parsed["VERSION_ID"] == "42" and parsed["USE_DSO_TRANSFORMER_POSITIONS"] is True
    assert parsed["TRANSFORMER_MAPPING"] == {1: [100], 2: [250, 400], 3: [630]}
    assert "# Change version if you changed any grid parameters" in new
    assert len(new.splitlines()) == len(text.splitlines())  # only the changed lines were rewritten


def test_validation_reports_yaml_and_pylovo_errors():
    _, issues = config_io.validate_text("VERSION_ID: [1, 2")
    assert issues[0]["level"] == "error" and issues[0]["line"] == 1
    text = config_io.read_config().text
    _, issues = config_io.validate_text(text.replace("\nVN: 400", "\nVNX: 400"))
    assert any("VN" in i["message"] for i in issues)
    _, issues = config_io.validate_text(text.replace('VERSION_ID: "1"', 'VERSION_ID: "12345678901"'), deep=False)
    assert any(i.get("key") == "VERSION_ID" for i in issues)


def test_config_save_requires_confirm_and_makes_a_backup(client, project):
    state = client.get("/api/config").json()
    body = {"changes": {"VERSION_COMMENT": "ui test"}, "base_sha": state["sha"]}
    assert client.post("/api/config/values", json=body).status_code == 400  # no confirm
    dry = client.post("/api/config/values", json=body | {"dry_run": True}).json()
    assert "+VERSION_COMMENT: ui test" in dry["diff"]
    saved = client.post("/api/config/values", json=body | {"confirm": True}).json()
    assert saved["saved"] and saved["backup"]
    assert (project / ".pylovo-api" / "config-backups" / saved["backup"]).exists()
    assert "VERSION_COMMENT: ui test" in (project / "config" / "config_generation.yaml").read_text()
    # a stale base hash is a conflict
    stale = client.post("/api/config/values", json={"changes": {"VERSION_COMMENT": "x"}, "base_sha": state["sha"], "confirm": True})
    assert stale.status_code == 409
    restored = client.post("/api/config/restore", json={"name": saved["backup"], "confirm": True}).json()
    assert restored["saved"]
    assert "VERSION_COMMENT: default" in (project / "config" / "config_generation.yaml").read_text()


def test_topology_labels_feeders_and_distances():
    buses = [{"pp_index": 0, "name": "LVbus 1"}, {"pp_index": 1, "name": "MVbus 1"}, {"pp_index": 2, "name": "Connection Nodebus 9"},
             {"pp_index": 3, "name": "Connection Nodebus 3"}, {"pp_index": 4, "name": "Consumer Nodebus 4"},
             {"pp_index": 5, "name": "Consumer Nodebus 5"}]
    lines = [{"pp_index": 0, "from_bus": 0, "to_bus": 2, "length_km": 0.001},                     # busbar link
             {"pp_index": 1, "from_bus": 2, "to_bus": 3, "length_km": 0.2, "feeder_section_id": 1},
             {"pp_index": 2, "from_bus": 3, "to_bus": 4, "length_km": 0.02, "service_sizing_basis": "ampacity"},
             {"pp_index": 3, "from_bus": 0, "to_bus": 5, "length_km": 0.05, "service_sizing_basis": "ampacity"}]
    topo = topology.analyse(buses, lines)
    assert topo["station"] == [0, 2]
    # The service cable straight from the busbar (line 3) is a direct connection, not a feeder.
    assert [f["feeder"] for f in topo["feeders"]] == [1]
    assert topo["line_feeder"][1] == topo["line_feeder"][2] == 1 and 3 not in topo["line_feeder"]
    assert topo["direct"] == [{"line": 3, "bus": 5}] and 5 not in topo["bus_feeder"]
    assert abs(topo["distance_km"][4] - 0.22) < 1e-9
    assert topology.line_role(lines[0]) == "link" and topology.line_role(lines[2]) == "service"


def test_job_manager_runs_streams_and_blocks_second_writer(tmp_path):
    manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
    code = "import time\nfor i in range(3):\n    print('progress: %d/3' % (i + 1), flush=True); time.sleep(0.2)\nprint('WARNING something')"
    job = manager.start("test", "test job", [sys.executable, "-c", code])
    try:
        manager.start("test", "second", [sys.executable, "-c", "pass"])
        raise AssertionError("a second writing job must be refused")
    except Exception as exc:  # noqa: BLE001
        assert "still running" in str(exc)
    for _ in range(100):
        if not job.active:
            break
        time.sleep(0.1)
    assert job.status == "succeeded" and job.exit_code == 0 and job.progress == 1.0
    assert [line["text"] for line in job.lines][1:4] == ["progress: 1/3", "progress: 2/3", "progress: 3/3"]
    assert job.counts["warning"] == 1
    reloaded = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs").get(job.id)
    assert reloaded and reloaded.status == "succeeded" and len(reloaded.lines) == len(job.lines)


def test_job_cancel(tmp_path):
    manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
    job = manager.start("test", "sleeper", [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(60)"])
    time.sleep(0.5)
    manager.cancel(job.id)
    for _ in range(100):
        if not job.active:
            break
        time.sleep(0.1)
    assert job.status == "cancelled"


def test_classify_line():
    assert classify_line("2026-09-25 01:38:30,927 - GridGenerator - WARNING - x") == "warning"
    assert classify_line("✓ Deleted all networks") == "success"
    assert classify_line("Traceback (most recent call last):") == "error"
    assert classify_line("ValueError: nope") == "error"
    assert classify_line("Elapsed Time: 0 minutes") == "info"


def test_guards(client):
    from fastapi.testclient import TestClient

    bare = TestClient(client.app)
    assert bare.post("/api/jobs/setup", json={"confirm": "x"}).status_code == 403  # no X-Pylovo-UI header
    assert bare.get("/api/config", headers={"host": "evil.example"}).status_code == 421
    assert client.post("/api/jobs/setup", json={"confirm": "wrong"}).status_code == 400
    assert client.post("/api/jobs/delete-versions", json={"version_ids": ["1"], "confirm": "2"}).status_code == 400
    # headless: the browser UI (page, static files, plugin list) lives in GridPlanner
    for path in ("/", "/static/js/main.js", "/popout.html", "/api/plugins"):
        assert bare.get(path).status_code == 404, path
