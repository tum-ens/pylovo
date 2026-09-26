"""Transformer editor (replaces the old Flask ``pylovo-import transformers-ui``).

Reads come from :mod:`pylovo_api.queries` (GeoJSON with source classification); every edit
goes through the ``*_trafo_ui`` methods of ``DatabaseClient`` so the UI and the library share
one implementation.
"""
from __future__ import annotations

import io
import re
import time
import uuid
from typing import Annotated

import pandas as pd
import requests
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from pylovo_api import config_io, db, queries
from pylovo_api.deps import ensure_no_writer, require_confirm
from pylovo_api.settings import paths

router = APIRouter(prefix="/api/transformers", tags=["transformers"])

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class NewTransformer(BaseModel):
    plz: int
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    transformer_rated_power: int | None = Field(None, ge=1, le=100_000)


class CapacityUpdate(BaseModel):
    transformer_rated_power: int = Field(ge=1, le=100_000)


class BulkUpdate(BaseModel):
    plz: int
    method: str = Field(pattern="^(uniform|percentage)$")
    transformer_rated_power: int | None = Field(None, ge=1, le=100_000)
    capacity_distribution: dict[int, float] | None = None
    confirm: bool = False


class ClearCapacities(BaseModel):
    plz: int
    confirm: str | None = None


def capacity_options() -> list[dict]:
    """Transformer sizes of the current ``TRANSFORMERS`` config (read fresh)."""
    items = config_io.current_values().get("TRANSFORMERS") or []
    options = [{"kva": int(t["s_max_kva"]), "name": t.get("name"), "cost_eur": t.get("cost_eur")}
               for t in items if isinstance(t, dict) and t.get("s_max_kva")]
    return sorted(options, key=lambda o: o["kva"])


@router.get("/capacities")
def capacities() -> list[dict]:
    """Transformer sizes offered by the editor (from ``TRANSFORMERS`` in the config)."""
    return capacity_options()


@router.get("")
def list_transformers(plz: int | None = None, bbox: str | None = None) -> dict:
    """Transformers of a PLZ (spatial intersection with its polygon) or of a map bounding box."""
    try:
        box = queries.parse_bbox(bbox)
        return queries.transformers(plz=plz, bbox=box)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("", status_code=201)
def add_transformer(body: NewTransformer, request: Request) -> dict:
    """Add a manual transformer position (``osm_id = manual/<epoch-ms>``)."""
    ensure_no_writer(request)
    osm_id = f"manual/{int(time.time() * 1000)}"
    with db.database_client() as dbc:
        result = dbc.add_transformer_position_trafo_ui(
            plz=body.plz, geom_wkt=f"POINT({body.lon:.7f} {body.lat:.7f})", osm_id=osm_id,
            transformer_rated_power=body.transformer_rated_power)
    return {"osm_id": result}


@router.patch("/{osm_id:path}")
def update_capacity(osm_id: str, body: CapacityUpdate, request: Request) -> dict:
    """Set the rated power of one transformer."""
    ensure_no_writer(request)
    with db.database_client() as dbc:
        ok = dbc.update_transformer_capacity_trafo_ui(osm_id, body.transformer_rated_power)
    if not ok:
        raise HTTPException(404, f"Transformer {osm_id} not found")
    return {"osm_id": osm_id, "transformer_rated_power": body.transformer_rated_power}


@router.delete("/{osm_id:path}")
def delete_transformer(osm_id: str, request: Request, force: bool = False) -> dict:
    """Delete one transformer. Positions of generated grids that use it are removed as well."""
    ensure_no_writer(request)
    usage = queries.transformer_usage(osm_id)
    if not usage:
        raise HTTPException(404, f"Transformer {osm_id} not found")
    if usage["used_by_grids"] and not force:
        raise HTTPException(409, f"{osm_id} is the station of {usage['used_by_grids']} generated grid(s); "
                                 "deleting it also deletes their transformer_positions rows. Pass force=true.")
    with db.database_client() as dbc:
        ok = dbc.delete_transformer_by_osm_id_trafo_ui(osm_id)
    if not ok:
        raise HTTPException(404, f"Transformer {osm_id} not found")
    return {"deleted": osm_id}


@router.post("/bulk")
def bulk_update(body: BulkUpdate, request: Request) -> dict:
    """Set all capacities of a PLZ to one size, or distribute sizes randomly by percentage."""
    ensure_no_writer(request)
    if not body.confirm:
        raise HTTPException(400, "Bulk updates overwrite every capacity in the PLZ; send confirm=true.")
    with db.database_client() as dbc:
        if body.method == "uniform":
            if not body.transformer_rated_power:
                raise HTTPException(400, "transformer_rated_power is required for the uniform method")
            ok = dbc.bulk_update_capacities_uniform_trafo_ui(body.plz, body.transformer_rated_power)
        else:
            distribution = {int(k): float(v) for k, v in (body.capacity_distribution or {}).items() if v > 0}
            if not distribution or abs(sum(distribution.values()) - 100) > 0.5:
                raise HTTPException(400, "The capacity distribution must add up to 100 %")
            ok = dbc.bulk_update_capacities_percentage_trafo_ui(body.plz, distribution)
    if not ok:
        raise HTTPException(404, f"No transformers found in PLZ {body.plz}")
    return {"updated": True}


@router.post("/clear-capacities")
def clear_capacities(body: ClearCapacities, request: Request) -> dict:
    """Remove the rated power of every transformer in a PLZ."""
    ensure_no_writer(request)
    require_confirm(body.confirm, str(body.plz), "clear all capacities of this PLZ")
    with db.database_client() as dbc:
        ok = dbc.clear_capacities_trafo_ui(body.plz)
    if not ok:
        raise HTTPException(500, "Clearing capacities failed")
    return {"cleared": True}


