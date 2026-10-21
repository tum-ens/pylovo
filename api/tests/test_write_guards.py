"""Direct writers (configuration, transformers) and database-writing jobs exclude each other, without a database."""
from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager

import pytest
from pylovo_api import config_io
from pylovo_api.jobs import Job, JobConflict

CONFIG = "config_generation_lv.yaml"


def running_job(client, kind: str = "generate", writes_db: bool = True) -> None:
    client.app.state.jobs._jobs["x"] = Job(id="x", kind=kind, title="Generate grids", argv=[], writes_db=writes_db,
                                           status="running")


def test_config_writes_are_refused_while_a_writer_job_runs(client, project):
    path = project / "config" / CONFIG
    text = path.read_text(encoding="utf-8")
    backups = project / ".pylovo-api" / "config-backups"
    (backups / "config_generation.20260101-000000.yaml").write_text(text, encoding="utf-8")
    calls = {
        "text": lambda: client.put("/api/config", json={"text": text + "# edited\n", "confirm": True}),
        "values": lambda: client.post("/api/config/values", json={"changes": {"VERSION_ID": "zz"}, "confirm": True}),
        "restore": lambda: client.post("/api/config/restore", json={"name": "config_generation.20260101-000000.yaml",
                                                                    "confirm": True}),
    }
    running_job(client)
    for name, call in calls.items():
        response = call()
        assert response.status_code == 409 and "Generate grids" in response.json()["detail"], name
        assert path.read_text(encoding="utf-8") == text, name
    dry_run = client.post("/api/config/values", json={"changes": {"VERSION_ID": "zz"}, "dry_run": True})
    assert dry_run.status_code == 200  # nothing is written
    running_job(client, kind="export", writes_db=False)  # a read-only job does not block
    assert calls["text"]().status_code == 200 and path.read_text(encoding="utf-8") == text + "# edited\n"


@pytest.mark.parametrize("method, url, body", [
    ("post", "/api/transformers", {"plz": 85653, "lon": 11.7, "lat": 48.1}),
    ("patch", "/api/transformers/manual/1", {"transformer_rated_power": 400}),
    ("post", "/api/transformers/clear-capacities", {"plz": 85653, "confirm": "85653"}),
])
def test_a_transformer_edit_holds_the_writer_lease_while_it_writes(client, monkeypatch, method, url, body):
    manager = client.app.state.jobs
    seen = []

    class Writer:
        def __getattr__(self, name):
            def write(*args, **kwargs):
                seen.append(manager._edit_leases)
                with pytest.raises(JobConflict):  # no writer job can start between the check and the write
                    manager.start("delete", "second writer", [sys.executable, "-c", "pass"])
                return kwargs.get("osm_id", True)
            return write

    @contextmanager
    def database_client():
        yield Writer()

    monkeypatch.setattr("pylovo_api.db.database_client", database_client)
    response = getattr(client, method)(url, json=body)
    assert response.status_code in (200, 201), response.text
    assert seen == [1] and manager._edit_leases == 0
    running_job(client)
    assert getattr(client, method)(url, json=body).status_code == 409
    assert seen == [1]  # the refused edit did not write


def test_two_saves_at_once_do_not_both_pass_the_base_check(client, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    validate = config_io.validate_text

    def slow(text, deep=True):  # the validation is the long part of a save
        entered.set()
        release.wait(10)
        return validate(text, deep=False)

    monkeypatch.setattr(config_io, "validate_text", slow)
    current = config_io.read_config()
    results = {}

    def save(name):
        try:
            results[name] = config_io.save_text(f"{current.text}# {name}\n", current.sha)
        except config_io.ConfigError as exc:
            results[name] = exc

    first, second = threading.Thread(target=save, args=("a",)), threading.Thread(target=save, args=("b",))
    first.start()
    assert entered.wait(10)
    second.start()
    time.sleep(0.3)  # the second save waits for the first one
    release.set()
    first.join(10)
    second.join(10)
    assert results["a"]["saved"] is True
    assert isinstance(results["b"], config_io.ConfigError) and results["b"].issues[0].get("conflict")
    assert config_io.read_config().text.endswith("# a\n")
