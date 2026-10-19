"""Medium-voltage (MV) grids on stored LV versions (``pylovo-mv``).

Subcommands:

- ``generate``: run the MV stage for one MV variant of an LV version (:func:`pylovo.mv_grid.stage.run_mv_stage`).
- ``couple``: LV design-point check and merged MV+LV power flows of a done variant.
- ``delete``: delete MV variants of a version.
- ``list``: list the MV variants (status, PLZ, grids, km, stale).
- ``graph``: build or reuse the MV routing graph of a region and print its QA tables.
- ``export``: write the stored pandapower net of one MV grid to a JSON file.
- ``boundary``: write the HV boundary of a variant (one row per supplying HV/MV substation with its eTraGo 110 kV bus,
  load-case P and Q, HV/MV transformers and their loading) to a CSV file.
- ``display``: store the display geometry of variants again (lanes where lines share a route, busbar circles;
  :mod:`pylovo.mv_grid.display`), e.g. for variants built before it existed.

Every output line starts with ✓ or ✗; the exit code is 0 when the command (the variant, the coupling) is done and
1 otherwise. The command never constructs ``GridGenerator``: it works on stored LV versions with an explicit
``--version``.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

from pylovo.mv_grid.model import CASES
from pylovo.mv_grid.options import parse_assignment

EPILOG = """
Examples:
  # MV grids for two PLZ of LV version mv1, MV variant d0 with cable rings only
  pylovo-mv generate --version mv1 --plz 80803 80802 --variant d0 --set MV_STUBS=none MV_OVERHEAD_LINES=none

  # All generated PLZ of a version with the configured MV variant, four workers
  pylovo-mv generate --version mv1 --all --jobs 4

  # Status of the MV variants of a version, then delete one
  pylovo-mv list --version mv1
  pylovo-mv delete --version mv1 --variant d0

  # Stored net of one MV grid as pandapower JSON (feed-in case)
  pylovo-mv export --version mv1 --variant d1 --grid 12 --output grid_12.json --case feed_in_pv

  # HV boundary for a coupling with eTraGo: eTraGo 110 kV bus, P and Q per HV/MV substation
  pylovo-mv boundary --version mv1 --variant d1 --output boundary.csv

  # Display geometry (lanes, busbar circles) of variants built before it existed
  pylovo-mv display --version mv1 --variant d1 d2

