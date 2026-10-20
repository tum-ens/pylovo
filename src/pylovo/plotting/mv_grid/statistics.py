"""Statistics plots of the MV grids of the MV stage (:mod:`pylovo.mv_grid`).

- :func:`plot_metric_distribution`: box and strip plot of one metric per variant (or any group) with literature
  ranges as bands.
- :func:`plot_variant_summary`: small multiples of the variant statistics (view ``pylovo.mv_variant_stats``).
- :func:`plot_voltage_profile`: voltage against the distance from the busbar along one ring, load and (n-1) case.
- :func:`plot_loading_histogram`: line loadings of one case with the 60 % and 100 % limits.
- :func:`plot_voltage_budget`: the voltage budget per station from the busbar to the lowest LV voltage.
- :func:`plot_tap_distribution`: MV/LV tap positions and band conflicts per variant.
- :func:`plot_plausibility`: metric values against literature ranges with traffic lights.
- :func:`plot_solver_convergence`: ring length after the savings step and after the local search, and runtime.

The functions take data frames with the column names of the MV tables (``pylovo.mv_node``, ``mv_line``,
``mv_grid``, ``mv_ring``, the view ``mv_variant_stats``) or of :mod:`pylovo.mv_grid.metrics`. The default limits
are the shipped values of the MV stage (busbar 1.02 p.u., 5 % drop in normal operation, 10 % in (n-1), lines at 60 %
in normal operation and 100 % in (n-1) and feed-in, MV/LV taps of 2.5 %); pass the values of a variant to change
them. Needs the ``plots`` extra. The functions return a matplotlib ``Figure`` and never call ``plt.show()``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import NullFormatter, ScalarFormatter

from pylovo.analysis import mv_validation
from pylovo.plotting.mv_grid.maps import _HALF_COLORS, _as_bool, _column, _line_table, _node_table, ring_paths
from pylovo.plotting.utils import add_limit_lines, add_statistics_box, get_color_map, setup_axes

__all__ = [
    "METRIC_LABELS",
    "STATUS_COLORS",
    "metric_label",
    "plot_loading_histogram",
    "plot_metric_distribution",
    "plot_plausibility",
    "plot_solver_convergence",
    "plot_tap_distribution",
    "plot_variant_summary",
    "plot_voltage_budget",
    "plot_voltage_profile",
]

#: Axis labels of the metrics of :mod:`pylovo.mv_grid.metrics` and of the columns of ``mv_variant_stats``.
METRIC_LABELS: dict[str, str] = {
    "ring_members": "Ring members per ring",
    "ring_members_incl_stubs": "Ring members per ring incl. stubs",
    "ring_km": "Ring length (km)",
    "ring_km_incl_stubs": "Ring length incl. stubs (km)",
    "feeder_supply_points": "Supply points per feeder",
    "feeder_km": "Feeder (half-ring) length (km)",
    "feeder_km_incl_stubs": "Feeder length incl. stubs (km)",
    "feeders_per_substation": "Feeders per substation",
    "supply_points_per_substation": "Supply points per substation",
    "km_per_supply_point": "Circuit km per supply point",
    "cable_share": "Cable share of the circuit length",
    "km_per_km2": "Circuit km per km²",
    "circuit_km_per_substation": "Circuit km per substation",
    "station_spacing_m": "Station spacing (m)",
    "customer_share": "MV customer share of supply points",
    "substation_peak_mva": "Substation peak (MVA)",
    "hv_mv_unit_mva": "HV/MV transformer rating (MVA)",
    "peak_to_station_rating": "Peak over installed station ratings",
    "max_line_loading_percent": "Highest line loading, normal operation (%)",
    "open_ring_grid_share": "Share of grids operated as open rings",
    "open_meshed_share": "Open-meshed share",
    "length_ratio_to_real": "Synthetic over real circuit length",
    "n_grids": "MV grids",
    "n_done": "MV grids done",
    "n_failed": "MV grids failed",
    "n_complete": "Complete districts",
    "n_converged": "Grids without violations",
    "n_stations": "MV/LV stations",
    "n_customers": "MV customers",
    "n_generators": "MV generators",
    "n_rings": "Rings and feeders",
    "n_stub_nodes": "Stub nodes",
    "circuit_km": "Circuit length (km)",
    "cable_km": "Cable length (km)",
    "overhead_km": "Overhead line length (km)",
    "trench_km": "Trench length (km)",
    "peak_load_mw": "Peak load (MW)",
    "n_band_conflicts": "Band conflicts",
    "runtime_s": "Runtime (s)",
}

#: Colours of the traffic-light statuses of ``mv_validation.compare_to_literature``.
STATUS_COLORS: dict[str, str] = {"green": "#2ca02c", "amber": "#ff9f1c", "red": "#d62728", "n/a": "#9a9a9a"}

_RANGE_COLORS = ("#2ca02c", "#9467bd", "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f")
_LOADING_COLUMNS = {
    "load": "loading_load_percent",
    "n1": "loading_n1_percent", "n1a": "loading_n1_percent", "n1b": "loading_n1_percent",
    "feed_in": "loading_feed_in_percent", "feed_in_wind": "loading_feed_in_percent",
    "feed_in_pv": "loading_feed_in_percent",
}
_LOADING_CASE_LABELS = {
    "loading_load_percent": "load case",
    "loading_n1_percent": "(n-1) cases",
    "loading_feed_in_percent": "feed-in cases",
}
_PLAUSIBILITY_RATIO_LIMITS = (0.125, 8.0)
_N1_COLORS = {"a": "#0b3c66", "b": "#9c4a00"}


def metric_label(name: str) -> str:
    """Readable axis label of a metric or statistics column (the name itself if it is unknown)."""
    return METRIC_LABELS.get(name, name.replace("_", " "))


def _figure(ax: Optional[plt.Axes], figsize: Tuple[float, float]) -> tuple[Figure, plt.Axes]:
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()
    return fig, ax


def _opt(value: Any) -> Optional[float]:
    """A finite float, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: float) -> str:
    """Short number text: thousands separators from 1,000, else up to four significant digits."""
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.4g}"