# --------------------------------------------------------------------------- OSM relation lookup
@router.get("/osm-relations")
def osm_relations(q: str = Query(..., min_length=2, max_length=120)) -> list[dict]:
    """Find OSM boundary relations by name (Nominatim) to fill in ``--relation-id``."""
    try:
        response = requests.get(
            NOMINATIM_URL, timeout=15,
            params={"q": q, "format": "jsonv2", "limit": 10, "addressdetails": 0, "extratags": 0},
            headers={"User-Agent": "pylovo-api (https://github.com/tum-ens/pylovo)"})
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(502, f"Nominatim is not reachable: {exc}") from exc
    hits = []
    for item in response.json():
        if item.get("osm_type") != "relation":
            continue
        bbox = [float(v) for v in item.get("boundingbox", [])] if item.get("boundingbox") else None
        area_deg2 = abs((bbox[1] - bbox[0]) * (bbox[3] - bbox[2])) if bbox else None
        hits.append({"relation_id": int(item["osm_id"]), "name": item.get("display_name"),
                     "category": item.get("category"), "type": item.get("type"),
                     "addresstype": item.get("addresstype"),
                     "bounds": [bbox[2], bbox[0], bbox[3], bbox[1]] if bbox else None,
                     "approx_km2": round(area_deg2 * 111 * 74, 1) if area_deg2 else None})
    return hits


# --------------------------------------------------------------------------- DSO CSV
def _read_dso_csv(raw: bytes, source: str | None) -> pd.DataFrame:
    """Parse a DSO CSV like ``pylovo.data_import.dso_transformers`` does (for the preview)."""
    df = pd.read_csv(io.BytesIO(raw))
    missing = {"external_id", "lon", "lat"} - set(df.columns)
    if missing:
        raise ValueError(f"Missing required column(s): {', '.join(sorted(missing))}")
    df["external_id"] = df["external_id"].astype(str).str.strip()
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    if "transformer_rated_power" in df.columns:
        df["transformer_rated_power"] = pd.to_numeric(df["transformer_rated_power"], errors="coerce")
    else:
        df["transformer_rated_power"] = pd.NA
    if source:
        df["source"] = source
    elif "source" not in df.columns:
        df["source"] = "csv"
    df["source"] = df["source"].map(lambda s: re.sub(r"[^a-z0-9_.-]+", "_", str(s or "csv").strip().lower()) or "csv")
    df["osm_id"] = "dso/" + df["source"] + "/" + df["external_id"]
    return df


def _upload_path(upload_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", upload_id or ""):
        raise HTTPException(400, "Invalid upload id")
    path = paths().uploads_dir / f"{upload_id}.csv"
    if not path.exists():
        raise HTTPException(404, "Upload not found (it may have expired); upload the file again")
    return path


@router.post("/dso-csv/preview")
async def dso_preview(file: Annotated[UploadFile, File()], source: str | None = None) -> dict:
    """Store an uploaded DSO CSV and return a validation preview (nothing is imported yet)."""
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "The CSV is larger than 20 MB")
    try:
        df = _read_dso_csv(raw, source)
    except (ValueError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise HTTPException(400, f"Cannot read the CSV: {exc}") from exc
    invalid = df[df["external_id"].eq("") | df["lon"].isna() | df["lat"].isna()]
    out_of_range = df[(df["lon"].abs() > 180) | (df["lat"].abs() > 90)]
    upload_id = uuid.uuid4().hex[:12]
    (paths().uploads_dir / f"{upload_id}.csv").write_bytes(raw)
    valid = df.drop(index=invalid.index.union(out_of_range.index))
    existing = 0
    if len(valid):
        row = db.fetch_one("SELECT count(*) AS n FROM pylovo.transformers WHERE osm_id = ANY(%s)",
                           (valid["osm_id"].tolist(),))
        existing = row["n"] if row else 0
    points = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(r.lon, 6), round(r.lat, 6)]},
               "properties": {"osm_id": r.osm_id, "transformer_rated_power":
                              None if pd.isna(r.transformer_rated_power) else int(r.transformer_rated_power)}}
              for r in valid.head(5000).itertuples()]
    capacities = valid["transformer_rated_power"].dropna().astype(int).value_counts().sort_index()
    return {
        "upload_id": upload_id, "filename": file.filename, "rows": len(df), "valid_rows": len(valid),
        "invalid_rows": [int(i) + 2 for i in invalid.index[:50]],
        "out_of_range_rows": [int(i) + 2 for i in out_of_range.index[:50]],
        "sources": sorted(df["source"].unique().tolist()),
        "existing_ids": existing, "with_capacity": int(valid["transformer_rated_power"].notna().sum()),
        "capacities": [{"kva": int(k), "count": int(v)} for k, v in capacities.items()],
        "columns": list(df.columns),
        "sample": valid.head(8)[["osm_id", "lon", "lat", "transformer_rated_power"]].astype(object)
                       .where(valid.head(8)[["osm_id", "lon", "lat", "transformer_rated_power"]].notna(), None)
                       .to_dict("records"),
        "bounds": [float(valid.lon.min()), float(valid.lat.min()), float(valid.lon.max()), float(valid.lat.max())]
        if len(valid) else None,
        "points": {"type": "FeatureCollection", "features": points},
    }


def upload_path(upload_id: str):
    """Path of a stored upload (used by the import job)."""
    return _upload_path(upload_id)
