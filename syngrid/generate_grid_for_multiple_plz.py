import pandas as pd

from syngrid.GridGenerator import GridGenerator
from syngrid.GridGenerator import ResultExistsError


def generate_grid_for_multiple_plz(df_samples: pd.DataFrame) -> None:
    """generates grid for all plz contained in the column 'plz' of df_samples

    :param df_samples: table that contains PLZ for grid generation
    :type df_samples: pd.DataFrame
    """
    for index, row in df_samples.iterrows():
        plz = str(row['plz'])
        print('-------------------- start', plz, '---------------------------')
        try:
            gg = GridGenerator(plz=plz)
            gg.generate_grid()
            gg.analyse_results()
        except ResultExistsError:
            print('Grids for this PLZ have already been generated.')
        print('-------------------- end', plz, '-----------------------------')