# ---------------------------------------------------------------------------------------------------------------
# Distributions and variant comparisons
# ---------------------------------------------------------------------------------------------------------------


def _resolve_ranges(ranges: Any, metric: str, area_class: Optional[str]) -> pd.DataFrame:
    """Literature ranges as a data frame (``low``, ``high``, ``typical``, ``label``, ``area_class``)."""
    columns = ["low", "high", "typical", "label", "area_class"]
    if ranges is None or ranges is False:
        return pd.DataFrame(columns=columns)
    if ranges is True or (isinstance(ranges, str) and ranges == "auto"):
        return mv_validation.literature_ranges(metric, area_class)
    if isinstance(ranges, pd.DataFrame):
        frame = ranges
        if "metric" in frame.columns:
            frame = frame[frame["metric"] == metric]
    else:
        frame = pd.DataFrame([r.__dict__ if isinstance(r, mv_validation.LiteratureRange) else dict(r) for r in ranges])
    for column in columns:
        if column not in frame.columns:
            frame = frame.assign(**{column: None})
    return frame.reset_index(drop=True)


def _draw_ranges(ax: plt.Axes, ranges: pd.DataFrame) -> int:
    """Draw ranges as horizontal bands, one-sided bounds as dashed lines and typical values as dotted lines."""
    drawn = 0
    for i, row in enumerate(ranges.itertuples(index=False)):
        color = _RANGE_COLORS[i % len(_RANGE_COLORS)]
        name = str(row.label) if isinstance(row.label, str) and row.label else f"range {i + 1}"
        if isinstance(row.area_class, str) and row.area_class:
            name += f" ({row.area_class})"
        low, high, typical = _opt(row.low), _opt(row.high), _opt(row.typical)
        if low is not None and high is not None:
            ax.axhspan(low, high, color=color, alpha=0.13, zorder=1, label=f"{name}: {_fmt(low)} to {_fmt(high)}")
            drawn += 1
        elif high is not None:
            ax.axhline(high, color=color, linestyle="--", linewidth=1.2, zorder=2, label=f"{name}: at most {_fmt(high)}")
            drawn += 1
        elif low is not None:
            ax.axhline(low, color=color, linestyle="--", linewidth=1.2, zorder=2, label=f"{name}: at least {_fmt(low)}")
            drawn += 1
        if typical is not None:
            ax.axhline(typical, color=color, linestyle=":", linewidth=1.4, zorder=2,
                       label=f"{name}: typical {_fmt(typical)}")
            drawn += 1
    return drawn


def plot_metric_distribution(df: pd.DataFrame, metric: str, by: str = "variant", ranges: Any = None,
                             ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (10, 6),
                             area_class: Optional[str] = None, order: Optional[Sequence[Any]] = None,
                             title: Optional[str] = None) -> Figure:
    """Box and strip plot of one metric per group (e.g. per variant) with literature ranges as bands.

    Args:
        df: One row per entity (district, ring, feeder, ...) with the columns ``by`` and ``metric``, e.g.
            ``metrics.ring_table`` or ``metrics.feeder_table`` rows of several variants with a ``variant`` column.
        metric: Column to plot; its literature ranges have the same name (``mv_validation.LITERATURE_RANGES``).
        by: Grouping column.
        ranges: None (no ranges), ``"auto"`` or True (the ranges of ``metric`` in ``mv_validation.LITERATURE_RANGES``,
            filtered by ``area_class``), a data frame like ``mv_validation.literature_ranges()`` or a sequence of
            ``mv_validation.LiteratureRange``.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        area_class: Area class for ``ranges="auto"`` (``rural`` or ``urban``; None: all classes).
        order: Group order; default: order of appearance.
        title: Plot title.

    Returns:
        The figure that contains the plot.

    Raises:
        ValueError: If ``metric`` or ``by`` is not a column of ``df``.
    """
    for column in (metric, by):
        if column not in df.columns:
            raise ValueError(f"the data frame has no column {column!r}")
    groups = list(order) if order is not None else list(pd.unique(df[by]))
    colors = get_color_map([str(g) for g in groups])
    fig, ax = _figure(ax, figsize)
    numbers = pd.to_numeric(df[metric], errors="coerce")
    values = [numbers[(df[by] == g).to_numpy()].dropna().to_numpy(dtype=float) for g in groups]
    positions = np.arange(1, len(groups) + 1)
    n_ranges = _draw_ranges(ax, _resolve_ranges(ranges, metric, area_class))
    filled = [(p, g, v) for p, g, v in zip(positions, groups, values) if len(v)]
    if filled:
        boxes = ax.boxplot([v for _, _, v in filled], positions=[p for p, _, _ in filled], widths=0.5,
                           showfliers=False, patch_artist=True, zorder=3)
        for patch, (_, g, _) in zip(boxes["boxes"], filled):
            patch.set_facecolor(colors[str(g)])
            patch.set_alpha(0.35)
        for median in boxes["medians"]:
            median.set_color("black")
    rng = np.random.default_rng(0)
    for p, g, v in zip(positions, groups, values):
        ax.scatter(p + rng.uniform(-0.15, 0.15, len(v)), v, s=14, color=colors[str(g)], alpha=0.8,
                   edgecolors="none", zorder=4)
    ax.set_xticks(positions)
    ax.set_xticklabels([f"{g}\n(n={len(v)})" for g, v in zip(groups, values)])
    ax.set_xlim(0.4, len(groups) + 0.6)
    setup_axes(ax, xlabel=by, ylabel=metric_label(metric), title=title or f"{metric_label(metric)} by {by}")
    if n_ranges:
        ax.legend(fontsize=8, loc="best", framealpha=0.9)
    return fig


