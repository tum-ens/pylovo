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


def import_test_postcodes():
    """Import test postcode geometries for testing."""
    from pylovo.data_import.test_postcodes import main as test_main
    test_main()


def main():
    """Main entry point for import operations."""
    parser = argparse.ArgumentParser(
        prog="pylovo-import",
        description="Import various data into pylovo database",
        epilog="""
Examples:
  # Import transformers from OSM by relation ID
  pylovo-import transformers-osm --relation-id 62464
  
  # Launch interactive transformer UI
  pylovo-import transformers-ui
  
  # Import test postcode geometries
  pylovo-import test-postcodes
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

    # Subcommand: test-postcodes
    subparsers.add_parser(
        "test-postcodes",
        help="Import test postcode geometries"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "transformers-osm":
            import_transformers_osm(args.relation_id)
        elif args.command == "transformers-ui":
            import_transformers_ui_with_options(
                host=args.host,
                port=args.port,
                debug=args.debug,
                cleanup=args.cleanup,
                auto_cleanup=args.auto_cleanup,
            )
        elif args.command == "test-postcodes":
            import_test_postcodes()
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == "__main__":
    main()

