"""Per-grid figures derived from data pylovo already stores (no new power flow is run).

When pylovo saves a grid (``GridGenerator.save_net``) it first runs a *validation power flow*
at the synthetic transformer-coincident operating point: the loads stored with the grid. It
stores the status (``grid_result.power_flow_status``), the LV voltage drops measured from the
LV busbar (``max_*_voltage_drop_pu``) and the solved pandapower net itself, including its
``res_bus``, ``res_line`` and ``res_trafo`` tables, in ``grid_result.grid``.

The status only checks the voltage band ``POWER_FLOW_VOLTAGE_LIMITS`` (``config_analysis.yaml``):
``converged`` means "the solver converged and every bus is inside the band", not "cables and
transformer are within their ratings". This module reads the stored result tables to show the
criteria separately (solver, voltage band, cable ampacity, transformer planning utilisation),
the voltage budget from nominal voltage (station + feeder + service) and per-feeder figures
for the Inspector. The on-demand power flow of :mod:`pylovo_api.powerflow` reruns the same stored
operating point, so at load scaling 1 it reproduces these numbers.
"""
from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from typing import Any

from pylovo_api import db

# Summaries of very large versions skip the stored-result extraction (about 1 ms per grid).
MAX_GRIDS_WITH_VALIDATION = 3000

_RESULT_SQL = """
    SELECT g.grid_result_id, o->'res_bus'->>'_object' AS res_bus, o->'res_line'->>'_object' AS res_line,
           o->'res_trafo'->>'_object' AS res_trafo
    FROM pylovo.grid_result g, LATERAL (SELECT g.grid->'_object' AS o OFFSET 0) x
    WHERE g.grid_result_id = ANY(%(ids)s)"""


def _num(value: Any, digits: int | None = None) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return round(value, digits) if digits is not None else value


def _frame(text: str | None) -> dict[int, dict[str, Any]]:
    """Parse a pandas ``to_json(orient='split')`` string into ``{index: {column: value}}``."""
    if not text:
        return {}
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    columns = data.get("columns") or []
    return {int(i): dict(zip(columns, row)) for i, row in zip(data.get("index") or [], data.get("data") or [])}


def _cross_section(std_type: str | None) -> float:
    match = re.search(r"_(\d+)(?:_|$)", re.sub(r"^.*?_4_", "_", str(std_type or "")))
    return float(match.group(1)) if match else float("nan")


# --------------------------------------------------------------------------- stored validation
def validation_results(grid_ids: list[int], limits: dict[str, Any] | None = None) -> dict[int, dict[str, Any]]:
    """Figures of the stored validation power flow of each grid.

    Args:
        grid_ids: ``grid_result_id`` values.
        limits: ``min_vm_pu``, ``max_vm_pu`` and ``planning_utilisation`` of the version (the
            stored generation parameters); defaults 0.9 / 1.1 / 0.8.

    Returns:
        ``{grid_result_id: {...}}`` with ``solved``, ``vm_busbar_pu``, ``min_vm_pu``, ``max_vm_pu``,
        ``trafo_loading_percent``, ``trafo_kva``, ``max_line_loading_percent``,
        ``overloaded_lines``, ``losses_kw``, the voltage ``budget`` at the weakest consumer and
        the separate ``checks``. Grids without stored results are missing from the dict.
    """
    if not grid_ids:
        return {}
    limits = limits or {}
    with db.cursor() as cur:
        cur.execute(_RESULT_SQL, {"ids": list(grid_ids)})
        results = {r["grid_result_id"]: r for r in cur.fetchall()}
        cur.execute("SELECT grid_result_id, pp_index, name, vn_kv FROM pylovo.pandapower_bus "
                    "WHERE grid_result_id = ANY(%(ids)s)", {"ids": list(grid_ids)})
        buses: dict[int, list[dict]] = defaultdict(list)
        for row in cur.fetchall():
            buses[row["grid_result_id"]].append(row)
        cur.execute("SELECT grid_result_id, pp_index, from_bus, to_bus FROM pylovo.pandapower_line "
                    "WHERE grid_result_id = ANY(%(ids)s)", {"ids": list(grid_ids)})
        lines: dict[int, list[dict]] = defaultdict(list)
        for row in cur.fetchall():
            lines[row["grid_result_id"]].append(row)
    out = {}
    for gid, row in results.items():
        out[gid] = _evaluate(_frame(row["res_bus"]), _frame(row["res_line"]), _frame(row["res_trafo"]),
                             buses.get(gid, []), lines.get(gid, []), limits)
    return out


