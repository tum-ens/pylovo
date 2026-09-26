"""Database side of the grid diagnostics: compact inputs, version context and caches.

:mod:`pylovo_api.diagnostics` is pure Python and works on the compact *grid inputs* built
here (one dict per grid with buses, lines, loads, buildings, transformer and split points).
This module only runs SELECT queries; nothing is written to the database.

Caches (per process, keyed by a fingerprint so that regenerated or edited grids are never
served stale):

* power-flow results of the on-demand power flow, keyed by grid, load scaling and the MD5
  of the stored pandapower JSON;
* the version context (statistics over all grids of a version), keyed by the version's
  grid fingerprints;
* the batch findings of the Statistics panel.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from pylovo_api import db, diagnostics, queries

MAX_CONTEXT_GRIDS = 300   # version statistics are sampled from at most this many grids
MAX_BATCH_GRIDS = 500     # the Statistics panel scans at most this many grids at once
_CHUNK = 100

_lock = threading.Lock()
_pf_cache: OrderedDict[tuple, dict] = OrderedDict()
_context_cache: OrderedDict[tuple, dict] = OrderedDict()
_batch_cache: OrderedDict[tuple, dict] = OrderedDict()
_overrides: dict[str, Any] = {"mtime": None, "value": {}}


def _remember(cache: OrderedDict, key: tuple, value: Any, size: int) -> None:
    with _lock:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > size:
            cache.popitem(last=False)


def _recall(cache: OrderedDict, key: tuple) -> Any:
    with _lock:
        value = cache.get(key)
        if value is not None:
            cache.move_to_end(key)
        return value


# --------------------------------------------------------------------------- fingerprints
def fingerprints(grid_ids: list[int]) -> dict[int, str]:
    """MD5 of the stored pandapower JSON per grid (changes with every regeneration or edit)."""
    if not grid_ids:
        return {}
    rows = db.fetch_all("SELECT grid_result_id, md5(COALESCE(grid::text, '')) AS fp FROM pylovo.grid_result "
                        "WHERE grid_result_id = ANY(%s)", (list(grid_ids),))
    return {r["grid_result_id"]: r["fp"] for r in rows}


def _version_grids(version_id: str, plz: list[int] | None = None) -> list[dict]:
    """Grids of a version with a cheap change key (loads and lines), without hashing the stored nets."""
    params: dict[str, Any] = {"v": str(version_id)}
    where = "g.version_id = %(v)s"
    if plz:
        where += " AND g.plz = ANY(%(plz)s)"
        params["plz"] = list(plz)
    return db.fetch_all(
        f"""SELECT g.grid_result_id, g.plz,
                   concat_ws(':', ld.n, round(ld.p::numeric, 9), round(ld.q::numeric, 9), ln.n, round(ln.km::numeric, 6)) AS fp
            FROM pylovo.grid_result g
            LEFT JOIN (SELECT grid_result_id, count(*) AS n, sum(p_mw) AS p, sum(max_p_mw) AS q FROM pylovo.pandapower_load
                       WHERE grid_result_id IN (SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(v)s)
                       GROUP BY 1) ld ON ld.grid_result_id = g.grid_result_id
            LEFT JOIN (SELECT grid_result_id, count(*) AS n, sum(length_km * COALESCE(parallel, 1)) AS km
                       FROM pylovo.pandapower_line
                       WHERE grid_result_id IN (SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(v)s)
                       GROUP BY 1) ln ON ln.grid_result_id = g.grid_result_id
            WHERE {where} ORDER BY g.plz, g.kcid, g.bcid""", params)


# --------------------------------------------------------------------------- overrides
def threshold_overrides() -> dict[str, Any]:
    """Optional ``GRID_DIAGNOSTICS`` block of ``config/config_analysis.yaml`` (heuristics only)."""
    try:
        from pylovo_api.settings import paths

        path: Path = paths().config_dir / "config_analysis.yaml"
        mtime = (str(path), path.stat().st_mtime)
    except Exception:  # noqa: BLE001 - no project root or file: defaults apply
        return {}
    if _overrides["mtime"] != mtime:
        value: dict[str, Any] = {}
        try:
            import yaml

            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            block = raw.get("GRID_DIAGNOSTICS") or {}
            if isinstance(block, dict):
                value = {str(k).lower(): v for k, v in block.items()}
        except Exception:  # noqa: BLE001 - a broken file must not break the inspector
            value = {}
        _overrides.update(mtime=mtime, value=value)
    return dict(_overrides["value"])


# --------------------------------------------------------------------------- grid inputs
def _ext_grid_vm(raw: str | None) -> float:
    """``ext_grid.vm_pu`` from the stored pandapower JSON (DataFrame in 'split' orientation)."""
    try:
        frame = json.loads(raw) if raw else None
        column = frame["columns"].index("vm_pu")
        value = float(frame["data"][0][column])
        return value if math.isfinite(value) else 1.0
    except Exception:  # noqa: BLE001 - missing or unusual net: pylovo always uses 1.0
        return 1.0


def load_inputs(grid_ids: list[int], light: bool = False) -> dict[int, dict]:
    """Build the compact diagnostic inputs of several grids with a handful of bulk queries.

    Args:
        grid_ids: ``grid_result_id`` values (any version or PLZ).
        light: Skip buildings and split points (enough for the version statistics).

    Returns:
        ``{grid_result_id: inputs}``; see :func:`pylovo_api.diagnostics.diagnose` for the keys.
    """
    out: dict[int, dict] = {}
    ids = list(dict.fromkeys(int(i) for i in grid_ids))
    for start in range(0, len(ids), _CHUNK):
        out.update(_load_chunk(ids[start:start + _CHUNK], light))
    return out


def _load_chunk(ids: list[int], light: bool) -> dict[int, dict]:
    if not ids:
        return {}
    with db.cursor() as cur:  # one connection for all queries of the chunk
        return _load_chunk_with(cur, ids, light)


def _load_chunk_with(cur, ids: list[int], light: bool) -> dict[int, dict]:
    def fetch_all(sql: str, params: Any) -> list[dict]:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    heads = fetch_all(
        """SELECT g.grid_result_id, g.version_id, g.plz, g.ampacity_max_feeder_voltage_drop_percent AS amp_feeder,
                  g.ampacity_max_service_voltage_drop_percent AS amp_service,
                  md5(COALESCE(g.grid::text, '')) AS fp, g.grid->'_object'->'ext_grid'->>'_object' AS ext_grid,
                  pr.settlement_type
           FROM pylovo.grid_result g
           LEFT JOIN pylovo.postcode_result pr ON pr.version_id = g.version_id AND pr.postcode_result_plz = g.plz
           WHERE g.grid_result_id = ANY(%s)""", (ids,))
    if not heads:
        return {}
    records: dict[int, dict] = {}
    groups: dict[tuple, set[int]] = {}
    for h in heads:
        groups.setdefault((h["version_id"], h["plz"]), set()).add(h["grid_result_id"])
    for (version_id, plz), members in groups.items():
        for row in queries._grid_rows(version_id, [plz]):  # noqa: SLF001 - same KPI record as the inspector
            if row["grid_result_id"] in members:
                records[row["grid_result_id"]] = queries._grid_record(row)  # noqa: SLF001

    inputs: dict[int, dict] = {}
    for h in heads:
        gid = h["grid_result_id"]
        grid = dict(records.get(gid) or {"grid_result_id": gid, "version_id": h["version_id"], "plz": h["plz"]})
        grid.update(ampacity_feeder_drop_pct=queries.num(h["amp_feeder"], 2),
                    ampacity_service_drop_pct=queries.num(h["amp_service"], 2),
                    settlement_type=h["settlement_type"])
        inputs[gid] = {"grid": grid, "fingerprint": h["fp"], "vm_ext": _ext_grid_vm(h["ext_grid"]),
                       "buses": [], "lines": [], "loads": [], "trafo": [], "buildings": [], "splits": []}

    params = (list(inputs),)
    for row in fetch_all(
            """SELECT grid_result_id, pp_index, name, vn_kv,
                      round(ST_X(ST_GeomFromGeoJSON(geo::text))::numeric, 6)::float AS lon,
                      round(ST_Y(ST_GeomFromGeoJSON(geo::text))::numeric, 6)::float AS lat
               FROM pylovo.pandapower_bus WHERE grid_result_id = ANY(%s) ORDER BY grid_result_id, pp_index""", params):
        inputs[row.pop("grid_result_id")]["buses"].append(row)
    for row in fetch_all(
            f"""SELECT l.grid_result_id, l.pp_index, l.name, l.std_type, l.from_bus, l.to_bus,
                       round((l.length_km * 1000)::numeric, 1)::float AS length_m, COALESCE(l.parallel, 1) AS parallel,
                       l.max_i_ka, COALESCE(l.df, 1) AS df, l.r_ohm_per_km, l.x_ohm_per_km, l.feeder_section_id,
                       l.feeder_sizing_basis, l.ampacity_std_type, l.ampacity_parallel, l.service_sizing_basis,
                       round(l.service_selected_voltage_drop_percent::numeric, 3)::float AS service_drop_pct,
                       round(l.total_design_voltage_drop_percent::numeric, 3)::float AS design_drop_pct,
                       l.service_ampacity_voltage_drop_percent AS service_ampacity_drop_pct,
                       l.service_voltage_drop_limit_met, l.service_length_review, {queries.LINE_ROLE_SQL} AS role
                FROM pylovo.pandapower_line l
                WHERE l.grid_result_id = ANY(%s) AND COALESCE(l.in_service, true)
                ORDER BY l.grid_result_id, l.pp_index""", params):
        inputs[row.pop("grid_result_id")]["lines"].append(row)
    for row in fetch_all(
            """SELECT grid_result_id, pp_index, bus, category, p_mw * COALESCE(scaling, 1) AS p_mw,
                      COALESCE(q_mvar, 0) * COALESCE(scaling, 1) AS q_mvar, max_p_mw, load_units, service_design_p_mw
               FROM pylovo.pandapower_load WHERE grid_result_id = ANY(%s) AND COALESCE(in_service, true)
               ORDER BY grid_result_id, pp_index""", params):
        inputs[row.pop("grid_result_id")]["loads"].append(row)
    for row in fetch_all(
            """SELECT grid_result_id, pp_index, name, std_type, sn_mva, vn_hv_kv, vn_lv_kv, vk_percent, vkr_percent,
                      COALESCE(parallel, 1) AS parallel, hv_bus, lv_bus, tap_side, tap_neutral, tap_min, tap_max,
                      tap_step_percent, tap_pos
               FROM pylovo.pandapower_trafo WHERE grid_result_id = ANY(%s) ORDER BY grid_result_id, pp_index""",
            params):
        inputs[row.pop("grid_result_id")]["trafo"].append(row)
    if light:
        return inputs
    for row in fetch_all(
            """SELECT grid_result_id, objectid, type, households, peak_load_in_kw AS peak_kw, street, house_number,
                      building_use, floor_number, floor_area, height, vertice_id, residential_floor_area,
                      nonresidential_floor_area, nonresidential_use, residential_peak_load_in_kw,
                      nonresidential_peak_load_in_kw, nonresidential_mv_direct, occupants, assigned_way_id,
                      connection_point,
                      round(ST_X(ST_Transform(ST_PointOnSurface(geom), 4326))::numeric, 6)::float AS lon,
                      round(ST_Y(ST_Transform(ST_PointOnSurface(geom), 4326))::numeric, 6)::float AS lat
               FROM pylovo.buildings_result WHERE grid_result_id = ANY(%s)
               ORDER BY grid_result_id, objectid""", params):
        inputs[row.pop("grid_result_id")]["buildings"].append(row)
    for row in fetch_all(
            """SELECT grid_result_id, split_bus, outgoing_count, split_type,
                      round(ST_X(ST_Transform(geom, 4326))::numeric, 6)::float AS lon,
                      round(ST_Y(ST_Transform(geom, 4326))::numeric, 6)::float AS lat
               FROM pylovo.split_points WHERE grid_result_id = ANY(%s) ORDER BY grid_result_id, split_bus""", params):
        inputs[row.pop("grid_result_id")]["splits"].append(row)
    return inputs


def generation_parameters(version_id: str) -> dict:
    """The stored generation parameters of a version ({} for old versions without them)."""
    row = queries.version_parameters(str(version_id))
    return (row or {}).get("generation_parameters") or {}


# --------------------------------------------------------------------------- version context
def version_context(version_id: str, plz: int | None = None) -> dict:
    """Statistics over the grids of a version, cached per version fingerprint.

    Contains the feeder reach P90, the median demand density, the peer-section table, the
    building load quantiles, the median service length and the stations of the version
    (for "closer to another station"). Versions with more than :data:`MAX_CONTEXT_GRIDS`
    grids are sampled evenly (the grids of ``plz`` first).
    """
    grids = _version_grids(version_id)
    key = ("ctx", str(version_id), hashlib.md5("|".join(f"{g['grid_result_id']}:{g['fp']}" for g in grids)
                                                 .encode()).hexdigest())
    cached = _recall(_context_cache, key)
    if cached is not None:
        return cached
    gp = generation_parameters(version_id)
    ids = [g["grid_result_id"] for g in grids]
    if len(ids) > MAX_CONTEXT_GRIDS:
        own = [g["grid_result_id"] for g in grids if plz is not None and g["plz"] == plz][:MAX_CONTEXT_GRIDS // 2]
        rest = [i for i in ids if i not in set(own)]
        step = max(1, len(rest) // (MAX_CONTEXT_GRIDS - len(own)))
        ids = own + rest[::step][:MAX_CONTEXT_GRIDS - len(own)]
    inputs = load_inputs(ids, light=True)
    context = diagnostics.version_statistics(list(inputs.values()), gp)
    context["sampled"] = len(ids) < len(grids)
    context["grids"] = len(grids)
    quant = db.fetch_one(
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY peak_load_in_kw) AS median,
                  percentile_cont(0.99) WITHIN GROUP (ORDER BY peak_load_in_kw) AS p99
           FROM pylovo.buildings_result WHERE version_id = %s AND grid_result_id IS NOT NULL""", (str(version_id),))
    service = db.fetch_one(
        """SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY l.length_km * 1000) AS median
           FROM pylovo.pandapower_line l JOIN pylovo.grid_result g ON g.grid_result_id = l.grid_result_id
           WHERE g.version_id = %s AND l.service_sizing_basis IS NOT NULL""", (str(version_id),))
    context["building_peak_median_kw"] = queries.num((quant or {}).get("median"), 2)
    context["building_peak_p99_kw"] = queries.num((quant or {}).get("p99"), 2)
    context["service_length_median_m"] = queries.num((service or {}).get("median"), 1)
    context["stations"] = [
        {"grid_result_id": r["grid_result_id"], "plz": r["plz"], "kcid": r["kcid"], "bcid": r["bcid"],
         "lon": r["lon"], "lat": r["lat"], "size_label": r["size_label"], "kva": r["kva"],
         "coincident_kw": r["coincident_kw"]}
        for r in (queries._grid_record(row) for row in queries._grid_rows(str(version_id), None))  # noqa: SLF001
        if r["lon"] is not None]
    _remember(_context_cache, key, context, 8)
    return context


