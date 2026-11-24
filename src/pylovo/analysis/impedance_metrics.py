"""Impedance figures of one LV grid along its feeder paths, computed from its pandapower net (no database).

:func:`impedance_metrics` gives the resistance, the reactance and the voltage-drop proxy of the feeder paths of a
net. The loads of a path are weighted with the simultaneity factors of their categories.
"""

import logging
from itertools import pairwise
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
import pandapower as pp
import pandas as pd

from pylovo import utils
from pylovo.config_loader import PEAK_LOAD_HOUSEHOLD, SIM_FACTOR

from pylovo.analysis import net_metrics
from pylovo.analysis.net_metrics import GENERIC_BUS_TYPE_CONFIG, PYLOVO_BUS_TYPE_CONFIG, classify_bus_types

logger = logging.getLogger(__name__)


def _empty_impedance_summary() -> Dict[str, float]:
    """Return the all-zero result of the path impedance aggregation."""
    return {
        "max_branch_weight": 0.0,
        "total_resistance": 0.0,
        "total_reactance": 0.0,
        "max_branch_resistance": 0.0,
        "branch_resistance": 0.0,
        "total_weight": 0.0,
    }


def _impedance_root_bus(pandapower_net: pp.pandapowerNet, networkx_graph: nx.Graph) -> int:
    """Resolve the root bus used by the shared impedance engine."""
    root_bus = net_metrics.resolve_root_bus(
        pandapower_net,
        net_metrics.uses_synthetic_bus_naming(pandapower_net),
    )
    if root_bus not in networkx_graph:
        raise ValueError(
            f"Resolved root bus {root_bus} is absent from the topology graph for this net."
        )
    return root_bus


def _legacy_impedance_loads(pandapower_net: pp.pandapowerNet) -> pd.DataFrame:
    """Return the legacy VSW load basis without mutating ``net.load``."""
    if pandapower_net.load.empty or "bus" not in pandapower_net.load.columns:
        return pd.DataFrame(columns=["bus", "max_p_mw"])

    load = pandapower_net.load.copy()
    if "max_p_mw" in load.columns:
        return load

    unique_buses = pd.Index(load["bus"].dropna().unique())
    if unique_buses.empty:
        return pd.DataFrame(columns=["bus", "max_p_mw"])

    # Real exports often carry many sub-load rows per connection bus but no
    # max_p_mw column. The legacy VSW metric uses one household equivalent per
    # unique consumer bus to stay comparable with synthetic grids.
    return pd.DataFrame({
        "bus": unique_buses.to_numpy(),
        "max_p_mw": PEAK_LOAD_HOUSEHOLD / 1000.0,
    })


def _impedance_line_table(pandapower_net: pp.pandapowerNet) -> pd.DataFrame:
    """Return the line table when the impedance columns needed by the engine exist."""
    df_line = pandapower_net.line.copy()
    if df_line.empty:
        return df_line

    required_columns = {"from_bus", "to_bus", "length_km", "r_ohm_per_km", "x_ohm_per_km"}
    missing_columns = sorted(required_columns - set(df_line.columns))
    if missing_columns:
        logger.warning(
            "Cannot calculate impedance metrics because the line table is missing columns %s.",
            missing_columns,
        )
        return pd.DataFrame(columns=list(required_columns))

    return df_line


def _line_lookup(df_line: pd.DataFrame) -> Dict[Tuple[int, int], Dict[str, Any]]:
    """Build a stable edge-to-line lookup for path aggregation."""
    line_lookup = {}
    duplicate_edges = 0
    if df_line.empty:
        return line_lookup

    for row in df_line.to_dict("records"):
        key = tuple(sorted((int(row["from_bus"]), int(row["to_bus"]))))
        if key in line_lookup:
            duplicate_edges += 1
            continue
        line_lookup[key] = row

    if duplicate_edges:
        logger.debug(
            "Collapsed %s duplicate line rows onto existing bus-to-bus edges while building the impedance lookup. "
            "This can indicate parallel lines or incomplete line segmentation in %s.",
            duplicate_edges,
            "this net",
        )

    return line_lookup


def _branch_from_path(path: List[int], kvs_buses: set) -> Tuple[int, int] | None:
    """Resolve the feeder branch of one source-to-consumer path: its first edge, or the edge
    behind a cable distribution cabinet (``kvs_buses``) at the first hop."""
    if len(path) < 2:
        return None

    if path[1] in kvs_buses and len(path) >= 3:
        return tuple(sorted((path[1], path[2])))

    return tuple(sorted((path[0], path[1])))


