"""Compose named LV grid parameter sets from the ParameterCalculator toolbox."""

import copy
from typing import TYPE_CHECKING, Any, Dict, Optional

import pandas as pd
import pandapower as pp

from pylovo.config_loader import PEAK_LOAD_HOUSEHOLD

if TYPE_CHECKING:
    from pylovo.analysis.parameter_calculation import ParameterCalculator


REAL_HOUSEHOLD_LOAD_TYPES = {"HH"}


def _get_transformer_mva(net: pp.pandapowerNet) -> float:
    """Return the transformer rating in MVA from ``net.trafo["sn_mva"]``.

    Both synthetic grids and real LV subnets carry the transformer as an
    out-of-service element (added by
    :func:`~pylovo.analysis.validation_helpers.extract_lv_grids`), so
    ``sn_mva`` is always readable directly from the network object.
    """
    if not net.trafo.empty and "sn_mva" in net.trafo.columns:
        val = net.trafo["sn_mva"].iloc[0]
        if pd.notna(val):
            return float(val)
    return float("nan")


def _calculate_resistance(
    calculator: "ParameterCalculator",
    net: pp.pandapowerNet,
    with_service_lines: bool = False,
    consumer_buses: list[int] | None = None,
    bus_type_config: Optional[Dict[str, str]] = None,
) -> float:
    """Return the active comparison resistance proxy.

    Comparison now uses aggregate routed-line resistance so the metric remains
    meaningful for both real and synthetic grids even when house-connection
    modelling differs or cable-distribution stations are absent.
    """
    return calculator.calculate_graph_resistance(
        net,
        only_in_service=True,
        with_service_lines=with_service_lines,
        additional_house_connection_buses=consumer_buses,
        bus_type_config=bus_type_config,
    )


def _with_absolute_line_lengths(net: pp.pandapowerNet) -> pp.pandapowerNet:
    """Return a shallow analysis copy with non-negative line lengths.

    Negative line lengths can arise from edge orientation in generated helper
    routes.  Comparison metrics use physical distance magnitudes, while raw
    negative counts stay available in the exported diagnostics.
    """
    analysis_net = copy.deepcopy(net)
    if "length_km" in analysis_net.line.columns:
        analysis_net.line["length_km"] = pd.to_numeric(
            analysis_net.line["length_km"], errors="coerce"
        ).abs()
    return analysis_net


def _calculate_buildings_per_feeder(
    consumer_buses: list[int],
    feeder_lines: int,
) -> float:
    """Return feeder occupancy based on resolved consumer connection points.

    The active comparison workflow resolves consumer points differently for
    synthetic and real grids, but both represent the occupancy carried by the
    feeder structure. This keeps the comparison metric aligned across sources.
    """
    if feeder_lines <= 0:
        return 0.0
    return float(len(consumer_buses)) / float(feeder_lines)


