"""MV routing graph: region CSR, weighted Voronoi, run graph, distances, paths, geometry, trench, mixin SQL."""

import dataclasses
import heapq
import logging
import re

import numpy as np
import pandas as pd
import pytest
from psycopg2 import sql
from shapely import LineString, Point

import pylovo.database.mv_graph_mixin as mixin_module
from pylovo.database.mv_graph_mixin import MvGraphMixin, build_parameters, edges_sql, graph_id_for
from pylovo.mv_grid import graph
from pylovo.mv_grid.model import NODE_COLUMNS, EdgeSet
from pylovo.mv_grid.parameters import MvParameters


# ----------------------------------------------------------------------------------------------------- helpers
def _dijkstra(adj: dict, start) -> dict:
    dist, queue = {start: 0.0}, [(0.0, start)]
    while queue:
        d, node = heapq.heappop(queue)
        if d > dist[node]:
            continue
        for nxt, w in adj.get(node, []):
            if d + w < dist.get(nxt, np.inf):
                dist[nxt] = d + w
                heapq.heappush(queue, (d + w, nxt))
    return dist


def _adjacency(edges):
    adj = {}
    for s, t, w in edges:
        if s != t:
            adj.setdefault(s, []).append((t, w))
            adj.setdefault(t, []).append((s, w))
    return adj


def _edge_set(edges, coords, settlement=None):
    """EdgeSet from [(edge_id, source, target)] with lengths = straight distance between the vertex coordinates."""
    ids = np.asarray([e[0] for e in edges], dtype=np.int64)
    src = np.asarray([e[1] for e in edges], dtype=np.int64)
    tgt = np.asarray([e[2] for e in edges], dtype=np.int64)
    length = np.asarray([e[3] if len(e) > 3 else float(np.hypot(*np.subtract(coords[e[1]], coords[e[2]])))
                         for e in edges], dtype=np.float64)
    vids = np.asarray(sorted(coords), dtype=np.int64)
    return EdgeSet(edge_id=ids, source=src, target=tgt, length_m=length,
                   in_settlement=np.zeros(len(ids), bool) if settlement is None else np.asarray(settlement),
                   settlement_id=np.full(len(ids), -1, dtype=np.int64), vertex_id=vids,
                   vertex_xy=np.asarray([coords[v] for v in vids], dtype=np.float64))


def _nodes(rows):
    """Node table from dicts with x, y, attach_edge_id, attach_fraction, attach_offset_m (+ optional kind/hint)."""
    frame = pd.DataFrame(rows)
    for column in NODE_COLUMNS:
        if column not in frame.columns:
            frame[column] = np.nan
    frame["kind"] = [r.get("kind", "station") for r in rows]
    frame["connection_hint"] = [r.get("connection_hint", "ring") for r in rows]
    frame.index = pd.RangeIndex(1, len(rows) + 1, name="mv_node_id")
    return frame


def _line_geoms(edge_set, coords, bend=0.0):
    """Edge geometries source -> target; with ``bend`` a polyline through a shifted midpoint (length recomputed)."""
    geoms = {}
    for eid, s, t in zip(edge_set.edge_id, edge_set.source, edge_set.target):
        a, b = np.asarray(coords[int(s)], float), np.asarray(coords[int(t)], float)
        if bend:
            mid = (a + b) / 2 + np.array([-(b - a)[1], (b - a)[0]]) / np.hypot(*(b - a)) * bend
            geoms[int(eid)] = LineString([a, mid, b])
        else:
            geoms[int(eid)] = LineString([a, b])
    return geoms


# ----------------------------------------------------------------------------------------------------- region
def test_region_csr_keeps_minimum_of_parallel_edges_and_drops_loops():
    csr, index = graph.region_csr(np.array([10, 10, 20, 30, 30]), np.array([20, 20, 30, 30, 40]),
                                  np.array([5.0, 3.0, 4.0, 1.0, 0.0]))
    assert index == {10: 0, 20: 1, 30: 2, 40: 3}
    dense = csr.toarray()
    assert dense[0, 1] == 3.0 and dense[1, 2] == 4.0
    assert dense[2, 2] == 0.0                                   # loop dropped
    from scipy.sparse.csgraph import dijkstra
    d = dijkstra(csr, directed=False, indices=0)
    np.testing.assert_allclose(d, [0.0, 3.0, 7.0, 7.0])        # zero-length edge 30-40 is an edge (explicit zero)


def _random_graph(rng, n=40, extra=60):
    edges = set()
    for v in range(1, n):
        edges.add((int(rng.integers(0, v)), v))
    while len(edges) < n - 1 + extra:
        a, b = sorted(int(x) for x in rng.integers(0, n, 2))
        if a != b:
            edges.add((a, b))
    edges = sorted(edges)
    return edges, rng.uniform(10.0, 200.0, len(edges))


def test_graph_voronoi_matches_brute_force_and_is_contiguous():
    rng = np.random.default_rng(7)
    edges, length = _random_graph(rng)
    source = np.array([e[0] for e in edges]) * 10 + 100        # graph vertex ids differ from the CSR rows
    target = np.array([e[1] for e in edges]) * 10 + 100
    csr, index = graph.region_csr(source, target, length)
    adj = _adjacency([(index[int(s)], index[int(t)], w) for s, t, w in zip(source, target, length)])
    n_sites = 4
    site_edges = rng.choice(len(edges), n_sites, replace=False)
    f = rng.uniform(0.1, 0.9, n_sites)
    o = rng.uniform(0.0, 50.0, n_sites)
    su = np.array([index[int(source[e])] for e in site_edges])
    sv = np.array([index[int(target[e])] for e in site_edges])
    cu = o + f * length[site_edges]
    cv = o + (1 - f) * length[site_edges]
    dist, site = graph.graph_voronoi(csr, su, sv, cu, cv)
    ref = np.full((n_sites, csr.shape[0]), np.inf)
    for s in range(n_sites):
        du, dv = _dijkstra(adj, su[s]), _dijkstra(adj, sv[s])
        for x in range(csr.shape[0]):
            ref[s, x] = min(cu[s] + du.get(x, np.inf), cv[s] + dv.get(x, np.inf))
    np.testing.assert_allclose(dist, ref.min(axis=0), rtol=1e-12)
    np.testing.assert_array_equal(site, ref.argmin(axis=0))
    # contiguity: the vertices of every cell are connected through vertices of the same cell
    for s in range(n_sites):
        owned = set(np.flatnonzero(site == s).tolist())
        starts = [v for v in (su[s], sv[s]) if v in owned]
        reached, stack = set(starts), list(starts)
        while stack:
            node = stack.pop()
            for nxt, _ in adj.get(node, []):
                if nxt in owned and nxt not in reached:
                    reached.add(nxt)
                    stack.append(nxt)
        assert reached == owned


