"""Build the ``municipal_register`` table (PLZ, AGS, population, RegioStaR classes).

The register joins three files in ``data/municipal_register``:

- ``gemeindeverzeichnis/plz_einwohner.xlsx``: population and area per PLZ,
- ``gemeindeverzeichnis/zuordnung_plz_ort.xlsx``: PLZ to AGS mapping
  (both from https://www.suche-postleitzahl.org/downloads),
- ``regiostar/regiostar.xlsx``: RegioStaR 5/7 classes per municipality (BMDV).
"""
import os
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

import pylovo.database.database_client as dbc


def _get_repo_root() -> Path:
    """Return the directory that contains ``data/``.

    Priority: ``$PYLOVO_ROOT``, then the first parent of this package that contains ``data/``,
    then the current working directory.
    """
    env_root = os.getenv("PYLOVO_ROOT")
    if env_root:
        root_path = Path(env_root)
        if root_path.exists():
            return root_path

    current = Path(__file__).parent
    while current != current.parent:
        if (current / "data").exists():
            return current
        current = current.parent

    return Path.cwd()


def _get_data_file_path(relative_path: str) -> str:
    """Return the path of a file in ``data/municipal_register``.

    Raises:
        FileNotFoundError: If the file does not exist (with setup hints).
    """
    repo_root = _get_repo_root()
    file_path = repo_root / "data" / "municipal_register" / relative_path
    if not file_path.exists():
        raise FileNotFoundError(
            f"Municipal data file not found: {file_path}\n\n"
            f"Setup options:\n"
            f"1. Clone full repo: git clone https://github.com/tum-ens/pylovo.git\n"
            f"2. Docker/pip install: Set PYLOVO_ROOT environment variable to your project directory\n"
            f"   Example: export PYLOVO_ROOT=/app\n"
            f"   Then ensure data/ directory exists at $PYLOVO_ROOT/data/"
        )
    return str(file_path)


def import_regiostar() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read the RegioStaR classification of German municipalities.

    RegioStaR (Regionalstatistische Raumtypologie of the Federal Ministry for Digital and
    Transport, https://bmdv.bund.de/SharedDocs/DE/Artikel/G/regionalstatistische-raumtypologie.html).

    Returns:
        Tuple of the full table (columns ``mun_code``, ``name_city``, ``pop``, ``area``,
        ``fed_state``, ``regio7``, ``regio5``, ``pop_den``) and its Bavaria-only subset
        (without ``fed_state``).
    """
    data_path = _get_data_file_path('regiostar/regiostar.xlsx')
    name_worksheet = "ReferenzGebietsstand2020"
    wb = load_workbook(data_path)
    ws = wb[name_worksheet]
    data = ws.values
    columns = next(data)[0:]
    regiostar = pd.DataFrame(data, columns=columns)
    drop_columns = ["gemrs_20", "vbgem_20", "vbgemrs_20", "vbgnam_20", "RegioStaR2", "RegioStaR4", "RegioStaR17",
                    "RegioStaRGem7", "RegioStaRGem5", "RegioStaR_Stadtregion", "RegioStaR_NameStadtregion"]
    regiostar5_7 = regiostar.drop(drop_columns, axis=1)
    # municipal code (Gemeindeschlüssel), name, population, area, federal state, RegioStaR 7 and 5
    regiostar5_7.columns = ["mun_code", "name_city", "pop", "area", "fed_state", "regio7", "regio5"]
    regiostar5_7["pop_den"] = regiostar5_7["pop"] / regiostar5_7["area"]
    regiostar5_7_bayern = regiostar5_7.loc[regiostar5_7['fed_state'] == 9]
    regiostar5_7_bayern = regiostar5_7_bayern.drop(["fed_state"], axis=1)
    return regiostar5_7, regiostar5_7_bayern


def import_plz_einwohner() -> pd.DataFrame:
    """Read population, area, latitude and longitude per PLZ (suche-postleitzahl.org)."""
    data_path = _get_data_file_path('gemeindeverzeichnis/plz_einwohner.xlsx')
    return pd.read_excel(data_path)


def import_zuordnung_plz() -> pd.DataFrame:
    """Read the PLZ to AGS mapping (suche-postleitzahl.org) without its ``osm_id`` column."""
    data_path = _get_data_file_path('gemeindeverzeichnis/zuordnung_plz_ort.xlsx')
    plz_zuordnung = pd.read_excel(data_path)
    return plz_zuordnung.drop(columns=["osm_id"])


def import_tables() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Read the three source tables.

    Returns:
        Tuple ``(plz_einwohner, plz_zuordnung, regiostar)``: population per PLZ, PLZ to AGS
        mapping and the full RegioStaR table.
    """
    plz_zuordnung = import_zuordnung_plz()
    plz_einwohner = import_plz_einwohner()
    regiostar, _ = import_regiostar()
    return plz_einwohner, plz_zuordnung, regiostar


def join_regiostar_plz(plz_pop: pd.DataFrame, plz_ags: pd.DataFrame, regiostar: pd.DataFrame) -> pd.DataFrame:
    """Join population per PLZ, the PLZ to AGS mapping and the RegioStaR classes.

    ``pop`` and ``area`` of the result refer to the PLZ, not to the municipality.

    Args:
        plz_pop: Population per PLZ (column ``population``).
        plz_ags: PLZ to AGS mapping.
        regiostar: RegioStaR table from :func:`import_regiostar`.

    Returns:
        One row per (PLZ, AGS) with RegioStaR classes and the PLZ population density.
    """
    plz_pop_ags = plz_pop.merge(plz_ags, left_on="plz", right_on="plz")
    plz_pop_ags_regio = plz_pop_ags.merge(regiostar, left_on="ags", right_on="mun_code")
    plz_pop_ags_regio = plz_pop_ags_regio.drop(
        columns=["note", "ort", "landkreis", "bundesland", "mun_code", "pop", "area", "pop_den"])
    plz_pop_ags_regio = plz_pop_ags_regio.rename(columns={"population": "pop", "qkm": "area"})
    plz_pop_ags_regio["pop_den"] = plz_pop_ags_regio["pop"] / plz_pop_ags_regio["area"]
    return plz_pop_ags_regio


def municipal_register_to_db(regiostar_plz: pd.DataFrame) -> None:
    """Write the municipal register to the database if the table is still empty.

    Args:
        regiostar_plz: Register from :func:`join_regiostar_plz`.

    Raises:
        Exception: Any database error during the insert (after printing it).
    """
    dbc_client = dbc.DatabaseClient()

    if not dbc_client.is_table_empty('municipal_register'):
        print("Municipal register table already contains data, skipping import.")
        dbc_client.close()
        return

    print(f"Importing municipal register data ({len(regiostar_plz)} rows)...")
    try:
        regiostar_plz.to_sql(
            'municipal_register',
            con=dbc_client.sqla_engine,
            if_exists='append',
            index=False,
        )
        print(f"Successfully imported {len(regiostar_plz)} rows to municipal_register table.")
    except Exception as e:
        print(f"Error importing municipal register: {e}")
        raise
    finally:
        dbc_client.close()


def create_municipal_register() -> None:
    """Build the municipal register from the source files and write it to ``municipal_register``."""
    plz_einwohner, plz_zuordnung, regiostar = import_tables()
    plz_einwohner = plz_einwohner.rename(columns={"einwohner": "population"})
    regiostar_plz = join_regiostar_plz(plz_einwohner, plz_zuordnung, regiostar)
    municipal_register_to_db(regiostar_plz)
