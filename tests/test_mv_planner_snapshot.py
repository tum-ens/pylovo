"""Snapshot of the pure MV district planner on three synthetic districts.

:func:`pylovo.mv_grid.planner.plan_district` is deterministic for the same input. This test plans three street-grid
districts with the real planning modules and compares the plans with ``tests/mv_planner_snapshot.json``: rings
with their members and open points, lines with type and parallel count, node roles, repairs, violations, the
power flow of every case, the station budgets, the HV/MV sizing, the stored net and the metrics (without run
times). A restructuring of the MV package must leave the file unchanged. After an intended change of the MV
results, write the snapshot again and commit it with the change::

    PYLOVO_UPDATE_MV_PLANNER_SNAPSHOT=1 uv run pytest tests/test_mv_planner_snapshot.py

The districts:

* ``ring_repairs_20kv``: 20 kV with the 95 mm² cable and a 9 MW ring customer; the repairs give the overloaded
  ring sections larger conductors and a parallel cable.
* ``stubs_dedicated_10kv``: 10 kV with the 95 mm² cable, rural stations as stubs on T-joints, a dedicated
  feeder and a station on a street island; the repairs give larger conductors for voltage, loading and the
  dedicated feeder.
* ``auto_10kv_split``: a district at 10 kV of a variant with ``MV_NOMINAL_VOLTAGE_KV`` auto and its default
  types; a repair splits a ring.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import shapely
from test_mv_planner import LV, node_table

from pylovo.mv_grid import planner
from pylovo.mv_grid.model import DistrictInput, DistrictPlan, EdgeSet
from pylovo.mv_grid.parameters import MvParameters

SNAPSHOT = Path(__file__).parent / "mv_planner_snapshot.json"
#: metrics that are run times, not results
RUN_TIMES = {"planner_runtime_s", "solver_runtime_s", "solver_construction_s", "solver_local_search_s"}
#: columns of the stored net that are compared (the geometry columns are left out)
NET_COLUMNS = {
    "bus": ["vn_kv", "node_key", "mv_node_id", "kind", "in_service"],
    "line": ["std_type", "from_bus", "to_bus", "length_km", "r_ohm_per_km", "x_ohm_per_km", "c_nf_per_km",
             "max_i_ka", "parallel", "in_service", "line_key", "role", "kind", "ring_id", "exit", "cable_km",
             "overhead_km"],
    "trafo": ["std_type", "hv_bus", "lv_bus", "sn_mva", "tap_pos", "in_service"],
    "load": ["bus", "p_mw", "q_mvar", "in_service"],
    "ext_grid": ["bus", "vm_pu", "va_degree"],
    "switch": ["bus", "element", "et", "closed"],
    "res_bus": ["vm_pu", "va_degree", "p_mw", "q_mvar"],
    "res_line": ["p_from_mw", "q_from_mvar", "loading_percent"],
    "res_trafo": ["p_hv_mw", "q_hv_mvar", "loading_percent"],
}


def street_grid_district(*, block: float, n_stations: int = 30, res_kw: float = 150.0, com_kw: float = 30.0,
                         urban: bool = True, customers: tuple[tuple[float, str], ...], island: bool,
                         vn_kv: float, params: MvParameters) -> DistrictInput:
    """A 7 x 7 street grid of ``block`` m, ``n_stations`` stations on its east-west streets, MV customers 30 m off
    the street and, with ``island``, a street island east of the grid with the first station on it."""
    n = 7
    xy = [(block * i, block * j) for i in range(n) for j in range(n)]
    vertex = {(i, j): i * n + j + 1 for i in range(n) for j in range(n)}
    src, dst = [], []
    for (i, j), v in vertex.items():
        if i + 1 < n:
            src.append(v), dst.append(vertex[(i + 1, j)])
        if j + 1 < n:
            src.append(v), dst.append(vertex[(i, j + 1)])
    if island:
        xy += [(block * (n + 4), block * 3), (block * (n + 5), block * 3)]
        src.append(len(xy) - 1), dst.append(len(xy))
    xy_arr = np.array(xy)
    length = np.hypot(*(xy_arr[np.array(src) - 1] - xy_arr[np.array(dst) - 1]).T)
    edge_id = np.arange(1, len(src) + 1)
    geoms = {int(e): shapely.LineString([xy_arr[a - 1], xy_arr[b - 1]]) for e, a, b in zip(edge_id, src, dst)}
    edges = EdgeSet(edge_id=edge_id, source=np.array(src), target=np.array(dst), length_m=length,
                    in_settlement=np.full(len(src), urban), settlement_id=np.full(len(src), 1 if urban else -1),
                    vertex_id=np.arange(1, len(xy) + 1), vertex_xy=xy_arr)
    first = xy_arr[src[0] - 1]                                       # the busbar sits on the first vertex
    rows = [{"mv_node_id": 1, "kind": "busbar", "connection_hint": "busbar", "x": float(first[0]),
             "y": float(first[1]), "attach_edge_id": int(edge_id[0]), "attach_fraction": 0.0, "sn_kva": np.nan}]
    horizontal = [int(e) for e, a, b in zip(edge_id, src, dst) if xy_arr[a - 1][1] == xy_arr[b - 1][1]
                  and not (island and e == edge_id[-1])]
    for k in range(n_stations):
        e = horizontal[(7 * k) % len(horizontal)]
        x, y = (xy_arr[src[e - 1] - 1] + xy_arr[dst[e - 1] - 1]) / 2
        rows.append({"mv_node_id": 100 + k, "x": x, "y": y, "attach_edge_id": e, "attach_fraction": 0.5,
                     "inst_res": res_kw, "units_res": 40.0, "inst_com": com_kw, "units_com": 2.0,
                     "settlement_id": 1 if urban else np.nan, "urban": urban, "grid_result_id": 1000 + k})
    if island:
        rows[1].update(attach_edge_id=int(edge_id[-1]), x=float(xy_arr[-2:, 0].mean()), y=float(xy_arr[-1, 1]))
    for k, (kw, hint) in enumerate(customers):
        e = horizontal[(11 * k + 3) % len(horizontal)]
        rows.append({"mv_node_id": 200 + k, "kind": "customer", "connection_hint": hint, "p_fixed_kw": kw,
                     "x": float(xy_arr[src[e - 1] - 1][0]), "y": float(xy_arr[src[e - 1] - 1][1]) + 30.0,
                     "attach_edge_id": e, "attach_fraction": 0.0, "attach_offset_m": 30.0, "sn_kva": np.nan})
    return DistrictInput(mv_grid_id=1, substation_id=1, vn_kv=vn_kv, nodes=node_table(rows), edges=edges,
                         params=params, lv=LV, edge_geometry=lambda ids: {int(i): geoms[int(i)] for i in ids})


def districts() -> dict[str, DistrictInput]:
    return {
        "ring_repairs_20kv": street_grid_district(
            block=400.0, customers=((9000.0, "ring"), (400.0, "ring")), island=False, vn_kv=20.0,
            params=MvParameters(vn_kv=20.0, cable_type="NA2XS2Y 1x95 RM/25 12/20 kV")),
        "stubs_dedicated_10kv": street_grid_district(
            block=1000.0, com_kw=300.0, urban=False, island=True, vn_kv=10.0,
            customers=((2000.0, "ring"), (1500.0, "ring"), (6000.0, "dedicated")),
            params=MvParameters(vn_kv=10.0, cable_type="NA2XS2Y 1x95 RM/25 6/10 kV")),
        "auto_10kv_split": street_grid_district(
            block=1200.0, com_kw=400.0, urban=False, island=True, vn_kv=10.0,
            customers=((2500.0, "ring"), (6000.0, "dedicated")), params=MvParameters()),
    }


def plain(value: Any) -> Any:
    """JSON-native values: numbers rounded to 10 significant digits, NaN and infinity as None, keys as text."""
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(f"{float(value):.10g}") if math.isfinite(value) else None
    if value is None or isinstance(value, str):
        return value
    if hasattr(value, "item"):                                         # pandas NA and numpy scalars
        try:
            return plain(value.item())
        except (AttributeError, ValueError):
            pass
    return None if value is None or str(value) in ("<NA>", "nan", "NaT") else str(value)


def record(plan: DistrictPlan) -> dict:
    """The compared content of a district plan."""
    topo = plan.topology
    diagnostics = dict(topo.diagnostics)
    node_flags = diagnostics.pop("node_flags", {})
    nodes = sorted(topo.node_role)
    net = plan.net
    return plain({
        "status": plan.power_flow_status,
        "hv_mv": {"units": plan.hv_mv_units, "unit_mva": plan.hv_mv_unit_mva, "sizing": plan.hv_mv_sizing,
                  "oltc_tap": plan.oltc_tap, "peak_load_mw": plan.peak_load_mw},
        "rings": [{"ring_id": r.ring_id, "kind": r.kind, "members": r.members, "nop_key": r.nop_key,
                   "feeder_a": r.feeder_a, "feeder_b": r.feeder_b,
                   "check": None if r.check is None else vars(r.check)} for r in topo.rings],
        "lines": [{"key": ln.key, "from": ln.from_key, "to": ln.to_key, "role": ln.role, "kind": ln.kind,
                   "feeder_id": ln.feeder_id, "ring_id": ln.ring_id, "length_m": ln.length_m, "cable_m": ln.cable_m,
                   "overhead_m": ln.overhead_m, "straight_m": ln.straight_m, "bridge_m": ln.bridge_m,
                   "std_type": ln.std_type, "parallel": ln.parallel, "normally_open": ln.normally_open,
                   "open_at_key": ln.open_at_key, "exit": ln.exit, "pieces": list(ln.pieces),
                   "geometry_m": None if ln.geometry is None else ln.geometry.length,
                   "geometry_points": None if ln.geometry is None else len(ln.geometry.coords)}
                  for ln in sorted(topo.lines, key=lambda line: line.key)],
        "nodes": {k: {"role": topo.node_role.get(k), "feeder": topo.node_feeder.get(k), "ring": topo.node_ring.get(k),
                      "position": topo.node_position.get(k), "stub_parent": topo.stub_parent.get(k),
                      "flags": node_flags.get(k)} for k in nodes},
        "joints": {k: list(xy) for k, xy in sorted(topo.joints.items())},
        "diagnostics": diagnostics,
        "repairs": plan.repairs,
        "violations": [vars(v) for v in plan.violations],
        "cases": {name: {"converged": c.converged, "slack_p_mw": c.slack_p_mw, "slack_q_mvar": c.slack_q_mvar,
                         "vm_pu": c.vm_pu.sort_index().to_dict(),
                         "loading_percent": c.loading_percent.sort_index().to_dict()}
                  for name, c in sorted(plan.cases.items())},
        "budgets": {k: vars(b) for k, b in sorted(plan.budgets.items())},
        "net": {table: getattr(net, table)[[c for c in columns if c in getattr(net, table).columns]]
                .to_dict(orient="index") for table, columns in NET_COLUMNS.items()},
        "metrics": {k: v for k, v in sorted(plan.metrics.items()) if k not in RUN_TIMES},
    })


def dump(value: Any, indent: str = "") -> str:
    """JSON with sorted keys; a dict or list of scalars (a table row, a member list) stays on one line."""
    def flat(v: Any) -> bool:
        return not isinstance(v, (dict, list)) or not v or all(not isinstance(x, (dict, list)) for x in
                                                               (v.values() if isinstance(v, dict) else v))
    if flat(value):
        return json.dumps(value, sort_keys=True)
    inner = indent + " "
    if isinstance(value, dict):
        items = [f"{inner}{json.dumps(k)}: {dump(v, inner)}" for k, v in sorted(value.items())]
        return "{\n" + ",\n".join(items) + "\n" + indent + "}"
    return "[\n" + ",\n".join(inner + dump(v, inner) for v in value) + "\n" + indent + "]"


def differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    """The paths whose values differ; numbers count as equal within 1e-6 (relative or absolute)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return [line for key in sorted(expected.keys() | actual.keys())
                for line in differences(expected.get(key), actual.get(key), f"{path}/{key}")]
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        return [line for i, (e, a) in enumerate(zip(expected, actual)) for line in differences(e, a, f"{path}/{i}")]
    numbers = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (expected, actual))
    if expected == actual or numbers and math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-6):
        return []
    return [f"{path.lstrip('/')}: {expected} -> {actual}"]