def _variant_labels(stats: pd.DataFrame) -> list[str]:
    if "mv_variant_id" in stats.columns:
        if "version_id" in stats.columns and stats["version_id"].nunique() > 1:
            return [f"{v}/{m}" for v, m in zip(stats["version_id"], stats["mv_variant_id"])]
        return [str(m) for m in stats["mv_variant_id"]]
    if "variant" in stats.columns:
        return [str(v) for v in stats["variant"]]
    return [str(i) for i in stats.index]


def plot_variant_summary(stats: pd.DataFrame, metrics: Sequence[str], figsize: Optional[Tuple[float, float]] = None,
                         ncols: int = 3, title: Optional[str] = None) -> Figure:
    """Small multiples of variant statistics: one bar chart per metric, one bar per variant.

    Args:
        stats: One row per variant, e.g. ``read_variant_stats()`` (view ``mv_variant_stats``: bars are labelled
            ``mv_variant_id``, or ``version_id/mv_variant_id`` when there are several versions), or any frame
            with a ``variant`` column (else the index labels the bars).
        metrics: Columns to plot.
        figsize: Figure size in inches; default 4.6 x 3.4 per panel.
        ncols: Panels per row.
        title: Figure title.

    Returns:
        The figure with one panel per metric.

    Raises:
        ValueError: If a metric is not a column of ``stats`` or no metric is given.
    """
    metrics = list(metrics)
    if not metrics:
        raise ValueError("no metrics given")
    missing = [m for m in metrics if m not in stats.columns]
    if missing:
        raise ValueError(f"the statistics have no column {', '.join(map(repr, missing))}")
    labels = _variant_labels(stats)
    colors = get_color_map(list(dict.fromkeys(labels)))
    ncols = max(1, min(ncols, len(metrics)))
    nrows = math.ceil(len(metrics) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=figsize or (4.6 * ncols, 3.4 * nrows), squeeze=False)
    x = np.arange(len(labels))
    for ax, metric in zip(axes.flat, metrics):
        values = pd.to_numeric(stats[metric], errors="coerce").to_numpy(dtype=float)
        ax.bar(x, np.nan_to_num(values), color=[colors[label] for label in labels], edgecolor="black",
               linewidth=0.4)
        for xi, value in zip(x, values):
            if np.isfinite(value):
                ax.annotate(_fmt(value), (xi, value), xytext=(0, 2), textcoords="offset points", ha="center",
                            va="bottom", fontsize=8)
        ax.set_xticks(x)
        rotate = len(labels) > 4
        ax.set_xticklabels(labels, rotation=45 if rotate else 0, ha="right" if rotate else "center", fontsize=9)
        ax.set_title(metric_label(metric), fontsize=11, fontweight="bold")
        ax.grid(True, axis="y", alpha=0.3)
        ax.margins(y=0.15)
    for ax in axes.flat[len(metrics):]:
        ax.set_visible(False)
    if title:
        fig.suptitle(title, fontsize=14, fontweight="bold")
    fig.tight_layout()
    return fig


# ---------------------------------------------------------------------------------------------------------------
# Electrical results
# ---------------------------------------------------------------------------------------------------------------


