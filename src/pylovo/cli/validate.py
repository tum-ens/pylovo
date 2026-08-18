"""
Validation and metrics operations for pylovo grids.
"""
import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd

from pylovo.analysis.synthetic_metric_export import process_synthetic_grids
from pylovo.database.database_client import DatabaseClient
from validations.grid_comparison.audit import write_input_audit
from validations.grid_comparison.common import (
    COMPARISON_SCOPES,
    metric_output_suffixes,
    validation_grid_data_path,
    validation_grid_split_subdir,
)
from validations.grid_comparison.real_swf_metric_export import (
    process_real_grids,
    resolve_real_grid_data_path,
)


def run_comparison(
    plz: int | None = None,
    output_dir: str | None = None,
    output_suffix: str | None = None,
    with_service_lines: bool = False,
    scope: str = "both",
    split_subdir: str | None = None,
) -> None:
    """Run the grid comparison workflow directly from the validation CLI."""
    if scope not in COMPARISON_SCOPES:
        raise ValueError(f"scope must be one of {sorted(COMPARISON_SCOPES)}, got {scope!r}.")

    effective_plz = plz if plz is not None else 91301
    effective_output_dir = Path(output_dir) if output_dir is not None else Path("validations/metrics")
    effective_split_subdir = validation_grid_split_subdir(split_subdir)
    synthetic_output_suffix, real_output_suffix, audit_output_suffix = metric_output_suffixes(
        output_suffix,
        effective_split_subdir,
    )

    synthetic_df = pd.DataFrame()
    real_df = pd.DataFrame()

    with DatabaseClient() as dbc:
        if scope in {"both", "synthetic"}:
            synthetic_df = process_synthetic_grids(
                dbc,
                effective_plz,
                effective_output_dir,
                output_suffix=synthetic_output_suffix,
                with_service_lines=with_service_lines,
            )

        if scope in {"both", "real"}:
            configured_grid_data_path = validation_grid_data_path()
            grid_data_path = resolve_real_grid_data_path(
                configured_grid_data_path,
                split_subdir=effective_split_subdir,
            )
            if grid_data_path.exists():
                real_df = process_real_grids(
                    dbc,
                    grid_data_path,
                    effective_plz,
                    effective_output_dir,
                    output_suffix=real_output_suffix,
                    with_service_lines=with_service_lines,
                    split_subdir=effective_split_subdir,
                )
            else:
                print(f"GRID_DATA_PATH not found or invalid: {configured_grid_data_path} (resolved to {grid_data_path})")

        if scope == "both":
            write_input_audit(synthetic_df, real_df, effective_output_dir, output_suffix=audit_output_suffix)


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
        default=None,
        help="Optional suffix inserted before each output CSV extension. Defaults to VERSION_COMMENT for synthetic metrics and GRID_SPLIT_SUBDIR for real metrics.",
    )
    compare_parser.add_argument(
        "--which",
        choices=["both", "synthetic", "real"],
        default="both",
        help="Select which metrics to create. Defaults to both.",
    )
    compare_parser.add_argument(
        "--grid-split-subdir",
        default=None,
        help="Override GRID_SPLIT_SUBDIR from the project .env for real-grid metrics.",
    )
    compare_parser.add_argument(
        "--with-service-lines",
        action="store_true",
        help="Include terminal house/consumer service connections in line length, resistance, and distance metrics.",
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
                with_service_lines=args.with_service_lines,
                scope=args.which,
                split_subdir=args.grid_split_subdir,
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

