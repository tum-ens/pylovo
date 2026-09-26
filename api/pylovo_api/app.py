"""FastAPI application of pylovo-api."""
from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg2
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from pylovo_api import API_VERSION, db
from pylovo_api.coverage import InputCoverage
from pylovo_api.jobs import JobManager
from pylovo_api.routers import config, coverage, flow, jobs, regions, results, status, transformers
from pylovo_api.routers import diagnostics as diagnostics_router
from pylovo_api.routers import load_edits as load_edit_routes
from pylovo_api.settings import init_paths

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}


def create_app(root: Path, allowed_hosts: set[str] | None = None, allow_any_host: bool = False) -> FastAPI:
    """Build the app for one pylovo project root.

    Args:
        root: Project directory with ``config/`` (the working directory of all jobs).
        allowed_hosts: Extra host names accepted in the ``Host`` header besides the loopback
            names (protects against DNS rebinding).
        allow_any_host: Accept every ``Host`` header (only for ``--host 0.0.0.0``).
    """
    paths = init_paths(root)
    hosts = set(allowed_hosts or ()) | LOCAL_HOSTS
    # Load pylovo's settings now: it reads config/ from the working directory and applies the
    # project's .env to os.environ, which every job subprocess inherits. A broken config must not
    # keep the UI from starting (its config editor is the way to repair it); Python retries the
    # import on the next request.
    try:
        import pylovo.config_loader  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("pylovo_api").warning("pylovo could not load its configuration: %s", exc)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.coverage.start()
        yield
        app.state.coverage.shutdown()
        app.state.jobs.shutdown()

    app = FastAPI(title="pylovo-api", version=str(API_VERSION), lifespan=lifespan,
                  description="HTTP API of the pylovo workflow (database, regions, data, generation, results) "
                              "for the GridPlanner UI.")
    app.state.jobs = JobManager(cwd=paths.root, jobs_dir=paths.jobs_dir)
    # Region gate: which PLZ have generation input (background worker, see pylovo_api.coverage).
    app.state.coverage = InputCoverage(paths.state_dir / "cache" / "input-coverage.json", jobs=app.state.jobs,
                                       root=paths.root)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        header = request.headers.get("host") or ""
        host = header.split("]")[0] + "]" if header.startswith("[") else header.rsplit(":", 1)[0]
        if not allow_any_host and host and host not in hosts:
            return JSONResponse({"detail": f"Host '{host}' is not allowed"}, status_code=421)
        # State-changing API calls must come from the UI itself: browsers only send this custom
        # header from same-origin scripts, which blocks cross-site form posts (CSRF).
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api/") \
                and request.headers.get("x-pylovo-ui") != "1":
            return JSONResponse({"detail": "Missing X-Pylovo-UI header"}, status_code=403)
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            app.state.coverage.touch()  # change checks of the region gate run only while the UI is used
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(db.DatabaseUnavailable)
    async def db_unavailable(_: Request, exc: db.DatabaseUnavailable):
        return JSONResponse({"detail": f"Database not reachable: {exc}"}, status_code=503)

    @app.exception_handler(psycopg2.errors.UndefinedTable)
    async def undefined_table(_: Request, exc: Exception):
        return JSONResponse({"detail": "The pylovo schema is missing or incomplete. Run the database setup first. "
                                       f"({str(exc).splitlines()[0]})"}, status_code=409)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        # Python's json module accepts NaN/Infinity in request bodies; the default handler echoes the
        # input in its 422 answer and then fails to encode it (500). Echo such numbers as text.
        def finite(value):
            if isinstance(value, float) and not math.isfinite(value):
                return str(value)
            if isinstance(value, dict):
                return {k: finite(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [finite(v) for v in value]
            return value
        return JSONResponse({"detail": finite(jsonable_encoder(exc.errors()))}, status_code=422)

    @app.exception_handler(psycopg2.Error)
    async def db_error(_: Request, exc: psycopg2.Error):
        return JSONResponse({"detail": f"Database error: {str(exc).strip().splitlines()[0]}"}, status_code=500)

    # coverage.router first: its /api/regions/coverage and /input must win over /api/regions/{plz}
    for router in (status.router, coverage.router, regions.router, transformers.router, config.router, jobs.router,
                   results.router, load_edit_routes.router):
        app.include_router(router)
    app.include_router(diagnostics_router.router)
    app.include_router(flow.router)  # pre-flight, regenerate / re-analyse
    return app
