import glob
import os
import sys

from pylovo.GridGenerator import GridGenerator
from pylovo.SyngridDatabaseConstructor import SyngridDatabaseConstructor


def import_buildings_for_single_plz(gg):
    """
    Imports ags building data to the database for a given PLZ specified in the GridGenerator object.
    AGS is added to ags_log table to avoid importing the same building data again.

    :param gg: Grid generator object for querying relevant PLZ and AGS data
    """
    # Retrieve AGS for the specified PLZ
    pg = gg.pgr
    ags_to_add = pg.get_municipal_register_for_plz(plz=gg.plz)

    # Check if the PLZ exists
    if ags_to_add.empty:
        raise Exception("PLZ does not exist in the municipal register.")

    # Extract name and AGS for the desired PLZ
    gg.logger.info(f"LV grids will be generated for {ags_to_add.iloc[0]['plz']} {ags_to_add.iloc[0]['name_city']}")
    ags = ags_to_add.iloc[0]["ags"]
    gg.logger.info(f"It's AGS is: {ags}")

    # Check if AGS is already in the database (avoid duplication)
    df_log = pg.get_ags_log()
    if ags in df_log["ags"].values:
        gg.logger.info("Buildings of this AGS are already in the pylovo database.")
        return
    else:
        gg.logger.info("Buildings for this AGS are not in the database and will be added.")

    # Define the path for building shapefiles
    data_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "buildings"))
    shapefiles_pattern = os.path.join(data_path, "*.shp")  # Pattern for shapefiles

    # Retrieve all matching shapefiles
    files_list = glob.glob(shapefiles_pattern, recursive=True)

    # Filter files containing the specific AGS in their filenames
    files_to_add = [file for file in files_list if str(ags) in file]

    # Handle cases where no matching files are found
    if not files_to_add:
        raise FileNotFoundError(f"No shapefiles found for AGS {ags} in {data_path}")

    # Create a list of dictionaries for ogr_to_db()
    ogr_ls_dict = create_list_of_shp_files(files_to_add)

    # Add building data to the database
    sgc = SyngridDatabaseConstructor(pgr=pg)
    sgc.ogr_to_db(ogr_ls_dict)

    # Log the successfully added AGS to the log table in the database
    pg.write_ags_log(ags)

    gg.logger.info(f"Buildings for AGS {ags} have been successfully added to the database.")



def import_buildings_for_multiple_plz(sample_plz):
    """
    imports building data to db for multiple plz
    """
    # Define the path for building shapefiles
    data_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "buildings"))
    shapefiles_pattern = os.path.join(data_path, "*.shp")  # Pattern for shapefiles

    # retrieving all shape files
    files_list = glob.glob(shapefiles_pattern, recursive=True)

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
        ogr_ls_dict = create_list_of_shp_files(files_to_add)

        # adding the buildings to the database
        sgc = SyngridDatabaseConstructor()
        sgc.ogr_to_db(ogr_ls_dict)

        # adding the added ags to the log file
        for ags in ags_to_add:
            pg.write_ags_log(int(ags))

def create_list_of_shp_files(files_to_add):
    """
    Creates a list of dictionaries required for the scg.ogr_to_db() function.

    :param files_to_add: List of shapefile paths to add.
    :return: A list of dictionaries with keys "path" and "table_name".
    """
    ogr_ls_dict = []

    # Process each file path
    for file_path in files_to_add:
        # Determine table_name based on file naming convention
        if "Oth" in file_path:
            table_name = "oth"
        elif "Res" in file_path:
            table_name = "res"
        else:
            raise ValueError(f"Shapefile '{file_path}' cannot be assigned to 'res' or 'oth'.")

        ogr_ls_dict.append({"path": file_path, "table_name": table_name})

    # Ensure the list is not empty
    if ogr_ls_dict:
        return ogr_ls_dict
    else:
        raise Exception("No valid shapefiles found for the requested PLZ.")