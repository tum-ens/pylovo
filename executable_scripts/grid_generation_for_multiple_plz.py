# generate the grid for the multiple PLZ set below
# building data import is included

import pandas as pd
import time

from classification.sampling.sample import get_municipal_register_as_dataframe
from raw_data.preprocessing_scripts.import_building_data import import_buildings_for_multiple_plz
from pylovo.GridGenerator import GridGenerator

# start timing the script
start_time = time.time()

# enter the PLZ for which the geodata is exported
plz_list = [80802, 80687]

# get ags info for plz areas
municipal_register = get_municipal_register_as_dataframe()
df_plz_ags = municipal_register[municipal_register['plz'].isin(plz_list)]
# import buildings and generate grids
import_buildings_for_multiple_plz(sample_plz=df_plz_ags)

# initialize GridGenerator
gg = GridGenerator()
df_plz = pd.DataFrame(list(map(str,plz_list)), columns=['plz'])
gg.generate_grid_for_multiple_plz(df_plz=df_plz, analyze_grids=False)

# end timing and print results
elapsed_time = time.time() - start_time
minutes, seconds = divmod(elapsed_time, 60)
print(f"--- Elapsed Time: {int(minutes)} minutes and {seconds:.2f} seconds ---")