# --------------------------------------------------------------------------- power-flow cache
def remember_powerflow(result: dict, fingerprint: str | None = None) -> None:
    """Keep a converged on-demand power flow for the diagnostics endpoint."""
    if not result or not result.get("converged"):
        return
    gid = int(result["grid_result_id"])
    fp = fingerprint or fingerprints([gid]).get(gid)
    _remember(_pf_cache, (gid, round(float(result.get("load_scaling") or 1.0), 4), fp), result, 24)


def cached_powerflow(grid_result_id: int, load_scaling: float, fingerprint: str | None) -> dict | None:
    return _recall(_pf_cache, (int(grid_result_id), round(float(load_scaling), 4), fingerprint))


# --------------------------------------------------------------------------- entry points
def grid_diagnostics(grid_result_id: int, load_scaling: float = 1.0, pf: dict | None = None,
                     use_cache: bool = True) -> dict | None:
    """Diagnostics of one grid (design, generation check and, if available, power flow).

    Args:
        grid_result_id: Grid to diagnose.
        load_scaling: Load scaling of the power flow to use (a cached result is reused).
        pf: A power-flow result to use instead of the cache (e.g. the one just computed).
        use_cache: Look up a cached power flow when ``pf`` is not given.

    Returns:
        The diagnostics payload (see :func:`pylovo_api.diagnostics.diagnose`) or ``None`` if
        the grid does not exist.
    """
    started = time.time()
    inputs = load_inputs([grid_result_id]).get(int(grid_result_id))
    if inputs is None:
        return None
    grid = inputs["grid"]
    if pf is None and use_cache:
        pf = cached_powerflow(grid_result_id, load_scaling, inputs["fingerprint"])
    if pf is not None and not pf.get("converged"):
        pf = {"converged": False, "error": pf.get("error"), "load_scaling": pf.get("load_scaling", load_scaling)}
    gp = generation_parameters(grid["version_id"])
    context = version_context(grid["version_id"], grid.get("plz"))
    overrides = threshold_overrides()
    result = diagnostics.diagnose(inputs, gp, pf=pf, context=context, thresholds=overrides)
    if pf is not None:
        baseline = diagnostics.diagnose(inputs, gp, pf=None, context=context, thresholds=overrides)
        result["comparison"] = dict(diagnostics.compare(result, baseline),
                                    baseline="generation check ×1.0 with the linearised estimate")
    result["meta"]["took_s"] = round(time.time() - started, 3)
    result["meta"]["fingerprint"] = inputs["fingerprint"]
    return result


