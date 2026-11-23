"""The key figures of one synthetic LV grid, composed from the figures of its pandapower net.

- :func:`key_figures`: the figures stored per grid by ``pylovo-analyze`` (table ``clustering_parameters``), from
  :mod:`pylovo.analysis.net_metrics` and :mod:`pylovo.analysis.impedance_metrics`.

The structural metrics that compare grids of two datasets are in :mod:`pylovo.analysis.comparison`.
"""

import math
from typing import Any, Dict

import pandapower as pp

from pylovo.analysis import impedance_metrics, net_metrics
from pylovo.config_loader import PEAK_LOAD_HOUSEHOLD

#: Version of the definitions of :func:`key_figures`, stored with every row of ``clustering_parameters``
#: and with the classifications clustered on them. Raise it when a definition changes: the per-grid analysis then
#: computes stored rows again. 1: pylovo 1.0.0; 2: the feeders, branch ends and route resistance of the structural
#: metrics, the reactance weighted like the resistance.
KEY_FIGURES_VERSION = 2


def key_figures(net: pp.pandapowerNet) -> Dict[str, Any]:
    """Compute the key figures of one synthetic LV grid from its net.

    ``simultaneous_peak_load_mw`` is the coincident peak load of the grid's own loads, with the
    formula of the PLZ-level transformer statistics
    (:func:`~pylovo.analysis.net_metrics.simultaneous_peak_load`).
    A branch is a feeder of the structural metrics of the validation: a cable that leaves the station towards at
    least two buildings (:func:`~pylovo.analysis.net_metrics.feeder_structure`).
    ``no_branches``, the per-branch figures and the figures of the largest branch use these feeders and the buildings
    behind them; a grid without a feeder has 0 of each. ``no_branch_ends`` and ``max_route_resistance_ohm`` are the
    branch ends and the highest route resistance of the structural metrics.

    Args:
        net: Synthetic pylovo grid.

    Returns:
        Dictionary with the columns of the ``clustering_parameters`` table (without ids).
    """
    no_house_connections = net_metrics.count_buses_by_keyword(net, net_metrics.CONSUMER_BUS_KEYWORD)
    no_connection_buses = net_metrics.count_buses_by_keyword(net, net_metrics.CONNECTION_BUS_KEYWORD)
    no_households = net_metrics.count_households(net)
    max_power_mw = net_metrics.total_installed_power(net)

    no_household_equ = max_power_mw * 1000.0 / PEAK_LOAD_HOUSEHOLD
    cable_length_km = net_metrics.cable_length(net)
    cable_len_per_house = cable_length_km / no_house_connections if no_house_connections > 0 else 0.0

    graph = pp.topology.create_nxgraph(net, respect_switches=True)
    root_idx = net_metrics.resolve_synthetic_root_bus(net)
    consumer_buses = net_metrics.resolve_consumer_buses(net, uses_synthetic_naming=True)
    feeder_of_bus, no_branch_ends = net_metrics.feeder_structure(net, root_idx, consumer_buses)
    no_branches = len(set(feeder_of_bus.values()))
    max_route_resistance_ohm = net_metrics.max_route_resistance(net, root_idx, consumer_buses)
    avg_trafo_dis, max_trafo_dis = net_metrics.transformer_distances(net, graph)

    if no_branches > 0:
        behind = net.load["bus"].isin(list(feeder_of_bus))
        load_mw_behind_feeders = net.load.loc[behind, "max_p_mw"].sum() if "max_p_mw" in net.load.columns else 0.0
        no_house_connections_per_branch = len(feeder_of_bus) / no_branches
        no_household_equ_per_branch = load_mw_behind_feeders * 1000.0 / (PEAK_LOAD_HOUSEHOLD * no_branches)
    else:
        no_house_connections_per_branch = 0.0
        no_household_equ_per_branch = 0.0

    transformer_mva = net_metrics.transformer_power(net)
    house_distance_km = net_metrics.average_house_distance(net)
    simultaneous_peak_load_mw = net_metrics.simultaneous_peak_load(net) / 1000.0

    (
        max_household_equ_of_a_branch,
        resistance,
        reactance,
        ratio,
        max_vsw_of_a_branch,
        feeder_resistance,
    ) = impedance_metrics.impedance_metrics(net, graph, feeder_of_bus)

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
