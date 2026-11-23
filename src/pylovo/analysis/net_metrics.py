"""Figures of one LV grid, computed from its pandapower net alone (no database).

The functions treat the LV busbar of the station as the root of a predominantly radial graph and respect the
operational topology (graphs are built with ``respect_switches=True``). They recognise the buses of a pylovo net
by their names (:data:`LV_BUSBAR_KEYWORD`, :data:`CONSUMER_BUS_KEYWORD`, :data:`CONNECTION_BUS_KEYWORD`); nets
without these names, such as exports of real grids, are read through their loads and transformer. Geographic and
projected coordinates are told apart where a distance needs it.

:func:`pylovo.analysis.grid_analysis.key_figures` composes the key figures of a grid from these functions and
from :mod:`pylovo.analysis.impedance_metrics`.
"""

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np
import pandapower as pp
import pandas as pd
from sklearn.metrics.pairwise import haversine_distances

from pylovo import utils
from pylovo.analysis import comparison
from pylovo.config_loader import SIM_FACTOR

logger = logging.getLogger(__name__)

#: Substrings of the bus names of a pylovo net: the LV busbar of the station, the consumer buses and the
#: connection buses on the street.
LV_BUSBAR_KEYWORD = "LVbus"
CONSUMER_BUS_KEYWORD = "Consumer Nodebus"
CONNECTION_BUS_KEYWORD = "Connection Nodebus"

# ---------------------------------------------------------------------------
# Bus type classification configs for feeder counting
# ---------------------------------------------------------------------------
# Each config maps bus and line name patterns to the roles used by the unified feeder counter
# (see classify_bus_types); a pattern that is None matches nothing.
#
# Grids without the pylovo bus names, e.g. exports of real grids, use GENERIC_BUS_TYPE_CONFIG: no name
# patterns, so the load buses are the house connections and every other bus is backbone. To mark service
# buses, cable distribution cabinets and service cables by the names of an export, pass a config with
# its patterns as ``bus_type_config``.

GENERIC_BUS_TYPE_CONFIG: Dict[str, Optional[str]] = {
    "name_column": "name",
    "house_connection_pattern": None,
    "kvs_pattern": None,
    "service_line_pattern": None,
}

PYLOVO_BUS_TYPE_CONFIG: Dict[str, Optional[str]] = {
    "name_column": "name",
    "house_connection_pattern": r"Consumer Nodebus",
    "kvs_pattern": None,
}


def station_mva(net: pp.pandapowerNet) -> float:
    """Rating in MVA of the (first) transformer station of a net: ``sn_mva x parallel``.

    pandapower's ``sn_mva`` is the rating of one unit; pylovo builds some stations from two
    parallel units (1260 kVA as 2 x 630 kVA), so the station size is the product.
    """
    row = net.trafo.iloc[0]
    parallel = row["parallel"] if "parallel" in net.trafo.columns and pd.notna(row["parallel"]) else 1
    return float(row["sn_mva"]) * max(1, int(parallel))


def classify_bus_types(
    net: pp.pandapowerNet,
    config: Dict[str, str],
) -> Dict[int, str]:
    """Build a mapping ``{bus_index: bus_type}`` from naming patterns.

    Recognised bus types:

    - ``"house_connection"``: service/attachment buses that only count when
      topology shows they continue or split the backbone.
    - ``"kvs"``: cable distribution stations treated as transparent splitters.
    - ``"backbone"``: everything else (cable nodes, connection buses, ...).

    Args:
        net: Network whose ``bus`` table is inspected.
        config: Bus-type config with ``name_column`` and optionally ``house_connection_pattern``
            and ``kvs_pattern`` (see :data:`GENERIC_BUS_TYPE_CONFIG`). Patterns are
            case-insensitive regexes matched with ``re.search``; a missing or None pattern
            matches nothing.

    Returns:
        Bus type per bus index; empty if the name column is missing.
    """
    col = config["name_column"]
    if col not in net.bus.columns:
        return {}

    patterns = {
        bus_type: re.compile(config[key], re.IGNORECASE)
        for bus_type, key in (("kvs", "kvs_pattern"), ("house_connection", "house_connection_pattern"))
        if config.get(key)
    }
    return {
        idx: next((bus_type for bus_type, regex in patterns.items() if regex.search(str(name))), "backbone")
        for idx, name in net.bus[col].fillna("").items()
    }


