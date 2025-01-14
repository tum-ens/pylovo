import math
import random

import contextily as cx
import geopandas as gpd
import networkx as nx
import pandas as pd
from matplotlib import pyplot as plt
from pandapower.plotting import create_generic_coordinates
from pandapower.plotting.plotly import simple_plotly
from pandapower.topology import create_nxgraph
from shapely import linestrings

from plotting.config_plots import *
from pylovo.GridGenerator import GridGenerator


def get_network_info_for_plotting(df_network_info: pd.DataFrame) -> (str, int, int):
    """extracts network metadata plz, kcid, bcid of a pandas series if columns exist"""
    plz = df_network_info['plz']
    kcid = int(df_network_info['kcid'])
    bcid = int(df_network_info['bcid'])
    return plz, kcid, bcid


def read_net_with_grid_generator(plz: str, kcid: int, bcid: int):
    gg = GridGenerator(plz=plz)
    pg = gg.pgr
    net = pg.read_net(plz=str(int(plz)), kcid=kcid, bcid=bcid)
    return net


def get_colormap_for_treegraph(networkx_graph):
    """make colormap for tree graphs, assign colors to different bus types"""
    color_map = []
    for node in networkx_graph.nodes():
        if node == 1 or node == 0:
            color_map.append(NODE_COLOR_TRAFO)
        elif networkx_graph.degree(node) == 1:
            color_map.append(NODE_COLOR_CONSUMER)
        else:
            color_map.append(NODE_COLOR_CONNECTION_BUS)
    return color_map


def plot_contextily(plz: str, kcid: int, bcid: int, zoomfactor: int = 19) -> None:
    """plots a network with all its features (cables, houses and load, trafo) on a contextily basemap

    :param plz: PLZ of grid
    :type plz: string
    :param kcid: kmeans cluster id of grid
    :type kcid: int
    :param bcid: buildings cluster id of grid
    :type bcid: int
    :param zoomfactor: zoom factor for basemap, defaults to 17
    :type zoomfactor: int
    """
    gg = GridGenerator(plz=plz)
    net = gg.pgr.read_net(plz=str(int(plz)), kcid=kcid, bcid=bcid)
    pg = gg.pgr
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_xticks([])
    ax.set_yticks([])
    # Buildings
    buildings_gdf = pg.get_geo_df(table="buildings_result", plz=int(plz))
    buildings_8_gdf = buildings_gdf[buildings_gdf.bcid == bcid]
    buildings_8_gdf = buildings_8_gdf[buildings_8_gdf.kcid == kcid]
    # cables / lines
    net.line_gdf = gpd.GeoDataFrame(net.line.copy(), geometry=net.line_geodata.coords.map(linestrings),
                                    crs="EPSG:4326").to_crs(buildings_8_gdf.crs.to_string())
    ax = net.line_gdf.plot(ax=ax, edgecolor="black", linewidth=1, label="Lines")
    ax = buildings_8_gdf.plot(ax=ax, column="peak_load_in_kw", cmap="YlOrBr",
                              legend=True, legend_kwds={'label': "Peak load in kW"})
    # trafo
    trafo_gdf = pg.get_geo_df(table="transformer_positions", plz=int(plz), bcid=bcid)
    ax.scatter(trafo_gdf.loc[0].geom.x, trafo_gdf.loc[0].geom.y, marker=(5, 0), s=80, color="blue", label="Transformer")
    # basemap
    cx.add_basemap(ax, crs=buildings_8_gdf.crs.to_string(), zoom=zoomfactor, source=cx.providers.OpenStreetMap.Mapnik)
    ax.legend()

    # plt.savefig('contextily.png', dpi=500)
    fig


def plot_with_generic_coordinates(plz: str, kcid: int, bcid: int) -> None:
    net = read_net_with_grid_generator(plz, kcid, bcid)
    net.bus_geodata.drop(net.bus_geodata.index, inplace=True)
    net.line_geodata.drop(net.line_geodata.index, inplace=True)
    generic_net = create_generic_coordinates(net, library='igraph', respect_switches=False,
                                             overwrite=True, geodata_table='bus_geodata')
    simple_plotly(generic_net, aspectratio=(1, 1))


