"""Feeders, distances and voltage profiles of one LV grid (a pandapower net with power-flow results).

- :func:`feeder_ids`: the feeder (1, 2, ...) of every line: each line that leaves the LV busbar starts one.
- :func:`bus_distances`: the cable distance of every bus from the LV busbar.
- :func:`plot_feeder_voltage_profile`: the voltage along every feeder over the distance from the station, with
  the voltage band of the version.

Needs the ``plots`` extra; reads no database.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

import matplotlib.pyplot as plt
import networkx as nx
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from pylovo.plotting import style as st

__all__ = ["bus_distances", "feeder_ids", "lv_busbar", "plot_feeder_voltage_profile", "voltage_band"]


def lv_busbar(net: Any) -> int:
    """The LV bus of the (first) transformer of an LV grid: the root of its feeders."""
    if net.trafo.empty:
        raise ValueError("the net has no transformer")
    return int(net.trafo["lv_bus"].iloc[0])


def _line_graph(net: Any) -> nx.Graph:
    graph = nx.Graph()
    lines = net.line[net.line["in_service"].fillna(True).astype(bool)] if "in_service" in net.line else net.line
    for index, row in lines.iterrows():
        graph.add_edge(int(row["from_bus"]), int(row["to_bus"]), index=int(index),
                       length_m=float(row["length_km"]) * 1000.0)
    return graph


def _feeder_numbers(graph: nx.Graph, root: int) -> pd.Series:
    """``{edge index: feeder}`` by a search from ``root``; feeders are numbered in the order of their first line."""
    feeders: dict[int, int] = {}
    if root not in graph:
        return pd.Series(feeders, dtype=float)
    first = sorted(graph.edges(root, data="index"), key=lambda edge: edge[2])
    for number, (_, child, index) in enumerate(first, start=1):
        feeders[index] = number
        stack, seen = [child], {root, child}
        while stack:
            node = stack.pop()
            for _, nxt, idx in graph.edges(node, data="index"):
                if nxt not in seen:
                    seen.add(nxt)
                    feeders[idx] = number
                    stack.append(nxt)
    return pd.Series(feeders, dtype=float)


def feeder_ids(net: Any) -> pd.Series:
    """The feeder of every line of an LV grid (index: line index of the net, values 1, 2, ...).

    Every line that leaves the LV busbar of the transformer starts a feeder; all lines behind it belong to it.
    Lines out of service and lines that the busbar cannot reach get no feeder.
    """
    return _feeder_numbers(_line_graph(net), lv_busbar(net))


def bus_distances(net: Any) -> pd.Series:
    """Cable distance (m) of every bus from the LV busbar along the in-service lines (index: bus index)."""
    graph = _line_graph(net)
    root = lv_busbar(net)
    if root not in graph:
        return pd.Series(dtype=float)
    return pd.Series(nx.single_source_dijkstra_path_length(graph, root, weight="length_m"), dtype=float)


def voltage_band(parameters: Optional[Mapping[str, Any]] = None) -> dict:
    """The voltage band of a version: ``lv_reference_pu`` (LV busbar at the load of the validation power flow),
    ``min_vm_pu`` and ``max_vm_pu``.

    Args:
        parameters: The generation parameter snapshot of the version (``read_generation_parameters``); without
            it the values are those of the configuration (``LV_REFERENCE_VOLTAGE_PU``, ``POWER_FLOW_*_VM_PU``).
    """
    if parameters:
        flow = parameters.get("power_flow_assessment") or {}
        return {"lv_reference_pu": flow.get("lv_reference_voltage_pu"), "min_vm_pu": flow.get("min_vm_pu", 0.9),
                "max_vm_pu": flow.get("max_vm_pu", 1.1)}
    from pylovo import config_loader as cfg

    return {"lv_reference_pu": cfg.LV_REFERENCE_VOLTAGE_PU, "min_vm_pu": cfg.POWER_FLOW_MIN_VM_PU,
            "max_vm_pu": cfg.POWER_FLOW_MAX_VM_PU}


def plot_feeder_voltage_profile(net: Any, band: Optional[Mapping[str, Any]] = None, ax: Optional[plt.Axes] = None,
                                figsize: Tuple[float, float] = (7.4, 4.2), title: Optional[str] = None) -> Figure:
    """The voltage along every feeder over the cable distance from the station.

    Every cable is a segment from the voltage and distance of its first bus to those of its second, coloured by
    feeder; dots mark the house connections (buses with a load). The voltage band of the version frames the
    profile: the LV busbar at its design voltage, the lower limit at the customers.

    Args:
        net: pandapower net of one LV grid with power-flow results (``net.res_bus``), e.g. from ``read_net_db``.
        band: ``{"lv_reference_pu", "min_vm_pu", ...}`` as from :func:`voltage_band`; default the configuration.
        ax: Axes to draw into; a new figure if None.
        figsize: Size of a new figure.
        title: Title; default names the lowest voltage.

    Returns:
        The figure.

    Raises:
        ValueError: The net has no power-flow results.
    """
    if net.res_bus.empty or net.res_bus["vm_pu"].isna().all():
        raise ValueError("the net has no power-flow results; run pandapower.runpp(net) first")
    band = voltage_band() if band is None else band
    vm = net.res_bus["vm_pu"]
    distance = bus_distances(net)
    feeders = feeder_ids(net)
    colors = st.categorical_colors(feeders.dropna().astype(int))
    load_buses = set(net.load["bus"].astype(int)) if not net.load.empty else set()
    with st.style():
        if ax is None:
            fig, ax = plt.subplots(figsize=figsize)
        else:
            fig = ax.get_figure()
        segments, segment_colors = [], []
        for index, row in net.line.iterrows():
            a, b = int(row["from_bus"]), int(row["to_bus"])
            if a in distance and b in distance and index in feeders:
                segments.append([(distance[a], vm[a]), (distance[b], vm[b])])
                segment_colors.append(colors[int(feeders[index])])
        ax.add_collection(LineCollection(segments, colors=segment_colors, linewidths=1.4, zorder=3))
        bus_feeder = {}
        for index, row in net.line.iterrows():
            if index in feeders:
                for bus in (int(row["from_bus"]), int(row["to_bus"])):
                    bus_feeder.setdefault(bus, int(feeders[index]))
        consumers = [b for b in load_buses if b in distance and b in bus_feeder]
        ax.scatter([distance[b] for b in consumers], [vm[b] for b in consumers],
                   c=[colors[bus_feeder[b]] for b in consumers], s=12, zorder=4, edgecolor=st.SURFACE, lw=0.4)
        root = lv_busbar(net)
        ax.scatter([0], [vm[root]], marker="*", s=160, color=st.INK, edgecolor="white", lw=1, zorder=5)
        xmax = max(distance.max() if len(distance) else 1.0, 1.0) * 1.04
        handles = [Line2D([], [], marker="*", ls="", color=st.INK, markeredgecolor="white", markersize=10,
                          label=f"LV busbar ({vm[root]:.3f} p.u.)")]
        reference = band.get("lv_reference_pu")
        if reference:
            ax.axhline(reference, color=st.INK_SECONDARY, lw=0.9, ls=":", zorder=2)
            handles.append(Line2D([], [], color=st.INK_SECONDARY, lw=0.9, ls=":",
                                  label=f"Busbar design voltage {reference:g} p.u."))
        low = band.get("min_vm_pu")
        bottom = min(float(vm.min()), low if low else float(vm.min())) - 0.01
        top = max(float(vm.max()), reference or 0) + 0.01
        if low:
            ax.axhline(low, color=st.STATUS["critical"], lw=1.0, zorder=2)
            ax.axhspan(bottom - 1, low, color=st.STATUS["critical"], alpha=0.06, lw=0, zorder=1)
            handles.append(Line2D([], [], color=st.STATUS["critical"], lw=1.0, label=f"Lower limit {low:g} p.u."))
        handles += [Line2D([], [], color=colors[f], lw=2, label=f"Feeder {f}") for f in sorted(colors)]
        handles.append(Line2D([], [], marker="o", ls="", color=st.INK_MUTED, markersize=4, label="House connection"))
        ax.set_xlim(-xmax * 0.02, xmax)
        ax.set_ylim(bottom, top)
        ax.set_xlabel("Cable distance from the station (m)")
        ax.set_ylabel("Voltage (p.u.)")
        st.tidy_value_axes(ax, "both")
        ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.01, 1.0), fontsize=7.5)
        lowest = float(vm[list(load_buses)].min()) if load_buses else float(vm.min())
        ax.set_title(title if title is not None else
                     f"Voltage along the feeders: lowest {lowest:.3f} p.u. at a house connection")
    return fig
