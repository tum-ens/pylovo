# Grid Comparison Files

This directory contains the notebook-facing calibration workflow for comparing exported synthetic and real grid metrics.

## Analysis Runner

The active comparison workflow lives in `src/pylovo/analysis/comparison_helpers.py` and is invoked through the validation CLI in `src/pylovo/cli/validate.py`.
- **Input (Real)**: radialized regular LV-load subnets from the configured `GRID_DATA_PATH` (`radialized/LV_*__radialized__regular__lvload.xlsx` when available).
- **Input (Synthetic)**: grids for PLZ `91301` from the `pylovo` database.
- **Output (Synthetic, canonical)**: `validations/metrics/synthetic_grid_metrics.csv`
- **Output (Real)**: `validations/metrics/real_grid_metrics.csv`

## Usage

```bash
uv run pylovo-validate compare-grids

# Optional overrides
uv run pylovo-validate compare-grids --plz 91301 --output-dir validations/metrics
```

For the new DSO preprocessing layout, set `GRID_DATA_PATH` to the parent directory containing `logical/`, `radialized/`, and `split_manifest.csv`. After regenerating the CSVs, open `validations/grid_comparison/lightweight_analysis.ipynb` to inspect:
- full-data Wasserstein scores at the top
- status-stratified diagnostics by `power_flow_status`
- feeder count together with `buildings_per_feeder`
- box, violin, pairplot, and diagonal KDE comparison views

Superseded validation files are archived under `validation/old`.