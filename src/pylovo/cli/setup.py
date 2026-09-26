"""Set up the pylovo database (``pylovo-setup``).

The setup is destructive: it drops the ``pylovo`` schema with ``CASCADE`` (all generated grids,
analysis results and imported transformers are lost) and rebuilds it from scratch. Without
``--yes`` it asks the user to type the database name before anything is changed.
"""

import argparse
import sys

from pylovo import utils
from pylovo.config_loader import CSV_FILE_LIST, DBNAME, HOST, LOG_LEVEL, PORT, USE_INFDB
from pylovo.data_import.municipal_register import create_municipal_register
from pylovo.database.database_constructor import DatabaseConstructor

DESCRIPTION = """\
Create or reset the pylovo database schema.

WARNING: this drops the schema 'pylovo' with CASCADE in the database configured in .env
and deletes every generated grid, analysis result and imported transformer in it.

Steps, in this order:
  1. drop the schema 'pylovo' (CASCADE) and create it again
  2. create all pylovo tables
  3. import the transformer positions from the processed OSM geojson in
     data/transformer_data/processed_trafos (if the file is missing, the transformers are
     fetched from the Overpass API and processed first, which can take more than 30 min)
  4. with USE_INFDB=True: copy the postcode data from the InfDB;
     with USE_INFDB=False: import data/postcode.csv and build the ways table from OSM (~30 min)
  5. load the PostGIS SQL functions used for ways preprocessing
  6. fill the municipal register (data/municipal_register)
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pylovo-setup",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Without --yes you are asked to type the database name before anything is dropped.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt and drop the schema 'pylovo' right away (for scripts)",
    )
    return parser


def confirm_schema_reset(dbname: str, host: str, port: str) -> bool:
    """Ask the user to confirm the schema reset by typing the database name.

    Args:
        dbname: Name of the database whose ``pylovo`` schema will be dropped.
        host: Database host, shown to the user.
        port: Database port, shown to the user.

    Returns:
        True only if stdin is interactive and the user typed exactly ``dbname``. A mismatch,
        end of input (Ctrl-D) or a non-interactive stdin return False.
    """
    print("pylovo-setup drops the schema 'pylovo' (CASCADE) and rebuilds it.")
    print(f"  host:     {host}")
    print(f"  port:     {port}")
    print(f"  database: {dbname}")
    print("All grids, results and transformers stored in this schema will be deleted.")
    if sys.stdin is None or not sys.stdin.isatty():
        print("stdin is not interactive: aborting. Use --yes to run the setup non-interactively.")
        return False
    try:
        answer = input(f"Type the database name '{dbname}' to continue: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip() == dbname


def run_setup() -> None:
    """Run all setup steps in their fixed order (drops and rebuilds the ``pylovo`` schema)."""
    log_dir = utils.reset_log_directory()
    logger = utils.create_logger(name="setup", log_file=log_dir / "log.txt", log_level=LOG_LEVEL)

    logger.info("### CREATING DATABASE CONSTRUCTOR CLASS ###")
    sgc = DatabaseConstructor()
    logger.info("### RESETTING PYLOVO SCHEMA ###")
    sgc.reset_schema()

    logger.info("### CREATING SCHEMA pylovo ###")
    sgc.create_schema()

    logger.info("### CREATE ALL TABLES ###")
    sgc.create_table(table_name="all")

    logger.info("### DELETE EXISTING TRANSFORMERS AND INSERT NEW ONES INTO DB (without geojson in data/transformer_data this can take more than 30 min) ###")
    sgc.transformers_to_db(clear_existing=True)

    if USE_INFDB:
        # Copy the postcode polygons from the InfDB into the local 'postcode' table.
        logger.info("### FETCH AND POPULATE POSTCODE DATA FROM INFDB ###")
        sgc.load_postcode_from_infdb()
    else:
        # File-based data path: postcode CSV plus the OSM ways table.
        logger.info("### POPULATE DB WITH CSV RAW DATA ###")
        sgc.csv_to_db(CSV_FILE_LIST)

        logger.info("### POPULATE public_2po_4pgr TABLE (~30 min) ###")
        sgc.create_public_2po_table()

        logger.info("### PROCESS WAYS AND INSERTING THEM INTO ways TABLE ###")
        sgc.ways_to_db()

    logger.info("### LOAD POSTGIS FUNCTIONS FOR WAYS PREPROCESSING ###")
    sgc.load_ways_preprocessing_functions()

    # Table with all German municipalities (PLZ <-> AGS, RegioStaR classes).
    logger.info("### FILL municipal_register TABLE ###")
    create_municipal_register()

    logger.info("### DONE ###")


def main(argv: list[str] | None = None) -> None:
    """Entry point of ``pylovo-setup``: parse arguments, confirm, then run the setup."""
    args = _build_parser().parse_args(argv)
    if args.yes:
        print(f"--yes given: dropping schema 'pylovo' in database '{DBNAME}' on {HOST}:{PORT}.")
    elif not confirm_schema_reset(DBNAME, HOST, PORT):
        print("Setup aborted. Nothing was changed.")
        sys.exit(1)
    try:
        run_setup()
    except RuntimeError as exc:  # e.g. the PostGIS check of reset_schema, which runs before any drop
        print(f"✗ Setup stopped: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
