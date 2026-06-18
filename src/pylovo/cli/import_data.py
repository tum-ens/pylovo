"""
Import operations for pylovo data.
"""
import argparse
import sys
import time
from pathlib import Path

from pylovo.data_import.import_transformers import (
    get_trafos_processed_target_geojson_path,
    fetch_trafos,
    process_trafos,
)
import pylovo.database.database_constructor
from pylovo.data_import.dso_transformers import import_dso_transformers_csv


def import_transformers_osm(relation_id: int):
    """Fetch transformers from Overpass API and import to database."""
    start_time = time.time()

    print("Fetching transformers...")
    fetch_trafos(relation_id)

    print("Processing transformers...")
    process_trafos(relation_id)

    out_file = get_trafos_processed_target_geojson_path(relation_id)

    # Load into database
    print("Loading transformers into database...")
    constructor = pylovo.database.database_constructor.DatabaseConstructor()
    constructor.transformers_to_db_from_geojson(out_file, clear_existing=False)

    elapsed = time.time() - start_time
    print(f"✓ Completed in {elapsed:.1f}s")


def import_transformers_dso_csv(csv_path: str, source: str | None, replace_source: bool):
    """Import DSO transformer positions from a CSV file."""
    start_time = time.time()
    count = import_dso_transformers_csv(csv_path, source=source, replace_source=replace_source)
    elapsed = time.time() - start_time
    print(f"✓ Imported {count} DSO transformer positions in {elapsed:.1f}s")


def import_transformers_ui():
    """Launch interactive UI for transformer management."""
    from pylovo.data_import.transformers_ui import run_transformers_ui
    run_transformers_ui()


def import_transformers_ui_with_options(host: str, port: int, debug: bool, cleanup: bool, auto_cleanup: bool):
    """Launch interactive UI for transformer management with explicit options."""
    from pylovo.data_import.transformers_ui import run_transformers_ui
    run_transformers_ui(
        host=host,
        port=port,
        debug=debug,
        cleanup=cleanup,
        auto_cleanup=auto_cleanup,
    )


def main():
    """Main entry point for import operations."""
    parser = argparse.ArgumentParser(
        prog="pylovo-import",
        description="Import various data into pylovo database",
        epilog="""
Examples:
  # Import transformers from OSM by relation ID
  pylovo-import transformers-osm --relation-id 62464
  
  # Import DSO transformer positions from CSV
  pylovo-import transformers-dso-csv path/to/transformers.csv --source my_region --replace-source

  # Launch interactive transformer UI
  pylovo-import transformers-ui
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Import operation to perform")

    # Subcommand: transformers-osm
    osm_parser = subparsers.add_parser(
        "transformers-osm",
        help="Fetch and import transformers from OpenStreetMap"
    )
    osm_parser.add_argument(
        "--relation-id",
        type=int,
        required=True,
        help="OSM relation ID of the area"
    )

    # Subcommand: transformers-dso-csv
    dso_csv_parser = subparsers.add_parser(
        "transformers-dso-csv",
        help="Import DSO transformer positions from CSV"
    )
    dso_csv_parser.add_argument(
        "csv_path",
        help="Path to CSV with external_id, lon, lat and optional transformer_rated_power/source columns"
    )
    dso_csv_parser.add_argument(
        "--source",
        help="Source label used in generated ids dso/<source>/<external_id>; overrides a CSV source column"
    )
    dso_csv_parser.add_argument(
        "--replace-source",
        action="store_true",
        help="Delete existing dso/<source>/... rows before importing this source"
    )

    # Subcommand: transformers-ui
    ui_parser = subparsers.add_parser(
        "transformers-ui",
        help="Launch interactive UI for transformer management"
    )
    ui_parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host address (default: 0.0.0.0)"
    )
    ui_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port number (default: 8080, 0 for auto-detect)"
    )
    ui_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode"
    )
    ui_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Clean up lingering connections before starting"
    )
    ui_parser.add_argument(
        "--auto-cleanup",
        action="store_true",
        default=True,
        help="Automatically clean up port conflicts (default: True)"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "transformers-osm":
            import_transformers_osm(args.relation_id)
        elif args.command == "transformers-dso-csv":
            import_transformers_dso_csv(args.csv_path, args.source, args.replace_source)
        elif args.command == "transformers-ui":
            import_transformers_ui_with_options(
                host=args.host,
                port=args.port,
                debug=args.debug,
                cleanup=args.cleanup,
                auto_cleanup=args.auto_cleanup,
            )
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == "__main__":
    main()