--set takes MV_* keys of config_generation.yaml (not MV_GRID_GENERATION, MV_VARIANT_ID, MV_VARIANT_COMMENT) and the
constants of pylovo.mv_grid.options.CONSTANTS, e.g. --set MV_RING_SOLVER=sweep local_search_rounds=0.
"""


def _set_value(text: str) -> tuple[str, object]:
    """argparse type of ``--set``: ``KEY=VALUE`` parsed and coerced (errors become usage errors)."""
    try:
        return parse_assignment(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_region(parser: argparse.ArgumentParser) -> None:
    region = parser.add_mutually_exclusive_group(required=True)
    region.add_argument("--plz", type=int, nargs="+", help="PLZ with LV grids of the version")
    region.add_argument("--ags", type=int, nargs="+", help="municipality codes (AGS); their PLZ are used")
    region.add_argument("--all", action="store_true", help="every PLZ with LV grids of the version")


def build_parser() -> argparse.ArgumentParser:
    """The ``pylovo-mv`` argument parser."""
    parser = argparse.ArgumentParser(prog="pylovo-mv", description="Generate and manage MV grids on stored LV "
                                     "versions of pylovo", epilog=EPILOG,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="run the MV stage for one MV variant of an LV version")
    gen.add_argument("--version", required=True, help="LV version (VERSION_ID of a generated version)")
    _add_region(gen)
    gen.add_argument("--variant", help="MV variant id (default MV_VARIANT_ID)")
    gen.add_argument("--comment", help="comment of a new MV variant (default MV_VARIANT_COMMENT)")
    gen.add_argument("--set", dest="assignments", type=_set_value, action="extend", nargs="+", default=[],
                     metavar="KEY=VALUE", help="MV parameter (MV_* key or constant name) of this variant")
    parallelism = gen.add_mutually_exclusive_group()
    parallelism.add_argument("--parallel", dest="parallel", action="store_true", default=None,
                             help="plan the districts in worker processes (default PARALLEL)")
    parallelism.add_argument("--no-parallel", dest="parallel", action="store_false",
                             help="plan the districts one after the other in this process")
    gen.add_argument("--jobs", type=int, help="district workers (default min(4, N_JOBS), capped by free memory)")
    gen.add_argument("--replace", action="store_true", help="delete the variant first and build it again")

    couple = sub.add_parser("couple", help="LV design-point check and merged MV+LV power flows of a done variant")
    couple.add_argument("--version", required=True)
    couple.add_argument("--variant", required=True)
    couple.add_argument("--grid", dest="grids", type=int, nargs="+", help="MV grid ids (default: all done grids)")
    couple.add_argument("--jobs", type=int, default=2, help="coupling workers (default 2)")
    couple.add_argument("--no-merged", dest="merged", action="store_false",
                        help="only the design-point check, no merged MV+LV flows")

    delete = sub.add_parser("delete", help="delete MV variants of a version (all without --variant)")
    delete.add_argument("--version", required=True)
    delete.add_argument("--variant", dest="variants", nargs="+", help="MV variant id(s)")

    listing = sub.add_parser("list", help="list the MV variants")
    listing.add_argument("--version", help="only this LV version")

    graph = sub.add_parser("graph", help="build or reuse the MV routing graph of a region")
    graph.add_argument("--version", required=True)
    _add_region(graph)
    graph.add_argument("--qa", action="store_true", help="print the QA tables of the graph")

    export = sub.add_parser("export", help="write the stored pandapower net of one MV grid to JSON")
    export.add_argument("--version", required=True)
    export.add_argument("--variant", required=True)
    export.add_argument("--grid", type=int, required=True, help="MV grid id")
    export.add_argument("--output", type=Path, required=True, help="JSON file to write")
    export.add_argument("--case", choices=CASES, default="load")
    export.add_argument("--coupled", action="store_true", help="the merged MV+LV net with its LV grids")

    boundary = sub.add_parser("boundary", help="write the HV boundary (eTraGo 110 kV bus, P, Q) of a variant to CSV")
    boundary.add_argument("--version", required=True)
    boundary.add_argument("--variant", required=True)
    boundary.add_argument("--output", type=Path, required=True, help="CSV file to write")

    display = sub.add_parser("display", help="store the display geometry (lanes, busbar circles) of variants again")
    display.add_argument("--version", required=True)
    display.add_argument("--variant", dest="variants", nargs="+", required=True, help="MV variant id(s)")
    return parser


def _database_client():
    from pylovo.database.database_client import DatabaseClient

    return DatabaseClient()


def _plz_of(args: argparse.Namespace) -> list[int] | None:
    """The PLZ of ``--plz``/``--ags``/``--all`` (None for ``--all``)."""
    if args.all:
        return None
    if args.plz:
        return sorted({int(p) for p in args.plz})
    from pylovo.data_import.region_resolver import resolve_regions

    with _database_client() as dbc:
        plz_list, _ = resolve_regions(dbc, ags=[int(a) for a in args.ags])
    return plz_list


def _parameters(args: argparse.Namespace):
    from pylovo.mv_grid.parameters import MvParameters

    params = MvParameters.from_config()
    changes = {}
    if args.variant is not None:
        changes["variant_id"] = args.variant
    if args.comment is not None:
        changes["comment"] = args.comment
    if changes:
        params = params.replace(**changes)
    if args.assignments:
        params = params.replace_raw(dict(args.assignments))
        params.check_types()
    return params


def _generate(args: argparse.Namespace) -> int:
    from pylovo.config_loader import N_JOBS, PARALLEL
    from pylovo.mv_grid import stage

    params = _parameters(args)
    plz_list = _plz_of(args)
    parallel = PARALLEL if args.parallel is None else args.parallel
    jobs = args.jobs if args.jobs is not None else min(stage.DEFAULT_MAX_JOBS, N_JOBS)
    summary = stage.run_mv_stage(args.version, plz_list, params, parallel=parallel, jobs=jobs, replace=args.replace)
    print(summary.describe())
    return 0 if summary.status == "done" else 1


def _couple(args: argparse.Namespace) -> int:
    from pylovo.mv_grid import coupling

    summary = coupling.run_coupling(args.version, args.variant, args.grids, jobs=args.jobs, merged=args.merged)
    print(summary.describe())
    return 0 if summary.status == "done" else 1


def _delete(args: argparse.Namespace) -> int:
    with _database_client() as dbc:
        count = dbc.delete_variants(args.version, args.variants)
    which = ", ".join(args.variants) if args.variants else "all"
    print(f"✓ Deleted {count} MV variant{'s' if count != 1 else ''} of version {args.version} ({which})")
    return 0


def _list(args: argparse.Namespace) -> int:
    with _database_client() as dbc:
        df = dbc.list_variants(args.version)
    if df.empty:
        print("✓ No MV variants" + (f" of version {args.version}" if args.version else ""))
        return 0
    for row in df.to_dict("records"):
        stale = row.get("stale")
        ok = row["status"] == "done" and stale is not True
        km = row.get("circuit_km")
        km_text = f"{float(km):,.0f} km" if km is not None and km == km else "- km"
        stale_text = {True: ", stale", None: ", stale unknown"}.get(stale, "")
        electrical_violations = row.get("n_electrical_violations", 0)
        violation_text = f"{electrical_violations} violation{'s' if electrical_violations != 1 else ''}"
        generation_mode = "generation included" if bool(row.get("include_generation", True)) else "load-only"
        print(f"{'✓' if ok else '✗'} {row['version_id']}/{row['mv_variant_id']}: generation {row['status']}, "
              f"{row['n_plz']} PLZ, {row['n_grids']} grids ({row['n_done']} done, {row['n_failed']} failed), "
              f"{generation_mode}; electrical: {row.get('n_electrical_pass', 0)} pass, "
              f"{violation_text}, "
              f"{row.get('n_electrical_not_checked', 0)} not checked, "
              f"{km_text}{stale_text} - {row.get('mv_variant_comment') or ''}")
    return 0


def _graph(args: argparse.Namespace) -> int:
    from pylovo.mv_grid.parameters import MvParameters

    params = MvParameters.from_config()
    plz_list = _plz_of(args)
    with _database_client() as dbc:
        plz = dbc.generated_plz(args.version, plz_list)
        if not plz:
            print(f"✗ none of the requested PLZ has LV grids in version {args.version}")
            return 1
        region = dbc.planning_region(plz)
        dbc.commit_changes()
        graph_id = dbc.ensure_graph(region, params)
        print(f"✓ MV routing graph {graph_id} for {len(plz)} PLZ of version {args.version}")
        if args.qa:
            for name, table in dbc.graph_qa(graph_id).items():
                print(f"✓ {name}")
                for line in table.to_string(index=False).splitlines():
                    print(f"✓   {line}")
    return 0


def _export(args: argparse.Namespace) -> int:
    if args.coupled:
        from pylovo.mv_grid import coupling

        path = coupling.export_coupled_net(args.version, args.variant, args.grid, args.output, case=args.case)
    else:
        from pylovo.mv_grid import stage

        path = stage.export_net(args.version, args.variant, args.grid, args.output, case=args.case)
    print(f"✓ Wrote MV grid {args.grid} ({args.case}) to {path}")
    return 0


def _boundary(args: argparse.Namespace) -> int:
    with _database_client() as dbc:
        table = dbc.hv_boundary(args.version, args.variant)
        dbc.rollback_changes()
    if table.empty:
        print(f"✗ MV variant {args.variant} of version {args.version} has no done grids")
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    table.assign(flags=table["flags"].map(lambda f: ";".join(f or []))).to_csv(args.output, index=False)
    linked = int(table["etrago_bus_id"].notna().sum())
    print(f"✓ Wrote the HV boundary of {len(table)} substations ({linked} with an eTraGo 110 kV bus, "
          f"{table['p_mw'].sum():.1f} MW) to {args.output}")
    return 0


def _display(args: argparse.Namespace) -> int:
    import logging

    from pylovo.mv_grid import stage

    failed = 0
    with _database_client() as dbc:
        dbc.ensure_grid_persistence_schema()
        for variant in args.variants:
            lanes = stage.display_geometry(dbc, args.version, variant, logging.getLogger(__name__))
            if not lanes:
                print(f"✗ MV variant {variant} of version {args.version}: no display geometry (no lines, or an error)")
                failed += 1
            else:
                print(f"✓ MV variant {variant} of version {args.version}: {lanes} lines with display geometry")
    return 1 if failed else 0


_COMMANDS = {"generate": _generate, "couple": _couple, "delete": _delete, "list": _list, "graph": _graph,
             "export": _export, "boundary": _boundary, "display": _display}


def main(argv: list[str] | None = None) -> None:
    """Entry point of ``pylovo-mv``; returns when the command is done, else exits with 1 (2: usage error)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "generate" and args.jobs is not None and args.jobs < 1:
        parser.error("--jobs must be at least 1")
    try:
        code = _COMMANDS[args.command](args)
    except KeyboardInterrupt:
        print("✗ Interrupted")
        code = 1
    except (ValueError, LookupError, RuntimeError) as exc:
        print(f"✗ {exc}")
        code = 1
    except Exception as exc:
        print(f"✗ {type(exc).__name__}: {exc}")
        traceback.print_exc()
        code = 1
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
