# functions to import data tables
import pandas as pd

from ..gemeindeverzeichnis.file_names import *

def import_plz_einwohner() -> pd.DataFrame:
    """imports table with PLZ, population, area, latitude and longitude,
    from source: https://www.suche-postleitzahl.org/downloads

    :return: table with population data
    :rtype: pd.DataFrame
    """
    plz_einwohner = pd.read_excel(PATH_PLZ_EINWOHNER)
    return plz_einwohner


def import_zuordnung_plz() -> pd.DataFrame:
    """imports excel table with matching PLZ and AGS
    from source: https://www.suche-postleitzahl.org/downloads

    :return: table with PLZ and AGS data
    :rtype: pd.DataFrame
    """
    plz_zuordnung = pd.read_excel(PATH_ZUORDNUNG_PLZ)
    plz_zuordnung = plz_zuordnung.drop(columns=["osm_id"])
    return plz_zuordnung
