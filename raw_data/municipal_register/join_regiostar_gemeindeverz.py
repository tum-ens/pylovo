import pandas as pd
import psycopg2
from sqlalchemy import create_engine

from raw_data.municipal_register.regiostar.import_regiostar import import_regiostar
from raw_data.municipal_register.gemeindeverzeichnis.import_functions import import_plz_einwohner, import_zuordnung_plz
from syngrid.config_data import *


def import_tables() -> (pd.DataFrame, pd.DataFrame, pd.DataFrame):
    # retrieve data from gemeindeverzeichnis and regiostar
    plz_zuordnung = import_zuordnung_plz()
    plz_einwohner = import_plz_einwohner()
    regiostar, regiostar_bayern = import_regiostar()
    return plz_einwohner, plz_zuordnung, regiostar


def join_regiostar_plz(plz_pop: pd.DataFrame, plz_ags: pd.DataFrame, regiostar: pd.DataFrame) -> pd.DataFrame:
    """
    writes table that contains regiostar classes for plz,
    pop and area columns contain specific data for the PLZ
    """
    # delete regiostar pop and area that is specific to the ags

    plz_pop_ags = plz_pop.merge(plz_ags, left_on="plz", right_on="plz")
    plz_pop_ags_regio = plz_pop_ags.merge(regiostar, left_on="ags", right_on="mun_code")
    plz_pop_ags_regio = plz_pop_ags_regio.drop(
        columns=["note", "ort", "landkreis", "bundesland", "mun_code", "pop", "area", "pop_den"])
    plz_pop_ags_regio = plz_pop_ags_regio.rename(columns={"einwohner": "pop", "qkm": "area"})
    plz_pop_ags_regio["pop_den"] = plz_pop_ags_regio["pop"] / plz_pop_ags_regio["area"]
    return plz_pop_ags_regio


def municipal_register_to_db(regiostar_plz: pd.DataFrame) -> None:
    """writes register to database"""
    conn = psycopg2.connect(database=DBNAME, user=USER, password=PASSWORD, host=HOST, port=PORT)
    sqlalchemy_engine = create_engine(
        f"postgresql+psycopg2://{USER}:{PASSWORD}@{HOST}:{PORT}/{DBNAME}")
    cur = conn.cursor()
    try:
        regiostar_plz.to_sql(
            'municipal_register', 
            con=sqlalchemy_engine, 
            if_exists='replace', 
            index=False
        )
    except Exception as e:
        print(e)
    conn.commit()


def create_municipal_register() -> None:
    """join gemeindeverzeichnis with regiostar, so that each PLZ can be associated with a AGS and regiostar class.
    The data is written do the database table 'municipal_register'.
    """
    plz_einwohner, plz_zuordnung, regiostar = import_tables()
    regiostar_plz = join_regiostar_plz(plz_einwohner, plz_zuordnung, regiostar)
    municipal_register_to_db(regiostar_plz)
