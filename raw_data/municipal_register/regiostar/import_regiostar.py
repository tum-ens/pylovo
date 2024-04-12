# Import Regiostar Dataset
import os
import sys

import pandas as pd
from openpyxl import load_workbook

# File name
DATA_PATH = os.path.join(os.path.dirname(__file__), 'regiostar.xlsx')
sys.path.append(DATA_PATH)


def import_regiostar()->(pd.DataFrame, pd.DataFrame):
    """ imports RegioStaR dataset from excel datasheet
    Regiostar: Regionalstatistische Raumtypologie des Bundesministeriums für Digitales und Verkehr (BMVI)
    classification of German Municipalities
    source: https://bmdv.bund.de/SharedDocs/DE/Artikel/G/regionalstatistische-raumtypologie.html

    :return: table with AGS, name, Regiostar 5 and 7 classes
    :rtype: pd.DataFrame
    """
    name_worksheet = "ReferenzGebietsstand2020"

    # load excel work book
    wb = load_workbook(DATA_PATH)
    # load excel sheet
    ws = wb[name_worksheet]

    # write excel regiostar table into dataframe
    data = ws.values
    columns = next(data)[0:]
    regiostar = pd.DataFrame(data, columns=columns)

    # columns to be dropped
    drop_columns = ["gemrs_20", "vbgem_20", "vbgemrs_20", "vbgnam_20", "RegioStaR2", "RegioStaR4", "RegioStaR17",
                    "RegioStaRGem7", "RegioStaRGem5", "RegioStaR_Stadtregion", "RegioStaR_NameStadtregion"]
    regiostar5_7 = regiostar.drop(drop_columns, axis=1)

    # The columns are municipal code (Gemeindeschlüssel), Name (Gemeindename), population, area, federal state (Bundesland)
    # and regio 5 and 7 code
    regiostar5_7.columns = ["mun_code", "name_city", "pop", "area", "fed_state", "regio7", "regio5"]

    # Calculating the population density
    regiostar5_7["pop_den"] = regiostar5_7["pop"] / regiostar5_7["area"]

    # selecting municipalities in Bayern
    regiostar5_7_bayern = regiostar5_7.loc[regiostar5_7['fed_state'] == 9]
    regiostar5_7_bayern = regiostar5_7_bayern.drop(["fed_state"], axis=1)
    return regiostar5_7, regiostar5_7_bayern
