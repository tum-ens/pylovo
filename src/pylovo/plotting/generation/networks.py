"""Plots of single generated grids.

- :func:`plot_contextily`: lines, buildings (peak load) and transformer on an OSM basemap (matplotlib).
- :func:`plot_simple_grid`, :func:`plot_grid_on_map`, :func:`plot_with_generic_coordinates`:
  interactive pandapower/plotly plots.
- :func:`draw_tree_network`, :func:`draw_tree_network_spacing`, :func:`draw_radial_network`:
  networkx tree layouts of the grid topology (root: bus 1, the MV bus of pylovo grids).

Grids are read for the ``VERSION_ID`` of ``config_generation.yaml``. Needs the ``plots`` extra;
:func:`plot_with_generic_coordinates` additionally needs ``python-igraph``.
"""

import math
import random
from typing import Optional, Tuple

import contextily as cx
import networkx as nx
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.figure import Figure
from pandapower.plotting import create_generic_coordinates
from pandapower.plotting.plotly import simple_plotly
from pandapower.topology import create_nxgraph

from pylovo.config_loader import NODE_COLOR_CONNECTION_BUS, NODE_COLOR_CONSUMER, NODE_COLOR_TRAFO, TARGET_EPSG
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.utils import OSM_TILE_HEADERS, pandapower_on_map


def get_network_info_for_plotting(df_network_info: pd.Series) -> Tuple[int, int, int]:
    """Return ``(plz, kcid, bcid)`` of a grid from a row with these columns.

    Args:
        df_network_info: Row (e.g. of a representative-grid table) with ``plz``, ``kcid``, ``bcid``.

    Returns:
        Tuple ``(plz, kcid, bcid)``; ``kcid`` and ``bcid`` as int, ``plz`` unchanged.
    """
    plz = df_network_info['plz']
    kcid = int(df_network_info['kcid'])
    bcid = int(df_network_info['bcid'])
    return plz, kcid, bcid


def read_net_with_grid_generator(plz: int, kcid: int, bcid: int):
    """Read a pandapower network from the database.

    The name is historic: the grid is read with a plain :class:`DatabaseClient`, so no version row
    is written and a changed configuration does not block plotting.

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.

    Returns:
        The pandapower network.
    """
    with DatabaseClient() as dbc_client:
        return dbc_client.read_net_db(plz=plz, kcid=kcid, bcid=bcid)


def get_colormap_for_treegraph(networkx_graph: nx.Graph) -> list:
    """Return one colour per node for the tree plots.

    Buses 0 and 1 (LV and MV bus of the transformer) get ``NODE_COLOR_TRAFO``, leaves (degree 1,
    consumers) ``NODE_COLOR_CONSUMER`` and all other nodes ``NODE_COLOR_CONNECTION_BUS``
    (``NETWORK_COLORS`` in ``config_analysis.yaml``).

    Args:
        networkx_graph: Graph of the grid, e.g. from :func:`pandapower.topology.create_nxgraph`.

    Returns:
        Colours in the order of ``networkx_graph.nodes()``.
    """
    color_map = []
    for node in networkx_graph.nodes():
        if node == 1 or node == 0:
            color_map.append(NODE_COLOR_TRAFO)
        elif networkx_graph.degree(node) == 1:
            color_map.append(NODE_COLOR_CONSUMER)
        else:
            color_map.append(NODE_COLOR_CONNECTION_BUS)
    return color_map


