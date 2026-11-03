"""In-memory routing on the MV street graph (pure: no database, configuration or plotting imports).

The persistent regional graph (``pylovo.mv_graph_edge``, built by
:class:`~pylovo.database.mv_graph_mixin.MvGraphMixin`) is used in two forms:

* **Region CSR** (:func:`region_csr`) for the supply areas: a graph Voronoi (:func:`graph_voronoi`) assigns every
  vertex to the HV/MV substation nearest along the streets; :mod:`pylovo.mv_grid.districts` places the nodes on it.
  Cells are contiguous along shortest paths by construction.
* **Run graph** of one district (:func:`build_run_graph`): the district subgraph split at the attachment points of
  its terminals, with straight connectors for terminals off the street and straight bridges joining islands to the
  busbar's component. Distances (:func:`terminal_distances`), vertex paths (:func:`vertex_paths`), geometries
  (:func:`path_geometry`) and the trench length (:func:`trench_km`) are computed on it with SciPy.

Conventions: lengths in m; graph vertex and edge ids are int64 and never truncated; unreachable pairs have the
distance ``inf`` (never 0) and every requested terminal is kept; the CSR matrices are undirected and keep the minimum
weight per vertex pair (parallel edges are not summed).
"""
from __future__ import annotations

import dataclasses
from collections import Counter, defaultdict
from typing import Any, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd
import scipy.sparse
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from pylovo.mv_grid.model import EdgeSet, RunGraph

#: Columns of :attr:`RunGraph.pieces <pylovo.mv_grid.model.RunGraph.pieces>`.
PIECE_COLUMNS: tuple[str, ...] = ("piece_id", "u", "v", "length_m", "edge_id", "f0", "f1", "kind", "in_settlement",
                                  "settlement_id", "overhead", "cost_m")
#: Weight surcharge per traced unit in :func:`terminal_path_sums` (small enough not to change a route).
PATH_TRACE: float = 1e-6
#: Split points of one edge closer than this (m along the edge) share one vertex.
SPLIT_MERGE_M: float = 1e-3
#: Margin (m) of the local graph of a bounded search beyond the distance its cost limit reaches
#: (:func:`bounded_searches`).
LOCAL_MARGIN_M: float = 100.0


