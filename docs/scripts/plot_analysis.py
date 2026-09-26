"""Charts of stored generation results for the documentation.

Requires a pylovo database with the grid versions created by
``docs/scripts/make_demo_versions.py`` (``1``, ``docs_bf``, ``docs_km``) for the demo
region PLZ 85653. The script only reads from the database. Run from the repository
root::

    uv run python docs/scripts/plot_analysis.py

Output: ``docs/source/images/analysis/*.png``.
"""
from __future__ import annotations

import json

import _figure_style as st
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.lines import Line2D

PLZ = 85653
VERSIONS = ["1", "docs_bf", "docs_km"]
SUBDIR = "analysis"


def feeder_voltage_drops(engine) -> None:
    """Dumbbell chart: ampacity-only vs. selected end-to-end feeder drop per grid."""
    df = pd.read_sql(
        """SELECT gr.version_id, gr.kcid, gr.bcid, gr.power_flow_status,
                  gr.ampacity_max_feeder_voltage_drop_percent AS ampacity,
                  gr.selected_max_feeder_voltage_drop_percent AS selected,
                  gr.feeder_voltage_drop_limit_met AS met,
                  v.generation_parameters
           FROM pylovo.grid_result gr JOIN pylovo.version v USING (version_id)
           WHERE gr.plz = %(plz)s AND gr.version_id = ANY(%(versions)s)
           ORDER BY array_position(%(versions)s, gr.version_id::text), gr.kcid, gr.bcid""",
        engine,
        params={"plz": PLZ, "versions": VERSIONS},
    )
    params = df["generation_parameters"].map(lambda p: p if isinstance(p, dict) else json.loads(p))
    limits = params.map(lambda p: p["cable_dimensioning"]["max_end_to_end_feeder_voltage_drop_percent"]).unique()
    if len(limits) != 1:
        raise ValueError(f"Versions use different feeder limits: {limits}")
    limit = float(limits[0])
    df["label"] = df.apply(lambda r: f"{r.version_id} · kcid {r.kcid} / bcid {r.bcid}", axis=1)

    fig, ax = plt.subplots(figsize=(7.2, 5.4))
    y = np.arange(len(df))[::-1]
    ax.axvline(limit, color=st.INK_SECONDARY, lw=1, zorder=1)
    ax.text(limit, y.max() + 0.9, f" limit {limit:g} %", color=st.INK_SECONDARY, fontsize=7.5, va="bottom")
    for yi, row in zip(y, df.itertuples()):
        ax.plot([row.ampacity, row.selected], [yi, yi], color=st.BASELINE, lw=2, zorder=2, solid_capstyle="round")
        ax.scatter(row.ampacity, yi, s=34, facecolor=st.SURFACE, edgecolor=st.INK_MUTED, lw=1.5, zorder=3)
        ax.scatter(row.selected, yi, s=40, color=st.CATEGORICAL[0], edgecolor=st.SURFACE, lw=1.5, zorder=4)
        notes = []
        if not row.met:
            notes.append("limit not met")
        if row.power_flow_status != "converged":
            notes.append(f"power flow: {row.power_flow_status}")
        if notes:
            ax.text(max(row.ampacity, row.selected) + 0.25, yi, "  ✕ " + ", ".join(notes), va="center",
                    fontsize=7.2, color=st.INK)
    ax.set_yticks(y, df["label"], fontsize=7.5)
    ax.set_xlabel("Maximum end-to-end feeder voltage drop in % (planning envelope, asset-coincident loads)")
    ax.set_xlim(0, max(df["ampacity"].max(), df["selected"].max()) * 1.45)
    ax.set_ylim(-0.8, y.max() + 1.6)
    ax.grid(axis="x", color=st.GRID, lw=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.tick_params(axis="y", length=0)
    handles = [
        Line2D([], [], marker="o", ls="", markerfacecolor=st.SURFACE, markeredgecolor=st.INK_MUTED, markersize=6,
               label="Ampacity-only design"),
        Line2D([], [], marker="o", ls="", color=st.CATEGORICAL[0], markersize=6,
               label="After voltage-driven conductor upsizing"),
    ]
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.45, -0.1), ncol=2, fontsize=7.5)
    ax.set_title(f"Feeder voltage-drop planning per grid, PLZ {PLZ}")
    st.save(fig, SUBDIR, "feeder_voltage_drop_per_grid")


def cable_length_by_type(engine) -> None:
    """Horizontal grouped bars: installed feeder and service cable length per type (version 1)."""
    df = pd.read_sql(
        """SELECT pl.std_type, (pl.service_sizing_basis IS NOT NULL) AS is_service,
                  SUM(pl.length_km * COALESCE(pl.parallel, 1)) AS km
           FROM pylovo.pandapower_line pl JOIN pylovo.grid_result gr USING (grid_result_id)
           WHERE gr.plz = %(plz)s AND gr.version_id = '1'
           GROUP BY 1, 2""",
        engine,
        params={"plz": PLZ},
    )
    table = df.pivot_table(index="std_type", columns="is_service", values="km", fill_value=0.0)
    table = table.rename(columns={False: "Feeder", True: "Service connection"})
    table = table.loc[sorted(table.index, key=lambda t: int(t.split("_")[-1]))]
    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    y = np.arange(len(table))[::-1]
    left = np.zeros(len(table))
    for name, color in (("Feeder", st.CATEGORICAL[0]), ("Service connection", st.CATEGORICAL[1])):
        values = table.get(name, pd.Series(0.0, index=table.index)).to_numpy()
        ax.barh(y, values, left=left, height=0.5, color=color, edgecolor=st.SURFACE, lw=1.0, label=name, zorder=2)
        left += values
    for yi, total in zip(y, left):
        ax.text(total + 0.08, yi, f"{total:.2f} km", va="center", fontsize=7, color=st.INK_SECONDARY)
    ax.set_yticks(y, table.index, fontsize=7.5)
    ax.set_xlabel("Installed cable length in km (parallel cables counted separately)")
    ax.grid(axis="x", color=st.GRID, lw=0.8)
    ax.set_axisbelow(True)
    for spine in ("top", "right", "left"):
        ax.spines[spine].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_xlim(0, table.sum(axis=1).max() * 1.15)
    ax.legend(loc="lower right", fontsize=7.5)
    ax.set_title(f"Cable length by type, PLZ {PLZ}, version 1")
    st.save(fig, SUBDIR, "cable_length_by_type")


def main() -> None:
    st.apply_style()
    engine = st.sql_engine()
    feeder_voltage_drops(engine)
    cable_length_by_type(engine)


if __name__ == "__main__":
    main()
