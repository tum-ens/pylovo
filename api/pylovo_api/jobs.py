"""Background jobs: the UI runs the existing ``pylovo-*`` command-line tools as subprocesses.

Running the CLIs (instead of calling library functions in the server process) keeps the UI a
thin shell around the documented workflow: a job does exactly what the same command does in a
terminal, it always sees the current ``config/`` files, and it can be cancelled without
leaving the web server in a half-initialised state.

A :class:`JobManager` keeps status, exit code and the full log of every job. Browsers follow a
job through Server-Sent Events (see :mod:`pylovo_api.routers.jobs`). Only one job that writes
to the database may run at a time.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pylovo_api import job_outcomes

# Console script -> module with a ``main()`` (fallback when the script is not on disk).
SCRIPT_MODULES = {
    "pylovo-setup": "pylovo.cli.setup",
    "pylovo-generate": "pylovo.cli.generate",
    "pylovo-analyze": "pylovo.cli.analyze",
    "pylovo-delete": "pylovo.cli.delete",
    "pylovo-export": "pylovo.cli.export",
    "pylovo-import": "pylovo.cli.import_data",
}

MAX_LINES_IN_MEMORY = 50_000
HISTORY_LIMIT = 60

_LEVEL_RE = re.compile(r" - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - ")
_PROGRESS_RE = re.compile(r"progress: (\d+)\s*/\s*(\d+)", re.IGNORECASE)
_PERCENT_RE = re.compile(r"(\d{1,3}) ?% processed")
_PHASE_RE = re.compile(r"###\s*(.+?)\s*###|-{5,} (start|end) (\d+) -{5,}")
_ERROR_START = ("Traceback", "✗", "❌", "GENERATE FAILED")
_EXCEPTION_RE = re.compile(r"^[\w.]*(Error|Exception|Interrupt)\b")


def classify_line(line: str) -> str:
    """Return the display level of one log line: debug, info, success, warning or error."""
    match = _LEVEL_RE.search(line)
    if match:
        return {"WARNING": "warning", "CRITICAL": "error"}.get(match.group(1), match.group(1).lower())
    text = line.strip()
    if text.startswith(_ERROR_START) or _EXCEPTION_RE.match(text) or "error occurred" in text.lower():
        return "error"
    if text.startswith("✓"):
        return "success"
    if text.lower().startswith("warning"):
        return "warning"
    return "info"


def pylovo_command(script: str, *args: str) -> list[str]:
    """Build the argv for a pylovo console script of the running environment."""
    bindir = str(Path(sys.executable).parent)
    exe = shutil.which(script, path=bindir) or shutil.which(script)
    if exe:
        return [exe, *args]
    return [sys.executable, "-m", SCRIPT_MODULES[script], *args]


class JobConflict(RuntimeError):
    """Raised when a database-writing job is started while another one is running."""


@dataclass
class Job:
    """One run of a pylovo command."""

    id: str
    kind: str
    title: str
    argv: list[str]
    writes_db: bool
    params: dict[str, Any] = field(default_factory=dict)
    output_dir: str | None = None
    status: str = "queued"  # queued | running | succeeded | failed | cancelled
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    exit_code: int | None = None
    pid: int | None = None
    progress: float | None = None
    phase: str | None = None
    counts: dict[str, int] = field(default_factory=lambda: {"warning": 0, "error": 0})
    plz_outcomes: dict[str, Any] = field(default_factory=dict)  # generate jobs, see job_outcomes
    lines: list[dict] = field(default_factory=list, repr=False)
    first_seq: int = 0
    next_seq: int = 0
    cancel_requested: bool = False
    in_traceback: bool = False
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _log_path: Path | None = field(default=None, repr=False)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def summary(self) -> dict[str, Any]:
        """JSON-serialisable description without the log lines."""
        end = self.finished_at or time.time()
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "command": " ".join(_display_arg(a) for a in self.argv),
            "writes_db": self.writes_db,
            "params": self.params,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": round(end - self.started_at, 1) if self.started_at else None,
            "exit_code": self.exit_code,
            "progress": self.progress,
            "phase": self.phase,
            "counts": dict(self.counts),
            "line_count": self.next_seq,
            "has_output": bool(self.output_dir),
            "plz_outcomes": self.plz_outcomes or None,
        }

    def lines_after(self, seq: int) -> list[dict]:
        """Log lines with a sequence number >= ``seq`` that are still in memory."""
        start = max(0, seq - self.first_seq)
        return self.lines[start:]


def _display_arg(arg: str) -> str:
    name = os.path.basename(arg)
    if name.startswith(("pylovo-", "python")):
        return name
    return arg if re.fullmatch(r"[\w./:=@%+-]+", arg) else json.dumps(arg)


class JobManager:
    """Start, observe and cancel ``pylovo-*`` subprocesses."""

    def __init__(self, cwd: Path, jobs_dir: Path):
        self.cwd = Path(cwd)
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._edit_leases = 0  # load edits being written (see db_edit_lease)
        self._load_history()

    # ------------------------------------------------------------------ queries
    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def running_writer(self) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs.values() if j.active and j.writes_db), None)

    # ------------------------------------------------------------------ load-edit lease
    # Kinds of database-writing jobs a load edit must not overlap with. ``generate`` is not one of
    # them: it refuses to write a (version, PLZ) that already has results (ResultExistsError).
    EDIT_CONFLICT_KINDS = ("setup", "import", "delete")

    def conflicting_writer(self, version_id: str | None = None, plz: int | None = None) -> Job | None:
        """The active job a load edit of ``(version_id, plz)`` must wait for, if any."""
        with self._lock:
            for job in self._jobs.values():
                if not (job.active and job.writes_db):
                    continue
                if job.kind in self.EDIT_CONFLICT_KINDS:
                    return job
                job_plz = job.params.get("plz")
                job_plz = job_plz if isinstance(job_plz, list) else [job_plz]  # "analyse all" lists several PLZ
                same = str(job.params.get("version_id")) == str(version_id) and (plz is None or plz in job_plz)
                if job.kind == "analyze" and plz is not None and same:
                    return job
                if job.kind == "generate" and job.params.get("regenerate") and same:
                    return job  # a regenerate job (routers/flow.py) deletes the grids of its PLZ first
            return None

    @contextmanager
    def db_edit_lease(self, version_id: str | None = None, plz: int | None = None):
        """Hold while a load edit writes: refuses conflicting jobs in both directions.

        Raises:
            JobConflict: If a conflicting job is active when the lease is requested.
        """
        with self._lock:
            busy = self.conflicting_writer(version_id, plz)
            if busy:
                raise JobConflict(f"'{busy.title}' is running. Load edits are blocked until it has finished.")
            self._edit_leases += 1
        try:
            yield
        finally:
            with self._lock:
                self._edit_leases -= 1

    # ------------------------------------------------------------------ control
    def start(self, kind: str, title: str, argv: list[str], *, writes_db: bool = True,
              params: dict | None = None, output_dir: Path | None = None) -> Job:
        """Start a job in a background thread.

        Raises:
            JobConflict: If ``writes_db`` and another database-writing job is active.
        """
        with self._lock:
            if writes_db:
                busy = self.running_writer()
                if busy:
                    raise JobConflict(f"'{busy.title}' is still running. Wait for it or cancel it first.")
                if self._edit_leases:
                    raise JobConflict("A load edit (or a refresh of the GIS view) is being written, try again in a moment.")
            job = Job(id=uuid.uuid4().hex[:10], kind=kind, title=title, argv=list(argv), writes_db=writes_db,
                      params=params or {}, output_dir=str(output_dir) if output_dir else None)
            job._log_path = self.jobs_dir / f"{job.id}.log"
            self._jobs[job.id] = job
            self._trim_history()
        threading.Thread(target=self._run, args=(job,), name=f"job-{job.id}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> Job | None:
        """Ask a running job to stop (SIGINT, then SIGTERM and SIGKILL if it does not react)."""
        job = self.get(job_id)
        if not job or not job.active:
            return job
        job.cancel_requested = True
        proc = job._proc
        if proc and proc.poll() is None:
            self._append(job, "Cancellation requested by the user …", "warning")
            threading.Thread(target=self._escalate, args=(proc,), daemon=True).start()
        return job

    def shutdown(self, wait_s: float = 3.0) -> None:
        """Stop all running jobs when the server exits and record them as cancelled."""
        running = [j for j in self.list() if j.active and j._proc and j._proc.poll() is None]
        for job in running:
            job.cancel_requested = True
            self._append(job, "pylovo-api is shutting down: stopping the job", "warning")
            self._signal(job._proc, signal.SIGTERM)
        deadline = time.time() + wait_s
        while running and time.time() < deadline and any(j.active for j in running):
            time.sleep(0.1)

    # ------------------------------------------------------------------ internals
    def _run(self, job: Job) -> None:
        env = dict(os.environ)
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYLOVO_API_JOB": job.id})
        job.started_at = time.time()
        job.status = "running"
        self._append(job, f"$ {job.summary()['command']}", "command")
        try:
            proc = subprocess.Popen(
                job.argv, cwd=self.cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
                start_new_session=(os.name == "posix"),
            )
        except OSError as exc:
            self._append(job, f"Could not start the command: {exc}", "error")
            self._finish(job, "failed", None)
            return
        job._proc = proc
        job.pid = proc.pid
        assert proc.stdout is not None
        for raw in proc.stdout:
            self._append(job, raw.rstrip("\r\n"))
        code = proc.wait()
        # The CLIs exit with 0 even when they skipped a region after logging an error; the UI
        # shows such runs as "succeeded" with an error count instead of hiding the errors.
        status = "cancelled" if job.cancel_requested else ("succeeded" if code == 0 else "failed")
        self._finish(job, status, code)

    def _finish(self, job: Job, status: str, code: int | None) -> None:
        job.exit_code = code
        job.finished_at = time.time()
        job.status = status
        job_outcomes.finish(job)
        if status == "succeeded":
            job.progress = 1.0
        label = {"succeeded": "success", "failed": "error", "cancelled": "warning"}[status]
        took = job.finished_at - (job.started_at or job.finished_at)
        self._append(job, f"Job {status} (exit code {code}) after {took:.1f} s", label)
        job._proc = None
        try:
            (self.jobs_dir / f"{job.id}.json").write_text(json.dumps(job.summary() | {"argv": job.argv, "output_dir": job.output_dir}),
                                                         encoding="utf-8")
        except OSError:
            pass

    def _append(self, job: Job, text: str, level: str | None = None) -> None:
        if level is None:
            level = classify_line(text)
            # A Python traceback: its indented frame lines belong to the error.
            if text.startswith("Traceback"):
                job.in_traceback = True
            elif job.in_traceback:
                if text.startswith((" ", "\t")) or not text.strip():
                    level = "trace"
                else:
                    job.in_traceback = False
                    level = "error"
        if level in job.counts:
            job.counts[level] += 1
        entry = {"seq": job.next_seq, "t": round(time.time(), 3), "text": text, "level": level}
        job.lines.append(entry)
        job.next_seq += 1
        if len(job.lines) > MAX_LINES_IN_MEMORY:
            drop = len(job.lines) - MAX_LINES_IN_MEMORY
            del job.lines[:drop]
            job.first_seq += drop
        self._update_progress(job, text)
        job_outcomes.observe(job, text)
        if job._log_path:
            try:
                with job._log_path.open("a", encoding="utf-8") as fh:
                    fh.write(text + "\n")
            except OSError:
                pass

    @staticmethod
    def _update_progress(job: Job, text: str) -> None:
        match = _PROGRESS_RE.search(text)
        if match and int(match.group(2)) > 0:
            job.progress = min(1.0, int(match.group(1)) / int(match.group(2)))
        else:
            match = _PERCENT_RE.search(text)
            if match:
                job.progress = min(1.0, int(match.group(1)) / 100)
        match = _PHASE_RE.search(text)
        if match:
            job.phase = match.group(1).capitalize() if match.group(1) else f"PLZ {match.group(3)} {match.group(2)}"

    @staticmethod
    def _signal(proc: subprocess.Popen, sig: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGINT:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _escalate(self, proc: subprocess.Popen) -> None:
        for sig, wait_s in ((signal.SIGINT, 6), (signal.SIGTERM, 6), (getattr(signal, "SIGKILL", signal.SIGTERM), 0)):
            if proc.poll() is not None:
                return
            self._signal(proc, sig)
            deadline = time.time() + wait_s
            while wait_s and time.time() < deadline:
                if proc.poll() is not None:
                    return
                time.sleep(0.2)

    def _trim_history(self) -> None:
        finished = sorted((j for j in self._jobs.values() if not j.active), key=lambda j: j.created_at)
        for job in finished[: max(0, len(self._jobs) - HISTORY_LIMIT)]:
            self._jobs.pop(job.id, None)

    def _load_history(self) -> None:
        metas = sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)[-HISTORY_LIMIT:]
        for meta_path in metas:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                job = Job(id=meta["id"], kind=meta["kind"], title=meta["title"], argv=meta.get("argv", []),
                          writes_db=meta.get("writes_db", True), params=meta.get("params") or {},
                          status=meta["status"], created_at=meta["created_at"], started_at=meta.get("started_at"),
                          finished_at=meta.get("finished_at"), exit_code=meta.get("exit_code"),
                          progress=meta.get("progress"), phase=meta.get("phase"),
                          counts=meta.get("counts") or {"warning": 0, "error": 0},
                          plz_outcomes=meta.get("plz_outcomes") or {})
                output_dir = meta.get("output_dir")
                job.output_dir = output_dir if output_dir and Path(output_dir).is_dir() else None
                log_path = self.jobs_dir / f"{job.id}.log"
                if log_path.exists():
                    for i, text in enumerate(log_path.read_text(encoding="utf-8", errors="replace").splitlines()):
                        job.lines.append({"seq": i, "t": None, "text": text, "level": classify_line(text)})
                    job.next_seq = len(job.lines)
                    if job.lines:
                        job.lines[0]["level"] = "command"
                        job.lines[-1]["level"] = {"succeeded": "success", "failed": "error"}.get(job.status, "warning")
                self._jobs[job.id] = job
            except (OSError, ValueError, KeyError):
                continue
