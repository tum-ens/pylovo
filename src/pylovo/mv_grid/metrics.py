"""Topology metrics of medium-voltage (MV) grids in one generic table format.

The MV stage stores :func:`topology_metrics` of every planned grid (``mv_grid.metrics``). The same code runs on other
grids in the generic format: :mod:`pylovo.analysis.mv_validation` reads Ding0 grids and pandapower nets into it and
compares the metrics with published values. The module is pure: it imports no database, configuration or plotting
module.

**Generic format.** Two data frames:

- ``nodes``: ``node_id``, ``kind`` (``busbar``, ``station``, ``customer``, ``generator`` or ``joint``), ``x``, ``y``
  (metres in a projected CRS, TARGET_EPSG for pylovo). Optional: ``area_class`` (``urban`` or ``rural``),
  ``n_transformers`` and ``transformer_mva`` (HV/MV units of a busbar), ``sn_kva`` (station rating).
- ``lines``: ``line_id``, ``from_node``, ``to_node``, ``length_km`` (length of one line route),
  ``kind`` (``cable``, ``overhead`` or ``mixed``), ``cable_km``, ``overhead_km``, ``normally_open``, ``parallel``.
  Optional: ``open_at_node`` (the end of a normally-open line that holds the open switch) and ``role``
  (``ring``, ``stub``, ``dedicated``, ``generator`` or ``tie``; ``tie`` marks a tie to another substation).

``cable_km`` and ``overhead_km`` default to the line length by ``kind``; ``normally_open`` defaults to False and
``parallel`` to 1. Inventory totals multiply each line length by ``parallel``; geometric feeder and ring
distances use each route once. ``normalise_tables`` checks the format and fills these defaults.

**Feeders, rings and stubs** (DESIGN section 7.2). Remove the busbars and every normally-open line (NOP line): the
connected components of the rest are the feeders; the closed lines between a busbar and a feeder are its outlets. A
NOP line counts with the feeder of its closed end (the end without the open switch; ``from_node`` when
``open_at_node`` is unknown). Two feeders of the same busbar joined by one NOP line form an open ring (``ring``);
larger groups of feeders joined by NOP lines are open-meshed (``mesh``). A feeder joined to its own busbar by a NOP
line is a two-leg feeder (``two_leg``: a 2-leg customer feeder, or a ring whose open section ends at the busbar,
as in some Ding0 grids); a feeder with two or more closed outlets is a closed ring
(``closed_ring``); a feeder without any of these is ``radial``; a component without an outlet is an ``island``
(unsupplied in normal operation). NOP lines between feeders of different busbars, lines with role ``tie`` and
normally-open lines to a node outside the table (a district's ties to its neighbours) are ties; a feeder with a tie
end has ``has_tie``. The **main path** of a feeder runs from the busbar along the shortest closed path to every end of its NOP
lines (radial and closed-ring feeders also to their farthest node). Nodes off the main paths are stub nodes and
lines off the main paths are stub lines. The **ring members** of a ring are the stations, customers and generators
on the main paths of its feeders (the cap definition of the MV stage; generators on a main path are looped in);
joints and stub nodes do not count.

The metric names that :mod:`pylovo.analysis.mv_validation` compares with published values are listed there.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Hashable, Iterable, Sequence

import networkx as nx
import numpy as np
import pandas as pd

__all__ = [
    "GENERIC_NODE_COLUMNS",
    "GENERIC_LINE_COLUMNS",
    "GENERIC_NODE_KINDS",
    "GENERIC_LINE_KINDS",
    "feeder_table",
    "node_roles",
    "normalise_tables",
    "ring_table",
    "station_spacing_m",
    "topology_metrics",
]

#: Required columns of the generic node table.
GENERIC_NODE_COLUMNS: tuple[str, ...] = ("node_id", "kind", "x", "y")
#: Columns of the generic line table (``cable_km``, ``overhead_km``, ``normally_open`` and ``parallel`` have defaults).
GENERIC_LINE_COLUMNS: tuple[str, ...] = (
    "line_id", "from_node", "to_node", "length_km", "kind", "cable_km", "overhead_km", "normally_open", "parallel",
)
#: Node kinds of the generic format.
GENERIC_NODE_KINDS: tuple[str, ...] = ("busbar", "station", "customer", "generator", "joint")
#: Line kinds of the generic format.
GENERIC_LINE_KINDS: tuple[str, ...] = ("cable", "overhead", "mixed")

#: Node kinds that are supply points (SimBench and BMWi basis).
SUPPLY_POINT_KINDS: tuple[str, ...] = ("station", "customer")
#: Node kinds that hold switchgear and count as ring members on a main path.
MEMBER_KINDS: tuple[str, ...] = ("station", "customer", "generator")
#: Feeder kinds whose main-path supply points are (n-1) secure.
SECURE_FEEDER_KINDS: tuple[str, ...] = ("ring", "mesh", "two_leg", "closed_ring")


# ---------------------------------------------------------------------------------------------------------------
# Generic format
# ---------------------------------------------------------------------------------------------------------------


def normalise_tables(nodes: pd.DataFrame, lines: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Check the generic format and fill the optional columns.

    Args:
        nodes: Node table (``node_id`` and ``kind`` required; ``x`` and ``y`` default to NaN, or to the
            coordinates of a ``geometry`` column of points).
        lines: Line table (``from_node``, ``to_node`` and ``length_km`` required; ``line_id`` defaults to the
            index).

    Returns:
        Copies of both tables with every column of ``GENERIC_NODE_COLUMNS`` and ``GENERIC_LINE_COLUMNS``, plus
        ``open_at_node`` (None where unknown).

    Raises:
        ValueError: A required column is missing, a kind is unknown, a node id is duplicated, or a mixed line has
            no ``cable_km`` and ``overhead_km``.
    """
    for column in ("node_id", "kind"):
        if column not in nodes.columns:
            raise ValueError(f"nodes table needs a {column!r} column")
    nodes = pd.DataFrame(nodes).copy()
    if "x" not in nodes.columns or "y" not in nodes.columns:
        xy = _node_xy(nodes)
        nodes["x"], nodes["y"] = xy[:, 0], xy[:, 1]
    bad = sorted(set(nodes["kind"]) - set(GENERIC_NODE_KINDS), key=str)
    if bad:
        raise ValueError(f"unknown node kinds {bad}; expected {GENERIC_NODE_KINDS}")
    if nodes["node_id"].duplicated().any():
        dup = nodes.loc[nodes["node_id"].duplicated(), "node_id"].tolist()[:5]
        raise ValueError(f"duplicate node ids, e.g. {dup}")

    for column in ("from_node", "to_node", "length_km"):
        if column not in lines.columns:
            raise ValueError(f"lines table needs a {column!r} column")
    lines = pd.DataFrame(lines).copy()
    if "line_id" not in lines.columns:
        lines["line_id"] = lines.index
    lines["length_km"] = lines["length_km"].astype(float)
    if "kind" not in lines.columns:
        lines["kind"] = "cable"
    bad = sorted(set(lines["kind"]) - set(GENERIC_LINE_KINDS), key=str)
    if bad:
        raise ValueError(f"unknown line kinds {bad}; expected {GENERIC_LINE_KINDS}")
    has_split = "cable_km" in lines.columns and "overhead_km" in lines.columns
    if not has_split:
        if (lines["kind"] == "mixed").any():
            raise ValueError("mixed lines need cable_km and overhead_km")
        lines["cable_km"] = np.where(lines["kind"] == "cable", lines["length_km"], 0.0)
        lines["overhead_km"] = np.where(lines["kind"] == "overhead", lines["length_km"], 0.0)
    lines["cable_km"] = lines["cable_km"].astype(float)
    lines["overhead_km"] = lines["overhead_km"].astype(float)
    if "normally_open" not in lines.columns:
        lines["normally_open"] = False
    lines["normally_open"] = lines["normally_open"].fillna(False).astype(bool)
    if "parallel" not in lines.columns:
        lines["parallel"] = 1
    parallel = pd.to_numeric(lines["parallel"].fillna(1), errors="coerce")
    if (~np.isfinite(parallel) | (parallel < 1) | (parallel % 1 != 0)).any():
        raise ValueError("parallel must be a positive integer")
    lines["parallel"] = parallel.astype(int)
    if "open_at_node" not in lines.columns:
        lines["open_at_node"] = None
    lines["open_at_node"] = lines["open_at_node"].astype(object).where(lines["open_at_node"].notna(), None)
    return nodes.reset_index(drop=True), lines.reset_index(drop=True)


