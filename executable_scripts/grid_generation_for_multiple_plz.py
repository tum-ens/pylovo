# generate the grid for the multiple PLZ set below
# building data import is included

import pandas as pd

from classification.sampling.sample import get_municipal_register_as_dataframe
from raw_data.import_building_data import import_buildings_for_multiple_plz
from syngrid.generate_grid_for_multiple_plz import generate_grid_for_multiple_plz

# enter the PLZ for which the geodata is exported
plz_list = [86685, 86681]
df_plz = pd.DataFrame(list(map(str,plz_list)), columns=['plz'])

# get ags info for plz areas
municipal_register = get_municipal_register_as_dataframe()
df_plz_ags = municipal_register[municipal_register['plz'].isin(plz_list)]

# import buildings and generate grids
import_buildings_for_multiple_plz(sample_plz=df_plz_ags)
generate_grid_for_multiple_plz(df_samples=df_plz)