def _evaluate(res_bus: dict, res_line: dict, res_trafo: dict, buses: list[dict], lines: list[dict],
              limits: dict[str, Any]) -> dict[str, Any]:
    min_limit = float(limits.get("min_vm_pu") or 0.9)
    max_limit = float(limits.get("max_vm_pu") or 1.1)
    planning = float(limits.get("planning_utilisation") or 0.8)
    vm = {i: _num(r.get("vm_pu")) for i, r in res_bus.items()}
    solved = bool(vm) and any(v is not None for v in vm.values())
    result: dict[str, Any] = {"solved": solved}
    if not solved:
        result["checks"] = {"solver": "fail", "voltage_band": "unknown", "cable_thermal": "unknown",
                            "trafo_planning": "unknown"}
        return result
    names = {b["pp_index"]: b["name"] or "" for b in buses}
    lv = [b["pp_index"] for b in buses if float(b["vn_kv"] or 0) < 1.0 and vm.get(b["pp_index"]) is not None]
    busbar = next((b["pp_index"] for b in buses if (b["name"] or "") == "LVbus 1"), None)
    vm_lv = [vm[i] for i in lv]
    loading = {i: _num(r.get("loading_percent")) for i, r in res_line.items()}
    loads = [v for v in loading.values() if v is not None]
    trafo_rows = list(res_trafo.values())
    trafo_loading = max((_num(t.get("loading_percent")) or 0 for t in trafo_rows), default=None)
    trafo_kva = sum(math.hypot(_num(t.get("p_hv_mw")) or 0, _num(t.get("q_hv_mvar")) or 0) for t in trafo_rows) * 1000
    losses = sum(_num(r.get("pl_mw")) or 0 for r in res_line.values()) + sum(_num(t.get("pl_mw")) or 0 for t in trafo_rows)

    # Voltage budget at the weakest consumer, split the way save_net splits the stored drops:
    # busbar -> connection node (feeder part) -> consumer (service part).
    consumers = {i for i, n in names.items() if n.startswith("Consumer Nodebus ")}
    service_from = {ln["to_bus"]: ln["from_bus"] for ln in lines if ln["to_bus"] in consumers}
    budget = None
    vb = vm.get(busbar) if busbar is not None else None
    weakest = min((c for c in service_from if vm.get(c) is not None), key=lambda c: vm[c], default=None)
    if vb is not None and weakest is not None:
        v_conn = vm.get(service_from[weakest])
        v_cons = vm[weakest]
        budget = {
            "bus": weakest,
            "trafo_pct": _num((1 - vb) * 100, 2),     # station: 1.0 p.u. down to the LV busbar (MV side, transformer)
            "feeder_pct": _num((vb - v_conn) * 100, 2) if v_conn is not None else None,
            "service_pct": _num((v_conn - v_cons) * 100, 2) if v_conn is not None else None,
            "total_pct": _num((1 - v_cons) * 100, 2),
            "lv_pct": _num((vb - v_cons) * 100, 2),
        }
    min_vm = min(vm_lv) if vm_lv else None
    max_vm = max(vm_lv) if vm_lv else None
    max_line = max(loads, default=None)
    band_ok = (min_vm is None or min_vm >= min_limit) and (max_vm is None or max_vm <= max_limit)
    result.update({
        "vm_busbar_pu": _num(vb, 4),
        "min_vm_pu": _num(min_vm, 4),
        "max_vm_pu": _num(max_vm, 4),
        "max_drop_from_nominal_pct": _num((1 - min_vm) * 100, 2) if min_vm is not None else None,
        "trafo_loading_percent": _num(trafo_loading, 1),
        "trafo_kva": _num(trafo_kva, 1),
        "max_line_loading_percent": _num(max_line, 1),
        "max_line_loading_index": max(loading, key=lambda i: loading[i] if loading[i] is not None else -1)
        if loads else None,
        "overloaded_lines": sum(1 for v in loads if v > 100),
        "losses_kw": _num(losses * 1000, 2),
        "budget": budget,
        "limits": {"min_vm_pu": min_limit, "max_vm_pu": max_limit, "planning_utilisation": planning},
        "checks": {
            "solver": "ok",
            "voltage_band": "ok" if band_ok else "fail",
            "cable_thermal": "unknown" if max_line is None else ("ok" if max_line <= 100 else "fail"),
            "trafo_planning": "unknown" if trafo_loading is None else (
                "ok" if trafo_loading <= planning * 100 else "warn" if trafo_loading <= 100 else "fail"),
        },
    })
    return result


