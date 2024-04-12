import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

from syngrid.GridGenerator import GridGenerator


def get_bus_line_geo_for_network(pandapower_net, plz, net_index=0):
    """get bus and line data for a single pandapower net,
    export lines (cables) and buses (trafo position, consumers, connections) as geometric elements in csv table
    to be used in qgis"""
    # line data
    line_geo = pandapower_net.line_geodata
    line_list = []
    for line in line_geo['coords']:
        line_list.append(LineString(line))
    line_geo = gpd.GeoDataFrame(line_geo, geometry=line_list)
    line_geo['net'] = net_index
    line = pandapower_net.line
    line_geo = line_geo.merge(line, left_index=True, right_index=True)
    line_geo['plz'] = plz

    # bus data
    bus_geo = pandapower_net.bus_geodata
    bus_geo = gpd.GeoDataFrame(bus_geo, geometry=gpd.points_from_xy(bus_geo['x'], bus_geo['y']))
    bus_geo['net'] = net_index
    bus = pandapower_net.bus
    bus_geo = bus_geo.merge(bus, left_index=True, right_index=True)
    bus_geo['consumer_bus'] = bus_geo['name'].str.contains("Consumer Nodebus")
    bus_geo['plz'] = plz
    return line_geo, bus_geo


def get_bus_line_geo_for_plz(plz):
    """
    input: plz
    returns two dataframes: one with bus geometry (Nodebuses) and one with line geometry (cables)
    """
    # connect to database
    gg = GridGenerator(plz=plz)
    pg = gg.pgr

    # find all networks
    cluster_list = pg.get_list_from_plz(plz)

    # initialise geo dataframes
    gdf_line = gpd.GeoDataFrame()
    gdf_bus = gpd.GeoDataFrame()

    # index all networks
    net_index = 0

    # loop over all networks and extract line and bus data
    for kcid, bcid in cluster_list:
        net = pg.read_net(plz, kcid, bcid)
        line_geo, bus_geo = get_bus_line_geo_for_network(pandapower_net=net, net_index=net_index, plz=plz)

        gdf_line = pd.concat([gdf_line, line_geo])
        gdf_bus = pd.concat([gdf_bus, bus_geo])
        net_index += 1

    # return line and bus data
    return gdf_line, gdf_bus


def save_geodata_as_csv(df_plz: pd.DataFrame, data_path_lines: str, data_path_bus: str) -> None:
    """saves the geodata to csv"""
    gdf_line = gpd.GeoDataFrame()
    gdf_bus = gpd.GeoDataFrame()

    for plz in df_plz['plz']:
        print("Saving geodata of plz:", str(plz), "to csv.")
        # get_bus_line_geo(str(plz))
        gdf_line_tmp, gdf_bus_tmp = get_bus_line_geo_for_plz(str(plz))
        gdf_line = pd.concat([gdf_line, gdf_line_tmp])
        gdf_bus = pd.concat([gdf_bus, gdf_bus_tmp])

    gdf_line.to_csv(data_path_lines)
    gdf_bus.to_csv(data_path_bus)