@pytest.fixture(scope="module")
def plans() -> dict[str, dict]:
    return {name: record(planner.plan_district(inp)) for name, inp in districts().items()}


def test_the_districts_cover_the_planner_paths(plans):
    """The snapshot is only worth its size while the districts reach these parts of the planner."""
    repairs = {name: {(r["action"], r.get("reason")) for r in plan["repairs"]} for name, plan in plans.items()}
    assert {("upgrade", "loading"), ("parallel", "loading")} <= repairs["ring_repairs_20kv"]
    assert {("upgrade", "voltage"), ("upgrade", "loading"), ("upgrade", "dedicated")} <= repairs["stubs_dedicated_10kv"]
    assert ("split_ring", None) in repairs["auto_10kv_split"]
    roles = {name: {node["role"] for node in plan["nodes"].values()} for name, plan in plans.items()}
    assert {"stub", "joint", "dedicated"} <= roles["stubs_dedicated_10kv"]
    assert plans["stubs_dedicated_10kv"]["metrics"]["bridged_nodes"] == [100]          # the island station
    for plan in plans.values():
        assert plan["status"] == "converged" and not plan["metrics"]["ls_time_guard_hit"]


def test_the_plans_equal_the_snapshot(plans):
    if os.getenv("PYLOVO_UPDATE_MV_PLANNER_SNAPSHOT"):
        SNAPSHOT.write_text(dump(plans) + "\n", encoding="utf-8")
        pytest.skip(f"wrote {SNAPSHOT}: check its diff and commit it with the change")
    changed = differences(json.loads(SNAPSHOT.read_text(encoding="utf-8")), plans)
    assert not changed, (
        f"The district plans differ from {SNAPSHOT.name} (district/path: snapshot -> now):\n  "
        + "\n  ".join(changed[:50])
        + (f"\n  ... and {len(changed) - 50} more" if len(changed) > 50 else "")
        + "\nIf the change is intended, write the snapshot again (see the docstring of this module) and commit it.")


def test_planning_again_gives_the_same_plans(plans):
    again = {name: record(planner.plan_district(inp)) for name, inp in districts().items()}
    assert differences(plans, again) == []
