"""Create or migrate the PyLovo schema; reset requires an explicit command and target."""

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pylovo import utils
from pylovo.config_loader import CSV_FILE_LIST, DBNAME, HOST, LOG_LEVEL, PORT, USE_INFDB
from pylovo.data_import.municipal_register import create_municipal_register, missing_input_files
from pylovo.database.database_constructor import DatabaseConstructor
from pylovo.utils import get_user_data_dir

DESCRIPTION = """Create new PyLovo tables or migrate an existing schema.

With no command, setup is non-destructive. It creates missing tables, runs pending
migrations and imports each reference table (transformers, postcodes, municipal
register) while it is empty, so a rerun completes an interrupted setup. Use the
explicit reset command to delete all data in the PyLovo schema.
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


@dataclass
class ImportStep:
    """A reference table that setup fills while it is empty."""

    title: str
    table: str
    run: Callable[[], None]
    check: Callable[[], list[str]] = list  # problems with the input data; checked before any change


def _missing(paths: list[Path]) -> list[str]:
    return [f"missing input file {path}" for path in paths if not path.exists()]


def import_steps(sgc: DatabaseConstructor) -> list[ImportStep]:
    """Return the reference tables of a complete schema in import order."""

    def ways() -> None:
        sgc.create_public_2po_table()
        sgc.ways_to_db()

    steps = [ImportStep("IMPORT OSM TRANSFORMERS (without the processed GeoJSON in data/transformer_data "
                        "this can take more than 30 min)", "transformers",
                        lambda: sgc.transformers_to_db(clear_existing=False))]
    if USE_INFDB:
        # Copy the postcode polygons from the InfDB into the local 'postcode' table.
        steps.append(ImportStep("FETCH AND POPULATE POSTCODE DATA FROM INFDB", "postcode", sgc.load_postcode_from_infdb,
                                lambda: [problem] if (problem := sgc.infdb_postcodes_problem()) else []))
    else:
        # File-based data path: postcode CSV plus the OSM ways table.
        steps.append(ImportStep("POPULATE DB WITH CSV RAW DATA", "postcode", lambda: sgc.csv_to_db(CSV_FILE_LIST),
                                lambda: _missing([Path(f["path"]) for f in CSV_FILE_LIST])))
        steps.append(ImportStep("POPULATE public_2po_4pgr AND THE ways TABLE (~30 min)", "ways", ways,
                                lambda: _missing([get_user_data_dir() / "ways" / "ways_public_2po_4pgr.sql"])))
    # Table with all German municipalities (PLZ <-> AGS, RegioStaR classes).
    steps.append(ImportStep("FILL municipal_register TABLE", "municipal_register", create_municipal_register,
                            lambda: [f"missing input file {path}" for path in missing_input_files()]))
    return steps


def build_schema(sgc: DatabaseConstructor, steps: list[ImportStep], logger: logging.Logger) -> None:
    """Create or migrate the tables, fill the empty reference tables and load the SQL functions."""
    logger.info("### CREATING OR MIGRATING PYLOVO SCHEMA ###")
    sgc.migrate_schema()
    for step in steps:
        if sgc.table_is_empty_or_missing(step.table):
            logger.info(f"### {step.title} ###")
            step.run()
        else:
            logger.info(f"### SKIPPED: pylovo.{step.table} already has rows ###")
    logger.info("### LOAD POSTGIS FUNCTIONS FOR WAYS PREPROCESSING ###")
    sgc.load_ways_preprocessing_functions()


def run_setup(reset: bool = False) -> None:
    """Create or migrate the schema, then import every reference table that is still empty.

    A rerun therefore completes an interrupted setup; tables with rows are kept.
    """
    logger = utils.create_logger(name="setup", log_file=Path("log/log.txt"), log_level=LOG_LEVEL)

    logger.info("### CREATING DATABASE CONSTRUCTOR CLASS ###")
    sgc = DatabaseConstructor()
    steps = import_steps(sgc)
    logger.info("### CHECKING THE INPUT DATA ###")
    todo = [step for step in steps if reset or sgc.table_is_empty_or_missing(step.table)]
    problems = [problem for step in todo for problem in step.check()]
    if problems:
        raise RuntimeError("nothing was changed, the input data is incomplete: " + "; ".join(problems))
    if reset:
        logger.info("### RESETTING PYLOVO SCHEMA ###")
        sgc.reset_schema()
    build_schema(sgc, steps, logger)
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
