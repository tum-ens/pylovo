"""MV supply areas (pylovo.mv_grid.districts): routed assignment, overload report and the nominal voltage.

DB-free. The weighted graph Voronoi of pylovo.mv_grid.graph is replaced by a small brute-force fake with the
same contract (one Dijkstra per site); one test compares with the real module when it is importable.
"""
from __future__ import annotations

import importlib
import types

import numpy as np
import pandas as pd
import pytest
import scipy.sparse
from scipy.sparse.csgraph import dijkstra

from pylovo.mv_grid import coincidence, districts
from pylovo.mv_grid.parameters import MvParameters

G = np.array([0.07, 0.5, 0.6, 0.6])
PARAMS = MvParameters()


def _fake_graph_voronoi(csr, site_u, site_v, site_cost_u, site_cost_v):
    """Brute force: one Dijkstra per site on the graph plus its dummy vertex; ties go to the lower site index."""
    n = csr.shape[0]
    n_sites = len(site_u)
    coo = csr.tocoo()
    dist = np.full((max(n_sites, 1), n), np.inf)
    for s in range(n_sites):
        rows, cols, data = list(coo.row), list(coo.col), list(coo.data)
        for end, cost in ((site_u[s], site_cost_u[s]), (site_v[s], site_cost_v[s])):
            if end >= 0:
                rows.append(int(end))
                cols.append(n)
                data.append(float(cost))
        aug = scipy.sparse.csr_matrix((data, (rows, cols)), shape=(n + 1, n + 1))
        dist[s] = dijkstra(aug, directed=False, indices=n)[:n]
    best = dist.min(axis=0)
    site = np.where(np.isfinite(best), dist.argmin(axis=0), -1)
    return best, site.astype(np.int64)


@pytest.fixture(autouse=True)
def fake_graph(monkeypatch):
    fake = types.SimpleNamespace(graph_voronoi=_fake_graph_voronoi)
    monkeypatch.setattr(districts, "_graph", lambda: fake)
    return fake


def _path_graph(lengths, start=1000):
    """A path of vertices start, start+1, ... with the given edge lengths; edge id k joins vertex k and k+1."""
    ids = np.arange(start, start + len(lengths) + 1)
    source = ids[:-1]
    target = ids[1:]
    vertex_index = {int(v): i for i, v in enumerate(ids)}
    u = np.array([vertex_index[int(s)] for s in source])
    v = np.array([vertex_index[int(t)] for t in target])
    csr = scipy.sparse.csr_matrix((np.asarray(lengths, dtype=float), (u, v)), shape=(len(ids), len(ids)))
    edges = pd.DataFrame({"edge_id": np.arange(len(lengths)) + 1, "edge_source": source, "edge_target": target,
                          "edge_length_m": np.asarray(lengths, dtype=float)})
    return csr, vertex_index, edges


def _attach(frame: pd.DataFrame, edges: pd.DataFrame, edge_id, fraction, offset=0.0) -> pd.DataFrame:
    frame = frame.copy()
    frame["attach_edge_id"] = edge_id
    frame["attach_fraction"] = fraction
    frame["attach_offset_m"] = offset
    return frame.join(edges.set_index("edge_id"), on="attach_edge_id")


def _nodes(n, *, households=100.0, kind="station", hint="ring", x=None):
    frame = pd.DataFrame({"kind": kind, "connection_hint": hint,
                          "x": np.arange(n, dtype=float) * 100.0 if x is None else x, "y": 0.0,
                          "inst_res": households * 2.0, "units_res": households, "inst_com": 0.0, "units_com": 0.0,
                          "inst_pub": 0.0, "units_pub": 0.0, "inst_unk": 0.0, "units_unk": 0.0, "p_fixed_kw": 0.0},
                         index=pd.Index(np.arange(n) + 1, name="mv_node_id"))
    return frame


def _sites(n, x):
    return pd.DataFrame({"x": np.asarray(x, dtype=float), "y": 0.0, "osm_ratings_mva": [None] * n})


