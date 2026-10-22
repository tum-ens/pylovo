"""Region gate: input coverage of InfDB (or the building shapefiles) per postcode.

Registered before :mod:`pylovo_api.routers.regions`, whose ``/api/regions/{plz}`` would
otherwise swallow ``/api/regions/coverage`` and ``/api/regions/input``.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from pylovo_api.coverage import flags_key, read_flags
from pylovo_api.deps import coverage

router = APIRouter(prefix="/api/regions", tags=["regions"])


class RefreshBody(BaseModel):
    scope: Literal["changes", "all"] = "changes"


@router.get("/coverage")
def get_coverage(request: Request) -> dict:
    """State of the input check: strategy per table, progress, counts, suggested indexes."""
    return coverage(request).coverage()


@router.post("/coverage/refresh", status_code=202)
def refresh_coverage(request: Request, body: RefreshBody | None = None) -> dict:
    """Run change detection now (``changes``) or recount every municipality (``all``)."""
    cov = coverage(request)
    cov.request_refresh((body or RefreshBody()).scope)
    return cov.coverage()


@router.get("/input")
def region_input(request: Request, plz: Annotated[list[int], Query()],
                 ensure: bool = False, wait_s: float = Query(5, ge=0, le=15)) -> dict:
    """InputStatus of up to 500 PLZ; ``ensure=1`` counts their municipalities first (waits ``wait_s``)."""
    if len(plz) > 500:
        raise HTTPException(400, "At most 500 PLZ per request")
    cov = coverage(request)
    if ensure:
        cov.ensure(plz, wait_s=wait_s)
    flags = read_flags()
    info = cov.brief_status()
    return {"coverage": {k: info[k] for k in ("state", "version", "flags_key", "building", "stale", "error")}
            | {"flags_key": flags_key(flags)},
            "regions": {str(p): status for p, status in cov.statuses(plz, flags).items()}}
