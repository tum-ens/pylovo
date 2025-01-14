# generate the grid for the PLZ set below
# building data import is included

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from plotting.plot_for_plz import plot_boxplot_plz, plot_pie_of_trafo_cables
from raw_data.preprocessing_scripts.import_building_data import *
from pylovo.GridGenerator import GridGenerator

# enter a plz to generate grid for:
plz = "80803"
analyze_grids = False
plot_results = False

# timing of the script
start_time = time.time()

# initialize GridGenerator with the provided postal code (PLZ)
gg = GridGenerator(plz=plz)

# import building data to the database and get information about the plz
import_buildings_for_single_plz(gg)

# generate a grid for the specified region
gg.generate_grid()

if analyze_grids:
    gg.analyse_results()
if plot_results:
    ### plot data from the generated grids
    pg = gg.pgr
    cluster_list = gg.pgr.get_list_from_plz(plz)
    print('The PLZ has', len(cluster_list), 'grids.')
    plot_boxplot_plz(plz)
    plot_pie_of_trafo_cables(plz)

# End timing and print results
elapsed_time = time.time() - start_time
minutes, seconds = divmod(elapsed_time, 60)
print(f"--- Elapsed Time: {int(minutes)} minutes and {seconds:.2f} seconds ---")