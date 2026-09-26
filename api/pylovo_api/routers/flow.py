"""Workflow helpers: config pre-flight, regenerate / re-analyse as chained jobs, generate state."""
from __future__ import annotations

import sys
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from pylovo_api import chain, config_io, db, gate, preflight
from pylovo_api.deps import coverage, jobs, require_confirm
from pylovo_api.jobs import JobConflict, pylovo_command

router = APIRouter(prefix="/api/flow", tags=["workflow"])


class PreflightBody(BaseModel):
    text: str | None = None
    changes: dict[str, Any] | None = None
    base_version_id: str | None = None


class RegionBody(BaseModel):
    plz: int
    confirm: str | None = None


def _start(request: Request, kind: str, title: str, argv: list[str], params: dict) -> dict:
    try:
        return jobs(request).start(kind, title, argv, writes_db=True, params=params).summary()
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def _plz_state(version_id: str) -> tuple[set[int], set[int]]:
    """PLZ with grids and PLZ with analysis results of a version."""
    with db.cursor() as cur:
        cur.execute("SELECT postcode_result_plz AS plz FROM pylovo.postcode_result WHERE version_id = %s", (version_id,))
        generated = {r["plz"] for r in cur.fetchall()}
        cur.execute("SELECT plz FROM pylovo.plz_parameters WHERE version_id = %s", (version_id,))
        analysed = {r["plz"] for r in cur.fetchall()}
    return generated, analysed


def _require_matching_config() -> dict:
    state = preflight.check()
    if state.get("error"):
        raise HTTPException(409, state["error"])
    comparison = state["comparison"] or {}
    if comparison.get("matches") is False:
        n = len(comparison.get("differences") or [])
        raise HTTPException(409, f"The saved config differs from the stored parameters of v{state['version_id']} in "
                                 f"{n} key(s); pylovo-generate would refuse it. Switch to a new VERSION_ID instead.")
    return state


@router.get("/generate-state")
def generate_state(plz: Annotated[list[int] | None, Query()] = None) -> dict:
    """Pre-flight of the saved config against the stored snapshot of its VERSION_ID, plus the
    generated / analysed state of the given PLZ in that version."""
    state = preflight.check()
    version_id = state.get("version_id") or str(config_io.current_values().get("VERSION_ID", ""))
    try:
        generated, analysed = _plz_state(version_id)
    except Exception:  # noqa: BLE001 - schema missing: the Database step explains it
        generated, analysed = set(), set()
    wanted = set(plz or [])
    return state | {
        "generated": sorted(generated & wanted) if wanted else sorted(generated),
        "analysed": sorted(analysed & wanted) if wanted else sorted(analysed),
        "unanalysed_in_version": sorted(generated - analysed),
    }


@router.post("/config-preflight")
def config_preflight(body: PreflightBody) -> dict:
    """Pre-flight of a candidate config (``text``, and/or form ``changes`` applied to it or to the saved file).

    Used by the config dialogs: when the candidate changes generation parameters of an existing
    VERSION_ID, the dialog offers to switch to the next free version id in the same save.
    """
    current = config_io.read_config()
    text = body.text if body.text is not None else current.text
    if body.changes:
        unknown = [k for k in body.changes if k not in config_io.FORM_FIELDS]
        if unknown:
            raise HTTPException(400, f"Not editable in the form: {', '.join(unknown)}")
        try:
            text = config_io.set_top_level_values(text, body.changes)
        except config_io.ConfigError as exc:
            raise HTTPException(422, {"message": str(exc), "issues": exc.issues}) from exc
    _, issues = config_io.validate_text(text, deep=False)
    return preflight.check(text, body.base_version_id) | {
        "diff": config_io.diff(current.text, text), "text": text, "sha": current.sha, "issues": issues}


@router.post("/regenerate", status_code=202)
def regenerate(body: RegionBody, request: Request) -> dict:
    """Delete the grids of one PLZ in the config version and generate them again (one job)."""
    state = _require_matching_config()
    version_id = state["version_id"]
    require_confirm(body.confirm, f"{version_id}/{body.plz}", "delete and regenerate the grids of this PLZ")
    generated, _ = _plz_state(version_id)
    if body.plz not in generated:
        raise HTTPException(404, f"PLZ {body.plz} has no grids in version {version_id}")
    # Region gate: the grids are deleted first, so the PLZ must still have input data to generate again.
    status = gate.classify([body.plz], coverage(request), version_id, wait_s=15)["statuses"][body.plz]
    if not status.get("selectable"):
        why = status.get("reason") or status.get("short") or "input missing"
        raise HTTPException(409, f"PLZ {body.plz} has no input data for pylovo-generate any more ({why}); "
                                 "regenerating would delete its grids and then fail.")
    if not status.get("verified"):
        raise HTTPException(409, f"The input check of PLZ {body.plz} has not finished; try again in a moment.")
    argv = chain.build(pylovo_command("pylovo-delete", "networks", "--plz", str(body.plz), "--version", version_id),
                       pylovo_command("pylovo-generate", "--plz", str(body.plz)))
    return _start(request, "generate", f"Regenerate grids · {body.plz} · v{version_id}", argv,
                  {"plz": [body.plz], "version_id": version_id, "regenerate": True})


@router.post("/reanalyse", status_code=202)
def reanalyse(body: RegionBody, request: Request) -> dict:
    """Clear the analysis of one PLZ in the config version and run ``pylovo-analyze --all`` again."""
    version_id = str(config_io.current_values().get("VERSION_ID", ""))
    require_confirm(body.confirm, f"{version_id}/{body.plz}", "delete and recompute the analysis of this PLZ")
    generated, _ = _plz_state(version_id)
    if body.plz not in generated:
        raise HTTPException(404, f"PLZ {body.plz} has no grids in version {version_id}")
    argv = chain.build([sys.executable, "-m", "pylovo_api.tasks", "clear-analysis", "--plz", str(body.plz),
                        "--version", version_id],
                       pylovo_command("pylovo-analyze", "--plz", str(body.plz), "--all"))
    return _start(request, "analyze", f"Re-analyse grids · {body.plz} · v{version_id}", argv,
                  {"plz": body.plz, "version_id": version_id, "reanalyse": True})


@router.post("/analyse-all", status_code=202)
def analyse_all(request: Request) -> dict:
    """``pylovo-analyze --plz P --all`` for every PLZ of the config version without analysis."""
    version_id = str(config_io.current_values().get("VERSION_ID", ""))
    generated, analysed = _plz_state(version_id)
    todo = sorted(generated - analysed)
    if not todo:
        raise HTTPException(409, f"Every PLZ of version {version_id} is already analysed.")
    argv = chain.build(*(pylovo_command("pylovo-analyze", "--plz", str(p), "--all") for p in todo))
    label = ", ".join(map(str, todo[:4])) + (" …" if len(todo) > 4 else "")
    return _start(request, "analyze", f"Analyse {len(todo)} PLZ · {label} · v{version_id}", argv,
                  {"plz": todo, "version_id": version_id})
