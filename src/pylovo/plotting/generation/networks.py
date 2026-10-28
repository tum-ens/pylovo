"""Plots of single generated grids.

- :func:`plot_contextily`: lines, buildings (peak load) and transformer on an OSM basemap (matplotlib).
- :func:`plot_simple_grid`, :func:`plot_grid_on_map`, :func:`plot_with_generic_coordinates`:
  interactive pandapower/plotly plots.
- :func:`draw_tree_network`, :func:`draw_tree_network_spacing`, :func:`draw_radial_network`:
  the grid topology as a tree, top-down or radial (root: the MV bus of the transformer, bus 1 in pylovo grids).
  Pass the net to colour the cables by type and the consumers by category; the drawings need no database.

Grids are read for the ``VERSION_ID`` of ``config_generation_lv.yaml`` unless a ``version_id`` is given. Needs the
``plots`` extra; :func:`plot_with_generic_coordinates` additionally needs ``python-igraph``.

The matplotlib functions return their figure and never call ``plt.show()``. A notebook with the
inline backend shows the open figure at the end of the cell (assign the result, ``fig = ...``, so
that it is shown once), a script closes it with ``plt.close(fig)`` after saving.

The plotly functions return their figure as well; they write no HTML file and open no browser. A
notebook shows the figure as the value of the cell, a script saves it with ``fig.write_html(...)``.
"""

import math
import random
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
from pandapower.topology import create_nxgraph

from pylovo.plotting import style as st
from pylovo.plotting._deprecation import deprecated

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


@deprecated("draw_tree_network(G, net=net), which colours the cables by type")
def get_colormap_for_treegraph(networkx_graph: nx.Graph) -> list:
    """Return one colour per node for the tree plots.

    .. deprecated:: 1.1
       Use draw_tree_network(G, net=net), which colours the cables by type; removed in pylovo 2.0.

    Buses 0 and 1 (LV and MV bus of the transformer) get ``NODE_COLOR_TRAFO``, leaves (degree 1,
    consumers) ``NODE_COLOR_CONSUMER`` and all other nodes ``NODE_COLOR_CONNECTION_BUS``
    (``NETWORK_COLORS`` in ``config_analysis.yaml``).

    Args:
        networkx_graph: Graph of the grid, e.g. from :func:`pandapower.topology.create_nxgraph`.

    Returns:
        Colours in the order of ``networkx_graph.nodes()``.
    """
    try:
        from pylovo.config_loader import NODE_COLOR_CONNECTION_BUS, NODE_COLOR_CONSUMER, NODE_COLOR_TRAFO
    except ValueError:      # no .env: the colours of the pylovo style
        NODE_COLOR_TRAFO, NODE_COLOR_CONSUMER, NODE_COLOR_CONNECTION_BUS = st.INK, st.CATEGORICAL[0], st.INK_MUTED
    color_map = []
    for node in networkx_graph.nodes():
        if node == 1 or node == 0:
            color_map.append(NODE_COLOR_TRAFO)
        elif networkx_graph.degree(node) == 1:
            color_map.append(NODE_COLOR_CONSUMER)
        else:
            color_map.append(NODE_COLOR_CONNECTION_BUS)
    return color_map


