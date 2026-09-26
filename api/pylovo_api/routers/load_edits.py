"""Load edits of stored grids: building context, preview, apply, undo, history, export.

The logic is :class:`pylovo.load_editing.LoadEditor`. Previews are read-only and allowed while
jobs run; writes hold the job manager's edit lease (setup, import, delete and an analysis of the
same PLZ block them and are blocked by them) and lock the grid row for a short transaction.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from pylovo_api import load_edit_service as svc
from pylovo_api.deps import jobs
from pylovo_api.jobs import JobConflict

router = APIRouter(prefix="/api", tags=["load edits"])


class Changes(BaseModel):
    """New input values of one building (only the fields that change)."""

    model_config = ConfigDict(extra="forbid")
    households: int | None = None
    residential_floor_area: float | None = Field(None, allow_inf_nan=False)
    nonresidential_floor_area: float | None = Field(None, allow_inf_nan=False)
    nonresidential_use: Literal["Commercial", "Public"] | None = None

    def given(self) -> dict:
        return self.model_dump(exclude_unset=True)


class PreviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objectid: str = Field(max_length=200)
    changes: Changes


class ApplyBody(PreviewBody):
    reason: str | None = Field(None, max_length=500)
    if_match: str = Field(max_length=80)
    acknowledge: list[Literal["non_convergence"]] = Field(default_factory=list, max_length=1)
    first_version_edit_confirm: str | None = Field(None, max_length=20)


class RevertBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objectid: str = Field(max_length=200)
    reason: str | None = Field(None, max_length=500)
    if_match: str = Field(max_length=80)
    acknowledge: list[Literal["non_convergence"]] = Field(default_factory=list, max_length=1)
    first_version_edit_confirm: str | None = Field(None, max_length=20)


class IfMatchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    if_match: str | None = Field(None, max_length=80)


def _lease(request: Request, version_id: str, plz: int):
    """The job manager's edit lease for (version, PLZ); 409 if a conflicting job runs."""
    manager = jobs(request)
    busy = manager.conflicting_writer(version_id, plz)
    if busy:
        raise HTTPException(409, {"code": "writer_job", "message": f"'{busy.title}' is running. Load edits are blocked "
                                  "until it has finished.", "job": busy.summary()})
    return manager.db_edit_lease(version_id, plz)


def _grid_ident(grid_result_id: int) -> dict:
    from pylovo_api import db

    row = db.fetch_one("SELECT version_id, plz FROM pylovo.grid_result WHERE grid_result_id = %s", (grid_result_id,))
    if not row:
        raise HTTPException(404, "Grid not found")
    return row


def _grid_record(grid_result_id: int) -> dict | None:
    from pylovo_api import queries

    detail = svc.annotate_grid_detail(queries.grid_detail(grid_result_id, offsets=False))
    return detail["grid"] if detail else None


def _call(fn):
    """Run ``fn`` and map load-edit errors and job conflicts to HTTP errors with a ``{code, message}`` detail."""
    from pylovo.load_editing import (
        LoadEditError,  # pylovo is imported lazily (its config may be broken)
    )

    try:
        return fn()
    except LoadEditError as exc:
        raise HTTPException(exc.status, exc.as_dict()) from exc
    except JobConflict as exc:
        raise HTTPException(409, {"code": "writer_job", "message": str(exc)}) from exc


# --------------------------------------------------------------------------- read
@router.get("/grids/{grid_result_id}/load-edit/building")
def building_context(grid_result_id: int, request: Request, objectid: str = Query(max_length=200)) -> dict:
    """The building, its generated inputs, the version parameters, the guard results and its history."""
    def run():
        with svc.editor() as ed:
            return ed.context(grid_result_id, objectid)
    out = _call(run)
    busy = jobs(request).conflicting_writer(out["version_id"], out["plz"])
    out["conflicting_job"] = busy.title if busy else None
    out["history"] = [svc.public_history_row(r) for r in out["history"]]
    return out


@router.get("/grids/{grid_result_id}/load-edit/check")
def check(grid_result_id: int) -> dict:
    """Storage, reproduction and power-flow baseline checks of one grid (read-only)."""
    def run():
        with svc.editor() as ed:
            return ed.check(grid_result_id)
    return _call(run)


@router.post("/grids/{grid_result_id}/load-edit/preview")
async def preview(grid_result_id: int, body: PreviewBody) -> dict:
    """Before/after numbers of a change; nothing is written."""
    def run():
        with svc.editor() as ed:
            return ed.preview(grid_result_id, body.objectid, body.changes.given())
    return await run_in_threadpool(_call, run)


@router.get("/grids/{grid_result_id}/load-edits")
def grid_history(grid_result_id: int, objectid: str | None = Query(None, max_length=200)) -> list[dict]:
    """Audit rows of a grid, newest first."""
    def run():
        with svc.editor() as ed:
            return [svc.public_history_row(r) for r in ed.history(grid_result_id, objectid=objectid)]
    return _call(run)


