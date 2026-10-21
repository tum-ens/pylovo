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