def test_site_capacity_mva():
    sites = pd.DataFrame({"x": [0.0, 1.0, 2.0]})
    capacity = districts.site_capacity_mva(sites, PARAMS)
    assert capacity.tolist() == [63.0, 63.0, 63.0]                               # (2 - 1) x 63 MVA
    one_unit = PARAMS.replace(hv_mv_units=1, hv_mv_n1_utilization=0.8)
    assert districts.site_capacity_mva(sites.iloc[:1], one_unit).tolist() == [pytest.approx(63.0 * 0.8)]
    three = PARAMS.replace(hv_mv_units=3)
    assert districts.site_capacity_mva(sites.iloc[:1], three).tolist() == [126.0]


def test_site_loads_follow_the_category_law():
    rng = np.random.default_rng(3)
    installed = rng.uniform(0, 500, (30, 4))
    units = rng.integers(0, 50, (30, 4)).astype(float)
    fixed = np.where(rng.random(30) < 0.2, rng.uniform(100, 900, 30), 0.0)
    owner = rng.integers(-1, 3, 30)
    counts = rng.random(30) < 0.8
    loads = districts.site_loads_mva(owner, installed, units, fixed, counts, 3, G, 0.9)
    for s in range(3):
        mask = (owner == s) & counts
        expected = coincidence.pooled_kw(installed[mask], units[mask], fixed[mask], G) / 0.9 / 1000.0
        assert loads[s] == pytest.approx(expected, rel=1e-12)


def test_nearest_site_along_the_street():
    # path of 10 edges of 100 m; sites attached at both ends, nodes along the path
    csr, vertex_index, edges = _path_graph([100.0] * 10)
    sites = _attach(_sites(2, [0.0, 1000.0]), edges, [1, 10], [0.0, 1.0], [5.0, 5.0])
    nodes = _attach(_nodes(5), edges, [2, 4, 6, 8, 9], [0.5, 0.5, 0.2, 0.5, 0.9])
    result = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95,
                                         districts.site_capacity_mva(sites, PARAMS))
    # node positions along the path: 150, 350, 520, 750, 890 m
    assert result.site_of_node.tolist() == [0, 0, 1, 1, 1]
    assert not result.bridged.any() and result.overloaded == []
    assert result.distance_m[0] == pytest.approx(5.0 + 150.0)
    assert result.distance_m[2] == pytest.approx(5.0 + 480.0)


def test_same_edge_direct_cost_wins():
    # one long edge of 2 km; site in the middle, a second site 1 km beyond the end; the node next to the first
    csr, vertex_index, edges = _path_graph([2000.0, 1000.0])
    sites = _attach(_sites(2, [1000.0, 3000.0]), edges, [1, 2], [0.5, 1.0])
    nodes = _attach(_nodes(1, x=[1900.0]), edges, [1], [0.95])
    result = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95, np.array([63.0, 63.0]))
    # through the edge ends site 0 would need 1000 + 100 = 1100 m, site 1 1000 + 100 = 1100 m; directly 900 m
    assert result.site_of_node.tolist() == [0]
    assert result.distance_m[0] == pytest.approx(900.0)


def test_edge_length_from_mapping_and_ties_to_lower_site():
    csr, vertex_index, edges = _path_graph([100.0, 100.0])
    sites = _attach(_sites(2, [0.0, 200.0]), edges, [1, 2], [0.0, 1.0]).drop(columns="edge_length_m")
    nodes = _attach(_nodes(1, x=[100.0]), edges, [1], [1.0]).drop(columns="edge_length_m")
    result = districts.routed_assignment(csr, vertex_index, {1: 100.0, 2: 100.0}, sites, nodes, G, 0.95,
                                         np.array([63.0, 63.0]))
    assert result.site_of_node.tolist() == [0]                     # 100 m to both: the lower site index


