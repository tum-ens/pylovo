"""Generate synthetic LV grids for postcodes (PLZ) or municipalities (AGS) (``pylovo-generate``).

- One PLZ: :func:`create_grid_single_plz` (sequential, with building import in file mode).
- Several PLZ: :func:`create_grid_multiple_plz` (in parallel unless ``PARALLEL: False`` or ``--no-parallel``).
- One or more AGS: :func:`create_grids_for_ags` generates all PLZ of the municipalities.

With ``USE_INFDB=False`` the building shapefiles are imported first (see
:mod:`pylovo.data_import.import_buildings`). ``ANALYZE_GRIDS`` in ``config_generation_lv.yaml``
controls whether the basic analysis runs after generation.

With ``MV_GRID_GENERATION: True`` (or ``--mv``) the MV stage (:func:`pylovo.mv_grid.stage.run_mv_stage`)
runs once after all PLZ of the call, for the configured MV variant of ``VERSION_ID``. The LV grids are
committed per PLZ first, so an MV failure never undoes them; an MV stage that does not end ``done`` exits
with code 1. The MV stage runs for the PLZ that have LV grids. The MV modules are imported only when the stage runs.

Exit codes: 0 if every PLZ has LV grids (generated now or before), 1 after an error (including an MV stage that
does not end ``done``), 2 for wrong arguments and :data:`EXIT_PLZ_NOT_GENERATED` if the run finished but some PLZ
failed or have no grids (named in one summary line; the reasons are in the log).
"""
import sys
import time
from typing import TYPE_CHECKING

import pandas as pd

import pylovo.config_loader as config_loader
import pylovo.database.database_client as dbc
from pylovo.cli import parsers
from pylovo.config_loader import ANALYZE_GRIDS, MV_GRID_GENERATION, N_JOBS, PARALLEL, USE_INFDB, VERSION_ID
from pylovo.data_import.import_buildings import import_buildings_for_multiple_plz, import_buildings_for_single_plz
from pylovo.data_import.region_resolver import resolve_regions
from pylovo.grid_generator import GenerationOutcome, GridGenerator

#: Exit code of a run that finished but left PLZ failed or without grids (1 stays "crashed", 2 "wrong arguments",
#: 3 is the code of ``pylovo-analyze --coverage``). ``pylovo-api`` counts such a generate job as succeeded.
EXIT_PLZ_NOT_GENERATED = 4
_NOT_GENERATED = (GenerationOutcome.FAILED, GenerationOutcome.EMPTY)

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import MvParameters


def create_grid_single_plz(plz: int) -> dict[int, GenerationOutcome]:
    """Generate the grids of one PLZ.

    Args:
        plz: Postal code to generate grids for.

    Returns:
        ``{plz: outcome}`` (the PLZ of the call, with what became of it).
    """
    print(f"Creating grid for single PLZ: {plz}")
    gg = GridGenerator(plz=plz)

    if not USE_INFDB:
        import_buildings_for_single_plz(gg)

    outcome = gg.generate_grid_for_single_plz(plz=plz, analyze_grids=ANALYZE_GRIDS)
    return {int(plz): outcome}


def _generate_plz_list(plz_list: list[int], parallel: bool) -> dict[int, GenerationOutcome]:
    """Generate the grids of several PLZ with one grid generator and return the outcome of every PLZ."""
    gg = GridGenerator()
    df_plz = pd.DataFrame({"plz": plz_list})
    return gg.generate_grid_for_multiple_plz(df_plz=df_plz, analyze_grids=ANALYZE_GRIDS, parallel=parallel)


def create_grid_multiple_plz(plz_list: list, parallel: bool = True) -> dict[int, GenerationOutcome]:
    """Generate the grids of several PLZ.

    Args:
        plz_list: Postal codes to generate grids for.
        parallel: Generate the PLZ in parallel worker processes.

    Returns:
        The outcome of every PLZ of the call.
    """
    print(f"Creating grids for multiple PLZ: {plz_list}")
    plz_list = [int(p) for p in plz_list]

    if not USE_INFDB:
        with dbc.DatabaseClient() as dbc_client:
            _, df_plz_ags = resolve_regions(dbc_client, plz=plz_list)
            import_buildings_for_multiple_plz(df_plz_ags, dbc_client=dbc_client)

    return _generate_plz_list(plz_list, parallel)


