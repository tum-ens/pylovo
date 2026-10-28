"""Plots of single generated grids.

- :func:`plot_simple_grid`, :func:`plot_grid_on_map`, :func:`plot_with_generic_coordinates`:
  interactive pandapower/plotly plots.
- :func:`draw_tree_network`, :func:`draw_radial_network`: the grid topology as a tree, top-down or radial (root:
  the MV bus of the transformer, bus 1 in pylovo grids), laid out by :func:`tidy_tree_pos`. Pass the net to colour
  the cables by type and the consumers by category; the drawings need no database.

Maps of grids drawn from their layers are in :mod:`pylovo.plotting.lv_grid`.

Grids are read for the ``VERSION_ID`` of ``config_generation_lv.yaml`` unless a ``version_id`` is given. Needs the
``plots`` extra; :func:`plot_with_generic_coordinates` additionally needs ``python-igraph``.

The matplotlib functions return their figure and never call ``plt.show()``. A notebook with the
inline backend shows the open figure at the end of the cell (assign the result, ``fig = ...``, so
that it is shown once), a script closes it with ``plt.close(fig)`` after saving.

The plotly functions return their figure as well; they write no HTML file and open no browser. A
notebook shows the figure as the value of the cell, a script saves it with ``fig.write_html(...)``.
"""

import math
from typing import Optional, Tuple

import networkx as nx
import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from pandapower.plotting import create_generic_coordinates
from pandapower.plotting.plotly import simple_plotly

from pylovo.plotting import style as st

# The database client and the configuration are imported where a function reads a grid, so that the drawings of
# a net need no .env (example data of the tutorials).


def get_network_info_for_plotting(df_network_info: pd.Series) -> Tuple[int, int, int]:
    """Return ``(plz, kcid, bcid)`` of a grid from a row with these columns.

    Args:
        df_network_info: Row (e.g. of a representative-grid table) with ``plz``, ``kcid``, ``bcid``.

    Returns:
        Tuple ``(plz, kcid, bcid)`` as plain ints; the values of a DataFrame row are ``numpy.int64``,
        which psycopg2 cannot adapt when the grid is read from the database.
    """
    plz = int(df_network_info['plz'])
    kcid = int(df_network_info['kcid'])
    bcid = int(df_network_info['bcid'])
    return plz, kcid, bcid


def read_net_with_grid_generator(plz: int, kcid: int, bcid: int, version_id: Optional[str] = None):
    """Read a pandapower network from the database.

    The name is historic: the grid is read with a plain read-only
    :class:`~pylovo.database.database_client.DatabaseClient`, so no version row is written and a changed
    configuration does not block plotting.

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
        version_id: Version of the grid; defaults to the configured ``VERSION_ID``.

    Returns:
        The pandapower network.
    """
    from pylovo.database.database_client import DatabaseClient

    with DatabaseClient.read_only() as dbc_client:
        return dbc_client.read_net_db(plz=plz, kcid=kcid, bcid=bcid, version_id=version_id)


def plot_with_generic_coordinates(plz: int, kcid: int, bcid: int, version_id: Optional[str] = None):
    """Plot one grid with generic (topology-based) coordinates instead of its geodata (plotly).

    Needs the optional package ``python-igraph`` (pandapower raises an ImportError without it).

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
        version_id: Version of the grid; defaults to the configured ``VERSION_ID``.

    Returns:
        The plotly figure.
    """
    net = read_net_with_grid_generator(plz, kcid, bcid, version_id)

    # Clear the geodata so that the generic layout is used for buses and lines.
    if "geo" in net.bus.columns:
        net.bus["geo"] = None
    if "geo" in net.line.columns:
        net.line["geo"] = None

    # pandapower >= 3 keeps the bus coordinates in net.bus["geo"] (geodata_table="bus").
    generic_net = create_generic_coordinates(net, library='igraph', respect_switches=False, overwrite=True,
        geodata_table='bus')
    return simple_plotly(generic_net, aspectratio=(1, 1), filename=None, auto_open=False)


