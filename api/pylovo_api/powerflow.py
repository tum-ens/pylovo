"""On-demand power flow for one stored grid (results are returned, never written back)."""
from __future__ import annotations

import json
import math
import threading
import time
from typing import Any

from pylovo_api import queries, topology

_PF_LOCK = threading.Semaphore(2)  # pandapower is CPU bound; keep the server responsive


def _clean(value: Any, digits: int = 5) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return round(value, digits) if math.isfinite(value) else None


def run(grid_result_id: int, load_scaling: float = 1.0) -> dict[str, Any] | None:
    """Run a Newton-Raphson power flow on the stored pandapower net of a grid.

    The stored loads are pylovo's synthetic transformer-coincident operating point, so with
    ``load_scaling=1`` the result reproduces the validation power flow of the generator.

    Args:
        grid_result_id: Grid to calculate.
        load_scaling: Factor applied to all loads (what-if analysis).

    Returns:
        Bus voltages, line and transformer loadings keyed by pandapower index, extremes and a
        voltage profile along the feeders, or ``None`` if the grid does not exist.
    """
    import pandapower as pp

    stored = queries.grid_json(grid_result_id)
    if stored is None:
        return None
    grid, ident = stored
    started = time.time()
    with _PF_LOCK:
        net = pp.from_json_string(json.dumps(grid))
        if len(net.load):
            net.load["scaling"] = net.load["scaling"].fillna(1.0) * float(load_scaling)
        error = None
        try:
            pp.runpp(net, algorithm="nr", init="auto")
        except Exception as exc:  # noqa: BLE001 - reported to the user
            error = f"{type(exc).__name__}: {exc}"
    converged = bool(getattr(net, "converged", False)) and error is None
    result: dict[str, Any] = {"grid_result_id": grid_result_id, **ident, "load_scaling": load_scaling,
                              "converged": converged, "error": error, "took_s": round(time.time() - started, 2)}
    if not converged:
        return result

    buses = [{"pp_index": int(i), "name": net.bus.at[i, "name"]} for i in net.bus.index]
    lines = []
    for i in net.line.index:
        row = net.line.loc[i]
        lines.append({"pp_index": int(i), "from_bus": int(row.from_bus), "to_bus": int(row.to_bus),
                      "length_km": float(row.length_km), "feeder_section_id": row.get("feeder_section_id"),
                      "service_sizing_basis": row.get("service_sizing_basis")})
    for line in lines:  # pandas NaN -> None for the role test
        for key in ("feeder_section_id", "service_sizing_basis"):
            value = line[key]
            if isinstance(value, float) and math.isnan(value):
                line[key] = None
    topo = topology.analyse(buses, lines)

    vm = {int(i): _clean(v, 5) for i, v in net.res_bus.vm_pu.items()}
    loading = {int(i): _clean(v, 2) for i, v in net.res_line.loading_percent.items()}
    current = {int(i): _clean(v * 1000, 1) for i, v in net.res_line.i_ka.items()}
    trafo = [{"pp_index": int(i), "loading_percent": _clean(net.res_trafo.at[i, "loading_percent"], 2),
              "p_kw": _clean(net.res_trafo.at[i, "p_hv_mw"] * 1000, 2)} for i in net.res_trafo.index]
    lv_buses = [i for i in net.bus.index if float(net.bus.at[i, "vn_kv"]) < 1.0]
    vm_lv = [vm[i] for i in lv_buses if vm.get(i) is not None]
    role = {b["pp_index"]: topology.bus_role(b["name"]) for b in buses}
    profile = [{"bus": b, "distance_m": _clean(topo["distance_km"][b] * 1000, 1), "vm_pu": vm.get(b),
                "feeder": topo["bus_feeder"].get(b), "role": role.get(b)}
               for b in lv_buses if b in topo["distance_km"] and vm.get(b) is not None]
    worst_line = max(loading.items(), key=lambda kv: kv[1] if kv[1] is not None else -1, default=(None, None))
    result.update({
        "bus_vm_pu": vm,
        "line_loading_percent": loading,
        "line_current_a": current,
        "trafo": trafo,
        "min_vm_pu": min(vm_lv) if vm_lv else None,
        "max_vm_pu": max(vm_lv) if vm_lv else None,
        "max_voltage_drop_pct": _clean((1 - min(vm_lv)) * 100, 2) if vm_lv else None,
        "max_line_loading_percent": worst_line[1],
        "max_line_loading_index": worst_line[0],
        "total_load_kw": _clean(net.res_load.p_mw.sum() * 1000, 1) if len(net.res_load) else 0.0,
        "losses_kw": _clean((net.res_line.pl_mw.sum() + net.res_trafo.pl_mw.sum()) * 1000, 2),
        "overloaded_lines": sum(1 for v in loading.values() if v is not None and v > 100),
        "profile": profile,
    })
    return result