@deprecated("pylovo.plotting.lv_grid.plot_lv_grid (vector map layers, no tile download)")
def plot_contextily(plz: int, kcid: int, bcid: int, zoomfactor: int = 19, ax: Optional[plt.Axes] = None,
        figsize: Tuple[int, int] = (8, 8)) -> Figure:
    """Plot the lines, buildings and transformer of one grid on an OpenStreetMap basemap.

    .. deprecated:: 1.1
       Use pylovo.plotting.lv_grid.plot_lv_grid (vector map layers, no tile download); removed in pylovo 2.0.

    The basemap tiles are downloaded by contextily (internet access needed).

    Args:
        plz: Postal code of the grid.
        kcid: K-means cluster id of the grid.
        bcid: Building cluster id of the grid.
        zoomfactor: Zoom level of the basemap tiles.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.

    Returns:
        The figure that contains the plot.
    """
    import contextily as cx

    from pylovo.config_loader import TARGET_EPSG
    from pylovo.database.database_client import DatabaseClient
    from pylovo.plotting.utils import OSM_TILE_HEADERS

    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()

    ax.set_xticks([])
    ax.set_yticks([])

    grid_filter = {"plz": int(plz), "kcid": int(kcid), "bcid": int(bcid)}
    with DatabaseClient() as dbc_client:
        buildings_gdf = dbc_client.get_geo_df_join(
            ["gr.version_id", "plz", "kcid", "bcid", "br.*"], "buildings_result br", "grid_result gr",
            ("br.grid_result_id", "gr.grid_result_id"), **grid_filter)
        line_gdf = dbc_client.get_geo_df_join(
            [
                "pl.*",
                "gr.kcid",
                "gr.bcid",
                "gr.plz",
                f"ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(pl.geo::text), 4326), {TARGET_EPSG}) AS geom",
            ],
            "pandapower_line pl",
            "grid_result gr",
            ("pl.grid_result_id", "gr.grid_result_id"),
            **grid_filter,
        )
        trafo_gdf = dbc_client.get_geo_df_join(
            ["geom"], "transformer_positions tp", "grid_result gr",
            ("tp.grid_result_id", "gr.grid_result_id"), **grid_filter)

    ax = line_gdf.plot(ax=ax, edgecolor="black", linewidth=1, label="Lines")
    ax = buildings_gdf.plot(ax=ax, column="peak_load_in_kw", cmap="YlOrBr", legend=True,
        legend_kwds={'label': "Peak load in kW"})
    trafo_point = trafo_gdf.geom.iloc[0]
    ax.scatter(trafo_point.x, trafo_point.y, marker=(5, 0), s=80, color="blue", label="Transformer")

    cx.add_basemap(ax, crs=buildings_gdf.crs.to_string(), zoom=zoomfactor,
                   source=cx.providers.OpenStreetMap.Mapnik, headers=OSM_TILE_HEADERS)
    ax.legend()

    return fig


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


@deprecated("pylovo.plotting.generation.networks.tidy_tree_pos")
def hierarchy_pos(G, root=None, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5):
    """Return hierarchical (top-down) layout positions of a tree.

    .. deprecated:: 1.1
       Use pylovo.plotting.generation.networks.tidy_tree_pos; removed in pylovo 2.0.

    From Joel's answer at https://stackoverflow.com/a/29597209/2966723
    (CC BY-SA).

    Args:
        G: The graph; must be a tree.
        root: Root node. For a directed tree it defaults to the topological root (if given, only
            its descendants are placed); for an undirected tree to a random node.
        width: Horizontal space of the whole tree (siblings split their parent's width).
        vert_gap: Vertical gap between levels.
        vert_loc: Vertical position of the root.
        xcenter: Horizontal position of the root.

    Returns:
        Dictionary ``{node: (x, y)}``.

    Raises:
        TypeError: If ``G`` is not a tree.
    """
    if not nx.is_tree(G):
        raise TypeError('cannot use hierarchy_pos on a graph that is not a tree')

    if root is None:
        if isinstance(G, nx.DiGraph):
            root = next(iter(nx.topological_sort(G)))
        else:
            root = random.choice(list(G.nodes))

    def _hierarchy_pos(G, root, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5, pos=None, parent=None):
        """Place ``root`` and recurse into its children (``parent`` is skipped in undirected trees)."""
        if pos is None:
            pos = {root: (xcenter, vert_loc)}
        else:
            pos[root] = (xcenter, vert_loc)
        children = list(G.neighbors(root))
        if not isinstance(G, nx.DiGraph) and parent is not None:
            children.remove(parent)
        if len(children) != 0:
            dx = width / len(children)
            nextx = xcenter - width / 2 - dx / 2
            for child in children:
                nextx += dx
                pos = _hierarchy_pos(G, child, width=dx, vert_gap=vert_gap, vert_loc=vert_loc - vert_gap, xcenter=nextx,
                                     pos=pos, parent=root)
        return pos

    return _hierarchy_pos(G, root, width, vert_gap, vert_loc, xcenter)


