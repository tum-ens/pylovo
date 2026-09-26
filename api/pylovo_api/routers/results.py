"""Generated results: versions, statistics, grid maps, pandapower downloads, power flow, LoD2 models."""
from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from pylovo_api import diagnostics_data, load_edit_service, lod2, powerflow, queries

router = APIRouter(prefix="/api", tags=["results"])
log = logging.getLogger(__name__)


@router.get("/versions")
def versions() -> list[dict]:
    """All versions with PLZ, grid and analysis counts."""
    return load_edit_service.annotate_versions(queries.versions())


@router.get("/versions/compare")
def compare(a: str, b: str) -> dict:
    """Generation parameters of two versions side by side."""
    try:
        return queries.compare_parameters(a, b)
    except KeyError as exc:
        raise HTTPException(404, "Version not found") from exc


@router.get("/versions/{version_id}")
def version(version_id: str) -> dict:
    """One version including its stored generation parameters."""
    row = queries.version_parameters(version_id)
    if not row:
        raise HTTPException(404, f"Version {version_id} not found")
    return row


@router.get("/results/{version_id}/summary")
def summary(version_id: str, plz: Annotated[list[int] | None, Query()] = None) -> dict:
    """KPIs and chart data of a version (all PLZ, or the given ones)."""
    return load_edit_service.annotate_summary(queries.results_summary(version_id, plz), version_id)


@router.get("/results/{version_id}/{plz}/map")
def overview(version_id: str, plz: int, offsets: bool = True) -> dict:
    """All grids of one PLZ as GeoJSON layers (cables, stations, buildings)."""
    return load_edit_service.annotate_overview(queries.overview_geojson(version_id, plz, offsets), version_id, plz)


@router.get("/grids/{grid_result_id}")
def grid(grid_result_id: int, offsets: bool = True) -> dict:
    """One grid with KPIs, feeders and GeoJSON layers."""
    detail = load_edit_service.annotate_grid_detail(queries.grid_detail(grid_result_id, offsets))
    if detail is None:
        raise HTTPException(404, "Grid not found")
    return detail


@router.get("/grids/{grid_result_id}/pandapower.json")
def grid_download(grid_result_id: int) -> Response:
    """The stored pandapower network (load it with ``pandapower.from_json``)."""
    stored = queries.grid_json(grid_result_id)
    if stored is None:
        raise HTTPException(404, "Grid not found")
    grid, ident = stored
    name = f"pylovo_v{ident['version_id']}_{ident['plz']}_k{ident['kcid']}_b{ident['bcid']}.json"
    return Response(json.dumps(grid), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/grids/{grid_result_id}/lod2")
def grid_lod2(grid_result_id: int, request: Request) -> Response:
    """LoD2 models of the grid's buildings from ``citydb`` as a binary mesh (format: :func:`pylovo_api.lod2.encode`).

    Buildings without LoD2, or a database without ``citydb``, give an empty mesh and never an error:
    the map then shows those buildings in 2D.
    """
    try:
        blob = lod2.grid_lod2(grid_result_id)
    except Exception as exc:  # noqa: BLE001 - the 3D view is optional; fall back to 2D
        log.warning("LoD2 of grid %s could not be read: %s", grid_result_id, exc)
        blob = lod2.encode({"grid_result_id": grid_result_id, "available": False,
                            "reason": "the LoD2 data could not be read"})
    headers = {"Cache-Control": "private, max-age=300"}
    if "gzip" in request.headers.get("accept-encoding", ""):
        return Response(lod2.gzipped(blob), media_type="application/octet-stream",
                        headers={**headers, "Content-Encoding": "gzip"})
    return Response(blob, media_type="application/octet-stream", headers=headers)


@router.post("/grids/{grid_result_id}/powerflow")
async def grid_powerflow(grid_result_id: int, load_scaling: float = Query(1.0, gt=0, le=5)) -> dict:
    """Run a pandapower power flow now (nothing is stored)."""
    result = await run_in_threadpool(powerflow.run, grid_result_id, load_scaling)
    if result is None:
        raise HTTPException(404, "Grid not found")
    # Grid diagnostics: keep the result for GET .../diagnostics and refresh the findings in one go.
    result["diagnostics"] = await run_in_threadpool(diagnostics_data.after_powerflow, result)
    return result
