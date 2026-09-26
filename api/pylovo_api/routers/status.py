"""Health check, database status and global metadata."""
from __future__ import annotations

import platform

from fastapi import APIRouter, Request
from pydantic import BaseModel

from pylovo_api import (
    API_VERSION,
    __version__,
    code_state,
    config_io,
    load_edit_service,
    queries,
)
from pylovo_api.deps import coverage, jobs
from pylovo_api.settings import paths

router = APIRouter(prefix="/api", tags=["status"])


class Health(BaseModel):
    """Answer of ``GET /api/health``."""

    ok: bool
    service: str
    api: int
    version: str
    revision: str | None


@router.get("/health")
def health() -> Health:
    """Liveness and contract version, without database access (container health checks, UI version check).

    ``api`` is the contract version (bumped only for breaking changes), ``version`` the pylovo
    package version, ``revision`` the git commit of the running code (``null`` if unknown).
    """
    return Health(ok=True, service="pylovo-api", api=API_VERSION, version=__version__,
                  revision=code_state.STARTED_REVISION)


@router.get("/status")
def get_status(request: Request) -> dict:
    """Database connection, schema state, table counts, config version and the running job."""
    info = queries.status()
    values = config_io.current_values()
    version_id = str(values.get("VERSION_ID", ""))
    info["config"] = {
        "version_id": version_id,
        "version_comment": values.get("VERSION_COMMENT"),
        "use_open_transformer_positions": values.get("USE_OPEN_TRANSFORMER_POSITIONS"),
        "use_dso_transformer_positions": values.get("USE_DSO_TRANSFORMER_POSITIONS"),
        "use_manual_transformer_positions": values.get("USE_MANUAL_TRANSFORMER_POSITIONS"),
        "version": queries.version_exists(version_id) if info.get("connected") else {"exists": False},
        "error": info.get("error") if info.get("config_error") else None,
    }
    busy = jobs(request).running_writer()
    info["running_job"] = busy.summary() if busy else None
    # Region gate (in memory; the checks themselves run in the coverage worker).
    info["coverage"] = coverage(request).brief_status()
    if info["coverage"].get("buildings_total") is not None and info["coverage"]["state"] == "ready":
        info["infdb_buildings"] = info["coverage"]["buildings_total"]
    info["load_edit"] = load_edit_service.status_info() if info.get("connected") else None
    info["ui"] = {"version": __version__, "root": str(paths().root), "python": platform.python_version(),
                  "code": code_state.state()}
    return info