def test_overload_is_flagged_when_capacity_is_short():
    csr, vertex_index, edges = _path_graph([100.0] * 4)
    sites = _attach(_sites(2, [0.0, 400.0]), edges, [1, 4], [0.0, 1.0])
    nodes = _attach(_nodes(3, households=2000.0), edges, [1, 2, 3], 1.0)
    result = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95, np.array([0.5, 0.5]))
    assert result.overloaded == [0] and result.site_of_node.tolist() == [0, 0, 1]   # reported, the load stays
    assert result.load_mva[0] > 0.5
    assert (result.site_of_node >= 0).all()


def test_generators_and_excluded_nodes_do_not_count():
    csr, vertex_index, edges = _path_graph([100.0] * 4)
    sites = _attach(_sites(1, [0.0]), edges, [1], [0.0])
    nodes = _attach(_nodes(3, households=5000.0), edges, [1, 2, 3], 1.0)
    nodes["kind"] = ["generator", "customer", "station"]
    nodes["connection_hint"] = ["tee", "excluded", "ring"]
    nodes.loc[nodes.index[1], "p_fixed_kw"] = 30000.0
    result = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 1.0, np.array([63.0]))
    only_station = coincidence.coincident_kw(np.array([10000.0, 0, 0, 0]), np.array([5000.0, 0, 0, 0]), G) / 1000.0
    assert result.load_mva[0] == pytest.approx(only_station)
    assert result.site_of_node.tolist() == [0, 0, 0]


def test_unreachable_nodes_go_to_the_nearest_site_and_are_bridged():
    # two disconnected paths: vertices 1000-1002 and 2000-2001
    csr_a, index_a, edges_a = _path_graph([100.0, 100.0], start=1000)
    csr_b, index_b, edges_b = _path_graph([100.0], start=2000)
    csr = scipy.sparse.block_diag([csr_a, csr_b]).tocsr()
    vertex_index = {**index_a, **{k: v + csr_a.shape[0] for k, v in index_b.items()}}
    edges_b = edges_b.assign(edge_id=edges_b["edge_id"] + 10)
    edges = pd.concat([edges_a, edges_b], ignore_index=True)
    sites = _attach(_sites(2, [0.0, 5000.0]), edges, [1, 2], [0.0, 1.0])     # both on the first island
    nodes = _attach(_nodes(3, x=[150.0, 4900.0, 100.0]), edges, [2, 11, np.nan], [0.5, 0.5, np.nan])
    result = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95, np.array([63.0, 63.0]))
    assert result.bridged.tolist() == [False, True, True]
    # node 0 is 150 m from site 0 and 50 m from site 1 along the street; the others are Euclidean
    assert result.site_of_node.tolist() == [1, 1, 0]
    assert np.isinf(result.distance_m[1:]).all()


def test_input_checks():
    csr, vertex_index, edges = _path_graph([100.0])
    sites = _attach(_sites(1, [0.0]), edges, [1], [0.0])
    nodes = _attach(_nodes(1), edges, [1], [0.5])
    with pytest.raises(ValueError, match="capacity_mva"):
        districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95, np.array([1.0, 2.0]))
    with pytest.raises(ValueError, match="power_factor"):
        districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.0, np.array([1.0]))



def test_routed_assignment_with_the_graph_module(monkeypatch):
    try:
        graph = importlib.import_module("pylovo.mv_grid.graph")
    except ImportError:
        pytest.skip("pylovo.mv_grid.graph is not available")
    if not hasattr(graph, "graph_voronoi") or not hasattr(graph, "region_csr"):
        pytest.skip("pylovo.mv_grid.graph has no graph_voronoi")
    rng = np.random.default_rng(7)
    side = 12
    ids = np.arange(side * side).reshape(side, side) + 500
    source = np.concatenate([ids[:, :-1].ravel(), ids[:-1, :].ravel()])
    target = np.concatenate([ids[:, 1:].ravel(), ids[1:, :].ravel()])
    length = rng.uniform(50, 150, len(source))
    csr, vertex_index = graph.region_csr(source, target, length)
    edges = pd.DataFrame({"edge_id": np.arange(len(source)) + 1, "edge_source": source, "edge_target": target,
                          "edge_length_m": length})
    site_edges = rng.choice(edges["edge_id"], 4, replace=False)
    sites = _attach(_sites(4, rng.uniform(0, 1000, 4)), edges, site_edges, rng.uniform(0, 1, 4), 10.0)
    node_edges = rng.choice(edges["edge_id"], 80)
    nodes = _attach(_nodes(80, households=60.0), edges, node_edges, rng.uniform(0, 1, 80), rng.uniform(0, 20, 80))
    capacity = np.array([1.5, 1.5, 63.0, 63.0])
    ours = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95, capacity)
    monkeypatch.setattr(districts, "_graph", lambda: graph)
    real = districts.routed_assignment(csr, vertex_index, {}, sites, nodes, G, 0.95, capacity)
    assert real.site_of_node.tolist() == ours.site_of_node.tolist()
    np.testing.assert_allclose(real.load_mva, ours.load_mva)



