# select single or multiple PLZ
# the geodata of the grids of the PLZ selected below is exported as two csv-files to be used for visualisation in QGIS
# one file contains the line and the other the bus data
import os
import sys

import pandas as pd

from plotting.export_net import save_geodata_as_csv

# enter the PLZ for which the geodata is exported
plz_list = ['91720', '80639']
df_plz = pd.DataFrame(plz_list, columns=['plz'])

# define the datapaths you want to export the grids to
line_datapath = os.path.join(os.path.dirname(__file__), '../QGIS', 'lines_multiple_grids.csv')
sys.path.append(line_datapath)
bus_datapath = os.path.join(os.path.dirname(__file__), '../QGIS', 'bus_multiple_grids.csv')
sys.path.append(bus_datapath)

save_geodata_as_csv(df_plz=df_plz, data_path_lines=line_datapath, data_path_bus=bus_datapath)