def plot_voltage_profile(nodes: Any, lines: Any, mv_grid_id: int, ring_id: int, ax: Optional[plt.Axes] = None,
                         figsize: Tuple[float, float] = (10, 6), busbar_vm_pu: float = 1.02,
                         max_drop_percent: float = 5.0, max_drop_n1_percent: float = 10.0,
                         title: Optional[str] = None) -> Figure:
    """Voltage against the distance from the busbar along one ring: load case and (n-1).

    The load case (``vm_load_pu``) is drawn per half at the feeding distance in normal operation, starting at the
    busbar. The (n-1) voltage of a node (``vm_n1_pu``, the worse of both exits out) is drawn at its feeding
    distance around the ring from the far exit (see :func:`~pylovo.plotting.mv_grid.maps.ring_paths`), per half:
    half A as fed from exit B and half B as fed from exit A, i.e. the far ends of the two (n-1) configurations.
    Main-path nodes are connected, stub nodes are single markers, the open point is a dotted vertical line.
    Limits: ``busbar_vm_pu`` minus ``max_drop_percent`` (0.97) and minus ``max_drop_n1_percent`` (0.92).

    Args:
        nodes: MV nodes (``mv_node`` columns incl. ``vm_load_pu`` and ``vm_n1_pu``).
        lines: MV lines (``mv_line`` columns).
        mv_grid_id: The MV grid.
        ring_id: The ring.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        busbar_vm_pu: MV busbar voltage of the stage (``busbar_vm_pu``).
        max_drop_percent: Voltage drop limit in normal operation (``MV_MAX_VOLTAGE_DROP_PERCENT``).
        max_drop_n1_percent: Voltage drop limit in (n-1) (``max_drop_n1_percent``).
        title: Plot title.

    Returns:
        The figure that contains the plot.
    """
    node_t, line_t = ring_paths(nodes, lines, mv_grid_id, ring_id)
    table = _node_table(nodes)
    vm_load = pd.to_numeric(_column(table, "vm_load_pu"), errors="coerce").reindex(node_t.index)
    vm_n1 = pd.to_numeric(_column(table, "vm_n1_pu"), errors="coerce").reindex(node_t.index)
    busbar = node_t.index[node_t["kind"] == "busbar"]
    v0 = float(vm_load[busbar].dropna().iloc[0]) if len(busbar) and vm_load[busbar].notna().any() else busbar_vm_pu
    fig, ax = _figure(ax, figsize)
    for half in ("a", "b"):
        sel = node_t[(node_t["half"] == half) & vm_load.notna() & node_t["dist_km"].notna()]
        if sel.empty:
            continue
        color = _HALF_COLORS[half]
        main = sel[sel["main"]].sort_values("dist_km")
        ax.plot([0.0, *main["dist_km"]], [v0, *vm_load[main.index]], "-o", color=color, markersize=4,
                linewidth=1.5, zorder=3, label=f"load case, half {half.upper()}")
        stubs = sel[~sel["main"]]
        if not stubs.empty:
            ax.scatter(stubs["dist_km"], vm_load[stubs.index], facecolors="none", edgecolors=color, s=30,
                       zorder=3, label=f"load case, stub nodes of half {half.upper()}")
    for half, other in (("a", "B"), ("b", "A")):
        sel = node_t[(node_t["half"] == half) & node_t["dist_n1_km"].notna() & vm_n1.notna()]
        if sel.empty:
            continue
        color = _N1_COLORS[half]
        main = sel[sel["main"]].sort_values("dist_n1_km")
        if not main.empty:
            ax.plot(main["dist_n1_km"], vm_n1[main.index], "--x", color=color, markersize=5, linewidth=1.2,
                    zorder=2, label=f"(n-1), half {half.upper()} fed from exit {other}")
        stubs = sel[~sel["main"]]
        if not stubs.empty:
            ax.scatter(stubs["dist_n1_km"], vm_n1[stubs.index], marker="x", color=color, s=25, zorder=2,
                       label=f"(n-1), stub nodes of half {half.upper()}")
    nop_lines = line_t[line_t["half"] == "nop"]
    if not nop_lines.empty:
        full = _line_table(lines)
        at = _column(full, "open_at_node").reindex(nop_lines.index)
        nop_nodes = [int(a) if pd.notna(a) else int(t) for a, t in zip(at, nop_lines["to_node"])]
        for i, node in enumerate(nop_nodes):
            if node in node_t.index and pd.notna(node_t.at[node, "dist_km"]):
                ax.axvline(node_t.at[node, "dist_km"], color="#d62728", linestyle=":", linewidth=1.2,
                           label="normally-open point" if i == 0 else None)
    ax.axhline(busbar_vm_pu, color="#2c3e50", linestyle=":", linewidth=1.0, label=f"busbar {busbar_vm_pu:.3f} p.u.")
    add_limit_lines(ax, {
        busbar_vm_pu - max_drop_percent / 100: {"label": f"normal limit ({max_drop_percent:g} % drop)",
                                                "color": "#d62728", "linewidth": 1.2},
        busbar_vm_pu - max_drop_n1_percent / 100: {"label": f"(n-1) limit ({max_drop_n1_percent:g} % drop)",
                                                   "color": "#7f0000", "linestyle": "-.", "linewidth": 1.2},
    })
    setup_axes(ax, xlabel="Distance from the busbar along the lines (km)", ylabel="Voltage (p.u.)",
               title=title or f"Voltage profile of ring {ring_id} of MV grid {mv_grid_id}")
    ax.legend(fontsize=8, loc="lower left", framealpha=0.9)
    return fig


