# Grid Comparison Files

This directory contains the notebook-facing calibration workflow for comparing exported synthetic and real grid metrics.

The methodological assumptions behind the benchmark metrics are documented in `validation_assumption.md`.

## Analysis Runner

The active comparison workflow lives in `src/pylovo/analysis/comparison_helpers.py` and is invoked through the validation CLI in `src/pylovo/cli/validate.py`.

## Usage

```bash
uv run pylovo-validate compare-grids

# Optional overrides
uv run pylovo-validate compare-grids --plz 91301 --output-dir validations/metrics

# Diagnostic export using the old service-line-inclusive distance/length/resistance metrics
uv run pylovo-validate compare-grids --with-service-lines --output-suffix with_service_lines
```