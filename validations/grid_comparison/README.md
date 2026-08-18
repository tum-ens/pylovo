# Grid Comparison

This directory contains the validation workflow for comparing synthetic PyLoVo grid metrics with prepared real-grid metrics.

The methodological assumptions behind the benchmark metrics are documented in `validation_assumption.md`. Real-grid splitting and radialization are handled separately in `validations/grid_preparation/`.

## Modules

- `src/pylovo/cli/validate.py`: coordinates synthetic and real metric CSV generation for the `pylovo-validate compare-grids` command.
- `validations/grid_comparison/real_metric_export.py`: loads prepared real grids and exports real-grid metrics.
- `validations/grid_comparison/scoring.py`: computes Wasserstein summary scores.
- `validations/grid_comparison/common.py`: contains validation `.env` and filename helpers.
- `validations/grid_comparison/audit.py`: writes the combined input audit CSV.
- `validations/grid_comparison/comparison_notebook.py`: notebook helper functions for loading metrics and plotting diagnostics.
- `src/pylovo/analysis/grid_analysis.py`: reusable low-level parameter calculations.
- `src/pylovo/analysis/synthetic_metric_export.py`: exports synthetic metrics from the PyLoVo database.

## Configuration

Real-grid metric generation reads the project-root `.env`:

```text
GRID_DATA_PATH="/home/breveron/data"
GRID_SPLIT_SUBDIR="swf_split_hybrid"
```

Synthetic metric filenames use `VERSION_COMMENT` from `config/config_generation.yaml`; whitespace is replaced by `_`. Real metric filenames use `GRID_SPLIT_SUBDIR`. An explicit `--output-suffix` overrides both.

## Usage

Prepare or refresh the real-grid split first when needed:

```bash
uv run python validations/grid_preparation/legacy/split_to_subgrids.py
```

Generate both metric sets:

```bash
uv run pylovo-validate compare-grids --plz 91301 --output-dir validations/metrics
```

Generate only one side:

```bash
uv run pylovo-validate compare-grids --which synthetic --plz 91301 --output-dir validations/metrics
uv run pylovo-validate compare-grids --which real --plz 91301 --output-dir validations/metrics
```

Diagnostic export using service-line-inclusive distance, length, and resistance metrics:

```bash
uv run pylovo-validate compare-grids --with-service-lines --output-suffix with_service_lines
```
