"""Per-PLZ outcomes of ``pylovo-generate`` jobs, parsed from the log.

``pylovo-generate`` exits with 0 even when it skipped a PLZ after an error, so the exit code
alone does not tell what happened. The generator logs one of these per PLZ
(``GridGenerator.generate_grid_for_single_plz`` / ``generate_grid_for_multiple_plz``):

* ``-------------------- start <plz> ----`` / ``… end <plz> ----``
* ``Grid for the postcode area <plz> has already been generated.``  → ``exists``
* ``Error during grid generation for PLZ <plz>: <error>`` and ``Skipped PLZ <plz> due to
  generation error.``  → ``failed``
* ``No grid_result rows were generated for PLZ <plz>``  → ``empty``
* ``Grid generation interrupted by user for PLZ <plz>.``  → ``cancelled``
* ``Completed PLZ <plz> (<k>/<n>)`` (parallel runs) → overall progress

:func:`observe` keeps ``job.plz_outcomes = {"items": {plz: {"state", "error"}}, "done", "total"}``
up to date and, for jobs with several PLZ, drives ``job.progress`` from the finished PLZ
(the per-PLZ "progress: x/y" lines of the cable installation become a sub-step).
"""
from __future__ import annotations

import re
from typing import Any

_START = re.compile(r"^-{5,} start (\d+) -{5,}")
_END = re.compile(r"^-{5,} end (\d+) -{5,}")
_EXISTS = re.compile(r"Grid for the postcode area (\d+) has already been generated")
_ERROR = re.compile(r"Error during grid generation for PLZ (\d+): (.*)$")
_SKIPPED = re.compile(r"Skipped PLZ (\d+) due to generation error")
_EMPTY = re.compile(r"No grid_result rows were generated for PLZ (\d+)")
_INTERRUPTED = re.compile(r"Grid generation interrupted by user for PLZ (\d+)")
_WORKER_FAILED = re.compile(r"PLZ (\d+) (?:generated an exception|failed during graceful shutdown)[^:]*: (.*)$")
_COMPLETED = re.compile(r"Completed PLZ (\d+) \((\d+)/(\d+)\)")
_SUB_PROGRESS = re.compile(r"progress: (\d+)\s*/\s*(\d+)", re.IGNORECASE)
_EXCEPTION = re.compile(r"^[\w.]*(Error|Exception)\b: (.*)$")

TERMINAL = {"generated", "exists", "failed", "empty", "cancelled", "not_run"}


def _state(job) -> dict[str, Any]:
    outcomes = job.plz_outcomes
    if not outcomes:
        plz = (job.params or {}).get("plz") or []
        outcomes.update({"items": {str(p): {"state": "pending", "error": None} for p in plz},
                         "done": 0, "total": len(plz), "last_error": None, "sub": None})
    return outcomes


def _set(outcomes: dict, plz: str, state: str, error: str | None = None) -> None:
    item = outcomes["items"].setdefault(plz, {"state": "pending", "error": None})
    if item["state"] == "failed" and state in ("generated", "running"):
        return  # "end" follows the error of a skipped PLZ
    if item["state"] in ("exists", "empty") and state == "generated":
        return
    item["state"] = state
    if error and not item.get("error"):
        item["error"] = error[:400]


def observe(job, text: str) -> None:
    """Update ``job.plz_outcomes`` (and the overall progress) from one log line of a generate job."""
    if job.kind != "generate":
        return
    outcomes = _state(job)
    line = text.strip()
    match = _EXCEPTION.match(line)
    if match:
        outcomes["last_error"] = line[:400]
    if m := _START.search(line):
        _set(outcomes, m.group(1), "running")
    elif m := _END.search(line):
        _set(outcomes, m.group(1), "generated")
    elif m := _EXISTS.search(line):
        _set(outcomes, m.group(1), "exists")
    elif m := _ERROR.search(line):
        _set(outcomes, m.group(1), "failed", m.group(2))
    elif m := _SKIPPED.search(line):
        _set(outcomes, m.group(1), "failed")
    elif m := _EMPTY.search(line):
        _set(outcomes, m.group(1), "empty", "no grids were generated for this PLZ")
    elif m := _INTERRUPTED.search(line):
        _set(outcomes, m.group(1), "cancelled")
    elif m := _WORKER_FAILED.search(line):
        _set(outcomes, m.group(1), "failed", m.group(2))
    elif m := _COMPLETED.search(line):
        outcomes["total"] = max(outcomes["total"], int(m.group(3)))
    elif m := _SUB_PROGRESS.search(line):
        if int(m.group(2)) > 0:
            outcomes["sub"] = min(1.0, int(m.group(1)) / int(m.group(2)))
    items = outcomes["items"].values()
    outcomes["total"] = max(outcomes["total"], len(outcomes["items"]))
    outcomes["done"] = sum(1 for i in items if i["state"] in TERMINAL)
    running = sum(1 for i in items if i["state"] == "running")
    if outcomes["total"] > 1:
        sub = (outcomes.get("sub") or 0) if running == 1 else 0
        job.progress = min(1.0, (outcomes["done"] + sub) / outcomes["total"])
        if running == 0:
            outcomes["sub"] = None


def finish(job) -> None:
    """Close the outcomes when the job ends (PLZ that never finished are cancelled or not run)."""
    if job.kind != "generate":
        return
    outcomes = _state(job)
    for item in outcomes["items"].values():
        if item["state"] == "running":
            item["state"] = "cancelled" if job.status == "cancelled" else "failed"
            if item["state"] == "failed" and not item.get("error"):
                item["error"] = outcomes.get("last_error") or "the job ended before this PLZ finished"
        elif item["state"] == "pending":
            item["state"] = "not_run"
            if job.status != "cancelled":
                item["error"] = outcomes.get("last_error")
    outcomes["done"] = sum(1 for i in outcomes["items"].values() if i["state"] in TERMINAL)
    outcomes["sub"] = None
