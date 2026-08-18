"""
This script creates a pylovo database and fills with raw data from referenced files.
Do not use DatabaseConstructor class unless you want to create a new database.
"""

from pylovo.data_import.municipal_register import create_municipal_register
from pylovo.database.database_constructor import DatabaseConstructor
from pylovo import utils
from pylovo.config_loader import *


def main():
    # Set up logging
    log_dir = utils.reset_log_directory()
    logger = utils.create_logger(name="setup", log_file=log_dir / "log.txt", log_level=LOG_LEVEL)

    ### Create constructor class
    logger.info("### CREATING DATABASE CONSTRUCTOR CLASS ###")
    sgc = DatabaseConstructor()
    logger.info("### DROPPING ALL TABLES ###")
    # sgc.drop_all_tables() #uncomment for debugging

    ### Create schema if it doesn't exist
    logger.info("### CREATING SCHEMA pylovo IF NOT EXISTS ###")
    sgc.create_schema()

    ### Create database with predefined table structure
    logger.info("### CREATE ALL TABLES ###")
    sgc.create_table(table_name="all")

    ### Add transformer data from geojson to the database
    logger.info("### DELETE EXISTING TRANSFORMERS AND INSERT NEW ONES INTO DB (without geojson in data/transformer_data this can take more than 30 min) ###")
    sgc.transformers_to_db(clear_existing=True)

    if USE_INFDB:
        ### Fetch postcode data from InfDB and insert into local 'postcode' table
        logger.info("### FETCH AND POPULATE POSTCODE DATA FROM INFDB ###")
        sgc.load_postcode_from_infdb()

    if not USE_INFDB:
        ### Add defined csv raw data from CSV_FILE_LIST to the database (ATM only postcode data)
        logger.info("### POPULATE DB WITH CSV RAW DATA ###")
        sgc.csv_to_db(CSV_FILE_LIST)

        ### Create table with data from osm
        logger.info("### POPULATE public_2po_4pgr TABLE (~30 min) ###")
        sgc.create_public_2po_table()

        ### Transform these data into our ways table
        logger.info("### PROCESS WAYS AND INSERTING THEM INTO ways TABLE ###")
        sgc.ways_to_db()

    # Load PostGIS SQL functions required for preprocessing ways
    logger.info("### LOAD POSTGIS FUNCTIONS FOR WAYS PREPROCESSING ###")
    sgc.load_ways_preprocessing_functions()

    ### Create table with entries of all German municipalities and cities
    logger.info("### FILL municipal_register TABLE ###")
    create_municipal_register()

    logger.info("### DONE ###")


if __name__ == "__main__":
    main()