# --------------------------------------------------------------------------- summary extras
def _extra_rows(ids: list[int]) -> dict[int, dict[str, Any]]:
    """Stored columns the base grid query does not select (design drops, structure, cabinets)."""
    rows = db.fetch_all(
        """SELECT g.grid_result_id, g.ampacity_max_feeder_voltage_drop_percent AS amp_feeder,
                  g.ampacity_max_service_voltage_drop_percent AS amp_service,
                  cp.cable_len_per_house, cp.ratio AS r_x, cp.no_households_per_branch AS hh_per_branch,
                  cp.max_no_of_households_of_a_branch AS max_hh_branch, cp.no_house_connections,
                  (SELECT count(*) FROM pylovo.split_points s WHERE s.grid_result_id = g.grid_result_id) AS cabinets,
                  (SELECT sum(l.length_km * COALESCE(l.parallel, 1)) FROM pylovo.pandapower_line l
                    WHERE l.grid_result_id = g.grid_result_id AND l.service_sizing_basis IS NULL
                      AND NOT (l.feeder_section_id IS NULL AND COALESCE(l.length_km, 0) <= 0.0011)) AS feeder_conductor_km,
                  (SELECT count(*) FROM pylovo.pandapower_line l WHERE l.grid_result_id = g.grid_result_id
                      AND COALESCE(l.parallel, 1) > 1 AND l.service_sizing_basis IS NULL
                      AND NOT (l.feeder_section_id IS NULL AND COALESCE(l.length_km, 0) <= 0.0011)) AS parallel_sections
           FROM pylovo.grid_result g
           LEFT JOIN pylovo.clustering_parameters cp ON cp.grid_result_id = g.grid_result_id
           WHERE g.grid_result_id = ANY(%(ids)s)""", {"ids": ids})
    return {r["grid_result_id"]: r for r in rows}


def extend_records(grids: list[dict], limits: dict[str, Any]) -> dict[str, Any]:
    """Add stored-validation and design figures to the grid records of a summary (in place).

    Args:
        grids: Records of :func:`pylovo_api.queries._grid_record`.
        limits: ``summary["limits"]`` of the version.

    Returns:
        Extra KPIs for ``summary["kpis"]`` (station count, criteria counts, design compliance).
    """
    ids = [g["grid_result_id"] for g in grids]
    validated = len(ids) <= MAX_GRIDS_WITH_VALIDATION
    stored = validation_results(ids, limits) if validated and ids else {}
    extra = _extra_rows(ids) if ids else {}
    for g in grids:
        e = extra.get(g["grid_result_id"], {})
        v = stored.get(g["grid_result_id"])
        g["validation"] = v
        g["min_vm_pu"] = (v or {}).get("min_vm_pu")
        g["pf_trafo_loading"] = (v or {}).get("trafo_loading_percent")
        g["pf_max_line_loading"] = (v or {}).get("max_line_loading_percent")
        g["drop_from_nominal_pct"] = (v or {}).get("max_drop_from_nominal_pct")
        g["ampacity_feeder_drop_pct"] = _num(e.get("amp_feeder"), 2)
        g["ampacity_service_drop_pct"] = _num(e.get("amp_service"), 2)
        g["cable_m_per_connection"] = _num(e.get("cable_len_per_house") and e["cable_len_per_house"] * 1000, 1)
        g["r_x"] = _num(e.get("r_x"), 2)
        g["households_per_branch"] = _num(e.get("hh_per_branch"), 1)
        g["max_households_per_branch"] = _num(e.get("max_hh_branch"), 0)
        g["cabinets"] = e.get("cabinets") or 0
        g["feeder_conductor_km"] = _num(e.get("feeder_conductor_km") or 0, 3)
        g["parallel_sections"] = e.get("parallel_sections") or 0
        g["design_ok"] = bool(g.get("feeder_limit_met")) and g.get("service_limit_met") is not False
        g["kva_reserve"] = _num((g["kva"] or 0) - v["trafo_kva"], 0) if v and v.get("trafo_kva") is not None else None
        g["kva_per_household"] = _num((g["kva"] or 0) / g["households"], 2) if g.get("households") else None
    checks = defaultdict(lambda: defaultdict(int))
    for g in grids:
        for name, state in ((g["validation"] or {}).get("checks") or {}).items():
            checks[name][state] += 1
    return {
        "stations": len(grids),
        "parallel_stations": sum(1 for g in grids if g["units"] > 1),
        "validation_available": sum(1 for g in grids if g["validation"]),
        "validation_skipped": not validated,
        "checks": {k: dict(v) for k, v in checks.items()},
        "within_band": sum(1 for g in grids if ((g["validation"] or {}).get("checks") or {}).get("voltage_band") == "ok"),
        "design_ok": sum(1 for g in grids if g["design_ok"]),
        "min_vm_pu": min((g["min_vm_pu"] for g in grids if g["min_vm_pu"] is not None), default=None),
        "max_drop_from_nominal_pct": max((g["drop_from_nominal_pct"] for g in grids
                                          if g["drop_from_nominal_pct"] is not None), default=None),
        "mean_pf_trafo_loading": _num(sum(g["pf_trafo_loading"] for g in grids if g["pf_trafo_loading"] is not None)
                                      / max(1, sum(1 for g in grids if g["pf_trafo_loading"] is not None)), 1)
        if any(g["pf_trafo_loading"] is not None for g in grids) else None,
        "direct_connections": sum(g.get("direct_connections") or 0 for g in grids),
        "feeder_conductor_km": _num(sum(g["feeder_conductor_km"] or 0 for g in grids), 2),
        "parallel_sections": sum(g["parallel_sections"] for g in grids),
        "violations": sum(1 for g in grids if g["power_flow_status"] != "converged"),
    }