# ---------------------------------------------------------------------------------------------------------------
# sparse helpers
# ---------------------------------------------------------------------------------------------------------------
def _min_pair_index(u: np.ndarray, v: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Indices of the minimum-weight entry per undirected vertex pair (ties: lowest index); loops are dropped."""
    a = np.minimum(u, v)
    b = np.maximum(u, v)
    idx = np.flatnonzero((a != b) & np.isfinite(w))
    if not len(idx):
        return idx
    order = np.lexsort((idx, w[idx], b[idx], a[idx]))
    sel = idx[order]
    first = np.ones(len(sel), dtype=bool)
    first[1:] = (a[sel][1:] != a[sel][:-1]) | (b[sel][1:] != b[sel][:-1])
    return sel[first]


def _undirected_csr(u: np.ndarray, v: np.ndarray, w: np.ndarray, n: int) -> tuple[scipy.sparse.csr_matrix, np.ndarray]:
    """Upper-triangular CSR (use with ``directed=False``) with the minimum weight per pair, and the chosen indices.

    Zero weights are kept as explicit entries (SciPy treats stored zeros of a sparse matrix as edges).
    """
    chosen = _min_pair_index(u, v, w)
    a = np.minimum(u, v)[chosen]
    b = np.maximum(u, v)[chosen]
    csr = scipy.sparse.csr_matrix((w[chosen].astype(np.float64), (a, b)), shape=(n, n))
    return csr, chosen


# ---------------------------------------------------------------------------------------------------------------
# region graph and supply areas
# ---------------------------------------------------------------------------------------------------------------
def region_csr(source: np.ndarray, target: np.ndarray,
               length_m: np.ndarray) -> tuple[scipy.sparse.csr_matrix, dict[int, int]]:
    """Undirected CSR of a graph given by its edges, with the index of every graph vertex id.

    Args:
        source: Graph vertex id per edge (int64).
        target: Graph vertex id per edge (int64).
        length_m: Length per edge.

    Returns:
        ``(csr, vertex_index)``: the CSR over the sorted distinct vertex ids (minimum length per vertex pair, loops
        dropped) and the mapping from graph vertex id to its row in the CSR.
    """
    source = np.asarray(source, dtype=np.int64)
    target = np.asarray(target, dtype=np.int64)
    length_m = np.asarray(length_m, dtype=np.float64)
    vertices = np.unique(np.concatenate([source, target]))
    u = np.searchsorted(vertices, source)
    v = np.searchsorted(vertices, target)
    csr, _ = _undirected_csr(u, v, length_m, len(vertices))
    return csr, {int(x): i for i, x in enumerate(vertices.tolist())}


def graph_voronoi(csr: Any, site_u: np.ndarray, site_v: np.ndarray, site_cost_u: np.ndarray,
                  site_cost_v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Graph Voronoi: the nearest site of every vertex by street distance.

    Every site ``s`` becomes a dummy vertex joined to both ends of its attachment edge with the costs
    ``site_cost_u[s]`` and ``site_cost_v[s]`` (offset plus the part of the edge up to that end). One multi-source
    Dijkstra (``min_only``) gives every vertex its owner. A vertex on the shortest path from a site to one of its
    vertices belongs to the same site, so the cells are contiguous.

    Args:
        csr: Region CSR (:func:`region_csr`).
        site_u: CSR row of the first end of each site's attachment edge (-1: not attached).
        site_v: CSR row of the second end (-1: not attached).
        site_cost_u: Cost from the site to ``site_u`` (offset + fraction x edge length).
        site_cost_v: Cost from the site to ``site_v`` (offset + (1 - fraction) x edge length).

    Returns:
        ``(dist, site)`` per CSR row: the distance to the owning site (``inf`` if none is reachable) and the site
        index (-1 if none).

    Raises:
        ValueError: If a cost is negative or the arrays differ in length.
    """
    n = csr.shape[0]
    site_u = np.asarray(site_u, dtype=np.int64)
    site_v = np.asarray(site_v, dtype=np.int64)
    cost_u = np.asarray(site_cost_u, dtype=np.float64)
    cost_v = np.asarray(site_cost_v, dtype=np.float64)
    n_sites = len(site_u)
    if not (len(site_v) == len(cost_u) == len(cost_v) == n_sites):
        raise ValueError("site arrays differ in length")
    if (cost_u < 0).any() or (cost_v < 0).any():
        raise ValueError("site costs must be non-negative")
    if n_sites == 0:
        return np.full(n, np.inf), np.full(n, -1, dtype=np.int64)
    coo = csr.tocoo()
    dummy = np.arange(n, n + n_sites, dtype=np.int64)
    rows = [coo.row.astype(np.int64)]
    cols = [coo.col.astype(np.int64)]
    data = [coo.data.astype(np.float64)]
    for ends, cost in ((site_u, cost_u), (site_v, cost_v)):
        ok = ends >= 0
        rows.append(ends[ok])
        cols.append(dummy[ok])
        data.append(cost[ok])
    aug, _ = _undirected_csr(np.concatenate(rows), np.concatenate(cols), np.concatenate(data), n + n_sites)
    dist, _pred, sources = dijkstra(aug, directed=False, indices=dummy, min_only=True, return_predecessors=True)
    site = np.where(sources[:n] >= n, sources[:n] - n, -1).astype(np.int64)
    return dist[:n], site


# ---------------------------------------------------------------------------------------------------------------
# run graph of one district
# ---------------------------------------------------------------------------------------------------------------
def _column(nodes: pd.DataFrame, name: str, default: float) -> np.ndarray:
    if name not in nodes.columns:
        return np.full(len(nodes), default, dtype=np.float64)
    values = pd.to_numeric(nodes[name], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan)
    return np.where(np.isfinite(values), values, default)


def build_run_graph(edges: EdgeSet, nodes: pd.DataFrame, *, end_snap_m: float = 1.0, connector_min_m: float = 0.5,
                    long_connector_m: float = 300.0, bridge_detour: float = 1.3,
                    exit_radius_m: float = 0.0, osm_cost_factor: float = 1.0) -> RunGraph:
    """The routing graph of one district: the subgraph split at the terminals, connectors and island bridges.

    * Every attachment edge is split at the sorted fractions of its terminals; a fraction within ``end_snap_m`` of an
      end snaps to that end vertex. A piece costs ``length_m x (f1 - f0)`` and keeps ``(edge_id, f0, f1)``.
    * A terminal with ``attach_offset_m > connector_min_m`` gets its own vertex at ``(x, y)`` and a straight
      connector of cost ``attach_offset_m`` (x ``bridge_detour`` above ``long_connector_m``). Terminals without an
      attachment edge (or one missing from ``edges``) get an own vertex only.
    * **Busbar exits**: the busbar's vertex gets a straight connector (cost = distance) to every street vertex (graph
      vertex or split point, no other terminal's own vertex) within ``exit_radius_m``, so feeders can leave the
      substation in several directions.
    * **Costs**: a piece on an OSM MV line (``EdgeSet.overhead``) costs ``osm_cost_factor`` times its length, every
      other piece its length (``pieces.cost_m``). Routes and distances use the costs; ``length_m`` stays the true
      length (:func:`terminal_path_sums`).
    * Every component that holds terminals but not the busbar's is joined by a straight bridge between its closest
      vertex pair and the connected part (``cKDTree``, cost straight x ``bridge_detour``), nearest island first,
      until all terminals are connected. Rows with ``connection_hint == 'excluded'`` never cause a bridge.

    Args:
        edges: District subgraph (vertex coordinates in ``vertex_xy``).
        nodes: Rows of :attr:`DistrictInput.nodes` (busbar first): ``x``, ``y``, ``attach_edge_id``,
            ``attach_fraction``, ``attach_offset_m``; optional ``kind``, ``connection_hint`` and the attachment point
            ``ax``, ``ay`` (better coordinates for split vertices of terminals off the street).
        end_snap_m: Snap distance to the edge ends.
        connector_min_m: Offsets above this get a connector.
        long_connector_m: Connectors above this cost ``bridge_detour`` x their length.
        bridge_detour: Detour factor of long connectors and bridges.
        exit_radius_m: Radius of the busbar exits (0: none).
        osm_cost_factor: Cost per m of a piece on an OSM MV line.

    Returns:
        The :class:`~pylovo.mv_grid.model.RunGraph`; ``terminal_vertex`` and ``bridged_rows`` follow the row order
        of ``nodes``.
    """
    vid = np.asarray(edges.vertex_id, dtype=np.int64)
    vxy = np.asarray(edges.vertex_xy, dtype=np.float64).reshape(-1, 2)
    order = np.argsort(vid, kind="stable")
    vid, vxy = vid[order], vxy[order]
    edge_id = np.asarray(edges.edge_id, dtype=np.int64)
    e_src = np.asarray(edges.source, dtype=np.int64)
    e_tgt = np.asarray(edges.target, dtype=np.int64)
    if len(edge_id) and (not np.isin(e_src, vid).all() or not np.isin(e_tgt, vid).all()):
        raise ValueError("every edge end must be listed in EdgeSet.vertex_id")
    es = np.searchsorted(vid, e_src)
    et = np.searchsorted(vid, e_tgt)
    e_len = np.asarray(edges.length_m, dtype=np.float64)
    e_in = np.asarray(edges.in_settlement, dtype=bool)
    e_sid = np.asarray(edges.settlement_id, dtype=np.int64)
    e_oh = (np.zeros(len(edge_id), dtype=bool) if getattr(edges, "overhead", None) is None
            else np.asarray(edges.overhead, dtype=bool))
    edge_pos = {int(e): i for i, e in enumerate(edge_id.tolist())}

    n_rows = len(nodes)
    x = _column(nodes, "x", np.nan)
    y = _column(nodes, "y", np.nan)
    att = _column(nodes, "attach_edge_id", np.nan)
    frac = np.clip(_column(nodes, "attach_fraction", 0.0), 0.0, 1.0)
    off = np.maximum(_column(nodes, "attach_offset_m", 0.0), 0.0)
    ax = _column(nodes, "ax", np.nan)
    ay = _column(nodes, "ay", np.nan)
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        raise ValueError("every node row needs finite x and y")
    hint = nodes["connection_hint"].astype(str).to_numpy() if "connection_hint" in nodes.columns \
        else np.full(n_rows, "")
    active = hint != "excluded"
    busbar_row = 0
    if "kind" in nodes.columns:
        is_busbar = np.flatnonzero(nodes["kind"].astype(str).to_numpy() == "busbar")
        if len(is_busbar):
            busbar_row = int(is_busbar[0])

    extra_xy: list[tuple[float, float]] = []

    def new_vertex(px: float, py: float) -> int:
        extra_xy.append((px, py))
        return len(vid) + len(extra_xy) - 1

    # 1. attachment vertex of every row (end snap or split point)
    attach_vertex = np.full(n_rows, -1, dtype=np.int64)
    row_edge = np.full(n_rows, -1, dtype=np.int64)
    splits: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for r in range(n_rows):
        if not np.isfinite(att[r]) or int(att[r]) not in edge_pos:
            continue
        p = edge_pos[int(att[r])]
        row_edge[r] = p
        along = frac[r] * e_len[p]
        if min(along, e_len[p] - along) <= end_snap_m:
            attach_vertex[r] = es[p] if along <= e_len[p] - along else et[p]
        else:
            splits[p].append((along, r))

    p_u: list[np.ndarray] = []
    p_v: list[np.ndarray] = []
    p_len: list[np.ndarray] = []
    p_edge: list[np.ndarray] = []
    p_f0: list[np.ndarray] = []
    p_f1: list[np.ndarray] = []
    p_kind: list[np.ndarray] = []
    p_in: list[np.ndarray] = []
    p_sid: list[np.ndarray] = []
    p_oh: list[np.ndarray] = []

    def add_pieces(u, v, length, edge, f0, f1, kind, in_s, sid, oh=None):
        p_u.append(np.asarray(u, dtype=np.int64))
        p_v.append(np.asarray(v, dtype=np.int64))
        p_len.append(np.asarray(length, dtype=np.float64))
        p_edge.append(np.asarray(edge, dtype=np.int64))
        p_f0.append(np.asarray(f0, dtype=np.float64))
        p_f1.append(np.asarray(f1, dtype=np.float64))
        p_kind.append(np.asarray(kind, dtype=object))
        p_in.append(np.asarray(in_s, dtype=bool))
        p_sid.append(np.asarray(sid, dtype=np.int64))
        p_oh.append(np.zeros(len(p_u[-1]), dtype=bool) if oh is None else np.asarray(oh, dtype=bool))

    unsplit = np.ones(len(edge_id), dtype=bool)
    for p in sorted(splits):
        unsplit[p] = False
        items = sorted(splits[p])
        length = e_len[p]
        fracs: list[float] = []
        verts: list[int] = []
        for along, r in items:
            if fracs and along - fracs[-1] * length <= SPLIT_MERGE_M:
                attach_vertex[r] = verts[-1]
                continue
            f = along / length
            if np.isfinite(ax[r]) and np.isfinite(ay[r]):
                pxy = (ax[r], ay[r])
            elif off[r] <= connector_min_m:
                pxy = (x[r], y[r])
            else:                                            # chord interpolation (no edge geometry here)
                pxy = tuple(vxy[es[p]] + f * (vxy[et[p]] - vxy[es[p]]))
            verts.append(new_vertex(float(pxy[0]), float(pxy[1])))
            fracs.append(f)
            attach_vertex[r] = verts[-1]
        chain_v = [int(es[p]), *verts, int(et[p])]
        chain_f = [0.0, *fracs, 1.0]
        k = len(chain_v) - 1
        f0 = np.asarray(chain_f[:-1])
        f1 = np.asarray(chain_f[1:])
        add_pieces(chain_v[:-1], chain_v[1:], length * (f1 - f0), np.full(k, edge_id[p]), f0, f1,
                   np.full(k, "street", dtype=object), np.full(k, e_in[p]), np.full(k, e_sid[p]), np.full(k, e_oh[p]))
    idx = np.flatnonzero(unsplit)
    add_pieces(es[idx], et[idx], e_len[idx], edge_id[idx], np.zeros(len(idx)), np.ones(len(idx)),
               np.full(len(idx), "street", dtype=object), e_in[idx], e_sid[idx], e_oh[idx])

    n_street_vertices = len(vid) + len(extra_xy)                 # graph vertices and split points

    # 2. terminal vertices and connectors
    terminal_vertex = np.full(n_rows, -1, dtype=np.int64)
    seen: dict[tuple[float, float, int], int] = {}
    con_u, con_v, con_len, con_in, con_sid = [], [], [], [], []
    for r in range(n_rows):
        base = int(attach_vertex[r])
        if base >= 0 and off[r] <= connector_min_m:
            terminal_vertex[r] = base
            continue
        key = (float(x[r]), float(y[r]), base)
        if key in seen:
            terminal_vertex[r] = seen[key]
            continue
        tv = new_vertex(float(x[r]), float(y[r]))
        seen[key] = tv
        terminal_vertex[r] = tv
        if base >= 0:
            p = row_edge[r]
            con_u.append(tv)
            con_v.append(base)
            con_len.append(off[r] * (bridge_detour if off[r] > long_connector_m else 1.0))
            con_in.append(bool(e_in[p]))
            con_sid.append(int(e_sid[p]))
    if con_u:
        k = len(con_u)
        add_pieces(con_u, con_v, con_len, np.full(k, -1), np.full(k, np.nan), np.full(k, np.nan),
                   np.full(k, "connector", dtype=object), con_in, con_sid)

    n_vertices = len(vid) + len(extra_xy)
    vertex_xy = np.vstack([vxy, np.asarray(extra_xy, dtype=np.float64).reshape(-1, 2)])
    if exit_radius_m > 0 and n_street_vertices:
        bv = int(terminal_vertex[busbar_row])
        near = np.asarray(cKDTree(vertex_xy[:n_street_vertices]).query_ball_point(vertex_xy[bv], exit_radius_m),
                          dtype=np.int64)
        near = np.sort(near[near != bv])
        if len(near):
            k = len(near)
            length = np.maximum(np.hypot(*(vertex_xy[near] - vertex_xy[bv]).T), SPLIT_MERGE_M)
            add_pieces(np.full(k, bv), near, length, np.full(k, -1), np.full(k, np.nan), np.full(k, np.nan),
                       np.full(k, "connector", dtype=object), np.zeros(k, bool), np.full(k, -1))
    u = np.concatenate(p_u)
    v = np.concatenate(p_v)
    w = np.concatenate(p_len)
    w = np.where(np.concatenate(p_oh), w * float(osm_cost_factor), w)

    # 3. bridges: islands with terminals joined to the busbar's component, nearest first
    csr, _ = _undirected_csr(u, v, w, n_vertices)
    _, labels = connected_components(csr, directed=False)
    root = labels[terminal_vertex[busbar_row]]
    bridged_rows = active & (labels[terminal_vertex] != root)
    bridged_rows[busbar_row] = False
    pending = sorted({int(c) for c in labels[terminal_vertex[active]]} - {int(root)})
    bridges: list[tuple[int, int, float]] = []
    if pending:
        members = {c: np.flatnonzero(labels == c) for c in [int(root), *pending]}
        best: dict[int, tuple[float, int, int]] = {}

        def update(target_vertices: np.ndarray, candidates: list[int]) -> None:
            tree = cKDTree(vertex_xy[target_vertices])
            for c in candidates:
                d, j = tree.query(vertex_xy[members[c]])
                k = int(np.argmin(d))
                cand = (float(d[k]), int(members[c][k]), int(target_vertices[j[k]]))
                if c not in best or cand[0] < best[c][0]:
                    best[c] = cand

        update(members[int(root)], pending)
        while pending:
            c = min(pending, key=lambda comp: (best[comp][0], comp))
            d, a, b = best[c]
            bridges.append((a, b, d * bridge_detour))
            pending.remove(c)
            if pending:
                update(members[c], pending)
    if bridges:
        k = len(bridges)
        add_pieces([b[0] for b in bridges], [b[1] for b in bridges], [b[2] for b in bridges], np.full(k, -1),
                   np.full(k, np.nan), np.full(k, np.nan), np.full(k, "bridge", dtype=object), np.zeros(k, bool),
                   np.full(k, -1))
        u = np.concatenate(p_u)
        v = np.concatenate(p_v)
        w = np.where(np.concatenate(p_oh), np.concatenate(p_len) * float(osm_cost_factor), np.concatenate(p_len))

    pieces = pd.DataFrame({
        "piece_id": np.arange(len(u), dtype=np.int64), "u": u, "v": v, "length_m": np.concatenate(p_len),
        "edge_id": np.concatenate(p_edge), "f0": np.concatenate(p_f0), "f1": np.concatenate(p_f1),
        "kind": np.concatenate(p_kind), "in_settlement": np.concatenate(p_in),
        "settlement_id": np.concatenate(p_sid), "overhead": np.concatenate(p_oh), "cost_m": w,
    }, columns=list(PIECE_COLUMNS))
    csr, chosen = _undirected_csr(u, v, w, n_vertices)
    a = np.minimum(u, v)[chosen].tolist()
    b = np.maximum(u, v)[chosen].tolist()
    pair_piece = {(int(i), int(j)): int(p) for i, j, p in zip(a, b, chosen.tolist())}
    return RunGraph(csr=csr, n_vertices=n_vertices, vertex_xy=vertex_xy, terminal_vertex=terminal_vertex,
                    pieces=pieces, pair_piece=pair_piece, n_bridges=len(bridges), bridged_rows=bridged_rows)


# ---------------------------------------------------------------------------------------------------------------
# distances and paths
# ---------------------------------------------------------------------------------------------------------------
def terminal_distances(rg: RunGraph, rows: Sequence[int], *, chunk: int = 64) -> np.ndarray:
    """Street distances between node rows on the run graph.

    One SciPy Dijkstra per chunk of at most ``chunk`` distinct source vertices, each result sliced at once to the
    terminal columns.

    Args:
        rg: Run graph.
        rows: Node rows (indices into :attr:`DistrictInput.nodes`); repeated rows are allowed.
        chunk: Source vertices per Dijkstra call.

    Returns:
        ``(k, k)`` float64 matrix in the order of ``rows``: 0 on the diagonal (and between rows on the same vertex),
        ``inf`` for unreachable pairs. Every requested row is kept.
    """
    rows = np.asarray(rows, dtype=np.int64)
    k = len(rows)
    if k == 0:
        return np.zeros((0, 0))
    verts = rg.terminal_vertex[rows]
    uniq, inverse = np.unique(verts, return_inverse=True)
    block = np.empty((len(uniq), len(uniq)), dtype=np.float64)
    step = max(1, int(chunk))
    for start in range(0, len(uniq), step):
        sources = uniq[start:start + step]
        block[start:start + len(sources)] = dijkstra(rg.csr, directed=False, indices=sources)[:, uniq]
    out = block[np.ix_(inverse, inverse)]
    np.fill_diagonal(out, 0.0)
    return out


def terminal_path_sums(rg: RunGraph, rows: Sequence[int], values: np.ndarray, *, chunk: int = 64) -> np.ndarray:
    """Sum of a per-piece value along the routes of :func:`terminal_distances` between every pair of node rows.

    Every piece weighs its cost plus ``PATH_TRACE`` times its value in a second Dijkstra; the difference of the two
    distances divided by ``PATH_TRACE`` is the sum (to about 1e-3; only routes whose costs tie within millimetres
    can be traced on the other one). Unreachable pairs get ``inf``.

    Args:
        rg: Run graph.
        rows: Node rows.
        values: One non-negative value per piece (e.g. its length, or its length on OSM MV lines).
        chunk: Source vertices per Dijkstra call (see :func:`terminal_distances`).
    """
    rows = np.asarray(rows, dtype=np.int64)
    pieces = rg.pieces
    values = np.asarray(values, dtype=np.float64)
    cost = (pieces["cost_m"] if "cost_m" in pieces.columns else pieces["length_m"]).to_numpy(dtype=np.float64)
    traced, _ = _undirected_csr(pieces["u"].to_numpy(dtype=np.int64), pieces["v"].to_numpy(dtype=np.int64),
                                cost + PATH_TRACE * values, rg.n_vertices)
    plain = terminal_distances(rg, rows, chunk=chunk)
    with np.errstate(invalid="ignore"):
        out = (terminal_distances(dataclasses.replace(rg, csr=traced), rows, chunk=chunk) - plain) / PATH_TRACE
    out = np.where(np.isfinite(plain), np.clip(out, 0.0, None), np.inf)
    np.fill_diagonal(out, 0.0)
    return out


def terminal_overhead(rg: RunGraph, rows: Sequence[int], *, chunk: int = 64) -> np.ndarray:
    """Overhead length (m) of the routes between every pair of node rows: their pieces on OSM MV lines
    (``pieces.overhead``); zero without such pieces, ``inf`` where unreachable."""
    pieces = rg.pieces
    flags = (pieces["overhead"].fillna(False).to_numpy(dtype=bool) if "overhead" in pieces.columns
             else np.zeros(len(pieces), dtype=bool))
    if not flags.any():
        rows = np.asarray(rows, dtype=np.int64)
        out = np.zeros((len(rows), len(rows)))
        out[~np.isfinite(terminal_distances(rg, rows, chunk=chunk))] = np.inf
        return out
    return terminal_path_sums(rg, rows, pieces["length_m"].to_numpy(dtype=np.float64) * flags, chunk=chunk)


def terminal_lengths(rg: RunGraph, rows: Sequence[int], *, chunk: int = 64) -> np.ndarray:
    """True length (m) of the routes between every pair of node rows; the distances themselves when every piece
    costs its length."""
    pieces = rg.pieces
    if "cost_m" not in pieces.columns or np.allclose(pieces["cost_m"], pieces["length_m"]):
        return terminal_distances(rg, rows, chunk=chunk)
    return terminal_path_sums(rg, rows, pieces["length_m"].to_numpy(dtype=np.float64), chunk=chunk)


def _walk(pred_row: np.ndarray, source: int, target: int) -> list[int]:
    path = [target]
    node = target
    while node != source:
        node = int(pred_row[node])
        if node < 0:
            return []
        path.append(node)
    path.reverse()
    return path


def _local_graph(csr: Any, csc: Any, vertices: np.ndarray, inside: np.ndarray, local: np.ndarray
                 ) -> tuple[scipy.sparse.csr_matrix, np.ndarray]:
    """The subgraph of ``csr`` induced by the sorted ``vertices`` (local ids in their order, the entries of every
    row in the order of ``csr``) and, per local vertex, the smallest weight of its edges to a vertex outside
    (``inf`` without one). ``inside`` and ``local`` are scratch arrays of the graph size, ``inside`` all false on
    entry and on return."""
    k = len(vertices)
    inside[vertices] = True
    local[vertices] = np.arange(k, dtype=np.int64)
    out_min = np.full(k, np.inf)

    def entries(matrix: Any) -> tuple[np.ndarray, np.ndarray]:
        starts = matrix.indptr[vertices].astype(np.int64)
        counts = matrix.indptr[vertices + 1].astype(np.int64) - starts
        offsets = np.repeat(starts - np.cumsum(counts) + counts, counts)
        return offsets + np.arange(int(counts.sum()), dtype=np.int64), np.repeat(np.arange(k, dtype=np.int64), counts)

    pos, row = entries(csr)
    keep = inside[csr.indices[pos]]
    np.minimum.at(out_min, row[~keep], csr.data[pos[~keep]])
    pos, row = pos[keep], row[keep]
    sub = scipy.sparse.csr_matrix((csr.data[pos], local[csr.indices[pos]],
                                   np.r_[0, np.cumsum(np.bincount(row, minlength=k))]), shape=(k, k))
    pos, column = entries(csc)                                   # edges stored in the rows of outside vertices
    outside = ~inside[csc.indices[pos]]
    np.minimum.at(out_min, column[outside], csc.data[pos[outside]])
    inside[vertices] = False
    return sub, out_min


def bounded_searches(rg: RunGraph, sources: Sequence[int], limit: float, *, chunk: int = 64,
                     return_predecessors: bool = False
                     ) -> Iterator[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray | None]]:
    """Dijkstra searches from run-graph vertices that stop at the cost ``limit``, each on a local part of the graph.

    SciPy returns a row of all graph vertices per source, so on the graph of a whole variant every search costs the
    size of the graph, however local it is. The sources are therefore grouped by cells of half the search radius
    (``limit`` over the smallest cost per m of the pieces, plus :data:`LOCAL_MARGIN_M`), at most ``chunk`` per group,
    and each group searches the vertices within that radius of its box. A search that could leave its part within
    ``limit`` (a reached vertex plus an edge to a vertex outside within ``limit``) runs again with twice the radius:
    the part then holds every vertex the search reaches and every edge it relaxes, in the order of the whole graph,
    so the distances and predecessors are those of the whole graph.

    Yields:
        ``(batch, vertices, dist, pred)``: source vertices, the sorted run-graph vertices of the part (the columns of
        ``dist`` and ``pred``), the distances (``inf`` beyond ``limit``) and the predecessors as columns of the part
        (``None`` without ``return_predecessors``). Every source comes exactly once.
    """
    pending = np.unique(np.asarray(sources, dtype=np.int64))
    if not len(pending):
        return
    limit = float(limit)
    csr = rg.csr
    n = csr.shape[0]
    xy = np.asarray(rg.vertex_xy, dtype=np.float64).reshape(-1, 2)
    pieces = rg.pieces
    length = pieces["length_m"].to_numpy(dtype=np.float64)
    cost = (pieces["cost_m"] if "cost_m" in pieces.columns else pieces["length_m"]).to_numpy(dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = cost[length > 0] / length[length > 0]
    ratio = float(np.min(ratio)) if len(ratio) else 1.0
    radius = limit / ratio + LOCAL_MARGIN_M if ratio > 0 else np.inf
    span = float(np.max(np.ptp(xy, axis=0))) if n else 0.0
    by_x = np.argsort(xy[:, 0], kind="stable")
    xs = xy[by_x, 0]
    csc = csr.tocsc()
    inside = np.zeros(n, dtype=bool)
    local = np.zeros(n, dtype=np.int64)
    step = max(1, int(chunk))
    while len(pending):
        if not radius < span:                                    # the part would be the whole graph
            vertices = np.arange(n, dtype=np.int64)
            for start in range(0, len(pending), step):
                batch = pending[start:start + step]
                result = dijkstra(csr, directed=False, indices=batch, limit=limit,
                                  return_predecessors=return_predecessors)
                dist, pred = result if return_predecessors else (result, None)
                yield batch, vertices, np.atleast_2d(dist), None if pred is None else np.atleast_2d(pred)
            return
        cell = np.floor((xy[pending] - xy[pending].min(axis=0)) / (radius / 2.0)).astype(np.int64)
        order = np.lexsort((pending, cell[:, 1], cell[:, 0]))
        pending, cell = pending[order], cell[order]
        new_cell = np.r_[True, (cell[1:] != cell[:-1]).any(axis=1)]
        group = np.cumsum(new_cell) - 1
        rank = np.arange(len(pending)) - np.flatnonzero(new_cell)[group]
        cuts = np.flatnonzero(new_cell | (rank % step == 0))
        failed = []
        for batch in np.split(pending, cuts[1:]):
            low = xy[batch].min(axis=0) - radius
            high = xy[batch].max(axis=0) + radius
            strip = by_x[np.searchsorted(xs, low[0], side="left"):np.searchsorted(xs, high[0], side="right")]
            y = xy[strip, 1]
            vertices = np.sort(strip[(y >= low[1]) & (y <= high[1])])
            sub, out_min = _local_graph(csr, csc, vertices, inside, local)
            result = dijkstra(sub, directed=False, indices=np.searchsorted(vertices, batch), limit=limit,
                              return_predecessors=return_predecessors)
            dist, pred = result if return_predecessors else (result, None)
            dist = np.atleast_2d(dist)
            leaves = (dist + out_min <= limit).any(axis=1)
            if not leaves.all():
                keep = ~leaves
                yield batch[keep], vertices, dist[keep], None if pred is None else np.atleast_2d(pred)[keep]
            failed.append(batch[leaves])
        pending = np.sort(np.concatenate(failed))
        radius *= 2.0


def _columns(vertices: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Columns of ``targets`` in the sorted ``vertices`` of a part (-1 where a target is not in the part)."""
    targets = np.asarray(targets, dtype=np.int64)
    col = np.searchsorted(vertices, targets)
    found = col < len(vertices)
    found[found] = vertices[col[found]] == targets[found]
    return np.where(found, col, -1)


def bounded_distances(rg: RunGraph, pairs: Sequence[tuple[int, int]], limit: float, *,
                      chunk: int = 64) -> np.ndarray:
    """Cost of the shortest route per vertex pair ``(a, b)`` within ``limit`` (``inf`` beyond it or unreachable), by
    :func:`bounded_searches`."""
    pairs = np.asarray(pairs, dtype=np.int64).reshape(-1, 2)
    out = np.full(len(pairs), np.inf)
    by_source: dict[int, list[int]] = defaultdict(list)
    for p, a in enumerate(pairs[:, 0].tolist()):
        by_source[a].append(p)
    for batch, vertices, dist, _ in bounded_searches(rg, list(by_source), limit, chunk=chunk):
        for i, a in enumerate(batch.tolist()):
            ps = np.asarray(by_source[a], dtype=np.int64)
            col = _columns(vertices, pairs[ps, 1])
            out[ps[col >= 0]] = dist[i, col[col >= 0]]
    return out


def vertex_paths(rg: RunGraph, pairs: Sequence[tuple[int, int]], *,
                 chunk: int = 64, limit: float | None = None) -> dict[tuple[int, int], list[int]]:
    """Shortest vertex paths between run-graph vertices.

    Predecessors are computed only for the distinct sources of ``pairs``, at most ``chunk`` per Dijkstra call. With
    ``limit`` the search of a source stops at that cost (the ties and links know that their routes are not
    longer) and runs on a local part of the graph (:func:`bounded_searches`), which keeps it local in the graph of a
    whole variant.

    Returns:
        ``{(a, b): [a, ..., b]}``; ``[a]`` for ``a == b`` and ``[]`` when ``b`` is unreachable from ``a`` (or costs
        more than ``limit``).
    """
    by_source: dict[int, list[int]] = defaultdict(list)
    for a, b in pairs:
        by_source[int(a)].append(int(b))
    out: dict[tuple[int, int], list[int]] = {}
    sources = sorted(by_source)
    if limit is not None:
        for batch, vertices, dist, pred in bounded_searches(rg, sources, limit, chunk=chunk, return_predecessors=True):
            for i, a in enumerate(batch.tolist()):
                start = int(_columns(vertices, [a])[0])
                targets = by_source[a]
                for b, col in zip(targets, _columns(vertices, targets).tolist()):
                    if a == b:
                        out[(a, b)] = [a]
                    elif col < 0 or not np.isfinite(dist[i, col]):
                        out[(a, b)] = []
                    else:
                        out[(a, b)] = vertices[_walk(pred[i], start, col)].tolist()
        return out
    step = max(1, int(chunk))
    for start in range(0, len(sources), step):
        batch = sources[start:start + step]
        dist, pred = dijkstra(rg.csr, directed=False, indices=batch, return_predecessors=True)
        for i, a in enumerate(batch):
            for b in by_source[a]:
                if a == b:
                    out[(a, b)] = [a]
                elif not np.isfinite(dist[i, b]):
                    out[(a, b)] = []
                else:
                    out[(a, b)] = _walk(pred[i], a, b)
    return out


def path_pieces(rg: RunGraph, vertex_path: Sequence[int]) -> list[int]:
    """Run-graph piece ids along a vertex path (the minimum-weight piece of every consecutive vertex pair)."""
    path = [int(p) for p in vertex_path]
    return [rg.pair_piece[(min(a, b), max(a, b))] for a, b in zip(path[:-1], path[1:])]


def path_geometry(rg: RunGraph, pieces: Sequence[int], edge_geoms: Mapping[int, Any], *,
                  start_vertex: int | None = None) -> Any:
    """One continuous LineString along a list of pieces, oriented along the traversal.

    Street pieces are ``shapely.ops.substring(edge_geom, f0, f1, normalized=True)`` (reversed when traversed from
    ``v`` to ``u``); connectors and bridges are straight, their ends moved onto the neighbouring street piece, so the
    line has no gaps. Street pieces meet exactly because the graph build snapped the edge ends to their vertices.

    Args:
        rg: Run graph.
        pieces: Piece ids in path order (:func:`path_pieces`).
        edge_geoms: Graph edge id -> shapely LineString (source -> target), for the street pieces.
        start_vertex: First vertex of the path; needed to orient a single piece and for an empty path.

    Returns:
        shapely LineString in the coordinates of the edge geometries (``TARGET_EPSG``); a two-point line of zero
        length for an empty path with a ``start_vertex``.

    Raises:
        ValueError: If the pieces do not form a path or the path is empty without ``start_vertex``.
    """
    from shapely import LineString
    from shapely.ops import substring

    pieces = [int(p) for p in pieces]
    if not pieces:
        if start_vertex is None:
            raise ValueError("an empty path needs start_vertex")
        xy = rg.vertex_xy[int(start_vertex)]
        return LineString([xy, xy])
    table = rg.pieces
    pu = table["u"].to_numpy()
    pv = table["v"].to_numpy()
    kinds = table["kind"].to_numpy()
    edge_ids = table["edge_id"].to_numpy()
    f0s = table["f0"].to_numpy()
    f1s = table["f1"].to_numpy()
    if start_vertex is None:
        first = pieces[0]
        current = int(pu[first])
        if len(pieces) > 1:
            following = (int(pu[pieces[1]]), int(pv[pieces[1]]))
            if int(pv[first]) not in following and int(pu[first]) in following:
                current = int(pv[first])          # the first piece is traversed from v to u
    else:
        current = int(start_vertex)
    segments: list[list[Any]] = []
    for p in pieces:
        if pu[p] == current:
            forward, current = True, int(pv[p])
        elif pv[p] == current:
            forward, current = False, int(pu[p])
        else:
            raise ValueError(f"piece {p} does not continue the path at vertex {current}")
        if kinds[p] == "street":
            part = substring(edge_geoms[int(edge_ids[p])], float(f0s[p]), float(f1s[p]), normalized=True)
            coords = np.array(part.coords, dtype=np.float64)[:, :2].copy()
            if len(coords) == 1:
                coords = np.vstack([coords, coords])
            straight = False
        else:
            coords = np.vstack([rg.vertex_xy[pu[p]], rg.vertex_xy[pv[p]]]).astype(np.float64)
            straight = True
        if not forward:
            coords = coords[::-1].copy()
        segments.append([coords, straight])
    for i in range(1, len(segments)):
        prev, cur = segments[i - 1], segments[i]
        if cur[1]:
            cur[0][0] = prev[0][-1]
        elif prev[1]:
            prev[0][-1] = cur[0][0]
    coords = np.vstack([segments[0][0]] + [s[0][1:] for s in segments[1:]])
    keep = np.ones(len(coords), dtype=bool)
    keep[1:] = np.any(coords[1:] != coords[:-1], axis=1)
    coords = coords[keep]
    if len(coords) == 1:
        coords = np.vstack([coords, coords])
    return LineString(coords)


def trench_km(rg: RunGraph, piece_lists: Sequence[Sequence[int]]) -> tuple[float, int]:
    """Trench length of a set of circuits and the most circuits sharing one trench.

    The run-graph pieces partition every graph edge, so the union of the covered fraction intervals of an edge is the
    sum of its distinct pieces. Connectors and bridges are straight trenches and count once as well.

    Args:
        rg: Run graph.
        piece_lists: Piece ids of each circuit (one list per cable line; parallel cables of one circuit are one list).

    Returns:
        ``(km, max_circuits)``: trench km and the largest number of circuits on one piece (0 without pieces).
    """
    counts: Counter[int] = Counter()
    for pieces in piece_lists:
        counts.update({int(p) for p in pieces})
    if not counts:
        return 0.0, 0
    lengths = rg.pieces["length_m"].to_numpy()
    km = float(lengths[np.fromiter(counts.keys(), dtype=np.int64)].sum()) / 1000.0
    return km, int(max(counts.values()))