def plot_loading_histogram(lines: pd.DataFrame, case: str = "load", ax: Optional[plt.Axes] = None,
                           figsize: Tuple[float, float] = (10, 6), bins: int = 30, by: Optional[str] = None,
                           utilization: float = 0.6, n1_utilization: float = 1.0,
                           title: Optional[str] = None) -> Figure:
    """Histogram of the line loadings of one case with the 60 % (normal) and 100 % ((n-1), feed-in) limits.

    Ties are left out; normally-open lines are left out of the load and feed-in cases (they carry no current).

    Args:
        lines: MV lines with ``loading_load_percent``, ``loading_n1_percent`` and ``loading_feed_in_percent``.
        case: ``load``, ``n1`` (also ``n1a``, ``n1b``), ``feed_in`` (also ``feed_in_wind``, ``feed_in_pv``) or a
            column name.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        bins: Number of histogram bins.
        by: Optional grouping column (e.g. ``variant``): one step histogram per group.
        utilization: Planned utilization in normal operation (``MV_LINE_PLANNING_UTILIZATION``).
        n1_utilization: Utilization limit in (n-1) and feed-in (``n1_utilization``).
        title: Plot title.

    Returns:
        The figure that contains the plot.

    Raises:
        ValueError: If the loading column of ``case`` is missing.
    """
    column = _LOADING_COLUMNS.get(case, case)
    if column not in lines.columns:
        raise ValueError(f"the lines have no column {column!r} (case {case!r})")
    frame = pd.DataFrame(lines)
    keep = _column(frame, "role", "ring").fillna("ring").astype(str) != "tie"
    if column != "loading_n1_percent":
        keep &= ~_as_bool(_column(frame, "normally_open", False))
    frame = frame[keep.to_numpy()]
    values = pd.to_numeric(frame[column], errors="coerce")
    fig, ax = _figure(ax, figsize)
    finite = values.dropna()
    edges = np.histogram_bin_edges(finite, bins=bins) if len(finite) else bins
    if by is not None and by in frame.columns:
        groups = list(pd.unique(frame[by]))
        colors = get_color_map([str(g) for g in groups])
        for g in groups:
            v = values[(frame[by] == g).to_numpy()].dropna()
            ax.hist(v, bins=edges, histtype="step", linewidth=1.8, color=colors[str(g)], label=f"{g} ({len(v)})")
    else:
        ax.hist(finite, bins=edges, color="#1f77b4", edgecolor="black", alpha=0.7)
    normal, high = 100 * utilization, 100 * n1_utilization
    add_limit_lines(ax, {
        normal: {"label": f"planning limit, normal operation ({normal:g} %)", "color": "#ff7f0e"},
        high: {"label": f"(n-1) and feed-in limit ({high:g} %)", "color": "#d62728"},
    }, orientation="vertical")
    limit = normal if column == "loading_load_percent" else high
    if len(finite):
        share = 100 * float((finite > limit + 1e-9).mean())
        add_statistics_box(ax, f"lines: {len(finite)}\nmax: {finite.max():.1f} %\nabove {limit:g} %: {share:.1f} %",
                           position="upper right")
    case_label = _LOADING_CASE_LABELS.get(column, column)
    setup_axes(ax, xlabel=f"Line loading, {case_label} (%)", ylabel="Lines",
               title=title or f"MV line loading, {case_label}")
    ax.legend(fontsize=8, loc="center right", framealpha=0.9)
    return fig


def _station_rows(nodes: Any) -> pd.DataFrame:
    frame = pd.DataFrame(nodes)
    if "kind" in frame.columns:
        frame = frame[frame["kind"].astype(str) == "station"]
    return frame


def _tap_colors(taps: Sequence[int]) -> dict[int, Any]:
    low, high = min(-2, *taps) if taps else -2, max(2, *taps) if taps else 2
    cmap = plt.get_cmap("coolwarm")
    return {t: cmap((t - low) / max(high - low, 1)) for t in range(low, high + 1)}


