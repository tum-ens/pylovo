"""Normally-open ties between the rings of neighbouring HV/MV supply districts (DESIGN §4.5, WP14), and normally-open
links between the rings of one district.

After all districts of a variant are planned, :func:`plan_and_save_ties` adds at most one normally-open tie per
ring where a ring of another district (fed by another HV/MV substation, at the same nominal voltage) comes within
``tie_max_length_m`` along the routing graph. The ties model the reserve between substations (Gegenstation) that a
cost-optimal plan of single districts lacks:

1. **Candidates** (:func:`tie_candidates`): the switchgear members of the open rings of the done districts, i.e.
   the stations and MV customers looped into a ring. Joints, stub nodes, dedicated feeders and busbar radials never
   hold a tie.
2. **Distances** (:func:`plan_ties`): pairs of candidates of different districts with the same nominal voltage
   within ``tie_max_length_m`` (Euclidean pre-filter) get their street distance on one run graph of the routing
   graph (split at the attachment points, with connectors, without island bridges) from a Dijkstra limited to
   ``tie_max_length_m``.
3. **Link rule** of the district links (:func:`pylovo.mv_grid.topology.distance_model`): a tie follows the
   routing graph and is an overhead line where its route runs on OSM MV lines, a cable elsewhere.
4. **Selection:** greedy by tie length (then street distance and node ids), at most one tie per ring. The tie
   belongs to the district with the lower ``mv_grid_id`` (``from_node``) and is open at the other end
   (``to_node``).
5. **Flags:** ``incomplete_district`` (a district of the tie is not complete: its cell is cut by the region border
   or its substation lies outside the region) and ``substation_outside_region``. Such ties are kept.
6. **Takeover** (:func:`takeover_checks`, informational): with the tie closed and the busbar exit of the half-ring
   at one end out of service, can the other district supply that half-ring through its own ring within the (n-1)
   limits? One pandapower flow per tie end on the two stored district nets.

Ties change no normal-operation flow. They are ``mv_line`` rows with ``role = 'tie'``, ``normally_open = true``,
``open_at_node = to_node``, ``mv_grid_id`` of the ``from_node`` and no ring or feeder; they are never part of the
district nets or their cases. ``mv_grid.circuit_km`` and the district metrics exclude them; their length, the
share of rings with a tie and the takeover results go into ``mv_variant.summary['ties']`` and
``mv_grid.metrics['ties']``, and ``mv_ring.has_tie`` marks the rings with a tie.

**Links** (:func:`plan_links`, :func:`plan_and_save_links`): real MV grids join neighbouring rings by cross
connections, so that their stations can be resupplied in more ways than through their own ring, and many short
loops run between rings rather than through the busbar. After the ties, the stage joins ring members of different
rings of the same district within their reach (true length along the routing graph), greedy by length: each member
takes at most one link (a third cable bay) and two rings at most two links, which close a loop between them. The
reach of a node is ``link_max_length_m`` of the settlement type of its PLZ (:func:`link_reach`), and a link takes the
shorter reach of its two ends. Links are ``mv_line`` rows with ``role = 'link'``, normally open at ``to_node``,
outside the cases like the ties; ``mv_variant.summary['links']`` and ``mv_grid.metrics['links']`` hold their counts.

**Links from stub ends** (``link_stub_ends``): after the links between rings, each stub end (a station or MV
customer on a stub with one MV line, :func:`stub_link_candidates`) may take one link to a ring member or stub node of
another feeder of its district within the reach, greedy by length with the same limit per node. The stub end then
has a second infeed, as many real rural stations have. These links have role ``link`` as well; the summary counts
them and the stub ends with a link.

The planning functions are pure (no database, configuration or plotting import); :func:`plan_and_save_ties` and
:func:`plan_and_save_links` read and write through the methods of
:class:`~pylovo.database.mv_result_mixin.MvResultMixin` and :class:`~pylovo.database.mv_graph_mixin.MvGraphMixin`.
"""
from __future__ import annotations

import logging
import math
import time
import warnings
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping, Sequence

import numpy as np
import pandas as pd
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree
from shapely.geometry import LineString

from pylovo.mv_grid import equipment, graph
from pylovo.mv_grid.model import EdgeSet, line_kind, reach_text, stub_end_text
from pylovo.mv_grid.options import SETTLEMENT_TYPE_KEYS

if TYPE_CHECKING:
    import pandapower as pp

    from pylovo.mv_grid.parameters import MvParameters

_LOGGER = logging.getLogger(__name__)

#: Node kinds that may hold a tie: the switchgear ring members with a load.
TIE_NODE_KINDS: tuple[str, ...] = ("station", "customer")
#: Node connections of the links from stub ends (``link_stub_ends``): ring members and stub nodes.
STUB_LINK_CONNECTIONS: tuple[str, ...] = ("ring", "stub")
#: Flags of a tie (kept in ``mv_grid.metrics['ties']`` and counted in the variant summary).
TIE_FLAGS: tuple[str, ...] = ("incomplete_district", "substation_outside_region")
#: Columns of the tie table of :func:`plan_ties`.
TIE_COLUMNS: tuple[str, ...] = (
    "from_node", "to_node", "from_grid", "to_grid", "from_ring", "to_ring", "from_feeder", "to_feeder", "kind",
    "length_m", "street_m", "straight_m", "cable_m", "overhead_m", "parallel", "std_type", "flags", "geometry",
)
#: Columns of the table of :func:`takeover_checks`.
TAKEOVER_COLUMNS: tuple[str, ...] = (
    "tie", "side", "mv_grid_id", "ring_id", "node", "partner_grid_id", "converged", "drop_percent",
    "loading_percent", "ok", "error",
)
#: Shortest stored tie (no zero-impedance line between co-located nodes), as for the district lines.
MIN_TIE_M: float = 1.0
#: Links per ring member: one more cable bay than the two of a ring station.
LINKS_PER_NODE: int = 1
#: Links between the same two rings: two links close a loop between the rings that does not run through the busbar.
LINKS_PER_RING_PAIR: int = 2
#: Slack of the Euclidean pre-filter (m): the edge lengths of the graph may be slightly shorter than its geometry.
PREFILTER_SLACK_M: float = 50.0
#: Margin of the subgraph around the candidate points beyond ``tie_max_length_m`` (m).
SUBGRAPH_MARGIN_M: float = 100.0
#: Source vertices per Dijkstra call.
_CHUNK: int = 64


