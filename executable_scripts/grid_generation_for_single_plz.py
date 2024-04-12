# generate the grid for the PLZ set below
# building data import is included

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from plotting.plot_for_plz import plot_boxplot_plz, plot_pie_of_trafo_cables
from raw_data.import_building_data import *
from syngrid.GridGenerator import GridGenerator

# enter a plz to generate grid for:
# plz = "80687"  # Laim
# plz = "91301"  # forchheim
# plz = "82402"  # seeshaupt
plz = '89278'

# timing of the script
start_time = time.time()

# generate grid
gg = GridGenerator(plz=plz)

# import building data to the database and get information about the plz
import_buildings_for_single_plz(gg)

gg.generate_grid()
gg.analyse_results()

# plot data from the generated grids
pg = gg.pgr
cluster_list = gg.pgr.get_list_from_plz(plz)
print('The PLZ has', len(cluster_list), 'grids.')
plot_boxplot_plz(plz)
plot_pie_of_trafo_cables(plz)

# end timing
print("--- %s seconds ---" % (time.time() - start_time))
