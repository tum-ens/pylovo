"""Generate synthetic LV grids for postcodes (PLZ) or municipalities (AGS) (``pylovo-generate``).

- One PLZ: :func:`create_grid_single_plz` (sequential, with building import in file mode).
- Several PLZ: :func:`create_grid_multiple_plz` (in parallel unless ``PARALLEL: False`` or ``--no-parallel``).
- One or more AGS: :func:`create_grids_for_ags` generates all PLZ of the municipalities.

With ``USE_INFDB=False`` the building shapefiles are imported first (see
:mod:`pylovo.data_import.import_buildings`). ``ANALYZE_GRIDS`` in ``config_generation.yaml``
controls whether the basic analysis runs after generation.
"""
import argparse
import time

import pandas as pd

import pylovo.database.database_client as dbc
from pylovo.config_loader import ANALYZE_GRIDS, PARALLEL, USE_INFDB
from pylovo.data_import.import_buildings import import_buildings_for_multiple_plz, import_buildings_for_single_plz
from pylovo.data_import.region_resolver import resolve_regions
from pylovo.grid_generator import GridGenerator


def create_grid_single_plz(plz: int):
    """Generate the grids of one PLZ.

    Args:
        plz: Postal code to generate grids for.
    """
    print(f"Creating grid for single PLZ: {plz}")
    gg = GridGenerator(plz=plz)

    if not USE_INFDB:
        import_buildings_for_single_plz(gg)

    gg.generate_grid_for_single_plz(plz=plz, analyze_grids=ANALYZE_GRIDS)


def _generate_plz_list(plz_list: list[int], parallel: bool) -> None:
    """Generate the grids of several PLZ with one grid generator."""
    gg = GridGenerator()
    df_plz = pd.DataFrame({"plz": plz_list})
    gg.generate_grid_for_multiple_plz(df_plz=df_plz, analyze_grids=ANALYZE_GRIDS, parallel=parallel)


def create_grid_multiple_plz(plz_list: list, parallel: bool = True):
    """Generate the grids of several PLZ.

    Args:
        plz_list: Postal codes to generate grids for.
        parallel: Generate the PLZ in parallel worker processes.
    """
    print(f"Creating grids for multiple PLZ: {plz_list}")
    plz_list = [int(p) for p in plz_list]

    if not USE_INFDB:
        with dbc.DatabaseClient() as dbc_client:
            _, df_plz_ags = resolve_regions(dbc_client, plz=plz_list)
            import_buildings_for_multiple_plz(df_plz_ags, dbc_client=dbc_client)

    _generate_plz_list(plz_list, parallel)


def create_grids_for_ags(ags_list: list, parallel: bool = True):
    """Generate the grids of all PLZ that belong to the given municipalities.

    Args:
        ags_list: Municipality codes (Amtlicher Gemeindeschlüssel, AGS).
        parallel: Generate the PLZ in parallel worker processes.

    Raises:
        ValueError: If an AGS is not in the municipal register.
    """
    print(f"Creating grids for AGS: {ags_list}")
    with dbc.DatabaseClient() as dbc_client:
        plz_list, df_plz_ags = resolve_regions(dbc_client, ags=[int(a) for a in ags_list])
        if not USE_INFDB:
            import_buildings_for_multiple_plz(df_plz_ags, dbc_client=dbc_client)

    _generate_plz_list(plz_list, parallel)


def main():
    """Entry point of ``pylovo-generate``."""
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

  # Disable parallel processing (useful for debugging); PARALLEL in the config sets the default
  pylovo-generate --plz 80803 80802 --no-parallel

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
    parallelism = parser.add_mutually_exclusive_group()
    parallelism.add_argument('--parallel', dest='parallel', action='store_true', default=None,
                             help='Generate several regions in parallel worker processes')
    parallelism.add_argument('--no-parallel', dest='parallel', action='store_false',
                             help='Generate several regions one after the other')

    args = parser.parse_args()
    # PARALLEL in config_generation.yaml is the default; the flags override it
    parallel = PARALLEL if args.parallel is None else args.parallel

    print("Pylovo Grid Creation Script")
    print("=" * 60)
    start_time = time.time()

    try:
        if args.plz:
            if len(args.plz) == 1:
                create_grid_single_plz(args.plz[0])
            else:
                create_grid_multiple_plz(args.plz, parallel=parallel)
        else:
            create_grids_for_ags(args.ags, parallel=parallel)
    except Exception as e:
        print("=" * 60)
        print(f"Error occurred during grid creation: {str(e)}")
        raise
    finally:
        minutes, seconds = divmod(time.time() - start_time, 60)
        print(f"Elapsed Time: {int(minutes)} minutes and {seconds:.2f} seconds")


if __name__ == "__main__":
    main()
