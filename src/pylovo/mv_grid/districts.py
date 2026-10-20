"""Supply areas of the HV/MV substations (pure: numpy, pandas, SciPy and shapely; no database or configuration).

DESIGN §4.1. Three rules assign every MV node (station, customer, generator) to one HV/MV substation (site):

* :func:`routed_assignment` (``MV_SUPPLY_AREAS: routed``, default): an **additively weighted graph Voronoi** on the
  regional street graph (:func:`pylovo.mv_grid.graph.weighted_owner`): every node goes to the site with the
  smallest ``weight + street distance``. Cells are contiguous along shortest paths. The weights are 0; with
  ``rebalance_max_iter`` above 0 (off by default), the sites above their (n-1) capacity are raised by
  ``rebalance_step_m`` until no site is overloaded or the raises are spent.
* :func:`municipal_assignment` (``MV_SUPPLY_AREAS: municipal``): municipalities stay whole, as in Ding0's MV grid
  districts (Hülk et al. 2017). A municipality with one site goes to that site, one with several sites is split
  among them by the routed Voronoi of those sites, and one without a site goes as a whole to the site that the
  routed Voronoi gives the largest part of its load.
* :func:`egon_assignment` (``MV_SUPPLY_AREAS: egon``): the eGon-data MV grid district polygon that contains the
  node; its depot is the site with the same eGon bus id.

The routed rules report the sites above their (n-1) capacity (flag ``over_capacity``) and never move load away from
them: the sizing gives such a site more transformer units. Nodes without a street path to any site go to the
Euclidean nearest site and are reported as ``bridged``.

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
        distance_m: Weighted street distance of each node to its site (``inf`` for bridged nodes, NaN for nodes that
            the municipal rule moved away from their nearest site).
        history: Per iteration the number of overloaded sites and the largest load/capacity ratio.
        moved: Per node row: the municipal rule gave it another site than its nearest one.
        summary: Counts of the municipal rule (``municipalities_one_site``, ``municipalities_split``,
            ``municipalities_no_site``, ``nodes_without_municipality``, ``nodes_moved``).
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
    moved: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool))
    summary: dict = field(default_factory=dict)


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
@dataclass
class _Routed:
    """The arrays of one routed assignment (:func:`_routed_problem`): sites, nodes and their attach edges."""

    csr: Any
    site_u: np.ndarray
    site_v: np.ndarray
    site_cost_u: np.ndarray
    site_cost_v: np.ndarray
    node_u: np.ndarray
    node_v: np.ndarray
    node_cost_u: np.ndarray
    node_cost_v: np.ndarray
    pair_node: np.ndarray
    pair_site: np.ndarray
    pair_cost: np.ndarray
    installed: np.ndarray
    units: np.ndarray
    fixed: np.ndarray
    counts: np.ndarray
    node_xy: np.ndarray
    site_xy: np.ndarray
    g: np.ndarray
    power_factor: float

    @property
    def n_sites(self) -> int:
        return len(self.site_u)

    def owner(self, weights: np.ndarray, active: np.ndarray | None = None
              ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """``(site, weighted distance, bridged)`` per node by ``weight + street distance`` among the ``active``
        sites (bool per site; default all). A node without a street path to an active site goes to the Euclidean
        nearest active site (``bridged``)."""
        n = len(self.node_u)
        active = np.ones(self.n_sites, dtype=bool) if active is None else np.asarray(active, dtype=bool)
        if self.n_sites and active.any():
            site_u = np.where(active, self.site_u, -1)
            site_v = np.where(active, self.site_v, -1)
            dist, site = _graph().weighted_owner(self.csr, site_u, site_v, self.site_cost_u, self.site_cost_v,
                                                 weights)
            dist = np.asarray(dist, dtype=np.float64)
            site = np.asarray(site, dtype=np.int64)
        else:
            dist = np.full(self.csr.shape[0], np.inf)
            site = np.full(self.csr.shape[0], -1, dtype=np.int64)
        keep = active[self.pair_site] if len(self.pair_site) else np.zeros(0, dtype=bool)
        direct_cost, direct_site = _direct(n, self.pair_node[keep], self.pair_site[keep], self.pair_cost[keep],
                                           weights)
        owner, distance = _owner(dist, site, self.node_u, self.node_v, self.node_cost_u, self.node_cost_v,
                                 direct_cost, direct_site)
        bridged = owner < 0
        nearest = _nearest_site(self.node_xy, np.where(active[:, None], self.site_xy, np.nan))
        return np.where(bridged, nearest, owner), distance, bridged

    def loads(self, owner: np.ndarray) -> np.ndarray:
        """Coincident apparent load per site (MVA) of an assignment (:func:`site_loads_mva`)."""
        return site_loads_mva(owner, self.installed, self.units, self.fixed, self.counts, self.n_sites, self.g,
                              self.power_factor)


def _routed_problem(csr, vertex_index: dict[int, int], edge_len: Mapping[int, float], sites: pd.DataFrame,
                    nodes: pd.DataFrame, g: np.ndarray, power_factor: float) -> _Routed:
    """The arrays of a routed assignment (see :func:`routed_assignment` for the inputs)."""
    sites = pd.DataFrame(sites).reset_index(drop=True)
    nodes = pd.DataFrame(nodes)
    n_sites = len(sites)
    n = len(nodes)
    if not 0.0 < float(power_factor) <= 1.0:
        raise ValueError(f"power_factor must be in (0, 1], got {power_factor!r}")
    site_u, site_v, site_cu, site_cv, site_edge, site_geo = _edge_ends(sites, vertex_index, edge_len)
    node_u, node_v, node_cu, node_cv, node_edge, node_geo = _edge_ends(nodes, vertex_index, edge_len)
    pair_node, pair_site, pair_cost = _same_edge(node_edge, node_geo, site_edge, site_geo)
    installed = np.column_stack([_column(nodes, c, 0.0) for c in INSTALLED_COLUMNS]) if n \
        else np.zeros((0, len(CATEGORIES)))
    units = np.column_stack([_column(nodes, c, 0.0) for c in UNITS_COLUMNS]) if n else np.zeros((0, len(CATEGORIES)))
    kind = nodes["kind"].astype(object).to_numpy() if "kind" in nodes.columns else np.full(n, "station", dtype=object)
    hint = nodes["connection_hint"].astype(object).to_numpy() if "connection_hint" in nodes.columns \
        else np.full(n, "", dtype=object)
    return _Routed(
        csr=csr,
        # site-side costs to the edge ends without the weight; node-side costs from the ends to the node
        site_u=site_u, site_v=site_v, site_cost_u=np.where(site_u >= 0, site_cu, 0.0),
        site_cost_v=np.where(site_v >= 0, site_cv, 0.0),
        node_u=node_u, node_v=node_v, node_cost_u=node_cu, node_cost_v=node_cv,
        pair_node=pair_node, pair_site=pair_site, pair_cost=pair_cost,
        installed=np.where(np.isfinite(installed), installed, 0.0), units=np.where(np.isfinite(units), units, 0.0),
        fixed=np.nan_to_num(_column(nodes, "p_fixed_kw", 0.0), nan=0.0),
        counts=np.isin(kind, LOAD_KINDS) & (hint != "excluded"),
        node_xy=np.column_stack([_column(nodes, "x"), _column(nodes, "y")]) if n else np.zeros((0, 2)),
        site_xy=np.column_stack([_column(sites, "x"), _column(sites, "y")]) if n_sites else np.zeros((0, 2)),
        g=np.asarray(g, dtype=np.float64), power_factor=float(power_factor))


def _check_capacity(capacity_mva: np.ndarray, n_sites: int) -> np.ndarray:
    capacity = np.asarray(capacity_mva, dtype=np.float64)
    if capacity.shape != (n_sites,):
        raise ValueError(f"capacity_mva must have one value per site ({n_sites},), got shape {capacity.shape}")
    return capacity


def _overloads(load: np.ndarray, capacity: np.ndarray) -> tuple[np.ndarray, float]:
    over = load > capacity * (1.0 + 1e-12)
    ratio = float(np.max(load / np.where(capacity > 0, capacity, np.inf))) if len(load) else 0.0
    return over, ratio


def _log_result(overloaded: list[int], load: np.ndarray, capacity: np.ndarray, bridged: np.ndarray,
                steps: int) -> None:
    if overloaded:
        after = f" after {steps} rebalancing steps" if steps else ""
        logger.warning(f"{len(overloaded)} HV/MV substations are above their (n-1) capacity{after} and get more "
                       "transformer units: " + ", ".join(f"{s} ({load[s]:.1f}/{capacity[s]:.1f} MVA)"
                                                         for s in overloaded))
    if bridged.any():
        logger.info(f"{int(bridged.sum())} MV nodes have no street path to a substation (Euclidean nearest)")


def routed_assignment(csr, vertex_index: dict[int, int], edge_len: Mapping[int, float], sites: pd.DataFrame,
                      nodes: pd.DataFrame, g: np.ndarray, power_factor: float, capacity_mva: np.ndarray,
                      params: "MvParameters") -> Assignment:
    """Assign nodes to sites by the additively weighted graph Voronoi (DESIGN §4.1).

    A site reaches a node through the ends of the node's attach edge (``dist[end] + part of the edge + offset``)
    or, when both lie on the same edge, directly along it. With ``rebalance_max_iter`` above 0, the sites whose
    coincident load (see :func:`site_loads_mva`) exceeds ``capacity_mva`` get ``rebalance_step_m`` more weight per
    iteration, at most ``rebalance_max_iter`` times; by default (0) every node goes to its nearest site.
    Generators and nodes with ``connection_hint == 'excluded'`` are assigned but do not count for the capacity.

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
    capacity = _check_capacity(capacity_mva, len(sites))
    problem = _routed_problem(csr, vertex_index, edge_len, sites, nodes, g, power_factor)
    weights = np.zeros(problem.n_sites, dtype=np.float64)
    history: list[tuple[int, float]] = []
    max_raises = max(int(params.rebalance_max_iter), 0)
    iterations = 0
    while True:
        iterations += 1
        owner, distance, bridged = problem.owner(weights)
        load = problem.loads(owner)
        over, ratio = _overloads(load, capacity)
        history.append((int(over.sum()), ratio))
        if not over.any() or iterations > max_raises:
            break
        weights = weights + np.where(over, float(params.rebalance_step_m), 0.0)
    overloaded = [int(s) for s in np.flatnonzero(over)]
    _log_result(overloaded, load, capacity, bridged, iterations - 1)
    return Assignment(site_of_node=owner.astype(np.int64), weights_m=weights, iterations=iterations,
                      load_mva=load, overloaded=overloaded, bridged=bridged, capacity_mva=capacity,
                      distance_m=np.where(bridged, np.inf, distance), history=history,
                      moved=np.zeros(len(owner), dtype=bool))


