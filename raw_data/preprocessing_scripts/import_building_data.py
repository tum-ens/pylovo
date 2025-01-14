import glob
import os
import sys

import pandas as pd

from pylovo.GridGenerator import GridGenerator
from pylovo.SyngridDatabaseConstructor import SyngridDatabaseConstructor


def create_list_of_shp_files(files_to_add, path_to_this_folder):
    """
    making a list of dicts for the function scg.ogr_to_db()
    """
    ogr_ls_dict = []
    for file in files_to_add:
        if "Oth" in file:
            table_name = "oth"
        elif "Res" in file:
            table_name = "res"
        else:
            raise ValueError("shape file cannot be assigned to res or oth")
        path = file
        path = path.replace(path_to_this_folder, "./raw_data")  # ".\\raw_data")  #
        ogr_ls_dict.append({"path": path, "table_name": table_name})
    if ogr_ls_dict:
        return ogr_ls_dict
    else:
        raise Exception("Shapefiles of buildings for requested PLZ are not available.")


def import_buildings_for_single_plz(gg: GridGenerator) -> None:  # , plz_regiostar):
    """imports building data to db for plz:\n
    * PLZ is matched with AGS\n
    * file name is generated\n
    * buildings files are imported to database with SyngridDatabaseConstructor\n
    * AGS is added to AGS as not to import same building data again

    :param gg: Grid generator object to get the plz and functions from
    :type plz: string
    """
    # get AGS for PLZ
    pg = gg.pgr
    ags_to_add = pg.get_municipal_register_for_plz(plz=gg.plz)

    # check whether plz exists
    if ags_to_add.empty:
        raise Exception("PLZ does not exist in register")
    # get name and ags for the desired plz
    gg.logger.info(f"LV grids will be generated for {ags_to_add.iloc[0]['plz']} {ags_to_add.iloc[0]['name_city']}")
    ags = ags_to_add.iloc[0]['ags']
    gg.logger.info(f'It´s AGS is:{ags}')

    # check in ags_log if ags is already on the database
    df_log = pg.get_ags_log()
    if ags in df_log['ags'].values:
        gg.logger.info('Buildings of AGS are already on the pylovo database.')
    else:
        gg.logger.info('Buildings are not yet on the database and will be added to pylovo database.')

        # absolute path to search all shape files inside a subfolders
        path_to_this_folder = os.path.dirname(__file__)
        data_path = os.path.join(path_to_this_folder, '**', '*.shp')
        sys.path.append(data_path)

        # retrieving all shape files
        files_list = glob.glob(data_path, recursive=True)

        # creating a list that only contains the files to add
        files_to_add = []
        for file in files_list:
            if str(ags) in file:
                files_to_add.append(file)

        # making a list of dicts for the function scg.ogr_to_db()
        ogr_ls_dict = create_list_of_shp_files(files_to_add, path_to_this_folder)

        # adding the buildings to the database
        sgc = SyngridDatabaseConstructor(pgr=pg)
        sgc.ogr_to_db(ogr_ls_dict)

        # adding the added ags to the log table
        pg.write_ags_log(ags)


def import_buildings_for_multiple_plz(sample_plz):
    """
    imports building data to db for multiple plz
    """
    # absolute path to search all shape files inside a subfolders
    path_to_this_folder = os.path.dirname(__file__)
    data_path = os.path.join(path_to_this_folder, '**', '*.shp')
    sys.path.append(data_path)

    # retrieving all shape files
    files_list = glob.glob(data_path, recursive=True)

    # get all AGS that need to be imported for the classification
    ags_to_add = sample_plz['ags']
    ags_to_add = ags_to_add.tolist()
    ags_to_add = list(set(ags_to_add))  # dropping duplicates

    # check in ags_log if any ags are already on the database
    gg = GridGenerator(plz='80639')
    pg = gg.pgr
    df_log = pg.get_ags_log()
    log_ags_list = df_log['ags'].values.tolist()
    ags_to_add = list(set(ags_to_add).difference(log_ags_list))  # dropping already imported ags
    ags_to_add = list(map(str, ags_to_add))

    # creating a list that only contains the files to add
    files_to_add = []
    for file in files_list:
        for ags in ags_to_add:
            if ags in file:
                files_to_add.append(file)
    files_to_add = list(set(files_to_add))  # dropping duplicates

    if files_to_add:
        # define a list of required shapefiles to add to the database for the function scg.ogr_to_db()
        ogr_ls_dict = create_list_of_shp_files(files_to_add, path_to_this_folder)

        # adding the buildings to the database
        sgc = SyngridDatabaseConstructor()
        sgc.ogr_to_db(ogr_ls_dict)

        # adding the added ags to the log file
        for ags in ags_to_add:
            pg.write_ags_log(int(ags))