# ---------------------------------------------------------------------------------------------------------------
# Topology analysis
# ---------------------------------------------------------------------------------------------------------------


def _key(value: Any) -> tuple:
    """Sort key for ids that may be numbers or strings."""
    if isinstance(value, (int, np.integer, float, np.floating)) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0.0, str(value))


@dataclass
class _Analysis:
    nodes: pd.DataFrame          # normalised nodes
    lines: pd.DataFrame          # normalised internal lines without self loops, with columns feeder, nop_class, on_main
    feeders: pd.DataFrame
    rings: pd.DataFrame
    roles: pd.DataFrame
    diagnostics: dict


def _majority(values: Iterable[Any]) -> str | None:
    counts = Counter(v for v in values if isinstance(v, str))
    if not counts:
        return None
    best = max(counts.values())
    return sorted(k for k, c in counts.items() if c == best)[0]


def _analyse(nodes: pd.DataFrame, lines: pd.DataFrame) -> _Analysis:
    """Detect feeders, rings, main paths and stubs (see the module docstring)."""
    nodes, lines = normalise_tables(nodes, lines)
    kind = dict(zip(nodes["node_id"], nodes["kind"]))
    area = dict(zip(nodes["node_id"], nodes["area_class"])) if "area_class" in nodes.columns else {}
    known = set(kind)
    internal = lines["from_node"].isin(known) & lines["to_node"].isin(known)
    self_loop = internal & (lines["from_node"] == lines["to_node"])
    external = lines[~internal]
    lines = lines[internal & ~self_loop].copy()
    busbars = sorted((n for n, k in kind.items() if k == "busbar"), key=_key)
    is_busbar = set(busbars)
    roles = lines["role"] if "role" in lines.columns else pd.Series(None, index=lines.index, dtype=object)

    # feeders: components of the closed lines without the busbars
    graph = nx.MultiGraph()
    graph.add_nodes_from(n for n in sorted(known, key=_key) if n not in is_busbar)
    outlet_rows: list[tuple[int, Hashable, Hashable]] = []          # (line index, busbar, feeder node)
    busbar_links: list[int] = []
    closed = lines[~lines["normally_open"]]
    for idx, u, v, length in zip(closed.index, closed["from_node"], closed["to_node"], closed["length_km"]):
        if u in is_busbar and v in is_busbar:
            busbar_links.append(idx)
        elif u in is_busbar or v in is_busbar:
            outlet_rows.append((idx, u if u in is_busbar else v, v if u in is_busbar else u))
        else:
            graph.add_edge(u, v, key=idx, length=float(length))
    components = [sorted(c, key=_key) for c in nx.connected_components(graph)]
    comp_of_node = {n: i for i, comp in enumerate(components) for n in comp}
    comp_busbars: dict[int, set] = defaultdict(set)
    comp_outlets: dict[int, list] = defaultdict(list)
    for idx, bb, node in outlet_rows:
        comp_busbars[comp_of_node[node]].add(bb)
        comp_outlets[comp_of_node[node]].append(idx)
    comp_busbar = {i: (sorted(comp_busbars[i], key=_key)[0] if comp_busbars[i] else None)
                   for i in range(len(components))}
    order = sorted(range(len(components)),
                   key=lambda i: (comp_busbar[i] is None, _key(comp_busbar[i]) if comp_busbar[i] is not None else (),
                                  _key(components[i][0])))
    feeder_of_comp = {c: f for f, c in enumerate(order)}
    feeder_of_node = {n: feeder_of_comp[c] for n, c in comp_of_node.items()}
    n_feeders = len(order)
    feeder_nodes = [components[c] for c in order]
    feeder_busbar = [comp_busbar[c] for c in order]
    feeder_busbars = [sorted(comp_busbars[c], key=_key) for c in order]
    feeder_outlets = [sorted(comp_outlets[c]) for c in order]

    # NOP lines
    lines["feeder"] = pd.Series([None] * len(lines), index=lines.index, dtype=object)
    lines["nop_class"] = pd.Series([None] * len(lines), index=lines.index, dtype=object)
    for idx, u, v in zip(closed.index, closed["from_node"], closed["to_node"]):
        if idx in busbar_links:
            continue
        node = v if u in is_busbar else u
        lines.at[idx, "feeder"] = feeder_of_node[node]
    ring_edges: Counter = Counter()
    feeder_targets: dict[int, set] = defaultdict(set)
    feeder_flags: dict[int, set] = defaultdict(set)
    nops = lines[lines["normally_open"]]
    for idx, u, v, open_at in zip(nops.index, nops["from_node"], nops["to_node"], nops["open_at_node"]):
        closed_end = v if (open_at is not None and open_at == u) else u
        side_u = ("b", u) if u in is_busbar else ("f", feeder_of_node[u])
        side_v = ("b", v) if v in is_busbar else ("f", feeder_of_node[v])
        assigned: int | None = None
        if roles.get(idx) == "tie":
            cls = "tie"
        elif side_u[0] == "f" and side_v[0] == "f":
            fu, fv = side_u[1], side_v[1]
            if fu == fv:
                cls, assigned = "loop", fu
            elif feeder_busbar[fu] == feeder_busbar[fv] and feeder_busbar[fu] is not None:
                cls, assigned = "ring", feeder_of_node[closed_end]
                ring_edges[(min(fu, fv), max(fu, fv))] += 1
            elif feeder_busbar[fu] is None or feeder_busbar[fv] is None:
                cls, assigned = "island_link", feeder_of_node[closed_end]
            else:
                cls = "tie"
        elif side_u[0] == "f" or side_v[0] == "f":
            f = side_u[1] if side_u[0] == "f" else side_v[1]
            bb = side_v[1] if side_u[0] == "f" else side_u[1]
            cls = "two_leg" if feeder_busbar[f] in (bb, None) else "tie"
            assigned = f if cls == "two_leg" else None
        else:
            cls = "tie"
        lines.at[idx, "nop_class"] = cls
        lines.at[idx, "feeder"] = assigned
        for end, side in ((u, side_u), (v, side_v)):
            if side[0] == "f":
                feeder_targets[side[1]].add(end)
                if cls == "tie":
                    feeder_flags[side[1]].add("tie")
                elif cls == "loop":
                    feeder_flags[side[1]].add("loop")
                elif cls == "two_leg":
                    feeder_flags[side[1]].add("two_leg")

    # a normally-open line to a node outside the table (e.g. a tie of one district to its neighbour) is a tie of
    # the feeder of its known end
    for u, v, is_open in zip(external["from_node"], external["to_node"], external["normally_open"]):
        end = u if u in known else (v if v in known else None)
        if bool(is_open) and end is not None and end not in is_busbar:
            feeder_targets[feeder_of_node[end]].add(end)
            feeder_flags[feeder_of_node[end]].add("tie")

    # ring groups: feeders of one busbar joined by NOP lines
    ring_graph = nx.Graph()
    ring_graph.add_nodes_from(range(n_feeders))
    ring_graph.add_edges_from(ring_edges)
    groups = sorted((sorted(c) for c in nx.connected_components(ring_graph) if len(c) >= 2), key=lambda g: g[0])
    group_of = {f: g for g, members in enumerate(groups) for f in members}
    group_kind = []
    for members in groups:
        n_edges = sum(c for (a, b), c in ring_edges.items() if a in members and b in members)
        group_kind.append("ring" if len(members) == 2 and n_edges == 1 else "mesh")

    feeder_kind = []
    for f in range(n_feeders):
        if feeder_busbar[f] is None:
            feeder_kind.append("island")
        elif f in group_of:
            feeder_kind.append(group_kind[group_of[f]])
        elif "two_leg" in feeder_flags[f]:
            feeder_kind.append("two_leg")
        elif len(feeder_outlets[f]) >= 2:
            feeder_kind.append("closed_ring")
        else:
            feeder_kind.append("radial")

    # main paths
    length = lines["length_km"].to_dict()
    main_nodes: list[set] = [set() for _ in range(n_feeders)]
    main_lines: list[set] = [set() for _ in range(n_feeders)]
    for f in range(n_feeders):
        if feeder_busbar[f] is None:
            continue
        sub = nx.MultiGraph()
        sub.add_nodes_from(feeder_nodes[f])
        sub.add_nodes_from(feeder_busbars[f])
        for idx in feeder_outlets[f]:
            sub.add_edge(lines.at[idx, "from_node"], lines.at[idx, "to_node"], key=idx, length=length[idx])
        for u, v, idx in graph.subgraph(feeder_nodes[f]).edges(keys=True):
            sub.add_edge(u, v, key=idx, length=length[idx])
        dist, paths = nx.multi_source_dijkstra(sub, set(feeder_busbars[f]), weight="length")
        targets = set(feeder_targets[f])
        if feeder_kind[f] in ("radial", "closed_ring") or not targets:
            reachable = [n for n in feeder_nodes[f] if n in dist]
            if reachable:                                   # farthest node, ties: lowest id
                farthest = max(dist[n] for n in reachable)
                targets.add(min((n for n in reachable if dist[n] == farthest), key=_key))
        if feeder_kind[f] == "closed_ring":
            for idx in feeder_outlets[f]:
                u, v = lines.at[idx, "from_node"], lines.at[idx, "to_node"]
                targets.add(v if u in is_busbar else u)
        for target in sorted(targets, key=_key):
            path = paths.get(target)
            if not path:
                continue
            main_nodes[f].update(n for n in path if n not in is_busbar)
            for a, b in zip(path[:-1], path[1:]):
                edges = sub.get_edge_data(a, b)
                main_lines[f].add(min(edges, key=lambda k: (edges[k]["length"], k)))
    lines["on_main"] = False
    for f in range(n_feeders):
        if main_lines[f]:
            lines.loc[sorted(main_lines[f]), "on_main"] = True

    # feeder rows
    feeder_rows = []
    by_feeder = {int(f): df for f, df in lines[lines["feeder"].notna()].groupby(
        lines.loc[lines["feeder"].notna(), "feeder"].astype(int))}
    no_lines = lines.iloc[0:0]
    for f in range(n_feeders):
        comp = feeder_nodes[f]
        kinds = Counter(kind[n] for n in comp)
        stub_nodes = [n for n in comp if n not in main_nodes[f] and kind[n] in MEMBER_KINDS]
        members = [n for n in main_nodes[f] if kind[n] in MEMBER_KINDS]
        own = by_feeder.get(f, no_lines)
        own_closed = own[~own["normally_open"]]
        own_nop = own[own["normally_open"]]
        main_km = float(own_closed.loc[own_closed["on_main"], "length_km"].sum())
        nop_km = float(own_nop.loc[own_nop["nop_class"].isin(["ring", "loop", "island_link"]), "length_km"].sum())
        stub_km = float(own_closed.loc[~own_closed["on_main"], "length_km"].sum())
        busbar_nops = own_nop[own_nop["nop_class"] == "two_leg"]
        feeder_rows.append({
            "feeder": f,
            "busbar": feeder_busbar[f],
            "kind": feeder_kind[f],
            "ring": None,                                   # filled below
            "n_outlets": len(feeder_outlets[f]),
            "n_bays": len(feeder_outlets[f]) + len(busbar_nops),
            "n_nodes": len(comp),
            "n_stations": kinds.get("station", 0),
            "n_customers": kinds.get("customer", 0),
            "n_generators": kinds.get("generator", 0),
            "n_joints": kinds.get("joint", 0),
            "feeder_supply_points": kinds.get("station", 0) + kinds.get("customer", 0),
            "n_members": len(members),
            "n_stub_nodes": len(stub_nodes),
            "n_stub_supply_points": sum(kind[n] in SUPPLY_POINT_KINDS for n in stub_nodes),
            "n_main_supply_points": sum(kind[n] in SUPPLY_POINT_KINDS for n in members),
            "feeder_km": main_km + nop_km,
            "stub_km": stub_km,
            "feeder_km_incl_stubs": main_km + nop_km + stub_km,
            "length_km": float(own["length_km"].sum()),
            "cable_km": float((own["cable_km"] * own["parallel"]).sum()),
            "overhead_km": float((own["overhead_km"] * own["parallel"]).sum()),
            "two_leg_km": float(busbar_nops["length_km"].sum()),
            "n_nops": int(len(own_nop)),
            "has_tie": "tie" in feeder_flags[f],
            "has_loop": "loop" in feeder_flags[f],
            "multi_busbar": len(feeder_busbars[f]) > 1,
            "area_class": _majority(area.get(n) for n in comp if kind[n] in SUPPLY_POINT_KINDS) if area else None,
        })
    feeders = pd.DataFrame(feeder_rows, columns=_FEEDER_COLUMNS)
    feeders["busbar"] = pd.Series(feeder_busbar, index=feeders.index, dtype=object)   # keep the id type

    # ring rows: one per ring group, one per other feeder
    ring_rows = []
    units: list[list[int]] = []
    seen: set[int] = set()
    for f in range(n_feeders):
        if f in seen:
            continue
        unit = groups[group_of[f]] if f in group_of else [f]
        seen.update(unit)
        units.append(unit)
    for r, unit in enumerate(units):
        part = feeders.loc[unit]
        feeders.loc[unit, "ring"] = r
        kind_r = group_kind[group_of[unit[0]]] if unit[0] in group_of else feeder_kind[unit[0]]
        n_members = int(part["n_members"].sum())
        nodes_r = [n for f in unit for n in feeder_nodes[f]]
        ring_rows.append({
            "ring": r,
            "busbar": feeder_busbar[unit[0]],
            "kind": kind_r,
            "feeders": tuple(unit),
            "n_feeders": len(unit),
            "ring_members": n_members,
            "ring_members_incl_stubs": n_members + int(part["n_stub_nodes"].sum()),
            "n_stations": int(part["n_stations"].sum()),
            "n_customers": int(part["n_customers"].sum()),
            "n_generators": int(part["n_generators"].sum()),
            "n_stub_nodes": int(part["n_stub_nodes"].sum()),
            "supply_points": int(part["feeder_supply_points"].sum()),
            "ring_km": float(part["feeder_km"].sum() + part["two_leg_km"].sum()),
            "ring_km_incl_stubs": float(part["feeder_km_incl_stubs"].sum() + part["two_leg_km"].sum()),
            "stub_km": float(part["stub_km"].sum()),
            "cable_km": float(part["cable_km"].sum()),
            "overhead_km": float(part["overhead_km"].sum()),
            "n_nops": int(part["n_nops"].sum()),
            "has_tie": bool(part["has_tie"].any()),
            "area_class": _majority(area.get(n) for n in nodes_r if kind[n] in SUPPLY_POINT_KINDS) if area else None,
        })
    rings = pd.DataFrame(ring_rows, columns=_RING_COLUMNS)
    rings["busbar"] = pd.Series([feeder_busbar[u[0]] for u in units], index=rings.index, dtype=object)
    feeders["ring"] = feeders["ring"].astype("Int64")

    # node roles
    role_rows = []
    for n in nodes["node_id"]:
        if n in is_busbar:
            role_rows.append((n, kind[n], None, None, "busbar"))
            continue
        f = feeder_of_node[n]
        if feeder_busbar[f] is None:
            position = "unsupplied"
        else:
            position = "main" if n in main_nodes[f] else "stub"
        role_rows.append((n, kind[n], f, int(feeders.at[f, "ring"]), position))
    roles_df = pd.DataFrame(role_rows, columns=["node_id", "kind", "feeder", "ring", "position"])
    roles_df["feeder"] = roles_df["feeder"].astype("Int64")
    roles_df["ring"] = roles_df["ring"].astype("Int64")

    tie_lines = lines[lines["nop_class"] == "tie"]
    external_ties = external[external["normally_open"]] if len(external) else external
    diagnostics = {
        "n_self_loops": int(self_loop.sum()),
        "n_external_lines": int(len(external)),
        "n_busbar_links": len(busbar_links),
        "n_ties": int(len(tie_lines) + len(external_ties)),
        "tie_km": float((tie_lines["length_km"] * tie_lines["parallel"]).sum()
                        + (external_ties["length_km"] * external_ties["parallel"]).sum()),
    }
    return _Analysis(nodes, lines, feeders, rings, roles_df, diagnostics)


