# select one specific grid within a PLZ
# the geodata of the grid selected below is exported as two csv-files to be used for visualisation in QGIS
# one file contains the line and the other the bus data
import os
import sys

from plotting.export_net import get_bus_line_geo_for_network
from pylovo.GridGenerator import GridGenerator

# enter the grid data for the grid you want to export
plz = '91207'
kcid = 4
bcid = 30

# define the datapaths you want to export the grids to
line_datapath = os.path.join(os.path.dirname(__file__), '../QGIS', 'lines_single_grid_test.csv')
sys.path.append(line_datapath)
bus_datapath = os.path.join(os.path.dirname(__file__), '../QGIS', 'bus_single_grid_test.csv')
sys.path.append(bus_datapath)

# read grid from DB
gg = GridGenerator(plz)
pg = gg.pgr
net = pg.read_net(plz, kcid, bcid)

# get geodata
line_geo, bus_geo = get_bus_line_geo_for_network(pandapower_net=net, plz=plz)

# save geodata to csv
line_geo.to_csv(line_datapath)
bus_geo.to_csv(bus_datapath)
