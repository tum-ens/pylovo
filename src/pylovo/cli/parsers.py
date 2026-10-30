"""Argument parsers of the ``pylovo-*`` commands that need the configuration.

This module imports nothing of pylovo (and no pandas or database driver), so the console scripts of
:mod:`pylovo.cli.launch` can parse their arguments, print ``--help`` and report usage errors before
:mod:`pylovo.config_loader` is imported, which needs a complete ``.env`` and the configuration files. The command
modules call the same functions to read their arguments.
"""
import argparse

DEFAULT_HOST = "127.0.0.1"   # of the transformer UI (``pylovo-import transformers-ui``)
DEFAULT_PORT = 8080
DEFAULT_OUTPUT = "results/diagnostics"   # of ``pylovo-analyze --output``

DESCRIPTION_SETUP = """Create new PyLovo tables or migrate an existing schema.

With no command, setup is non-destructive. It creates missing tables, runs pending
migrations and imports each reference table (transformers, postcodes, municipal
register) while it is empty, so a rerun completes an interrupted setup. Use the
explicit reset command to delete all data in the PyLovo schema: it moves the schema to
a backup, rebuilds it, restores the backup if the rebuild fails and drops it otherwise.
"""

def setup_parser() -> argparse.ArgumentParser:
    """The ``pylovo-setup`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pylovo-setup",
        description=DESCRIPTION_SETUP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("command", nargs="?", choices=("setup", "reset"), default="setup")
    parser.add_argument("--database", help="required for reset; must match DBNAME in .env")
    parser.add_argument("--yes", action="store_true", help="skip interactive reset confirmation")
    return parser

#: Federal states of ``pylovo-generate --all``: ISO 3166-2:DE code (and NRW) -> state key, the first two AGS digits
FEDERAL_STATES = {"sh": "01", "hh": "02", "ni": "03", "hb": "04", "nw": "05", "nrw": "05", "he": "06", "rp": "07",
                  "bw": "08", "by": "09", "sl": "10", "be": "11", "bb": "12", "mv": "13", "sn": "14", "st": "15",
                  "th": "16"}


def _federal_state(value: str) -> str:
    """The two-digit state key of a federal state given by its code (``by``, ``nw``, ``nrw``) or key (``9``, ``09``)."""
    code = value.strip().lower()
    if code in FEDERAL_STATES:
        return FEDERAL_STATES[code]
    if code.isdigit() and 1 <= int(code) <= 16:
        return f"{int(code):02d}"
    raise argparse.ArgumentTypeError(f"unknown federal state {value!r}: use its code (e.g. by, nw or nrw) "
                                     "or its key (e.g. 09)")

def generate_parser() -> argparse.ArgumentParser:
    """The ``pylovo-generate`` argument parser."""
    parser = argparse.ArgumentParser(
        prog='pylovo-generate',
        description='Generate synthetic LV distribution grids for specified regions',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Generate for single postal code (PLZ)
  pylovo-generate --plz 80803

  # Generate for multiple postal codes
  pylovo-generate --plz 80803 80802 80801

  # Generate for single municipality (AGS)
  pylovo-generate --ags 09162000

  # Generate for multiple municipalities
  pylovo-generate --ags 09162000 09161000

  # Generate every postcode with buildings in the InfDB (USE_INFDB=True), the largest first
  pylovo-generate --all

  # Only the postcodes with buildings in Bavaria and North Rhine-Westphalia
  pylovo-generate --all by nrw

  # Disable parallel processing (useful for debugging); PARALLEL in the config sets the default
  pylovo-generate --plz 80803 80802 --no-parallel

  # Generate the LV grids, then the MV grids of the configured MV variant (MV_GRID_GENERATION sets the default)
  pylovo-generate --plz 80803 80802 --mv

Regional Identifiers:
  PLZ: German postal codes (Postleitzahl) - 5-digit codes
  AGS: Municipality codes (Amtlicher Gemeindeschlüssel) - 8-digit codes

  You can find PLZ and AGS codes in your database after running pylovo-setup:
    SELECT plz, note FROM pylovo.postcode LIMIT 10;
    SELECT ags, name_city FROM pylovo.municipal_register LIMIT 10;

For more information, see the README: https://github.com/tum-ens/pylovo
        '''
    )
    region = parser.add_mutually_exclusive_group(required=True)
    region.add_argument('--plz', type=int, nargs='+',
                        help='Postal code(s) to generate grids for (e.g., 80803 or 80803 80802)')
    region.add_argument('--ags', type=int, nargs='+',
                        help='Municipality code(s) (AGS) to generate grids for (e.g., 09162000)')
    region.add_argument('--all', dest='all_states', nargs='*', type=_federal_state, metavar='STATE',
                        help='Every postcode with buildings in the InfDB, or with buildings in the given federal '
                             'states (e.g. by nrw), the largest first (USE_INFDB=True only)')
    parallelism = parser.add_mutually_exclusive_group()
    parallelism.add_argument('--parallel', dest='parallel', action='store_true', default=None,
                             help='Generate several regions in parallel worker processes')
    parallelism.add_argument('--no-parallel', dest='parallel', action='store_false',
                             help='Generate several regions one after the other')
    mv_stage = parser.add_mutually_exclusive_group()
    mv_stage.add_argument('--mv', dest='mv', action='store_true', default=None,
                          help='Run the MV stage after the LV grids (default MV_GRID_GENERATION)')
    mv_stage.add_argument('--no-mv', dest='mv', action='store_false',
                          help='Do not run the MV stage')
    return parser

def classify_parser() -> argparse.ArgumentParser:
    """The ``pylovo-classify`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pylovo-classify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Classification of the generated grids into representative grid types. The questions are asked "
                    "on the terminal unless an option answers them, so a run can also be scripted.",
        epilog=(
            "The pipeline then:\n"
            "  1. creates the sample set of CLASSIFICATION_VERSION (config/config_classification.yaml),\n"
            "  2. imports buildings (USE_INFDB=False only), generates and analyses the sample grids,\n"
            "  3. selects the clustering parameters and numbers of clusters (asked, given or computed) and writes\n"
            "     them to config/config_clustering.yaml together with the answer on additional filtering,\n"
            "  4. clusters the grids that the filters leave, writes pylovo.transformer_classified, stores the\n"
            "     choices with the classification (pylovo.classification_version) and prints the grids per stage.\n"
            "Set a new CLASSIFICATION_VERSION before each run; an existing version is rejected.\n\n"
            "Example without questions:\n"
            "  pylovo-classify --no-additional-filtering --restrict-to-generated --manual \\\n"
            "      --parameters no_branches,avg_trafo_dis --clusters 4,5"
        ),
    )
    parser.add_argument("--additional-filtering", action=argparse.BooleanOptionalAction, default=None,
                        help="also filter the grids below the four lower thresholds (default: ask)")
    parser.add_argument("--restrict-to-generated", action=argparse.BooleanOptionalAction, default=None,
                        help="sample only PLZ that already have grids (postcode_result) for VERSION_ID, so nothing "
                             "is generated (default: ask)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--auto", action="store_true",
                      help="compute the clustering parameters and the numbers of clusters (default: ask)")
    mode.add_argument("--manual", action="store_true",
                      help="take the clustering parameters and the numbers of clusters from --parameters and "
                           "--clusters (default: ask)")
    parser.add_argument("--parameters", metavar="NAME[,NAME...]",
                        help="with --manual: clustering parameters, comma-separated (columns of clustering_parameters)")
    parser.add_argument("--clusters", metavar="KMEANS,GMM",
                        help="with --manual: number of clusters of k-means and of the GMM, both from "
                             "NO_OF_CLUSTERS_ALLOWED")
    parallelism = parser.add_mutually_exclusive_group()
    parallelism.add_argument("--parallel", dest="parallel", action="store_true", default=None,
                             help="generate the grids and compute their parameters in worker processes, several PLZ "
                                  "at a time (default: PARALLEL of config_generation_lv.yaml)")
    parallelism.add_argument("--no-parallel", dest="parallel", action="store_false",
                             help="generate the grids and compute their parameters one PLZ after the other")
    return parser

def analyze_parser() -> argparse.ArgumentParser:
    """The ``pylovo-analyze`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pylovo-analyze",
        description="Analyze generated grids and calculate parameters",
        epilog="""
Examples:
  # Calculate parameters for PLZ
  pylovo-analyze --plz 80803

  # Calculate parameters per grid (run after PLZ analysis)
  pylovo-analyze --plz 80803 --per-grid

  # Do both in sequence
  pylovo-analyze --plz 80803 --all

  # Account for every source building and report the border context (CSV/JSON in results/diagnostics)
  pylovo-analyze --plz 80331 80333 --coverage --border-diagnostics
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--plz",
        type=int,
        nargs="+",
        required=True,
        help="Postal code(s) to analyze"
    )

    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Account for every source building (in a grid, left out with a reason, or missing); exit 3 if any is "
             "missing or both in a grid and recorded as left out"
    )

    parser.add_argument(
        "--border-diagnostics",
        action="store_true",
        help="Report the PLZ-border and data-edge context of the PLZ and their grids"
    )

    parser.add_argument(
        "--version-id",
        default=None,
        help="Version for --coverage/--border-diagnostics (default: VERSION_ID of the config)"
    )

    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Directory of the --coverage/--border-diagnostics reports"
    )

    parser.add_argument(
        "--per-grid",
        action="store_true",
        help="Calculate parameters per individual grid (requires PLZ analysis first)"
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help="Run both PLZ and per-grid analysis"
    )
    return parser

def delete_parser() -> argparse.ArgumentParser:
    """The ``pylovo-delete`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pylovo-delete",
        description="Delete various pylovo data from database. Every command shows the database and asks you to type "
                    "its name before it deletes; --yes skips the question (needed when stdin is not a terminal).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Delete all networks for one version across all PLZ
  pylovo-delete --version 1

  # Delete all networks for multiple versions across all PLZ
  pylovo-delete --version 1 2 3

  # Delete networks for a specific PLZ and version
  pylovo-delete networks --plz 80803 --version 1

  # Delete all transformers
  pylovo-delete transformers

  # Delete classification version data
  pylovo-delete classification --version 1

  # Delete without the question (scripts)
  pylovo-delete networks --plz 80803 --version 1 --yes

For more information, see: https://github.com/tum-ens/pylovo
        """
    )
    parser.add_argument("--version", dest="versions", nargs="+", metavar="VERSION_ID",
                        help="Version ID(s) to delete with all their networks across all PLZ (e.g., 1 2 3)")
    parser.add_argument("--yes", action="store_true",
                        help="with --version: delete without asking for the confirmation (required when stdin is not "
                             "a terminal)")
    confirmation = argparse.ArgumentParser(add_help=False)
    confirmation.add_argument("--yes", action="store_true",
                              help="delete without asking for the confirmation (required when stdin is not a terminal)")

    subparsers = parser.add_subparsers(dest="command", help="Delete operation to perform", required=False)

    # Subcommand: networks
    networks_parser = subparsers.add_parser(
        "networks",
        parents=[confirmation],
        help="Delete networks for a specific PLZ and version",
        description="Delete all generated grid networks for a specific postal code and version ID"
    )
    networks_parser.add_argument("--plz", type=int, required=True,
                                help="Postal code (e.g., 80803)")
    networks_parser.add_argument("--version", type=str, required=True,
                                help="Version ID (e.g., 1)")

    # Subcommand: transformers
    subparsers.add_parser(
        "transformers",
        parents=[confirmation],
        help="Delete all transformers",
        description="Delete all rows of the transformer table, including the manual and DSO positions, which "
                    "cannot be recreated from the OSM files"
    )

    # Subcommand: classification
    class_parser = subparsers.add_parser(
        "classification",
        parents=[confirmation],
        help="Delete classification version data",
        description="Delete all data associated with a specific classification version"
    )
    class_parser.add_argument("--version", type=int, required=True,
                             help="Classification ID (e.g., 1)")
    return parser

def export_parser() -> argparse.ArgumentParser:
    """The ``pylovo-export`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pylovo-export",
        description="Export grid geodata to CSV for QGIS visualization",
        epilog="""
Examples:
  # Export single PLZ
  pylovo-export --plz 80803

  # Export multiple PLZ
  pylovo-export --plz 80803 80639 91720

  # Export specific grid within PLZ (bcid is negative for brownfield transformers)
  pylovo-export --grid --plz 91207 --kcid 4 --bcid 30

  # Custom output directory
  pylovo-export --plz 80803 --output /path/to/output
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--plz",
        type=int,
        nargs="+",
        required=True,
        help="Postal code(s) to export"
    )
    parser.add_argument(
        "--grid",
        action="store_true",
        help="Export specific grid (requires --kcid and --bcid)"
    )
    parser.add_argument(
        "--kcid",
        type=int,
        help="K-means cluster ID (for --grid mode)"
    )
    parser.add_argument(
        "--bcid",
        type=int,
        help="Building cluster ID (for --grid mode; negative for brownfield transformers)"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output directory for CSV files (default: QGIS/)"
    )
    return parser

def import_parser() -> argparse.ArgumentParser:
    """The ``pylovo-import`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="pylovo-import",
        description="Import various data into pylovo database",
        epilog="""
Examples:
  # Import transformers from OSM by relation ID
  pylovo-import transformers-osm --relation-id 62464

  # Import DSO transformer positions from CSV
  pylovo-import transformers-dso-csv path/to/transformers.csv --source my_region --replace-source

  # Check and edit transformer positions on a browser map (needs uv sync --extra legacy-ui)
  pylovo-import transformers-ui

  # Load the eGon-data HV/MV substations of the MV stage again (--refresh: download them again first)
  pylovo-import egon-substations --refresh
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
        help="Path to CSV with external_id, lon, lat (WGS84 degrees, EPSG:4326) and optional "
             "transformer_rated_power/source columns"
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
        help="Browser map for checking and editing transformer positions (needs the extra legacy-ui)"
    )
    add_ui_arguments(ui_parser)

    egon_parser = subparsers.add_parser(
        "egon-substations",
        help="Load the eGon-data HV/MV substations (eTraGo buses of the MV stage) from the Open Energy Platform"
    )
    egon_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Download them again instead of using the cached file of the data directory"
    )
    return parser


def add_ui_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the transformer UI options to ``parser`` (shared with ``pylovo-import transformers-ui``)."""
    parser.add_argument('--host', default=DEFAULT_HOST,
                        help=f'Interface to bind to (default: {DEFAULT_HOST}, local only; '
                             '0.0.0.0 exposes the UI and its write access to the network)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Port number (default: {DEFAULT_PORT}, 0 for the first free port)')
    parser.add_argument('--debug', action='store_true', help='Enable Flask debug mode and debug logging')
    parser.add_argument('--cleanup', action='store_true',
                        help='Check the database connection before starting')
    parser.add_argument('--auto-cleanup', action='store_true',
                        help='Terminate (kill) any process that already listens on --port before starting '
                             '(uses lsof; off by default)')