def test_graph_voronoi_without_sites_and_negative_costs():
    csr, _ = graph.region_csr(np.array([1]), np.array([2]), np.array([5.0]))
    dist, site = graph.graph_voronoi(csr, np.zeros(0), np.zeros(0), np.zeros(0), np.zeros(0))
    assert np.isinf(dist).all() and (site == -1).all()
    with pytest.raises(ValueError, match="non-negative"):
        graph.graph_voronoi(csr, np.array([0]), np.array([1]), np.array([-1.0]), np.array([1.0]))


# ----------------------------------------------------------------------------------------------------- run graph
LINE = {1: (0.0, 0.0), 2: (100.0, 0.0), 3: (200.0, 0.0)}


def test_run_graph_splits_an_edge_at_two_terminals():
    edges = _edge_set([(11, 1, 2), (12, 2, 3)], LINE)
    nodes = _nodes([
        {"kind": "busbar", "connection_hint": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11,
         "attach_fraction": 0.0, "attach_offset_m": 0.0},
        {"x": 30.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.3, "attach_offset_m": 0.0},
        {"x": 60.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.6, "attach_offset_m": 0.0},
    ])
    rg = graph.build_run_graph(edges, nodes)
    street = rg.pieces[rg.pieces.edge_id == 11].sort_values("f0")
    np.testing.assert_allclose(street.length_m, [30.0, 30.0, 40.0])
    np.testing.assert_allclose(street.f0, [0.0, 0.3, 0.6])
    np.testing.assert_allclose(street.f1, [0.3, 0.6, 1.0])
    assert rg.terminal_vertex[0] == 0                            # busbar snapped to vertex 1 (row 0 of the CSR)
    assert rg.terminal_vertex[1] >= 3 and rg.terminal_vertex[2] >= 3
    np.testing.assert_allclose(rg.vertex_xy[rg.terminal_vertex[1]], [30.0, 0.0])
    d = graph.terminal_distances(rg, [0, 1, 2])
    np.testing.assert_allclose(d, [[0, 30, 60], [30, 0, 30], [60, 30, 0]])
    assert rg.n_bridges == 0 and not rg.bridged_rows.any()
    assert len(rg.pieces) == 4 and set(rg.pieces.kind) == {"street"}


def test_end_snapping_and_shared_split_points():
    edges = _edge_set([(11, 1, 2), (12, 2, 3)], LINE)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.4, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.004,
         "attach_offset_m": 0.0},
        {"x": 99.2, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.992, "attach_offset_m": 0.0},
        {"x": 150.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 0.5, "attach_offset_m": 0.0},
        {"x": 150.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 0.5, "attach_offset_m": 0.0},
    ])
    rg = graph.build_run_graph(edges, nodes)
    assert rg.terminal_vertex[0] == 0 and rg.terminal_vertex[1] == 1     # within 1 m of the ends: snapped
    assert rg.terminal_vertex[2] == rg.terminal_vertex[3]               # one split point for both
    assert len(rg.pieces) == 3
    d = graph.terminal_distances(rg, [0, 1, 2, 3])
    np.testing.assert_allclose(d[0], [0, 100, 150, 150])
    assert d[2, 3] == 0.0


def test_connectors_and_long_connectors():
    edges = _edge_set([(11, 1, 2), (12, 2, 3)], LINE)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": -20.0, "attach_edge_id": 11, "attach_fraction": 0.0,
         "attach_offset_m": 20.0},
        {"x": 50.0, "y": 0.3, "attach_edge_id": 11, "attach_fraction": 0.5, "attach_offset_m": 0.3},
        {"x": 150.0, "y": 400.0, "attach_edge_id": 12, "attach_fraction": 0.5, "attach_offset_m": 400.0},
    ])
    rg = graph.build_run_graph(edges, nodes, long_connector_m=300.0, bridge_detour=1.3)
    connectors = rg.pieces[rg.pieces.kind == "connector"].sort_values("length_m")
    np.testing.assert_allclose(connectors.length_m, [20.0, 520.0])
    assert (connectors.edge_id == -1).all()
    np.testing.assert_allclose(rg.vertex_xy[rg.terminal_vertex[0]], [0.0, -20.0])
    np.testing.assert_allclose(rg.vertex_xy[rg.terminal_vertex[2]], [150.0, 400.0])
    assert rg.terminal_vertex[1] not in rg.pieces.loc[rg.pieces.kind == "connector", "u"].to_numpy()  # 0.3 m: none
    d = graph.terminal_distances(rg, [0, 1, 2])
    np.testing.assert_allclose(d[0], [0.0, 70.0, 20.0 + 150.0 + 520.0])


def test_busbar_exits_join_the_street_vertices_within_the_radius():
    coords = {1: (0.0, 0.0), 3: (120.0, 0.0), 4: (0.0, 1000.0), 5: (1000.0, 0.0), 6: (150.0, 120.0),
              7: (1000.0, 1000.0)}
    edges = _edge_set([(11, 1, 4), (12, 3, 5), (13, 6, 4), (14, 4, 7), (15, 7, 5)], coords)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": -50.0, "attach_edge_id": 11, "attach_fraction": 0.0,
         "attach_offset_m": 50.0},
        {"x": 1000.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0},
        {"x": 100.0, "y": -60.0, "attach_edge_id": 12, "attach_fraction": 0.0, "attach_offset_m": 60.0},
    ])
    plain = graph.build_run_graph(edges, nodes)
    rg = graph.build_run_graph(edges, nodes, exit_radius_m=200.0)
    assert plain.n_bridges == rg.n_bridges == 0
    bv = rg.terminal_vertex[0]
    exits = rg.pieces.iloc[len(plain.pieces):]                       # added after the terminal connectors
    assert (exits.kind == "connector").all() and (exits.u == bv).all() and (exits.edge_id == -1).all()
    reached = sorted((float(x), float(y)) for x, y in rg.vertex_xy[exits.v.to_numpy()])
    # the street vertices within 200 m of (0, -50): (0, 0) and (120, 0); (150, 120) is 228 m away, and the own
    # vertex of the off-street station at (100, -60) is no street vertex
    assert reached == [(0.0, 0.0), (120.0, 0.0)]
    np.testing.assert_allclose(sorted(exits.length_m), [50.0, np.hypot(120.0, 50.0)])
    assert graph.terminal_distances(plain, [0, 1])[0, 1] == pytest.approx(3050.0)     # round the block
    assert graph.terminal_distances(rg, [0, 1])[0, 1] == pytest.approx(np.hypot(120.0, 50.0) + 880.0)
    assert len(graph.build_run_graph(edges, nodes, exit_radius_m=0.0).pieces) == len(plain.pieces)