# --------------------------------------------------------------------------- grid detail extras
def feeder_table(detail: dict, validation: dict | None) -> list[dict[str, Any]]:
    """Per-feeder figures for the Inspector (trunk and service km, shares, cable types, cabinets).

    Args:
        detail: Output of :func:`pylovo_api.queries.grid_detail` (lines, buses, splits, feeders).
        validation: This grid's :func:`validation_results` entry (for the first-section loading).
    """
    lines = [f["properties"] for f in detail["lines"]["features"]]
    buses = {f["properties"]["pp_index"]: f["properties"] for f in detail["buses"]["features"]}
    station = set(detail.get("station_buses") or [])
    total_hh = sum(b.get("households") or 0 for b in buses.values()) or 0
    total_kw = sum(b.get("p_kw") or 0 for b in buses.values()) or 0
    # split_points.split_bus is the ways vertex in the bus name ("Connection Nodebus <vertex>").
    vertex_bus = {}
    for b in buses.values():
        match = re.search(r"(\d+)$", b.get("name") or "")
        if match and (b.get("name") or "").startswith("Connection"):
            vertex_bus[int(match.group(1))] = b["pp_index"]
    splits_by_bus = {vertex_bus.get(f["properties"]["split_bus"]) for f in detail["splits"]["features"]}
    if detail.get("cabinets") is not None:  # K1…Kn of pylovo_api.cabinets (station splits excluded)
        splits_by_bus = {c["bus"] for c in detail["cabinets"]}
    stored_loading = {}
    if validation and validation.get("solved"):
        stored_loading = validation.get("line_loading", {})
    parent: dict[int, dict] = {}
    for ln in lines:  # radial: the end farther from the station is the child
        a, b = ln["from_bus"], ln["to_bus"]
        da, dbb = buses.get(a, {}).get("distance_m"), buses.get(b, {}).get("distance_m")
        if da is None or dbb is None:
            continue
        child = b if dbb >= da else a
        parent[child] = ln
    rows = []
    for f in detail["feeders"]:
        fid = f["feeder"]
        f_lines = [ln for ln in lines if ln.get("feeder") == fid]
        f_buses = [b for b in buses.values() if b.get("feeder") == fid]
        trunk = [ln for ln in f_lines if ln["role"] == "feeder"]
        head = next((ln for ln in f_lines if ln["from_bus"] in station or ln["to_bus"] in station), None)
        far = max(f_buses, key=lambda b: b.get("distance_m") or 0, default=None)
        path_types: list[str] = []
        node = far["pp_index"] if far else None
        seen = set()
        while node is not None and node not in station and node not in seen:
            seen.add(node)
            ln = parent.get(node)
            if not ln:
                break
            if ln["role"] == "feeder" and ln["std_type"] not in path_types:
                path_types.append(ln["std_type"])
            node = ln["from_bus"] if ln["to_bus"] == node else ln["to_bus"]
        path_types.sort(key=lambda t: -(_cross_section(t) if not math.isnan(_cross_section(t)) else 0))
        mix: dict[str, dict[str, float]] = {}
        for ln in trunk:
            entry = mix.setdefault(ln["std_type"], {"std_type": ln["std_type"], "km": 0.0, "sections": 0})
            entry["km"] += (ln["length_m"] or 0) / 1000
            entry["sections"] += 1
        cable_mix = sorted(({**m, "km": _num(m["km"], 3)} for m in mix.values()),
                           key=lambda m: -(_cross_section(m["std_type"]) if not math.isnan(_cross_section(m["std_type"])) else 0))
        hh = sum(b.get("households") or 0 for b in f_buses)
        kw = sum(b.get("p_kw") or 0 for b in f_buses)
        head_loading = stored_loading.get(head["pp_index"]) if head else None
        rows.append({
            "feeder": fid,
            "trunk_km": _num(sum((ln["length_m"] or 0) for ln in trunk) / 1000, 3),
            "service_km": _num(sum((ln["length_m"] or 0) for ln in f_lines if ln["role"] == "service") / 1000, 3),
            "households_share": _num(hh / total_hh, 3) if total_hh else None,
            "load_share": _num(kw / total_kw, 3) if total_kw else None,
            "path_types": path_types,
            "cable_mix": cable_mix,
            "cabinets": sum(1 for b in f_buses if b["pp_index"] in splits_by_bus),
            "head_line": head["pp_index"] if head else None,
            "head_std_type": head["std_type"] if head else None,
            "head_parallel": head["parallel"] if head else None,
            "head_stored_loading": head_loading,
        })
    return rows


