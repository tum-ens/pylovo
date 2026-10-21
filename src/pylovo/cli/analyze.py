"""Analyse generated grids (``pylovo-analyze``).

The PLZ analysis stores transformer, load, bus and cable statistics per PLZ; the per-grid analysis
stores the clustering parameters of every grid and needs the PLZ analysis first.

``--coverage`` accounts for every source building of the PLZ (in a grid, left out with a reason, or
missing) and ``--border-diagnostics`` reports the PLZ-border and data-edge context of the PLZ and their
grids; both write CSV/JSON reports and change nothing in the database.
"""
import argparse
import json
import sys
import traceback
from pathlib import Path

from pylovo.analysis.parameter_calculation import ParameterCalculator

#: Exit code of ``--coverage`` when source buildings are missing.
EXIT_INCOMPLETE = 3
#: Default of ``--output``.
DEFAULT_OUTPUT = "results/diagnostics"


def analyze_plz(plz: int) -> bool:
    """Calculate and store the PLZ-level parameters (skipped if they already exist).

    Returns:
        False if the PLZ has no grids, else True (also if it was analysed before).
    """
    status = ParameterCalculator().analyze_parameters_for_plz(plz)
    if status == "no_grids":
        print(f"✗ PLZ {plz} has no grids in the configured version: generate it first")
    elif status == "already_analysed":
        print(f"- PLZ {plz} is already analysed, skipped")
    else:
        print(f"✓ Calculated parameters for PLZ {plz}")
    return status != "no_grids"


def analyze_grid(plz: int) -> bool:
    """Calculate and store the clustering parameters of every grid of a PLZ (after :func:`analyze_plz`).

    Returns:
        False if the PLZ-level parameters are missing (nothing was calculated), else True.
    """
    status = ParameterCalculator().analyze_grid_parameters_for_plz(plz)
    if status == "not_analysed":
        print(f"✗ PLZ {plz} is not analysed yet (or has no grids): run pylovo-analyze --plz {plz} first")
    else:
        print(f"✓ Calculated parameters per grid for PLZ {plz}")
    return status != "not_analysed"


def run_diagnostics(plz_list: list[int], coverage: bool, border: bool, version_id: str | None,
                    output: Path) -> int:
    """Write the coverage and/or border reports of ``plz_list`` to ``output``; return the number of missing buildings."""
    from pylovo.analysis import border_diagnostics
    from pylovo.analysis.coverage import coverage_totals, reconcile_coverage
    from pylovo.config_loader import VERSION_ID
    from pylovo.database.database_client import DatabaseClient
    from pylovo.infdb.infdb_client import InfdbClient

    version_id = version_id or VERSION_ID
    output.mkdir(parents=True, exist_ok=True)
    report = {"version_id": version_id, "plz": plz_list}
    missing = 0
    with DatabaseClient() as dbc:
        inf_dbc = InfdbClient()
        if coverage:
            buildings, summary = reconcile_coverage(dbc, inf_dbc, plz_list, version_id)
            buildings.to_csv(output / f"coverage_buildings_{version_id}.csv", index=False)
            summary.to_csv(output / f"coverage_summary_{version_id}.csv", index=False)
            report["coverage"] = coverage_totals(buildings)
            missing = int(report["coverage"].get("missing", 0))
        if border:
            context = border_diagnostics.postcode_context(dbc, inf_dbc, plz_list)
            grids = border_diagnostics.grid_edges(dbc, inf_dbc, plz_list, version_id)
            context.to_csv(output / f"postcode_context_{version_id}.csv", index=False)
            grids.to_csv(output / f"grid_edges_{version_id}.csv", index=False)
            report["postcodes"] = context.round(4).to_dict(orient="records")
            report["grids"] = border_diagnostics.summarize(grids)
    (output / f"diagnostics_{version_id}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "postcodes"}, indent=2, default=str))
    return missing


def ignored_options(args: argparse.Namespace) -> list[str]:
    """Describe the options that the chosen mode of ``pylovo-analyze`` ignores."""
    if args.coverage or args.border_diagnostics:
        if args.per_grid or args.all:
            return ["--per-grid and --all are ignored: --coverage and --border-diagnostics run alone"]
        return []
    notes = []
    if args.version_id is not None:
        notes.append("--version-id is ignored without --coverage or --border-diagnostics "
                     "(the analysis uses VERSION_ID of the configuration)")
    if args.output != DEFAULT_OUTPUT:
        notes.append("--output is ignored without --coverage or --border-diagnostics")
    return notes


def main():
    """Entry point of ``pylovo-analyze``."""
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
        help="Account for every source building (in a grid, left out with a reason, or missing); exit 3 if any is missing"
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

    args = parser.parse_args()
    for note in ignored_options(args):
        print(f"Warning: {note}", file=sys.stderr)

    try:
        if args.coverage or args.border_diagnostics:
            missing = run_diagnostics(args.plz, args.coverage, args.border_diagnostics, args.version_id,
                                      Path(args.output))
            if missing:
                print(f"✗ {missing} source buildings are neither in a grid nor recorded as left out")
                sys.exit(EXIT_INCOMPLETE)
            return
        plz_analysis = args.all or not args.per_grid     # --all: the PLZ analysis, then per grid
        grid_analysis = args.all or args.per_grid
        failed = []
        for plz in args.plz:
            if plz_analysis and not analyze_plz(plz):
                failed.append(plz)
            elif grid_analysis and not analyze_grid(plz):
                failed.append(plz)
        if failed:
            print(f"✗ Nothing was calculated for PLZ {failed}")
            sys.exit(1)

    except Exception as e:
        print(f"✗ Error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

