# generate the grid for the multiple PLZ set below
# building data import is included

import pandas as pd
import time

from src.classification.sampling.sample import get_municipal_register_as_dataframe
from src.data_import.import_buildings import import_buildings_for_multiple_plz
from src.grid_generator import GridGenerator
from src.config_loader import ANALYZE_GRIDS, USE_INFDB

# start timing the script
start_time = time.time()

# enter the PLZ for which the geodata is exported
plz_list = [80331,80333,80797,80799,80805,80807,81675,81929,81369,81241]

if not USE_INFDB:
    # get ags info for plz areas
    municipal_register = get_municipal_register_as_dataframe()
    df_plz_ags = municipal_register[municipal_register['plz'].isin(plz_list)]
    # import buildings and generate grids
    import_buildings_for_multiple_plz(sample_plz=df_plz_ags)

# initialize GridGenerator
gg = GridGenerator()
df_plz = pd.DataFrame(list(map(str,plz_list)), columns=['plz'])
gg.generate_grid_for_multiple_plz(df_plz=df_plz, analyze_grids=ANALYZE_GRIDS)

# end timing and print results
elapsed_time = time.time() - start_time
minutes, seconds = divmod(elapsed_time, 60)
print(f"--- Elapsed Time: {int(minutes)} minutes and {seconds:.2f} seconds ---")