def _codes(frame: pd.DataFrame, column: str) -> np.ndarray:
    """The text codes of a column (None where missing or empty)."""
    if column not in frame.columns:
        return np.full(len(frame), None, dtype=object)
    return np.array([None if v is None or (isinstance(v, float) and np.isnan(v)) or str(v).strip() == ""
                     else str(v).strip() for v in frame[column]], dtype=object)


def municipal_assignment(csr, vertex_index: dict[int, int], edge_len: Mapping[int, float], sites: pd.DataFrame,
                         nodes: pd.DataFrame, g: np.ndarray, power_factor: float, capacity_mva: np.ndarray
                         ) -> Assignment:
    """Assign nodes to sites so that municipalities stay whole (``MV_SUPPLY_AREAS: municipal``, DESIGN §4.1).

    The rule of Ding0's MV grid districts (Hülk et al. 2017), with street distances instead of Voronoi polygons:

    1. a municipality with exactly one site goes to that site;
    2. a municipality with several sites is split among them by the routed Voronoi of those sites alone;
    3. a municipality without a site goes as a whole to the site that the routed Voronoi of all sites gives the
       largest part of its coincident load (more nodes, then the lower site index, on a tie).

    Nodes without a municipality keep their nearest site. The load is never moved for capacity: the sites above
    ``capacity_mva`` are reported (``overloaded``) and get more units in the sizing.

    Args:
        sites: As in :func:`routed_assignment`, plus ``ags``: the municipality of the site (missing: none, e.g. a
            site outside the municipalities of the graph).
        nodes: As in :func:`routed_assignment`, plus ``ags``: the municipality of the node.
        Others: As in :func:`routed_assignment`.

    Returns:
        The :class:`Assignment` with ``moved`` (nodes that are not at their nearest site) and ``summary``.

    Raises:
        ValueError: If ``capacity_mva`` does not have one value per site or the power factor is not in (0, 1].
    """
    capacity = _check_capacity(capacity_mva, len(sites))
    problem = _routed_problem(csr, vertex_index, edge_len, sites, nodes, g, power_factor)
    zero = np.zeros(problem.n_sites, dtype=np.float64)
    nearest, distance, bridged = problem.owner(zero)
    owner = nearest.copy()
    node_ags = _codes(pd.DataFrame(nodes), "ags")
    site_ags = _codes(pd.DataFrame(sites).reset_index(drop=True), "ags")
    summary = {"municipalities_one_site": 0, "municipalities_split": 0, "municipalities_no_site": 0,
               "nodes_without_municipality": int(sum(a is None for a in node_ags)), "nodes_moved": 0}
    for ags in sorted({a for a in node_ags if a is not None}):
        rows = np.flatnonzero(node_ags == ags)
        own = np.flatnonzero(site_ags == ags)
        if len(own) == 1:
            owner[rows] = own[0]
            summary["municipalities_one_site"] += 1
        elif len(own) > 1:
            active = np.zeros(problem.n_sites, dtype=bool)
            active[own] = True
            owner[rows] = problem.owner(zero, active)[0][rows]
            summary["municipalities_split"] += 1
        else:
            near = nearest[rows]
            if not (near >= 0).any():
                continue
            part = np.full(len(nearest), -1, dtype=np.int64)
            part[rows] = near
            load = problem.loads(part)
            count = np.bincount(near[near >= 0], minlength=problem.n_sites)
            owner[rows] = min(np.unique(near[near >= 0]), key=lambda s: (-load[s], -count[s], s))
            summary["municipalities_no_site"] += 1
    moved = owner != nearest
    summary["nodes_moved"] = int(moved.sum())
    load = problem.loads(owner)
    over, ratio = _overloads(load, capacity)
    overloaded = [int(s) for s in np.flatnonzero(over)]
    _log_result(overloaded, load, capacity, bridged, 0)
    logger.info(f"Municipal supply areas: {summary['municipalities_one_site']} municipalities on their own "
                f"substation, {summary['municipalities_split']} split among their substations, "
                f"{summary['municipalities_no_site']} without one on a neighbour; {summary['nodes_moved']} nodes "
                f"not at their nearest substation, {summary['nodes_without_municipality']} without municipality")
    return Assignment(site_of_node=owner.astype(np.int64), weights_m=zero, iterations=1, load_mva=load,
                      overloaded=overloaded, bridged=bridged, capacity_mva=capacity,
                      distance_m=np.where(bridged, np.inf, np.where(moved, np.nan, distance)),
                      history=[(int(over.sum()), ratio)], moved=moved, summary=summary)


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