def _line_table_with_simultaneity(
    net: pp.pandapowerNet,
    graph: nx.Graph,
    load_table: pd.DataFrame | None = None,
    root_bus: int | None = None,
) -> pd.DataFrame:
    """Augment the line table with per-line simultaneity and aggregated category metadata.

    The routine assumes a predominantly radial synthetic LV grid. It seeds line-
    level category counts at house-connection buses and then walks upstream toward
    the root to accumulate counts, loads, and cumulated simultaneity factors.
    Missing columns are handled conservatively so higher-level metric computation
    can continue on imperfect inputs.
    """
    df_line = _impedance_line_table(net)
    if df_line.empty:
        return df_line

    df_line["sim_load"] = 0.0
    df_line["sim_factor_cumulated"] = 1.0

    effective_load_table = net.load.copy() if load_table is None else load_table.copy()
    if effective_load_table.empty:
        return df_line

    if "bus" not in effective_load_table.columns or "max_p_mw" not in effective_load_table.columns:
        logger.warning(
            "Cannot augment line simultaneity because the load table is missing required columns."
        )
        return df_line

    loads = effective_load_table.copy()

    if "category" in loads.columns:
        loads["zone"] = loads["category"]
    elif "zone" in net.bus.columns:
        loads = loads.merge(net.bus[["zone"]], left_on="bus", right_index=True, how="left")
    else:
        loads["zone"] = "Residential"
        logger.debug("Bus table has no zone column; defaulting load categories to Residential.")

    loads["zone"] = loads["zone"].fillna("Residential")
    loads["zone"] = loads["zone"].replace(["MFH", "SFH", "AB", "TH"], "Residential")
    # Map any zone not recognised by SIM_FACTOR (e.g. the zone codes of a DSO export) to
    # "Residential" so the category-based upstream propagation produces valid non-zero
    # sim_factor_cumulated values for real grid exports.
    _known_zones = set(SIM_FACTOR.keys())
    loads.loc[~loads["zone"].isin(_known_zones), "zone"] = "Residential"

    if "load_units" not in loads.columns:
        loads["load_units"] = 1.0
    loads["load_units"] = pd.to_numeric(loads["load_units"], errors="coerce").fillna(1.0)

    bus_zone_stats = loads.groupby(["bus", "zone"]).agg(
        count=("load_units", "sum"),
        max_p_mw=("max_p_mw", "sum"),
    ).reset_index()

    if bus_zone_stats.empty:
        return df_line

    # The first line of a bus and of a bus pair in table order: the line the earlier boolean masks picked with
    # index[0] and idxmax. Plain dicts instead of a mask over the line table per bus keep the walk linear.
    first_line_of_bus: Dict[Any, Any] = {}
    first_line_of_pair: Dict[Tuple[Any, Any], Any] = {}
    for line_idx, from_bus, to_bus in zip(df_line.index, df_line["from_bus"], df_line["to_bus"]):
        first_line_of_bus.setdefault(from_bus, line_idx)
        first_line_of_bus.setdefault(to_bus, line_idx)
        first_line_of_pair.setdefault((from_bus, to_bus), line_idx)
        first_line_of_pair.setdefault((to_bus, from_bus), line_idx)
    stats_rows = list(zip(bus_zone_stats["bus"], bus_zone_stats["zone"], bus_zone_stats["count"],
                          bus_zone_stats["max_p_mw"]))

    sim_load = dict.fromkeys(df_line.index, 0.0)
    sim_factor_cumulated = dict.fromkeys(df_line.index, 1.0)

    # House-connection buses are expected to attach to a single upstream line.
    line_stats: Dict[Any, List[float]] = {}
    for bus_idx, zone, count, peak in stats_rows:
        line_idx = first_line_of_bus.get(bus_idx)
        if line_idx is None:
            continue
        seeded = line_stats.setdefault(line_idx, [0.0, 0.0])
        seeded[0] += utils.category_simultaneous_load(1, count, SIM_FACTOR.get(zone, 1.0)) * peak
        seeded[1] += peak
    for line_idx, (seeded_sim_load, seeded_peak_load) in line_stats.items():
        sim_load[line_idx] = seeded_sim_load
        sim_factor_cumulated[line_idx] = seeded_sim_load / seeded_peak_load if seeded_peak_load > 0 else 0.0

    def write(category_totals: Dict[str, Tuple[Dict[Any, float], Dict[Any, float]]] | None = None) -> pd.DataFrame:
        df_line["sim_load"] = pd.Series(sim_load, dtype=float)
        df_line["sim_factor_cumulated"] = pd.Series(sim_factor_cumulated, dtype=float)
        for category_name, (numbers, loads_mw) in (category_totals or {}).items():
            df_line[f"no_{category_name}"] = pd.Series(numbers, dtype=float)
            df_line[f"load_{category_name}_mw"] = pd.Series(loads_mw, dtype=float)
        return df_line

    try:
        resolved_root_bus = net_metrics.resolve_synthetic_root_bus(net) if root_bus is None else root_bus
    except ValueError:
        logger.debug("Could not resolve a synthetic root bus; returning seeded line simultaneity only.")
        return write()

    if resolved_root_bus not in graph:
        logger.debug(
            "Synthetic root bus %s is not present in the topology graph; returning seeded line simultaneity only.",
            resolved_root_bus,
        )
        return write()

    graph_distances = nx.shortest_path_length(graph, source=resolved_root_bus)
    # Process buses from the leaves upward so child-line totals are available
    # before their parent line is recomputed.
    buses_by_reverse_distance = sorted(
        graph_distances.keys(),
        key=lambda bus_idx: graph_distances[bus_idx],
        reverse=True,
    )

    # Category-specific totals are kept separately because simultaneity factors
    # differ between Residential, Commercial, and Public loads.
    category_names = ["Commercial", "Public", "Residential"]
    number = {name: dict.fromkeys(df_line.index, 0.0) for name in category_names}
    load_mw = {name: dict.fromkeys(df_line.index, 0.0) for name in category_names}
    for bus_idx, category_name, count, peak in stats_rows:
        line_idx = first_line_of_bus.get(bus_idx)
        if category_name not in category_names or line_idx is None:
            continue
        number[category_name][line_idx] += count
        load_mw[category_name][line_idx] += peak

    for bus_idx in buses_by_reverse_distance:
        if bus_idx == resolved_root_bus:
            continue

        parent_bus = None
        child_buses = []
        for neighbor in graph.neighbors(bus_idx):
            if graph_distances[neighbor] < graph_distances[bus_idx]:
                parent_bus = neighbor
            else:
                child_buses.append(neighbor)

        if parent_bus is None:
            continue

        upstream_line_idx = first_line_of_pair.get((parent_bus, bus_idx))
        if upstream_line_idx is None:
            continue

        for child_bus in child_buses:
            downstream_line_idx = first_line_of_pair.get((bus_idx, child_bus))
            if downstream_line_idx is None:
                continue
            for category_name in category_names:
                number[category_name][upstream_line_idx] += number[category_name][downstream_line_idx]
                load_mw[category_name][upstream_line_idx] += load_mw[category_name][downstream_line_idx]

        total_sim_load = 0.0
        total_peak_load = 0.0
        for category_name in category_names:
            category_count = number[category_name][upstream_line_idx]
            category_peak_load = load_mw[category_name][upstream_line_idx]
            sim_factor = SIM_FACTOR.get(category_name, 1.0)

            if category_count > 0 and category_peak_load > 0:
                total_sim_load += utils.category_simultaneous_load(category_peak_load, category_count, sim_factor)
            total_peak_load += category_peak_load

        sim_load[upstream_line_idx] = total_sim_load
        sim_factor_cumulated[upstream_line_idx] = total_sim_load / total_peak_load if total_peak_load > 0 else 0.0

    return write({name: (number[name], load_mw[name]) for name in category_names})


