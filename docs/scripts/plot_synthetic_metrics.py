"""Plot the six comparison metrics for stored synthetic grids only.

Read-only: loads version 1, PLZ 91301 and never invokes the comparison export,
which drops and recreates ``pylovo.grid_parameters``. Run from the repository root::

    uv run --extra plots python docs/scripts/plot_synthetic_metrics.py
"""
from __future__ import annotations

import _figure_style as style
import matplotlib.pyplot as plt
import numpy as np

from pylovo.analysis.grid_analysis import compute_comparison_parameters
from pylovo.analysis.parameter_calculation import ParameterCalculator

VERSION_ID = "1"
PLZ = 91301
MIN_BUSES = 5
METRICS = (
    ("feeder_lines", "Terminal feeder branches", "count"),
    ("graph_length", "Feeder network length", "km"),
    ("avg_trafo_distance", "Mean transformer distance", "km"),
    ("max_trafo_distance", "Maximum transformer distance", "km"),
    ("transformer_mva", "Transformer capacity", "MVA"),
    ("graph_resistance", "Feeder resistance", "Ω"),
)


def main() -> None:
    style.apply_style()
    calculator = ParameterCalculator()
    dbc = calculator.dbc
    try:
        dbc.cur.execute(
            "SELECT kcid, bcid FROM pylovo.grid_result "
            "WHERE version_id = %s AND plz = %s ORDER BY kcid, bcid",
            (VERSION_ID, PLZ),
        )
        identities = dbc.cur.fetchall()
        if not identities:
            raise RuntimeError(f"No synthetic grids for version {VERSION_ID}, PLZ {PLZ}")
        rows = []
        failed = []
        skipped = 0
        for kcid, bcid in identities:
            try:
                net = dbc.read_net_db(PLZ, kcid, bcid, version_id=VERSION_ID)
                if len(net.bus) < MIN_BUSES:
                    skipped += 1
                    continue
                rows.append(compute_comparison_parameters(calculator, net))
            except Exception as exc:
                failed.append((kcid, bcid, str(exc)))
        if failed:
            raise RuntimeError(f"Metric calculation failed for {len(failed)} grids: {failed[:3]}")
        if not rows:
            raise RuntimeError("No eligible synthetic grids")
        fig, axes = plt.subplots(2, 3, figsize=(11, 5.8), constrained_layout=True)
        for ax, (field, title, unit) in zip(axes.flat, METRICS):
            values = np.array([row[field] for row in rows], dtype=float)
            values = values[np.isfinite(values)]
            if not len(values):
                raise RuntimeError(f"No finite values for {field}")
            print(f"{field}: median={np.median(values):.3f}, p10={np.percentile(values, 10):.3f}, p90={np.percentile(values, 90):.3f}")
            ax.hist(values, bins=min(18, max(4, int(np.sqrt(len(values))))),
                    color=style.CATEGORICAL[0], edgecolor=style.SURFACE)
            ax.axvline(np.median(values), color=style.CATEGORICAL[1], lw=1.8,
                       label=f"Median {np.median(values):.2g} {unit}")
            ax.set_title(title)
            ax.set_xlabel(unit)
            ax.set_ylabel("Grids")
            ax.legend(loc="upper right", fontsize=7)
            ax.grid(axis="y", color=style.GRID, lw=0.7)
            ax.set_axisbelow(True)
        fig.suptitle(f"Synthetic LV grids · PLZ {PLZ} · version {VERSION_ID} · n={len(rows)}")
        style.save(fig, "analysis", "synthetic_comparison_metrics")
        print(f"Eligible grids: {len(rows)}; skipped (<{MIN_BUSES} buses): {skipped}")
    finally:
        dbc.close()


if __name__ == "__main__":
    main()
