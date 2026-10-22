"""Region search (PLZ / AGS / municipality name) and postcode polygons.

Every region payload carries the region gate's verdict (``selectable`` and a brief ``input``
status, see :mod:`pylovo_api.coverage`); the gate endpoints themselves live in
:mod:`pylovo_api.routers.coverage`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from pylovo_api import queries
from pylovo_api.coverage import brief, read_flags, status_texts
from pylovo_api.deps import coverage

router = APIRouter(prefix="/api/regions", tags=["regions"])


@router.get("/search")
def search(request: Request, q: str = Query(..., min_length=1, max_length=80),
           limit: int = Query(60, ge=1, le=200), only_selectable: bool = False) -> list[dict]:
    """Search the municipal register and ``pylovo.postcode`` by PLZ, AGS or name."""
    cov = coverage(request)
    flags = read_flags()
    selectable = cov.selectable_set(flags)
    rows = queries.search_regions(q, limit, sel=sorted(selectable))
    statuses = cov.statuses([r["plz"] for r in rows], flags, detail=False)
    digits = "".join(ch for ch in q if ch.isdigit()).lstrip("0")
    for row in rows:
        row["has_geometry"] = row["available"]  # 'available' is kept as a deprecated alias
        row["input"] = brief(statuses[row["plz"]])
        row["selectable"] = row["input"]["selectable"]
    if only_selectable:
        rows = [r for r in rows if r["selectable"] or r["versions"]]
    rank = {"pending": 1, "unknown": 1}
    rows.sort(key=lambda r: (str(r["plz"]) != digits, not r["selectable"], rank.get(r["input"]["severity"], 0),
                             r["name_city"] or "", r["plz"]))
    cov.hint([r["plz"] for r in rows if r["input"]["severity"] == "pending"])
    return rows


@router.get("/available")
def available(request: Request, limit: int = Query(200, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict:
    """The postcodes that have input data (checked and selectable), for the list under an empty search.

    Rows have the shape of ``/search`` rows. ``pending`` counts the postcodes whose check has not
    finished yet; they are selectable (*not verified*) but not listed here.
    """
    cov = coverage(request)
    flags = read_flags()
    ready = cov.ready_set(flags)
    rows, total = queries.regions_for_plz(sorted(ready), limit, offset)
    statuses = cov.statuses([r["plz"] for r in rows], flags, detail=False)
    for row in rows:
        row["has_geometry"] = row["available"]
        row["input"] = brief(statuses[row["plz"]])
        row["selectable"] = row["input"]["selectable"]
    info = cov.brief_status()
    return {"rows": rows, "total": total, "plz_total": len(ready), "offset": offset,
            "pending": info.get("pending", 0), "state": info["state"], "version": info["version"]}


@router.get("/overview")
def overview(request: Request) -> dict:
    """Extent of all postcode polygons, the PLZ with generated grids and the extent of the selectable PLZ."""
    data = queries.regions_overview()
    info = coverage(request).coverage()
    data["ready_bounds"] = info["ready_bounds"]
    data["input_counts"] = info["counts"]
    return data


@router.get("/postcodes")
def postcodes(request: Request, bbox: str | None = None, zoom: float | None = None,
              plz: Annotated[list[int] | None, Query()] = None, only_selectable: bool = False) -> dict:
    """Postcode polygons (GeoJSON, EPSG:4326) in a bounding box ``minLon,minLat,maxLon,maxLat``."""
    try:
        box = queries.parse_bbox(bbox)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cov = coverage(request)
    flags = read_flags()
    selectable = cov.selectable_set(flags)
    fc = queries.postcodes_geojson(box, zoom, plz, first=sorted(selectable), only_first=only_selectable)
    statuses = cov.statuses([f["properties"]["plz"] for f in fc["features"]], flags, detail=False)
    for feature in fc["features"]:
        props = feature["properties"]
        st = statuses[props["plz"]]
        props.update(status=st["status"], severity=st["severity"], selectable=st["selectable"],
                     verified=st["verified"], input_short=st["short"],
                     n_buildings=(st.get("counts") or {}).get("buildings_importable"),
                     partial=any(w["code"].startswith("partial") for w in st["warnings"]))
    info = cov.brief_status()
    fc["input"] = {"version": info["version"], "state": info["state"], "flags_key": info["flags_key"],
                   "texts": status_texts()}
    return fc


@router.get("/ags/{ags}")
def ags(ags: int, request: Request) -> list[dict]:
    """All PLZ of a municipality key (AGS)."""
    rows = queries.ags_regions(ags)
    statuses = coverage(request).statuses([r["plz"] for r in rows], detail=False)
    for row in rows:
        row["input"] = brief(statuses[row["plz"]])
        row["selectable"] = row["input"]["selectable"]
    return rows


@router.get("/{plz}")
def region(plz: int, request: Request, fresh: bool = False) -> dict:
    """Postcode details: municipality, transformers by source, generated versions, input data (region gate)."""
    detail = queries.region_detail(plz)
    if detail is None:
        raise HTTPException(404, f"PLZ {plz} is neither in pylovo.postcode nor in the municipal register")
    cov = coverage(request)
    cov.ensure([plz], wait_s=5 if fresh else 3, fresh=fresh)
    status = cov.status_of(plz)
    if detail.get("versions"):
        status["actions"].append({"kind": "results", "versions": [v["version_id"] for v in detail["versions"]],
                                  "label": "Show the generated grids"})
    detail["input"] = status
    detail["selectable"] = status["selectable"]
    detail["infdb_buildings"] = (status.get("counts") or {}).get("buildings_importable")  # compat
    return detail