#: Columns of :func:`municipal_density`.
MUNICIPAL_DENSITY_COLUMNS: tuple[str, ...] = ("installed_mva", "area_km2", "density_mva_km2")


def node_installed_kva(nodes: pd.DataFrame) -> pd.Series:
    """The installed MV/LV capacity of every node as :func:`installed_density` counts it (kVA): a station its
    rating ``sn_kva``, an MV customer its corrected own peak ``installed_kw`` over :data:`DENSITY_CUSTOMER_COS_PHI`;
    HV customers (``connection_hint`` excluded), generators and other kinds 0."""
    frame = pd.DataFrame(nodes)
    kind = frame["kind"].astype(object) if "kind" in frame.columns else pd.Series(None, index=frame.index)
    hint = frame["connection_hint"].astype(object) if "connection_hint" in frame.columns \
        else pd.Series(None, index=frame.index)

    def numbers(column: str) -> pd.Series:
        if column not in frame.columns:
            return pd.Series(0.0, index=frame.index)
        values = pd.to_numeric(frame[column], errors="coerce")
        return values.where(np.isfinite(values) & (values > 0), 0.0)

    station = kind.eq("station")
    customer = kind.eq("customer") & ~hint.eq("excluded")
    return (numbers("sn_kva").where(station, 0.0)
            + (numbers("installed_kw") / DENSITY_CUSTOMER_COS_PHI).where(customer, 0.0)).astype(float)


