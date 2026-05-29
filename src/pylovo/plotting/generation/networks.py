"""
Network plotting functions.

This module contains functions for visualizing power distribution networks,
including geographic visualizations with contextily basemaps and generic
network layouts.
"""

import math
import random
from typing import Tuple, Optional

import contextily as cx
import geopandas as gpd
import networkx as nx
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.figure import Figure
from pandapower.plotting import create_generic_coordinates
from pandapower.plotting.plotly import simple_plotly
from pandapower.topology import create_nxgraph

from pylovo.config_loader import (NODE_COLOR_TRAFO, NODE_COLOR_CONSUMER, NODE_COLOR_CONNECTION_BUS)
from pylovo.grid_generator import GridGenerator


def get_network_info_for_plotting(df_network_info: pd.DataFrame) -> Tuple[str, int, int]:
    """
    Extract network metadata (plz, kcid, bcid) from a pandas DataFrame.

    Parameters
    ----------
    df_network_info : pd.DataFrame
        DataFrame containing network information with columns 'plz', 'kcid', 'bcid'.

    Returns
    -------
    tuple
        (plz, kcid, bcid) - Postal code, kmeans cluster ID, and building cluster ID.
    """
    plz = df_network_info['plz']
    kcid = int(df_network_info['kcid'])
    bcid = int(df_network_info['bcid'])
    return plz, kcid, bcid


def read_net_with_grid_generator(plz: int, kcid: int, bcid: int):
    """
    Read a pandapower network from the database using GridGenerator.

    Parameters
    ----------
    plz : int
        Postal code.
    kcid : int
        Kmeans cluster ID.
    bcid : int
        Buildings cluster ID.

    Returns
    -------
    pandapowerNet
        The loaded pandapower network.
    """
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc
    net = dbc_client.read_net_db(plz=plz, kcid=kcid, bcid=bcid)
    return net


def get_colormap_for_treegraph(networkx_graph: nx.Graph) -> list:
    """
    Create a colormap for tree graph visualization.

    Assigns colors to different bus types:
    - Transformer buses (node 0, 1): Ivory color
    - Consumer buses (degree 1): Blue color
    - Connection buses: Green color

    Parameters
    ----------
    networkx_graph : networkx.Graph
        NetworkX graph representation of the power network.

    Returns
    -------
    list
        List of colors corresponding to each node in the graph.
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
    """
    Plot a network with all features (cables, buildings, loads, trafo) on a contextily basemap.

    Parameters
    ----------
    plz : int
        Postal code of the grid.
    kcid : int
        Kmeans cluster ID of the grid.
    bcid : int
        Buildings cluster ID of the grid.
    zoomfactor : int, optional
        Zoom factor for the basemap (default: 19).
    ax : matplotlib.axes.Axes, optional
        Axes object for subplot integration. If None, creates a new figure.
    figsize : tuple of int, optional
        Figure size in inches (width, height). Default: (8, 8).

    Returns
    -------
    matplotlib.figure.Figure
        The Figure object containing the plot.
    """
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc

    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()

    ax.set_xticks([])
    ax.set_yticks([])

    # Buildings
    buildings_gdf = dbc_client.get_geo_df_join(["gr.version_id", "plz", "kcid", "bcid", "br.*"], "buildings_result br",
        "grid_result gr", ("br.grid_result_id", "gr.grid_result_id"), plz=int(plz))
    buildings_8_gdf = buildings_gdf[buildings_gdf.bcid == bcid]
    buildings_8_gdf = buildings_8_gdf[buildings_8_gdf.kcid == kcid]

    line_gdf = dbc_client.get_geo_df_join(
        [
            "pl.*",
            "gr.kcid",
            "gr.bcid",
            "gr.plz",
            "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(pl.geo::text), 4326), 3035) AS geom",
        ],
        "pandapower_line pl",
        "grid_result gr",
        ("pl.grid_result_id", "gr.grid_result_id"),
        plz=int(plz),
    )
    line_gdf = line_gdf[(line_gdf.bcid == bcid) & (line_gdf.kcid == kcid)]

    ax = line_gdf.plot(ax=ax, edgecolor="black", linewidth=1, label="Lines")
    ax = buildings_8_gdf.plot(ax=ax, column="peak_load_in_kw", cmap="YlOrBr", legend=True,
        legend_kwds={'label': "Peak load in kW"})

    # Transformer
    trafo_gdf = dbc_client.get_geo_df_join(["geom"], "transformer_positions tp", "grid_result gr",
        ("tp.grid_result_id", "gr.grid_result_id"), plz=int(plz), bcid=bcid)
    ax.scatter(trafo_gdf.loc[0].geom.x, trafo_gdf.loc[0].geom.y, marker=(5, 0), s=80, color="blue", label="Transformer")

    # Basemap
    cx.add_basemap(ax, crs=buildings_8_gdf.crs.to_string(), zoom=zoomfactor, source=cx.providers.OpenStreetMap.Mapnik)
    ax.legend()

    return fig


def plot_with_generic_coordinates(plz: int, kcid: int, bcid: int) -> None:
    """
    Plot network using generic coordinates layout.

    Creates a network visualization using igraph-based automatic layout.

    Parameters
    ----------
    plz : int
        Postal code.
    kcid : int
        Kmeans cluster ID.
    bcid : int
        Buildings cluster ID.
    """
    net = read_net_with_grid_generator(plz, kcid, bcid)

    # Clear geodata
    if "geo" in net.bus.columns:
        net.bus["geo"] = None
    if "geo" in net.line.columns:
        net.line["geo"] = None

    generic_net = create_generic_coordinates(net, library='igraph', respect_switches=False, overwrite=True,
        geodata_table='bus_geodata')
    simple_plotly(generic_net, aspectratio=(1, 1))


def plot_simple_grid(plz: int, kcid: int, bcid: int) -> None:
    """
    Plot network on a blank base.

    Parameters
    ----------
    plz : int
        Postal code.
    kcid : int
        Kmeans cluster ID.
    bcid : int
        Buildings cluster ID.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    simple_plotly(net)


