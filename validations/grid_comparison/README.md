# Grid Comparison Files

This directory contains the notebook-facing calibration workflow for comparing exported synthetic and real grid metrics.

## Analysis Runner

The active comparison workflow lives in `src/pylovo/analysis/comparison_helpers.py` and is invoked through the validation CLI in `src/pylovo/cli/validate.py`.
- **Input (Real)**: `LV_*.json` subnets from the configured `GRID_DATA_PATH`.
- **Input (Synthetic)**: grids for PLZ `91301` from the `pylovo` database.
- **Output (Synthetic, canonical)**: `validation/metrics/synthetic_grid_metrics.csv`
- **Output (Real)**: `validation/metrics/real_grid_metrics.csv`

## Usage

```bash
uv run pylovo-validate compare-grids

# Optional overrides
uv run pylovo-validate compare-grids --plz 91301 --output-dir validation/metrics
```

After regenerating the CSVs, open `validation/grid_comparison/lightweight_analysis.ipynb` to inspect:
- full-data Wasserstein scores at the top
- status-stratified diagnostics by `power_flow_status`
- feeder count together with `buildings_per_feeder`
- box, violin, pairplot, and diagonal KDE comparison views

Superseded validation files are archived under `validation/old`.