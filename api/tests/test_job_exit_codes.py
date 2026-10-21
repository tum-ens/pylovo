"""Exit codes of ``pylovo-*`` commands as the job manager reads them (no database)."""
from __future__ import annotations

import sys
import time

import pytest

from pylovo_api import jobs
from pylovo_api.jobs import JobManager


def finished(manager: JobManager, kind: str, code: int, *lines: str):
    script = "".join(f"print({line!r}, flush=True)\n" for line in lines) + f"raise SystemExit({code})"
    job = manager.start(kind, "test", [sys.executable, "-c", script], writes_db=False)
    for _ in range(100):
        if not job.active:
            break
        time.sleep(0.1)
    return job


def test_the_api_knows_the_exit_code_of_pylovo_generate():
    from pylovo.cli.generate import EXIT_PLZ_NOT_GENERATED

    assert jobs.GENERATE_PARTIAL_EXIT_CODE == EXIT_PLZ_NOT_GENERATED


def test_a_generate_job_that_finished_with_failed_plz_is_succeeded_with_the_errors_of_the_log(tmp_path):
    manager = JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs")
    job = finished(manager, "generate", jobs.GENERATE_PARTIAL_EXIT_CODE,
                   "-------------------- start 80803 ---------------------------",
                   "Error during grid generation for PLZ 80803: boom",
                   "✗ 1 of 1 PLZ not generated: 80803 (failed)")
    assert job.status == "succeeded" and job.exit_code == jobs.GENERATE_PARTIAL_EXIT_CODE
    assert job.plz_outcomes["items"]["80803"]["state"] == "failed" and "boom" in job.plz_outcomes["items"]["80803"]["error"]
    assert job.counts["error"] >= 1


@pytest.mark.parametrize("kind, code, status", [
    ("generate", 0, "succeeded"),
    ("generate", 1, "failed"),             # a crash or a failed MV stage
    ("generate", 2, "failed"),
    ("analyze", jobs.GENERATE_PARTIAL_EXIT_CODE, "failed"),      # only pylovo-generate has this code
    ("analyze", 1, "failed"),
    ("analyze", 0, "succeeded"),
])
def test_job_status_follows_the_exit_code(tmp_path, kind, code, status):
    job = finished(JobManager(cwd=tmp_path, jobs_dir=tmp_path / "jobs"), kind, code)
    assert job.status == status
