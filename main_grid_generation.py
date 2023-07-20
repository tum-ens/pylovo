import time

from plotting.plotting import plot_boxplot_plz, plot_pie_of_trafo_cables
from raw_data.import_building_data import *
from syngrid.GridGenerator import GridGenerator

# enter a plz to generate grid for:
plz = "80687"  # Laim
# plz = "61273"  # Wehrheim
# plz = "85221"  # dachau
# plz = "85232"  # bergkirchen
# plz = "85748" # garching
# plz = "85375"  # neufahrn
# plz = "91301"  # forchheim

# timing of the script
start_time = time.time()

# import building data onto the pylovo database
# and get information about the plz
path_to_regiostar_plz = os.path.join(os.path.dirname(__file__),
                                     'classification\\preselection_of_plz\\join_regiostar_gemeindeverzeichnis',
                                     'regiostar_plz.csv')

plz_ags = pd.read_csv(path_to_regiostar_plz)
import_buildings_for_single_plz(plz, plz_ags)

# generate grid
gg = GridGenerator(plz=plz)
gg.generate_grid()
gg.analyse_results()

# plot data from the generated grids
pg = gg.pgr
cluster_list = gg.pgr.getListFromPlz(plz)
print('The PLZ has', len(cluster_list), 'grids.')
plot_boxplot_plz(plz)
plot_pie_of_trafo_cables(plz)

# end timing
print("--- %s seconds ---" % (time.time() - start_time))

# go to QGIS directory and inspect the grids in QGIS. Make sure your database is connected.