@deprecated("pylovo.plotting.generation.networks.tidy_tree_pos")
def hierarchy_pos2(G, root, levels=None, width=1., height=1.):
    """Return hierarchical layout positions with the nodes of each level evenly spaced.

    .. deprecated:: 1.1
       Use pylovo.plotting.generation.networks.tidy_tree_pos; removed in pylovo 2.0.

    Recurses without a visited set, so a cycle reachable from ``root`` recurses forever.

    Args:
        G: The graph (a tree).
        root: Root node.
        levels: Optional ``{level: number of nodes}``; computed from ``G`` if None.
        width: Horizontal space of the drawing.
        height: Vertical space of the drawing.

    Returns:
        Dictionary ``{node: (x, y)}``.
    """
    TOTAL = "total"
    CURRENT = "current"

    def make_levels(levels, node=root, currentLevel=0, parent=None):
        """Compute the number of nodes for each level."""
        if currentLevel not in levels:
            levels[currentLevel] = {TOTAL: 0, CURRENT: 0}
        levels[currentLevel][TOTAL] += 1
        neighbors = G.neighbors(node)
        for neighbor in neighbors:
            if not neighbor == parent:
                levels = make_levels(levels, neighbor, currentLevel + 1, node)
        return levels

    def make_pos(pos, node=root, currentLevel=0, parent=None, vert_loc=0):
        """Create position dictionary."""
        dx = 1 / levels[currentLevel][TOTAL]
        left = dx / 2
        pos[node] = ((left + dx * levels[currentLevel][CURRENT]) * width, vert_loc)
        levels[currentLevel][CURRENT] += 1
        neighbors = G.neighbors(node)
        for neighbor in neighbors:
            if not neighbor == parent:
                pos = make_pos(pos, neighbor, currentLevel + 1, node, vert_loc - vert_gap)
        return pos

    if levels is None:
        levels = make_levels({})
    else:
        levels = {level: {TOTAL: levels[level], CURRENT: 0} for level in levels}
    vert_gap = height / (max(levels) + 1)
    return make_pos({})


def tidy_tree_pos(G: nx.Graph, root, y: str = "depth") -> dict:
    """Top-down positions of a tree: leaves evenly spaced, every parent centred above its children.

    Unlike :func:`hierarchy_pos`, which gives every child the same share of the width, the leaves decide the
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


@deprecated("draw_tree_network with a net from DatabaseClient.read_net_db or pylovo.examples.TutorialData")
def draw_tree_network_with_spacing_from_grid_id(plz: int, kcid: int, bcid: int,
                                                version_id: Optional[str] = None) -> Figure:
    """Read one grid and draw it with :func:`draw_tree_network_spacing`, cables coloured by type.

    .. deprecated:: 1.1
       Use draw_tree_network with a net from DatabaseClient.read_net_db or pylovo.examples.TutorialData; removed in pylovo 2.0.

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
        version_id: Version of the grid; defaults to the configured ``VERSION_ID``.

    Returns:
        The figure with the drawing.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid, version_id=version_id)
    G = create_nxgraph(net)
    return draw_tree_network_spacing.__wrapped__(G, net=net, edge_color_by="cable")


@deprecated('draw_tree_network(G, net=net, y="distance") or draw_radial_network')
def draw_tree_network_spacing(G, *, net=None, ax: Optional[plt.Axes] = None, labels: Optional[bool] = None,
                              edge_color_by: Optional[str] = None, title: Optional[str] = None) -> Figure:
    """Draw the grid graph as a tree with the buses of each level evenly spaced (:func:`hierarchy_pos2`).

    .. deprecated:: 1.1
       Use draw_tree_network(G, net=net, y="distance") or draw_radial_network; removed in pylovo 2.0.

    Each level holds the buses at the same number of cables from the MV bus; spreading them evenly fills the
    width, also for large grids, at the cost of longer edges than :func:`draw_tree_network`.

    Args:
        G: Tree graph of the grid with bus 1 (MV bus) as root.
        net: The pandapower net of the graph (consumer categories, cable types, root).
        ax: Axes to draw into; a new figure if None.
        labels: Write the bus indices; default only for graphs of at most 60 buses.
        edge_color_by: ``"cable"`` (needs ``net``) or None.
        title: Title of the drawing.

    Returns:
        The figure with the drawing.
    """
    root = _tree_root(G, net)
    simple = nx.Graph(G)
    pos = hierarchy_pos2.__wrapped__(simple, root=root, width=max(6.0, 0.11 * simple.number_of_nodes()), height=4.0)
    return _draw_tree(G, pos, net=net, ax=ax, labels=labels, edge_color_by=edge_color_by,
                      figsize=(float(np.clip(1.5 + 0.06 * simple.number_of_nodes(), 6.0, 18.0)), 4.6), title=title)


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