def _path_impedance_summary(
    pandapower_net: pp.pandapowerNet,
    networkx_graph: nx.Graph,
    bus_weights: pd.DataFrame,
    line_table: pd.DataFrame,
    apply_simultaneity: bool,
    bus_type_config: Optional[Dict[str, Optional[str]]] = None,
    branch_of_bus: Optional[Dict[int, int]] = None,
) -> Dict[str, float]:
    """Run the source-to-consumer path impedance aggregation of the VSW metric.

    The caller provides the bus weights and decides whether cumulated
    simultaneity should affect line resistance.
    Paths are grouped by the branch of their consumer in ``branch_of_bus``; consumers without an entry count in
    the totals but in no branch. Without it, by feeder branch: a first-hop bus of type ``kvs`` in
    ``bus_type_config`` (default: by the bus naming) splits its feeders.
    """
    if bus_weights.empty or line_table.empty:
        return _empty_impedance_summary()

    try:
        root_bus = _impedance_root_bus(pandapower_net, networkx_graph)
    except ValueError:
        return _empty_impedance_summary()

    bus_type_config = bus_type_config or (
        PYLOVO_BUS_TYPE_CONFIG if net_metrics.uses_synthetic_bus_naming(pandapower_net) else GENERIC_BUS_TYPE_CONFIG
    )
    kvs_buses = {bus for bus, bus_type in classify_bus_types(pandapower_net, bus_type_config).items()
                 if bus_type == "kvs"}
    line_lookup = _line_lookup(line_table)
    records = []
    missing_line_edges = 0

    for row in bus_weights.itertuples(index=False):
        consumer_bus = int(row.consumer_bus)
        path_weight = float(row.path_weight)
        if path_weight <= 0:
            continue

        try:
            path = nx.shortest_path(networkx_graph, source=root_bus, target=consumer_bus, weight="weight")
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue

        if branch_of_bus is None:
            branch = _branch_from_path(path, kvs_buses)
        else:
            branch = branch_of_bus.get(consumer_bus)
        path_resistance = 0.0
        path_reactance = 0.0

        for from_bus, to_bus in pairwise(path):
            line_data = line_lookup.get(tuple(sorted((from_bus, to_bus))))
            if line_data is None:
                missing_line_edges += 1
                continue

            length_km = float(line_data.get("length_km", 0.0) or 0.0)
            resistance_per_km = float(line_data.get("r_ohm_per_km", 0.0) or 0.0)
            reactance_per_km = float(line_data.get("x_ohm_per_km", 0.0) or 0.0)
            sim_factor = float(line_data.get("sim_factor_cumulated", 1.0) or 1.0) if apply_simultaneity else 1.0
            # parallel cables of one line divide its impedance
            parallel = float(line_data.get("parallel", 1) or 1)
            parallel = parallel if parallel > 0 else 1.0        # NaN or 0: one cable

            # R and X alike, so their ratio is a weighted mean of the R/X of the lines
            path_resistance += length_km * resistance_per_km / parallel * sim_factor
            path_reactance += length_km * reactance_per_km / parallel * sim_factor

        records.append({
            "branch": branch,
            "path_weight": path_weight,
            "weighted_resistance": path_weight * path_resistance,
            "weighted_reactance": path_weight * path_reactance,
        })

    if missing_line_edges:
        logger.debug(
            "Impedance path collection skipped %s path edges because no matching line row was found. "
            "This can indicate missing line segmentation in %s.",
            missing_line_edges,
            "this net",
        )

    if not records:
        return _empty_impedance_summary()

    df_records = pd.DataFrame(records)
    in_branch = df_records if branch_of_bus is None else df_records[df_records["branch"].notna()]
    branch_weights = in_branch.groupby("branch", dropna=False)["path_weight"].sum()
    branch_resistance = in_branch.groupby("branch", dropna=False)["weighted_resistance"].sum()
    return {
        "max_branch_weight": float(branch_weights.max()) if not branch_weights.empty else 0.0,
        "total_resistance": float(df_records["weighted_resistance"].sum()),
        "total_reactance": float(df_records["weighted_reactance"].sum()),
        "max_branch_resistance": float(branch_resistance.max()) if not branch_resistance.empty else 0.0,
        "branch_resistance": float(in_branch["weighted_resistance"].sum()),
        "total_weight": float(df_records["path_weight"].sum()),
    }