def uses_synthetic_bus_naming(net: pp.pandapowerNet) -> bool:
    """Return whether the grid follows the PyLoVo synthetic bus naming convention."""
    return "name" in net.bus.columns and net.bus["name"].fillna("").str.contains(LV_BUSBAR_KEYWORD).any()


def _default_root_bus(pandapower_net: pp.pandapowerNet) -> int:
    """Resolve a generic root/source bus from transformer, ext-grid, or bus table."""
    if not pandapower_net.trafo.empty and "lv_bus" in pandapower_net.trafo.columns:
        return pandapower_net.trafo["lv_bus"].iloc[0]
    if not pandapower_net.ext_grid.empty and "bus" in pandapower_net.ext_grid.columns:
        return pandapower_net.ext_grid["bus"].iloc[0]
    if pandapower_net.bus.empty:
        raise ValueError("Cannot resolve a root bus because the bus table is empty.")
    return pandapower_net.bus.index[0]


def resolve_synthetic_root_bus(pandapower_net: pp.pandapowerNet) -> int:
    """Resolve the LV root bus index for synthetic PyLoVo grids."""
    if "name" in pandapower_net.bus.columns:
        root = pandapower_net.bus[
            pandapower_net.bus["name"].fillna("").str.contains(LV_BUSBAR_KEYWORD, na=False)
        ]
        if not root.empty:
            return root.index[0]

    return _default_root_bus(pandapower_net)


def resolve_root_bus(net: pp.pandapowerNet, uses_synthetic_naming: bool) -> int:
    """Resolve the source bus used for feeder and distance calculations."""
    if uses_synthetic_naming:
        return resolve_synthetic_root_bus(net)
    return _default_root_bus(net)


def _synthetic_consumer_buses(pandapower_net: pp.pandapowerNet) -> List[int]:
    """Resolve consumer buses in synthetic grids from the configured naming convention."""
    if "name" not in pandapower_net.bus.columns:
        return []
    consumer_mask = pandapower_net.bus["name"].fillna("").str.contains(CONSUMER_BUS_KEYWORD, na=False)
    return pandapower_net.bus[consumer_mask].index.tolist()


def resolve_consumer_buses(net: pp.pandapowerNet, uses_synthetic_naming: bool) -> List[int]:
    """Resolve the consumer bus indices used by the structural metric set."""
    if uses_synthetic_naming:
        return _synthetic_consumer_buses(net)

    if net.load.empty or "bus" not in net.load.columns:
        return []
    return net.load["bus"].unique().tolist()


def count_buses_by_keyword(pandapower_net: pp.pandapowerNet, bus_description: str) -> int:
    """Count buses whose name contains a given description (substring match)."""
    if "name" not in pandapower_net.bus.columns:
        return 0
    return pandapower_net.bus["name"].fillna("").str.contains(bus_description, na=False).sum()