def plot_contextily(plz: int, kcid: int, bcid: int, zoomfactor: int = 19, ax: Optional[plt.Axes] = None,
        figsize: Tuple[int, int] = (8, 8)) -> Figure:
    """Plot the lines, buildings and transformer of one grid on an OpenStreetMap basemap.

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


def plot_with_generic_coordinates(plz: int, kcid: int, bcid: int) -> None:
    """Plot one grid with generic (topology-based) coordinates instead of its geodata (plotly).

    Needs the optional package ``python-igraph`` (pandapower raises an ImportError without it).

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
    """
    net = read_net_with_grid_generator(plz, kcid, bcid)

    # Clear the geodata so that the generic layout is used for buses and lines.
    if "geo" in net.bus.columns:
        net.bus["geo"] = None
    if "geo" in net.line.columns:
        net.line["geo"] = None

    # pandapower >= 3 keeps the bus coordinates in net.bus["geo"] (geodata_table="bus").
    generic_net = create_generic_coordinates(net, library='igraph', respect_switches=False, overwrite=True,
        geodata_table='bus')
    simple_plotly(generic_net, aspectratio=(1, 1))


def plot_simple_grid(plz: int, kcid: int, bcid: int) -> None:
    """Plot one grid with its geodata on a blank background (plotly).

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    simple_plotly(net)


def plot_grid_on_map(plz: int, kcid: int, bcid: int):
    """Plot one grid on an OpenStreetMap background (plotly map).

    pandapower checks ``on_map`` plots by reverse-geocoding the first bus with Nominatim, but passes
    the coordinates as "lon, lat" where Nominatim expects "lat, lon". For grids in Germany the
    lookup then lands in the Gulf of Aden, returns nothing and pandapower fails with
    ``AttributeError``. pylovo geodata is always WGS84 lon/lat, so the check is skipped.

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.

    Returns:
        The plotly figure.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    with pandapower_on_map():
        fig = simple_plotly(net, on_map=True, map_style="open-street-map")
    return fig


def hierarchy_pos(G, root=None, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5):
    """Return hierarchical (top-down) layout positions of a tree.

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


def hierarchy_pos2(G, root, levels=None, width=1., height=1.):
    """Return hierarchical layout positions with the nodes of each level evenly spaced.

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
        if not currentLevel in levels:
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
        levels = {l: {TOTAL: levels[l], CURRENT: 0} for l in levels}
    vert_gap = height / (max([l for l in levels]) + 1)
    return make_pos({})


def _draw_colored_graph(G: nx.Graph, pos: dict) -> None:
    """Draw ``G`` with node labels and the tree colours in a new 20x10 inch figure."""
    plt.figure(figsize=(20, 10))
    nx.draw_networkx(G, node_color=get_colormap_for_treegraph(networkx_graph=G), pos=pos, with_labels=True)


def draw_tree_network(G, width=1.):
    """Draw the grid graph as a tree with bus 1 (MV bus) as root.

    Colours: see :func:`get_colormap_for_treegraph` (transformer ivory, connection nodes green,
    consumers blue).

    Args:
        G: Tree graph of the grid, e.g. ``pandapower.topology.create_nxgraph(net)``.
        width: Horizontal space of the layout.
    """
    _draw_colored_graph(G, hierarchy_pos(G, root=1, width=width))


def draw_tree_network_with_spacing_from_grid_id(plz: int, kcid: int, bcid: int):
    """Read one grid and draw it with :func:`draw_tree_network_spacing`.

    Args:
        plz: Postal code.
        kcid: K-means cluster id.
        bcid: Building cluster id.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    G = create_nxgraph(net)
    draw_tree_network_spacing(G)


def draw_tree_network_spacing(G):
    """Draw the grid graph as a tree with evenly spaced nodes per level (better for large grids).

    Colours: see :func:`get_colormap_for_treegraph`.

    Args:
        G: Tree graph of the grid with bus 1 (MV bus) as root.
    """
    _draw_colored_graph(G, hierarchy_pos2(G, root=1))
    plt.show()


def draw_radial_network(G):
    """Draw the grid graph in a radial tree layout around bus 1 (MV bus).

    Colours: see :func:`get_colormap_for_treegraph`.

    Args:
        G: Tree graph of the grid.
    """
    pos = hierarchy_pos(G, 1, width=2 * math.pi, xcenter=0)
    new_pos = {u: (r * math.cos(theta), r * math.sin(theta)) for u, (theta, r) in pos.items()}
    plt.figure(figsize=(20, 10))
    nx.draw(G, pos=new_pos, node_color=get_colormap_for_treegraph(networkx_graph=G), node_size=200)
    plt.show()