def compute_comparison_parameters(
    calculator: "ParameterCalculator",
    net: pp.pandapowerNet,
    consumer_buses: list[int] | None = None,
    bus_type_config: Optional[Dict[str, str]] = None,
    with_service_lines: bool = False,
) -> Dict[str, Any]:
    """Compute the active real-vs-synthetic comparison parameter set for one LV grid.

    The active metrics must fail visibly when their topology assumptions are not
    met.  Callers are responsible for recording failed rows; this function does
    not replace failures with zero-valued metrics.

    Parameters
    ----------
    bus_type_config : dict, optional
        Naming-pattern dictionary forwarded to the unified feeder counter.
        When ``None`` the config is auto-detected from the bus naming
        convention (see :data:`~pylovo.analysis.parameter_calculation.SWF_BUS_TYPE_CONFIG`).
    with_service_lines : bool, default False
        Include terminal house/consumer service connections in length,
        resistance, and transformer-distance metrics. The default benchmark
        excludes them and measures feeder/backbone structure.
    """
    from pylovo.analysis.parameter_calculation import PYLOVO_BUS_TYPE_CONFIG, SWF_BUS_TYPE_CONFIG

    uses_synthetic_naming = calculator.uses_synthetic_bus_naming(net)
    active_bus_type_config = bus_type_config or (
        PYLOVO_BUS_TYPE_CONFIG if uses_synthetic_naming else SWF_BUS_TYPE_CONFIG
    )
    root_idx = calculator.resolve_root_bus(net, uses_synthetic_naming)
    resolved_consumer_buses = (
        consumer_buses
        if consumer_buses is not None
        else calculator.resolve_consumer_buses(net, uses_synthetic_naming)
    )
    analysis_net = _with_absolute_line_lengths(net)

    graph = pp.topology.create_nxgraph(analysis_net, respect_switches=True)
    feeder_lines_first_hop = calculator.count_feeders(
        analysis_net,
        graph,
        root_idx,
        uses_synthetic_naming,
        bus_type_config=active_bus_type_config,
        recursive_expansion=False,
        additional_house_connection_buses=resolved_consumer_buses,
    )
    feeder_lines_label_aware = calculator.count_feeders(
        analysis_net,
        graph,
        root_idx,
        uses_synthetic_naming,
        bus_type_config=active_bus_type_config,
        recursive_expansion=True,
        additional_house_connection_buses=resolved_consumer_buses,
    )
    # Raw terminal topology expands unlabeled real split points like synthetic
    # connection nodes. It is useful as a diagnostic, but still counts terminal
    # service-parent stubs that only exist because consumer points attach there.
    feeder_lines_terminal_topology = calculator.count_feeders(
        analysis_net,
        graph,
        root_idx,
        True,
        bus_type_config=active_bus_type_config,
        recursive_expansion=True,
        additional_house_connection_buses=resolved_consumer_buses,
    )
    # Active benchmark definition: count terminal backbone branches after pruning
    # resolved consumer endpoints and terminal non-KVS service stubs. This keeps
    # KVS/split topology visible without treating house connections as splits.
    feeder_lines_terminal_backbone = calculator.count_feeders(
        analysis_net,
        graph,
        root_idx,
        True,
        bus_type_config=active_bus_type_config,
        recursive_expansion=True,
        additional_house_connection_buses=resolved_consumer_buses,
        collapse_service_connection_leaves=True,
    )
    feeder_lines_collapse_non_kvs = calculator.count_feeders(
        analysis_net,
        graph,
        root_idx,
        False,
        bus_type_config=active_bus_type_config,
        recursive_expansion=True,
        additional_house_connection_buses=resolved_consumer_buses,
    )
    feeder_lines = feeder_lines_terminal_backbone
    if with_service_lines:
        distance_graph = graph
        avg_trafo_distance, max_trafo_distance = calculator.calculate_trafo_distances(
            distance_graph,
            root_idx,
            resolved_consumer_buses,
        )
    else:
        distance_graph = calculator.build_service_pruned_graph(
            analysis_net,
            additional_house_connection_buses=resolved_consumer_buses,
            bus_type_config=active_bus_type_config,
        )
        avg_trafo_distance, max_trafo_distance = calculator.calculate_feeder_terminal_distances(
            distance_graph,
            root_idx,
        )

    transformer_mva = _get_transformer_mva(net)
    graph_length = calculator.calculate_graph_length(
        analysis_net,
        only_in_service=True,
        with_service_lines=with_service_lines,
        additional_house_connection_buses=resolved_consumer_buses,
        bus_type_config=active_bus_type_config,
    )
    graph_resistance = _calculate_resistance(
        calculator,
        analysis_net,
        with_service_lines=with_service_lines,
        consumer_buses=resolved_consumer_buses,
        bus_type_config=active_bus_type_config,
    )
    buildings_per_feeder = _calculate_buildings_per_feeder(
        resolved_consumer_buses,
        feeder_lines,
    )
    line_df = net.line
    active_line_df = line_df[line_df["in_service"]] if "in_service" in line_df.columns else line_df
    length = pd.to_numeric(line_df.get("length_km", pd.Series(dtype=float)), errors="coerce")

    return {
        "metric_status": "ok",
        "metric_error": "",
        "uses_synthetic_naming": bool(uses_synthetic_naming),
        "root_bus": int(root_idx),
        "bus_count": int(len(net.bus)),
        "line_count": int(len(line_df)),
        "active_line_count": int(len(active_line_df)),
        "consumer_bus_count": int(len(resolved_consumer_buses)),
        "load_count": int(len(net.load)),
        "trafo_count": int(len(net.trafo)),
        "ext_grid_count": int(len(net.ext_grid)),
        "negative_length_count": int((length < 0).sum()),
        "zero_length_count": int((length == 0).sum()),
        "missing_length_count": int(length.isna().sum()),
        "feeder_lines": int(feeder_lines),
        "feeder_lines_first_hop": int(feeder_lines_first_hop),
        "feeder_lines_label_aware": int(feeder_lines_label_aware),
        "feeder_lines_terminal_topology": int(feeder_lines_terminal_topology),
        "feeder_lines_terminal_backbone": int(feeder_lines_terminal_backbone),
        "feeder_lines_expand_all": int(feeder_lines_terminal_topology),
        "feeder_lines_collapse_non_kvs": int(feeder_lines_collapse_non_kvs),
        "feeder_count_delta_label_aware": int(feeder_lines_label_aware - feeder_lines),
        "feeder_count_delta_terminal_topology": int(feeder_lines_terminal_topology - feeder_lines),
        "feeder_count_delta_expand_all": int(feeder_lines_terminal_topology - feeder_lines),
        "feeder_count_delta_collapse_non_kvs": int(feeder_lines_collapse_non_kvs - feeder_lines),
        "buildings_per_feeder": float(buildings_per_feeder),
        "graph_length": float(graph_length),
        "avg_trafo_distance": float(avg_trafo_distance),
        "max_trafo_distance": float(max_trafo_distance),
        "transformer_mva": transformer_mva,
        "graph_resistance": float(graph_resistance),
    }