# ---------------------------------------------------------------------------------------------- nominal voltage
def _density_nodes() -> pd.DataFrame:
    """District 1: two stations (630 + 400 kVA) and a 950 kW customer; district 2: a 250 kVA station, a 190 kW
    customer, an HV customer and a generator; one station without a district."""
    return pd.DataFrame({
        "mv_node_id": [1, 2, 3, 4, 5, 6, 7, 8],
        "mv_grid_id": [1, 1, 1, 2, 2, 2, 2, np.nan],
        "kind": ["station", "station", "customer", "station", "customer", "customer", "generator", "station"],
        "connection_hint": ["ring", "ring", "ring", "ring", "ring", "excluded", "tee", "ring"],
        "sn_kva": [630.0, 400.0, np.nan, 250.0, np.nan, np.nan, np.nan, 400.0],
        "installed_kw": [99.0, 50.0, 950.0, 10.0, 190.0, 30000.0, 5000.0, 1.0],
    })


def test_installed_density_counts_station_ratings_and_customer_peaks():
    table = districts.installed_density(_density_nodes(), {1: 0.25, 2: 2.0, 3: 0.0})
    assert table.index.tolist() == [1, 2, 3] and list(table.columns) == list(districts.DENSITY_COLUMNS)
    one, two, empty = table.loc[1], table.loc[2], table.loc[3]
    assert (one["n_stations"], one["n_customers"], two["n_stations"], two["n_customers"]) == (2, 1, 1, 1)
    assert one["station_kva"] == 1030.0 and one["customer_kva"] == pytest.approx(1000.0)    # 950 kW / 0.95
    assert one["density_mva_km2"] == pytest.approx(2.03 / 0.25)                             # 8.12 MVA/km²
    assert two["station_kva"] == 250.0 and two["customer_kva"] == pytest.approx(200.0)      # no HV customer
    assert two["density_mva_km2"] == pytest.approx(0.45 / 2.0)
    assert empty["installed_mva"] == 0.0 and np.isnan(empty["density_mva_km2"])             # no area: no density
    assert np.isnan(districts.installed_density(_density_nodes(), {1: np.nan}).loc[1, "density_mva_km2"])


@pytest.mark.parametrize("density, expected", [
    (7.999, 20.0), (8.0, 10.0), (8.12, 10.0), (0.3, 20.0), (np.nan, 20.0), (None, 20.0)])
def test_auto_voltage_takes_ten_kv_from_the_threshold_on(density, expected):
    assert PARAMS.auto_voltage and PARAMS.mv_10kv_min_density_mva_km2 == 5.0
    assert PARAMS.replace(mv_10kv_min_density_mva_km2=8.0).district_kv(density) == expected
    assert MvParameters(vn_kv=20).district_kv(density) == 20.0                 # a fixed voltage ignores the density
    assert MvParameters(vn_kv=10).district_kv(density) == 10.0


