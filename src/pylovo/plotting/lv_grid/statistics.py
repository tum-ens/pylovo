"""Charts of the LV grids of one or many postcode areas.

- :func:`plot_settlement_type`: how the two density metrics of a postcode area give its settlement type.
- :func:`plot_rating_counts`: the number of grids per station rating.
- :func:`plot_cable_inventory`: installed cable length per type, feeders and house connections.
- :func:`plot_voltage_drop_check`: the design voltage drop of every grid against the limit, before and after the
  voltage-drop upsizing.
- :func:`plot_grid_distributions`: distributions of key figures per grid, e.g. by settlement type.
- :func:`plot_exclusions`: why buildings were left out of the grids.

The inputs are the layers of ``DatabaseClient.read_lv_layers`` or the table of ``read_grid_summary``. The
functions return their matplotlib figure, never call ``plt.show()`` and read no database.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon

from pylovo.plotting import style as st

__all__ = [
    "SETTLEMENT_NAMES",
    "plot_cable_inventory",
    "plot_exclusions",
    "plot_grid_distributions",
    "plot_rating_counts",
    "plot_settlement_type",
    "plot_voltage_drop_check",
    "settlement_score",
]

#: Names of the settlement types of ``postcode_result.settlement_type``.
SETTLEMENT_NAMES = {1: "rural", 2: "semi-urban", 3: "urban"}

#: Labels of the key figures of ``read_grid_summary``.
SUMMARY_LABELS = {
    "buildings": "Buildings per grid",
    "households": "Households per grid",
    "feeder_km": "Feeder cable per grid (km)",
    "service_km": "House connections per grid (km)",
    "transformer_rated_power": "Station rating (kVA)",
    "peak_load_kw": "Sum of building peak loads (kW)",
    "feeder_m_per_building": "Feeder cable per building (m)",
    "max_total_lv_voltage_drop_pu": "Largest voltage drop in the power flow (p.u.)",
}


def _figure(ax: Optional[plt.Axes], figsize: Tuple[float, float]):
    if ax is None:
        return plt.subplots(figsize=figsize)
    return ax.get_figure(), ax


# --------------------------------------------------------------------------- settlement type
def settlement_score(households: float | None, distance: float | None, thresholds: Mapping[str, float]) -> dict:
    """The settlement type of a postcode area from its two metrics, as in generation.

    Args:
        households: Average number of households per residential building.
        distance: House distance (m): mean distance of the buildings to their four nearest neighbours.
        thresholds: ``rural_max_households``, ``urban_min_households``, ``rural_min_building_distance`` and
            ``urban_max_building_distance`` (the ``transformer_placement`` group of the version parameters).

    Returns:
        ``{"households", "distance", "score", "type"}``: both metrics scaled to 0 (rural) ... 1 (urban), their
        mean and the type (1 rural, 2 semi-urban, 3 urban); a missing metric counts as rural.
    """
    hh = 0.0
    if households is not None and not pd.isna(households):
        span = max(1e-9, thresholds["urban_min_households"] - thresholds["rural_max_households"])
        hh = min(1.0, max(0.0, (float(households) - thresholds["rural_max_households"]) / span))
    dist = 0.0
    if distance is not None and not pd.isna(distance):
        span = max(1e-9, thresholds["rural_min_building_distance"] - thresholds["urban_max_building_distance"])
        dist = 1.0 - min(1.0, max(0.0, (float(distance) - thresholds["urban_max_building_distance"]) / span))
    score = 0.5 * hh + 0.5 * dist
    kind = 3 if score >= 2 / 3 else 2 if score >= 1 / 3 else 1
    return {"households": hh, "distance": dist, "score": score, "type": kind}


def _thresholds(parameters: Optional[Mapping[str, Any]]) -> dict:
    placement = dict((parameters or {}).get("transformer_placement") or {})
    if placement:
        return placement
    from pylovo import config_loader as cfg

    return {"rural_max_households": cfg.RURAL_MAX_HOUSEHOLDS, "urban_min_households": cfg.URBAN_MIN_HOUSEHOLDS,
            "rural_min_building_distance": cfg.RURAL_MIN_BUILDING_DISTANCE,
            "urban_max_building_distance": cfg.URBAN_MAX_BUILDING_DISTANCE,
            "transformer_mapping": cfg.TRANSFORMER_MAPPING}


def plot_settlement_type(postcodes: pd.DataFrame, parameters: Optional[Mapping[str, Any]] = None,
                         context: Optional[pd.DataFrame] = None, labels: Optional[Mapping[int, str]] = None,
                         ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (6.6, 5.6),
                         title: Optional[str] = None) -> Figure:
    """How the two density metrics of a postcode area give its settlement type.

    Both metrics are scaled between their rural and urban thresholds (0 rural, 1 urban); their mean selects the
    type: below 1/3 rural, below 2/3 semi-urban, else urban. The type selects the station ratings allowed in the
    area (``transformer_mapping``). The diagonal bands show the three types.

    Args:
        postcodes: Rows with ``plz``, ``avg_households_per_building`` and ``house_distance`` (the ``postcode``
            layer of one or more areas).
        parameters: The generation parameters of the version (thresholds and rating mapping); default the
            configuration.
        context: More postcode areas drawn as small grey dots, e.g. all areas of the version.
        labels: ``{plz: label}`` for the marked areas; default the postcode.
        ax: Axes to draw into; a new figure if None.
        figsize: Size of a new figure.
        title: Title.

    Returns:
        The figure.
    """
    thresholds = _thresholds(parameters)
    mapping = thresholds.get("transformer_mapping") or {}
    with st.style():
        fig, ax = _figure(ax, figsize)
        band_colors = {kind: st.mix_with_surface(color, 0.13) for kind, color in st.SETTLEMENT_COLORS.items()}
        corners = {1: [(0, 0), (2 / 3, 0), (0, 2 / 3)],
                   2: [(2 / 3, 0), (1, 0), (1, 1 / 3), (1 / 3, 1), (0, 1), (0, 2 / 3)],
                   3: [(1, 1 / 3), (1, 1), (1 / 3, 1)]}
        for kind, points in corners.items():
            ax.add_patch(Polygon(points, closed=True, color=band_colors[kind], lw=0, zorder=0))
        for value in (2 / 3, 4 / 3):   # score 1/3 and 2/3: x + y = 2 * score
            x0, x1 = max(0.0, value - 1), min(1.0, value)
            ax.plot([x0, x1], [value - x0, value - x1], color=st.BASELINE, lw=0.8, zorder=1)
        names = {1: (0.13, 0.13), 2: (0.47, 0.47), 3: (0.85, 0.85)}
        for kind, (x, y) in names.items():
            ratings = mapping.get(kind, mapping.get(str(kind)))
            text = SETTLEMENT_NAMES[kind] + (f"\n{' / '.join(f'{r:g}' for r in ratings)} kVA" if ratings else "")
            ax.text(x, y, text, ha="center", va="center", fontsize=8, color=st.INK_SECONDARY, rotation=-45,
                    zorder=2)
        if context is not None and len(context):
            scores = [settlement_score(h, d, thresholds) for h, d in
                      zip(context["avg_households_per_building"], context["house_distance"])]
            jitter = np.random.default_rng(1).normal(0, 0.006, size=(len(scores), 2))
            ax.scatter([s["households"] for s in scores] + jitter[:, 0], [s["distance"] for s in scores] + jitter[:, 1],
                       s=4, color=st.INK_MUTED, alpha=0.35, lw=0, zorder=3)
        handles = []
        for row in postcodes.itertuples():
            score = settlement_score(row.avg_households_per_building, row.house_distance, thresholds)
            color = st.SETTLEMENT_COLORS[score["type"]]
            ax.scatter(score["households"], score["distance"], s=90, color=color, edgecolor="white", lw=1.2, zorder=5)
            name = (labels or {}).get(int(row.plz), str(int(row.plz)))
            handles.append(Line2D([], [], marker="o", ls="", color=color, markeredgecolor="white", markersize=8,
                                  label=f"{name}: {row.avg_households_per_building:.2f} households/building, "
                                        f"{row.house_distance:.0f} m house distance, {SETTLEMENT_NAMES[score['type']]}"))
        if context is not None and len(context):
            handles.append(Line2D([], [], marker="o", ls="", color=st.INK_MUTED, alpha=0.5, markersize=3,
                                  label=f"Other postcode areas ({len(context)})"))
        lo_h, hi_h = thresholds["rural_max_households"], thresholds["urban_min_households"]
        lo_d, hi_d = thresholds["rural_min_building_distance"], thresholds["urban_max_building_distance"]
        ax.set_xlim(-0.03, 1.03)
        ax.set_ylim(-0.03, 1.03)
        ax.set_xticks([0, 0.5, 1], [f"≤ {lo_h:g}", f"{(lo_h + hi_h) / 2:.2f}", f"≥ {hi_h:g}"])
        ax.set_yticks([0, 0.5, 1], [f"≥ {lo_d:g} m", f"{(lo_d + hi_d) / 2:.1f} m", f"≤ {hi_d:g} m"])
        ax.set_xlabel("Households per residential building")
        ax.set_ylabel("House distance (to the four nearest buildings)")
        ax.set_aspect("equal")
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.14), fontsize=7.5)
        ax.set_title(title if title is not None else "Settlement type and allowed station ratings")
    return fig


# --------------------------------------------------------------------------- one postcode area
def plot_rating_counts(grids: pd.DataFrame, ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (5.2, 3.2),
                       title: Optional[str] = None) -> Figure:
    """The number of grids per station rating (kVA); a station of two 400 kVA units counts as 800 kVA."""
    counts = pd.to_numeric(grids["transformer_rated_power"], errors="coerce").dropna().value_counts().sort_index()
    colors = st.rating_colors(counts.index)
    with st.style():
        fig, ax = _figure(ax, figsize)
        labels = [f"{r:g}" for r in counts.index]
        bars = ax.bar(labels, counts.to_numpy(), color=[colors[r] for r in counts.index], width=0.62, zorder=3)
        ax.bar_label(bars, padding=2, fontsize=8, color=st.INK_SECONDARY)
        ax.set_xlabel("Station rating (kVA)")
        ax.set_ylabel("Grids")
        st.tidy_value_axes(ax, "y")
        ax.set_title(title if title is not None else f"{int(counts.sum())} stations by rating")
    return fig


def plot_cable_inventory(lines: pd.DataFrame, ax: Optional[plt.Axes] = None,
                         figsize: Tuple[float, float] = (6.4, 3.4), title: Optional[str] = None) -> Figure:
    """Installed cable length per type, split into feeders and house connections (parallel cables counted)."""
    km = pd.to_numeric(lines["length_km"], errors="coerce").fillna(0) * \
        pd.to_numeric(lines.get("parallel", 1), errors="coerce").fillna(1)
    service = lines["is_service"].fillna(False).astype(bool) if "is_service" in lines else \
        pd.Series(False, index=lines.index)
    table = pd.DataFrame({"std_type": lines["std_type"], "km": km, "kind": np.where(service, "service", "feeder")})
    pivot = table.pivot_table(index="std_type", columns="kind", values="km", aggfunc="sum", fill_value=0.0)
    for column in ("feeder", "service"):
        if column not in pivot:
            pivot[column] = 0.0
    pivot = pivot.loc[sorted(pivot.index, key=st.cross_section)]
    colors = st.cable_colors(pivot.index)
    with st.style():
        fig, ax = _figure(ax, figsize)
        y = np.arange(len(pivot))
        ax.barh(y, pivot["feeder"], color=[colors[t] for t in pivot.index], height=0.62, zorder=3)
        ax.barh(y, pivot["service"], left=pivot["feeder"], color=st.SURFACE, height=0.62, zorder=3, hatch="//////",
                edgecolor=[colors[t] for t in pivot.index], lw=0.8)
        for yi, (feeder, service) in enumerate(zip(pivot["feeder"], pivot["service"])):
            ax.text(feeder + service, yi, f"  {feeder + service:.1f} km", va="center", fontsize=7.5,
                    color=st.INK_SECONDARY)
        ax.set_yticks(y, pivot.index)
        ax.set_xlabel("Installed length (km)")
        ax.set_xlim(0, (pivot["feeder"] + pivot["service"]).max() * 1.22 if len(pivot) else 1)
        st.tidy_value_axes(ax, "x")
        ax.legend(handles=[Patch(color=st.SEQUENTIAL_BLUE[3], label="Feeders"),
                           Patch(facecolor=st.SURFACE, hatch="//////", edgecolor=st.SEQUENTIAL_BLUE[3],
                                 label="House connections")], loc="upper center", bbox_to_anchor=(0.5, -0.2),
                  ncol=2, fontsize=7.5)
        ax.set_title(title if title is not None else
                     f"{(pivot['feeder'] + pivot['service']).sum():.1f} km of cable by type")
    return fig


def plot_voltage_drop_check(grids: pd.DataFrame, limit_percent: Optional[float] = None,
                            ax: Optional[plt.Axes] = None, figsize: Optional[Tuple[float, float]] = None,
                            title: Optional[str] = None) -> Figure:
    """The design voltage drop of the feeders of every grid: sized for the current only, and as built.

    pylovo first sizes every feeder for its current (ampacity); where the voltage drop from the station to the
    farthest house connection then exceeds the limit, it upsizes feeder sections until the limit holds. Hollow
    dots show the drop after the ampacity sizing, filled dots the drop of the built grid.

    Args:
        grids: The ``grids`` layer, or a summary table, with ``kcid``, ``bcid``,
            ``ampacity_max_feeder_voltage_drop_percent`` and ``selected_max_feeder_voltage_drop_percent``.
        limit_percent: The end-to-end feeder limit of the version
            (``cable_dimensioning.max_end_to_end_feeder_voltage_drop_percent``); default the configuration.
    """
    if limit_percent is None:
        from pylovo import config_loader as cfg

        limit_percent = float(cfg.MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT)
    table = grids.copy()
    table["ampacity"] = pd.to_numeric(table["ampacity_max_feeder_voltage_drop_percent"], errors="coerce")
    table["selected"] = pd.to_numeric(table["selected_max_feeder_voltage_drop_percent"], errors="coerce")
    table = table.dropna(subset=["selected"]).sort_values("selected")
    figsize = figsize or (6.8, 1.0 + 0.16 * len(table))
    with st.style():
        fig, ax = _figure(ax, figsize)
        y = np.arange(len(table))
        upsized = table["ampacity"] > table["selected"] + 1e-6
        for yi, (a, s, up) in enumerate(zip(table["ampacity"], table["selected"], upsized)):
            if up:
                ax.plot([a, s], [yi, yi], color=st.BASELINE, lw=2, zorder=2, solid_capstyle="round")
                ax.scatter(a, yi, s=26, facecolor=st.SURFACE, edgecolor=st.INK_MUTED, lw=1.3, zorder=3)
        ax.scatter(table["selected"], y, s=30, color=np.where(table["selected"] <= limit_percent + 1e-6,
                                                               st.CATEGORICAL[0], st.STATUS["critical"]),
                   edgecolor=st.SURFACE, lw=1.0, zorder=4)
        ax.axvline(limit_percent, color=st.INK_SECONDARY, lw=1, zorder=1)
        ax.text(limit_percent, len(table) - 0.3, f" limit {limit_percent:g} %", fontsize=7.5,
                color=st.INK_SECONDARY, va="bottom")
        names = [f"{int(k)}/{int(b)}" + (f" · {r:g} kVA" if "transformer_rated_power" in table and pd.notna(r) else "")
                 for k, b, r in zip(table["kcid"], table["bcid"],
                                    table.get("transformer_rated_power", pd.Series(np.nan, index=table.index)))]
        ax.set_yticks(y, names, fontsize=6.5 if len(table) > 30 else 7.5)
        ax.set_ylim(-0.8, len(table) - 0.2)
        ax.set_xlabel("Largest feeder voltage drop from the station (% of 400 V)")
        st.tidy_value_axes(ax, "x")
        handles = [Line2D([], [], marker="o", ls="", markerfacecolor=st.SURFACE, markeredgecolor=st.INK_MUTED,
                          label="Sized for the current only"),
                   Line2D([], [], marker="o", ls="", color=st.CATEGORICAL[0], label="As built")]
        ax.legend(handles=handles, loc="lower right", fontsize=7.5)
        ax.set_title(title if title is not None else
                     f"{int(upsized.sum())} of {len(table)} grids needed thicker feeders for the voltage drop")
    return fig


# --------------------------------------------------------------------------- many grids
def plot_grid_distributions(summary: pd.DataFrame, columns: Sequence[str] = ("buildings", "households", "feeder_km",
                                                                             "transformer_rated_power"),
                            by: Optional[str] = "settlement_type", order: Optional[Sequence] = None,
                            names: Optional[Mapping] = None, figsize: Optional[Tuple[float, float]] = None,
                            title: Optional[str] = None) -> Figure:
    """Distributions of key figures per grid as cumulative curves, one panel per figure and one curve per group.

    A cumulative curve shows for every value the share of grids at or below it; the median is where a curve
    crosses 50 %. Curves of groups (e.g. settlement types) side by side show how their grids differ.

    Args:
        summary: One row per grid (``read_grid_summary``); ``feeder_m_per_building`` is derived if requested.
        columns: The key figures (columns of ``summary``), see :data:`SUMMARY_LABELS`.
        by: Group column (default the settlement type of the postcode area), or None for all grids together.
        order: Order of the groups; default sorted.
        names: Labels of the groups; default :data:`SETTLEMENT_NAMES` for ``settlement_type``.
        figsize: Size of the figure; default 3.2 inches per panel.
        title: Title of the figure.
    """
    data = summary.copy()
    if "feeder_m_per_building" in columns and "feeder_m_per_building" not in data:
        data["feeder_m_per_building"] = 1000 * data["feeder_km"] / data["buildings"].where(data["buildings"] > 0)
    groups = [(None, data)] if by is None else [(key, data[data[by] == key]) for key in
                                                 (order if order is not None else sorted(data[by].dropna().unique()))]
    if names is None and by == "settlement_type":
        names = SETTLEMENT_NAMES
    if by == "settlement_type":
        colors = {key: st.SETTLEMENT_COLORS.get(key, st.INK_MUTED) for key, _ in groups}
    else:
        colors = {key: st.CATEGORICAL[i % len(st.CATEGORICAL)] for i, (key, _) in enumerate(groups)}
    figsize = figsize or (3.2 * len(columns), 3.0)
    with st.style():
        fig, axes = plt.subplots(1, len(columns), figsize=figsize, squeeze=False)
        for ax, column in zip(axes[0], columns):
            for key, group in groups:
                values = np.sort(pd.to_numeric(group[column], errors="coerce").dropna().to_numpy())
                if len(values) == 0:
                    continue
                share = np.arange(1, len(values) + 1) / len(values)
                ax.step(values, share, where="post", color=colors[key], lw=1.6)
                median = float(np.median(values))
                ax.scatter([median], [0.5], color=colors[key], s=18, zorder=4, edgecolor=st.SURFACE, lw=0.8)
            ax.axhline(0.5, color=st.BASELINE, lw=0.7, zorder=0)
            ax.set_xlabel(SUMMARY_LABELS.get(column, column))
            ax.set_ylim(0, 1.02)
            values = pd.to_numeric(data[column], errors="coerce").dropna()
            if len(values) and values.max() > 20 * max(1e-9, values.median()):
                ax.set_xscale("log")    # long tail: the 1st percentile of the positive values is the left edge
                ax.set_xlim(left=float(np.percentile(values[values > 0], 1)))
            st.tidy_value_axes(ax, "both")
        axes[0][0].set_ylabel("Share of grids")
        axes[0][0].yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.0%}"))
        for ax in axes[0][1:]:
            ax.set_yticklabels([])
        if by is not None:
            handles = [Line2D([], [], color=colors[key], lw=2,
                              label=f"{(names or {}).get(key, key)} ({len(group)} grids, dots: median)")
                       for key, group in groups]
            fig.legend(handles=handles, loc="lower center", ncol=len(handles), fontsize=8,
                       bbox_to_anchor=(0.5, -0.02))
            fig.subplots_adjust(bottom=0.3)
        if title:
            fig.suptitle(title, x=0.01, ha="left", fontsize=10, fontweight="bold")
        fig.tight_layout(rect=(0, 0.08 if by is not None else 0, 1, 0.95 if title else 1))
    return fig


def plot_exclusions(exclusions: pd.DataFrame, supplied: Optional[int] = None, ax: Optional[plt.Axes] = None,
                    figsize: Tuple[float, float] = (5.6, 2.6), title: Optional[str] = None) -> Figure:
    """Why buildings of the postcode area are in no LV grid (``building_exclusion.reason``).

    Args:
        exclusions: Rows with a ``reason`` (the ``exclusions`` layer).
        supplied: Number of supplied buildings, for the title.
    """
    counts = exclusions["reason"].value_counts().sort_values()
    with st.style():
        fig, ax = _figure(ax, figsize)
        y = np.arange(len(counts))
        bars = ax.barh(y, counts.to_numpy(), color=st.INK_MUTED, height=0.6, zorder=3)
        ax.bar_label(bars, padding=2, fontsize=7.5, color=st.INK_SECONDARY)
        ax.set_yticks(y, [str(r).replace("_", " ") for r in counts.index])
        ax.set_xlabel("Buildings")
        st.tidy_value_axes(ax, "x")
        total = int(counts.sum())
        default = f"{total} buildings in no LV grid" + (f" (supplied: {supplied})" if supplied is not None else "")
        ax.set_title(title if title is not None else default)
    return fig