def municipal_density(nodes: pd.DataFrame, areas_km2: Mapping[str, float]) -> pd.DataFrame:
    """The installed MV/LV capacity per km² of each municipality (AGS) of the planning region.

    All nodes of a municipality count (:func:`node_installed_kva`, whatever their district), over the municipality's
    area inside the planning region, so that a municipality at the region border is not diluted by its uncovered part.

    Args:
        nodes: Node rows with ``ags`` (the municipality of the node's street edge; missing: not counted) and the
            columns of :func:`node_installed_kva`.
        areas_km2: Area of each municipality inside the planning region in km².

    Returns:
        Index ``ags`` (every municipality of ``areas_km2`` or ``nodes``, sorted), :data:`MUNICIPAL_DENSITY_COLUMNS`
        (``density_mva_km2`` NaN without a positive finite area).
    """
    frame = pd.DataFrame(nodes)
    ags = frame["ags"].astype(object) if "ags" in frame.columns else pd.Series(None, index=frame.index)
    known = ags.notna()
    kva = node_installed_kva(frame)[known]
    keys = sorted({str(a) for a in areas_km2} | {str(a) for a in ags[known]})
    out = pd.DataFrame(index=pd.Index(keys, name="ags", dtype=object))
    out["installed_mva"] = (kva.groupby(ags[known].astype(str)).sum() / 1000.0).reindex(out.index, fill_value=0.0)
    area = pd.Series({str(a): float(v) if v is not None else np.nan for a, v in areas_km2.items()}, dtype=float)
    out["area_km2"] = area.reindex(out.index).astype(float)
    valid = np.isfinite(out["area_km2"]) & (out["area_km2"] > 0)
    out["density_mva_km2"] = (out["installed_mva"] / out["area_km2"].where(valid)).astype(float)
    return out[list(MUNICIPAL_DENSITY_COLUMNS)]


