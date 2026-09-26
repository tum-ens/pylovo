"""Export generated grids as CSV files for QGIS (``pylovo-export``).

Each export writes a line table and a bus table with WKT geometries (see
:mod:`pylovo.plotting.gis_preparation.io_geodata`) for the ``VERSION_ID`` of
``config_generation.yaml``.
"""
import argparse
import os
import sys
from pathlib import Path

import pandas as pd

from pylovo.config_loader import PROJECT_ROOT
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.gis_preparation.io_geodata import get_bus_line_geo_for_network, save_geodata_as_csv


def _output_paths(output_dir: str | Path | None, suffix: str) -> tuple[str, str]:
    """Create the output directory and return the line and bus CSV paths.

    Args:
        output_dir: Target directory; ``<working directory>/QGIS`` if None.
        suffix: File name suffix, e.g. ``single_grid``.
    """
    output_dir = PROJECT_ROOT / "QGIS" if output_dir is None else output_dir
    os.makedirs(output_dir, exist_ok=True)
    return (os.path.join(output_dir, f"lines_{suffix}.csv"),
            os.path.join(output_dir, f"bus_{suffix}.csv"))


def _print_paths(line_datapath: str, bus_datapath: str) -> None:
    print(f"  Lines: {line_datapath}")
    print(f"  Buses: {bus_datapath}")


def export_plz(plz_list: list[int], output_dir: str | None = None):
    """Export all grids of one or several PLZ.

    Args:
        plz_list: Postal codes to export.
        output_dir: Target directory (default ``QGIS/`` in the working directory). The files are
            called ``*_single_grid.csv`` for one PLZ and ``*_multiple_grids.csv`` otherwise.
    """
    suffix = "single_grid" if len(plz_list) == 1 else "multiple_grids"
    line_datapath, bus_datapath = _output_paths(output_dir, suffix)

    df_plz = pd.DataFrame(plz_list, columns=['plz'])
    save_geodata_as_csv(df_plz=df_plz, data_path_lines=line_datapath, data_path_bus=bus_datapath)

    print(f"✓ Exported geodata for PLZ: {plz_list}")
    _print_paths(line_datapath, bus_datapath)


def export_grid(plz: int, kcid: int, bcid: int, output_dir: str | None = None):
    """Export one grid to ``lines_single_grid.csv`` and ``bus_single_grid.csv``.

    Args:
        plz: Postal code of the grid.
        kcid: K-means cluster id.
        bcid: Building cluster id (negative for brownfield transformers).
        output_dir: Target directory (default ``QGIS/`` in the working directory).
    """
    line_datapath, bus_datapath = _output_paths(output_dir, "single_grid")

    with DatabaseClient() as dbc_client:
        net = dbc_client.read_net_db(plz, kcid, bcid)

    line_geo, bus_geo = get_bus_line_geo_for_network(pandapower_net=net, plz=plz)
    line_geo.to_csv(line_datapath)
    bus_geo.to_csv(bus_datapath)

    print(f"✓ Exported geodata for grid PLZ={plz}, kcid={kcid}, bcid={bcid}")
    _print_paths(line_datapath, bus_datapath)


def main():
    """Entry point of ``pylovo-export``."""
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

    args = parser.parse_args()

    if args.grid:
        # 0 and negative ids are valid, so only a missing value is an error.
        if args.kcid is None or args.bcid is None:
            parser.error("--grid mode requires --kcid and --bcid")
        if len(args.plz) != 1:
            parser.error("--grid mode requires exactly one PLZ")

    try:
        if args.grid:
            export_grid(args.plz[0], args.kcid, args.bcid, args.output)
        else:
            export_plz(args.plz, args.output)
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
