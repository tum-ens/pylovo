# Grid Comparison Files

This directory contains the notebook-facing calibration workflow for comparing exported synthetic and real grid metrics.

The methodological assumptions behind the benchmark metrics are documented in `validation_assumption.md`.

## Analysis Runner

The active comparison workflow lives in `src/pylovo/analysis/comparison_helpers.py` and is invoked through the validation CLI in `src/pylovo/cli/validate.py`.
- **Input (Real)**: radialized regular LV-load subnets from the configured `GRID_DATA_PATH` (`radialized/LV_*__radialized__regular__lvload.xlsx` when available).
- **Input (Synthetic)**: grids for PLZ `91301` from the `pylovo` database.
- **Output (Synthetic, canonical)**: `validations/metrics/synthetic_grid_metrics.csv`
- **Output (Real)**: `validations/metrics/real_grid_metrics.csv`
- **Output (Diagnostics)**: `validations/metrics/comparison_input_audit.csv`

The two metrics CSVs are intentionally narrow. They contain only grid identifiers/status columns plus the active benchmark metrics: `feeder_lines`, `graph_length`, `avg_trafo_distance`, `max_trafo_distance`, `transformer_mva`, and `graph_resistance`. These metrics use the feeder/backbone definition by default: terminal house/consumer service connections are excluded from line length, resistance, and transformer-distance calculations. Development diagnostics such as feeder-count variants, topology counts, and source-data quality flags are written to `comparison_input_audit.csv` instead.

In `grid_comparison_notebook.ipynb`, set `SYNTHETIC_METRICS_FILE` and `REAL_METRICS_FILE` in the configuration cell to compare suffixed metric exports, for example a `dso20` run, without changing the helper code.

## Usage

```bash
uv run pylovo-validate compare-grids

# Optional overrides
uv run pylovo-validate compare-grids --plz 91301 --output-dir validations/metrics

# Diagnostic export using the old service-line-inclusive distance/length/resistance metrics
uv run pylovo-validate compare-grids --with-service-lines --output-suffix with_service_lines
```

For the new DSO preprocessing layout, set `GRID_DATA_PATH` to the parent directory containing `logical/`, `radialized/`, and `split_manifest.csv`. After regenerating the CSVs, open `validations/grid_comparison/lightweight_analysis.ipynb` to inspect:
- full-data Wasserstein scores at the top
- status-stratified diagnostics by `power_flow_status`
- feeder count together with `buildings_per_feeder`
- box, violin, pairplot, and diagonal KDE comparison views

Superseded validation files are archived under `validation/old`.