def plot_voltage_budget(nodes: Any, ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (12, 6),
                        busbar_vm_pu: float = 1.02, busbar_band_pu: float = 0.015, tap_step_percent: float = 2.5,
                        lv_reference_vm_pu: float = 0.96, lv_design_tolerance_pu: float = 0.002,
                        lv_min_limit_pu: float = 0.90, title: Optional[str] = None) -> Figure:
    """Voltage budget per station, from the HV/MV busbar to the lowest LV voltage (station budgets of the stage).

    Every station is one line through the stages busbar setpoint, lower edge of the OLTC band, MV node in the load
    case with the busbar at that edge (``vm_load_pu - busbar_band_pu``), after the MV/LV tap (a negative tap raises
    the LV side by ``tap_step_percent`` per step), LV busbar (``lv_busbar_vm_pu``) and lowest LV voltage
    (``lv_min_vm_pu``). Lines are coloured by tap position, band conflicts red. The requirement of the LV net (the
    MV-side voltage that puts its busbar at the LV reference, ``lv_design_source_vm_pu``) is a black dash at the
    tap stage. Red dashes mark the limits ``lv_reference_vm_pu - lv_design_tolerance_pu`` and ``lv_min_limit_pu``.

    Args:
        nodes: MV nodes (``mv_node`` columns); only stations with ``vm_load_pu`` and ``lv_busbar_vm_pu`` are drawn.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        busbar_vm_pu: OLTC setpoint of the MV busbar (``busbar_vm_pu``).
        busbar_band_pu: Half width of the OLTC band (``busbar_band_pu``).
        tap_step_percent: MV/LV tap step (``mvlv_tap_step_percent``).
        lv_reference_vm_pu: LV busbar reference of the LV version (``LV_REFERENCE_VOLTAGE_PU``).
        lv_design_tolerance_pu: Tolerance below the LV reference (``lv_design_tolerance_pu``).
        lv_min_limit_pu: Lowest admissible LV voltage (DIN EN 50160).
        title: Plot title.

    Returns:
        The figure that contains the plot.

    Raises:
        ValueError: If ``vm_load_pu`` or ``lv_busbar_vm_pu`` is missing.
    """
    frame = _station_rows(nodes)
    for column in ("vm_load_pu", "lv_busbar_vm_pu"):
        if column not in frame.columns:
            raise ValueError(f"the MV nodes have no column {column!r}")
    vm = pd.to_numeric(frame["vm_load_pu"], errors="coerce")
    lv_bus = pd.to_numeric(frame["lv_busbar_vm_pu"], errors="coerce")
    frame = frame[(vm.notna() & lv_bus.notna()).to_numpy()]
    vm, lv_bus = vm[frame.index], lv_bus[frame.index]
    tap = pd.to_numeric(_column(frame, "mv_lv_tap_pos", 0), errors="coerce").fillna(0).round().astype(int)
    v_low = vm - busbar_band_pu
    after_tap = v_low / (1.0 + tap * tap_step_percent / 100.0)
    if "lv_min_vm_pu" in frame.columns:
        lv_min = pd.to_numeric(frame["lv_min_vm_pu"], errors="coerce")
    else:
        lv_min = lv_bus - pd.to_numeric(_column(frame, "lv_max_drop_pu"), errors="coerce")
    conflict = _as_bool(_column(frame, "band_conflict", False))
    tap_limit = _as_bool(_column(frame, "tap_limit", False)) if "tap_limit" in frame.columns else None
    chain = np.column_stack([np.full(len(frame), busbar_vm_pu), np.full(len(frame), busbar_vm_pu - busbar_band_pu),
                             v_low, after_tap, lv_bus, lv_min])
    stages = ["busbar\nsetpoint", "lower edge of\nthe OLTC band", "MV node\n(load case)", "after the\nMV/LV tap",
              "LV busbar", "lowest LV\nvoltage"]
    taps = sorted(set(tap))
    tap_color = _tap_colors(taps)
    fig, ax = _figure(ax, figsize)
    n = len(frame)
    alpha = float(min(0.9, max(0.08, 25.0 / max(n, 1))))
    for flag in (False, True):
        segs, colors = [], []
        for row, t, c in zip(chain, tap, conflict):
            if bool(c) != flag:
                continue
            ok = np.isfinite(row)
            segs.append(np.column_stack([np.arange(len(row))[ok], row[ok]]))
            colors.append("#d62728" if flag else tap_color[int(t)])
        if segs:
            ax.add_collection(LineCollection(segs, colors=colors, linewidths=1.6 if flag else 0.9,
                                             alpha=min(1.0, alpha * 2) if flag else alpha, zorder=4 if flag else 3))
    if "lv_design_source_vm_pu" in frame.columns:
        req = pd.to_numeric(frame["lv_design_source_vm_pu"], errors="coerce").dropna()
        if not req.empty:
            ax.scatter(np.full(len(req), 3.15), req, marker="_", s=60, color="black", linewidths=1.0, alpha=0.6,
                       zorder=5)
    for x, y, text in ((4, lv_reference_vm_pu - lv_design_tolerance_pu, "LV reference"),
                       (5, lv_min_limit_pu, "LV limit")):
        ax.plot([x - 0.3, x + 0.3], [y, y], color="#d62728", linestyle="--", linewidth=1.4, zorder=6)
        ax.annotate(f"{text} {y:.3f}", (x + 0.3, y), xytext=(2, 0), textcoords="offset points", fontsize=7,
                    va="center", color="#d62728")
    ax.set_xticks(range(len(stages)))
    ax.set_xticklabels(stages, fontsize=9)
    ax.set_xlim(-0.4, len(stages) - 0.4)
    ax.autoscale_view(scalex=False)
    handles = [Line2D([], [], color=tap_color[t], linewidth=2, label=f"tap {t:+d}" if t else "tap 0") for t in taps]
    handles.append(Line2D([], [], color="#d62728", linewidth=2, label="band conflict"))
    handles.append(Line2D([], [], marker="_", linestyle="none", color="black", markersize=10,
                          label="requirement of the LV net (MV side)"))
    ax.legend(handles=handles, fontsize=8, loc="lower left", framealpha=0.9)
    if title is None:
        title = f"Voltage budget of {n} MV/LV stations, {int(conflict.sum())} band conflicts"
        if tap_limit is not None:
            title += f", {int(tap_limit.sum())} at the tap limit"
    setup_axes(ax, ylabel="Voltage (p.u.)", title=title)
    return fig


def plot_tap_distribution(nodes_by_variant: Mapping[str, Any] | pd.DataFrame, ax: Optional[plt.Axes] = None,
                          figsize: Tuple[float, float] = (10, 5), share: bool = True,
                          title: Optional[str] = None) -> Figure:
    """MV/LV tap positions of the stations per variant; the band-conflict part of each bar is hatched.

    Args:
        nodes_by_variant: ``{variant: nodes}`` with ``mv_lv_tap_pos`` (and ``band_conflict``, ``kind``), or one
            data frame with a ``variant`` column.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        share: Bars in percent of the stations of the variant (True) or as counts.
        title: Plot title.

    Returns:
        The figure that contains the plot.
    """
    if isinstance(nodes_by_variant, pd.DataFrame):
        if "variant" not in nodes_by_variant.columns:
            raise ValueError("a single data frame needs a 'variant' column")
        nodes_by_variant = {v: g for v, g in nodes_by_variant.groupby("variant", sort=False)}
    data = {}
    for variant, nodes in nodes_by_variant.items():
        frame = _station_rows(nodes)
        taps = pd.to_numeric(_column(frame, "mv_lv_tap_pos"), errors="coerce")
        ok = taps.notna().to_numpy()
        conflict = _as_bool(_column(frame, "band_conflict", False))[ok]
        data[str(variant)] = (taps[ok].round().astype(int), conflict)
    observed = [int(t) for taps, _ in data.values() for t in taps]
    positions = list(range(min(-2, *observed) if observed else -2, (max(2, *observed) if observed else 2) + 1))
    colors = get_color_map(list(data))
    fig, ax = _figure(ax, figsize)
    width = 0.8 / max(len(data), 1)
    x = np.arange(len(positions))
    for i, (variant, (taps, conflict)) in enumerate(data.items()):
        total = len(taps)
        counts = taps.value_counts().reindex(positions, fill_value=0).to_numpy(dtype=float)
        conf = taps[conflict.to_numpy()].value_counts().reindex(positions, fill_value=0).to_numpy(dtype=float)
        scale = 100.0 / total if share and total else 1.0
        offset = (i - (len(data) - 1) / 2) * width
        n_conf = int(conflict.sum())
        pct = 100.0 * n_conf / total if total else 0.0
        ax.bar(x + offset, counts * scale, width, color=colors[variant], edgecolor="black", linewidth=0.4,
               label=f"{variant}: {total} stations, {n_conf} band conflicts ({pct:.1f} %)")
        ax.bar(x + offset, conf * scale, width, facecolor="none", edgecolor="black", hatch="///", linewidth=0.4)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{t:+d}" if t else "0" for t in positions])
    handles, _ = ax.get_legend_handles_labels()
    handles.append(Patch(facecolor="none", edgecolor="black", hatch="///", label="band conflict"))
    ax.legend(handles=handles, fontsize=8, framealpha=0.9)
    setup_axes(ax, xlabel="MV/LV tap position (a negative tap raises the LV side)",
               ylabel="Stations (%)" if share else "Stations", title=title or "MV/LV tap positions per variant")
    return fig