def grid_extras(detail: dict, record: dict | None, limits: dict[str, Any]) -> dict[str, Any]:
    """Extra keys for the grid detail payload (added next to the existing ones).

    Returns:
        ``validation`` (stored validation power flow incl. line loadings), ``feeder_stats``
        (see :func:`feeder_table`) and ``direct_connections`` (house connections at the station).
    """
    gid = (record or {}).get("grid_result_id")
    validation = None
    if gid is not None:
        with db.cursor() as cur:
            cur.execute(_RESULT_SQL, {"ids": [gid]})
            row = cur.fetchone()
        if row:
            buses = [{"pp_index": f["properties"]["pp_index"], "name": f["properties"]["name"],
                      "vn_kv": f["properties"]["vn_kv"]} for f in detail["buses"]["features"]]
            lines = [{"pp_index": f["properties"]["pp_index"], "from_bus": f["properties"]["from_bus"],
                      "to_bus": f["properties"]["to_bus"]} for f in detail["lines"]["features"]]
            res_line = _frame(row["res_line"])
            validation = _evaluate(_frame(row["res_bus"]), res_line, _frame(row["res_trafo"]), buses, lines, limits)
            if validation.get("solved"):
                validation["line_loading"] = {i: _num(r.get("loading_percent"), 1) for i, r in res_line.items()}
                validation["bus_vm_pu"] = {i: _num(r.get("vm_pu"), 5) for i, r in _frame(row["res_bus"]).items()}
    direct = []
    for d in detail.get("direct_connections") or []:
        bus = next((f["properties"] for f in detail["buses"]["features"] if f["properties"]["pp_index"] == d["bus"]), {})
        line = next((f["properties"] for f in detail["lines"]["features"] if f["properties"]["pp_index"] == d["line"]), {})
        direct.append({**d, "address": bus.get("address"), "households": bus.get("households"), "p_kw": bus.get("p_kw"),
                       "length_m": line.get("length_m"), "std_type": line.get("std_type")})
    return {"validation": validation, "feeder_stats": feeder_table(detail, validation), "direct_connections": direct}


def version_limits(generation_parameters: dict | None) -> dict[str, Any]:
    """Voltage band and planning utilisation of a version (its stored generation parameters)."""
    gp = generation_parameters or {}
    pf = gp.get("power_flow_assessment") or {}
    return {"min_vm_pu": pf.get("min_vm_pu"), "max_vm_pu": pf.get("max_vm_pu"),
            "planning_utilisation": (gp.get("transformer_placement") or {}).get("transformer_planning_utilization")}

