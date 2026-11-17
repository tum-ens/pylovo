"""Histograms of the eight structural metrics for the grids of one example area (synthetic grids only).

Needs no database and no internet: :class:`pylovo.examples.TutorialData` reads version 1 of the semi-urban example
area of the tutorials (PLZ 85256) from ``data/examples``, and :func:`pylovo.analysis.comparison.grid_metrics`
computes the metrics from its cables and stations. Run from the repository root::

    uv run --extra plots python docs/scripts/plot_synthetic_metrics.py

Output: ``docs/source/images/analysis/synthetic_comparison_metrics.png``. The script also prints the medians of the
metrics, which ``docs/source/user_guide/analysing_grids.rst`` quotes.
"""
from __future__ import annotations

import _figure_style as st
import numpy as np

from pylovo.analysis import comparison
from pylovo.examples import TutorialData
from pylovo.plotting import comparison as comparison_plots

PLZ = 85256
VERSION_ID = "1"
MIN_BUILDINGS = 5    # smaller grids are left out, as in the comparison notebook


def main() -> None:
    data = TutorialData(PLZ, VERSION_ID, source="examples")
    layers = data.layers
    table = comparison.grid_metrics(layers["lines"], layers["grids"], comparison.cable_resistances(data.parameters))
    metrics = table[table["building_count"] >= MIN_BUILDINGS].reset_index(drop=True)
    print(f"{len(metrics)} of {len(table)} grids have at least {MIN_BUILDINGS} buildings")
    for metric in comparison.METRICS:
        values = comparison.metric_values(metrics, metric) * metric.factor
        print(f"{metric.key}: median {np.median(values):.{metric.decimals}f} {metric.unit}")
    name = f"version {VERSION_ID}"
    fig = comparison_plots.plot_histograms({name: metrics}, {name: comparison_plots.group_colors([name], name)[name]},
                                           title=f"Synthetic LV grids · PLZ {PLZ} · version {VERSION_ID} · "
                                                 f"{len(metrics)} grids")
    st.save(fig, "analysis", "synthetic_comparison_metrics", dpi=110)


if __name__ == "__main__":
    main()