# ---------------------------------------------------------------------------------------------------------------
# Plausibility and solver diagnostics
# ---------------------------------------------------------------------------------------------------------------


def plot_plausibility(table: pd.DataFrame, ax: Optional[plt.Axes] = None,
                      figsize: Optional[Tuple[float, float]] = None,
                      tolerance: float = mv_validation.LITERATURE_TOLERANCE, title: Optional[str] = None) -> Figure:
    """Metric values against literature ranges with traffic lights (output of ``mv_validation.compare_to_literature``).

    One row per (metric, range). Values and ranges are divided by a reference of the range (its typical value,
    else the geometric mean of its bounds, else its one bound), so all metrics share one logarithmic axis; 1 is
    the reference. The range is a grey bar (open-ended ranges run to the axis edge), the amber zones within
    ``tolerance`` of a bound are orange, the values are dots in their traffic-light colour (several entities of
    one row side by side).

    Args:
        table: Rows of ``mv_validation.compare_to_literature`` (``metric``, ``label``, ``area_class``, ``value``,
            ``low``, ``high``, ``typical``, ``status``).
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches; default height grows with the number of rows.
        tolerance: Relative amber zone (``mv_validation.LITERATURE_TOLERANCE``).
        title: Plot title.

    Returns:
        The figure that contains the plot.

    Raises:
        ValueError: If a required column is missing.
    """
    required = ["metric", "value", "low", "high", "typical", "status"]
    missing = [c for c in required if c not in table.columns]
    if missing:
        raise ValueError(f"the table has no column {', '.join(map(repr, missing))}")
    frame = table.copy()
    for column in ("label", "area_class"):
        if column not in frame.columns:
            frame[column] = None
    frame["_key"] = list(zip(frame["metric"], frame["label"].astype(str),
                             frame["area_class"].where(frame["area_class"].notna(), "")))
    keys = list(dict.fromkeys(frame["_key"]))
    xmin, xmax = _PLAUSIBILITY_RATIO_LIMITS
    if figsize is None:
        figsize = (11, max(3.0, 0.42 * len(keys) + 1.6))
    fig, ax = _figure(ax, figsize)
    labels = []
    for i, key in enumerate(keys):
        y = len(keys) - 1 - i
        metric, label, area = key
        labels.append(f"{metric}: {label}" + (f" [{area}]" if area else ""))
        rows = frame[frame["_key"] == key]
        first = rows.iloc[0]
        low, high, typical = _opt(first["low"]), _opt(first["high"]), _opt(first["typical"])
        if typical is not None and typical > 0:
            ref = typical
        elif low is not None and high is not None and low > 0 and high > 0:
            ref = math.sqrt(low * high)
        else:
            ref = next((b for b in (low, high) if b is not None and b > 0), None)
        if ref is None:
            continue
        lo = low / ref if low is not None else None
        hi = high / ref if high is not None else None
        if lo is None and hi is None:
            lo = hi = typical / ref if typical is not None else None
        if lo is None and hi is None:
            continue
        left = lo if lo is not None else xmin
        right = hi if hi is not None else xmax
        ax.barh(y, max(right - left, 0.0), left=left, height=0.5, color="#d9d9d9", zorder=1)
        if lo is not None:
            ax.barh(y, lo * tolerance, left=lo * (1 - tolerance), height=0.5, color="#ffe0b3", zorder=1)
        if hi is not None:
            ax.barh(y, hi * tolerance, left=hi, height=0.5, color="#ffe0b3", zorder=1)
        if typical is not None:
            ax.plot([typical / ref] * 2, [y - 0.3, y + 0.3], color="black", linewidth=1.2, zorder=2)
        values = pd.to_numeric(rows["value"], errors="coerce").to_numpy(dtype=float) / ref
        ok = np.isfinite(values)
        if ok.any():
            status = rows["status"].astype(str).to_numpy()[ok]
            k = int(ok.sum())
            offsets = np.linspace(-0.15, 0.15, k) if k > 1 else np.zeros(1)
            ax.scatter(np.clip(values[ok], xmin, xmax), y + offsets, s=30, zorder=3,
                       c=[STATUS_COLORS.get(s, STATUS_COLORS["n/a"]) for s in status], edgecolors="black",
                       linewidths=0.4)
    ax.set_xscale("log")
    ax.set_xlim(xmin, xmax)
    ticks = [0.125, 0.25, 0.5, 1, 2, 4, 8]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t:g}" for t in ticks])
    ax.axvline(1.0, color="#555555", linestyle=":", linewidth=1.0, zorder=0)
    ax.set_yticks(range(len(keys)))
    ax.set_yticklabels(labels[::-1], fontsize=8)
    ax.set_ylim(-0.7, len(keys) - 0.3)
    counts = frame["status"].astype(str).value_counts()
    handles = [Line2D([], [], marker="o", linestyle="none", markerfacecolor=STATUS_COLORS[s],
                      markeredgecolor="black", markersize=7, label=f"{s} ({int(counts.get(s, 0))})")
               for s in ("green", "amber", "red", "n/a") if counts.get(s, 0)]
    handles += [Patch(facecolor="#d9d9d9", label="literature range"),
                Patch(facecolor="#ffe0b3", label=f"within {tolerance:.0%} of a bound")]
    ax.legend(handles=handles, fontsize=8, loc="lower right", framealpha=0.9)
    setup_axes(ax, xlabel="Value / reference of the range (log scale)",
               title=title or "Plausibility against literature ranges")
    ax.grid(True, axis="x", which="major", alpha=0.3)
    ax.grid(False, axis="y")
    fig.tight_layout()
    return fig


