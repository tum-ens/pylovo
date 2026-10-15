"""Supply areas of the HV/MV substations (pure: numpy, pandas, SciPy and shapely; no database or configuration).

DESIGN §4.1. Two rules assign every MV node (station, customer, generator) to one HV/MV substation (site):

* :func:`routed_assignment` (``MV_SUPPLY_AREAS: routed``, default): an **additively weighted graph Voronoi** on the
  regional street graph (:func:`pylovo.mv_grid.graph.weighted_owner`): every node goes to the site with the
  smallest ``weight + street distance``. Cells are contiguous along shortest paths. The weights start at 0 and the
  sites above their (n-1) capacity are raised by ``rebalance_step_m`` until no site is overloaded or
  ``rebalance_max_iter`` raises are spent; the remaining overloads are reported (flag ``over_capacity``). Nodes
  without a street path to any site go to the Euclidean nearest site and are reported as ``bridged``.
* :func:`egon_assignment` (``MV_SUPPLY_AREAS: egon``): the eGon-data MV grid district polygon that contains the
  node; its depot is the site with the same eGon bus id.

The load of a site is the coincident load of its loads (pylovo's category law on the summed statistics of its
stations plus the fixed loads of its customers, :mod:`pylovo.mv_grid.coincidence`) divided by the power factor;
generators and excluded nodes (HV customers) do not count.

**Nominal voltage per district** (``MV_NOMINAL_VOLTAGE_KV`` auto, decision of 2026-10-15): after the supply areas
are formed, :func:`installed_density` gives each district its installed MV/LV capacity per km² of its area, and
:func:`district_voltages` gives 10 kV to the districts at or above ``mv_10kv_min_density_mva_km2`` and 20 kV to the
others (dense city grids run at 10 kV, rural grids at 20 kV).
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid.coincidence import INSTALLED_COLUMNS, UNITS_COLUMNS, coincident_kw
from pylovo.mv_grid.model import CATEGORIES

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import MvParameters

logger = logging.getLogger(__name__)

#: Node kinds whose load counts for the capacity of a site.
LOAD_KINDS: tuple[str, ...] = ("station", "customer")
#: Power factor that turns the corrected own peak of an MV customer (kW) into the rating of its own station (kVA)
#: in the installed density of :func:`installed_density`.
DENSITY_CUSTOMER_COS_PHI: float = 0.95
#: Columns of :func:`installed_density` (and of :func:`district_voltages`, which adds ``vn_kv``).
DENSITY_COLUMNS: tuple[str, ...] = ("n_stations", "n_customers", "station_kva", "customer_kva", "installed_mva",
                                    "area_km2", "density_mva_km2")


def _graph():
    """The routing module :mod:`pylovo.mv_grid.graph` (imported on use; the tests replace it with a fake)."""
    return importlib.import_module("pylovo.mv_grid.graph")


@dataclass
class Assignment:
    """Result of :func:`routed_assignment`.

    Attributes:
        site_of_node: Site index (row of ``sites``) per node row; -1 only if there is no site at all.
        weights_m: Final additive weight per site (m).
        iterations: Number of assignments computed (1 without rebalancing).
        load_mva: Coincident apparent load per site (MVA) of the final assignment.
        overloaded: Site indices whose load exceeds the capacity after the rebalancing.
        bridged: Per node row: no street path to any site (assigned to the Euclidean nearest site).
        capacity_mva: Capacity per site used for the rebalancing (MVA).
        distance_m: Weighted street distance of each node to its site (``inf`` for bridged nodes).
        history: Per iteration the number of overloaded sites and the largest load/capacity ratio.
    """

    site_of_node: np.ndarray
    weights_m: np.ndarray
    iterations: int
    load_mva: np.ndarray
    overloaded: list[int]
    bridged: np.ndarray
    capacity_mva: np.ndarray = field(default_factory=lambda: np.zeros(0))
    distance_m: np.ndarray = field(default_factory=lambda: np.zeros(0))
    history: list[tuple[int, float]] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------- capacity
def _ratings(value: Any) -> list[float]:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    if isinstance(value, (int, float, np.integer, np.floating)):
        return [float(value)] if float(value) > 0 else []
    try:
        items = list(value)
    except TypeError:
        return []
    out = []
    for item in items:
        try:
            number = float(item)
        except (TypeError, ValueError):
            continue
        if np.isfinite(number) and number > 0:
            out.append(number)
    return out


def site_capacity_mva(sites: pd.DataFrame, params: "MvParameters") -> np.ndarray:
    """(n-1) capacity of each HV/MV substation in MVA (DESIGN §4.1).

    With at least two rated OSM transformers (``osm_ratings_mva``): ``(units - 1) x min(ratings)``; else
    ``(hv_mv_units - 1) x max(hv_mv_sizes_mva)`` (63 MVA with the defaults); at least one unit counts (a
    one-transformer layout has no reserve). Both are scaled by ``hv_mv_n1_utilization`` (1.0 by default).

    Args:
        sites: One row per site, optionally with ``osm_ratings_mva`` (list of MVA or None).
        params: MV parameters.

    Returns:
        Capacity per site row (float64).
    """
    sites = pd.DataFrame(sites)
    default = max(int(params.hv_mv_units) - 1, 1) * max(params.hv_mv_sizes_mva)
    ratings = sites["osm_ratings_mva"] if "osm_ratings_mva" in sites.columns else pd.Series([None] * len(sites))
    capacity = []
    for value in ratings:
        units = _ratings(value)
        capacity.append(max(len(units) - 1, 1) * min(units) if len(units) >= 2 else default)
    return np.asarray(capacity, dtype=np.float64) * float(params.hv_mv_n1_utilization)


# ---------------------------------------------------------------------------------------------- helpers
def _column(frame: pd.DataFrame, name: str, default: float = np.nan) -> np.ndarray:
    if name not in frame.columns:
        return np.full(len(frame), default, dtype=np.float64)
    return pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=np.float64, na_value=np.nan)


def _edge_ends(frame: pd.DataFrame, vertex_index: Mapping[int, int], edge_len: Mapping[int, float]
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """CSR rows of the attach edge ends, the costs to them, the edge id, the fraction and the edge length.

    Needs ``attach_edge_id``, ``attach_fraction``, ``attach_offset_m`` and the graph vertex ids of the edge ends
    ``edge_source``, ``edge_target`` (the fraction runs from source to target); ``edge_length_m`` is taken from
    ``edge_len`` when the column is missing. Rows without an attachment get -1 / ``inf``.
    """
    n = len(frame)
    edge = _column(frame, "attach_edge_id")
    fraction = np.clip(np.nan_to_num(_column(frame, "attach_fraction"), nan=0.0), 0.0, 1.0)
    offset = np.nan_to_num(_column(frame, "attach_offset_m"), nan=0.0).clip(min=0.0)
    length = _column(frame, "edge_length_m")
    missing_length = ~np.isfinite(length) & np.isfinite(edge)
    if missing_length.any():
        length[missing_length] = [float(edge_len.get(int(e), np.nan)) for e in edge[missing_length]]
    source = _column(frame, "edge_source")
    target = _column(frame, "edge_target")
    u = np.full(n, -1, dtype=np.int64)
    v = np.full(n, -1, dtype=np.int64)
    for i in range(n):
        if np.isfinite(edge[i]) and np.isfinite(source[i]) and np.isfinite(target[i]) and np.isfinite(length[i]):
            u[i] = vertex_index.get(int(source[i]), -1)
            v[i] = vertex_index.get(int(target[i]), -1)
    attached = (u >= 0) | (v >= 0)
    cost_u = np.where(u >= 0, offset + fraction * np.nan_to_num(length, nan=0.0), np.inf)
    cost_v = np.where(v >= 0, offset + (1.0 - fraction) * np.nan_to_num(length, nan=0.0), np.inf)
    edge = np.where(attached, edge, np.nan)
    return u, v, cost_u, cost_v, edge, np.column_stack([fraction, offset, np.nan_to_num(length, nan=0.0)])


def _owner(dist: np.ndarray, site: np.ndarray, u: np.ndarray, v: np.ndarray, cost_u: np.ndarray,
           cost_v: np.ndarray, direct_cost: np.ndarray, direct_site: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Owning site and weighted distance per node: cheapest of the two edge ends and a site on the same edge.

    Ties go to the lower site index.
    """
    n = len(u)
    big = np.iinfo(np.int64).max
    cand_cost = np.full((n, 3), np.inf)
    cand_site = np.full((n, 3), big, dtype=np.int64)
    for k, (ends, cost) in enumerate(((u, cost_u), (v, cost_v))):
        ok = ends >= 0
        owner = np.full(n, -1, dtype=np.int64)
        owner[ok] = site[ends[ok]]
        ok &= owner >= 0
        cand_cost[ok, k] = dist[ends[ok]] + cost[ok]
        cand_site[ok, k] = owner[ok]
    ok = direct_site >= 0
    cand_cost[ok, 2] = direct_cost[ok]
    cand_site[ok, 2] = direct_site[ok]
    order = np.lexsort((cand_site, cand_cost), axis=1) if n else np.zeros((0, 3), dtype=np.int64)
    best = order[:, 0] if n else np.zeros(0, dtype=np.int64)
    rows = np.arange(n)
    best_cost = cand_cost[rows, best]
    best_site = np.where(np.isfinite(best_cost), cand_site[rows, best], -1)
    return best_site.astype(np.int64), best_cost


