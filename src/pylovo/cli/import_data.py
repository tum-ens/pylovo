"""Import data into the pylovo database (``pylovo-import``).

Subcommands:
    transformers-osm: fetch transformers of an OSM relation from the Overpass API and add them.
    transformers-dso-csv: add DSO transformer positions from a CSV file.
    transformers-ui: browser map for checking and editing transformer positions (needs the extra legacy-ui).
"""
import json
import sys
import time

from pylovo.cli import parsers
from pylovo.data_import.dso_transformers import import_dso_transformers_csv
from pylovo.data_import.import_transformers import (
    fetch_trafos,
    get_trafos_processed_target_geojson_path,
    process_trafos,
)
from pylovo.data_import.transformers_ui import run_transformers_ui
from pylovo.database.database_constructor import DUPLICATE_KEY_ERROR, DatabaseConstructor


def _count_features(geojson_path) -> int:
    """Return the number of features of a GeoJSON file."""
    with open(geojson_path, encoding="utf-8") as file:
        return len(json.load(file)["features"])


def import_transformers_osm(relation_id: int):
    """Fetch, filter and import the transformers of an OSM relation (existing rows are kept).

    Prints how many transformers were added and how many were skipped (their id exists already).

    Args:
        relation_id: OSM relation id of the area, e.g. 62464.

    Raises:
        RuntimeError: If ``ogr2ogr`` reported errors other than duplicate keys.
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
    rows_before = constructor.row_count("transformers")
    errors = constructor.ogr_to_db([{"path": out_file, "table_name": "transformers"}], skip_failures=True)
    added = constructor.row_count("transformers") - rows_before
    summary = f"added {added}, skipped {_count_features(out_file) - added}"

    other_errors = [error for error in errors if DUPLICATE_KEY_ERROR not in error]
    if other_errors:
        raise RuntimeError(f"ogr2ogr reported errors other than duplicate keys ({summary}): " + " | ".join(other_errors))
    elapsed = time.time() - start_time
    print(f"✓ Completed in {elapsed:.1f}s: {summary}")


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
    parser = parsers.import_parser()

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