def impedance_metrics(
    pandapower_net: pp.pandapowerNet,
    networkx_graph: nx.Graph,
    branch_of_bus: Optional[Dict[int, int]] = None,
) -> Tuple[float, float, float, float, float, float]:
    """Calculate the legacy VSW-style impedance metrics used by clustering.

    Returns maximum household equivalents per branch, total resistance, total
    reactance, the resistance-to-reactance ratio, the maximum branch
    resistance proxy and the resistance of the consumers in a branch.
    Resistance and reactance of a line are weighted alike (household equivalents behind it
    times its cumulated simultaneity), so the ratio lies between the smallest and the largest
    R/X of the lines. They are divided by the number of parallel cables. The branches are the
    feeders of ``branch_of_bus`` (:meth:`feeder_structure`), else the feeder branches of
    :meth:`_calculate_path_impedance_summary`.
    """
    load_table = _legacy_impedance_loads(pandapower_net)
    if load_table.empty or "max_p_mw" not in load_table.columns:
        bus_weights = pd.DataFrame(columns=["consumer_bus", "path_weight"])
    else:
        bus_weights = (
            load_table.groupby("bus")["max_p_mw"].sum() * 1000.0 / PEAK_LOAD_HOUSEHOLD
        ).reset_index(name="path_weight").rename(columns={"bus": "consumer_bus"})
    line_table = _line_table_with_simultaneity(
        pandapower_net,
        networkx_graph,
        load_table=load_table,
    )
    result = _path_impedance_summary(
        pandapower_net,
        networkx_graph,
        bus_weights,
        line_table,
        apply_simultaneity=True,
        branch_of_bus=branch_of_bus,
    )
    total_reactance = result["total_reactance"]
    ratio = result["total_resistance"] / total_reactance if total_reactance > 0 else 0.0
    return (
        result["max_branch_weight"],
        result["total_resistance"],
        total_reactance,
        ratio,
        result["max_branch_resistance"],
        result["branch_resistance"],
    )