def _categorised_loads(pandapower_net: pp.pandapowerNet) -> pd.DataFrame:
    """Copy of the load table with the consumer category (``analysis_category``) and numeric ``load_units``.

    The category is the load's ``category``, else the ``zone`` of its bus, else Residential; the
    building types of older nets (MFH, SFH, AB, TH, Mixed) count as Residential. Missing
    ``load_units`` count as 1.
    """
    loads = pandapower_net.load.copy()
    if "category" in loads.columns:
        loads["analysis_category"] = loads["category"]
    elif "zone" in pandapower_net.bus.columns:
        loads = loads.merge(pandapower_net.bus[["zone"]], left_on="bus", right_index=True, how="left")
        loads["analysis_category"] = loads["zone"]
    else:
        loads["analysis_category"] = "Residential"
    loads["analysis_category"] = loads["analysis_category"].fillna("Residential")
    loads["analysis_category"] = loads["analysis_category"].replace(
        ["MFH", "SFH", "AB", "TH", "Mixed"], "Residential"
    )
    if "load_units" not in loads.columns:
        loads["load_units"] = 1.0
    loads["load_units"] = pd.to_numeric(loads["load_units"], errors="coerce").fillna(1.0)
    return loads


def count_households(pandapower_net: pp.pandapowerNet) -> int:
    """Number of households: the sum of ``load_units`` of the residential loads.

    ``load_units`` is the number of households of a residential load and 1 for a non-residential
    load, which is no household. A load without ``load_units`` counts as one household.
    """
    loads = _categorised_loads(pandapower_net)
    return int(round(loads.loc[loads["analysis_category"] == "Residential", "load_units"].sum()))


def total_installed_power(pandapower_net: pp.pandapowerNet) -> float:
    """Sum of installed maximum active power (MW) over all loads."""
    if pandapower_net.load.empty or "max_p_mw" not in pandapower_net.load.columns:
        return 0.0
    return pandapower_net.load["max_p_mw"].sum()


def simultaneous_peak_load(pandapower_net: pp.pandapowerNet) -> float:
    """Coincident peak load of a grid in kW: simultaneity per consumer category, then summed.

    Per category (Residential, Commercial, Public) ``utils.category_simultaneous_load`` of the
    installed power (``max_p_mw``) and the number of units (``load_units``: households of
    residential loads, one per non-residential load) with the ``SIM_FACTOR`` of the category.
    """
    loads = _categorised_loads(pandapower_net)
    sim_peak_load = 0.0
    for cat in ("Residential", "Commercial", "Public"):
        load_subset = loads[loads["analysis_category"] == cat]
        category_count = load_subset["load_units"].sum()
        if category_count > 0:
            sim_peak_load += utils.category_simultaneous_load(
                installed_power=load_subset["max_p_mw"].sum() * 1000.0,
                load_count=category_count,
                sim_factor=SIM_FACTOR.get(cat, 1.0)
            )
    return sim_peak_load


def transformer_power(pandapower_net: pp.pandapowerNet) -> float:
    """Return the station rating in MVA for a grid with exactly one LV transformer element.

    pandapower's ``sn_mva`` is the rating of one unit; a station of two parallel units
    (``parallel = 2``, e.g. 1260 kVA as 2 x 630 kVA) is rated ``sn_mva x parallel``.
    """
    if pandapower_net.trafo.empty or "sn_mva" not in pandapower_net.trafo.columns:
        raise ValueError("No transformer found in the net.")
    return station_mva(pandapower_net)


def cable_length(pandapower_net: pp.pandapowerNet, only_in_service: bool = False) -> float:
    """Installed cable length in km: ``length_km x parallel`` summed over the lines.

    A line of two parallel cables counts twice its route length, as in ``plz_parameters.cable_length``.

    Args:
        pandapower_net (pp.pandapowerNet): The grid model.
        only_in_service (bool): Restrict the sum to active lines.
    """
    line_df = pandapower_net.line
    if line_df.empty or "length_km" not in line_df.columns:
        return 0.0
    if only_in_service and "in_service" in line_df.columns:
        line_df = line_df[line_df["in_service"]]
    parallel = (
        pd.to_numeric(line_df["parallel"], errors="coerce").fillna(1.0).replace(0, 1.0)
        if "parallel" in line_df.columns
        else 1.0
    )
    return float((line_df["length_km"] * parallel).sum())