def _same_edge(node_edge: np.ndarray, node_geo: np.ndarray, site_edge: np.ndarray, site_geo: np.ndarray
               ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Node/site pairs on the same attach edge and their unweighted direct cost ``o_s + |f_t - f_s| L + o_t``."""
    sites_by_edge: dict[int, list[int]] = {}
    for s, e in enumerate(site_edge):
        if np.isfinite(e):
            sites_by_edge.setdefault(int(e), []).append(s)
    pair_node, pair_site, pair_cost = [], [], []
    if sites_by_edge:
        for t, e in enumerate(node_edge):
            if not np.isfinite(e):
                continue
            for s in sites_by_edge.get(int(e), ()):
                pair_node.append(t)
                pair_site.append(s)
                pair_cost.append(site_geo[s, 1] + abs(node_geo[t, 0] - site_geo[s, 0]) * node_geo[t, 2]
                                 + node_geo[t, 1])
    return (np.asarray(pair_node, dtype=np.int64), np.asarray(pair_site, dtype=np.int64),
            np.asarray(pair_cost, dtype=np.float64))


def _direct(n: int, pair_node: np.ndarray, pair_site: np.ndarray, pair_cost: np.ndarray, weights: np.ndarray
            ) -> tuple[np.ndarray, np.ndarray]:
    """Cheapest weighted same-edge cost and its site per node (ties: lower site index)."""
    cost = np.full(n, np.inf)
    site = np.full(n, -1, dtype=np.int64)
    if len(pair_node):
        weighted = pair_cost + weights[pair_site]
        order = np.lexsort((pair_site, weighted, pair_node))
        first = np.ones(len(order), dtype=bool)
        first[1:] = pair_node[order][1:] != pair_node[order][:-1]
        chosen = order[first]
        cost[pair_node[chosen]] = weighted[chosen]
        site[pair_node[chosen]] = pair_site[chosen]
    return cost, site


def site_loads_mva(owner: np.ndarray, installed: np.ndarray, units: np.ndarray, fixed_kw: np.ndarray,
                   counts: np.ndarray, n_sites: int, g: np.ndarray, power_factor: float) -> np.ndarray:
    """Coincident apparent load per site (MVA): the category law on the summed statistics plus the fixed loads,
    divided by the power factor. Nodes with ``counts`` False or without a site are left out."""
    use = counts & (owner >= 0)
    index = owner[use]
    inst = np.stack([np.bincount(index, weights=installed[use, c], minlength=n_sites)
                     for c in range(len(CATEGORIES))], axis=1) if n_sites else np.zeros((0, len(CATEGORIES)))
    cnt = np.stack([np.bincount(index, weights=units[use, c], minlength=n_sites)
                    for c in range(len(CATEGORIES))], axis=1) if n_sites else np.zeros((0, len(CATEGORIES)))
    fixed = np.bincount(index, weights=fixed_kw[use], minlength=n_sites) if n_sites else np.zeros(0)
    kw = np.asarray(coincident_kw(inst, cnt, g), dtype=np.float64).reshape(n_sites) + fixed
    return kw / float(power_factor) / 1000.0


def _nearest_site(xy: np.ndarray, site_xy: np.ndarray) -> np.ndarray:
    """Euclidean nearest site per point (-1 without sites or for points without coordinates)."""
    out = np.full(len(xy), -1, dtype=np.int64)
    ok_sites = np.isfinite(site_xy).all(axis=1)
    ok = np.isfinite(xy).all(axis=1)
    if not len(xy) or not ok_sites.any() or not ok.any():
        return out
    from scipy.spatial import cKDTree

    site_rows = np.flatnonzero(ok_sites)
    _, idx = cKDTree(site_xy[ok_sites]).query(xy[ok])
    out[ok] = site_rows[np.asarray(idx, dtype=np.int64)]
    return out


# ---------------------------------------------------------------------------------------------- routed
def routed_assignment(csr, vertex_index: dict[int, int], edge_len: Mapping[int, float], sites: pd.DataFrame,
                      nodes: pd.DataFrame, g: np.ndarray, power_factor: float, capacity_mva: np.ndarray,
                      params: "MvParameters") -> Assignment:
    """Assign nodes to sites by the additively weighted graph Voronoi with capacity rebalancing (DESIGN §4.1).

    A site reaches a node through the ends of the node's attach edge (``dist[end] + part of the edge + offset``)
    or, when both lie on the same edge, directly along it. Per iteration the sites whose coincident load (see
    :func:`site_loads_mva`) exceeds ``capacity_mva`` get ``rebalance_step_m`` more weight; at most
    ``rebalance_max_iter`` raises. Generators and nodes with ``connection_hint == 'excluded'`` are assigned but do
    not count for the capacity.

    Args:
        csr: Region CSR (:func:`pylovo.mv_grid.graph.region_csr`).
        vertex_index: Graph vertex id -> CSR row.
        edge_len: Edge id -> length (m), used where ``edge_length_m`` is missing.
        sites: One row per usable site: ``attach_edge_id``, ``attach_fraction``, ``attach_offset_m``,
            ``edge_source``, ``edge_target`` (graph vertex ids of the attach edge), optional ``edge_length_m``,
            and ``x``, ``y``.
        nodes: One row per node in :data:`~pylovo.mv_grid.model.NODE_COLUMNS` form (``kind``,
            ``connection_hint``, ``x``, ``y``, ``inst_*``, ``units_*``, ``p_fixed_kw``, ``attach_*``) plus
            ``edge_source``, ``edge_target`` (and optional ``edge_length_m``).
        g: Sim factors in CATEGORIES order (LV version).
        power_factor: cos phi of the loads (LV version).
        capacity_mva: Capacity per site (:func:`site_capacity_mva`).
        params: MV parameters (``rebalance_step_m``, ``rebalance_max_iter``).

    Returns:
        The :class:`Assignment`.

    Raises:
        ValueError: If ``capacity_mva`` does not have one value per site or the power factor is not in (0, 1].
    """
    sites = pd.DataFrame(sites).reset_index(drop=True)
    nodes = pd.DataFrame(nodes)
    n_sites = len(sites)
    n = len(nodes)
    capacity = np.asarray(capacity_mva, dtype=np.float64)
    if capacity.shape != (n_sites,):
        raise ValueError(f"capacity_mva must have one value per site ({n_sites},), got shape {capacity.shape}")
    if not 0.0 < float(power_factor) <= 1.0:
        raise ValueError(f"power_factor must be in (0, 1], got {power_factor!r}")
    g = np.asarray(g, dtype=np.float64)
    site_u, site_v, site_cu, site_cv, site_edge, site_geo = _edge_ends(sites, vertex_index, edge_len)
    node_u, node_v, node_cu, node_cv, node_edge, node_geo = _edge_ends(nodes, vertex_index, edge_len)
    # site-side costs to the edge ends without the weight; node-side costs from the ends to the node
    site_cost_u = np.where(site_u >= 0, site_cu, 0.0)
    site_cost_v = np.where(site_v >= 0, site_cv, 0.0)
    pair_node, pair_site, pair_cost = _same_edge(node_edge, node_geo, site_edge, site_geo)
    installed = np.column_stack([_column(nodes, c, 0.0) for c in INSTALLED_COLUMNS]) if n \
        else np.zeros((0, len(CATEGORIES)))
    units = np.column_stack([_column(nodes, c, 0.0) for c in UNITS_COLUMNS]) if n else np.zeros((0, len(CATEGORIES)))
    installed = np.where(np.isfinite(installed), installed, 0.0)
    units = np.where(np.isfinite(units), units, 0.0)
    fixed = np.nan_to_num(_column(nodes, "p_fixed_kw", 0.0), nan=0.0)
    kind = nodes["kind"].astype(object).to_numpy() if "kind" in nodes.columns else np.full(n, "station", dtype=object)
    hint = nodes["connection_hint"].astype(object).to_numpy() if "connection_hint" in nodes.columns \
        else np.full(n, "", dtype=object)
    counts = np.isin(kind, LOAD_KINDS) & (hint != "excluded")
    node_xy = np.column_stack([_column(nodes, "x"), _column(nodes, "y")]) if n else np.zeros((0, 2))
    site_xy = np.column_stack([_column(sites, "x"), _column(sites, "y")]) if n_sites else np.zeros((0, 2))
    nearest = _nearest_site(node_xy, site_xy)
    weights = np.zeros(n_sites, dtype=np.float64)
    graph = _graph()
    history: list[tuple[int, float]] = []
    max_raises = max(int(params.rebalance_max_iter), 0)
    iterations = 0
    while True:
        iterations += 1
        if n_sites:
            dist, site = graph.weighted_owner(csr, site_u, site_v, site_cost_u, site_cost_v, weights)
            dist = np.asarray(dist, dtype=np.float64)
            site = np.asarray(site, dtype=np.int64)
        else:
            dist = np.full(csr.shape[0], np.inf)
            site = np.full(csr.shape[0], -1, dtype=np.int64)
        direct_cost, direct_site = _direct(n, pair_node, pair_site, pair_cost, weights)
        owner, distance = _owner(dist, site, node_u, node_v, node_cu, node_cv, direct_cost, direct_site)
        bridged = owner < 0
        owner = np.where(bridged, nearest, owner)
        load = site_loads_mva(owner, installed, units, fixed, counts, n_sites, g, power_factor)
        over = load > capacity * (1.0 + 1e-12)
        ratio = float(np.max(load / np.where(capacity > 0, capacity, np.inf))) if n_sites else 0.0
        history.append((int(over.sum()), ratio))
        if not over.any() or iterations > max_raises:
            break
        weights = weights + np.where(over, float(params.rebalance_step_m), 0.0)
    overloaded = [int(s) for s in np.flatnonzero(over)]
    if overloaded:
        logger.warning(f"{len(overloaded)} HV/MV substations stay above their capacity after {iterations - 1} "
                       "rebalancing steps: " + ", ".join(f"{s} ({load[s]:.1f}/{capacity[s]:.1f} MVA)"
                                                         for s in overloaded))
    if bridged.any():
        logger.info(f"{int(bridged.sum())} MV nodes have no street path to a substation (Euclidean nearest)")
    return Assignment(site_of_node=owner.astype(np.int64), weights_m=weights, iterations=iterations,
                      load_mva=load, overloaded=overloaded, bridged=bridged, capacity_mva=capacity,
                      distance_m=np.where(bridged, np.inf, distance), history=history)


# ---------------------------------------------------------------------------------------------- nominal voltage
def installed_density(nodes: pd.DataFrame, areas_km2: Mapping[int, float]) -> pd.DataFrame:
    """The installed MV/LV capacity per km² of each district, the measure of ``MV_NOMINAL_VOLTAGE_KV`` auto.

    ``installed = Σ station ratings (kVA) + Σ corrected own peak (kW) / 0.95`` of the MV customer nodes, divided
    by the district's area: the station ratings of the MV model (``sn_kva``; a station overloaded by its
    station-busbar customers carries its upsized rating, and those customers count through it), the corrected own
    peak of a customer is its ``installed_kw`` after the hall rule (pylovo's law gives factor 1 for one building)
    over :data:`DENSITY_CUSTOMER_COS_PHI`. HV customers (``connection_hint`` excluded) and generators do not
    count. The area is the district's Voronoi cell clipped to the planning region (``mv_grid.area_km2``), so the
    areas of all districts add up to the region.

    Args:
        nodes: Node rows with ``mv_grid_id`` (NaN: not assigned), ``kind``, ``connection_hint``, ``sn_kva``
            (stations) and ``installed_kw`` (customers); missing columns count as 0.
        areas_km2: Area per ``mv_grid_id`` in km².

    Returns:
        Index ``mv_grid_id`` (every district of ``areas_km2`` or ``nodes``, ascending), :data:`DENSITY_COLUMNS`:
        node counts, ``station_kva``, ``customer_kva``, ``installed_mva``, ``area_km2`` and ``density_mva_km2``
        (NaN without a positive finite area).
    """
    frame = pd.DataFrame(nodes)
    grid = pd.to_numeric(frame["mv_grid_id"], errors="coerce") if "mv_grid_id" in frame.columns \
        else pd.Series(np.nan, index=frame.index)
    kind = frame["kind"].astype(object) if "kind" in frame.columns else pd.Series(None, index=frame.index)
    hint = frame["connection_hint"].astype(object) if "connection_hint" in frame.columns \
        else pd.Series(None, index=frame.index)

    def numbers(column: str) -> pd.Series:
        if column not in frame.columns:
            return pd.Series(0.0, index=frame.index)
        values = pd.to_numeric(frame[column], errors="coerce")
        return values.where(np.isfinite(values) & (values > 0), 0.0)

    station = kind.eq("station") & grid.notna()
    customer = kind.eq("customer") & ~hint.eq("excluded") & grid.notna()
    key = grid.fillna(-1).astype(np.int64)                  # -1: not assigned (never a district id)
    ids = sorted({int(g) for g in areas_km2} | {int(g) for g in key[station | customer]})
    out = pd.DataFrame(index=pd.Index(ids, name="mv_grid_id", dtype="int64"))

    def total(values: pd.Series) -> pd.Series:
        return values.groupby(key).sum().reindex(out.index, fill_value=0)

    out["n_stations"] = total(station.astype("int64")).astype("int64")
    out["n_customers"] = total(customer.astype("int64")).astype("int64")
    out["station_kva"] = total(numbers("sn_kva").where(station, 0.0)).astype(float)
    out["customer_kva"] = total(numbers("installed_kw").where(customer, 0.0) / DENSITY_CUSTOMER_COS_PHI).astype(float)
    out["installed_mva"] = (out["station_kva"] + out["customer_kva"]) / 1000.0
    area = pd.Series({int(g): float(a) if a is not None else np.nan for g, a in areas_km2.items()}, dtype=float)
    out["area_km2"] = area.reindex(out.index).astype(float)
    valid = np.isfinite(out["area_km2"]) & (out["area_km2"] > 0)
    out["density_mva_km2"] = (out["installed_mva"] / out["area_km2"].where(valid)).astype(float)
    return out[list(DENSITY_COLUMNS)]


def district_voltages(density: pd.DataFrame, params: "MvParameters") -> pd.DataFrame:
    """The nominal voltage of every district (``params.district_kv`` of its installed density).

    With ``MV_NOMINAL_VOLTAGE_KV`` auto: 10 kV at a density of at least ``mv_10kv_min_density_mva_km2``, else
    20 kV (also for a district without a finite density); otherwise the fixed voltage.

    Returns:
        ``density`` with the column ``vn_kv``.
    """
    out = pd.DataFrame(density).copy()
    out["vn_kv"] = [params.district_kv(d) for d in out["density_mva_km2"]]
    return out


def voltage_record(row: Mapping[str, Any], params: "MvParameters") -> dict:
    """The ``mv_grid.metrics['voltage_level']`` entry of a district (JSON-native): ``rule`` (``auto`` or
    ``fixed``), ``vn_kv``, ``density_mva_km2``, ``threshold_mva_km2`` and the parts of the density."""
    def number(value: Any) -> float | None:
        value = float(value)
        return value if np.isfinite(value) else None

    return {"rule": "auto" if params.auto_voltage else "fixed", "vn_kv": float(row["vn_kv"]),
            "density_mva_km2": number(row["density_mva_km2"]),
            "threshold_mva_km2": float(params.mv_10kv_min_density_mva_km2),
            "station_kva": number(row["station_kva"]), "customer_kva": number(row["customer_kva"]),
            "area_km2": number(row["area_km2"]), "n_stations": int(row["n_stations"]),
            "n_customers": int(row["n_customers"])}


def describe_voltages(table: pd.DataFrame, params: "MvParameters") -> str:
    """One log line of :func:`district_voltages`: the districts per nominal voltage and their density range."""
    if not len(table):
        return "Nominal voltage: no districts"
    density = table["density_mva_km2"].astype(float)
    counts = table["vn_kv"].astype(float).value_counts()
    parts = [f"{int(counts[vn])} at {vn:g} kV" for vn in sorted(counts.index, reverse=True)]
    rule = (f"auto, 10 kV from {params.mv_10kv_min_density_mva_km2:g} MVA/km²" if params.auto_voltage
            else f"fixed {float(params.vn_kv):g} kV")
    text = f"Nominal voltage ({rule}): {len(table)} districts, {', '.join(parts)}"
    if density.notna().any():
        text += (f"; installed density {density.min():.2f} to {density.max():.2f} MVA/km² "
                 f"(median {density.median():.2f})")
    return text


# ---------------------------------------------------------------------------------------------- eGon
def _polygons(districts: pd.DataFrame) -> list[Any]:
    import shapely

    geoms = []
    column = "geom_wkb" if "geom_wkb" in districts.columns else "geom"
    for value in districts[column]:
        if value is None:
            geoms.append(None)
        elif isinstance(value, (bytes, bytearray, memoryview)):
            geoms.append(shapely.from_wkb(bytes(value)))
        elif isinstance(value, str):
            geoms.append(shapely.from_wkb(value) if all(c in "0123456789abcdefABCDEF" for c in value[:16])
                         else shapely.from_wkt(value))
        else:
            geoms.append(value)
    return geoms


def egon_assignment(nodes_xy: np.ndarray, districts: pd.DataFrame, site_bus_ids: Sequence[int]) -> np.ndarray:
    """Assign nodes to sites by the eGon-data MV grid districts (``MV_SUPPLY_AREAS: egon``).

    A node goes to the district polygon that contains it (several: the lowest bus id), else to the nearest district
    polygon (logged as ``outside_egon``); the depot is the site with the district's bus id. Only districts whose
    bus id has a site take part, so a district without a site (its substation outside the search buffer) hands
    its nodes to the nearest district that has one.

    Args:
        nodes_xy: (n, 2) node positions (``TARGET_EPSG``).
        districts: Rows of :meth:`MvSourceMixin.egon_districts` (``bus_id``, ``geom_wkb``; shapely in ``geom`` is
            accepted too).
        site_bus_ids: eGon bus id per site row (-1 or None: no eGon substation).

    Returns:
        Site index per node (int64); -1 only where no district has a site.
    """
    import shapely

    xy = np.asarray(nodes_xy, dtype=np.float64).reshape(-1, 2)
    n = len(xy)
    bus_of_site = [int(b) if b is not None and not (isinstance(b, float) and np.isnan(b)) and int(b) >= 0 else None
                   for b in site_bus_ids]
    site_of_bus: dict[int, int] = {}
    for s, bus in enumerate(bus_of_site):
        if bus is not None and bus not in site_of_bus:
            site_of_bus[bus] = s
    owner = np.full(n, -1, dtype=np.int64)
    if not n or not bus_of_site:
        return owner
    frame = pd.DataFrame(districts).reset_index(drop=True)
    geoms = _polygons(frame) if len(frame) else []
    bus_ids = [int(b) for b in frame["bus_id"]] if len(frame) else []
    usable = [i for i, (geom, bus) in enumerate(zip(geoms, bus_ids)) if geom is not None and bus in site_of_bus]
    points = shapely.points(xy)
    if usable:
        tree = shapely.STRtree([geoms[i] for i in usable])
        node_idx, poly_idx = tree.query(points, predicate="within")
        best: dict[int, int] = {}
        for t, p in zip(node_idx.tolist(), poly_idx.tolist()):
            bus = bus_ids[usable[p]]
            if t not in best or bus < best[t]:
                best[t] = bus
        for t, bus in best.items():
            owner[t] = site_of_bus[bus]
        outside = np.flatnonzero(owner < 0)
        if len(outside):
            nearest = tree.query_nearest(points[outside], all_matches=False)
            for t_local, p in zip(nearest[0].tolist(), nearest[1].tolist()):
                owner[outside[t_local]] = site_of_bus[bus_ids[usable[p]]]
            logger.info(f"{len(outside)} MV nodes lie outside every eGon MV grid district with a substation "
                        "(outside_egon): nearest district")
    rest = owner < 0
    if rest.any():
        logger.warning(f"{int(rest.sum())} MV nodes have no eGon MV grid district with a substation (unassigned)")
    return owner
