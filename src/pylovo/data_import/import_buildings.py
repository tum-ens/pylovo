"""Import building shapefiles into the database (file-based data path, ``USE_INFDB=False``).

The shapefiles are expected in ``<user data dir>/buildings`` (see
:func:`pylovo.utils.get_user_data_dir`). A file belongs to an AGS if the AGS appears in its name,
and it goes into the table ``res`` or ``oth`` if its name contains ``Res`` or ``Oth``. Imported
AGS are recorded in ``ags_log`` so that they are not imported twice.
"""
import glob
import os

import pandas as pd

from pylovo.data_import.region_resolver import resolve_regions
from pylovo.database.database_constructor import DatabaseConstructor
from pylovo.utils import get_user_data_dir


def _file_matches_ags(file_path: str, ags) -> bool:
    """Return whether the AGS appears in the file name of ``file_path``."""
    return str(ags) in os.path.basename(file_path)


def _find_building_shapefiles(ags_list: list) -> list[str]:
    """Return the building shapefiles whose file name contains one of the given AGS."""
    data_path = get_user_data_dir() / "buildings"
    files_list = glob.glob(str(data_path / "*.shp"))
    return sorted(file for file in files_list if any(_file_matches_ags(file, ags) for ags in ags_list))


def _import_shapefiles(files_to_add: list[str], ags_list: list, dbc_client) -> None:
    """Load shapefiles with ogr2ogr and record the AGS that had a file in ``ags_log``.

    AGS without a matching file are not logged, so they are imported once their file exists.
    """
    sgc = DatabaseConstructor(dbc_obj=dbc_client)
    sgc.ogr_to_db(create_list_of_shp_files(files_to_add))
    for ags in ags_list:
        if any(_file_matches_ags(file, ags) for file in files_to_add):
            dbc_client.write_ags_log(int(ags))


def _ags_not_yet_imported(ags_list, dbc_client) -> list[int]:
    """Return the AGS of ``ags_list`` that are not in ``ags_log`` yet (sorted, without duplicates)."""
    already_imported = set(dbc_client.get_ags_log()["ags"].tolist())
    return sorted(set(ags_list) - already_imported)


def import_buildings_for_single_plz(gg):
    """Import the building shapefiles of all AGS that overlap the PLZ of a grid generator.

    Args:
        gg: :class:`~pylovo.grid_generator.GridGenerator` whose ``plz`` is imported; its
            database client and logger are used.

    Raises:
        FileNotFoundError: If no shapefile matches the AGS that still need to be imported.
    """
    dbc_client = gg.dbc
    _, df_plz_ags = resolve_regions(dbc_client, plz=int(gg.plz))

    # A PLZ can belong to several municipalities (AGS).
    gg.logger.info(
        f"LV grids will be generated for PLZ {int(gg.plz)} - {len(df_plz_ags)} municipal register entries"
    )
    ags_list = sorted(set(df_plz_ags["ags"].tolist()))
    gg.logger.info(f"AGS to import: {ags_list}")

    ags_to_import = _ags_not_yet_imported(ags_list, dbc_client)
    if not ags_to_import:
        gg.logger.info("Buildings for these AGS are already in the src database.")
        return
    gg.logger.info(f"Buildings for these AGS are not in the database and will be added: {ags_to_import}")

    files_to_add = _find_building_shapefiles(ags_to_import)
    if not files_to_add:
        raise FileNotFoundError(
            f"No shapefiles found for AGS {ags_to_import} in {get_user_data_dir() / 'buildings'}"
        )

    _import_shapefiles(files_to_add, ags_to_import, dbc_client)
    gg.logger.info(f"Buildings for AGS {ags_to_import} have been successfully added to the database.")


def import_buildings_for_multiple_plz(df_plz_ags: pd.DataFrame, dbc_client):
    """Import the building shapefiles of all AGS in a municipal-register slice.

    Unlike :func:`import_buildings_for_single_plz`, missing shapefiles are not an error: AGS
    without a file are skipped and not logged.

    Args:
        df_plz_ags: Rows of ``municipal_register`` with at least the column ``ags``.
        dbc_client: Open :class:`~pylovo.database.database_client.DatabaseClient`.
    """
    ags_to_add = _ags_not_yet_imported(df_plz_ags['ags'].tolist(), dbc_client)
    files_to_add = _find_building_shapefiles(ags_to_add)
    if files_to_add:
        _import_shapefiles(files_to_add, ags_to_add, dbc_client)


def create_list_of_shp_files(files_to_add):
    """Map shapefile paths to the ``ogr_to_db`` input format.

    Args:
        files_to_add: Shapefile paths; each name must contain ``Res`` (residential) or ``Oth`` (other).

    Returns:
        List of ``{"path": ..., "table_name": "res" | "oth"}`` dictionaries.

    Raises:
        ValueError: If a file name contains neither ``Res`` nor ``Oth``, or the list is empty.
    """
    ogr_ls_dict = []
    for file_path in files_to_add:
        if "Oth" in file_path:
            table_name = "oth"
        elif "Res" in file_path:
            table_name = "res"
        else:
            raise ValueError(f"Shapefile '{file_path}' cannot be assigned to 'res' or 'oth'.")
        ogr_ls_dict.append({"path": file_path, "table_name": table_name})

    if not ogr_ls_dict:
        raise ValueError("No valid shapefiles found for the requested PLZ.")
    return ogr_ls_dict