# ---------------------------------------------------------------------------------------------- candidates
def _numbers(frame: pd.DataFrame, column: str, default: float = np.nan) -> np.ndarray:
    if column not in frame.columns:
        return np.full(len(frame), default, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _texts(frame: pd.DataFrame, column: str) -> np.ndarray:
    if column not in frame.columns:
        return np.full(len(frame), "", dtype=object)
    return frame[column].astype(object).where(frame[column].notna(), "").astype(str).to_numpy()


def tie_candidates(nodes: pd.DataFrame, grids: pd.DataFrame) -> pd.DataFrame:
    """The nodes that may hold a tie: the switchgear members of the open rings of done districts.

    Args:
        nodes: One row per node, index ``mv_node_id``: ``mv_grid_id``, ``kind``, ``connection``, ``ring_id``,
            ``feeder_id``, ``x``, ``y``, ``attach_edge_id``, ``attach_fraction``, ``attach_offset_m``,
            ``settlement_id`` (NaN: open field) and ``area_class``; optional ``ring_kind`` (kind of the node's
            ``mv_ring`` row) and ``ax``, ``ay`` (street point of the attachment).
        grids: One row per district, index ``mv_grid_id``, with ``vn_kv``; optional ``status`` (only ``done``
            districts take part).

    Returns:
        The rows of ``nodes`` of kind station or customer, looped into a ring (``connection`` ring, a ring and a
        feeder, ``ring_kind`` ring when given) of a district of ``grids`` (done, with a nominal voltage), sorted
        by ``mv_node_id``.
    """
    return _members(nodes, grids, ("ring",))


def stub_link_candidates(nodes: pd.DataFrame, grids: pd.DataFrame) -> pd.DataFrame:
    """The nodes of the links from stub ends (``link_stub_ends``): the ring members of :func:`tie_candidates` and the
    stations and MV customers on the stubs of the open rings of done districts.

    Args:
        nodes: As for :func:`tie_candidates`, with ``degree`` (MV lines at the node, without ties and links).

    Returns:
        The rows sorted by ``mv_node_id``, with the column ``stub_end``: a stub node with one MV line.
    """
    out = _members(nodes, grids, STUB_LINK_CONNECTIONS).copy()
    out["stub_end"] = (_texts(out, "connection") == "stub") & (_numbers(out, "degree") == 1)
    return out


def _members(nodes: pd.DataFrame, grids: pd.DataFrame, connections: Sequence[str]) -> pd.DataFrame:
    """The station and customer rows of ``nodes`` with one of the ``connections``, on an open ring (a ring and a
    feeder, ``ring_kind`` ring when given) of a done district with a nominal voltage, sorted by ``mv_node_id``."""
    if nodes is None or not len(nodes):
        return pd.DataFrame(nodes).iloc[0:0]
    usable = grids
    if "status" in grids.columns:
        usable = grids[grids["status"].astype(object) == "done"]
    usable = usable[np.isfinite(_numbers(usable, "vn_kv"))]
    keep = (np.isin(_texts(nodes, "kind"), TIE_NODE_KINDS)
            & np.isin(_texts(nodes, "connection"), tuple(connections))
            & np.isfinite(_numbers(nodes, "ring_id"))
            & np.isfinite(_numbers(nodes, "feeder_id"))
            & np.isin(_numbers(nodes, "mv_grid_id"), np.asarray(usable.index, dtype=float))
            & np.isfinite(_numbers(nodes, "x")) & np.isfinite(_numbers(nodes, "y")))
    if "ring_kind" in nodes.columns:
        keep &= _texts(nodes, "ring_kind") == "ring"
    out = nodes[keep]
    return out.sort_index(kind="mergesort")


def close_pairs(candidates: pd.DataFrame, grids: pd.DataFrame, radius_m: float, *, within: bool = False,
                apart: str = "ring_id") -> np.ndarray:
    """Row pairs ``(i, j)``, ``i < j``, of candidates whose points lie within ``radius_m`` (the Euclidean pre-filter:
    a street path is never shorter than the straight line): of different districts with the same nominal voltage
    (ties), or with ``within`` of the same district and different rings (links).

    Args:
        candidates: Rows of :func:`tie_candidates` (positional rows are returned).
        grids: Districts with ``vn_kv``, index ``mv_grid_id``.
        radius_m: Search radius.
        within: Pairs inside one district (different rings) instead of pairs between districts.
        apart: With ``within``, the column whose values differ in a pair: ``ring_id`` (links between rings) or
            ``feeder_id`` (links from stub ends, which may join the two halves of one ring).

    Returns:
        ``(n, 2)`` int64 array of row positions, sorted.
    """
    if len(candidates) < 2:
        return np.zeros((0, 2), dtype=np.int64)
    xy = candidates[["x", "y"]].to_numpy(dtype=float)
    pairs = cKDTree(xy).query_pairs(float(radius_m), output_type="ndarray").astype(np.int64)
    if not len(pairs):
        return np.zeros((0, 2), dtype=np.int64)
    pairs = np.sort(pairs, axis=1)
    grid = candidates["mv_grid_id"].to_numpy(dtype=np.int64)
    vn = pd.to_numeric(grids["vn_kv"], errors="coerce").reindex(grid).to_numpy(dtype=float)
    same_voltage = np.isfinite(vn[pairs[:, 0]]) & np.isclose(vn[pairs[:, 0]], vn[pairs[:, 1]])
    if within:
        group = _numbers(candidates, apart)
        keep = (grid[pairs[:, 0]] == grid[pairs[:, 1]]) & (group[pairs[:, 0]] != group[pairs[:, 1]]) & same_voltage
    else:
        keep = (grid[pairs[:, 0]] != grid[pairs[:, 1]]) & same_voltage
    pairs = pairs[keep]
    return pairs[np.lexsort((pairs[:, 1], pairs[:, 0]))]


# ---------------------------------------------------------------------------------------------- planning
def empty_ties() -> pd.DataFrame:
    """A tie table without rows (the columns of :data:`TIE_COLUMNS`)."""
    return pd.DataFrame({c: pd.Series(dtype=object) for c in TIE_COLUMNS})


def _run_graph_rows(candidates: pd.DataFrame) -> pd.DataFrame:
    """The candidate rows in the form of :func:`graph.build_run_graph` (every row ``excluded``: no island bridges)."""
    frame = pd.DataFrame({
        "kind": _texts(candidates, "kind"),
        "connection_hint": "excluded",
        "x": _numbers(candidates, "x"),
        "y": _numbers(candidates, "y"),
        "attach_edge_id": _numbers(candidates, "attach_edge_id"),
        "attach_fraction": _numbers(candidates, "attach_fraction", 0.0),
        "attach_offset_m": _numbers(candidates, "attach_offset_m", 0.0),
        "ax": _numbers(candidates, "ax"),
        "ay": _numbers(candidates, "ay"),
    })
    return frame.reset_index(drop=True)


def _street_distances(rg: Any, pairs: np.ndarray, limit_m: float) -> np.ndarray:
    """Street distance per local row pair (``inf`` beyond ``limit_m`` or unreachable)."""
    out = np.full(len(pairs), np.inf)
    by_source: dict[int, list[int]] = defaultdict(list)
    for p, (i, _) in enumerate(pairs):
        by_source[int(i)].append(p)
    sources = sorted(by_source)
    vertex = np.asarray(rg.terminal_vertex, dtype=np.int64)
    for start in range(0, len(sources), _CHUNK):
        chunk = sources[start:start + _CHUNK]
        dist = dijkstra(rg.csr, directed=False, indices=vertex[chunk], limit=float(limit_m))
        dist = np.atleast_2d(dist)
        for k, source in enumerate(chunk):
            for p in by_source[source]:
                out[p] = dist[k, vertex[int(pairs[p, 1])]]
    return out


def _true_lengths(rg: Any, pairs: np.ndarray) -> np.ndarray:
    """True length (m) of the minimum-cost route per local row pair (``inf`` if unreachable).

    The run graph weights OSM MV lines below their length (``osm_cost_factor``), so the street distance of
    :func:`_street_distances` undercounts routes on OSM corridors.
    """
    vertex = np.asarray(rg.terminal_vertex, dtype=np.int64)
    keys = [(int(vertex[i]), int(vertex[j])) for i, j in pairs]
    paths = graph.vertex_paths(rg, list(set(keys)))
    piece_m = rg.pieces["length_m"].to_numpy(dtype=float)
    out = np.full(len(pairs), np.inf)
    for p, key in enumerate(keys):
        path = paths.get(key, [])
        if path:
            out[p] = float(piece_m[graph.path_pieces(rg, path)].sum())
    return out


def _straight_edge_geometries(edges: EdgeSet, edge_ids: Sequence[int]) -> dict[int, Any]:
    """Straight geometries of graph edges from their end vertices (when no edge geometry reader is given)."""
    index = {int(v): i for i, v in enumerate(np.asarray(edges.vertex_id, dtype=np.int64).tolist())}
    pos = {int(e): i for i, e in enumerate(np.asarray(edges.edge_id, dtype=np.int64).tolist())}
    xy = np.asarray(edges.vertex_xy, dtype=float).reshape(-1, 2)
    out = {}
    for e in edge_ids:
        i = pos.get(int(e))
        if i is not None:
            a = xy[index[int(edges.source[i])]]
            b = xy[index[int(edges.target[i])]]
            out[int(e)] = LineString([tuple(a), tuple(b)])
    return out


def _with_ends(line: Any, p0: np.ndarray, p1: np.ndarray) -> LineString:
    """``line`` starting and ending exactly at the node points (as the district lines)."""
    coords = [(float(x), float(y)) for x, y, *_ in line.coords] if line is not None else []
    start, end = (float(p0[0]), float(p0[1])), (float(p1[0]), float(p1[1]))
    if not coords or math.hypot(coords[0][0] - start[0], coords[0][1] - start[1]) > 1e-6:
        coords.insert(0, start)
    if math.hypot(coords[-1][0] - end[0], coords[-1][1] - end[1]) > 1e-6:
        coords.append(end)
    clean = [coords[0]] + [c for prev, c in zip(coords[:-1], coords[1:]) if c != prev]
    if len(clean) < 2:
        clean = [start, (end[0] + 0.01, end[1])]
    return LineString(clean)


def _known(value: Any) -> bool:
    """Whether a flag cell holds a value (not None, NaN or NA)."""
    return value is not None and not (isinstance(value, float) and math.isnan(value)) and value is not pd.NA


def _flags(grids: pd.DataFrame, grid_ids: Sequence[int]) -> list[str]:
    """The flags of a tie between the given districts: an incomplete district (``complete`` not true, also when
    unknown) and a substation outside the region (``in_region`` false); a missing column flags nothing."""
    flags = []
    if "complete" in grids.columns:
        values = [grids["complete"].get(g) for g in grid_ids]
        if any(not (_known(v) and bool(v)) for v in values):
            flags.append("incomplete_district")
    if "in_region" in grids.columns:
        values = [grids["in_region"].get(g) for g in grid_ids]
        if any(_known(v) and not bool(v) for v in values):
            flags.append("substation_outside_region")
    return flags


def _measure(cand: pd.DataFrame, pairs: np.ndarray, edges: EdgeSet, params: "MvParameters", limit: float
             ) -> dict | None:
    """Street distance, length and line kind of candidate row pairs within ``limit`` (steps 2 and 3); None when no
    pair is within reach."""
    rows = np.unique(pairs)
    local = {int(r): k for k, r in enumerate(rows.tolist())}
    sub = cand.iloc[rows]
    lpairs = np.array([(local[int(i)], local[int(j)]) for i, j in pairs], dtype=np.int64).reshape(-1, 2)
    rg = graph.build_run_graph(edges, _run_graph_rows(sub), long_connector_m=params.long_connector_m,
                               bridge_detour=params.bridge_detour, osm_cost_factor=params.osm_line_cost_factor)
    street = _street_distances(rg, lpairs, limit)
    ok = np.isfinite(street) & (street <= limit)
    lpairs, street = lpairs[ok], street[ok]
    if not len(lpairs):
        return None
    if "cost_m" in rg.pieces.columns and not np.allclose(rg.pieces["cost_m"], rg.pieces["length_m"]):
        street = _true_lengths(rg, lpairs)              # the limit and the ranking use the true route length
        ok = street <= limit
        lpairs, street = lpairs[ok], street[ok]
        if not len(lpairs):
            return None
    xy = sub[["x", "y"]].to_numpy(dtype=float)
    i, j = lpairs[:, 0], lpairs[:, 1]
    straight_m = np.hypot(*(xy[i] - xy[j]).T)
    return {"sub": sub, "rg": rg, "pairs": lpairs, "street": street, "straight_m": straight_m,
            "length": np.maximum(street, MIN_TIE_M), "xy": xy,
            "ids": np.asarray(sub.index, dtype=np.int64), "grid": sub["mv_grid_id"].to_numpy(dtype=np.int64),
            "ring": _numbers(sub, "ring_id").astype(np.int64), "feeder": _numbers(sub, "feeder_id").astype(np.int64)}


def _line_table(m: dict, ends: Sequence[tuple[int, int, int]], grids: pd.DataFrame, edges: EdgeSet,
                params: "MvParameters", edge_geometry: Callable[[Sequence[int]], Mapping[int, Any]] | None,
                flags: Callable[[int, int], list[str]]) -> pd.DataFrame:
    """The rows (:data:`TIE_COLUMNS`) of the chosen pairs ``(pair, from_row, to_row)`` of :func:`_measure`: street
    geometry, true length, kind and type."""
    rg, xy, street, length = m["rg"], m["xy"], m["street"].copy(), m["length"].copy()
    ids, grid = m["ids"], m["grid"]
    vertex = np.asarray(rg.terminal_vertex, dtype=np.int64)
    street_ends = [(int(vertex[f]), int(vertex[t])) for p, f, t in ends]
    paths = graph.vertex_paths(rg, street_ends) if street_ends else {}
    pieces = {key: graph.path_pieces(rg, path) for key, path in paths.items() if path}
    edge_kind = rg.pieces["kind"].astype(str).to_numpy()
    edge_ids = rg.pieces["edge_id"].to_numpy(dtype=np.int64)
    on_osm = (rg.pieces["overhead"].fillna(False).to_numpy(dtype=bool) if "overhead" in rg.pieces.columns
              else np.zeros(len(rg.pieces), dtype=bool))
    piece_m = rg.pieces["length_m"].to_numpy(dtype=float)
    wanted = sorted({int(edge_ids[q]) for ps in pieces.values() for q in ps if edge_kind[q] == "street"})
    if wanted:
        geoms = dict(edge_geometry(wanted)) if edge_geometry is not None else _straight_edge_geometries(edges, wanted)
        missing = [e for e in wanted if e not in geoms]
        if missing:
            geoms.update(_straight_edge_geometries(edges, missing))
    else:
        geoms = {}
    vn_of = pd.to_numeric(grids["vn_kv"], errors="coerce")             # both ends have the same voltage

    out = []
    for p, f, t in ends:
        key = (int(vertex[f]), int(vertex[t]))
        line = graph.path_geometry(rg, pieces.get(key, []), geoms, start_vertex=key[0])
        geometry = _with_ends(line, xy[f], xy[t])
        route = [q for q in pieces.get(key, []) if q >= 0]   # overhead where the route runs on OSM MV lines
        if route:                                   # the true length of the route (street is its cost)
            length[p] = street[p] = max(float(piece_m[route].sum()), MIN_TIE_M)
        overhead_m = min(float(length[p]), float(piece_m[route][on_osm[route]].sum()) if route else 0.0)
        kind = line_kind(overhead_m, float(length[p]))
        vn_kv = float(vn_of.loc[int(grid[f])])
        cable_type, overhead_type = params.cable_at(vn_kv), params.overhead_at(vn_kv)
        std_type = {"cable": cable_type.name, "overhead": overhead_type.name}.get(
            kind, f"{cable_type.name} + {overhead_type.name}")
        out.append({
            "from_node": int(ids[f]), "to_node": int(ids[t]), "from_grid": int(grid[f]), "to_grid": int(grid[t]),
            "from_ring": int(m["ring"][f]), "to_ring": int(m["ring"][t]), "from_feeder": int(m["feeder"][f]),
            "to_feeder": int(m["feeder"][t]), "kind": kind,
            "length_m": float(length[p]), "street_m": float(street[p]), "straight_m": float(m["straight_m"][p]),
            "cable_m": float(length[p]) - overhead_m, "overhead_m": overhead_m, "parallel": 1,
            "std_type": std_type, "flags": flags(int(grid[f]), int(grid[t])), "geometry": geometry,
        })
    table = pd.DataFrame(out, columns=list(TIE_COLUMNS))
    return table.sort_values(["from_grid", "from_ring", "from_node"], kind="mergesort").reset_index(drop=True)


def plan_ties(nodes: pd.DataFrame, grids: pd.DataFrame, edges: EdgeSet, params: "MvParameters",
              edge_geometry: Callable[[Sequence[int]], Mapping[int, Any]] | None = None) -> pd.DataFrame:
    """The normally-open ties of a variant (steps 1-5 of the module docstring; pure).

    Args:
        nodes: The nodes of the variant (see :func:`tie_candidates`; other rows are ignored).
        grids: The districts, index ``mv_grid_id``: ``vn_kv``, optional ``status``, ``complete`` and
            ``in_region`` (of the substation).
        edges: The routing graph around the candidates (at least within ``tie_max_length_m`` of them, with their
            attachment edges).
        params: The MV variant (``tie_max_length_m``, ``osm_line_cost_factor``, the line types of the
            districts' voltage).
        edge_geometry: Graph edge ids -> shapely LineStrings (source -> target) for the street geometries;
            None draws every edge straight between its end vertices.

    Returns:
        One row per tie (:data:`TIE_COLUMNS`), sorted by ``from_grid``, ``from_ring``: the ends (``from_node`` in the
        district with the lower ``mv_grid_id``, open at ``to_node``), their districts, rings and feeders, ``kind``
        (cable, overhead or mixed), ``length_m`` (the circuit length: the true length of the route), ``street_m``,
        ``straight_m``, ``cable_m``, ``overhead_m``, ``std_type``, ``flags`` (list) and ``geometry`` (shapely
        LineString from ``from_node`` to ``to_node``).
    """
    limit = float(params.tie_max_length_m)
    cand = tie_candidates(nodes, grids)
    pairs = close_pairs(cand, grids, limit + PREFILTER_SLACK_M)
    m = _measure(cand, pairs, edges, params, limit) if len(pairs) else None
    if m is None:
        return empty_ties()
    i, j = m["pairs"][:, 0], m["pairs"][:, 1]
    ids, grid, ring = m["ids"], m["grid"], m["ring"]
    # greedy by length, then street distance and node ids; at most one tie per ring
    order = np.lexsort((ids[j], ids[i], m["street"], m["length"]))
    used: set[tuple[int, int]] = set()
    ends = []
    for p in order:
        a, b = (int(grid[i[p]]), int(ring[i[p]])), (int(grid[j[p]]), int(ring[j[p]]))
        if a in used or b in used:
            continue
        used.update((a, b))
        # orientation: the district with the lower mv_grid_id owns the tie, open at the other end
        f, t = (int(i[p]), int(j[p])) if grid[i[p]] < grid[j[p]] else (int(j[p]), int(i[p]))
        ends.append((int(p), f, t))
    if not ends:
        return empty_ties()
    return _line_table(m, ends, grids, edges, params, edge_geometry,
                       lambda a, b: _flags(grids, (a, b)))


def link_reach(nodes: pd.DataFrame, params: "MvParameters") -> np.ndarray:
    """The reach (m) of each node for the links: ``link_max_length_m`` of the settlement type of its PLZ (column
    ``settlement_type``: 1 rural, 2 semi-urban, 3 urban); a node without a known type takes the urban reach."""
    reach = params.link_max_length_m
    types = _numbers(nodes, "settlement_type")
    keys = [SETTLEMENT_TYPE_KEYS.get(int(t), "urban") if np.isfinite(t) else "urban" for t in types]
    return np.array([float(reach[k]) for k in keys], dtype=float)


def largest_reach(params: "MvParameters") -> float:
    """The longest reach of ``link_max_length_m`` (the search radius of the links)."""
    return max(float(v) for v in params.link_max_length_m.values())


def _within_reach(m: dict | None, params: "MvParameters") -> dict | None:
    """The pairs of :func:`_measure` whose street distance is within the shorter reach of their two ends; None
    when no pair is left."""
    if m is None:
        return None
    reach = link_reach(m["sub"], params)
    i, j = m["pairs"][:, 0], m["pairs"][:, 1]
    ok = m["street"] <= np.minimum(reach[i], reach[j])
    if not ok.any():
        return None
    out = dict(m)
    for key in ("pairs", "street", "straight_m", "length"):
        out[key] = m[key][ok]
    return out


def stub_link_pairs(candidates: pd.DataFrame, grids: pd.DataFrame, limit_m: float,
                    full: Collection[int] = ()) -> np.ndarray:
    """Row pairs of :func:`stub_link_candidates` that may hold a link from a stub end: in one district, on different
    feeders, at least one end a stub end, within the pre-filter of ``limit_m``, and no end in ``full`` (the
    ``mv_node_id`` of nodes that already hold their links)."""
    pairs = close_pairs(candidates, grids, limit_m + PREFILTER_SLACK_M, within=True, apart="feeder_id")
    if not len(pairs):
        return pairs
    end = candidates["stub_end"].to_numpy(dtype=bool)
    ok = end[pairs[:, 0]] | end[pairs[:, 1]]
    if len(full):
        free = ~np.isin(np.asarray(candidates.index, dtype=np.int64), np.asarray(list(full), dtype=np.int64))
        ok &= free[pairs[:, 0]] & free[pairs[:, 1]]
    return pairs[ok]


def _greedy_links(m: dict, per_node: dict[int, int], per_pair: dict[tuple[int, int, int], int] | None
                  ) -> list[tuple[int, int, int]]:
    """The chosen pairs ``(pair, from_row, to_row)`` of :func:`_measure`, greedy by length (then street distance and
    node ids): at most :data:`LINKS_PER_NODE` links per node (``per_node`` by ``mv_node_id``, updated) and, with
    ``per_pair``, at most :data:`LINKS_PER_RING_PAIR` per ring pair. The end on the ring with the lower ``ring_id``
    (then the lower ``mv_node_id``) comes first."""
    i, j = m["pairs"][:, 0], m["pairs"][:, 1]
    ids, grid, ring = m["ids"], m["grid"], m["ring"]
    order = np.lexsort((ids[j], ids[i], m["street"], m["length"]))
    ends = []
    for p in order:
        a, b = int(i[p]), int(j[p])
        na, nb = int(ids[a]), int(ids[b])
        if per_node[na] >= LINKS_PER_NODE or per_node[nb] >= LINKS_PER_NODE:
            continue
        if per_pair is not None:
            key = (int(grid[a]), *sorted((int(ring[a]), int(ring[b]))))
            if per_pair[key] >= LINKS_PER_RING_PAIR:
                continue
            per_pair[key] += 1
        per_node[na] += 1
        per_node[nb] += 1
        f, t = (a, b) if (ring[a], ids[a]) < (ring[b], ids[b]) else (b, a)
        ends.append((int(p), f, t))
    return ends


def plan_links(nodes: pd.DataFrame, grids: pd.DataFrame, edges: EdgeSet, params: "MvParameters",
               edge_geometry: Callable[[Sequence[int]], Mapping[int, Any]] | None = None) -> pd.DataFrame:
    """The normally-open links inside the districts (pure).

    1. **Between rings:** pairs of ring members (stations and MV customers with switchgear) of different rings of the
       same district within their reach along the routing graph (true length), greedy by length: each member holds
       at most one link (a third cable bay, :data:`LINKS_PER_NODE`), and two rings are joined at most
       :data:`LINKS_PER_RING_PAIR` times, so that two links can close a short loop between them.
    2. **From stub ends** (with ``link_stub_ends``): pairs of a stub end and a ring member or stub node of another
       feeder of the same district (:func:`stub_link_pairs`) within their reach, greedy by length with the same limit
       per node, after the links between rings: the stub end gets a second infeed.

    The reach of a pair is the shorter :func:`link_reach` of its two ends (``link_max_length_m`` by the settlement
    type of the PLZ, column ``settlement_type``). Lengths, kinds and geometries as for the ties.

    Returns:
        One row per link (:data:`TIE_COLUMNS`; ``from_grid`` = ``to_grid``), the end on the ring with the lower
        ``ring_id`` first, open at ``to_node``; ``flags`` empty; sorted by ``from_grid``, ``from_ring`` and
        ``from_node``.
    """
    limit = largest_reach(params)
    per_node: dict[int, int] = defaultdict(int)
    tables = []
    cand = tie_candidates(nodes, grids)
    pairs = close_pairs(cand, grids, limit + PREFILTER_SLACK_M, within=True)
    m = _within_reach(_measure(cand, pairs, edges, params, limit) if len(pairs) else None, params)
    if m is not None:
        ends = _greedy_links(m, per_node, defaultdict(int))
        if ends:
            tables.append(_line_table(m, ends, grids, edges, params, edge_geometry, lambda a, b: []))
    if params.link_stub_ends:
        cand = stub_link_candidates(nodes, grids)
        pairs = stub_link_pairs(cand, grids, limit, [n for n, k in per_node.items() if k >= LINKS_PER_NODE])
        m = _within_reach(_measure(cand, pairs, edges, params, limit) if len(pairs) else None, params)
        if m is not None:
            ends = _greedy_links(m, per_node, None)
            if ends:
                tables.append(_line_table(m, ends, grids, edges, params, edge_geometry, lambda a, b: []))
    if not tables:
        return empty_ties()
    if len(tables) == 1:
        return tables[0]
    links = pd.concat(tables, ignore_index=True)
    return links.sort_values(["from_grid", "from_ring", "from_node"], kind="mergesort").reset_index(drop=True)


# ---------------------------------------------------------------------------------------------- takeover
def _prepared_net(net: Any, mv_grid_id: int, params: "MvParameters") -> "pp.pandapowerNet":
    """A stored district net in the load case without its HV side (ideal slack at the MV busbar), tagged with its
    ``mv_grid_id`` on the bus and line tables."""
    from pylovo.mv_grid.coupling import mv_part
    from pylovo.mv_grid.electrical import case_net

    if net is None:
        raise LookupError(f"MV grid {mv_grid_id} has no stored net")
    out = case_net(mv_part(net, vm_pu=float(params.busbar_vm_pu)), "load", params)
    out.bus["mv_grid_id"] = float(mv_grid_id)
    out.line["mv_grid_id"] = float(mv_grid_id)
    return out


def _bus(net: "pp.pandapowerNet", mv_grid_id: int, node: int) -> int:
    bus = net.bus
    sel = bus.index[(bus["mv_grid_id"] == float(mv_grid_id)) & (bus["mv_node_id"] == float(node))]
    if len(sel) != 1:
        raise LookupError(f"node {node} has {len(sel)} buses in the stored net of MV grid {mv_grid_id}")
    return int(sel[0])


def _runpp(net: "pp.pandapowerNet") -> bool:
    import pandapower as pp
    from pandapower.auxiliary import LoadflowNotConverged

    try:
        pp.runpp(net, algorithm="nr", init="auto")
    except LoadflowNotConverged:
        return False
    return bool(net.converged)


def _takeover_end(net: "pp.pandapowerNet", tie_line: int, end: dict, params: "MvParameters") -> dict:
    """One end of a tie: its half-ring out of its busbar exit, supplied through the closed tie."""
    line = net.line
    own = (line["mv_grid_id"] == float(end["mv_grid_id"])) & (line["ring_id"] == float(end["ring_id"]))
    partner = (line["mv_grid_id"] == float(end["partner_grid_id"])) & (line["ring_id"] == float(end["partner_ring"]))
    exit_column = line["exit"] if "exit" in line.columns else pd.Series(None, index=line.index, dtype=object)
    exits = line.index[own & exit_column.isin(("a", "b")) & (line["feeder_id"] == float(end["feeder_id"]))]
    if not len(exits):
        raise LookupError(f"ring {end['ring_id']} of MV grid {end['mv_grid_id']} has no busbar exit of feeder "
                          f"{end['feeder_id']}")
    lines = line.index[own | partner].append(pd.Index([tie_line]))
    buses = pd.Index(np.unique(np.r_[line.loc[lines, "from_bus"].to_numpy(), line.loc[lines, "to_bus"].to_numpy()]))
    result = {"converged": False, "drop_percent": None, "loading_percent": None, "ok": False, "error": None}
    line.loc[exits, "in_service"] = False
    try:
        if _runpp(net):
            vm = net.res_bus.loc[buses, "vm_pu"].astype(float)
            loading = net.res_line.loc[lines, "loading_percent"].astype(float)
            result["converged"] = True
            if vm.isna().any():
                result["error"] = "half-ring not supplied through the tie"
                return result
            drop = 100.0 * (float(params.busbar_vm_pu) - float(vm.min()))
            peak = float(np.nanmax(loading.to_numpy())) if loading.notna().any() else 0.0
            result.update(drop_percent=drop, loading_percent=peak,
                          ok=bool(drop <= float(params.max_drop_n1_percent) + 1e-9
                                  and peak <= 100.0 * float(params.n1_utilization) + 1e-9))
    finally:
        line.loc[exits, "in_service"] = True
    return result


def takeover_checks(ties: pd.DataFrame, nets: Mapping[int, Any], params: "MvParameters") -> pd.DataFrame:
    """Whether the partner district could take over the half-ring at each end of each tie in (n-1).

    For each tie and each end, the two stored district nets are merged without their HV sides (ideal slack at
    both MV busbars at ``busbar_vm_pu``, normal-case loads, no generation), the tie is closed and the busbar exit of
    the half-ring at that end is taken out of service, so this half-ring is supplied from the other substation
    through the other ring and the tie (one Newton-Raphson flow per end). The verdict uses the (n-1) limits on the
    two rings and the tie: the largest drop from ``busbar_vm_pu`` at most ``max_drop_n1_percent`` and the largest
    loading at most ``100 x n1_utilization``. The loads keep their half-ring values (the coincidence of the merged
    feeder would be lower), so the estimate is on the safe side. An end that cannot be checked gets ``error``.

    Args:
        ties: Ties of :func:`plan_ties`.
        nets: Stored pandapower net per ``mv_grid_id`` (``mv_grid.net``, buses with ``mv_node_id``, lines with
            ``ring_id``, ``feeder_id`` and ``exit``).
        params: The MV variant.

    Returns:
        One row per tie end (:data:`TAKEOVER_COLUMNS`): ``tie`` (row of ``ties``), ``side`` (``from`` or ``to``:
        the end whose half-ring is taken over), ``mv_grid_id``, ``ring_id``, ``node``, ``partner_grid_id``,
        ``converged``, ``drop_percent``, ``loading_percent``, ``ok`` and ``error``.
    """
    import pandapower as pp
    from pandapower.toolbox import merge_nets

    rows: list[dict] = []
    prepared: dict[int, Any] = {}
    errors: dict[int, str] = {}
    for t, tie in enumerate(ties.itertuples(index=False)):
        ends = [
            {"side": "from", "mv_grid_id": int(tie.from_grid), "ring_id": int(tie.from_ring), "node": int(tie.from_node),
             "feeder_id": int(tie.from_feeder), "partner_grid_id": int(tie.to_grid), "partner_ring": int(tie.to_ring)},
            {"side": "to", "mv_grid_id": int(tie.to_grid), "ring_id": int(tie.to_ring), "node": int(tie.to_node),
             "feeder_id": int(tie.to_feeder), "partner_grid_id": int(tie.from_grid), "partner_ring": int(tie.from_ring)},
        ]
        base = [{"tie": t, "side": e["side"], "mv_grid_id": e["mv_grid_id"], "ring_id": e["ring_id"],
                 "node": e["node"], "partner_grid_id": e["partner_grid_id"]} for e in ends]
        try:
            for g in (int(tie.from_grid), int(tie.to_grid)):
                if g in errors:
                    raise LookupError(errors[g])
                if g not in prepared:
                    try:
                        prepared[g] = _prepared_net(nets.get(g), g, params)
                    except Exception as exc:          # a grid without a usable net: every tie of it is skipped
                        errors[g] = f"{type(exc).__name__}: {exc}"
                        raise
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                net = merge_nets(prepared[int(tie.from_grid)], prepared[int(tie.to_grid)], validate=False,
                                 merge_results=False, net2_reindex_log_level=None)
            from_bus, to_bus = _bus(net, tie.from_grid, tie.from_node), _bus(net, tie.to_grid, tie.to_node)
            vn_kv = float(net.bus.at[from_bus, "vn_kv"])                  # the voltage of both districts
            cable_m = float(getattr(tie, "cable_m", 0.0) or 0.0)
            overhead_m = float(getattr(tie, "overhead_m", 0.0) or 0.0)
            values = equipment.line_parameters(tie.kind, params.cable_at(vn_kv), params.overhead_at(vn_kv),
                                               cable_m, overhead_m if cable_m + overhead_m > 0 else 1.0)
            tie_line = pp.create_line_from_parameters(
                net, from_bus, to_bus, length_km=max(float(tie.length_m), MIN_TIE_M) / 1000.0, name="tie",
                **{key: values[key] for key in ("r_ohm_per_km", "x_ohm_per_km", "c_nf_per_km", "max_i_ka")})
            net.line.at[tie_line, "role"] = "tie"
            for row, end in zip(base, ends):
                try:
                    row.update(_takeover_end(net, int(tie_line), end, params))
                except LookupError as exc:
                    row.update(converged=False, drop_percent=None, loading_percent=None, ok=False,
                               error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            text = " ".join(f"{type(exc).__name__}: {exc}".split())[:500]
            for row in base:
                row.update(converged=False, drop_percent=None, loading_percent=None, ok=False, error=text)
        rows.extend(base)
    return pd.DataFrame(rows, columns=list(TAKEOVER_COLUMNS))


# ---------------------------------------------------------------------------------------------- reporting
def _round(value: float | None, digits: int = 4) -> float | None:
    if value is None or not np.isfinite(value):
        return None
    return round(float(value), digits)


def grid_tie_metrics(ties: pd.DataFrame, takeover: pd.DataFrame | None = None) -> dict[int, dict]:
    """The tie entries of ``mv_grid.metrics['ties']`` per district touched by a tie.

    Returns:
        ``{mv_grid_id: {"n_ties", "tie_km", "n_rings_with_tie", "ties": [...]}}``; one record per tie end in the
        district: ``ring_id``, ``node``, ``partner_grid_id``, ``partner_ring_id``, ``partner_node``, ``length_km``,
        ``kind``, ``owner`` (the district owns the ``mv_line`` row), ``open_here`` (the open switch is at this
        end), ``flags`` and ``takeover`` (the partner takes over this end's half-ring: ``ok``, ``converged``,
        ``drop_percent``, ``loading_percent``, ``error``; None without a check).
    """
    parallel = _tie_parallel(ties)
    checks: dict[tuple[int, str], dict] = {}
    if takeover is not None and len(takeover):
        for rec in takeover.to_dict("records"):
            checks[(int(rec["tie"]), str(rec["side"]))] = {
                "ok": bool(rec["ok"]), "converged": bool(rec["converged"]),
                "drop_percent": _round(rec["drop_percent"]), "loading_percent": _round(rec["loading_percent"]),
                "error": rec["error"] if isinstance(rec["error"], str) else None}
    out: dict[int, dict] = {}
    for t, tie in enumerate(ties.itertuples(index=False)):
        for side, g, ring, node, pg, pring, pnode in (
                ("from", tie.from_grid, tie.from_ring, tie.from_node, tie.to_grid, tie.to_ring, tie.to_node),
                ("to", tie.to_grid, tie.to_ring, tie.to_node, tie.from_grid, tie.from_ring, tie.from_node)):
            entry = out.setdefault(int(g), {"n_ties": 0, "tie_km": 0.0, "n_rings_with_tie": 0, "ties": []})
            entry["ties"].append({
                "ring_id": int(ring), "node": int(node), "partner_grid_id": int(pg), "partner_ring_id": int(pring),
                "partner_node": int(pnode), "length_km": _round(float(tie.length_m) / 1000.0), "kind": tie.kind,
                "parallel": int(parallel[t]),
                "owner": side == "from", "open_here": side == "to", "flags": list(tie.flags),
                "takeover": checks.get((t, side))})
            entry["n_ties"] += 1
            entry["tie_km"] += float(tie.length_m) * parallel[t] / 1000.0
    for entry in out.values():
        entry["tie_km"] = _round(entry["tie_km"])
        entry["n_rings_with_tie"] = len({rec["ring_id"] for rec in entry["ties"]})
    return out


def grid_link_metrics(links: pd.DataFrame) -> dict[int, dict]:
    """The link entries of ``mv_grid.metrics['links']`` per district with a link.

    Returns:
        ``{mv_grid_id: {"n_links", "link_km", "n_rings_with_link", "n_nodes_with_link", "links": [...]}}``; one
        record per link: ``from_node``, ``to_node``, ``from_ring``, ``to_ring``, ``length_km`` and ``kind``.
    """
    out: dict[int, dict] = {}
    for link in links.itertuples(index=False):
        entry = out.setdefault(int(link.from_grid), {"n_links": 0, "link_km": 0.0, "links": []})
        entry["links"].append({"from_node": int(link.from_node), "to_node": int(link.to_node),
                               "from_ring": int(link.from_ring), "to_ring": int(link.to_ring),
                               "length_km": _round(float(link.length_m) / 1000.0), "kind": link.kind})
        entry["n_links"] += 1
        entry["link_km"] += float(link.length_m) / 1000.0
    for entry in out.values():
        entry["link_km"] = _round(entry["link_km"])
        entry["n_rings_with_link"] = len({r for rec in entry["links"] for r in (rec["from_ring"], rec["to_ring"])})
        entry["n_nodes_with_link"] = len({n for rec in entry["links"] for n in (rec["from_node"], rec["to_node"])})
    return out


def link_summary(links: pd.DataFrame, grids: pd.DataFrame, params: "MvParameters",
                 runtime_s: float | None = None, stub_ends: Collection[int] | None = None) -> dict:
    """The variant summary of the links inside the districts (``mv_variant.summary['links']``).

    Args:
        stub_ends: The ``mv_node_id`` of all stub ends of the done districts, with ``link_stub_ends``.

    Returns:
        A JSON-safe dict: ``n_links``, ``link_km``, ``link_cable_km``, ``link_overhead_km``, ``link_km_mean``,
        ``n_ring_pairs``, ``n_ring_pairs_closed`` (pairs of different rings joined twice: a loop that does not run
        through the busbar), ``n_rings``, ``n_rings_with_link``, ``ring_share_with_link``, ``n_supply_points``,
        ``n_nodes_with_link``, ``node_share_with_link``, ``link_max_length_m`` (the reach by settlement type),
        ``link_stub_ends`` and
        ``runtime_s``; with ``stub_ends`` also ``n_stub_ends``, ``n_stub_end_links`` (links with a stub end),
        ``n_stub_ends_with_link`` and ``stub_end_share_with_link``.
    """
    done = grids[grids["status"].astype(object) == "done"] if "status" in grids.columns else grids

    def total(column: str) -> float:
        return float(pd.to_numeric(done[column], errors="coerce").fillna(0.0).sum()) if column in done.columns else 0.0

    n = int(len(links))
    km = pd.to_numeric(links["length_m"], errors="coerce") / 1000.0 if n else pd.Series(dtype=float)
    pairs = pd.Series([(int(g), *sorted((int(a), int(b)))) for g, a, b in
                       zip(links["from_grid"], links["from_ring"], links["to_ring"]) if int(a) != int(b)],
                      dtype=object).value_counts() if n else pd.Series(dtype=int)
    rings = {(int(g), int(r)) for g, a, b in zip(links["from_grid"], links["from_ring"], links["to_ring"])
             for r in (a, b)} if n else set()
    nodes = {int(x) for x in links["from_node"]} | {int(x) for x in links["to_node"]} if n else set()
    n_rings = int(total("n_rings"))
    n_supply = int(total("n_stations") + total("n_customers"))
    out = {
        "n_links": n,
        "link_km": _round(float(km.sum())) if n else 0.0,
        "link_cable_km": _round(float(pd.to_numeric(links["cable_m"], errors="coerce").sum()) / 1000.0) if n else 0.0,
        "link_overhead_km": _round(float(pd.to_numeric(links["overhead_m"], errors="coerce").sum()) / 1000.0)
        if n else 0.0,
        "link_km_mean": _round(float(km.mean())) if n else None,
        "n_ring_pairs": int(len(pairs)),
        "n_ring_pairs_closed": int((pairs >= 2).sum()) if n else 0,
        "n_rings": n_rings,
        "n_rings_with_link": len(rings),
        "ring_share_with_link": _round(len(rings) / n_rings, 6) if n_rings else None,
        "n_supply_points": n_supply,
        "n_nodes_with_link": len(nodes),
        "node_share_with_link": _round(len(nodes) / n_supply, 6) if n_supply else None,
        "link_max_length_m": {k: float(v) for k, v in params.link_max_length_m.items()},
        "link_stub_ends": bool(params.link_stub_ends),
        "runtime_s": _round(runtime_s, 2) if runtime_s is not None else None,
    }
    if stub_ends is not None:
        ends = {int(x) for x in stub_ends}
        with_end = [int(a) in ends or int(b) in ends for a, b in zip(links["from_node"], links["to_node"])] if n \
            else []
        out.update({"n_stub_ends": len(ends), "n_stub_end_links": int(sum(with_end)),
                    "n_stub_ends_with_link": len(nodes & ends),
                    "stub_end_share_with_link": _round(len(nodes & ends) / len(ends), 6) if ends else None})
    return out


def describe_links(summary: Mapping[str, Any]) -> str:
    """One line of the link summary for the log."""
    n = int(summary.get("n_links") or 0)
    if not n:
        return (f"no normally-open links: no rings of one district within "
                f"{reach_text(summary.get('link_max_length_m'))}")
    stubs = bool(summary.get("n_stub_end_links"))
    text = (f"{n} normally-open links {'' if stubs else 'between rings '}inside the districts: "
            f"{summary.get('link_km') or 0:,.1f} km")
    if summary.get("n_rings_with_link") is not None and summary.get("n_rings"):
        text += f", {summary['n_rings_with_link']} of {summary['n_rings']} rings"
    if summary.get("node_share_with_link") is not None:
        text += (f", {100 * summary['node_share_with_link']:.0f} % of the supply points with "
                 f"{'a link' if stubs else 'a third cable'}")
    text += f", {summary.get('n_ring_pairs_closed', 0)} ring pairs joined twice"
    return text + stub_end_text(summary)


def _tie_parallel(ties: pd.DataFrame) -> np.ndarray:
    """Validated installed circuit count of each tie; older callers can omit the column."""
    values = pd.to_numeric(ties["parallel"].fillna(1), errors="coerce") if "parallel" in ties else pd.Series(
        np.ones(len(ties)))
    if (~np.isfinite(values) | (values < 1) | (values % 1 != 0)).any():
        raise ValueError("parallel must be a positive integer")
    return values.to_numpy(dtype=int)


def tie_summary(ties: pd.DataFrame, grids: pd.DataFrame, params: "MvParameters",
                takeover: pd.DataFrame | None = None, runtime_s: float | None = None) -> dict:
    """The variant summary of the ties (``mv_variant.summary['ties']``).

    Args:
        ties: Ties of :func:`plan_ties`.
        grids: The districts with ``status``, ``n_rings``, ``n_stations``, ``n_customers`` and ``circuit_km``
            (without ties); only ``done`` districts count.
        params: The MV variant.
        takeover: Results of :func:`takeover_checks`, or None.
        runtime_s: Wall-clock time of the tie step.

    Returns:
        A JSON-safe dict: ``n_ties``, ``tie_km``, ``tie_cable_km``, ``tie_overhead_km``, ``tie_km_mean``,
        ``tie_km_max``, ``n_district_pairs``, ``circuit_km`` (without ties), ``tie_share_of_circuit_km``,
        ``n_rings``, ``n_rings_with_tie``, ``ring_share_with_tie``, ``n_stations``, ``km_per_station`` and
        ``km_per_station_with_ties``, ``km_per_supply_point`` and ``km_per_supply_point_with_ties``, the flag
        counts ``n_incomplete_district`` and ``n_substation_outside_region``, the takeover counts
        (``takeover_checked``, ``takeover_ok``, ``takeover_share``, ``takeover_not_converged``,
        ``takeover_errors``, ``takeover_max_drop_percent``, ``takeover_max_loading_percent``),
        ``tie_max_length_m`` and ``runtime_s``.
    """
    done = grids[grids["status"].astype(object) == "done"] if "status" in grids.columns else grids

    def total(column: str) -> float:
        return float(pd.to_numeric(done[column], errors="coerce").fillna(0.0).sum()) if column in done.columns else 0.0

    n = int(len(ties))
    parallel = _tie_parallel(ties)
    lengths = pd.to_numeric(ties["length_m"], errors="coerce") / 1000.0 if n else pd.Series(dtype=float)
    circuit_lengths = lengths * parallel
    tie_km = float(circuit_lengths.sum()) if n else 0.0
    circuit_km, n_rings = total("circuit_km"), int(total("n_rings"))
    n_stations, n_supply = int(total("n_stations")), int(total("n_stations") + total("n_customers"))
    rings_with_tie = len({(int(g), int(r)) for g, r in zip(ties["from_grid"], ties["from_ring"])}
                         | {(int(g), int(r)) for g, r in zip(ties["to_grid"], ties["to_ring"])}) if n else 0
    flags = [f for fl in (ties["flags"] if n else []) for f in fl]
    out: dict[str, Any] = {
        "n_ties": n,
        "tie_km": _round(tie_km),
        "tie_cable_km": _round(float((pd.to_numeric(ties["cable_m"], errors="coerce") * parallel).sum()) / 1000.0
                               if n else 0.0),
        "tie_overhead_km": _round(float((pd.to_numeric(ties["overhead_m"], errors="coerce") * parallel).sum()) / 1000.0
                                  if n else 0.0),
        "tie_km_mean": _round(float(circuit_lengths.mean())) if n else None,
        "tie_km_max": _round(float(circuit_lengths.max())) if n else None,
        "n_district_pairs": len({(int(a), int(b)) for a, b in zip(ties["from_grid"], ties["to_grid"])}) if n else 0,
        "circuit_km": _round(circuit_km),
        "tie_share_of_circuit_km": _round(tie_km / circuit_km, 6) if circuit_km > 0 else None,
        "n_rings": n_rings,
        "n_rings_with_tie": rings_with_tie,
        "ring_share_with_tie": _round(rings_with_tie / n_rings, 6) if n_rings else None,
        "n_stations": n_stations,
        "km_per_station": _round(circuit_km / n_stations) if n_stations else None,
        "km_per_station_with_ties": _round((circuit_km + tie_km) / n_stations) if n_stations else None,
        "km_per_supply_point": _round(circuit_km / n_supply) if n_supply else None,
        "km_per_supply_point_with_ties": _round((circuit_km + tie_km) / n_supply) if n_supply else None,
        "n_incomplete_district": flags.count("incomplete_district"),
        "n_substation_outside_region": flags.count("substation_outside_region"),
        "tie_max_length_m": float(params.tie_max_length_m),
    }
    # takeover: ends with a flow (converged or not) are checked; ends without one (no net, no exit) are errors
    takeover = takeover if takeover is not None else pd.DataFrame(columns=list(TAKEOVER_COLUMNS))
    checked = takeover[takeover["error"].isna()]
    drops = pd.to_numeric(checked["drop_percent"], errors="coerce")
    loads = pd.to_numeric(checked["loading_percent"], errors="coerce")
    n_checked = int(len(checked))
    n_ok = int(checked["ok"].astype(bool).sum()) if n_checked else 0
    out.update({
        "takeover_checked": n_checked,
        "takeover_ok": n_ok,
        "takeover_share": _round(n_ok / n_checked, 6) if n_checked else None,
        "takeover_not_converged": int((~checked["converged"].astype(bool)).sum()) if n_checked else 0,
        "takeover_errors": int(takeover["error"].notna().sum()),
        "takeover_max_drop_percent": _round(float(drops.max())) if drops.notna().any() else None,
        "takeover_max_loading_percent": _round(float(loads.max())) if loads.notna().any() else None,
    })
    out["runtime_s"] = _round(runtime_s, 2) if runtime_s is not None else None
    return out


def describe_ties(summary: Mapping[str, Any]) -> str:
    """One line of the tie summary for the log."""
    n = int(summary.get("n_ties") or 0)
    if not n:
        return (f"no normally-open ties: no rings of different districts within "
                f"{float(summary.get('tie_max_length_m') or 0) / 1000:g} km")
    text = f"{n} normally-open ties between districts"
    if summary.get("tie_km") is not None:
        text += f": {summary['tie_km']:,.1f} km"
        share = summary.get("tie_share_of_circuit_km")
        if share is not None and summary.get("circuit_km") is not None:
            text += f" ({100 * share:.1f} % of {summary['circuit_km']:,.0f} km MV circuits)"
    if summary.get("n_rings") is not None and summary.get("n_rings_with_tie") is not None:
        text += f", {summary['n_rings_with_tie']} of {summary['n_rings']} rings"
        if summary.get("ring_share_with_tie") is not None:
            text += f" ({100 * summary['ring_share_with_tie']:.0f} %)"
        text += " with a tie"
    if summary.get("km_per_station") is not None and summary.get("km_per_station_with_ties") is not None:
        text += f", km per station {summary['km_per_station']:.2f} -> {summary['km_per_station_with_ties']:.2f}"
    if summary.get("takeover_checked"):
        text += (f"; (n-1) takeover of the half-ring through the tie within the limits at "
                 f"{summary['takeover_ok']} of {summary['takeover_checked']} tie ends")
    return text


# ---------------------------------------------------------------------------------------------- the post-pass
def plan_and_save_ties(dbc: Any, version_id: str, variant_id: str, graph_id: str, params: "MvParameters", *,
                       logger: logging.Logger | None = None, check_takeover: bool = True) -> dict:
    """Plan the ties of a variant and replace its stored ties (one transaction; commits).

    Reads the done districts and their ring members, the routing graph around the candidates (``graph_id``) and,
    for the takeover check, the stored nets of the districts with ties; deletes the variant's old ties, inserts the
    new ones (``mv_line``), sets ``mv_ring.has_tie`` and ``mv_grid.metrics['ties']``.

    Args:
        dbc: A :class:`~pylovo.database.database_client.DatabaseClient` (parent connection of the stage).
        version_id: The LV version.
        variant_id: The MV variant.
        graph_id: The routing graph of the variant.
        params: The MV variant parameters.
        logger: Logger of the stage (default: the module logger).
        check_takeover: Run :func:`takeover_checks` (one flow per tie end).

    Returns:
        The summary of :func:`tie_summary` (for ``mv_variant.summary['ties']``).
    """
    import shapely

    log = logger or _LOGGER
    start = time.time()
    grids = dbc.tie_grids(version_id, variant_id)
    nodes = dbc.tie_candidate_nodes(version_id, variant_id)
    cand = tie_candidates(nodes, grids)
    pairs = close_pairs(cand, grids, float(params.tie_max_length_m) + PREFILTER_SLACK_M)
    ties = empty_ties()
    if len(pairs):
        from pylovo.mv_grid.stage import attachment_points

        sub = cand.iloc[np.unique(pairs)]
        must = sorted({int(e) for e in pd.to_numeric(sub["attach_edge_id"], errors="coerce").dropna()})
        points = shapely.MultiPoint(sub[["x", "y"]].to_numpy(dtype=float))
        edges = dbc.subgraph_edges(graph_id, shapely.to_wkb(points),
                                   float(params.tie_max_length_m) + SUBGRAPH_MARGIN_M, must)
        cache: dict[int, Any] = {}

        def edge_geometry(edge_ids: Sequence[int]) -> dict[int, Any]:
            missing = sorted({int(e) for e in edge_ids} - set(cache))
            if missing:
                cache.update({int(k): v for k, v in dbc.edge_geometries(graph_id, missing).items()})
            return {int(e): cache[int(e)] for e in edge_ids if int(e) in cache}

        cand = cand.copy()
        ax, ay = attachment_points(sub, edge_geometry(must) if must else {})
        cand["ax"], cand["ay"] = np.nan, np.nan
        cand.loc[sub.index, "ax"], cand.loc[sub.index, "ay"] = ax, ay
        ties = plan_ties(cand, grids, edges, params, edge_geometry=edge_geometry)
    log.info(f"Ties: {len(cand)} ring members as candidates, {len(pairs)} pairs of different districts within "
             f"{float(params.tie_max_length_m) + PREFILTER_SLACK_M:g} m, {len(ties)} ties")
    takeover = None
    if len(ties) and check_takeover:
        nets = {}
        for g in sorted({int(x) for x in ties["from_grid"]} | {int(x) for x in ties["to_grid"]}):
            try:
                nets[g] = dbc.read_mv_grid_net(g)
            except LookupError as exc:
                log.warning(f"Tie takeover check of MV grid {g} skipped: {exc}")
        takeover = takeover_checks(ties, nets, params)
        for rec in takeover[takeover["error"].notna()].to_dict("records"):
            log.warning(f"Tie takeover check of node {rec['node']} (MV grid {rec['mv_grid_id']}): {rec['error']}")
    summary = tie_summary(ties, grids, params, takeover, runtime_s=time.time() - start)
    dbc.save_ties(version_id, variant_id, ties, grid_tie_metrics(ties, takeover))
    return summary


def plan_and_save_links(dbc: Any, version_id: str, variant_id: str, graph_id: str, params: "MvParameters", *,
                        logger: logging.Logger | None = None) -> dict:
    """Plan the links inside the districts of a variant and replace its stored links (one transaction; commits).

    Reads the done districts, their ring members (with ``link_stub_ends`` also their stub nodes) and the routing
    graph around them, deletes the variant's old links, inserts the new ones (``mv_line`` with role ``link``) and
    sets ``mv_grid.metrics['links']``.

    Returns:
        The summary of :func:`link_summary` (for ``mv_variant.summary['links']``).
    """
    import shapely

    log = logger or _LOGGER
    start = time.time()
    limit = largest_reach(params)
    grids = dbc.tie_grids(version_id, variant_id)
    nodes = dbc.tie_candidate_nodes(version_id, variant_id)
    cand = tie_candidates(nodes, grids)
    pairs = close_pairs(cand, grids, limit + PREFILTER_SLACK_M, within=True)
    rows = [cand.iloc[np.unique(pairs)]] if len(pairs) else []
    stub_ends, note = None, ""
    if params.link_stub_ends:
        stub_cand = stub_link_candidates(nodes, grids)
        stub_pairs = stub_link_pairs(stub_cand, grids, limit)
        stub_ends = np.asarray(stub_cand.index, dtype=np.int64)[stub_cand["stub_end"].to_numpy(dtype=bool)].tolist()
        if len(stub_pairs):
            rows.append(stub_cand.iloc[np.unique(stub_pairs)])
        note = f", {len(stub_ends)} stub ends with {len(stub_pairs)} pairs to another feeder"
    links = empty_ties()
    if rows:
        from pylovo.mv_grid.stage import attachment_points

        sub = pd.concat(rows) if len(rows) > 1 else rows[0]
        sub = sub[~sub.index.duplicated()]
        must = sorted({int(e) for e in pd.to_numeric(sub["attach_edge_id"], errors="coerce").dropna()})
        points = shapely.MultiPoint(sub[["x", "y"]].to_numpy(dtype=float))
        edges = dbc.subgraph_edges(graph_id, shapely.to_wkb(points), limit + SUBGRAPH_MARGIN_M, must)
        cache: dict[int, Any] = {}

        def edge_geometry(edge_ids: Sequence[int]) -> dict[int, Any]:
            missing = sorted({int(e) for e in edge_ids} - set(cache))
            if missing:
                cache.update({int(k): v for k, v in dbc.edge_geometries(graph_id, missing).items()})
            return {int(e): cache[int(e)] for e in edge_ids if int(e) in cache}

        nodes = nodes.copy()
        ax, ay = attachment_points(sub, edge_geometry(must) if must else {})
        nodes["ax"], nodes["ay"] = np.nan, np.nan
        nodes.loc[sub.index, "ax"], nodes.loc[sub.index, "ay"] = ax, ay
        links = plan_links(nodes, grids, edges, params, edge_geometry=edge_geometry)
    log.info(f"Links: {len(cand)} ring members as candidates, {len(pairs)} pairs of different rings of one "
             f"district within {limit + PREFILTER_SLACK_M:g} m{note}, {len(links)} links")
    summary = link_summary(links, grids, params, runtime_s=time.time() - start, stub_ends=stub_ends)
    dbc.save_ties(version_id, variant_id, links, grid_link_metrics(links), role="link")
    return summary