def plot_grid_on_map(plz: int, kcid: int, bcid: int) -> None:
    """
    Plot network on a basemap provided by plotly.

    Parameters
    ----------
    plz : int
        Postal code.
    kcid : int
        Kmeans cluster ID.
    bcid : int
        Buildings cluster ID.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    fig = simple_plotly(net, on_map=True, map_style="open-street-map")
    return fig


def hierarchy_pos(G, root=None, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5):
    """
    Calculate hierarchical layout positions for a tree graph.

    From Joel's answer at https://stackoverflow.com/a/29597209/2966723.
    Licensed under Creative Commons Attribution-Share Alike

    If the graph is a tree this will return the positions to plot this in a
    hierarchical layout.

    Parameters
    ----------
    G : networkx.Graph
        The graph (must be a tree).
    root : node, optional
        The root node of current branch.
        - if the tree is directed and this is not given, the root will be found and used
        - if the tree is directed and this is given, then the positions will be just
          for the descendants of this node.
        - if the tree is undirected and not given, then a random choice will be used.
    width : float, optional
        Horizontal space allocated for this branch - avoids overlap with other branches.
    vert_gap : float, optional
        Gap between levels of hierarchy.
    vert_loc : float, optional
        Vertical location of root.
    xcenter : float, optional
        Horizontal location of root.

    Returns
    -------
    dict
        Dictionary mapping nodes to (x, y) positions.
    """
    if not nx.is_tree(G):
        raise TypeError('cannot use hierarchy_pos on a graph that is not a tree')

    if root is None:
        if isinstance(G, nx.DiGraph):
            root = next(iter(nx.topological_sort(G)))
        else:
            root = random.choice(list(G.nodes))

    def _hierarchy_pos(G, root, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5, pos=None, parent=None):
        """
        Recursive helper for hierarchy_pos.

        Parameters
        ----------
        pos : dict, optional
            A dict saying where all nodes go if they have been assigned.
        parent : node, optional
            Parent of this branch - only affects it if non-directed.
        """
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
    """
    Calculate hierarchical layout with spacing for large networks.

    If there is a cycle that is reachable from root, then this will see infinite recursion.

    Parameters
    ----------
    G : networkx.Graph
        The graph.
    root : node
        The root node.
    levels : dict, optional
        A dictionary with:
        - key: level number (starting from 0)
        - value: number of nodes in this level
    width : float, optional
        Horizontal space allocated for drawing.
    height : float, optional
        Vertical space allocated for drawing.

    Returns
    -------
    dict
        Dictionary mapping nodes to (x, y) positions.
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


def draw_tree_network(G, width=1.):
    """
    Draw a tree graph of a networkx graph with specific node colors.

    Node colors:
    - Orange: transformers
    - Blue: connection nodes
    - Green: consumers

    Parameters
    ----------
    G : networkx.Graph
        The network graph.
    width : float, optional
        Width parameter for hierarchy layout.
    """
    pos = hierarchy_pos(G, root=1, width=width)
    labels = nx.get_edge_attributes(G, 'weight')
    plt.figure(figsize=(9, 6))
    color_map = get_colormap_for_treegraph(networkx_graph=G)
    plt.figure(figsize=(20, 10))
    ax = nx.draw_networkx(G, node_color=color_map, pos=pos, with_labels=True)
    return ax


def draw_tree_network_with_spacing_from_grid_id(plz: int, kcid: int, bcid: int):
    """
    Draw a tree graph with improved spacing for large networks from grid ID.

    Parameters
    ----------
    plz : int
        Postal code.
    kcid : int
        Kmeans cluster ID.
    bcid : int
        Buildings cluster ID.
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    G = create_nxgraph(net)
    draw_tree_network_spacing(G)


def draw_tree_network_spacing(G):
    """
    Draw a tree graph with improved spacing for large networks.

    Node colors:
    - Orange: transformers
    - Blue: connection nodes
    - Green: consumers

    Parameters
    ----------
    G : networkx.Graph
        The network graph.
    """
    pos = hierarchy_pos2(G, root=1)
    labels = nx.get_edge_attributes(G, 'weight')
    plt.figure(figsize=(9, 6))
    color_map = get_colormap_for_treegraph(networkx_graph=G)
    plt.figure(figsize=(20, 10))
    ax = nx.draw_networkx(G, node_color=color_map, pos=pos, with_labels=True)
    plt.show()


def draw_radial_network(G):
    """
    Draw a radial graph of a networkx graph with specific node colors.

    Node colors:
    - Orange: transformers
    - Blue: connection nodes
    - Green: consumers

    Parameters
    ----------
    G : networkx.Graph
        The network graph.
    """
    pos = hierarchy_pos(G, 1, width=2 * math.pi, xcenter=0)
    plt.figure(figsize=(20, 10))
    new_pos = {u: (r * math.cos(theta), r * math.sin(theta)) for u, (theta, r) in pos.items()}
    color_map = get_colormap_for_treegraph(networkx_graph=G)
    # ax = nx.draw(G, pos=new_pos, node_size=50)
    ax = nx.draw_networkx_nodes(G, pos=new_pos, node_color=color_map, node_size=200)
    plt.show()
