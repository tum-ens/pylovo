"""The viewer's web app: the page, its static files and the JSON/GeoJSON data routes (all GET).

Responses are gzipped when the browser accepts it; layer data is cached in memory (compressed) until
``/api/catalog?refresh=1``, so moving the map back and forth does not query the database again.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi import Query as Param
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from pylovo_viewer import __version__, regions, summary
from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database, DatabaseError
from pylovo_viewer.layers import LRU, LayerService, Query, to_json
from pylovo_viewer.settings import Settings

STATIC = Path(__file__).parent / "static"


class Unavailable(RuntimeError):
    """The layer exists, but not in this database."""


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


def create_app(settings: Settings, data_dir: Path, db: Database | None = None) -> FastAPI:
    """The app for the database of ``settings``; ``data_dir`` holds downloaded data (eTraGo)."""
    db = db or Database(settings)
    catalog = Catalog(db)
    service = LayerService(db, catalog, data_dir)
    cache = LRU(64)
    app = FastAPI(title="pylovo viewer", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db, app.state.catalog, app.state.service = db, catalog, service

    def send(request: Request, body: bytes, media_type: str, key: Any = None) -> Response:
        headers = {"Cache-Control": "no-store", "Vary": "Accept-Encoding"}
        if "gzip" not in request.headers.get("accept-encoding", "") or len(body) < 1024:
            return Response(body, media_type=media_type, headers=headers)
        packed = cache.get(("gz", key)) if key is not None else None
        if packed is None:
            packed = gzip.compress(body, compresslevel=5, mtime=0)
            if key is not None:
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
                                   "catalog": catalog.summary(), "data_dir": str(data_dir)})

    @app.get("/api/catalog")
    def layer_catalog(request: Request, refresh: bool = False) -> Response:
        if refresh:
            catalog.refresh()
            service.clear()
            cache.clear()
        return send_json(request, {"groups": service.groups(), "layers": service.describe(),
                                   "lod2": catalog.citydb_classes is not None})

    @app.get("/api/versions")
    def versions(request: Request) -> Response:
        return send_json(request, regions.versions(db, catalog))

    @app.get("/api/postcodes")
    def postcodes(request: Request, version: str) -> Response:
        return send_json(request, regions.postcodes(db, version))

    @app.get("/api/areas")
    def areas(request: Request, version: str, mv: str) -> Response:
        return send_json(request, regions.areas(db, version, mv))

    @app.get("/api/bounds")
    def bounds(request: Request, version: str | None = None, mv: str | None = None, plz: int | None = None,
               area: int | None = None, grid: int | None = None) -> Response:
        return send_json(request, {"bounds": regions.bounds(db, version, mv, plz, area, grid)})

    @app.get("/api/search")
    def search(request: Request, q: str = Param("", max_length=100), version: str | None = None,
               mv: str | None = None) -> Response:
        return send_json(request, regions.search(db, catalog, q, version, mv))

    @app.get("/api/layer/{layer_id}")
    def layer(request: Request, layer_id: str, version: str | None = None, mv: str | None = None,
              plz: int | None = None, area: int | None = None, grid: int | None = None,
              bbox: str | None = None) -> Response:
        if layer_id not in service.layers:
            raise LookupError(f"no layer {layer_id}")
        reason = service.unavailable(service.layers[layer_id])
        if reason:
            raise Unavailable(reason)
        q = query(version, mv, plz, area, grid, bbox)
        key = (layer_id, q.key())
        body = cache.get(key)
        if body is None:
            body = cache.put(key, service.geojson(layer_id, q).encode())
        return send(request, body, "application/geo+json", key)

    @app.get("/api/feature/{layer_id}/{fid}")
    def feature(request: Request, layer_id: str, fid: str, version: str | None = None,
                mv: str | None = None) -> Response:
        if layer_id not in service.layers:
            raise LookupError(f"no layer {layer_id}")
        return send_json(request, service.feature(layer_id, fid, query(version, mv)))

    @app.get("/api/summary")
    def key_figures(request: Request, version: str, mv: str | None = None, plz: int | None = None,
                    area: int | None = None, grid: int | None = None) -> Response:
        q = query(version, mv, plz, area, grid)
        lv_q = q if q.region() != "area" or q.mv else Query(version=version)
        return send_json(request, {"lv": summary.lv(db, lv_q) if catalog.has("pylovo.grid_result") else None,
                                   "mv": summary.mv(db, catalog, q)})

    @app.get("/api/lod2")
    def lod2(request: Request, version: str, bbox: str) -> Response:
        from pylovo_viewer.lod2 import view_mesh
        q = query(version, bbox=bbox)
        key = ("lod2", q.key())
        body = cache.get(key)
        if body is None:
            body = view_mesh(db, catalog, q)
            if catalog.citydb_classes is not None:
                cache.put(key, body)
        return send(request, body, "application/octet-stream", key)

    return app