_FEEDER_COLUMNS = [
    "feeder", "busbar", "kind", "ring", "n_outlets", "n_bays", "n_nodes", "n_stations", "n_customers",
    "n_generators", "n_joints", "feeder_supply_points", "n_members", "n_stub_nodes", "n_stub_supply_points",
    "n_main_supply_points", "feeder_km", "stub_km", "feeder_km_incl_stubs", "length_km", "cable_km", "overhead_km",
    "two_leg_km", "n_nops", "has_tie", "has_loop", "multi_busbar", "area_class",
]
_RING_COLUMNS = [
    "ring", "busbar", "kind", "feeders", "n_feeders", "ring_members", "ring_members_incl_stubs", "n_stations",
    "n_customers", "n_generators", "n_stub_nodes", "supply_points", "ring_km", "ring_km_incl_stubs", "stub_km",
    "cable_km", "overhead_km", "n_nops", "has_tie", "area_class",
]


def feeder_table(nodes: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """One row per feeder (component of the closed lines without the busbars), with its ring and stubs.

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        Columns ``feeder``, ``busbar``, ``kind`` (``ring``, ``mesh``, ``two_leg``, ``closed_ring``, ``radial`` or
        ``island``), ``ring`` (row of ``ring_table``), ``n_outlets`` (closed busbar lines), ``n_bays`` (busbar
        lines incl. the open leg of a two-leg feeder), node counts by kind, ``feeder_supply_points`` (stations and
        customers incl. stubs), ``n_members`` (stations, customers and generators on the main path),
        ``n_stub_nodes``, ``n_stub_supply_points``, ``n_main_supply_points``, ``feeder_km`` (busbar to the NOP
        along the main path, incl. the NOP line of its closed end), ``stub_km``, ``feeder_km_incl_stubs``,
        ``length_km``, ``cable_km`` and ``overhead_km`` (every line counted with the feeder), ``two_leg_km``
        (open leg of a two-leg feeder), ``n_nops``, ``has_tie``, ``has_loop``, ``multi_busbar`` and
        ``area_class`` (majority of its supply points, None without an ``area_class`` node column).
    """
    return _analyse(nodes, lines).feeders


def ring_table(nodes: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """One row per ring (two feeders joined by a NOP line), open mesh, or other feeder (like ``mv_ring``).

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        Columns ``ring``, ``busbar``, ``kind`` (``ring``, ``mesh``, ``two_leg``, ``closed_ring``, ``radial`` or
        ``island``), ``feeders``, ``n_feeders``, ``ring_members`` (stations, customers and generators on the main
        paths: the cap definition), ``ring_members_incl_stubs``, node counts, ``supply_points``, ``ring_km``
        (closed loop incl. both busbar sections, without stubs; a two-leg feeder counts both legs),
        ``ring_km_incl_stubs``, ``stub_km``, ``cable_km``, ``overhead_km``, ``n_nops``, ``has_tie`` and
        ``area_class``.
    """
    return _analyse(nodes, lines).rings


def node_roles(nodes: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Feeder, ring and position of every node.

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        Columns ``node_id``, ``kind``, ``feeder``, ``ring`` (row of ``ring_table``) and ``position``
        (``busbar``, ``main``, ``stub`` or ``unsupplied``).
    """
    return _analyse(nodes, lines).roles


def _stats(prefix: str, values: Sequence[float] | pd.Series) -> dict:
    arr = np.asarray(pd.Series(values, dtype=float).dropna(), dtype=float)
    if arr.size == 0:
        return {f"{prefix}_mean": None, f"{prefix}_median": None, f"{prefix}_min": None, f"{prefix}_max": None}
    return {f"{prefix}_mean": float(arr.mean()), f"{prefix}_median": float(np.median(arr)),
            f"{prefix}_min": float(arr.min()), f"{prefix}_max": float(arr.max())}


def _ratio(num: float, den: float) -> float | None:
    return float(num) / float(den) if den else None


def _clean(value: Any) -> Any:
    """JSON-safe Python scalars (NaN and infinity become None)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def _node_xy(nodes: pd.DataFrame) -> np.ndarray:
    """(n, 2) coordinates from ``x``, ``y`` or a point ``geometry`` column (NaN where unknown)."""
    if "x" in nodes.columns and "y" in nodes.columns:
        return nodes[["x", "y"]].to_numpy(dtype=float)
    if "geometry" in nodes.columns:
        return np.array([(g.x, g.y) if g is not None and not g.is_empty else (np.nan, np.nan)
                         for g in nodes["geometry"]], dtype=float).reshape(-1, 2)
    return np.full((len(nodes), 2), np.nan)


def station_spacing_m(nodes: pd.DataFrame) -> float | None:
    """Median nearest-neighbour distance between stations (m), None with fewer than two located stations.

    Args:
        nodes: Generic node table with ``x`` and ``y`` in metres.

    Returns:
        The median distance from each station to its nearest other station, or None.
    """
    from scipy.spatial import cKDTree

    xy = _node_xy(nodes[nodes["kind"] == "station"])
    xy = xy[np.isfinite(xy).all(axis=1)]
    if len(xy) < 2:
        return None
    dist, _ = cKDTree(xy).query(xy, k=2)
    return float(np.median(dist[:, 1]))


def topology_metrics(nodes: pd.DataFrame, lines: pd.DataFrame) -> dict:
    """Structural metrics of one MV grid (or of several grids together) in the generic format.

    Per-substation values divide by the number of busbars. Ring statistics use rings and open meshes (not
    ``two_leg`` feeders), feeder statistics the supplied feeders with at least one supply point. Ties (NOP lines
    between feeders of different busbars, lines with role ``tie`` and lines to nodes outside the table) are
    excluded from ``circuit_km`` and reported as ``n_ties`` and ``tie_km``. Circuit and material lengths
    count installed parallel circuits; ``route_km`` sums each included line once, including open lines.

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        A flat, JSON-safe dict (NaN becomes None): counts by node kind, ``n_hv_mv_transformers`` (with an
        ``n_transformers`` node column), line and NOP counts, feeder and ring counts by kind,
        ``n_rings_with_tie`` and ``tie_ring_share`` (rings and open meshes with a tie end), ``n_feeder_bays``,
        ``feeders_per_substation``, ``route_km``, ``circuit_km``, ``cable_km``, ``overhead_km``, ``cable_share``,
        ``circuit_km_per_substation``, ``supply_points_per_substation``, ``km_per_supply_point``,
        ``km_per_station``, statistics (``_mean``, ``_median``, ``_min``, ``_max``) of ``ring_members``,
        ``ring_members_incl_stubs``, ``ring_km``, ``feeder_km``, ``feeder_km_incl_stubs`` and
        ``feeder_supply_points``, ``mean_section_km`` (a ring of k members has k + 1 sections), stub and
        (n-1) shares of the supply points, ``station_spacing_m``, ``customer_share``, ``area_class`` and
        diagnostics.
    """
    a = _analyse(nodes, lines)
    nodes_n, internal = a.nodes, a.lines
    kinds = Counter(nodes_n["kind"])
    n_busbars = kinds.get("busbar", 0)
    n_supply = kinds.get("station", 0) + kinds.get("customer", 0)
    circuits = internal[internal["nop_class"] != "tie"]
    route_km = float(circuits["length_km"].sum())
    circuit_km = float((circuits["length_km"] * circuits["parallel"]).sum())
    cable_km = float((circuits["cable_km"] * circuits["parallel"]).sum())
    overhead_km = float((circuits["overhead_km"] * circuits["parallel"]).sum())
    feeders, rings = a.feeders, a.rings
    supplied = feeders[feeders["busbar"].notna()]
    with_supply = supplied[supplied["feeder_supply_points"] > 0]
    true_rings = rings[rings["kind"].isin(["ring", "mesh"])]
    secure = supplied[supplied["kind"].isin(SECURE_FEEDER_KINDS)]
    n_secure = int(secure["n_main_supply_points"].sum())
    n_unsupplied = int(feeders.loc[feeders["busbar"].isna(), "feeder_supply_points"].sum())
    n_stub_side = n_supply - n_secure - n_unsupplied
    n_nops = int((internal["normally_open"] & (internal["nop_class"] != "tie")).sum())
    plain_rings = rings[rings["kind"] == "ring"]
    sections = float((plain_rings["ring_members"] + 1).sum())
    out: dict[str, Any] = {
        "n_busbars": n_busbars,
        "n_stations": kinds.get("station", 0),
        "n_customers": kinds.get("customer", 0),
        "n_generators": kinds.get("generator", 0),
        "n_joints": kinds.get("joint", 0),
        "n_supply_points": n_supply,
        "n_hv_mv_transformers": (int(pd.to_numeric(nodes_n.loc[nodes_n["kind"] == "busbar", "n_transformers"],
                                                   errors="coerce").fillna(0).sum())
                                 if "n_transformers" in nodes_n.columns else None),
        "n_lines": int(len(circuits)),
        "n_nops": n_nops,
        "n_feeders": int(len(supplied)),
        "n_feeder_bays": int(supplied["n_bays"].sum()),
        "feeders_per_substation": _ratio(supplied["n_bays"].sum(), n_busbars),
        "n_rings": int((rings["kind"] == "ring").sum()),
        "n_meshes": int((rings["kind"] == "mesh").sum()),
        "n_rings_with_tie": int(true_rings["has_tie"].astype(bool).sum()),
        "tie_ring_share": _ratio(true_rings["has_tie"].astype(bool).sum(), len(true_rings)),
        "n_two_leg": int((rings["kind"] == "two_leg").sum()),
        "n_closed_rings": int((rings["kind"] == "closed_ring").sum()),
        "n_radial": int((rings["kind"] == "radial").sum()),
        "n_islands": int((rings["kind"] == "island").sum()),
        "route_km": route_km,
        "circuit_km": circuit_km,
        "cable_km": cable_km,
        "overhead_km": overhead_km,
        "cable_share": _ratio(cable_km, circuit_km),
        "circuit_km_per_substation": _ratio(circuit_km, n_busbars),
        "supply_points_per_substation": _ratio(n_supply, n_busbars),
        "km_per_supply_point": _ratio(circuit_km, n_supply),
        "km_per_station": _ratio(circuit_km, kinds.get("station", 0)),
        **_stats("ring_members", true_rings["ring_members"]),
        **_stats("ring_members_incl_stubs", true_rings["ring_members_incl_stubs"]),
        **_stats("ring_km", true_rings["ring_km"]),
        **_stats("feeder_km", with_supply["feeder_km"]),
        **_stats("feeder_km_incl_stubs", with_supply["feeder_km_incl_stubs"]),
        **_stats("feeder_supply_points", with_supply["feeder_supply_points"]),
        "mean_section_km": _ratio(plain_rings["ring_km"].sum(), sections),
        "n_stub_nodes": int(feeders["n_stub_nodes"].sum()),
        "stub_km": float(feeders["stub_km"].sum()),
        "n1_secure_share": _ratio(n_secure, n_supply),
        "stub_share": _ratio(n_stub_side, n_supply),
        "unsupplied_share": _ratio(n_unsupplied, n_supply),
        "station_spacing_m": station_spacing_m(nodes_n),
        "customer_share": _ratio(kinds.get("customer", 0), n_supply),
        "area_class": (_majority(nodes_n.loc[nodes_n["kind"].isin(SUPPLY_POINT_KINDS), "area_class"])
                       if "area_class" in nodes_n.columns else None),
        **a.diagnostics,
    }
    return _clean(out)
