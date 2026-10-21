"""The job runner without a database: commands that cannot start, cancel, log history, the event stream.

Every command is a Python one-liner, so no pylovo CLI starts and nothing touches a database.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest
from pylovo_api.jobs import JobManager


def wait(job, seconds: float = 10.0) -> None:
    end = time.time() + seconds
    while job.active and time.time() < end:
        time.sleep(0.05)


@pytest.fixture()
def manager(tmp_path):
    return JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")


# --------------------------------------------------------------------------- commands that cannot start
@pytest.mark.parametrize("bad", ["a\x00b", "\ud800"], ids=["NUL byte", "lone surrogate"])
def test_a_command_that_cannot_be_started_fails_the_job_and_frees_the_writer_lock(manager, bad):
    job = manager.start("delete", "bad argument", [sys.executable, "-c", "pass", bad])
    wait(job)
    assert job.status == "failed" and job.exit_code is None and job.finished_at is not None
    assert any(line["level"] == "error" and line["text"].startswith("Could not run the command") for line in job.lines)
    assert manager.running_writer() is None
    follow = manager.start("generate", "next writer", [sys.executable, "-c", "pass"])  # the one-writer lock is free
    wait(follow)
    assert follow.status == "succeeded"


def test_a_missing_program_fails_the_job(manager):
    job = manager.start("test", "missing", ["/nonexistent/pylovo-nothing"])
    wait(job)
    assert job.status == "failed" and job.exit_code is None
    assert any("FileNotFoundError" in line["text"] for line in job.lines)


def test_the_pipe_of_a_finished_job_is_closed(manager, monkeypatch):
    started = []
    popen = subprocess.Popen

    def recording(*args, **kwargs):
        started.append(popen(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(subprocess, "Popen", recording)
    job = manager.start("test", "quick", [sys.executable, "-c", "print('x')"])
    wait(job)
    assert job.status == "succeeded" and len(started) == 1
    assert started[0].stdout.closed


# --------------------------------------------------------------------------- arguments at the routes
def test_job_routes_refuse_arguments_a_command_cannot_receive(client, monkeypatch):
    from pylovo_api.routers import jobs as jobs_router

    started = []
    monkeypatch.setattr(jobs_router, "_start", lambda request, kind, title, argv, **kw: started.append(argv) or {})
    json_header = {"Content-Type": "application/json"}
    for path, raw in (
            ("delete-networks", '{"plz": 80803, "version_id": "a\\u0000b", "confirm": "a\\u0000b/80803"}'),
            ("delete-networks", '{"plz": 80803, "version_id": "\\ud800", "confirm": "x/80803"}'),
            ("delete-networks", '{"plz": 80803, "version_id": "-h", "confirm": "-h/80803"}'),
            ("delete-versions", '{"version_ids": ["1", "-h"], "confirm": "1 -h"}'),
            ("delete-versions", '{"version_ids": ["  "], "confirm": ""}'),
            ("delete-versions", '{"version_ids": [], "confirm": ""}'),
            ("import-dso", '{"upload_id": "0123456789ab", "source": "a\\u0000b"}')):
        assert client.post(f"/api/jobs/{path}", content=raw, headers=json_header).status_code == 422, (path, raw)
    assert started == []
    ok = client.post("/api/jobs/delete-versions", json={"version_ids": [" 1 ", "2"], "confirm": "1 2"})
    assert ok.status_code == 202 and started[-1][-3:] == ["--version", "1", "2"]


def test_a_blank_dso_source_means_no_source():
    from pylovo_api.routers.jobs import DsoImportBody

    assert DsoImportBody(upload_id="0123456789ab", source="  ").source is None
    assert DsoImportBody(upload_id="0123456789ab", source=" Netz 1 ").source == "Netz 1"


# --------------------------------------------------------------------------- cancel
def test_cancel_before_the_process_exists_prevents_the_start(manager, tmp_path):
    from pylovo_api.jobs import Job

    marker = tmp_path / "ran.txt"
    job = Job(id="early", kind="test", title="early", writes_db=True, status="queued",
              argv=[sys.executable, "-c", f"open({str(marker)!r}, 'w')"])
    manager._jobs[job.id] = job
    manager.cancel(job.id)  # no process yet: nothing to signal
    manager._run(job)  # the job thread of start() comes after the cancel
    assert job.status == "cancelled" and job.exit_code is None and not marker.exists()
    assert manager.running_writer() is None


def test_cancel_while_the_process_is_being_created_stops_it(manager, monkeypatch):
    popen = subprocess.Popen
    procs = []

    def popen_then_cancel(*args, **kwargs):
        procs.append(popen(*args, **kwargs))
        manager.cancel(manager.list()[0].id)  # the job has no process yet: cancel() finds nothing to stop
        return procs[-1]

    monkeypatch.setattr(subprocess, "Popen", popen_then_cancel)
    job = manager.start("test", "sleeper", [sys.executable, "-c", "import time; time.sleep(20)"])
    try:
        wait(job, 10)
        assert job.status == "cancelled"
    finally:
        procs[0].kill()
