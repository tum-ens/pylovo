"""Import data into the pylovo database (``pylovo-import``).

Subcommands:
    transformers-osm: fetch transformers of an OSM relation from the Overpass API and add them.
    transformers-dso-csv: add DSO transformer positions from a CSV file.
    transformers-ui: deprecated Flask map for editing transformer positions (use the GridPlanner UI).
"""
import argparse
import sys
import time

from pylovo.data_import.dso_transformers import import_dso_transformers_csv
from pylovo.data_import.import_transformers import (
    fetch_trafos,
    get_trafos_processed_target_geojson_path,
    process_trafos,
)
from pylovo.data_import.transformers_ui import _add_ui_arguments, run_transformers_ui
from pylovo.database.database_constructor import DatabaseConstructor


def import_transformers_osm(relation_id: int):
    """Fetch, filter and import the transformers of an OSM relation (existing rows are kept).

    Args:
        relation_id: OSM relation id of the area, e.g. 62464.
    """
    start_time = time.time()

    print("Fetching transformers...")
    fetch_trafos(relation_id)

    print("Processing transformers...")
    process_trafos(relation_id)

    out_file = get_trafos_processed_target_geojson_path(relation_id)

    # Append to the transformers table; rows that already exist (same osm_id) are skipped.
    print("Loading transformers into database...")
    constructor = DatabaseConstructor()
    constructor.ogr_to_db([{"path": out_file, "table_name": "transformers"}], skip_failures=True)

    elapsed = time.time() - start_time
    print(f"✓ Completed in {elapsed:.1f}s")


def import_transformers_dso_csv(csv_path: str, source: str | None, replace_source: bool):
    """Import DSO transformer positions from a CSV file.

    Args:
        csv_path: CSV with ``external_id``, ``lon``, ``lat`` (EPSG:4326) and optional
            ``transformer_rated_power`` and ``source`` columns.
        source: Source label for the generated ids ``dso/<source>/<external_id>``.
        replace_source: Delete existing rows of this source before importing.
    """
    start_time = time.time()
    count = import_dso_transformers_csv(csv_path, source=source, replace_source=replace_source)
    elapsed = time.time() - start_time
    print(f"✓ Imported {count} DSO transformer positions in {elapsed:.1f}s")


def main():
    """Entry point of ``pylovo-import``."""
    parser = argparse.ArgumentParser(
        prog="pylovo-import",
        description="Import various data into pylovo database",
        epilog="""
Examples:
  # Import transformers from OSM by relation ID
  pylovo-import transformers-osm --relation-id 62464

  # Import DSO transformer positions from CSV
  pylovo-import transformers-dso-csv path/to/transformers.csv --source my_region --replace-source

  # Deprecated: the old transformer map (use the GridPlanner UI; needs uv sync --extra legacy-ui)
  pylovo-import transformers-ui
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    subparsers = parser.add_subparsers(dest="command", help="Import operation to perform")

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

    ui_parser = subparsers.add_parser(
        "transformers-ui",
        help="Deprecated: the old transformer map (use the GridPlanner UI; needs the extra legacy-ui)"
    )
    _add_ui_arguments(ui_parser)

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
            run_transformers_ui(
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
        sys.exit(1)


if __name__ == "__main__":
    main()