def test_osm_pieces_cost_less_and_the_routes_keep_their_true_lengths():
    # two routes from A (0, 0) to B (1000, 0): the street straight (1,000 m) and an OSM line over (500, 400)
    coords = {1: (0.0, 0.0), 2: (1000.0, 0.0), 3: (500.0, 400.0)}
    edges = _edge_set([(11, 1, 2), (12, 1, 3), (13, 3, 2)], coords)
    edges = EdgeSet(**{**edges.__dict__, "overhead": np.array([False, True, True])})
    nodes = _nodes([{"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0,
                     "attach_offset_m": 0.0},
                    {"x": 1000.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 1.0, "attach_offset_m": 0.0}])
    osm_m = 2 * np.hypot(500.0, 400.0)                                   # 1,280.6 m on the OSM line
    plain = graph.build_run_graph(edges, nodes)
    assert graph.terminal_distances(plain, [0, 1])[0, 1] == pytest.approx(1000.0)
    assert graph.terminal_overhead(plain, [0, 1])[0, 1] == pytest.approx(0.0, abs=1e-3)
    rg = graph.build_run_graph(edges, nodes, osm_cost_factor=0.5)
    np.testing.assert_allclose(rg.pieces.cost_m, np.where(rg.pieces.overhead, 0.5, 1.0) * rg.pieces.length_m)
    assert graph.terminal_distances(rg, [0, 1])[0, 1] == pytest.approx(0.5 * osm_m)          # the cost
    assert graph.terminal_lengths(rg, [0, 1])[0, 1] == pytest.approx(osm_m, abs=1e-2)        # the true length
    assert graph.terminal_overhead(rg, [0, 1])[0, 1] == pytest.approx(osm_m, abs=1e-2)
    np.testing.assert_allclose(graph.terminal_lengths(plain, [0, 1]), graph.terminal_distances(plain, [0, 1]))


def test_island_bridging_connects_every_terminal_and_flags_the_rows():
    coords = {1: (0.0, 0.0), 2: (100.0, 0.0), 3: (100.0, 500.0), 4: (300.0, 500.0), 5: (900.0, 900.0),
              6: (950.0, 900.0)}
    edges = _edge_set([(11, 1, 2), (12, 3, 4), (13, 5, 6)], coords)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0, "attach_offset_m": 0},
        {"x": 300.0, "y": 500.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0},
        {"x": 120.0, "y": 80.0},                                            # no attachment edge: own vertex
        {"x": 950.0, "y": 900.0, "attach_edge_id": 13, "attach_fraction": 1.0, "attach_offset_m": 0.0,
         "connection_hint": "excluded"},
    ])
    rg = graph.build_run_graph(edges, nodes, bridge_detour=1.3)
    assert rg.n_bridges == 2
    np.testing.assert_array_equal(rg.bridged_rows, [False, True, True, False])
    bridges = rg.pieces[rg.pieces.kind == "bridge"]
    assert (bridges.edge_id == -1).all() and not bridges.in_settlement.any()
    d = graph.terminal_distances(rg, [0, 1, 2, 3])
    # the free point (120, 80) is bridged first (nearest to vertex 2), the island 3-4 then to that point
    first = np.hypot(20.0, 80.0) * 1.3
    second = np.hypot(20.0, 420.0) * 1.3
    assert d[0, 2] == pytest.approx(100.0 + first)
    assert d[0, 1] == pytest.approx(100.0 + first + second + 200.0)
    assert np.isinf(d[0, 3]) and np.isinf(d[3, 1])                   # excluded island: no bridge, inf (never 0)
    assert d[3, 3] == 0.0


def test_distances_keep_every_terminal_and_use_inf():
    coords = {1: (0.0, 0.0), 2: (10.0, 0.0), 3: (50.0, 0.0), 4: (60.0, 0.0)}
    edges = _edge_set([(11, 1, 2), (12, 3, 4)], coords)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0, "attach_offset_m": 0},
        {"x": 60.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0,
         "connection_hint": "excluded"},
    ])
    rg = graph.build_run_graph(edges, nodes)
    d = graph.terminal_distances(rg, [0, 1, 1, 0], chunk=1)
    assert d.shape == (4, 4) and d.dtype == np.float64
    assert np.isinf(d[0, 1]) and np.isinf(d[1, 3]) and d[1, 2] == 0.0 and d[0, 3] == 0.0
    assert graph.terminal_distances(rg, []).shape == (0, 0)


def _row_paths(rg, pairs):
    """Vertex paths between node rows (their terminal vertices) with graph.vertex_paths."""
    vertex = {(i, j): (int(rg.terminal_vertex[i]), int(rg.terminal_vertex[j])) for i, j in pairs}
    paths = graph.vertex_paths(rg, list(set(vertex.values())))
    return {pair: list(paths[vertex[pair]]) for pair in pairs}


def test_parallel_edges_keep_the_minimum_piece():
    coords = {1: (0.0, 0.0), 2: (30.0, 0.0)}
    edges = _edge_set([(11, 1, 2, 50.0), (12, 1, 2, 30.0), (13, 2, 1, 40.0)], coords)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0, "attach_offset_m": 0},
        {"x": 30.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0},
    ])
    rg = graph.build_run_graph(edges, nodes)
    assert graph.terminal_distances(rg, [0, 1])[0, 1] == 30.0
    piece = rg.pair_piece[(0, 1)]
    assert rg.pieces.loc[piece, "edge_id"] == 12
    assert graph.path_pieces(rg, _row_paths(rg, [(0, 1)])[(0, 1)]) == [piece]


def _random_district(rng, n_vertices=30, n_terminals=12):
    coords = {}
    for v in range(n_vertices):
        coords[1000 + v] = tuple(rng.uniform(0, 2000, 2))
    rng_edges, _ = _random_graph(rng, n=n_vertices, extra=25)
    edges = [(500 + i, 1000 + a, 1000 + b) for i, (a, b) in enumerate(rng_edges)]
    es = _edge_set(edges, coords)
    rows = []
    for k in range(n_terminals):
        e = int(rng.integers(0, len(edges)))
        f = float(rng.uniform(0.1, 0.9))
        o = float(rng.choice([0.0, 0.2, 15.0, 120.0]))
        a = np.asarray(coords[edges[e][1]])
        b = np.asarray(coords[edges[e][2]])
        rows.append({"kind": "busbar" if k == 0 else "station", "x": float((a + f * (b - a))[0]),
                     "y": float((a + f * (b - a))[1] + o), "attach_edge_id": edges[e][0], "attach_fraction": f,
                     "attach_offset_m": o})
    return coords, edges, es, _nodes(rows)


