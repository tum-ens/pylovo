"""The viewer's web app: the page, its static files and the JSON/GeoJSON data routes (all GET).

Every data route takes the database as the parameter ``db`` (default: ``DBNAME`` of the ``.env``) and
answers from that database's own site (:mod:`pylovo_viewer.databases`). Responses are gzipped when
the browser accepts it; layer data is cached in memory (compressed) per database until
``/api/catalog?refresh=1``, so moving the map back and forth does not query the database again.
"""
from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi import Query as Param
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pylovo_viewer import __version__, regions, summary
from pylovo_viewer.databases import Site, Sites
from pylovo_viewer.db import Database, DatabaseError, SchemaError
from pylovo_viewer.layers import Query, to_json
from pylovo_viewer.settings import Settings

STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


class Unavailable(RuntimeError):
    """The layer exists, but not in this database."""


def host_name(header: str) -> str:
    """The host of a ``Host`` header without its port (``[::1]:8770`` gives ``::1``)."""
    name = header if header.endswith("]") else header.rsplit(":", 1)[0]
    return name.strip("[]").lower()


def parse_bbox(text: str | None) -> tuple[float, float, float, float] | None:
    """``"west,south,east,north"`` in degrees, rounded to 5 decimals (about 1 m)."""
    if not text:
        return None
    try:
        west, south, east, north = (round(float(v), 5) for v in text.split(","))
    except ValueError:
        raise ValueError("bbox must be west,south,east,north in degrees") from None
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ValueError("bbox out of range")
    return west, south, east, north