@router.get("/versions/{version_id}/load-edits")
def version_history(version_id: str, plz: int | None = None, format: Literal["json", "csv"] = "json"):
    """All audit rows of a version (JSON or CSV with formula-injection escaping)."""
    with svc.edit_db() as dbx:
        rows = dbx.load_edit_history(version_id=version_id, plz=plz)
    if format == "csv":
        name = f"pylovo_v{version_id}_load_edits.csv".replace("/", "_")
        return Response(svc.history_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return [svc.public_history_row(r) for r in rows]


# --------------------------------------------------------------------------- write
@router.post("/grids/{grid_result_id}/load-edits", status_code=201)
async def apply(grid_result_id: int, body: ApplyBody, request: Request) -> dict:
    """Write the edit (building, pandapower loads, network JSON, validation columns, audit row)."""
    ident = _grid_ident(grid_result_id)
    lease = _lease(request, ident["version_id"], ident["plz"])

    def run():
        with svc.editor(readonly=False) as ed:
            return ed.apply(grid_result_id, body.objectid, body.changes.given(), if_match=body.if_match,
                            reason=body.reason, acknowledge=body.acknowledge,
                            first_version_edit_confirm=body.first_version_edit_confirm, write_guard=lease)
    out = await run_in_threadpool(_call, run)
    out["grid"] = _grid_record(grid_result_id)
    out["exported_file_outdated"] = _exported_file(grid_result_id)
    return out


@router.post("/grids/{grid_result_id}/load-edit/revert", status_code=201)
async def revert(grid_result_id: int, body: RevertBody, request: Request) -> dict:
    """Apply the generated inputs of a building again (new audit row, action ``revert``)."""
    ident = _grid_ident(grid_result_id)
    lease = _lease(request, ident["version_id"], ident["plz"])

    def run():
        with svc.editor(readonly=False) as ed:
            return ed.revert(grid_result_id, body.objectid, if_match=body.if_match, reason=body.reason,
                             acknowledge=body.acknowledge, first_version_edit_confirm=body.first_version_edit_confirm,
                             write_guard=lease)
    out = await run_in_threadpool(_call, run)
    out["grid"] = _grid_record(grid_result_id)
    return out


@router.post("/load-edits/{load_edit_id}/undo")
async def undo(load_edit_id: int, body: IfMatchBody, request: Request) -> dict:
    """Undo the latest active edit of a grid exactly."""
    with svc.edit_db() as dbx:
        edit = dbx.fetch_load_edit(load_edit_id) if dbx.load_edit_table_exists() else None
    if edit is None:
        raise HTTPException(404, "Load edit not found")
    lease = _lease(request, edit["version_id"], edit["plz"])

    def run():
        with svc.editor(readonly=False) as ed:
            return ed.undo(load_edit_id, if_match=body.if_match, write_guard=lease, undone_by=svc.client_label())
    out = await run_in_threadpool(_call, run)
    out["grid"] = _grid_record(edit["grid_result_id"])
    return out


@router.post("/grids/{grid_result_id}/load-edits/undo-all")
async def undo_all(grid_result_id: int, body: IfMatchBody, request: Request) -> dict:
    """Undo every active edit of a grid (newest first, one transaction, exact)."""
    ident = _grid_ident(grid_result_id)
    lease = _lease(request, ident["version_id"], ident["plz"])

    def run():
        with svc.editor(readonly=False) as ed:
            return ed.undo_all(grid_result_id, if_match=body.if_match, write_guard=lease, undone_by=svc.client_label())
    out = await run_in_threadpool(_call, run)
    out["grid"] = _grid_record(grid_result_id)
    return out


@router.post("/maintenance/refresh-views", status_code=202)
def refresh_views(request: Request) -> dict:
    """Refresh the GIS view ``buildings_result_with_grid`` in the background (edits leave it outdated)."""
    manager = jobs(request)
    busy = manager.conflicting_writer()
    if busy:
        raise HTTPException(409, {"code": "writer_job", "message": f"'{busy.title}' is running."})
    try:
        svc.refresh_views_async(manager.db_edit_lease())
    except RuntimeError as exc:
        raise HTTPException(409, {"code": "already_running", "message": str(exc)}) from exc
    return {"started": True}


def _exported_file(grid_result_id: int) -> str | None:
    """Path of a ``SAVE_GRID_FOLDER`` JSON file of the grid that is now outdated (it is not rewritten)."""
    try:
        from pathlib import Path

        from pylovo import config_loader as cl
        from pylovo_api import db

        if not cl.SAVE_GRID_FOLDER:
            return None
        row = db.fetch_one("SELECT version_id, plz, kcid, bcid FROM pylovo.grid_result WHERE grid_result_id = %s",
                           (grid_result_id,))
        path = Path(cl.RESULT_DIR, "grids", f"version_{row['version_id']}", str(row["plz"]),
                    f"kcid{row['kcid']}bcid{row['bcid']}.json")
        return str(path) if path.exists() else None
    except Exception:  # noqa: BLE001 - informational only
        return None