def dense_share(nodes: pd.DataFrame, municipalities: pd.DataFrame, threshold_mva_km2: float) -> pd.Series:
    """The share of each district's installed capacity that lies in municipalities with an installed density of at
    least ``threshold_mva_km2`` (:func:`municipal_density`); NaN for a district without installed capacity.

    Args:
        nodes: Node rows with ``mv_grid_id`` (NaN: not assigned), ``ags`` and the columns of :func:`node_installed_kva`.
        municipalities: :func:`municipal_density`.
        threshold_mva_km2: ``mv_10kv_min_density_mva_km2``.

    Returns:
        Index ``mv_grid_id`` (the districts of ``nodes``, ascending), the share 0 to 1.
    """
    frame = pd.DataFrame(nodes)
    grid = pd.to_numeric(frame["mv_grid_id"], errors="coerce")
    dense = set(municipalities.index[municipalities["density_mva_km2"] >= float(threshold_mva_km2)].astype(str))
    kva = node_installed_kva(frame)
    in_dense = frame["ags"].astype(object).map(lambda a: a is not None and str(a) in dense) \
        if "ags" in frame.columns else pd.Series(False, index=frame.index)
    keep = grid.notna()
    key = grid[keep].astype(np.int64)
    total = kva[keep].groupby(key).sum()
    part = kva[keep].where(in_dense[keep].astype(bool), 0.0).groupby(key).sum()
    share = (part / total.where(total > 0)).astype(float)
    share.index = share.index.rename("mv_grid_id")
    return share.sort_index()


