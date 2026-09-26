"""Analyse generated grids (``pylovo-analyze``).

The PLZ analysis stores transformer, load, bus and cable statistics per PLZ; the per-grid analysis
stores the clustering parameters of every grid and needs the PLZ analysis first.
"""
import argparse
import sys
import traceback

from pylovo.analysis.parameter_calculation import ParameterCalculator


def analyze_plz(plz: int):
    """Calculate and store the PLZ-level parameters (skipped if they already exist)."""
    pc = ParameterCalculator()
    pc.analyze_parameters_for_plz(plz)
    print(f"✓ Calculated parameters for PLZ {plz}")


def analyze_grid(plz: int):
    """Calculate and store the clustering parameters of every grid of a PLZ (after :func:`analyze_plz`)."""
    pc = ParameterCalculator()
    pc.analyze_grid_parameters_for_plz(plz)
    print(f"✓ Calculated parameters per grid for PLZ {plz}")


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
        """,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )

    parser.add_argument(
        "--plz",
        type=int,
        required=True,
        help="Postal code to analyze"
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

    try:
        if args.all:
            analyze_plz(args.plz)
            analyze_grid(args.plz)
        elif args.per_grid:
            analyze_grid(args.plz)
        else:
            analyze_plz(args.plz)

    except Exception as e:
        print(f"✗ Error: {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()

