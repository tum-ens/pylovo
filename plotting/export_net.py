import os
import sys
import pandas as pd
from syngrid.GridGenerator import GridGenerator
import geopandas as gpd
from shapely.geometry import Point, LineString

# export lines (cables) and buses (trafo position, consumers, connections) as geometric elements in csv table
# to be used in qgis

# import sample set
# data_path = os.path.join(os.path.dirname(__file__), 'preselection_of_plz',
#                          'regiostar_samples_bayern_replaced_06062023_5.csv')
# sys.path.append(data_path)
# df_plz = pd.read_csv(data_path, index_col=0)
# df_plz = df_plz[df_plz['regio7'] == 71]


def get_bus_line_geo(plz):
    """
    input: plz
    returns two dataframes: one with bus geometry (Nodebuses) and one with line geometry (cables)
    """
    # connect to database
    gg = GridGenerator(plz=plz)
    pg = gg.pgr

    # find all networks
    cluster_list = pg.getListFromPlz(plz)

    # initialise geo dataframes
    gdf_line = gpd.GeoDataFrame()
    gdf_bus = gpd.GeoDataFrame()

    # index all networks
    net_index = 0

    # loop over all networks and extract line and bus data
    for kcid, bcid in cluster_list:
        net = pg.read_net(plz, kcid, bcid)
        # line data
        line_geo = net.line_geodata
        line_list = []
        for line in line_geo['coords']:
            line_list.append(LineString(line))
        line_geo = gpd.GeoDataFrame(line_geo, geometry=line_list)
        line_geo['net'] = net_index
        line = net.line
        line_geo = line_geo.merge(line, left_index=True, right_index=True)
        line_geo['plz'] = plz
        gdf_line = pd.concat([gdf_line, line_geo])
        # bus data
        bus_geo = net.bus_geodata
        bus_geo = gpd.GeoDataFrame(bus_geo, geometry=gpd.points_from_xy(bus_geo['x'], bus_geo['y']))
        bus_geo['net'] = net_index
        bus = net.bus
        bus_geo = bus_geo.merge(bus, left_index=True, right_index=True)
        bus_geo['consumer_bus'] = bus_geo['name'].str.contains("Consumer Nodebus")
        bus_geo['plz'] = plz
        gdf_bus = pd.concat([gdf_bus, bus_geo])
        net_index += 1

    # return line and bus data
    return gdf_line, gdf_bus


# gdf_line = gpd.GeoDataFrame()
# gdf_bus = gpd.GeoDataFrame()

# for plz in df_plz['plz']:
#     print(str(plz))
#     get_bus_line_geo(str(plz))
#     gdf_line_tmp, gdf_bus_tmp = get_bus_line_geo(str(plz))
#     gdf_line = pd.concat([gdf_line, gdf_line_tmp])
#     gdf_bus = pd.concat([gdf_bus, gdf_bus_tmp])

# gdf_line.to_csv('line_geodata.csv')
# gdf_bus.to_csv('bus_geodata.csv')