def average_house_distance(pandapower_net: pp.pandapowerNet) -> float:
    """Calculate a spatial neighborhood distance proxy for consumer locations.

    The returned value (km) is the median over the consumer buses of the mean distance
    to their four nearest neighbouring consumer buses. Consumer buses are the
    ``Consumer Nodebus`` buses of pylovo grids and the load buses of other nets
    (:meth:`resolve_consumer_buses`). The method auto-detects whether the available
    geometry coordinates are geographic or projected. Without two consumer buses with a location (e.g. a
    single-building grid) the distance is undefined: NaN.
    """
    consumer_buses = resolve_consumer_buses(pandapower_net, uses_synthetic_bus_naming(pandapower_net))
    bus = pandapower_net.bus.loc[pandapower_net.bus.index.intersection(consumer_buses)]

    if "geo" not in bus.columns:
        logger.debug("Bus table has no geo column; returning NaN for house-distance proxy.")
        return float("nan")

    if bus["geo"].isna().all():
        logger.debug("Bus table has no populated geo values; returning NaN for house-distance proxy.")
        return float("nan")

    geometries = []
    invalid_geometry_count = 0
    for geo_str in bus["geo"].dropna():
        try:
            geo_dict = json.loads(geo_str)
            geometries.append(geo_dict["coordinates"])
        except (json.JSONDecodeError, KeyError, TypeError):
            invalid_geometry_count += 1
            continue

    if len(geometries) < 2:
        if invalid_geometry_count:
            logger.debug(
                "Skipping house-distance proxy because only %s valid bus geometries remained after parsing %s invalid entries.",
                len(geometries),
                invalid_geometry_count,
            )
        return float("nan")

    coords = np.array(geometries)

    is_geographic = (coords[:, 0].min() >= -180 and coords[:, 0].max() <= 180 and
                     coords[:, 1].min() >= -90 and coords[:, 1].max() <= 90)

    if is_geographic:
        coords_rad = np.radians(coords[:, ::-1])   # GeoJSON order (lon, lat); haversine_distances needs (lat, lon)
        dis_mat = haversine_distances(coords_rad, coords_rad) * 6371.0
    else:
        from scipy.spatial.distance import cdist
        dis_mat = cdist(coords, coords, metric="euclidean") / 1000.0

    k = min(len(dis_mat) - 1, 4)

    dis_mat.sort(axis=1)
    avg_dists = dis_mat[:, 1:k+1].mean(axis=1)

    return float(np.median(avg_dists))


def _path_lengths(graph: nx.Graph, source: int, targets: List[int]) -> Tuple[float, float]:
    """Calculate average and maximum weighted path lengths from one source to multiple targets."""
    if source not in graph:
        logger.debug(
            "Path-length calculation skipped because source bus %s is absent from the topology graph.",
            source,
        )
        return 0.0, 0.0

    path_lengths = []
    skipped_targets = 0
    for target in targets:
        if target not in graph:
            skipped_targets += 1
            continue
        try:
            length = nx.dijkstra_path_length(graph, source, target, weight="weight")
            path_lengths.append(length)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            skipped_targets += 1
            continue

    if not path_lengths:
        if targets:
            logger.debug(
                "No valid weighted paths found from source %s to %s targets; %s targets were skipped.",
                source,
                len(targets),
                skipped_targets,
            )
        return 0.0, 0.0

    return sum(path_lengths) / len(path_lengths), max(path_lengths)


def transformer_distances(pandapower_net: pp.pandapowerNet, networkx_graph: nx.Graph, root_idx: int | None = None, leaves: List[int] | None = None) -> Tuple[float, float]:
    """Calculate average and maximum weighted distances from the root to consumer buses.

    The preferred path is the synthetic consumer-bus naming convention. When
    explicit leaves are not provided and no consumer buses can be resolved, the
    method returns `(0.0, 0.0)` instead of failing.
    """
    if root_idx is None:
        root = resolve_synthetic_root_bus(pandapower_net)
    else:
        root = root_idx

    if leaves is None:
        leaves = _synthetic_consumer_buses(pandapower_net)

    if len(leaves) == 0:
        return 0.0, 0.0

    return _path_lengths(networkx_graph, root, leaves)