def test_distances_equal_a_reference_dijkstra():
    rng = np.random.default_rng(3)
    coords, edges, es, nodes = _random_district(rng)
    rg = graph.build_run_graph(es, nodes, end_snap_m=1.0, connector_min_m=0.5)
    d = graph.terminal_distances(rg, list(range(len(nodes))), chunk=3)
    length = dict(zip(es.edge_id.tolist(), es.length_m.tolist()))
    ends = {e: (s, t) for e, s, t in edges}
    adj = _adjacency([(s, t, length[e]) for e, s, t in edges])
    tables = {v: _dijkstra(adj, v) for v in coords}
    for i, ri in enumerate(nodes.itertuples()):
        for j, rj in enumerate(nodes.itertuples()):
            if i == j:
                continue
            oi = ri.attach_offset_m if ri.attach_offset_m > 0.5 else 0.0
            oj = rj.attach_offset_m if rj.attach_offset_m > 0.5 else 0.0
            ei, ej = int(ri.attach_edge_id), int(rj.attach_edge_id)
            li, lj = length[ei], length[ej]
            options = []
            for vi, ci in ((ends[ei][0], ri.attach_fraction * li), (ends[ei][1], (1 - ri.attach_fraction) * li)):
                for vj, cj in ((ends[ej][0], rj.attach_fraction * lj), (ends[ej][1], (1 - rj.attach_fraction) * lj)):
                    options.append(ci + tables[vi].get(vj, np.inf) + cj)
            if ei == ej:
                options.append(abs(ri.attach_fraction - rj.attach_fraction) * li)
            same_point = (ri.x, ri.y) == (rj.x, rj.y) and ei == ej
            expected = 0.0 if same_point else min(options) + oi + oj
            assert d[i, j] == pytest.approx(expected, rel=1e-9, abs=1e-6), (i, j)


def test_path_geometry_is_continuous_and_its_length_equals_the_distance():
    rng = np.random.default_rng(11)
    coords, edges, es, nodes = _random_district(rng, n_terminals=8)
    geoms = _line_geoms(es, coords, bend=40.0)
    es.length_m = np.asarray([geoms[int(e)].length for e in es.edge_id])
    # fractions along the bent geometry: recompute attachment points on the real geometry
    for idx, row in nodes.iterrows():
        g = geoms[int(row.attach_edge_id)]
        p = g.interpolate(row.attach_fraction, normalized=True)
        nodes.loc[idx, ["x", "y"]] = [p.x, p.y + row.attach_offset_m]
        nodes.loc[idx, "attach_offset_m"] = Point(p.x, p.y + row.attach_offset_m).distance(g)
        nodes.loc[idx, "attach_fraction"] = g.project(Point(p.x, p.y + row.attach_offset_m), normalized=True)
        q = g.interpolate(nodes.loc[idx, "attach_fraction"], normalized=True)
        nodes.loc[idx, ["ax", "ay"]] = [q.x, q.y]
    rg = graph.build_run_graph(es, nodes, long_connector_m=1e9)
    rows = list(range(len(nodes)))
    d = graph.terminal_distances(rg, rows)
    pairs = [(0, j) for j in rows[1:]] + [(3, 5), (5, 3)]
    paths = _row_paths(rg, pairs)
    for (i, j), path in paths.items():
        assert path[0] == rg.terminal_vertex[i] and path[-1] == rg.terminal_vertex[j]
        pieces = graph.path_pieces(rg, path)
        assert float(rg.pieces["length_m"].to_numpy()[pieces].sum()) == pytest.approx(d[i, j], rel=1e-9)
        line = graph.path_geometry(rg, pieces, geoms, start_vertex=path[0])
        assert line.length == pytest.approx(d[i, j], rel=1e-6, abs=1e-6)
        start, end = np.asarray(line.coords[0]), np.asarray(line.coords[-1])
        for row, point in ((i, start), (j, end)):
            node = nodes.iloc[row]
            limit = 1.0 + 1e-6 if node.attach_offset_m <= 0.5 else 1e-6     # snapped ends lie within end_snap_m
            assert np.hypot(*(point - [node.x, node.y])) <= max(limit, node.attach_offset_m + 1.0)
            if node.attach_offset_m > 0.5:
                np.testing.assert_allclose(point, [node.x, node.y], atol=1e-9)
        # orientation without start_vertex (more than one piece) and reversal
        if len(pieces) > 1:
            assert graph.path_geometry(rg, pieces, geoms).equals(line)
            back = graph.path_geometry(rg, pieces[::-1], geoms, start_vertex=path[-1])
            np.testing.assert_allclose(np.asarray(back.coords)[::-1], np.asarray(line.coords), atol=1e-9)
    with pytest.raises(ValueError, match="empty path"):
        graph.path_geometry(rg, [], geoms)
    assert graph.path_geometry(rg, [], geoms, start_vertex=0).length == 0.0


def test_vertex_paths_between_node_rows():
    edges = _edge_set([(11, 1, 2), (12, 2, 3)], LINE)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0, "attach_offset_m": 0},
        {"x": 150.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 0.5, "attach_offset_m": 0.0},
        {"x": 200.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0},
    ])
    rg = graph.build_run_graph(edges, nodes)
    paths = _row_paths(rg, [(0, 2), (2, 0), (1, 1)])
    assert paths[(0, 2)] == [0, 1, rg.terminal_vertex[1], 2]
    assert paths[(2, 0)] == paths[(0, 2)][::-1]
    assert paths[(1, 1)] == [rg.terminal_vertex[1]]


def test_vertex_paths_with_a_limit_find_the_same_paths_within_it():
    edges = _edge_set([(11, 1, 2), (12, 2, 3)], LINE)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0, "attach_offset_m": 0},
        {"x": 150.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 0.5, "attach_offset_m": 0.0},
        {"x": 200.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0},
    ])
    rg = graph.build_run_graph(edges, nodes)
    vertex = [int(v) for v in rg.terminal_vertex]
    pairs = [(vertex[0], vertex[1]), (vertex[0], vertex[2]), (vertex[1], vertex[2])]
    unbounded = graph.vertex_paths(rg, pairs)
    assert graph.vertex_paths(rg, pairs, limit=200.0) == unbounded           # every route costs at most 200 m
    near = graph.vertex_paths(rg, pairs, limit=160.0, chunk=1)
    assert near[pairs[0]] == unbounded[pairs[0]] and near[pairs[2]] == unbounded[pairs[2]]
    assert near[pairs[1]] == []                                              # 200 m: beyond the limit


