"""The pure district planner of the MV stage: one HV/MV supply district in, one district plan out.

:func:`plan_district` composes the planning modules for one district (DESIGN §4.8): the in-memory run graph
(:mod:`~pylovo.mv_grid.graph`), the distance model and the topology (:mod:`~pylovo.mv_grid.topology`), the ring
evaluator (:mod:`~pylovo.mv_grid.rings`), the ring solver (:mod:`~pylovo.mv_grid.solvers`), the pandapower cases,
the repair loop, the HV/MV sizing, the stored net and the station voltage budgets
(:mod:`~pylovo.mv_grid.electrical`), and the metrics of the generic table format (:mod:`~pylovo.mv_grid.metrics`).

The module is pure: it imports no database, configuration or plotting module. The district's street geometries
come through the callable :attr:`DistrictInput.edge_geometry <pylovo.mv_grid.model.DistrictInput.edge_geometry>`
(a database reader in the stage's worker, a dict lookup in tests), so the planner runs end to end without a
database. The planning modules are imported when a district is planned, so this module stays importable on its
own.
"""
from __future__ import annotations

import importlib
import time
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd

from pylovo.mv_grid.model import DistrictInput, DistrictPlan, TopologyPlan

#: Planning modules composed by :func:`plan_district` (imported on use).
PLANNING_MODULES: tuple[str, ...] = ("graph", "topology", "rings", "solvers", "electrical", "metrics", "coincidence")

_INSTALLED = ("inst_res", "inst_com", "inst_pub", "inst_unk")
_UNITS = ("units_res", "units_com", "units_pub", "units_unk")


def _module(name: str) -> ModuleType:
    """``pylovo.mv_grid.<name>``, imported on first use (``sys.modules`` first, so tests can put fakes there)."""
    return importlib.import_module(f"pylovo.mv_grid.{name}")


def modelled_rows(nodes: pd.DataFrame) -> pd.Series:
    """Rows of a district that are planned: every row whose ``connection_hint`` is not ``excluded``."""
    hint = nodes["connection_hint"].astype(object) if "connection_hint" in nodes.columns else None
    if hint is None:
        return pd.Series(True, index=nodes.index)
    return hint.ne("excluded")


def district_peak_kw(inp: DistrictInput) -> float:
    """Coincident peak of all planned nodes of a district: the category law on their summed statistics plus the
    fixed loads of the MV customers (DESIGN §4.6; never the sum of feeder heads)."""
    nodes = inp.nodes[modelled_rows(inp.nodes)]
    if not len(nodes):
        return 0.0
    installed = nodes.loc[:, list(_INSTALLED)].to_numpy(dtype=float, na_value=0.0)
    units = nodes.loc[:, list(_UNITS)].to_numpy(dtype=float, na_value=0.0)
    fixed = nodes["p_fixed_kw"].to_numpy(dtype=float, na_value=0.0)
    installed = np.where(np.isfinite(installed), installed, 0.0)
    units = np.where(np.isfinite(units), units, 0.0)
    fixed = np.where(np.isfinite(fixed), fixed, 0.0)
    g = inp.lv.g()
    coincidence = _module("coincidence")
    return float(np.sum(coincidence.coincident_kw(installed.sum(axis=0), units.sum(axis=0), g)) + fixed.sum())


def merged_node_flags(*sources: dict | None) -> dict[int, list[str]]:
    """Flags per node key from several sources (topology, electrical), each flag once, in order of appearance."""
    merged: dict[int, list[str]] = {}
    for source in sources:
        for key, flags in (source or {}).items():
            found = merged.setdefault(int(key), [])
            found.extend(str(f) for f in flags if str(f) not in found)
    return {key: flags for key, flags in merged.items() if flags}


