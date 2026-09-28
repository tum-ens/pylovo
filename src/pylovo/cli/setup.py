"""Create or migrate the PyLovo schema; reset requires an explicit command and target."""

import argparse
import sys
from pathlib import Path

from pylovo import utils
from pylovo.config_loader import CSV_FILE_LIST, DBNAME, HOST, LOG_LEVEL, PORT, USE_INFDB
from pylovo.data_import.municipal_register import create_municipal_register
from pylovo.database.database_constructor import DatabaseConstructor

DESCRIPTION = """Create new PyLovo tables or migrate an existing schema.

With no command, setup is non-destructive. It creates missing tables, runs pending
migrations, and imports source data only for a new schema. Use the explicit reset
command to delete all data in the PyLovo schema.
"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pylovo-setup",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=("setup", "reset"), default="setup")
    parser.add_argument("--database", help="required for reset; must match DBNAME in .env")
    parser.add_argument("--yes", action="store_true", help="skip interactive reset confirmation")
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
        print("stdin is not interactive: aborting. Use reset --database NAME --yes for a non-interactive reset.")
        return False
    try:
        answer = input(f"Type the database name '{dbname}' to continue: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip() == dbname


def run_setup(reset: bool = False) -> None:
    """Migrate in place; import raw data only for a new or explicitly reset schema."""
    logger = utils.create_logger(name="setup", log_file=Path("log/log.txt"), log_level=LOG_LEVEL)

    logger.info("### CREATING DATABASE CONSTRUCTOR CLASS ###")
    sgc = DatabaseConstructor()
    with sgc.dbc.conn.cursor() as cur:
        cur.execute("SELECT to_regclass('pylovo.version')")
        fresh = cur.fetchone()[0] is None
    sgc.dbc.conn.commit()
    if reset:
        logger.info("### RESETTING PYLOVO SCHEMA ###")
        sgc.reset_schema()
        fresh = True

    logger.info("### CREATING OR MIGRATING PYLOVO SCHEMA ###")
    sgc.migrate_schema()
    if not fresh:
        sgc.load_ways_preprocessing_functions()
        logger.info("### DONE: EXISTING DATABASE MIGRATED ###")
        return

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
    """Entry point: setup is safe by default, reset requires an explicit target."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "setup":
        if args.yes or args.database:
            parser.error("--yes and --database are only valid with reset")
    else:
        if not args.database:
            parser.error("reset requires --database matching DBNAME in .env")
        if args.database != DBNAME:
            parser.error(f"reset target {args.database!r} does not match configured database {DBNAME!r}")
        if not args.yes and not confirm_schema_reset(DBNAME, HOST, PORT):
            print("Reset aborted. Nothing was changed.")
            sys.exit(1)
    try:
        run_setup(reset=args.command == "reset")
    except RuntimeError as exc:
        print(f"✗ Setup stopped: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
