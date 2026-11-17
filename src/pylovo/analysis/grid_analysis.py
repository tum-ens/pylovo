"""Compose named LV grid parameter sets from the :class:`ParameterCalculator` toolbox.

- :func:`compute_clustering_metrics`: parameters stored per synthetic grid by ``pylovo-analyze``.

The structural metrics that compare grids of two datasets are in :mod:`pylovo.analysis.comparison`.
"""

import math
from typing import TYPE_CHECKING, Any, Dict

import pandapower as pp

from pylovo.config_loader import PEAK_LOAD_HOUSEHOLD

if TYPE_CHECKING:
    from pylovo.analysis.parameter_calculation import ParameterCalculator

#: Version of the definitions of :func:`compute_clustering_metrics`, stored with every row of ``clustering_parameters``
#: and with the classifications clustered on them. Raise it when a definition changes: the per-grid analysis then
#: computes stored rows again. 1: pylovo 1.0.0; 2: the feeders, branch ends and route resistance of the structural
#: metrics, the reactance weighted like the resistance.
KEY_FIGURES_VERSION = 2


def compute_clustering_metrics(calculator: "ParameterCalculator", net: pp.pandapowerNet) -> Dict[str, Any]:
    """Compute the clustering parameter set of one synthetic LV grid.

    ``simultaneous_peak_load_mw`` is the coincident peak load of the grid's own loads, with the
    formula of the PLZ-level transformer statistics
    (:meth:`~pylovo.analysis.parameter_calculation.ParameterCalculator.calculate_simultaneous_peak_load`).
    A branch is a feeder of the structural metrics of the validation: a cable that leaves the station towards at
    least two buildings (:meth:`~pylovo.analysis.parameter_calculation.ParameterCalculator.feeder_structure`).
    ``no_branches``, the per-branch figures and the figures of the largest branch use these feeders and the buildings
    behind them; a grid without a feeder has 0 of each. ``no_branch_ends`` and ``max_route_resistance_ohm`` are the
    branch ends and the highest route resistance of the structural metrics.

    Args:
        calculator: Calculator with the PLZ context of the grid.
        net: Synthetic pylovo grid.

    Returns:
        Dictionary with the columns of the ``clustering_parameters`` table (without ids).
    """
    no_house_connections = calculator.count_buses_by_keyword(net, calculator.consumer_bus_keyword)
    no_connection_buses = calculator.count_buses_by_keyword(net, calculator.connection_bus_keyword)
    no_households = calculator.count_households(net)
    max_power_mw = calculator.calculate_total_installed_power(net)

    no_household_equ = max_power_mw * 1000.0 / PEAK_LOAD_HOUSEHOLD
    cable_length_km = calculator.calculate_cable_length(net)
    cable_len_per_house = cable_length_km / no_house_connections if no_house_connections > 0 else 0.0

    graph = pp.topology.create_nxgraph(net, respect_switches=True)
    root_idx = calculator.resolve_synthetic_root_bus(net)
    consumer_buses = calculator.resolve_consumer_buses(net, uses_synthetic_naming=True)
    feeder_of_bus, no_branch_ends = calculator.feeder_structure(net, root_idx, consumer_buses)
    no_branches = len(set(feeder_of_bus.values()))
    max_route_resistance_ohm = calculator.calculate_max_route_resistance(net, root_idx, consumer_buses)
    avg_trafo_dis, max_trafo_dis = calculator.calculate_trafo_distances_for_synthetic_grid(net, graph)

    if no_branches > 0:
        behind = net.load["bus"].isin(list(feeder_of_bus))
        load_mw_behind_feeders = net.load.loc[behind, "max_p_mw"].sum() if "max_p_mw" in net.load.columns else 0.0
        no_house_connections_per_branch = len(feeder_of_bus) / no_branches
        no_household_equ_per_branch = load_mw_behind_feeders * 1000.0 / (PEAK_LOAD_HOUSEHOLD * no_branches)
    else:
        no_house_connections_per_branch = 0.0
        no_household_equ_per_branch = 0.0

    transformer_mva = calculator.get_transformer_power(net)
    house_distance_km = calculator.calculate_average_house_distance(net)
    simultaneous_peak_load_mw = calculator.calculate_simultaneous_peak_load(net) / 1000.0

    (
        max_household_equ_of_a_branch,
        resistance,
        reactance,
        ratio,
        max_vsw_of_a_branch,
        feeder_resistance,
    ) = calculator.calculate_impedance_metrics(net, graph, feeder_of_bus)

    vsw_per_branch = feeder_resistance / no_branches if no_branches > 0 else 0.0

    return {
        "no_connection_buses": int(no_connection_buses),
        "no_branches": int(no_branches),
        "no_branch_ends": int(no_branch_ends),
        "no_house_connections": int(no_house_connections),
        "no_house_connections_per_branch": float(no_house_connections_per_branch),
        "no_households": int(no_households),
        "no_household_equ": float(no_household_equ),
        "no_household_equ_per_branch": float(no_household_equ_per_branch),
        "max_household_equ_of_a_branch": float(max_household_equ_of_a_branch),
        "house_distance_km": None if math.isnan(house_distance_km) else float(house_distance_km),   # NULL if undefined
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
        "max_route_resistance_ohm": float(max_route_resistance_ohm),
    }
