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

from pylovo.cli import parsers
from pylovo.cli.parsers import DEFAULT_OUTPUT
from pylovo.analysis.parameter_calculation import ParameterCalculator

#: Exit code of ``--coverage`` when buildings have a failing status (:data:`FAILING_COVERAGE_STATUSES`).
EXIT_INCOMPLETE = 3
#: Statuses that fail ``--coverage``: source buildings in no grid and not recorded as left out (``missing``), and
#: buildings in a grid that are also recorded as left out (inconsistent records of the version). Stored rows
#: without a source building (``not_in_source``: the source data or its filters changed after the generation)
#: and PLZ without results (``postcode_not_generated``) are reported but do not fail.
FAILING_COVERAGE_STATUSES = ("missing", "generated_and_excluded")


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
    """Write the coverage and/or border reports of ``plz_list`` to ``output``.

    Returns:
        The number of buildings with a status in :data:`FAILING_COVERAGE_STATUSES` (0 without ``coverage``).
    """
    from pylovo.analysis import border_diagnostics
    from pylovo.analysis.coverage import coverage_totals, reconcile_coverage
    from pylovo.config_loader import VERSION_ID
    from pylovo.database.database_client import DatabaseClient
    from pylovo.infdb.infdb_client import InfdbClient

    version_id = version_id or VERSION_ID
    output.mkdir(parents=True, exist_ok=True)
    report = {"version_id": version_id, "plz": plz_list}
    failing = not_in_source = 0
    with DatabaseClient() as dbc:
        inf_dbc = InfdbClient()
        try:
            if coverage:
                buildings, summary = reconcile_coverage(dbc, inf_dbc, plz_list, version_id)
                buildings.to_csv(output / f"coverage_buildings_{version_id}.csv", index=False)
                summary.to_csv(output / f"coverage_summary_{version_id}.csv", index=False)
                report["coverage"] = coverage_totals(buildings)
                failing = sum(int(report["coverage"].get(status, 0)) for status in FAILING_COVERAGE_STATUSES)
                not_in_source = int(report["coverage"].get("not_in_source", 0))
            if border:
                context = border_diagnostics.postcode_context(dbc, inf_dbc, plz_list)
                grids = border_diagnostics.grid_edges(dbc, inf_dbc, plz_list, version_id)
                context.to_csv(output / f"postcode_context_{version_id}.csv", index=False)
                grids.to_csv(output / f"grid_edges_{version_id}.csv", index=False)
                rounded = context.round(4)
                # NaN (no scope table) becomes null: the JSON report must stay valid for strict parsers
                report["postcodes"] = rounded.astype(object).where(rounded.notna(), None).to_dict(orient="records")
                report["grids"] = border_diagnostics.summarize(grids)
        finally:
            inf_dbc.close()
    (output / f"diagnostics_{version_id}.json").write_text(
        json.dumps(report, indent=2, default=str, allow_nan=False) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "postcodes"}, indent=2, default=str,
                     allow_nan=False))
    if not_in_source:
        print(f"Note: {not_in_source} stored buildings have no source building (the source data or its filters "
              "changed after the generation); they do not fail the coverage", file=sys.stderr)
    return failing


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
    parser = parsers.analyze_parser()

    args = parser.parse_args()
    for note in ignored_options(args):
        print(f"Warning: {note}", file=sys.stderr)

    try:
        if args.coverage or args.border_diagnostics:
            failing = run_diagnostics(args.plz, args.coverage, args.border_diagnostics, args.version_id,
                                      Path(args.output))
            if failing:
                print(f"✗ {failing} buildings are missing (in no grid and not recorded as left out) or both in a "
                      "grid and recorded as left out")
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