def plan_district(inp: DistrictInput) -> DistrictPlan:
    """Plan one HV/MV supply district (DESIGN §4.8).

    Steps: run graph (§3.8) → terminals and distance model (§4.2) → ring evaluator (§4.3) → solver with local
    search (§4.4) → topology with stubs, dedicated feeders, open points and geometry (§4.5, §4.7) →
    pandapower cases, criteria and up to ``repair_rounds`` repair rounds, always re-evaluated after a repair
    (§5.1–§5.4) → HV/MV sizing on the district statistics (§4.6) → stored net with the OLTC tap (§5.5) → station
    voltage budgets (§5.6) → metrics.

    The flags of the topology (``plan.topology.diagnostics['node_flags']``) and the electrical flags of
    :func:`electrical.node_flags` are merged there per node key (for ``mv_node.flags``).

    The returned :class:`~pylovo.mv_grid.model.DistrictPlan` carries in ``metrics``: the topology metrics of the
    generic format, the case extremes of :func:`electrical.case_metrics`, the solver diagnostics,
    ``length_savings_m``, ``length_final_m``, ``solver_passes``, ``solver_moves``, ``ls_time_guard_hit``,
    ``solver_runtime_s``, ``n_bridges``, ``bridged_nodes`` (node keys whose connection needed a straight bridge
    between graph islands), ``trench_km``, ``max_circuits_per_piece``, ``n_repairs``, ``repair_rounds_used``,
    ``n_violations``, ``hv_mv_loading_n0_percent``, ``hv_mv_loading_n1_percent`` and ``planner_runtime_s``.

    Args:
        inp: The district (nodes in NODE_COLUMNS form, busbar first; subgraph; parameters).

    Returns:
        The district plan (deterministic for the same input).
    """
    start = time.perf_counter()
    graph, topology, rings, solvers, electrical, metrics = (
        _module(name) for name in ("graph", "topology", "rings", "solvers", "electrical", "metrics"))
    params = inp.params
    rg = graph.build_run_graph(inp.edges, inp.nodes, long_connector_m=params.long_connector_m,
                               bridge_detour=params.bridge_detour, exit_radius_m=params.busbar_exit_radius_m,
                               osm_cost_factor=params.osm_line_cost_factor)              # §3.8
    terminals = topology.terminal_rows(inp)                                              # busbar first
    model = topology.distance_model(inp, rg, terminals)                                  # §4.2
    ev = rings.RingEvaluator.from_model(inp, model)                                      # §4.3
    result = solvers.solve(ev, model.dist_m, model.candidates, rounds=params.local_search_rounds,
                           neighbours=params.neighbours, time_guard_s=params.ls_time_guard_s)  # §4.4
    plan = topology.build_plan(inp, rg, model, ev, result.routes)                       # §4.5, §4.7

    repairs: list[dict] = []
    rounds_used = 0
    for round_ in range(params.repair_rounds + 1):                                       # §5.4
        net = electrical.build_design_net(inp, plan)                                     # re-evaluated after a repair
        cases = electrical.run_cases(net, inp, plan)
        violations = electrical.check(cases, inp, plan)
        if not violations or round_ == params.repair_rounds:
            break
        plan, actions = electrical.repair(inp, rg, model, ev, plan, violations)
        if not actions:
            break                                                                        # nothing left to repair
        rounds_used += 1
        repairs.extend({**action, "round": rounds_used} if isinstance(action, dict) else
                       {"action": str(action), "round": rounds_used} for action in actions)

    units, unit_mva, sizing = electrical.size_substation(inp, plan)                     # §4.6
    stored, tap = electrical.build_stored_net(net, units, unit_mva, inp)                # §5.5
    budgets = electrical.station_budgets(inp, plan, cases)                               # §5.6
    status = electrical.power_flow_status(cases, violations)
    plan.diagnostics["node_flags"] = merged_node_flags(plan.diagnostics.get("node_flags"),
                                                       electrical.node_flags(inp, plan, cases, budgets))

    peak_load_mw = district_peak_kw(inp) / 1000.0
    s_load_mva = peak_load_mw / inp.lv.power_factor if inp.lv.power_factor > 0 else peak_load_mw
    pieces = getattr(rg, "pieces", None)                                                 # trench: cable pieces
    on_osm = (pieces["overhead"].fillna(False).to_numpy(dtype=bool)
              if pieces is not None and "overhead" in pieces.columns else None)
    trench, max_circuits = graph.trench_km(rg, [[p for p in line.pieces if on_osm is None or p < 0 or not on_osm[p]]
                                                for line in plan.lines if line.role != "tie" and line.kind != "overhead"])
    bridged = [int(k) for k, flag in zip(inp.nodes.index, np.asarray(rg.bridged_rows, dtype=bool)) if flag]

    nodes_t, lines_t = plan_tables(inp, plan)                                            # generic metric format
    district_metrics: dict[str, Any] = {
        **metrics.topology_metrics(nodes_t, lines_t),
        **electrical.case_metrics(cases),
        **(result.diagnostics or {}),
        "length_savings_m": float(result.length_savings_m),
        "length_final_m": float(result.length_final_m),
        "solver_passes": int(result.passes),
        "solver_moves": int(result.moves),
        "ls_time_guard_hit": bool(result.time_guard_hit),
        "solver_runtime_s": float(result.runtime_s),
        "n_bridges": int(rg.n_bridges),
        "bridged_nodes": bridged,
        "trench_km": float(trench),
        "max_circuits_per_piece": int(max_circuits),
        "n_repairs": len(repairs),
        "repair_rounds_used": rounds_used,
        "n_violations": len(violations),
        "hv_mv_loading_n0_percent": (100.0 * s_load_mva / (units * unit_mva)) if units * unit_mva > 0 else None,
        "hv_mv_loading_n1_percent": (100.0 * s_load_mva / ((units - 1) * unit_mva))
        if (units - 1) * unit_mva > 0 else None,
    }
    district_metrics["planner_runtime_s"] = time.perf_counter() - start
    return DistrictPlan(
        topology=plan, hv_mv_units=int(units), hv_mv_unit_mva=float(unit_mva), hv_mv_sizing=str(sizing),
        oltc_tap=int(tap), cases=cases, violations=list(violations), repairs=repairs, budgets=budgets, net=stored,
        metrics=district_metrics, power_flow_status=str(status), peak_load_mw=float(peak_load_mw))


