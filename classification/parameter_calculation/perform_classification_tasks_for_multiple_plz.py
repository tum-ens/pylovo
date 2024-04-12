import pandas as pd

from classification.parameter_calculation.ParameterCalculator import ParameterCalculator


def calculate_parameters_for_multiple_plz(df_samples: pd.DataFrame) -> None:
    """calculates grid parameters for all plz contained in the column 'plz' of df_samples

    :param df_samples: table that contains PLZ for parameter calculation
    :type df_samples: pd.DataFrame
    """

    for plz_index in df_samples['plz']:
        # compute parameters for plz
        pc = ParameterCalculator(str(plz_index))
        pc.calc_parameters_for_grids()