def after_powerflow(result: dict) -> dict:
    """Cache an on-demand power flow and return the diagnostics that use it.

    Never raises: a failure is returned as ``{"error": ...}`` so that the power-flow response
    itself is not lost.
    """
    try:
        gid = int(result["grid_result_id"])
        fp = fingerprints([gid]).get(gid)
        remember_powerflow(result, fp)
        return grid_diagnostics(gid, float(result.get("load_scaling") or 1.0), pf=result) or {"error": "grid not found"}
    except Exception as exc:  # noqa: BLE001 - reported next to the power flow
        return {"error": f"{type(exc).__name__}: {exc}"}


def version_diagnostics(version_id: str, plz: list[int] | None = None) -> dict:
    """Findings of the rules that need no power flow for all grids of a version (Statistics)."""
    started = time.time()
    grids = _version_grids(version_id, plz)
    truncated = len(grids) > MAX_BATCH_GRIDS
    grids = grids[:MAX_BATCH_GRIDS]
    key = ("batch", str(version_id), tuple(plz or ()),
           hashlib.md5("|".join(f"{g['grid_result_id']}:{g['fp']}" for g in grids).encode()).hexdigest())
    cached = _recall(_batch_cache, key)
    if cached is not None:
        return cached
    gp = generation_parameters(version_id)
    context = version_context(version_id, plz[0] if plz else None)
    overrides = threshold_overrides()
    inputs = load_inputs([g["grid_result_id"] for g in grids])
    rows = []
    for g in grids:
        inp = inputs.get(g["grid_result_id"])
        if inp is None:
            continue
        try:
            result = diagnostics.diagnose(inp, gp, pf=None, context=context, thresholds=overrides)
        except Exception as exc:  # noqa: BLE001 - one broken grid must not hide the others
            rows.append({"grid_result_id": g["grid_result_id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        rows.append(diagnostics.grid_summary(result))
    payload = {"version_id": str(version_id), "plz": plz or [], "grids": rows,
               "rules": diagnostics.rule_frequency(rows), "truncated": truncated,
               "took_s": round(time.time() - started, 2),
               "basis": "Rules that need no power flow: design data and the stored generation check."}
    _remember(_batch_cache, key, payload, 8)
    return payload