def create_grids_for_ags(ags_list: list, parallel: bool = True) -> dict[int, GenerationOutcome]:
    """Generate the grids of all PLZ that belong to the given municipalities.

    Args:
        ags_list: Municipality codes (Amtlicher Gemeindeschlüssel, AGS).
        parallel: Generate the PLZ in parallel worker processes.

    Returns:
        The outcome of every PLZ of the municipalities.

    Raises:
        ValueError: If an AGS is not in the municipal register.
    """
    print(f"Creating grids for AGS: {ags_list}")
    with dbc.DatabaseClient() as dbc_client:
        plz_list, df_plz_ags = resolve_regions(dbc_client, ags=[int(a) for a in ags_list])
        if not USE_INFDB:
            import_buildings_for_multiple_plz(df_plz_ags, dbc_client=dbc_client)

    return _generate_plz_list(plz_list, parallel)


def run_mv_stage_after_lv(plz_list: list[int], parallel: bool, params: "MvParameters") -> None:
    """Run the MV stage for the configured MV variant of ``VERSION_ID`` on the PLZ of this call.

    Args:
        plz_list: The PLZ of this call.
        parallel: Plan the districts in worker processes.
        params: The configured MV variant (``MvParameters.from_config``, resolved before the LV generation).

    Raises:
        SystemExit: With code 1 if the MV stage does not end ``done`` (the LV grids stay committed).
        Exception: Any error of the MV stage, after printing it.
    """
    try:
        from pylovo.mv_grid.stage import run_mv_stage   # lazy: no MV import cost for LV-only runs

        summary = run_mv_stage(VERSION_ID, plz_list, params, parallel=parallel, jobs=min(4, N_JOBS))
        print(summary.describe())
        if summary.status != "done":
            raise SystemExit(1)                           # LV is committed; an incomplete MV stage is an error
    except SystemExit:
        raise
    except Exception as exc:
        print(f"MV stage failed: {exc}")
        raise


def main(argv: list[str] | None = None):
    """Entry point of ``pylovo-generate``.

    Args:
        argv: Command-line arguments (default ``sys.argv[1:]``).
    """
    parser = parsers.generate_parser()

    args = parser.parse_args(argv)
    # PARALLEL in config_generation_lv.yaml is the default; the flags override it
    parallel = PARALLEL if args.parallel is None else args.parallel
    run_mv = MV_GRID_GENERATION if args.mv is None else args.mv
    mv_params = None
    if run_mv:   # before the LV grids, not after them
        from pylovo.mv_grid.parameters import MvParameters, MvStageError, require_local_source_data

        try:
            require_local_source_data(config_loader)
            mv_params = MvParameters.from_config()   # resolves the line and transformer types (pandapower)
        except (MvStageError, ValueError) as exc:
            print(f"MV stage cannot run: {exc}")
            raise SystemExit(1) from None

    print("Pylovo Grid Creation Script")
    print("=" * 60)
    start_time = time.time()

    try:
        if args.plz:
            if len(args.plz) == 1:
                outcomes = create_grid_single_plz(args.plz[0])
            else:
                outcomes = create_grid_multiple_plz(args.plz, parallel=parallel)
        else:
            outcomes = create_grids_for_ags(args.ags, parallel=parallel)
    except Exception as e:
        print("=" * 60)
        print(f"Error occurred during grid creation: {str(e)}")
        raise
    finally:
        minutes, seconds = divmod(time.time() - start_time, 60)
        print(f"Elapsed Time: {int(minutes)} minutes and {seconds:.2f} seconds")

    not_generated = {plz: outcome for plz, outcome in outcomes.items() if outcome in _NOT_GENERATED}
    if not_generated:
        listed = ", ".join(f"{plz} ({outcome.value})" for plz, outcome in sorted(not_generated.items()))
        print(f"✗ {len(not_generated)} of {len(outcomes)} PLZ not generated: {listed}")
    if run_mv:
        with_grids = sorted(set(outcomes) - set(not_generated))
        if with_grids:
            run_mv_stage_after_lv(with_grids, parallel, mv_params)
        else:
            print("MV stage skipped: no PLZ of this run has LV grids")
    if not_generated:
        sys.exit(EXIT_PLZ_NOT_GENERATED)


if __name__ == "__main__":
    main()
