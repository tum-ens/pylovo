"""The German HV/EHV grid of eGon-data (eTraGo, PyPSA format) from the Open Energy Platform, as GeoJSON.

The viewer shows "the PyPSA grid for Germany" as a context layer: the AC buses and lines (380, 220 and
110 kV) of the eGon-data eTraGo grid, built by osmTGmod from OpenStreetMap (2021) and published on the
Open Energy Platform (OEP) as ``data.edut_00_056`` (``grid.egon_etrago_bus``) and ``data.edut_00_063``
(``grid.egon_etrago_line``). :func:`fetch` downloads one scenario through the OEP's unauthenticated REST
API (sequential pages, only the needed columns), keeps the German buses and the lines between them and
writes ``lines.geojson``, ``buses.geojson`` and ``meta.json`` into :func:`dataset_dir`. The server hands
out these files (:func:`read`, :func:`status`), so the browser never calls the OEP itself.

GeoJSON in EPSG:4326, coordinates rounded to :data:`DECIMALS` decimals (about 1 m). A line follows its
OSM route (``geom``; the straight ``topo`` line when ``geom`` is missing), merged into one LineString
where its parts join up and simplified by :data:`SIMPLIFY_TOLERANCE_DEG`. The OEP metadata of these
tables names no licence. eGon-data publishes its eTraGo tables under ODbL 1.0 with the credit "© eGon
development team" (``license_egon_data_odbl`` of its metadata code), and they derive from OpenStreetMap,
hence :data:`ATTRIBUTION`.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import requests
import shapely

DEFAULT_SCENARIO = "eGon2035"
ATTRIBUTION = ("eGon-data eTraGo grid (osmTGmod, OSM 2021), © eGon development team, ODbL 1.0, derived from "
               "OpenStreetMap, © OpenStreetMap contributors")
PARTS = ("lines", "buses")

API = "https://openenergyplatform.org/api/v0/schema/data/tables/"
TABLES = {"buses": "edut_00_056", "lines": "edut_00_063"}   # data.edut_00 aliases of grid.egon_etrago_bus/_line
KEYS = {"buses": "bus_id", "lines": "line_id"}
COLUMNS = {"buses": ["bus_id", "v_nom", "type", "x", "y", "geom", "country"],
           "lines": ["line_id", "bus0", "bus1", "v_nom", "s_nom", "length", "num_parallel", "cables", "type",
                     "geom", "topo"]}
PAGE_SIZE = 5000                  # rows per request (a page of lines with geometries is about 5 MB)
RETRIES = 4                       # attempts per request on network errors, HTTP 429 and 5xx
BACKOFF_S = 3.0                   # pause before the second attempt, doubled for every further one
SIMPLIFY_TOLERANCE_DEG = 1e-4     # about 7 m east-west and 11 m north-south in Germany
DECIMALS = 5
_SCENARIO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


# --------------------------------------------------------------------------- cache
def dataset_dir(data_dir: Path, scenario: str = DEFAULT_SCENARIO) -> Path:
    """``<data_dir>/etrago/<scenario>/``; the name is checked because it may come from a request."""
    if not isinstance(scenario, str) or not _SCENARIO.fullmatch(scenario):
        raise ValueError(f"invalid eTraGo scenario name {scenario!r}")
    return Path(data_dir) / "etrago" / scenario


def status(data_dir: Path, scenario: str = DEFAULT_SCENARIO) -> dict[str, Any]:
    """Whether ``scenario`` is fetched, where, and (when it is) the fields of its ``meta.json``."""
    path = dataset_dir(data_dir, scenario)
    info = {"available": False, "scenario": scenario, "path": str(path)}
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    if not isinstance(meta, dict) or not all((path / f"{part}.geojson").is_file() for part in PARTS):
        return info
    return {**meta, **info, "available": True}


def read(data_dir: Path, part: str, scenario: str = DEFAULT_SCENARIO) -> bytes | None:
    """Raw GeoJSON of ``part`` (``lines`` or ``buses``), or ``None`` when it is not fetched yet.

    Raises:
        ValueError: unknown ``part`` or invalid ``scenario`` name.
    """
    if part not in PARTS:
        raise ValueError(f"unknown eTraGo part {part!r} (expected one of {', '.join(PARTS)})")
    try:
        return (dataset_dir(data_dir, scenario) / f"{part}.geojson").read_bytes()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to a temporary file next to ``path`` and rename it, so readers never see half a file."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- OEP
def _get(session: requests.Session, url: str, params: dict[str, Any], timeout: float) -> list[dict[str, Any]]:
    """Rows of one OEP request, retried with a growing pause on network errors, HTTP 429 and 5xx."""
    full_url = requests.Request("GET", url, params=params).prepare().url
    error = ""
    for attempt in range(RETRIES):
        if attempt:
            time.sleep(BACKOFF_S * 2 ** (attempt - 1))
        try:
            response = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code == 429 or response.status_code >= 500:
            error = f"HTTP {response.status_code}"
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code == 200 and isinstance(payload, list) and all(isinstance(r, dict) for r in payload):
            return payload
        raise RuntimeError(f"unexpected OEP answer (HTTP {response.status_code}) to {full_url}: {response.text[:300]}")
    raise RuntimeError(f"OEP request failed {RETRIES} times ({error}): {full_url}")


def fetch_rows(session: requests.Session, part: str, scenario: str, *, log: Callable[[str], None] = print,
               timeout: float = 60.0) -> list[dict[str, Any]]:
    """All AC rows of ``part`` in ``scenario``: the keys first (for the total), then pages ordered by key."""
    url = f"{API}{TABLES[part]}/rows/"
    where = [f"scn_name={scenario}", "carrier=AC"]
    key = KEYS[part]
    total = len(_get(session, url, {"where": where, "column": key}, timeout))
    rows: list[dict[str, Any]] = []
    while len(rows) < total:
        page = _get(session, url, {"where": where, "column": COLUMNS[part], "orderby": key, "limit": PAGE_SIZE,
                                   "offset": len(rows)}, timeout)
        if not page:
            break
        rows.extend(page)
        log(f"{part}: {len(rows)}/{total}")
    if len(rows) != total or len({row.get(key) for row in rows}) != total:
        raise RuntimeError(f"incomplete download of {part} from {url}: {len(rows)} rows for {total} keys")
    return rows


# --------------------------------------------------------------------------- conversion
def number(value: Any, ndigits: int | None = None) -> int | float | None:
    """A finite number from an OEP value (numeric columns arrive as strings), else ``None``.

    Rounded to ``ndigits`` if given; whole numbers become ``int`` (``"260"`` -> ``260``) to keep the GeoJSON small.
    """
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    if ndigits is not None:
        x = round(x, ndigits)
    return int(x) if x.is_integer() else x


def _int(value: Any) -> int | None:
    x = number(value)
    return x if isinstance(x, int) else None


def decode_wkb(value: str | bytes | None) -> shapely.Geometry | None:
    """Geometry of a (hex) WKB or EWKB value; ``None`` for missing, broken or empty geometries."""
    if not value:
        return None
    geom = shapely.from_wkb(value, on_invalid="ignore")
    return None if geom is None or geom.is_empty else geom


def german_buses(rows: Iterable[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """The rows of the buses in Germany (``country == 'DE'``) by bus id."""
    buses: dict[int, dict[str, Any]] = {}
    for row in rows:
        bus_id = _int(row.get("bus_id"))
        if bus_id is not None and row.get("country") == "DE":
            buses[bus_id] = row
    return buses


def german_lines(rows: Iterable[dict[str, Any]], bus_ids: Iterable[int]) -> list[dict[str, Any]]:
    """The rows of the lines whose ``bus0`` and ``bus1`` are both in ``bus_ids`` (the German buses)."""
    ids = set(bus_ids)
    return [row for row in rows if _int(row.get("bus0")) in ids and _int(row.get("bus1")) in ids]


def bus_feature(row: dict[str, Any]) -> dict[str, Any] | None:
    """GeoJSON Point feature of one bus row (``x``/``y``, else ``geom``); ``None`` without a position."""
    x, y = number(row.get("x")), number(row.get("y"))
    if x is None or y is None:
        geom = decode_wkb(row.get("geom"))
        if geom is None or geom.geom_type != "Point":
            return None
        x, y = geom.x, geom.y
    fid = _int(row.get("bus_id"))
    props: dict[str, Any] = {"fid": fid, "v_nom": number(row.get("v_nom"))}
    if row.get("type"):
        props["type"] = row["type"]
    return {"type": "Feature", "id": fid, "properties": props,
            "geometry": {"type": "Point", "coordinates": [round(x, DECIMALS), round(y, DECIMALS)]}}


def _coordinates(line: shapely.Geometry) -> list[list[float]]:
    """Rounded coordinates of one LineString without consecutive repeats (two positions at least)."""
    xy = np.round(shapely.get_coordinates(line), DECIMALS)
    xy = xy[np.r_[True, np.any(xy[1:] != xy[:-1], axis=1)]]
    return (xy if len(xy) > 1 else np.vstack([xy, xy])).tolist()   # a line shorter than the rounding


def line_geometry(row: dict[str, Any], tolerance: float = SIMPLIFY_TOLERANCE_DEG) -> dict[str, Any] | None:
    """GeoJSON (Multi)LineString of one line row; ``None`` without a usable geometry.

    Takes the OSM route ``geom`` (else the straight ``topo``), merges a MultiLineString whose parts join up
    into one LineString, simplifies it by ``tolerance`` degrees (topology preserving, so no part collapses)
    and rounds the coordinates to :data:`DECIMALS` decimals.
    """
    for column in ("geom", "topo"):
        geom = decode_wkb(row.get(column))
        if geom is not None and geom.geom_type in ("LineString", "MultiLineString"):
            break
    else:
        return None
    if geom.geom_type == "MultiLineString":
        merged = shapely.line_merge(geom)
        if merged.geom_type == "LineString":
            geom = merged
    geom = shapely.simplify(geom, tolerance, preserve_topology=True)
    parts = [_coordinates(part) for part in shapely.get_parts(geom) if not part.is_empty]
    if not parts:
        return None
    if len(parts) == 1:
        return {"type": "LineString", "coordinates": parts[0]}
    return {"type": "MultiLineString", "coordinates": parts}


def line_feature(row: dict[str, Any], tolerance: float = SIMPLIFY_TOLERANCE_DEG) -> dict[str, Any] | None:
    """GeoJSON feature of one line row (geometry from :func:`line_geometry`); ``None`` without a geometry."""
    geometry = line_geometry(row, tolerance)
    if geometry is None:
        return None
    fid = _int(row.get("line_id"))
    props = {"fid": fid, "v_nom": number(row.get("v_nom")), "s_nom": number(row.get("s_nom"), 1),
             "length_km": number(row.get("length"), 3), "num_parallel": number(row.get("num_parallel"), 3),
             "cables": number(row.get("cables")), "type": row.get("type") or None,
             "bus0": _int(row.get("bus0")), "bus1": _int(row.get("bus1"))}
    return {"type": "Feature", "id": fid, "properties": props, "geometry": geometry}


def feature_collection(features: list[dict[str, Any]]) -> bytes:
    """Compact GeoJSON FeatureCollection (strict JSON: no NaN or Infinity)."""
    return json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"),
                      allow_nan=False).encode()


def count_by_kv(features: list[dict[str, Any]]) -> dict[str, int]:
    """Number of features per nominal voltage, ascending (``{"110": n, "220": n, "380": n}``)."""
    counts = Counter(feature["properties"]["v_nom"] for feature in features)
    return {str(kv): counts[kv] for kv in sorted(counts, key=lambda kv: (kv is None, kv or 0))}


# --------------------------------------------------------------------------- fetch
def fetch(data_dir: Path, scenario: str = DEFAULT_SCENARIO, *, log: Callable[[str], None] = print,
          session: requests.Session | None = None, timeout: float = 60.0) -> dict[str, Any]:
    """Download the AC grid of ``scenario`` from the OEP and write it as GeoJSON into :func:`dataset_dir`.

    Keeps the buses with ``country == 'DE'`` and the lines between two of them. The files are replaced
    atomically, ``meta.json`` last.

    Args:
        data_dir: Cache root of the viewer.
        scenario: eGon-data scenario (``scn_name``).
        log: Receives short progress lines.
        session: HTTP session to use (a new one when ``None``).
        timeout: Seconds to wait for the OEP per request.

    Returns:
        The content of ``meta.json``.

    Raises:
        ValueError: invalid ``scenario`` name.
        RuntimeError: a request kept failing, the OEP answered something unexpected or has no buses of ``scenario``.
    """
    out = dataset_dir(data_dir, scenario)
    started = time.time()
    own_session = session is None
    if session is None:
        session = requests.Session()
    log(f"eTraGo {scenario}: AC buses and lines from {API}")
    try:
        bus_rows = fetch_rows(session, "buses", scenario, log=log, timeout=timeout)
        if not bus_rows:
            raise RuntimeError(f"the OEP has no AC buses of scenario {scenario!r} ({API}{TABLES['buses']}/rows/)")
        line_rows = fetch_rows(session, "lines", scenario, log=log, timeout=timeout)
    finally:
        if own_session:
            session.close()

    buses = german_buses(bus_rows)
    lines = german_lines(line_rows, buses)
    bus_features = [feature for feature in map(bus_feature, buses.values()) if feature is not None]
    line_features = [feature for feature in map(line_feature, lines) if feature is not None]
    dropped = {"buses_outside_de": len(bus_rows) - len(buses), "buses_without_position": len(buses) - len(bus_features),
               "lines_outside_de": len(line_rows) - len(lines),
               "lines_without_geometry": len(lines) - len(line_features)}
    log(f"kept {len(bus_features)} German buses and {len(line_features)} lines between them "
        f"({sum(not row.get('geom') for row in lines)} drawn straight without OSM route); dropped {dropped}")

    blobs = {"lines": feature_collection(line_features), "buses": feature_collection(bus_features)}
    meta = {"dataset": "etrago", "scenario": scenario,
            "source": f"Open Energy Platform, eGon-data (tables {TABLES['buses']} buses, {TABLES['lines']} lines)",
            "api": API, "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "attribution": ATTRIBUTION, "simplify_tolerance_deg": SIMPLIFY_TOLERANCE_DEG,
            "counts": {"lines": len(line_features), "buses": len(bus_features),
                       "lines_by_kv": count_by_kv(line_features), "buses_by_kv": count_by_kv(bus_features)},
            "bytes": {part: len(blob) for part, blob in blobs.items()}, "dropped": dropped}
    out.mkdir(parents=True, exist_ok=True)
    for part, blob in blobs.items():
        _write_atomic(out / f"{part}.geojson", blob)
    _write_atomic(out / "meta.json", json.dumps(meta, indent=1, ensure_ascii=False).encode("utf-8"))
    log(f"wrote {out}: lines {len(blobs['lines']) / 1e6:.1f} MB, buses {len(blobs['buses']) / 1e6:.1f} MB "
        f"in {time.time() - started:.0f} s")
    return meta
