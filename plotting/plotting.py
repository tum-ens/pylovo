import os
import random
import sys

import contextily as cx
import geopandas as gpd
import networkx as nx
import pandas as pd
import plotly.express as px
from matplotlib import pyplot as plt
from shapely import linestrings

from syngrid.GridGenerator import GridGenerator

sys.path.append(os.path.abspath('..'))
access_token = 'pk.eyJ1IjoibXVza2F0bnVzcyIsImEiOiJjbGdxYnVyY28wN3lhM2VvMnBtYWwxZGl6In0.y5gIcGJSpwb0X4wgQQVolA'
px.set_mapbox_access_token(access_token)


def plot_contextily(plz, bcid, kcid, zoomfactor=17):
    """plots a network with all its features (cables, houses and load, trafo) on a contextily basemap"""
    gg = GridGenerator(plz=plz)
    net = gg.pgr.read_net(plz, kcid, bcid)
    pg = gg.pgr
    fig, ax = plt.subplots(figsize=(8, 8))
    ax.set_xticks([])
    ax.set_yticks([])
    # Buildings
    buildings_gdf = pg.getGeoDataFrame(table="buildings_result", in_loadarea_cluster=int(plz))
    buildings_8_gdf = buildings_gdf[buildings_gdf.in_building_cluster == bcid]
    buildings_8_gdf = buildings_8_gdf[buildings_8_gdf.k_mean_cluster == kcid]
    # cables / lines
    net.line_gdf = gpd.GeoDataFrame(net.line.copy(), geometry=net.line_geodata.coords.map(linestrings),
                                    crs="EPSG:4326").to_crs(buildings_8_gdf.crs.to_string())
    ax = net.line_gdf.plot(ax=ax, edgecolor="black", linewidth=1, label="Lines")
    ax = buildings_8_gdf.plot(ax=ax, column="peak_load_in_kw", cmap="YlOrBr",
                              legend=True, legend_kwds={'label': "Peak load in kW"})
    # trafo
    trafo_gdf = pg.getGeoDataFrame(table="transformer_positions", loadarea_cluster=int(plz), building_cluster=bcid)
    ax.scatter(trafo_gdf.loc[0].geom.x, trafo_gdf.loc[0].geom.y, marker=(5, 0), s=80, color="blue", label="Transformer")
    # basemap
    cx.add_basemap(ax, crs=buildings_8_gdf.crs.to_string(), zoom=zoomfactor)
    ax.legend()
    fig


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


def plot_pie_of_trafo_cables(plz):
    """plots a pie chart of the trafos and cable types for a plz"""
    gg = GridGenerator(plz=plz)
    pg = gg.pgr
    data_list, data_labels, trafo_dict = pg.read_per_trafo_dict(plz=plz)
    fig, axs = plt.subplots(nrows=1, ncols=2, figsize=(16, 4))
    # Plot Transformer size distribution
    axs[0].pie(trafo_dict.values(), labels=trafo_dict.keys(), autopct='%1.1f%%',
               pctdistance=1.15, labeldistance=.6)
    axs[0].set_title('Transformer Size Distribution', fontsize=14)
    # Plot cable length distribution
    cable_dict = pg.read_cable_dict(plz)
    axs[1].pie(cable_dict.values(), labels=cable_dict.keys(), autopct="%.1f%%")
    axs[1].set_title("Installed Cable Length", fontsize=14)
    plt.show()


def plot_hist_trafos(plz):
    """
    plots histogram of trafo sizes in plz
    """
    gg = GridGenerator(plz=plz)
    pg = gg.pgr
    data_list, data_labels, trafo_dict = pg.read_per_trafo_dict(plz=plz)
    plt.bar(trafo_dict.keys(), height=trafo_dict.values(), width=0.3)
    plt.title('Transformer Size Distribution', fontsize=14)
    plt.xlabel("Trafo size")
    plt.ylabel("Count")
    plt.show()


def plot_boxplot_plz(plz):
    """
    boxplot of load number, bus number, simultaneaous load peak, max trafo distance, avg trafo distance
    """
    gg = GridGenerator(plz=plz)
    pg = gg.pgr
    data_list, data_labels, trafo_dict = pg.read_per_trafo_dict(plz=plz)
    trafo_sizes = list(data_list[0].keys())
    values = [list(d.values()) for d in data_list]

    # Create the figure and axes objects
    fig, axs = plt.subplots(nrows=1, ncols=len(data_list), figsize=(16, 4), sharey=True)
    for i, data_label in enumerate(data_labels):
        axs[i].boxplot(values[i], labels=trafo_sizes, vert=False, showfliers=False, patch_artist=True, notch=False)
        axs[i].set_title(data_label, fontsize=12)
    fig.supxlabel('Values', fontsize=12)
    fig.supylabel('Transformer Size (kVA)', fontsize=12)

    # Adjust the layout and save the plot
    plt.tight_layout()
    plt.show()


def plot_cable_length_of_types(plz):
    """
    plots distribution of cable length by length
    """
    gg = GridGenerator(plz=plz)
    pg = gg.pgr
    # distributed according to cross_section
    cluster_list = pg.getListFromPlz(plz)
    cable_length_dict = {}
    for kcid, bcid in cluster_list:
        try:
            net = pg.read_net(plz, kcid, bcid)
        except Exception as e:
            print(f" local network {kcid},{bcid} is problematic")
            raise e
        else:
            cable_df = net.line[net.line["in_service"] == True]

            cable_type = pd.unique(cable_df["std_type"]).tolist()
            for type in cable_type:

                if type in cable_length_dict:
                    cable_length_dict[type] += (
                            cable_df[cable_df["std_type"] == type]["parallel"]
                            * cable_df[cable_df["std_type"] == type]["length_km"]
                    ).sum()

                else:
                    cable_length_dict[type] = (
                            cable_df[cable_df["std_type"] == type]["parallel"]
                            * cable_df[cable_df["std_type"] == type]["length_km"]
                    ).sum()
    plt.bar(cable_length_dict.keys(), height=cable_length_dict.values(), width=0.3)
    plt.title('Cable Type Distribution', fontsize=14)
    plt.xlabel("Cable type")
    plt.ylabel("Length in m")
    plt.show()


def get_trafo_dicts(plz):
    """
    retrieve load count, bus count and cable lenth per type for a plz
    """
    gg = GridGenerator(plz=plz)
    pg = gg.pgr
    cluster_list = pg.getListFromPlz(plz)
    load_count_dict = {}
    bus_count_dict = {}
    cable_length_dict = {}
    trafo_dict = {}
    print("start basic parameter counting")
    for kcid, bcid in cluster_list:
        load_count = 0
        bus_list = []
        net = pg.read_net(plz, kcid, bcid)
        for row in net.load[["name", "bus"]].itertuples():
            load_count += 1
            bus_list.append(row.bus)
        bus_list = list(set(bus_list))
        bus_count = len(bus_list)
        cable_length = net.line['length_km'].sum()

        for row in net.trafo[["sn_mva", "lv_bus"]].itertuples():
            capacity = round(row.sn_mva * 1e3)

            if capacity in trafo_dict:
                trafo_dict[capacity] += 1

                load_count_dict[capacity].append(load_count)
                bus_count_dict[capacity].append(bus_count)
                cable_length_dict[capacity].append(cable_length)

            else:
                trafo_dict[capacity] = 1

                load_count_dict[capacity] = [load_count]
                bus_count_dict[capacity] = [bus_count]
                cable_length_dict[capacity] = [cable_length]
    return load_count_dict, bus_count_dict, cable_length_dict
