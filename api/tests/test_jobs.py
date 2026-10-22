"""The job runner without a database: commands that cannot start, cancel, log history, the event stream.

Every command is a Python one-liner, so no pylovo CLI starts and nothing touches a database.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pylovo_api.jobs import JobConflict, JobManager


def wait(job, seconds: float = 10.0) -> None:
    end = time.time() + seconds
    while job.active and time.time() < end:
        time.sleep(0.05)


def until(condition, seconds: float = 10.0) -> None:
    end = time.time() + seconds
    while not condition() and time.time() < end:
        time.sleep(0.05)
    assert condition()


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
    assert ok.status_code == 202 and started[-1][-4:] == ["--version", "1", "2", "--yes"]


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


# --------------------------------------------------------------------------- history
def write_finished_job(jobs_dir, job_id: str, lines: int, status: str = "succeeded") -> None:
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / f"{job_id}.log").write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")
    (jobs_dir / f"{job_id}.json").write_text(json.dumps({"id": job_id, "kind": "generate", "title": "old job", "argv": [],
                                                         "status": status, "created_at": 1.0}), encoding="utf-8")


def test_history_reload_keeps_only_the_last_lines_in_memory(tmp_path, monkeypatch):
    from pylovo_api import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_LINES_IN_MEMORY", 100)
    write_finished_job(tmp_path / "jobs", "big", 1000)
    write_finished_job(tmp_path / "jobs", "small", 30, status="failed")
    manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
    big = manager.get("big")
    assert len(big.lines) == 100 and big.first_seq == 900 and big.next_seq == 1000
    assert big.lines[0]["seq"] == 900 and big.lines[0]["text"] == "line 900" and big.lines[0]["level"] != "command"
    assert big.lines[-1]["seq"] == 999 and big.lines[-1]["level"] == "success"
    assert big.summary()["line_count"] == 1000
    assert [line["seq"] for line in big.lines_after(995)] == [995, 996, 997, 998, 999]
    small = manager.get("small")  # a short log is complete and starts with its command line
    assert len(small.lines) == 30 and small.first_seq == 0 and small.next_seq == 30
    assert small.lines[0]["level"] == "command" and small.lines[-1]["level"] == "error"


def test_the_text_download_of_a_finished_job_is_the_whole_log(client, monkeypatch):
    from pylovo_api import jobs as jobs_module

    monkeypatch.setattr(jobs_module, "MAX_LINES_IN_MEMORY", 100)
    manager = client.app.state.jobs
    write_finished_job(manager.jobs_dir, "big", 1000)
    manager._load_history()
    res = client.get("/api/jobs/big/log", params={"format": "text"})
    assert res.status_code == 200 and res.text == "".join(f"line {i}\n" for i in range(1000))
    assert res.headers["content-disposition"] == 'attachment; filename="pylovo-job-big.log"'
    assert res.headers["content-type"].startswith("text/plain")
    assert len(client.get("/api/jobs/big/log").json()["lines"]) == 100  # the JSON log is the part in memory


# --------------------------------------------------------------------------- event stream
def asgi_get(app, path: str, headers: list[tuple[bytes, bytes]]) -> tuple[int | None, str]:
    """One GET straight through the ASGI interface: an HTTP client would re-encode a header value such as b"\\xb2"."""
    sent = []
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
             "client": ("127.0.0.1", 1234), "server": ("127.0.0.1", 8765)}
    requested = False

    async def receive():
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(0.3)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(asyncio.wait_for(app(scope, receive, send), 10))  # an exception of the route is raised here
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body").decode()


def test_last_event_id_that_is_not_a_plain_number_is_ignored(client):
    job = client.app.state.jobs.start("test", "quick", [sys.executable, "-c", "print('hello')"], writes_db=False)
    wait(job)
    for value in (b"\xb2", b"1\xb2", b"abc", b""):  # b"\xb2" is "²" (latin-1): isdigit() but not an int
        status, body = asgi_get(client.app, f"/api/jobs/{job.id}/events",
                                [(b"host", b"127.0.0.1"), (b"last-event-id", value)])
        assert status == 200 and "hello" in body and "event: end" in body, value  # the stream starts at ``after`` = 0
    res = client.get(f"/api/jobs/{job.id}/events", headers={"Last-Event-ID": "0"})
    assert "hello" in res.text and '"text": "$ ' not in res.text  # a valid id resumes after that line


# --------------------------------------------------------------------------- finish hook
def test_the_finish_hook_runs_after_the_job_has_ended_and_may_fail(manager):
    seen = []

    def hook(job):
        seen.append((job.status, job.exit_code, job.finished_at is not None))
        raise RuntimeError("a failing listener must not break the runner")

    manager.on_finish = hook
    ok = manager.start("generate", "ok", [sys.executable, "-c", "pass"])
    wait(ok)
    bad = manager.start("generate", "bad", [sys.executable, "-c", "raise SystemExit(3)"])
    wait(bad)
    unstartable = manager.start("generate", "unstartable", ["/nonexistent/pylovo-nothing"])
    wait(unstartable)
    deadline = time.time() + 5
    while len(seen) < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert seen == [("succeeded", 0, True), ("failed", 3, True), ("failed", None, True)]
    assert (ok.status, bad.status) == ("succeeded", "failed")


def test_the_region_gate_hears_about_finished_jobs(client):
    assert client.app.state.jobs.on_finish == client.app.state.coverage.on_job_finished


# --------------------------------------------------------------------------- records, restarts, shutdown
def write_record(jobs_dir, job_id, **over):
    record = {"id": job_id, "kind": "generate", "title": "Generate grids", "argv": ["pylovo-generate"], "writes_db": True,
              "params": {}, "status": "running", "created_at": time.time() - 100, "started_at": time.time() - 100,
              "pid": None, "exit_code": None, "output_dir": None} | over
    jobs_dir.mkdir(exist_ok=True)
    (jobs_dir / f"{job_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (jobs_dir / f"{job_id}.log").write_text("$ pylovo-generate\nprogress: 1/2\n", encoding="utf-8")


def test_the_record_of_a_job_exists_while_it_runs_and_holds_the_pid(manager):
    job = manager.start("test", "slow", [sys.executable, "-c", "import time; time.sleep(30)"])
    path = manager.jobs_dir / f"{job.id}.json"
    until(lambda: path.exists())
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == "running" and record["pid"] == job.pid and record["finished_at"] is None
    manager.cancel(job.id)
    wait(job, 20)
    # updated at the end, just after the status changed
    until(lambda: json.loads(path.read_text(encoding="utf-8"))["status"] == "cancelled")
    assert json.loads(path.read_text(encoding="utf-8"))["finished_at"] is not None


def test_a_job_record_is_never_read_half_written(manager):
    for _ in range(10):  # read as soon as the file exists: a plain write_text showed most of these reads an empty file
        job = manager.start("test", "slow", [sys.executable, "-c", "import time; time.sleep(30)"])
        path = manager.jobs_dir / f"{job.id}.json"
        end = time.time() + 10
        while not path.exists() and time.time() < end:
            pass
        assert json.loads(path.read_text(encoding="utf-8"))["status"] == "running"
        manager.cancel(job.id)
        wait(job, 20)
    assert not list(manager.jobs_dir.glob("*.tmp"))


def test_a_job_that_was_running_when_the_server_died_is_marked_interrupted(tmp_path):
    jobs_dir = tmp_path / "jobs"
    for job_id, status in (("a1", "running"), ("a2", "queued")):
        write_record(jobs_dir, job_id, status=status, pid=os.getpid())  # a pid that exists, but is not the job's process
    manager = JobManager(cwd=tmp_path, jobs_dir=jobs_dir)
    for job_id in ("a1", "a2"):
        job = manager.get(job_id)
        assert job.status == "interrupted" and not job.active and job.exit_code is None
        assert job.finished_at is not None and job.finished_at <= time.time()
        assert job.lines[-1]["text"].startswith("Job interrupted") and job.lines[-1]["level"] == "warning"
        assert any("stopped while this job was running" in line["text"] for line in job.lines)
        assert json.loads((jobs_dir / f"{job_id}.json").read_text(encoding="utf-8"))["status"] == "interrupted"
    assert manager.running_writer() is None  # an interrupted job holds no writer lock
    assert JobManager(cwd=tmp_path, jobs_dir=jobs_dir).get("a1").status == "interrupted"  # and stays so


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the identity of a process is read from /proc")
def test_a_job_whose_process_outlived_the_server_keeps_the_writer_lock_until_it_ends(tmp_path):
    started = time.time()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        write_record(tmp_path / "jobs", "b1", pid=proc.pid, started_at=started)
        manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
        job = manager.get("b1")
        assert job.status == "running" and manager.running_writer() is job
        with pytest.raises(JobConflict):
            manager.start("delete", "second writer", [sys.executable, "-c", "pass"])
        manager.cancel("b1")  # signals the process group of the job
        wait(job, 20)
        assert job.status == "cancelled" and manager.running_writer() is None
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="the identity of a process is read from /proc")
def test_a_pid_that_belongs_to_another_process_is_not_taken_for_the_job(tmp_path):
    write_record(tmp_path / "jobs", "c1", pid=os.getpid(), started_at=time.time() - 3600)  # this process started later
    job = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs").get("c1")
    assert job.status == "interrupted" and job._proc is None


def test_shutdown_kills_a_job_that_ignores_sigterm(manager):
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    job = manager.start("test", "stubborn", [sys.executable, "-c", code])
    until(lambda: any(line["text"] == "ready" for line in job.lines))
    pid = job.pid
    manager.shutdown(wait_s=0.5)
    assert job.status == "cancelled"
    record = json.loads((manager.jobs_dir / f"{job.id}.json").read_text(encoding="utf-8"))
    assert record["status"] == "cancelled"  # shutdown returns only after the final record is written
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# --------------------------------------------------------------------------- retention of the state directory
def test_only_the_newest_jobs_keep_their_files(tmp_path, monkeypatch):
    monkeypatch.setattr("pylovo_api.jobs.HISTORY_LIMIT", 3)
    exports = tmp_path / "exports"
    manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs", exports_dir=exports)
    ids = []
    for n in range(5):
        out = exports / f"out{n}"
        out.mkdir(parents=True)
        job = manager.start("export", f"job {n}", [sys.executable, "-c", "print('x')"], writes_db=False, output_dir=out)
        wait(job)
        ids.append(job.id)
    assert {path.stem for path in (tmp_path / "jobs").iterdir()} == set(ids[2:])  # record and log of the newest three
    assert sorted(d.name for d in exports.iterdir()) == ["out2", "out3", "out4"]
    assert len(manager.list()) == 3


def test_files_beyond_the_history_and_unreferenced_exports_are_deleted_at_start(tmp_path, monkeypatch):
    monkeypatch.setattr("pylovo_api.jobs.HISTORY_LIMIT", 2)
    jobs_dir, exports = tmp_path / "jobs", tmp_path / "exports"
    for n in range(4):
        (exports / f"e{n}").mkdir(parents=True)
        write_record(jobs_dir, f"j{n}", status="succeeded", exit_code=0, finished_at=time.time(),
                     output_dir=str(exports / f"e{n}"))
        os.utime(jobs_dir / f"j{n}.json", (1000 + n, 1000 + n))  # j0 is the oldest
    (exports / "orphan").mkdir()
    manager = JobManager(cwd=tmp_path, jobs_dir=jobs_dir, exports_dir=exports)
    assert sorted(job.id for job in manager.list()) == ["j2", "j3"]
    assert {path.stem for path in jobs_dir.iterdir()} == {"j2", "j3"}
    assert sorted(d.name for d in exports.iterdir()) == ["e2", "e3"]


def test_old_uploads_are_deleted_at_start_and_the_missing_upload_says_so(client, project):
    from pylovo_api.app import create_app
    from pylovo_api.routers import transformers

    uploads = project / ".pylovo-api" / "uploads"
    old, new = uploads / f"{'a' * 12}.csv", uploads / f"{'b' * 12}.csv"
    old.write_text("x", encoding="utf-8")
    new.write_text("x", encoding="utf-8")
    aged = time.time() - (transformers.UPLOAD_MAX_AGE_H * 3600 + 60)
    os.utime(old, (aged, aged))
    create_app(project)  # a server start
    assert not old.exists() and new.exists()
    response = client.post("/api/jobs/import-dso", json={"upload_id": "a" * 12})
    assert response.status_code == 404 and "deleted after 24 hours" in response.json()["detail"]
    stale = uploads / f"{'c' * 12}.csv"  # older than a day while the server runs: an import does not use it
    stale.write_text("x", encoding="utf-8")
    os.utime(stale, (aged, aged))
    response = client.post("/api/jobs/import-dso", json={"upload_id": "c" * 12})
    assert response.status_code == 404 and not stale.exists()