def test_district_voltages_on_both_sides_of_the_threshold():
    table = districts.installed_density(_density_nodes(), {1: 0.25, 2: 2.0})
    auto = districts.district_voltages(table, PARAMS)
    assert auto["vn_kv"].to_dict() == {1: 10.0, 2: 20.0}                          # 8.12 and 0.23 MVA/km²
    above = districts.district_voltages(table, PARAMS.replace(mv_10kv_min_density_mva_km2=8.2))
    assert above["vn_kv"].to_dict() == {1: 20.0, 2: 20.0}
    low = districts.district_voltages(table, PARAMS.replace(mv_10kv_min_density_mva_km2=0.1))
    assert low["vn_kv"].to_dict() == {1: 10.0, 2: 10.0}
    fixed = districts.district_voltages(table, PARAMS.replace(vn_kv=20.0))
    assert fixed["vn_kv"].to_dict() == {1: 20.0, 2: 20.0}
    record = districts.voltage_record(auto.loc[1], PARAMS)
    assert record == {"rule": "auto", "vn_kv": 10.0, "density_mva_km2": pytest.approx(8.12),
                      "threshold_mva_km2": 5.0, "station_kva": 1030.0, "customer_kva": pytest.approx(1000.0),
                      "area_km2": 0.25, "n_stations": 2, "n_customers": 1}
    assert districts.voltage_record(fixed.loc[2], PARAMS.replace(vn_kv=20.0))["rule"] == "fixed"
    text = districts.describe_voltages(auto, PARAMS)
    assert "auto, 10 kV from 5 MVA/km²" in text and "1 at 20 kV, 1 at 10 kV" in text


def _municipal_nodes() -> pd.DataFrame:
    """The nodes of :func:`_density_nodes` in two municipalities: district 1 and the station without a district in
    the city "A", district 2 in "B" except its 190 kW customer, which lies in "A"."""
    return _density_nodes().assign(ags=["A", "A", "A", "B", "A", "B", "B", "A"])


def test_municipal_density_counts_every_node_of_the_municipality():
    table = districts.municipal_density(_municipal_nodes(), {"A": 0.5, "B": 2.0, "C": 0.0})
    assert table.index.tolist() == ["A", "B", "C"]
    assert list(table.columns) == list(districts.MUNICIPAL_DENSITY_COLUMNS)
    # A: 630 + 400 + 400 kVA (also the station without a district) + (950 + 190) kW / 0.95; B: 250 kVA only
    assert table.loc["A", "installed_mva"] == pytest.approx(1.43 + 1.2)
    assert table.loc["A", "density_mva_km2"] == pytest.approx(2.63 / 0.5)
    assert table.loc["B", "density_mva_km2"] == pytest.approx(0.25 / 2.0)    # no HV customer, no generator
    assert np.isnan(table.loc["C", "density_mva_km2"])


def test_dense_share_and_municipal_voltages():
    nodes = _municipal_nodes()
    munis = districts.municipal_density(nodes, {"A": 0.5, "B": 2.0})
    share = districts.dense_share(nodes, munis, 5.0)                       # A (5.26) is dense, B (0.13) not
    assert share.index.tolist() == [1, 2]
    assert share[1] == pytest.approx(1.0)
    assert share[2] == pytest.approx(200.0 / 450.0)                        # 200 of 250 + 200 kVA lie in A
    density = districts.installed_density(nodes, {1: 4.0, 2: 0.05})    # own cells: 0.51 and 9.0 MVA/km²
    table = districts.district_voltages(density, PARAMS, share)
    assert table["vn_kv"].to_dict() == {1: 10.0, 2: 20.0}                  # by the municipality, not the cell
    assert districts.district_voltages(density, PARAMS)["vn_kv"].to_dict() == {1: 20.0, 2: 10.0}   # by the cell
    record = districts.voltage_record(table.loc[1], PARAMS)
    assert record["scope"] == "municipality" and record["dense_share"] == pytest.approx(1.0)
    assert "scope" not in districts.voltage_record(districts.district_voltages(density, PARAMS).loc[1], PARAMS)
    fixed = districts.district_voltages(density, PARAMS.replace(vn_kv=20.0), share)
    assert fixed["vn_kv"].to_dict() == {1: 20.0, 2: 20.0}
    assert "in the municipality" in districts.describe_voltages(table, PARAMS)