def _grid_run_graph(side: int, teleports: int = 0) -> "graph.RunGraph":
    """Run graph of a square street grid (50 m, all edges the same length: many routes cost the same) with
    ``teleports`` short edges (10 m) between random far vertices, which the radius of a bounded search misses."""
    coords = {i: (float(i % side) * 50.0, float(i // side) * 50.0) for i in range(side * side)}
    edges = [(len(coords) + i, i, i + 1) for i in range(side * side) if i % side < side - 1]
    edges += [(2 * len(coords) + i, i, i + side) for i in range(side * (side - 1))]
    rng = np.random.default_rng(3)
    edges += [(3 * len(coords) + k, int(a), int(b), 10.0)
              for k, (a, b) in enumerate(rng.integers(0, side * side, (teleports, 2))) if a != b]
    nodes = _nodes([{"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": edges[0][0], "attach_fraction": 0.0,
                     "attach_offset_m": 0.0}])
    return graph.build_run_graph(_edge_set(edges, coords), nodes)


@pytest.mark.parametrize("teleports", [0, 40])
def test_bounded_searches_on_local_parts_equal_the_whole_graph(teleports):
    from scipy.sparse.csgraph import dijkstra

    rg = _grid_run_graph(60, teleports)
    sources = np.unique(np.random.default_rng(1).integers(0, rg.n_vertices, 150))
    seen, sizes = [], []
    for batch, vertices, dist, pred in graph.bounded_searches(rg, sources, 400.0, chunk=8, return_predecessors=True):
        full_dist, full_pred = dijkstra(rg.csr, directed=False, indices=batch, limit=400.0, return_predecessors=True)
        assert np.array_equal(full_dist[:, vertices], dist)
        assert np.isinf(np.delete(full_dist, vertices, axis=1)).all()        # nothing reached outside the part
        assert np.array_equal(full_pred[:, vertices], np.where(pred >= 0, vertices[np.maximum(pred, 0)], pred))
        seen += batch.tolist()
        sizes.append(len(vertices))
    assert sorted(seen) == sources.tolist()                                  # every source exactly once
    assert min(sizes) < rg.n_vertices / 4                                    # the parts are local
    if teleports:
        assert max(sizes) > min(sizes)                                       # searches through teleports ran again


def test_bounded_distances_and_paths_equal_the_whole_graph():
    from scipy.sparse.csgraph import dijkstra

    rg = _grid_run_graph(40, teleports=20)
    rng = np.random.default_rng(2)
    pairs = [(int(a), int(b)) for a, b in rng.integers(0, rg.n_vertices, (300, 2))] + [(5, 5)]
    full = dijkstra(rg.csr, directed=False, limit=600.0)
    expected = np.array([full[a, b] for a, b in pairs])
    np.testing.assert_array_equal(graph.bounded_distances(rg, pairs, 600.0, chunk=16), expected)
    whole = graph.vertex_paths(dataclasses.replace(rg, vertex_xy=np.zeros_like(rg.vertex_xy)), pairs, limit=600.0)
    assert graph.vertex_paths(rg, pairs, limit=600.0, chunk=16) == whole     # same predecessors as one part


def test_subgraph_edges_near_points_go_through_the_edge_index():
    rows = [(5, 100, 101, 12.5, True, 3, False), (7, 101, 102, 20.0, False, -1, True)]
    vertex_rows = [(100, 0.0, 0.0), (101, 12.5, 0.0), (102, 12.5, 20.0)]
    client = _client([(r"FROM mv_subgraph_point p", rows), (r"FROM pylovo\.mv_graph_vertex v", vertex_rows)])
    edges = client.subgraph_edges_near(GRAPH_ID, np.array([[1.0, 2.0], [3.0, 4.0]]), 2100.0, [7, None])
    assert edges.edge_id.tolist() == [5, 7] and edges.vertex_id.tolist() == [100, 101, 102]
    texts = [t for t, _ in client.cur.statements]
    points = next(p for t, p in client.cur.statements if "CREATE TEMP TABLE mv_subgraph_point" in t)
    assert points == {"x": [1.0, 3.0], "y": [2.0, 4.0]}
    query, params = next((t, p) for t, p in client.cur.statements if "FROM mv_subgraph_point p" in t)
    assert "ST_DWithin(n.geom, p.pt, %(buffer_m)s)" in query and "SELECT unnest(%(must)s::bigint[])" in query
    assert "UNION" in query and " OR " not in query       # an OR would rerun the subquery per edge of a large graph
    assert params == {"g": GRAPH_ID, "buffer_m": 2100.0, "must": [7], "line": "osm_mv_line"}
    assert any("USING gist (pt)" in t for t in texts) and texts[-2].startswith("DROP TABLE pg_temp.mv_subgraph_point")


def test_trench_counts_shared_pieces_once():
    edges = _edge_set([(11, 1, 2), (12, 2, 3)], LINE)
    nodes = _nodes([
        {"kind": "busbar", "x": 0.0, "y": 0.0, "attach_edge_id": 11, "attach_fraction": 0.0, "attach_offset_m": 0},
        {"x": 100.0, "y": 30.0, "attach_edge_id": 11, "attach_fraction": 1.0, "attach_offset_m": 30.0},
        {"x": 200.0, "y": 0.0, "attach_edge_id": 12, "attach_fraction": 1.0, "attach_offset_m": 0.0},
    ])
    rg = graph.build_run_graph(edges, nodes)
    paths = _row_paths(rg, [(0, 1), (0, 2), (1, 2)])
    circuits = [graph.path_pieces(rg, paths[p]) for p in [(0, 1), (0, 2), (1, 2)]]
    km, most = graph.trench_km(rg, circuits)
    assert km == pytest.approx((100.0 + 100.0 + 30.0) / 1000.0)
    assert most == 2
    assert graph.trench_km(rg, []) == (0.0, 0)


# ----------------------------------------------------------------------------------------------------- mixin SQL
def _render(query) -> str:
    if isinstance(query, str):
        return query
    if isinstance(query, sql.Composed):
        return "".join(_render(part) for part in query.seq)
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return ".".join(f'"{s}"' for s in query.strings)
    if isinstance(query, sql.Literal):
        return repr(query.wrapped)
    raise TypeError(type(query))


class _Cursor:
    def __init__(self, answers):
        self.answers = answers
        self.statements = []
        self.description = None
        self.rowcount = 0
        self._result = None

    def execute(self, query, params=None):
        text = _render(query)
        self.statements.append((text, params))
        self._result = None
        for pattern, answer in self.answers:
            if re.search(pattern, text, flags=re.S):
                self._result = answer(params) if callable(answer) else answer
                break

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


class _Conn:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _client(answers):
    client = object.__new__(MvGraphMixin)
    client.cur = _Cursor(answers)
    client.conn = _Conn()
    client.logger = logging.getLogger("mv-graph-test")
    return client


GRAPH_ID = "g0123456789abcdef"
#: Parameters of a graph build (the fake cursor has no OSM MV lines, so it holds the streets only).
STREETS = MvParameters()
BASE_ANSWERS = [
    (r"SELECT ST_SRID\(geom\)", [(25832,)]),
    # the municipalities of the graph: the box of the buffered region, the extent of the ways per AGS, their class
    (r"FROM mv_ags_region WHERE NOT ST_IsEmpty", [(0.0, 0.0, 1000.0, 1000.0)]),
    (r"ST_Extent\(w\.geom\) AS b", [("09000001", 0.0, 0.0, 400.0, 400.0), ("09000002", 500.0, 500.0, 900.0, 900.0)]),
    (r"THEN 0 WHEN ST_Contains", [("09000001", 1), ("09000002", 1)]),
    (r"max\(w\.changelog_id\)", [(10, 12345, 7, 0.0, 0.0, 1000.0, 1000.0)]),
    (r"max\(b\.changelog_id\)", [(20, 9)]),
    (r"SELECT ST_AsEWKB\(ST_Transform", [(b"box",)]),
    (r'SELECT count\(\*\) FROM "mvg', [(100,)]),                     # rows staged from the source
]


def _tables(*present: str):
    """Answer of the existence probe of the source reader: only the given ``schema.table`` exist."""
    return (r"to_regclass", lambda params: [(params[0].replace('"', "") in present,)])


def test_prune_graphs_deletes_the_unreferenced_graphs_under_their_build_lock():
    client = _client([(r"FROM pg_locks", [(0,)]), (r"DELETE FROM pylovo\.mv_graph g", [("g2",), ("g1",)])])
    assert client.prune_graphs() == ["g1", "g2"] and client.conn.commits == 1
    text, params = client.cur.statements[-1]
    assert "NOT EXISTS (SELECT 1 FROM pylovo.mv_variant v WHERE v.graph_id = g.graph_id)" in text
    assert "pg_try_advisory_xact_lock(%(key)s, hashtext(g.graph_id))" in text and params == {"key": 907363}
    busy = _client([(r"FROM pg_locks", [(1,)])])                        # an MV run holds a variant lock
    with pytest.raises(RuntimeError, match="an MV run .* is active"):
        busy.prune_graphs()
    assert busy.conn.rollbacks == 1 and not any("DELETE" in text for text, _ in busy.cur.statements)


def test_edges_sql_inlines_a_validated_graph_id():
    text = edges_sql(GRAPH_ID)
    assert f"WHERE graph_id = '{GRAPH_ID}'" in text and "FROM pylovo.mv_graph_edge" in text
    assert edges_sql(GRAPH_ID, settlement_only=True).endswith("AND in_settlement")
    for bad in ("g0123", "x0123456789abcdef", "g0123456789abcdef'; DROP TABLE x; --", None):
        with pytest.raises(ValueError):
            edges_sql(bad)


def test_graph_id_is_deterministic_and_content_addressed():
    params = build_parameters(MvParameters())
    assert params["excluded_klasse"] == ["Bundesautobahn"] and params["snap_tolerance_m"] == 0.25
    a = graph_id_for("basedata.ways_per_junction", ["09000002", "09000001"], params, "ways 1:2:3|buildings 4:5")
    assert re.fullmatch(r"g[0-9a-f]{16}", a)
    assert a == graph_id_for("basedata.ways_per_junction", ["09000001", "09000002"], dict(params),
                             "ways 1:2:3|buildings 4:5")
    assert a != graph_id_for("basedata.ways_per_junction", ["09000001"], params, "ways 1:2:3|buildings 4:5")
    assert a != graph_id_for("basedata.ways_per_junction", ["09000001", "09000002"], params, "ways 1:2:4|buildings 4:5")
    other = build_parameters(MvParameters().replace(urban_min_buildings=500))
    assert a != graph_id_for("basedata.ways_per_junction", ["09000001", "09000002"], other, "ways 1:2:3|buildings 4:5")


def test_ensure_graph_reuses_a_stored_graph_without_writing():
    client = _client(BASE_ANSWERS + [(r"SELECT 1 FROM pylovo\.mv_graph WHERE", [(1,)])])
    graph_id = client.ensure_graph(b"\x01", MvParameters())
    assert re.fullmatch(r"g[0-9a-f]{16}", graph_id)
    texts = [t for t, _ in client.cur.statements]
    assert not any(re.search(r"\b(INSERT|UPDATE|DELETE)\b", t) for t in texts)        # temporary tables only
    assert all("CREATE TEMP TABLE" in t for t in texts if re.search(r"\bCREATE\b", t))
    assert client.conn.commits == 0
    # the buffered region is computed in the pylovo database, the source is read by box and AGS only
    region_text, region_params = next((t, p) for t, p in client.cur.statements if "mv_ags_region ON COMMIT" in t)
    assert "ST_Buffer(ST_GeomFromWKB(%(region)s, 25832)" in region_text
    assert region_params == {"region": b"\x01", "src_srid": 25832, "buffer_m": 5000.0}
    ags_text, ags_params = next((t, p) for t, p in client.cur.statements if "ST_Extent(w.geom) AS b" in t)
    assert '"basedata"."ways_per_junction"' in ags_text and "%(region)s" not in ags_text
    assert ags_params == {"x0": 0.0, "y0": 0.0, "x1": 1000.0, "y1": 1000.0, "src_srid": 25832}
    # both AGS lie inside the buffer by their extent: no way is read one by one
    assert not any("mvg_ags_ways" in t and "CREATE" in t for t in texts)


def test_graph_ags_reads_the_ways_of_the_municipalities_at_the_border_of_the_buffer():
    answers = [a for a in BASE_ANSWERS if "THEN 0" not in a[0]] + [
        (r"THEN 0 WHEN ST_Contains", [("09000001", 1), ("09000002", 2), ("09000003", 0)]),
        (r"FROM mvg_ags_ways w, mv_ags_region r", [("09000002",)])]
    client = _client(answers)
    assert client.graph_ags(b"\x01", 5000.0) == ["09000001", "09000002"]
    stage, params = next((t, p) for t, p in client.cur.statements if 'CREATE TEMP TABLE "mvg_ags_ways"' in t)
    assert '"basedata"."ways_per_junction"' in stage and params["ags"] == ["09000002"]       # the border AGS only
    assert "w.geom && ST_MakeEnvelope(%(x0)s, %(y0)s, %(x1)s, %(y1)s, %(src_srid)s)" in stage
    assert client.cur.statements[-1][0].startswith("DROP TABLE IF EXISTS pg_temp.mv_ags_region")


def test_ensure_graph_builds_in_one_locked_transaction_with_qualified_tables():
    answers = BASE_ANSWERS + [
        (r"SELECT 1 FROM pylovo\.mv_graph WHERE", []),
        (r"WITH lone AS", [(3,)]),
        (r"ST_Extent", [(0.0, 0.0, 1000.0, 1000.0)]),
    ]
    client = _client(answers)
    graph_id = client.ensure_graph(b"\x01", STREETS)
    assert client.conn.commits == 1 and client.conn.rollbacks == 0
    texts = [t for t, _ in client.cur.statements]
    lock = next(i for i, t in enumerate(texts) if "pg_advisory_xact_lock" in t)
    first_write = next(i for i, t in enumerate(texts) if re.search(r"\bINSERT\b|CREATE TEMP TABLE \"?mvg_", t))
    assert lock < first_write
    assert client.cur.statements[lock][1] == {"key": 907363, "g": graph_id}
    # source tables only through the configured schema, MV tables always schema-qualified
    joined = "\n".join(texts)
    assert '"basedata"."ways_per_junction"' in joined and '"basedata"."buildings"' in joined
    assert not re.search(r"FROM\s+(ways_per_junction|buildings|mv_graph\w*)\b", joined)
    assert not re.search(r"(INSERT INTO|UPDATE)\s+mv_graph", joined)
    # the pgRouting edge queries carry the graph id as a literal
    pgr = [p for t, p in client.cur.statements if "pgr_connectedComponents" in t]
    assert len(pgr) == 2
    assert pgr[0]["edges_sql"] == edges_sql(graph_id)
    assert pgr[1]["settlement_edges_sql"] == edges_sql(graph_id, settlement_only=True)
    # fresh edge statistics between the settlement ids and the settlement polygons (stale ones give a quadratic plan)
    ids = next(i for i, t in enumerate(texts) if "SET settlement_id" in t)
    polygons = next(i for i, t in enumerate(texts) if "INSERT INTO pylovo.mv_graph_settlement" in t)
    assert "ANALYZE pylovo.mv_graph_edge" in texts[ids + 1:polygons]
    assert "sum(length_m ORDER BY edge_id)" in texts[polygons]
    assert "ST_Collect(geom ORDER BY edge_id)" in texts[polygons]
    summary = next(p for t, p in client.cur.statements if "UPDATE pylovo.mv_graph g" in t)
    assert summary["seams"] == 3 and summary["seconds"] >= 0
    insert = next(p for t, p in client.cur.statements if "INSERT INTO pylovo.mv_graph (" in t)
    assert insert["ags"] == ["09000001", "09000002"] and insert["excluded"] == ["Bundesautobahn"]
    assert insert["fingerprint"] == "ways 10:12345:7|buildings 20:9|landuse none|osm_lines none"
    # without the OSM land-use table the settlement flags use address buildings only (empty mvg_lu)
    assert any(t.startswith("CREATE TEMP TABLE mvg_lu (geom geometry)") for t in texts)
    # seam repair before the final endpoint clustering, recorded in the build parameters
    cut = next(i for i, t in enumerate(texts) if "CREATE TEMP TABLE mvg_cut" in t)
    clusters = [i for i, t in enumerate(texts) if "ST_ClusterDBSCAN" in t]
    assert len(clusters) == 2 and clusters[0] < cut < clusters[1] < next(i for i, t in enumerate(texts)
                                                                        if "CREATE TEMP TABLE mvg_vert" in t)
    assert client.cur.statements[cut][1] == {"split": 0.5}
    assert '"seam_split_m": 0.5' in insert["params"]


def test_settlement_flags_use_built_up_osm_landuse_when_present():
    answers = BASE_ANSWERS + [
        (r"SELECT 1 FROM pylovo\.mv_graph WHERE", []),
        _tables("osm.landuse_polygon"),
        (r'SELECT count\(\*\) FROM "osm"\."landuse_polygon" l WHERE l\.osm_type', [(4107,)]),
        (r"WITH lone AS", [(3,)]),
        (r"ST_Extent", [(0.0, 0.0, 1000.0, 1000.0)]),
    ]
    client = _client(answers)
    graph_id = client.ensure_graph(b"\x01", STREETS)
    texts = [t for t, _ in client.cur.statements]
    insert = next(p for t, p in client.cur.statements if "INSERT INTO pylovo.mv_graph (" in t)
    assert insert["fingerprint"] == "ways 10:12345:7|buildings 20:9|landuse 4107|osm_lines none"
    assert '"settlement_landuse": ["commercial", "industrial", "residential", "retail"]' in insert["params"]
    # the count of the fingerprint and the staged rows use the extent of the ways (EWKB box in the layer's SRID)
    count = next(p for t, p in client.cur.statements if 'SELECT count(*) FROM "osm"."landuse_polygon"' in t)
    assert count == {"types": ["commercial", "industrial", "residential", "retail"], "box": b"box"}
    staged, params = next((t, p) for t, p in client.cur.statements if 'CREATE TEMP TABLE "mvg_lu_raw"' in t)
    assert '"osm"."landuse_polygon"' in staged and params["types"] == ["commercial", "industrial", "residential",
                                                                     "retail"]
    landuse = next(t for t in texts if "CREATE TEMP TABLE mvg_lu ON COMMIT DROP" in t)
    assert "FROM pg_temp.mvg_lu_raw r" in landuse
    # the land-use table is filled before the settlement flags, which test the edge midpoints against it
    flags = next(i for i, t in enumerate(texts) if "SET in_settlement" in t)
    assert texts.index(landuse) < flags
    assert "ST_LineInterpolatePoint(e.geom, 0.5)" in texts[flags] and "OR EXISTS" in texts[flags]
    # the land-use count is part of the fingerprint and so of the graph id
    without = _client(BASE_ANSWERS + [(r"SELECT 1 FROM pylovo\.mv_graph WHERE", [(1,)])])
    assert without.ensure_graph(b"\x01", STREETS) != graph_id


def test_osm_mv_lines_join_the_graph_at_crossings_branches_and_loose_ends(monkeypatch):
    mv = LineString([(0.0, 0.0), (500.0, 0.0)]).wkb
    lines = [(11, "minor_line", {}, mv),                                        # untagged minor_line: MV
             (12, "line", {"voltage": "110000"}, mv),                           # 110 kV: left out
             (13, "minor_line", {"voltage": "20000", "disused:power": "line"}, mv),   # not in service
             (14, "line", {"voltage": "110000;20000"}, mv)]                     # shared towers: MV corridor
    answers = BASE_ANSWERS + [
        (r"SELECT 1 FROM pylovo\.mv_graph WHERE", []),
        _tables("osm.infrastructure_line", "osm.tags"),
        (r'FROM "osm"\.infrastructure_line l\s+WHERE l\.osm_type', [(1000, 2500, 99)]),
        (r'FROM "osm"\.infrastructure_line l\s+LEFT JOIN', lines),
        (r"WITH lone AS", [(0,)]),
        (r"ST_Extent", [(0.0, 0.0, 1000.0, 1000.0)]),
    ]
    inserted = []
    monkeypatch.setattr(mixin_module, "execute_values",
                        lambda cur, query, values, **kw: inserted.append((query, list(values))))
    client = _client(answers)
    graph_id = client.ensure_graph(b"\x01", MvParameters())
    assert graph_id != _client(BASE_ANSWERS + answers[len(BASE_ANSWERS):]).ensure_graph(
        b"\x01", MvParameters(osm_line_snap_m=50.0))                      # the snap distance is a build parameter
    texts = [t for t, _ in client.cur.statements]
    insert = next(p for t, p in client.cur.statements if "INSERT INTO pylovo.mv_graph (" in t)
    assert insert["fingerprint"].endswith("|osm_lines 1000:2500:99")
    assert '"osm_mv_lines": true' in insert["params"] and '"osm_line_snap_m": 100.0' in insert["params"]
    # the MV lines of the classifier (11 and 14) go into the build as edges of klasse osm_mv_line
    assert [(row[0], row[1]) for row in inserted[0][1]] == [(11, "minor_line"), (14, "line")]
    osm_insert = next(p for t, p in client.cur.statements if "FROM mvg_osm o" in t)
    assert osm_insert == {"klasse": "osm_mv_line"}
    # crossings and branch junctions cut first, then the loose ends join the streets within osm_line_snap_m
    order = [next(i for i, t in enumerate(texts) if marker in t)
             for marker in ("FROM mvg_osm o", "INSERT INTO mvg_cut", "CREATE TEMP TABLE mvg_conn", "ST_MakeLine(c.pt, c.cp)",
                            "CREATE TEMP TABLE mvg_vert")]
    assert order == sorted(order)
    loose = next(p for t, p in client.cur.statements if "CREATE TEMP TABLE mvg_conn" in t)
    assert loose["snap"] == 100.0 and loose["osm"] == ["osm_mv_line", "osm_mv_connector"]
    # OSM edges are never in a settlement
    flags = next(p for t, p in client.cur.statements if "SET in_settlement" in t)
    assert flags["osm"] == ["osm_mv_line", "osm_mv_connector"]


def test_osm_line_classifier_keeps_mv_overhead_lines_in_service():
    from pylovo.mv_grid.osm_lines import is_mv_overhead_line, parse_voltages

    assert parse_voltages("110000;20000") == {110000, 20000} and parse_voltages("20 kV") == {20000}
    assert parse_voltages("medium") == set() and parse_voltages(None) == set()
    keep = [("minor_line", {}), ("line", {"voltage": "20000"}), ("minor_line", {"voltage": "medium"}),
            ("line", {"voltage": "110000;20000"}), ("minor_line", {"cables": "3"}),
            # read as by the HV/MV substation filter (one parser): 20 kV each
            ("line", {"voltage": "20000/400"}), ("line", {"voltage": "20_kv"}), ("line", {"voltage": "20"})]
    drop = [("line", {}), ("cable", {"voltage": "20000"}), ("line", {"voltage": "110000"}),
            ("minor_line", {"voltage": "400"}), ("minor_line", {"cables": "4"}),
            ("minor_line", {"location": "underground"}), ("minor_line", {"line": "busbar"}),
            ("line", {"voltage": "15000", "frequency": "16.7"}), ("minor_line", {"abandoned:power": "line"}),
            ("minor_line", {"location": "roof"})]
    assert all(is_mv_overhead_line(sub, tags) for sub, tags in keep)
    assert not any(is_mv_overhead_line(sub, tags) for sub, tags in drop)
    assert is_mv_overhead_line("line", {"voltage": "20000", "frequency": "50;16.7"})


def test_seam_split_switch_changes_the_build_and_the_graph_id(monkeypatch):
    answers = BASE_ANSWERS + [(r"SELECT 1 FROM pylovo\.mv_graph WHERE", []), (r"WITH lone AS", [(0,)]),
                              (r"ST_Extent", [(0.0, 0.0, 1.0, 1.0)])]
    with_split = _client(answers).ensure_graph(b"\x01", MvParameters())
    monkeypatch.setattr(mixin_module, "SEAM_SPLIT_M", None)
    client = _client(answers)
    without = client.ensure_graph(b"\x01", MvParameters())
    assert without != with_split
    texts = [t for t, _ in client.cur.statements]
    assert not any("mvg_cut" in t and "CREATE" in t for t in texts)
    assert sum("ST_ClusterDBSCAN" in t for t in texts) == 1


def test_ensure_graph_rolls_back_a_failed_build():
    def fail(_params):
        raise RuntimeError("boom")

    answers = BASE_ANSWERS + [(r"SELECT 1 FROM pylovo\.mv_graph WHERE", []), (r"CREATE TEMP TABLE mvg_src", fail)]
    client = _client(answers)
    with pytest.raises(RuntimeError, match="boom"):
        client.ensure_graph(b"\x01", MvParameters())
    assert client.conn.rollbacks == 1 and client.conn.commits == 0


def test_attach_points_returns_one_typed_row_per_key(monkeypatch):
    rows = [(1, 11, 1, True, 5, False, 0.25, 0.0, 10.0, 0.0), (2, 12, 1, False, None, False, 0.5, 30.0, 5.0, 5.0)]
    client = _client([(r"FROM mv_point_tem p", rows)])
    calls = []
    monkeypatch.setattr(mixin_module, "execute_values", lambda cur, query, values, **kw: calls.append((query, values,
                                                                                                         kw)))
    frame = client.attach_points(GRAPH_ID, pd.DataFrame({"key": [2, 1], "x": [5.0, 10.0], "y": [35.0, 0.0]}))
    assert list(frame.columns) == ["key", "edge_id", "component", "in_settlement", "settlement_id",
                                   "settlement_urban", "fraction", "offset_m", "ax", "ay"]
    assert frame.settlement_id.isna().tolist() == [False, True] and str(frame.settlement_id.dtype) == "Int64"
    assert frame.in_settlement.dtype == bool and frame.offset_m.dtype == np.float64
    assert calls[0][1] == [(2, 5.0, 35.0), (1, 10.0, 0.0)] and "25832" in calls[0][2]["template"]
    texts = [t for t, _ in client.cur.statements]
    # equally near edges go to the lowest edge id
    assert any("ON COMMIT DROP" in t for t in texts) and any("ORDER BY e.geom <-> p.geom, e.edge_id LIMIT 1" in t
                                                             for t in texts)


def test_subgraph_edges_and_region_arrays_build_typed_arrays():
    edge_rows = [(5, 100, 101, 12.5, True, 3, False), (7, 101, 102, 20.0, False, -1, True)]
    vertex_rows = [(100, 0.0, 0.0), (101, 12.5, 0.0), (102, 12.5, 20.0)]
    client = _client([(r"FROM pylovo\.mv_graph_vertex v", vertex_rows), (r"FROM pylovo\.mv_graph_edge e\s+WHERE",
                                                                         edge_rows),
                      (r"ORDER BY edge_id", [(5, 100, 101, 12.5), (7, 101, 102, 20.0)])])
    es = client.subgraph_edges(GRAPH_ID, b"\x01", 2000.0, [7, np.int64(5), None])
    assert es.edge_id.dtype == np.int64 and es.vertex_id.tolist() == [100, 101, 102]
    assert es.vertex_xy.shape == (3, 2) and es.settlement_id.tolist() == [3, -1] and es.in_settlement.dtype == bool
    assert es.overhead.tolist() == [False, True]                    # edge 7 is an OSM MV line
    edge_params = next(p for t, p in client.cur.statements if "ST_DWithin" in t)
    assert edge_params["must"] == [5, 7] and edge_params["buffer_m"] == 2000.0
    edge_id, source, target, length = client.region_graph_arrays(GRAPH_ID)
    assert edge_id.dtype == source.dtype == target.dtype == np.int64 and length.dtype == np.float64
    assert graph.region_csr(source, target, length)[0].shape == (3, 3)


def test_edge_municipalities_give_the_ags_per_street_edge():
    client = _client([(r"SELECT edge_id, ags FROM pylovo\.mv_graph_edge", [(5, "09474126"), (7, 9474127)])])
    ags = client.edge_municipalities(GRAPH_ID)
    assert ags.to_dict() == {5: "09474126", 7: "9474127"} and ags.index.name == "edge_id"
    text, params = client.cur.statements[-1]
    assert "ags IS NOT NULL" in text and params["osm"] == ["osm_mv_line", "osm_mv_connector"]