def create_app(settings: Settings, data_dir: Path, db: Database | None = None,
               connect: Callable[[Settings], Database] = Database, local_only: bool = True) -> FastAPI:
    """The app for the databases of the server in ``settings``; ``data_dir`` holds downloaded data (eTraGo).

    ``db`` is the connection to ``settings.dbname`` if already open; ``connect`` opens the others.
    ``local_only`` answers only requests addressed to :data:`LOCAL_HOSTS`, so a web page cannot read the
    viewer through a domain of its own that resolves to this machine (DNS rebinding).
    """
    sites = Sites(settings, data_dir, db=db, connect=connect)
    app = FastAPI(title="pylovo viewer", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.sites = sites

    if local_only:
        @app.middleware("http")
        async def local_hosts_only(request: Request, call_next: Any) -> Response:
            if host_name(request.headers.get("host", "")) not in LOCAL_HOSTS:
                return JSONResponse({"detail": "the viewer answers only requests to 127.0.0.1 or localhost"},
                                    status_code=403)
            return await call_next(request)

    def site(name: str | None) -> Site:
        if name is not None and len(name) > 64:
            raise ValueError("db too long")
        return sites.get(name)

    def send(request: Request, body: bytes, media_type: str, key: Any = None, at: Site | None = None) -> Response:
        headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
        if "gzip" not in request.headers.get("accept-encoding", "") or len(body) < 1024:
            return Response(body, media_type=media_type, headers=headers)
        cache = at.cache if at is not None and key is not None else None
        packed = cache.get(("gz", key)) if cache is not None else None
        if packed is None:
            packed = gzip.compress(body, compresslevel=5, mtime=0)
            if cache is not None:
                cache.put(("gz", key), packed)
        return Response(packed, media_type=media_type, headers={**headers, "Content-Encoding": "gzip"})

    def send_json(request: Request, data: Any) -> Response:
        body = json.dumps(data, default=to_json, separators=(",", ":"), ensure_ascii=False).encode()
        return send(request, body, "application/json")

    def query(version: str | None = None, mv: str | None = None, plz: int | None = None, area: int | None = None,
              grid: int | None = None, bbox: str | None = None) -> Query:
        for name, value in (("version", version), ("mv", mv)):
            if value is not None and len(value) > 64:
                raise ValueError(f"{name} too long")
        return Query(version=version or None, mv=mv or None, plz=plz, area=area, grid=grid, bbox=parse_bbox(bbox))

    @app.exception_handler(ValueError)
    def bad_request(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(LookupError)
    def not_found(_: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse({"detail": str(exc).strip("'")}, status_code=404)

    @app.exception_handler(Unavailable)
    def unavailable(_: Request, exc: Unavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(SchemaError)
    def schema_error(_: Request, exc: SchemaError) -> JSONResponse:
        return JSONResponse({"detail": f"not in this database: {exc}"}, status_code=409)

    @app.exception_handler(DatabaseError)
    def database_error(_: Request, exc: DatabaseError) -> JSONResponse:
        return JSONResponse({"detail": f"database: {exc}"}, status_code=503)

    # ------------------------------------------------------------------------------------ page
    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ------------------------------------------------------------------------------------- api
    @app.get("/api/info")
    def info(request: Request) -> Response:
        return send_json(request, {"version": __version__, "database": settings.public(), "read_only": True,
                                   "data_dir": str(data_dir)})

    @app.get("/api/databases")
    def databases(request: Request, refresh: bool = False) -> Response:
        return send_json(request, {"default": sites.default, "databases": sites.names(refresh=refresh)})

    @app.get("/api/catalog")
    def layer_catalog(request: Request, db: str | None = None, refresh: bool = False) -> Response:
        s = site(db)
        if refresh:
            s.catalog.refresh()
            s.service.clear()
            s.cache.clear()
        return send_json(request, {"database": db or sites.default, "groups": s.service.groups(),
                                   "layers": s.service.describe(), "lod2": s.catalog.citydb_classes is not None,
                                   "mv_variants": bool(s.catalog.has_rows.get("pylovo.mv_variant")),
                                   "summary": s.catalog.summary()})

    @app.get("/api/versions")
    def versions(request: Request, db: str | None = None) -> Response:
        s = site(db)
        return send_json(request, regions.versions(s.db, s.catalog))

    @app.get("/api/postcodes")
    def postcodes(request: Request, version: str, db: str | None = None) -> Response:
        s = site(db)
        return send_json(request, regions.postcodes(s.db, s.catalog, version))

    @app.get("/api/areas")
    def areas(request: Request, version: str, mv: str, db: str | None = None) -> Response:
        s = site(db)
        return send_json(request, regions.areas(s.db, s.catalog, version, mv))

    @app.get("/api/bounds")
    def bounds(request: Request, version: str | None = None, mv: str | None = None, plz: int | None = None,
               area: int | None = None, grid: int | None = None, db: str | None = None) -> Response:
        s = site(db)
        return send_json(request, {"bounds": regions.bounds(s.db, s.catalog, version, mv, plz, area, grid)})

    @app.get("/api/search")
    def search(request: Request, q: str = Param("", max_length=100), version: str | None = None,
               mv: str | None = None, db: str | None = None) -> Response:
        s = site(db)
        return send_json(request, regions.search(s.db, s.catalog, q, version, mv))

    @app.get("/api/layer/{layer_id}")
    def layer(request: Request, layer_id: str, version: str | None = None, mv: str | None = None,
              plz: int | None = None, area: int | None = None, grid: int | None = None,
              bbox: str | None = None, db: str | None = None) -> Response:
        s = site(db)
        if layer_id not in s.service.layers:
            raise LookupError(f"no layer {layer_id}")
        reason = s.service.unavailable(s.service.layers[layer_id])
        if reason:
            raise Unavailable(reason)
        q = query(version, mv, plz, area, grid, bbox)
        key = (layer_id, q.key())
        body = s.cache.get(key)
        if body is None:
            body = s.cache.put(key, s.service.geojson(layer_id, q).encode())
        return send(request, body, "application/geo+json", key, s)

    @app.get("/api/feature/{layer_id}/{fid}")
    def feature(request: Request, layer_id: str, fid: str, version: str | None = None,
                mv: str | None = None, db: str | None = None) -> Response:
        s = site(db)
        if layer_id not in s.service.layers:
            raise LookupError(f"no layer {layer_id}")
        return send_json(request, s.service.feature(layer_id, fid, query(version, mv)))

    @app.get("/api/section/{cable}")
    def section(request: Request, cable: int, db: str | None = None) -> Response:
        return send_json(request, site(db).service.section(cable))

    @app.get("/api/summary")
    def key_figures(request: Request, version: str, mv: str | None = None, plz: int | None = None,
                    area: int | None = None, grid: int | None = None, db: str | None = None) -> Response:
        s = site(db)
        q = query(version, mv, plz, area, grid)
        lv_q = q if q.region() != "area" or q.mv else Query(version=version)
        return send_json(request, {"lv": summary.lv(s.db, s.catalog, lv_q) if s.catalog.has("pylovo.grid_result")
                                   else None, "mv": summary.mv(s.db, s.catalog, q)})

    @app.get("/api/lod2")
    def lod2(request: Request, version: str, bbox: str, db: str | None = None) -> Response:
        from pylovo_viewer.lod2 import view_mesh
        s = site(db)
        q = query(version, bbox=bbox)
        key = ("lod2", q.key())
        body = s.cache.get(key)
        if body is None:
            body = view_mesh(s.db, s.catalog, q)
            if s.catalog.citydb_classes is not None:
                s.cache.put(key, body)
        return send(request, body, "application/octet-stream", key, s)

    return app