def _line_tree(net: pp.pandapowerNet, root_idx: int) -> Tuple[Dict[int, int | None], List[int], Dict[int, Tuple[float, float]]]:
    """Return the radial line network seen from ``root_idx``.

    Lines between the same two buses form one link: the shortest length and the resistances in parallel. The
    resistance of a line is divided by its parallel cables.

    Returns:
        ``(parent, order, link)``: the parent of every reached bus (``None`` for the root), the buses in
        breadth-first order and, per bus except the root, ``(length_km, resistance_ohm)`` of the link to its parent.
    """
    graph = nx.Graph()
    for line in net.line.itertuples():
        parallel = float(getattr(line, "parallel", 1) or 1)
        length = float(line.length_km)
        resistance = float(line.r_ohm_per_km) * length / (parallel if parallel > 0 else 1.0)
        u, v = int(line.from_bus), int(line.to_bus)
        if graph.has_edge(u, v):
            old = graph.edges[u, v]
            length = min(length, old["length_km"])
            resistance = 0.0 if 0.0 in (resistance, old["r_ohm"]) else 1 / (1 / resistance + 1 / old["r_ohm"])
        graph.add_edge(u, v, length_km=length, r_ohm=resistance)
    if root_idx not in graph:
        return {root_idx: None}, [root_idx], {}
    parent, order, link = {root_idx: None}, [root_idx], {}
    for u, v in nx.bfs_edges(graph, root_idx, sort_neighbors=sorted):
        parent[v] = u
        order.append(v)
        link[v] = (graph.edges[u, v]["length_km"], graph.edges[u, v]["r_ohm"])
    return parent, order, link


def feeder_structure(net: pp.pandapowerNet, root_idx: int,
                     consumer_buses: List[int]) -> Tuple[Dict[int, int], int]:
    """Return the feeders and the branch ends of a net, by the rule of :func:`pylovo.analysis.comparison.feeder_structure`.

    The consumer buses are the houses, ``root_idx`` is the busbar.

    Args:
        net: A radial net.
        root_idx: The LV busbar.
        consumer_buses: Consumer buses of the net.

    Returns:
        ``({consumer bus: first bus of its feeder}, number of branch ends)``. A consumer of no feeder has no entry.
    """
    parent, order, link = _line_tree(net, root_idx)
    consumers = {int(bus) for bus in consumer_buses} & set(parent)
    downstream = {bus: int(bus in consumers) for bus in order}
    for bus in reversed(order[1:]):
        downstream[parent[bus]] += downstream[bus]
    station, feeder_list, branch_ends = comparison.feeder_structure(
        order, parent, {bus: link[bus][0] for bus in order[1:]}, downstream
    )
    feeders = set(feeder_list)
    feeder_of_bus = {}
    for consumer in consumers:
        bus = consumer
        while bus not in station and parent[bus] not in station:
            bus = parent[bus]
        if bus in feeders:
            feeder_of_bus[consumer] = bus
    return feeder_of_bus, branch_ends


def max_route_resistance(net: pp.pandapowerNet, root_idx: int, consumer_buses: List[int]) -> float:
    """Highest series resistance (ohm) of the cables from the busbar to a consumer bus, the most exposed building of
    the structural metrics; parallel cables divide the resistance of their line. 0.0 without a reached consumer."""
    parent, order, link = _line_tree(net, root_idx)
    resistance = {root_idx: 0.0}
    for bus in order[1:]:
        resistance[bus] = resistance[parent[bus]] + link[bus][1]
    values = [resistance[int(bus)] for bus in consumer_buses if int(bus) in resistance]
    return float(max(values)) if values else 0.0