def plot_simple_grid(plz: str, kcid: int, bcid: int) -> None:
    """
    plots network on a plank base
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    simple_plotly(net)


def plot_grid_on_map(plz: str, kcid: int, bcid: int) -> None:
    """
    plots network on a basemap provided by plotly
    """
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    fig = simple_plotly(net, on_map=True, map_style="open-street-map")
    # fig = fig.update_layout(mapbox={'zoom': 15, }) # TODO: fix double plotting in notebooks through zooming
    # fig.show()


def hierarchy_pos(G, root=None, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5):
    '''
    From Joel's answer at https://stackoverflow.com/a/29597209/2966723.
    Licensed under Creative Commons Attribution-Share Alike

    If the graph is a tree this will return the positions to plot this in a
    hierarchical layout.

    G: the graph (must be a tree)

    root: the root node of current branch
    - if the tree is directed and this is not given,
      the root will be found and used
    - if the tree is directed and this is given, then
      the positions will be just for the descendants of this node.
    - if the tree is undirected and not given,
      then a random choice will be used.

    width: horizontal space allocated for this branch - avoids overlap with other branches

    vert_gap: gap between levels of hierarchy

    vert_loc: vertical location of root

    xcenter: horizontal location of root
    '''
    if not nx.is_tree(G):
        raise TypeError('cannot use hierarchy_pos on a graph that is not a tree')

    if root is None:
        if isinstance(G, nx.DiGraph):
            root = next(iter(nx.topological_sort(G)))  # allows back compatibility with nx version 1.11
        else:
            root = random.choice(list(G.nodes))

    def _hierarchy_pos(G, root, width=1., vert_gap=0.2, vert_loc=0, xcenter=0.5, pos=None, parent=None):
        '''
        see hierarchy_pos docstring for most arguments

        pos: a dict saying where all nodes go if they have been assigned
        parent: parent of this branch. - only affects it if non-directed

        '''

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
                pos = _hierarchy_pos(G, child, width=dx, vert_gap=vert_gap,
                                     vert_loc=vert_loc - vert_gap, xcenter=nextx,
                                     pos=pos, parent=root)
        return pos

    return _hierarchy_pos(G, root, width, vert_gap, vert_loc, xcenter)


def hierarchy_pos2(G, root, levels=None, width=1., height=1.):
    """If there is a cycle that is reachable from root, then this will see infinite recursion.
       G: the graph
       root: the root node
       levels: a dictionary
               key: level number (starting from 0)
               value: number of nodes in this level
       width: horizontal space allocated for drawing
       height: vertical space allocated for drawing"""
    TOTAL = "total"
    CURRENT = "current"

    def make_levels(levels, node=root, currentLevel=0, parent=None):
        """Compute the number of nodes for each level
        """
        if not currentLevel in levels:
            levels[currentLevel] = {TOTAL: 0, CURRENT: 0}
        levels[currentLevel][TOTAL] += 1
        neighbors = G.neighbors(node)
        for neighbor in neighbors:
            if not neighbor == parent:
                levels = make_levels(levels, neighbor, currentLevel + 1, node)
        return levels

    def make_pos(pos, node=root, currentLevel=0, parent=None, vert_loc=0):
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
    """draws a tree graph of a networkx graph with specific node colors:
    orange: transformers,
    blue: connection nodes
    green: consumers"""
    pos = hierarchy_pos(G, root=1, width=width)
    labels = nx.get_edge_attributes(G, 'weight')
    plt.figure(figsize=(9, 6))
    color_map = get_colormap_for_treegraph(networkx_graph=G)
    plt.figure(figsize=(20, 10))
    ax = nx.draw_networkx(G, node_color=color_map, pos=pos, with_labels=True)
    # plt.savefig('tree.png', dpi=600)
    plt.show()


def draw_tree_network_with_improved_spacing_from_grid_id(plz: str, kcid: int, bcid: int):
    """draws a tree graph of a networkx graph with improved spacing for large networks#
    from grid id"""
    net = read_net_with_grid_generator(plz=plz, kcid=kcid, bcid=bcid)
    G = create_nxgraph(net)
    draw_tree_network_improved_spacing(G)


def draw_tree_network_improved_spacing(G):
    """draws a tree graph of a networkx graph with improved spacing for large networks
    with specific node colors:
    orange: transformers,
    blue: connection nodes
    green: consumers"""
    pos = hierarchy_pos2(G, root=1)
    labels = nx.get_edge_attributes(G, 'weight')
    plt.figure(figsize=(9, 6))

    color_map = get_colormap_for_treegraph(networkx_graph=G)
    plt.figure(figsize=(20, 10))
    ax = nx.draw_networkx(G, node_color=color_map, pos=pos, with_labels=True)
    plt.show()


def draw_radial_network(G):
    """draws a radial graph of a networkx graph with specific node colors:
    orange: transformers,
    blue: connection nodes
    green: consumers"""
    pos = hierarchy_pos(G, 1, width=2 * math.pi, xcenter=0)
    plt.figure(figsize=(20, 10))
    new_pos = {u: (r * math.cos(theta), r * math.sin(theta)) for u, (theta, r) in pos.items()}
    color_map = get_colormap_for_treegraph(networkx_graph=G)
    ax = nx.draw(G, pos=new_pos, node_size=50)
    ax = nx.draw_networkx_nodes(G, pos=new_pos, node_color=color_map, node_size=200)
    # ax = nx.draw_networkx_nodes(G, pos=new_pos, nodelist = [0], node_color=color_map, node_size=200)
    plt.show()