def compute_clustering_metrics(calculator: "ParameterCalculator", net: pp.pandapowerNet) -> Dict[str, Any]:
    """Compute the full clustering-oriented parameter set for one synthetic LV grid."""
    no_house_connections = calculator.count_buses_by_keyword(net, calculator.consumer_bus_keyword)
    no_connection_buses = calculator.count_buses_by_keyword(net, calculator.connection_bus_keyword)
    no_households = calculator.count_households(net)
    max_power_mw = calculator.calculate_total_installed_power(net)

    no_household_equ = max_power_mw * 1000.0 / PEAK_LOAD_HOUSEHOLD
    cable_length_km = calculator.calculate_cable_length(net)
    cable_len_per_house = cable_length_km / no_house_connections if no_house_connections > 0 else 0.0

    graph = pp.topology.create_nxgraph(net, respect_switches=True)
    root_idx = calculator.resolve_synthetic_root_bus(net)
    no_branches = calculator.count_feeders(net, graph, root_idx, uses_synthetic_naming=True)
    avg_trafo_dis, max_trafo_dis = calculator.calculate_trafo_distances_for_synthetic_grid(net, graph)

    if no_branches > 0:
        no_house_connections_per_branch = no_house_connections / no_branches
        no_households_per_branch = max_power_mw * 1000.0 / (PEAK_LOAD_HOUSEHOLD * no_branches)
    else:
        no_house_connections_per_branch = 0.0
        no_households_per_branch = 0.0

    transformer_mva = calculator.get_transformer_power(net)
    house_distance_km = calculator.calculate_average_house_distance(net)
    simultaneous_peak_load_mw = calculator.lookup_simultaneous_peak_load(transformer_mva, max_trafo_dis)

    (
        max_no_of_households_of_a_branch,
        resistance,
        reactance,
        ratio,
        max_vsw_of_a_branch,
    ) = calculator.calculate_impedance_metrics(net, graph)

    vsw_per_branch = resistance / no_branches if no_branches > 0 else 0.0

    return {
        "no_connection_buses": int(no_connection_buses),
        "no_branches": int(no_branches),
        "no_house_connections": int(no_house_connections),
        "no_house_connections_per_branch": float(no_house_connections_per_branch),
        "no_households": int(no_households),
        "no_household_equ": float(no_household_equ),
        "no_households_per_branch": float(no_households_per_branch),
        "max_no_of_households_of_a_branch": float(max_no_of_households_of_a_branch),
        "house_distance_km": float(house_distance_km),
        "transformer_mva": float(transformer_mva),
        "max_trafo_dis": float(max_trafo_dis),
        "avg_trafo_dis": float(avg_trafo_dis),
        "cable_length_km": float(cable_length_km),
        "cable_len_per_house": float(cable_len_per_house),
        "max_power_mw": float(max_power_mw),
        "simultaneous_peak_load_mw": float(simultaneous_peak_load_mw),
        "resistance": float(resistance),
        "reactance": float(reactance),
        "ratio": float(ratio),
        "vsw_per_branch": float(vsw_per_branch),
        "max_vsw_of_a_branch": float(max_vsw_of_a_branch),
    }