def plot_simple_grid(plz: int, kcid: int, bcid: int, version_id: Optional[str] = None):
    """Plot one grid with its geodata on a blank background (plotly).

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
        version_id: Version of the grid; defaults to the configured ``VERSION_ID``.

    Returns:
        The plotly figure.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid, version_id=version_id)
    return simple_plotly(net, filename=None, auto_open=False)


def plot_grid_on_map(plz: int, kcid: int, bcid: int, version_id: Optional[str] = None):
    """Plot one grid on an OpenStreetMap background (plotly map).

    pandapower checks ``on_map`` plots by reverse-geocoding the first bus with Nominatim, but passes
    the coordinates as "lon, lat" where Nominatim expects "lat, lon". For grids in Germany the
    lookup then lands in the Gulf of Aden, returns nothing and pandapower fails with
    ``AttributeError``. pylovo geodata is always WGS84 lon/lat, so the check is skipped.

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
        version_id: Version of the grid; defaults to the configured ``VERSION_ID``.

    Returns:
        The plotly figure.
    """
    from pylovo.plotting.utils import pandapower_on_map

    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid, version_id=version_id)
    with pandapower_on_map():
        fig = simple_plotly(net, on_map=True, map_style="open-street-map", filename=None, auto_open=False)
    return fig


def tidy_tree_pos(G: nx.Graph, root, y: str = "depth") -> dict:
    """Top-down positions of a tree: leaves evenly spaced, every parent centred above its children.

    Unlike a layout that gives every child the same share of the width, here the leaves decide the
    width, so large branches get room and nodes never overlap.

    Args:
        G: The graph (a tree; a ``MultiGraph`` of :func:`pandapower.topology.create_nxgraph` works).
        root: Root node.
        y: ``"depth"`` (one level per bus) or ``"distance"`` (cable distance from the root in metres, from the
            edge ``weight`` in km of pandapower's graph).

    Returns:
        ``{node: (x, y)}``; x counts leaves (0, 1, ...), y is 0 at the root and negative below it.
    """
    tree = nx.bfs_tree(G, root)
    children = {node: sorted(tree.successors(node), key=lambda n: (tree.out_degree(n) > 0, str(n)))
                for node in tree.nodes}
    order, stack = [], [root]           # pre-order: the leaves from left to right
    while stack:
        node = stack.pop()
        order.append(node)
        stack.extend(reversed(children[node]))
    x: dict = {}
    for node in order:
        if not children[node]:
            x[node] = float(len(x))
    for node in reversed(order):        # children before their parent
        if children[node]:
            values = [x[child] for child in children[node]]
            x[node] = (min(values) + max(values)) / 2
    if y == "distance":
        depth = nx.single_source_dijkstra_path_length(G, root, weight="weight")
        return {node: (x[node], -1000.0 * depth[node]) for node in x}
    level = nx.single_source_shortest_path_length(tree, root)
    return {node: (x[node], -float(level[node])) for node in x}


def _tables(net) -> bool:
    """Whether ``net`` is a pandapower net with the tables the drawings use."""
    return net is not None and all(hasattr(net, table) for table in ("trafo", "bus", "line"))


def _tree_root(G: nx.Graph, net=None):
    """The MV bus of the transformer (bus 1 in pylovo grids), else the first node."""
    if _tables(net) and not net.trafo.empty and int(net.trafo["hv_bus"].iloc[0]) in G:
        return int(net.trafo["hv_bus"].iloc[0])
    return 1 if 1 in G else next(iter(G.nodes))


def _station_buses(G: nx.Graph, net=None) -> set:
    if _tables(net) and not net.trafo.empty:
        return {int(net.trafo["hv_bus"].iloc[0]), int(net.trafo["lv_bus"].iloc[0])}
    return {node for node in (0, 1) if node in G}


def _draw_tree(G: nx.Graph, pos: dict, net=None, ax: Optional[plt.Axes] = None, labels: Optional[bool] = None,
               edge_color_by: Optional[str] = None, figsize: Optional[Tuple[float, float]] = None,
               title: Optional[str] = None, ylabel: Optional[str] = None) -> Figure:
    """Draw a tree layout of a grid: stations black, connection buses grey, consumers by category."""
    n = G.number_of_nodes()
    labels = n <= 60 if labels is None else labels
    node_size = 140 if n <= 60 else 50 if n <= 200 else 22 if n <= 600 else 10
    net = net if _tables(net) else None
    stations = _station_buses(G, net)
    zones = net.bus["zone"] if net is not None and "zone" in net.bus else pd.Series(dtype=object)
    consumers = [node for node in G if node not in stations and G.degree(node) == 1]
    connection = [node for node in G if node not in stations and node not in consumers]
    with st.style():
        if ax is None:
            fig, ax = plt.subplots(figsize=figsize or (7.4, 4.6))
        else:
            fig = ax.get_figure()
        # edges
        handles = []
        segments, colors, widths, styles = [], [], [], []
        cable_colors = {}
        if edge_color_by == "cable" and net is not None and "std_type" in net.line:
            cable_colors = st.cable_colors(net.line["std_type"].dropna())
        for u, v, key in (G.edges(keys=True) if G.is_multigraph() else ((a, b, None) for a, b in G.edges())):
            if u not in pos or v not in pos:
                continue
            segments.append((pos[u], pos[v]))
            if isinstance(key, tuple) and key[0] == "trafo":
                colors.append(st.INK), widths.append(1.6), styles.append("--")
            elif cable_colors and isinstance(key, tuple) and key[0] == "line" and key[1] in net.line.index:
                colors.append(cable_colors.get(net.line.at[key[1], "std_type"], st.INK_MUTED))
                widths.append(1.4 if v not in consumers and u not in consumers else 0.7)
                styles.append("-")
            else:
                colors.append(st.INK_SECONDARY), widths.append(0.8), styles.append("-")
        ax.add_collection(LineCollection(segments, colors=colors, linewidths=widths, linestyles=styles, zorder=1,
                                         capstyle="round"))
        ax.autoscale_view()
        if cable_colors:
            handles += [Line2D([], [], color=c, lw=2.5, label=t) for t, c in cable_colors.items()]
        else:
            handles.append(Line2D([], [], color=st.INK_SECONDARY, lw=1, label="Cable"))
        handles.append(Line2D([], [], color=st.INK, lw=1.6, ls="--", label="Transformer"))
        # nodes
        def scatter(nodes, color, size, label, marker="o"):
            nodes = [node for node in nodes if node in pos]
            if nodes:
                ax.scatter([pos[node][0] for node in nodes], [pos[node][1] for node in nodes], s=size, color=color,
                           marker=marker, edgecolor="white", lw=0.6, zorder=3)
                handles.append(Line2D([], [], marker=marker, ls="", color=color, markeredgecolor="white",
                                      markersize=6, label=label))
        scatter(connection, st.INK_MUTED, node_size * 0.45, f"Connection bus ({len(connection)})")
        if net is not None and len(zones):
            uses = [zones.get(node, None) for node in consumers if node in pos]
            for use in sorted({u for u in uses if u is not None}, key=str):
                group = [node for node in consumers if node in pos and zones.get(node) == use]
                scatter(group, st.USE_COLORS.get(use, st.CATEGORICAL[0]), node_size, f"{use} consumer ({len(group)})")
        else:
            scatter(consumers, st.CATEGORICAL[0], node_size, f"Consumer ({len(consumers)})")
        scatter(sorted(stations), st.INK, node_size * 1.6, "Station (MV and LV bus)", marker="s")
        if labels:
            for node in G:
                if node in pos:
                    ax.annotate(str(node), pos[node], xytext=(0, 5), textcoords="offset points", ha="center",
                                fontsize=6, color=st.INK_SECONDARY, zorder=4)
        ax.set_xticks([])
        for side in ("top", "right", "bottom"):
            ax.spines[side].set_visible(False)
        if ylabel:
            ax.set_ylabel(ylabel)
        else:
            ax.set_yticks([])
            ax.spines["left"].set_visible(False)
        ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=7.5)
        if title:
            ax.set_title(title)
    return fig


def _tree_figsize(pos: dict, base_height: float = 4.6) -> Tuple[float, float]:
    width = max(x for x, _ in pos.values()) + 1 if pos else 1
    return float(np.clip(1.5 + 0.11 * width, 6.0, 18.0)), base_height


def draw_tree_network(G, width=1., *, net=None, ax: Optional[plt.Axes] = None, labels: Optional[bool] = None,
                      edge_color_by: Optional[str] = None, y: str = "depth",
                      title: Optional[str] = None) -> Figure:
    """Draw the grid graph as a tree, from the MV bus of the transformer down to the consumers.

    The leaves (consumers) are evenly spaced and every bus sits above the buses it supplies
    (:func:`tidy_tree_pos`), so feeders appear as branches and nodes do not overlap.

    Args:
        G: Graph of the grid, e.g. ``pandapower.topology.create_nxgraph(net)``.
        width: Horizontal scale of the drawing.
        net: The pandapower net of the graph: roots the tree at its transformer, colours consumers by category
            and, with ``edge_color_by="cable"``, the cables by type.
        ax: Axes to draw into; a new figure if None.
        labels: Write the bus indices; default only for graphs of at most 60 buses.
        edge_color_by: ``"cable"`` (needs ``net``) or None.
        y: ``"depth"`` (one level per bus) or ``"distance"`` (cable distance from the station in metres).
        title: Title of the drawing.

    Returns:
        The figure with the drawing.
    """
    pos = tidy_tree_pos(G, _tree_root(G, net), y=y)
    pos = {node: (x * width, yy) for node, (x, yy) in pos.items()}
    return _draw_tree(G, pos, net=net, ax=ax, labels=labels, edge_color_by=edge_color_by,
                      figsize=_tree_figsize(pos), title=title,
                      ylabel="Cable distance from the station (m)" if y == "distance" else None)


def draw_radial_network(G, *, net=None, ax: Optional[plt.Axes] = None, labels: Optional[bool] = None,
                        edge_color_by: Optional[str] = None, radius: str = "depth",
                        title: Optional[str] = None) -> Figure:
    """Draw the grid graph radially: the station in the centre, the feeders as spokes, the consumers outside.

    The angles come from the leaf order of :func:`tidy_tree_pos`, so every consumer has its own direction; the
    radius is the number of cables (``"depth"``) or the cable distance from the station (``"distance"``).

    Args:
        G: Tree graph of the grid.
        net: The pandapower net of the graph (consumer categories, cable types, root).
        ax: Axes to draw into; a new figure if None.
        labels: Write the bus indices; default only for graphs of at most 60 buses.
        edge_color_by: ``"cable"`` (needs ``net``) or None.
        radius: ``"depth"`` or ``"distance"``.
        title: Title of the drawing.

    Returns:
        The figure with the drawing.
    """
    tidy = tidy_tree_pos(G, _tree_root(G, net), y=radius)
    leaves = max(x for x, _ in tidy.values()) + 1
    pos = {node: (-yy * math.cos(2 * math.pi * x / leaves), -yy * math.sin(2 * math.pi * x / leaves))
           for node, (x, yy) in tidy.items()}
    fig = _draw_tree(G, pos, net=net, ax=ax, labels=labels, edge_color_by=edge_color_by, figsize=(7.4, 6.4),
                     title=title)
    drawn = ax if ax is not None else fig.axes[0]      # not fig.axes[0]: that may be another drawing of the figure
    drawn.set_aspect("equal")
    drawn.axis("off")
    return fig