def _length_km(frame: pd.DataFrame, names_m: Sequence[str], names_km: Sequence[str]) -> Optional[pd.Series]:
    for name in names_m:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce") / 1000.0
    for name in names_km:
        if name in frame.columns:
            return pd.to_numeric(frame[name], errors="coerce")
    return None


def plot_solver_convergence(diagnostics: pd.DataFrame, ax: Any = None, figsize: Tuple[float, float] = (12, 5),
                            by: Optional[str] = None, title: Optional[str] = None) -> Figure:
    """Total ring length after the savings step against the length after the local search, and the runtime.

    Panel 1: length after savings against length after local search per district (points below the diagonal are
    improved by the local search). Panel 2 (with a runtime column): local-search gain in percent against the
    solver runtime.

    Args:
        diagnostics: One row per district (and variant) with ``length_savings_m`` and ``length_final_m`` (keys
            of ``mv_grid.metrics``; ``*_km`` columns also work) and optionally ``solver_runtime_s`` (else
            ``runtime_s``).
        ax: None (new figure with one or two panels), one Axes (panel 1 only) or a sequence of two Axes.
        figsize: Figure size in inches if a new figure is created.
        by: Grouping column for the colours; default ``variant`` when present.
        title: Figure title.

    Returns:
        The figure that contains the plot.

    Raises:
        ValueError: If the length columns are missing.
    """
    savings = _length_km(diagnostics, ("length_savings_m",), ("length_savings_km",))
    final = _length_km(diagnostics, ("length_final_m",), ("length_final_km",))
    if savings is None or final is None:
        raise ValueError("diagnostics need length_savings_m and length_final_m (or *_km) columns")
    runtime = None
    for name in ("solver_runtime_s", "runtime_s"):
        if name in diagnostics.columns:
            runtime = pd.to_numeric(diagnostics[name], errors="coerce")
            break
    if by is None and "variant" in diagnostics.columns:
        by = "variant"
    if ax is None:
        fig, axes = plt.subplots(1, 2 if runtime is not None else 1, figsize=figsize, squeeze=False)
        axes = list(axes[0])
    elif isinstance(ax, (list, tuple, np.ndarray)):
        axes = list(np.ravel(ax))
        fig = axes[0].get_figure()
    else:
        axes = [ax]
        fig = ax.get_figure()
    groups = list(pd.unique(diagnostics[by])) if by is not None and by in diagnostics.columns else [None]
    colors = get_color_map([str(g) for g in groups])
    gain = 100.0 * (savings - final) / savings.where(savings > 0)
    for g in groups:
        mask = (diagnostics[by] == g).to_numpy() if g is not None else np.ones(len(diagnostics), dtype=bool)
        label = str(g) if g is not None else None
        axes[0].scatter(savings[mask], final[mask], s=22, color=colors[str(g)], alpha=0.8, label=label, zorder=3)
        if len(axes) > 1 and runtime is not None:
            axes[1].scatter(runtime[mask], gain[mask], s=22, color=colors[str(g)], alpha=0.8, label=label, zorder=3)
    finite = pd.concat([savings, final]).replace([np.inf, -np.inf], np.nan).dropna()
    if len(finite):
        top = float(finite.max()) * 1.05
        axes[0].plot([0, top], [0, top], color="#555555", linestyle=":", linewidth=1.0, label="no change")
        axes[0].set_xlim(0, top)
        axes[0].set_ylim(0, top)
    setup_axes(axes[0], xlabel="Ring length after the savings step (km)",
               ylabel="Ring length after the local search (km)", title="Savings and local search")
    if gain.notna().any():
        add_statistics_box(axes[0], f"median gain: {gain.median():.1f} %\nmax gain: {gain.max():.1f} %",
                           position="lower right")
    if groups != [None]:
        axes[0].legend(fontsize=8, loc="upper left")
    if len(axes) > 1 and runtime is not None:
        positive = runtime[runtime > 0]
        if len(positive) and positive.max() >= 20 * positive.min():
            axes[1].set_xscale("log")
            axes[1].xaxis.set_major_formatter(ScalarFormatter())
            axes[1].xaxis.set_minor_formatter(NullFormatter())
        setup_axes(axes[1], xlabel="Solver runtime (s)", ylabel="Local search gain (%)",
                   title="Gain and runtime per district")
    if title:
        fig.suptitle(title, fontsize=14, fontweight="bold")
    return fig
