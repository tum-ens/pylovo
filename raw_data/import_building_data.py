import glob
import os
import sys

import pandas as pd

from syngrid.SyngridDatabaseConstructor import SyngridDatabaseConstructor


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
        path = path.replace(path_to_this_folder, ".\\raw_data")
        ogr_ls_dict.append({"path": path, "table_name": table_name})
    if ogr_ls_dict:
        return ogr_ls_dict
    else:
        raise Exception("Shapefiles of buildings for requested PLZ are not available.")


def import_buildings_for_single_plz(plz, plz_regiostar):
    """
    imports building data to db for plz
    """
    # check whether plz exists
    if int(plz) not in plz_regiostar['plz'].values:
        raise Exception("PLZ does not exist in register")
    # get name and ags for the desired plz
    ags_to_add = plz_regiostar[plz_regiostar['plz'] == int(plz)]
    print('LV grids will be generated for', ags_to_add.iloc[0]['plz'], ags_to_add.iloc[0]['name'])
    ags = ags_to_add.iloc[0]['ags']
    print('It´s AGS is:', ags)

    # check in ags_log if ags is already on the database
    df_log = pd.read_csv('raw_data\\ags_log.csv', index_col=0)
    if ags in df_log['ags'].values:
        print('Buildings of AGS are already on the pylovo database.')
    else:
        print('Buildings are not yet on the database and will be added to pylovo database.')

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
        sgc = SyngridDatabaseConstructor()
        sgc.ogr_to_db(ogr_ls_dict)

        # adding the added ags to the log file
        df_new_ags = pd.DataFrame(columns=['ags'])
        df_new_ags.at[0, 'ags'] = ags
        df_log = pd.concat([df_log, df_new_ags])
        df_log.reset_index(inplace=True, drop=True)
        df_log.to_csv('raw_data\\ags_log.csv')


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
    df_log = pd.read_csv('raw_data\\ags_log.csv', index_col=0)
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

    # making a list of dicts for the function scg.ogr_to_db()
    ogr_ls_dict = create_list_of_shp_files(files_to_add, path_to_this_folder)

    # adding the buildings to the database
    sgc = SyngridDatabaseConstructor()
    sgc.ogr_to_db(ogr_ls_dict)

    # adding the added ags to the log file
    new_ags = {'ags': ags_to_add}
    df_new_ags = pd.DataFrame(new_ags)
    df_log = pd.concat([df_log, df_new_ags])
    df_log.reset_index(inplace=True, drop=True)
    df_log.to_csv('raw_data\\ags_log.csv')
