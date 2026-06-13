"""
Validation and metrics operations for pylovo grids.
"""
import argparse
from pathlib import Path

from pylovo.analysis.comparison_helpers import run_grid_comparison


def run_comparison(
    plz: int | None = None,
    output_dir: str | None = None,
    output_suffix: str = "",
) -> None:
    """Run the grid comparison workflow directly from the validation CLI."""
    effective_plz = plz if plz is not None else 91301
    effective_output_dir = Path(output_dir) if output_dir is not None else Path("validations/metrics")
    run_grid_comparison(
        plz=effective_plz,
        output_dir=effective_output_dir,
        output_suffix=output_suffix,
    )


def main():
    """Main entry point for validation operations."""
    parser = argparse.ArgumentParser(
        prog="pylovo-validate",
        description="Validation and metrics operations for pylovo grids"
    )

    subparsers = parser.add_subparsers(dest="command", help="Validation operation to perform")

    # Subcommand: compare-grids
    compare_parser = subparsers.add_parser(
        "compare-grids",
        help="Run comparison between Real and Synthetic grids"
    )
    compare_parser.add_argument(
        "--plz",
        type=int,
        default=None,
        help="Postcode area to analyze. Defaults to the comparison module default.",
    )
    compare_parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory where comparison CSV outputs will be written.",
    )
    compare_parser.add_argument(
        "--output-suffix",
        default="",
        help="Optional suffix inserted before each output CSV extension.",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    try:
        if args.command == "compare-grids":
            run_comparison(
                plz=args.plz,
                output_dir=args.output_dir,
                output_suffix=args.output_suffix,
            )
        else:
             parser.print_help()
    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)


if __name__ == "__main__":
    main()