def plan_tables(inp: DistrictInput, plan: TopologyPlan) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The planned district in the generic MV table format of :mod:`pylovo.mv_grid.metrics`.

    Nodes: every planned node of the district (rows with ``connection_hint`` ``excluded`` or role ``excluded``
    are left out) and every joint of the plan, with ``node_id`` (the node key), ``kind``, ``x``, ``y``,
    ``area_class``, ``sn_kva`` and ``role``. Lines: every planned line with ``line_id`` (the line key),
    ``from_node``, ``to_node``, ``length_km``, ``kind``, ``cable_km``, ``overhead_km``, ``normally_open``,
    ``parallel``, ``open_at_node`` and ``role``.

    Args:
        inp: The district input.
        plan: Its planned topology.

    Returns:
        ``(nodes, lines)``.
    """
    nodes = inp.nodes
    keep = modelled_rows(nodes) & pd.Series([plan.node_role.get(int(k)) != "excluded" for k in nodes.index],
                                            index=nodes.index)
    rows = nodes[keep]
    urban = rows["urban"].astype(object).map(lambda u: bool(u) if pd.notna(u) else False) \
        if "urban" in rows.columns else pd.Series(False, index=rows.index)
    table = pd.DataFrame({
        "node_id": [int(k) for k in rows.index],
        "kind": rows["kind"].astype(str).to_numpy(),
        "x": rows["x"].to_numpy(dtype=float),
        "y": rows["y"].to_numpy(dtype=float),
        "area_class": np.where(urban.to_numpy(dtype=bool), "urban", "rural"),
        "sn_kva": pd.to_numeric(rows["sn_kva"], errors="coerce").to_numpy(dtype=float)
        if "sn_kva" in rows.columns else np.nan,
        "role": [plan.node_role.get(int(k)) for k in rows.index],
    })
    if plan.joints:
        keys = sorted(plan.joints, reverse=True)
        joints = pd.DataFrame({
            "node_id": keys, "kind": "joint", "x": [float(plan.joints[k][0]) for k in keys],
            "y": [float(plan.joints[k][1]) for k in keys], "area_class": None, "sn_kva": np.nan, "role": "joint",
        })
        table = pd.concat([table, joints], ignore_index=True)
    lines = pd.DataFrame(
        [{"line_id": int(ln.key), "from_node": int(ln.from_key), "to_node": int(ln.to_key),
          "length_km": float(ln.length_m) / 1000.0, "kind": ln.kind, "cable_km": float(ln.cable_m or 0.0) / 1000.0,
          "overhead_km": float(ln.overhead_m or 0.0) / 1000.0, "normally_open": bool(ln.normally_open),
          "parallel": int(ln.parallel or 1), "open_at_node": None if ln.open_at_key is None else int(ln.open_at_key),
          "role": ln.role}
         for ln in sorted(plan.lines, key=lambda line: line.key)],
        columns=["line_id", "from_node", "to_node", "length_km", "kind", "cable_km", "overhead_km",
                 "normally_open", "parallel", "open_at_node", "role"])
    return table, lines

