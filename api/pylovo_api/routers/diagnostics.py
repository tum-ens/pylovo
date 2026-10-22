"""Grid diagnostics: why a grid misses a voltage or loading limit (read-only)."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from pylovo_api import diagnostics, diagnostics_data

router = APIRouter(prefix="/api", tags=["diagnostics"])


@router.get("/diagnostics/rules")
def rules() -> dict:
    """The rule catalogue and the heuristic thresholds in effect."""
    overrides = diagnostics_data.threshold_overrides()
    thresholds = diagnostics.effective_thresholds(overrides)
    return {"rules": diagnostics.RULES, "categories": diagnostics.CATEGORIES, "thresholds": thresholds,
            "overrides": sorted(k for k in overrides if k in thresholds)}


@router.get("/grids/{grid_result_id}/diagnostics")
async def grid_diagnostics(grid_result_id: int, load_scaling: float = Query(1.0, gt=0, le=5),
                           cached: bool = True) -> dict:
    """Findings of one grid. Reuses a cached on-demand power flow at ``load_scaling`` if there is one.

    Before the power flow the symptoms come from the stored generation check and the design data,
    and the attribution is a linearised estimate. ``cached=false`` ignores a cached power flow (the
    inspector asks for this while it shows no power-flow result). Nothing is written to the database.
    """
    result = await run_in_threadpool(diagnostics_data.grid_diagnostics, grid_result_id, load_scaling, None, cached)
    if result is None:
        raise HTTPException(404, "Grid not found")
    return result


@router.get("/results/{version_id}/diagnostics")
async def version_diagnostics(version_id: str, plz: Annotated[list[int] | None, Query()] = None) -> dict:
    """Per-grid finding counts of a version from the rules that need no power flow (Statistics)."""
    return await run_in_threadpool(diagnostics_data.version_diagnostics, version_id, plz)