def district_voltages(density: pd.DataFrame, params: "MvParameters",
                      share: pd.Series | None = None) -> pd.DataFrame:
    """The nominal voltage of every district.

    With ``MV_NOMINAL_VOLTAGE_KV`` auto and the municipal ``share`` (:func:`dense_share`): 10 kV when at least half
    of the district's installed capacity lies in municipalities with an installed density of at least
    ``mv_10kv_min_density_mva_km2``, else 20 kV. The MV voltage is a decision of the grid operator for its whole
    grid (a city utility runs 10 kV also in its parks and outskirts), so the rule looks at the municipality, not at
    the district's own cell. Without ``share`` (or for a district without one): ``params.district_kv`` of the
    district's own density (the rule before stage version 11). A fixed voltage applies to every district.

    Returns:
        ``density`` with the columns ``dense_share`` (NaN without ``share``) and ``vn_kv``.
    """
    out = pd.DataFrame(density).copy()
    out["dense_share"] = share.reindex(out.index).astype(float) if share is not None else np.nan
    voltages = []
    for d, s in zip(out["density_mva_km2"], out["dense_share"]):
        if params.auto_voltage and share is not None and np.isfinite(s):
            voltages.append(params.district_kv(float(params.mv_10kv_min_density_mva_km2) if s >= 0.5 else 0.0))
        else:
            voltages.append(params.district_kv(d))
    out["vn_kv"] = voltages
    return out


def voltage_record(row: Mapping[str, Any], params: "MvParameters") -> dict:
    """The ``mv_grid.metrics['voltage_level']`` entry of a district (JSON-native): ``rule`` (``auto`` or
    ``fixed``), ``vn_kv``, ``density_mva_km2``, ``threshold_mva_km2`` and the parts of the density; with the
    municipal rule also ``scope`` (``municipality``) and ``dense_share``."""
    def number(value: Any) -> float | None:
        value = float(value)
        return value if np.isfinite(value) else None

    share = row.get("dense_share") if hasattr(row, "get") else None
    record = {"rule": "auto" if params.auto_voltage else "fixed", "vn_kv": float(row["vn_kv"]),
            "density_mva_km2": number(row["density_mva_km2"]),
            "threshold_mva_km2": float(params.mv_10kv_min_density_mva_km2),
            "station_kva": number(row["station_kva"]), "customer_kva": number(row["customer_kva"]),
            "area_km2": number(row["area_km2"]), "n_stations": int(row["n_stations"]),
            "n_customers": int(row["n_customers"])}
    if share is not None and np.isfinite(float(share)):
        record["scope"] = "municipality"
        record["dense_share"] = float(share)
    return record


def describe_voltages(table: pd.DataFrame, params: "MvParameters") -> str:
    """One log line of :func:`district_voltages`: the districts per nominal voltage and their density range."""
    if not len(table):
        return "Nominal voltage: no districts"
    density = table["density_mva_km2"].astype(float)
    counts = table["vn_kv"].astype(float).value_counts()
    parts = [f"{int(counts[vn])} at {vn:g} kV" for vn in sorted(counts.index, reverse=True)]
    municipal = "dense_share" in table.columns and table["dense_share"].notna().any()
    scope = " in the municipality" if municipal else ""
    rule = (f"auto, 10 kV from {params.mv_10kv_min_density_mva_km2:g} MVA/km²{scope}" if params.auto_voltage
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
