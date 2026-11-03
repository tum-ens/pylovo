"""Figures of the comparison of two grid datasets (:mod:`pylovo.analysis.comparison`): reference against candidate.

Every figure takes the per-grid tables of :func:`pylovo.analysis.comparison.grid_metrics`, keyed by the name of their
dataset, and the colours of :func:`group_colors`. The metric figures follow the matrix of
:data:`~pylovo.analysis.comparison.METRICS`: two categories as columns, four rows.

- :func:`plot_boxplots`, :func:`plot_histograms`: one box or histogram per dataset and metric, for any number of
  datasets.
- :func:`plot_distributions`: box plots above the distribution functions of reference and candidate, with the
  Wasserstein-1 distance shaded between them.
- :func:`plot_correlations`: rank correlations between the metrics in both datasets and their difference.
- :func:`plot_cable_shares`: installed cable length per equivalent cross-section.
- :func:`plot_score_matrix`: W̃1 of several candidates against one reference.
"""
from __future__ import annotations

import textwrap
from typing import Mapping, Optional, Sequence

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap, to_rgba
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter, LogLocator, NullLocator
from scipy.stats import gaussian_kde

from pylovo.analysis.comparison import CATEGORIES, METRICS, REFERENCE_CABLES, ROWS, Metric, metric_values
from pylovo.plotting import style

__all__ = ["group_colors", "plot_boxplots", "plot_cable_shares", "plot_correlations", "plot_distributions",
           "plot_histograms", "plot_score_matrix"]

#: The reference in red, the candidates in the other categorical colours of pylovo.
REFERENCE_COLOR = style.CATEGORICAL[7]
CANDIDATE_COLORS = [style.CATEGORICAL[i] for i in (0, 2, 3, 6, 4, 5)]
#: One fill transparency for bars, boxes and legend patches.
FILL_ALPHA = .28
#: Colour classes of W̃1 (upper bound, colour, label), and grey within sampling noise.
W1_CLASSES = ((.25, "#f3a068", r"$\tilde{W}_1 < 0.25$"), (.5, "#eb6834", r"$0.25$ to $0.5$"),
              (np.inf, "#b04a1a", r"$\geq 0.5$"))
NOISE_COLOR = style.BASELINE
#: Diverging colour map of the correlations: blue negative, orange positive.
DIVERGING = LinearSegmentedColormap.from_list(
    "pylovo_diverging", [style.SEQUENTIAL_BLUE[4], style.SEQUENTIAL_BLUE[0], style.SURFACE,
                         style.SEQUENTIAL_ORANGE[0], style.SEQUENTIAL_ORANGE[4]])


def group_colors(names: Sequence[str], reference: str) -> dict[str, str]:
    """``{name: colour}``: the reference red, the others in a fixed order."""
    others = [n for n in dict.fromkeys(names) if n != reference]
    return {reference: REFERENCE_COLOR, **{n: CANDIDATE_COLORS[i % len(CANDIDATE_COLORS)]
                                           for i, n in enumerate(others)}}


def _display_values(frame: pd.DataFrame, metric: Metric) -> np.ndarray:
    return metric_values(frame, metric) * metric.factor


def _panels(metrics: Sequence[Metric], title: Optional[str], height: float = 2.5):
    """The metric matrix: one column per category, one row per ``ROWS`` entry; returns the figure and
    ``[(axis, metric)]``."""
    columns = [c for c in CATEGORIES if any(m.category == c for m in metrics)]
    rows = [r for r in ROWS if any(m.row == r for m in metrics)]
    fig, axes = plt.subplots(len(rows), len(columns), figsize=(6.4 * len(columns) + .6, height * len(rows) + 1.2),
                             layout="constrained", squeeze=False)
    if title:
        fig.suptitle(title, x=0.01, ha="left", fontweight="bold", fontsize=11)
    used, panels = set(), []
    for metric in metrics:
        i, j = rows.index(metric.row), columns.index(metric.category)
        ax = axes[i, j]
        used.add((i, j))
        meaning = textwrap.fill(metric.meaning, 90)
        ax.set_title(metric.name, pad=6 + 10 * (meaning.count("\n") + 1))
        ax.text(0, 1.02, meaning, transform=ax.transAxes, va="bottom", fontsize=7.5, color=style.INK_MUTED)
        if i == 0:
            ax.annotate(f"{metric.category}: {CATEGORIES[metric.category].lower()}", xy=(0, 1), xycoords="axes fraction",
                        xytext=(0, 34), textcoords="offset points", fontsize=10, fontweight="bold",
                        color=style.INK_SECONDARY)
        if j == 0:
            ax.annotate(metric.row, xy=(0, .5), xycoords="axes fraction", xytext=(-52, 0), textcoords="offset points",
                        rotation=90, ha="center", va="center", fontsize=10, fontweight="bold",
                        color=style.INK_SECONDARY)
        panels.append((ax, metric))
    for cell, ax in np.ndenumerate(axes):
        if cell not in used:
            ax.remove()
    return fig, panels


def _legend(fig, groups: Mapping[str, pd.DataFrame], colors: Mapping[str, str], lines: bool = False) -> None:
    """One legend for all panels, above them: each dataset with its number of grids."""
    if lines:
        handles = [Line2D([0], [0], color=colors[name], linewidth=2, label=f"{name} ({len(frame)} grids)")
                   for name, frame in groups.items()]
    else:
        handles = [Patch(facecolor=to_rgba(colors[name], FILL_ALPHA), edgecolor=colors[name],
                         label=f"{name} ({len(frame)} grids)") for name, frame in groups.items()]
    fig.legend(handles=handles, loc="outside upper right", ncols=len(handles), fontsize=9)


def plot_boxplots(groups: Mapping[str, pd.DataFrame], colors: Mapping[str, str], metrics: Sequence[Metric] = METRICS,
                  show_outliers: bool = False, title: Optional[str] = None):
    """One box per dataset and metric: the middle half of the grids, the median as a line.

    Whiskers reach the last grids within 1.5 interquartile ranges; ``show_outliers`` draws the grids beyond them.
    """
    fig, panels = _panels(metrics, title)
    names = list(groups)
    for ax, metric in panels:
        for i, name in enumerate(names):
            values, color = _display_values(groups[name], metric), colors[name]
            if not len(values):
                continue
            ax.boxplot(values, positions=[i], widths=.5, patch_artist=True, showfliers=show_outliers,
                       boxprops={"facecolor": to_rgba(color, FILL_ALPHA), "edgecolor": color},
                       whiskerprops={"color": color}, capprops={"color": color}, medianprops={"color": style.INK},
                       flierprops={"marker": "o", "markersize": 2.5, "markerfacecolor": color,
                                   "markeredgecolor": "none", "alpha": .6})
        ax.set_xticks(range(len(names)), names)
        ax.tick_params(axis="x", length=0)
        ax.set_xlim(-.6, len(names) - .4)
        ax.set_ylim(bottom=0)
        ax.set_ylabel(metric.unit)
        style.tidy_value_axes(ax, "y")
    _legend(fig, groups, colors)
    return fig


def _w1_color(s: Mapping) -> str:
    """Grey within sampling noise, else the :data:`W1_CLASSES` colour of W̃1."""
    if s["verdict"] == "same":
        return NOISE_COLOR
    return next(color for bound, color, _ in W1_CLASSES if s["normalized"] < bound)


def _ecdf(values: np.ndarray, grid: np.ndarray) -> np.ndarray:
    return np.searchsorted(np.sort(values), grid, side="right") / len(values)


def plot_distributions(groups: Mapping[str, pd.DataFrame], colors: Mapping[str, str], scores: pd.DataFrame,
                       metrics: Sequence[Metric] = METRICS, upper_percentile: float = .975,
                       log_metrics: Sequence[str] = ("transformer_capacity_per_building_kva",),
                       title: Optional[str] = None):
    """Box plots above the empirical distribution functions of reference and candidate (``groups`` in this order).

    The area between the two curves is the Wasserstein-1 distance within the displayed range, shaded grey within
    sampling noise and otherwise in the :data:`W1_CLASSES` colour of W̃1 (``scores`` of
    :func:`pylovo.analysis.comparison.compare`). The lower right gives W̃1 and, in brackets, W1 over the full range in
    the unit of the metric. Each axis ends at the larger ``upper_percentile`` of the two; ``log_metrics`` get a
    logarithmic axis, which distorts the shaded area.
    """
    if len(groups) != 2:
        raise ValueError("plot_distributions compares two datasets: the reference and one candidate")
    fig, panels = _panels(metrics, title)
    step = .12
    for ax, metric in panels:
        samples = [(name, _display_values(frame, metric)) for name, frame in groups.items()]
        if not all(len(values) for _, values in samples):
            ax.text(.5, .5, "No valid values", transform=ax.transAxes, ha="center")
            continue
        limit = max(float(np.quantile(values, upper_percentile)) for _, values in samples)
        low = min(float(values.min()) for _, values in samples)
        for i, (name, values) in enumerate(samples):
            color = colors[name]
            ax.boxplot(values, positions=[1 + step * (len(samples) - i)], widths=.75 * step, orientation="horizontal",
                       patch_artist=True, showfliers=False, manage_ticks=False,
                       boxprops={"facecolor": to_rgba(color, FILL_ALPHA), "edgecolor": color},
                       whiskerprops={"color": color}, capprops={"color": color}, medianprops={"color": style.INK})
            x = np.sort(values)
            ax.step(np.concatenate(([low], x, [max(limit, x[-1]) * 1.001])),
                    np.concatenate(([0.], np.arange(1, len(x) + 1) / len(x), [1.])),
                    where="post", color=color, linewidth=1.8, zorder=3)
        s = scores.loc[metric.key]
        fill = _w1_color(s)
        reference_values, candidate_values = (values for _, values in samples)
        grid = np.unique(np.concatenate((reference_values, candidate_values, [limit])))
        ax.fill_between(grid, _ecdf(reference_values, grid), _ecdf(candidate_values, grid), step="post", color=fill,
                        linewidth=0, zorder=2)
        ax.add_patch(Rectangle((.975, .07), .022, .06, transform=ax.transAxes, facecolor=fill,
                               edgecolor=style.INK_MUTED, linewidth=.5, zorder=4))
        ax.text(.96, .1, rf"$\tilde{{W}}_1 = {s['normalized']:.2f}$ ({s['wasserstein'] * metric.factor:.3g} "
                         f"{metric.unit})", transform=ax.transAxes, ha="right", va="center", fontsize=9, zorder=4,
                color=style.INK if s["verdict"] != "same" else style.INK_MUTED)
        ax.set_ylim(0, 1 + step * (len(samples) + 1))
        ax.set_yticks([0, .5, 1], ["0", "0.5", "1"])
        ax.spines["left"].set_bounds(0, 1)
        ax.axhline(.5, color=style.GRID, linewidth=.8, zorder=1)
        if metric.key in log_metrics and 0 < low < limit:
            ax.set_xscale("log")
            ax.set_xlim(low, limit)
            ax.xaxis.set_major_locator(LogLocator(subs=(1, 2, 5)))
            ax.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
            ax.xaxis.set_minor_locator(NullLocator())
        else:
            span = (limit - low) or max(abs(low), 1.)
            ax.set_xlim(max(0., low - .02 * span), limit if limit > low else limit + .02 * span)
        ax.set_xlabel(metric.unit)
        ax.set_ylabel("share of grids up to")
    _legend(fig, groups, colors, lines=True)
    keys = [Patch(facecolor=NOISE_COLOR, edgecolor=style.INK_MUTED, label="within sampling noise")]
    keys += [Patch(facecolor=color, edgecolor=style.INK_MUTED, label=label) for _, color, label in W1_CLASSES]
    fig.legend(handles=keys, loc="outside lower center", ncols=4, fontsize=9)
    return fig


def plot_histograms(groups: Mapping[str, pd.DataFrame], colors: Mapping[str, str], metrics: Sequence[Metric] = METRICS,
                    bins: int = 8, upper_percentile: float = .99, title: Optional[str] = None):
    """Histograms with shared bins, each dataset normalized to its own share of grids, and smoothed curves.

    The pooled values above ``upper_percentile`` are left out, so a few large grids cannot squeeze every distribution
    into the first bin. The curves are kernel density estimates (Scott bandwidth) scaled to the bars, a visual guide
    only; a dataset with one value gets a dashed line. More than two datasets are drawn as outlines.
    """
    outlined = len(groups) > 2
    fig, panels = _panels(metrics, title)
    for ax, metric in panels:
        samples = [(name, _display_values(frame, metric)) for name, frame in groups.items()]
        available = [values for _, values in samples if len(values)]
        if not available:
            ax.text(.5, .5, "No valid values", transform=ax.transAxes, ha="center")
            continue
        limit = np.quantile(np.concatenate(available), upper_percentile)
        samples = [(name, values[values <= limit]) for name, values in samples]
        pooled = np.concatenate([values for _, values in samples])
        edges = np.histogram_bin_edges(pooled, bins=bins)
        width = edges[1] - edges[0]
        grid = np.linspace(max(0., float(pooled.min())), float(pooled.max()), 256)    # one grid for all curves
        for name, values in samples:
            if not len(values):
                continue
            color, weights = colors[name], np.full(len(values), 1 / len(values))
            if outlined:
                ax.hist(values, bins=edges, weights=weights, histtype="step", color=color, linewidth=1, alpha=.7)
            else:
                ax.hist(values, bins=edges, weights=weights, color=color, alpha=FILL_ALPHA, edgecolor=color)
            if len(values) >= 2 and np.ptp(values) > 0:
                ax.plot(grid, gaussian_kde(values)(grid) * width, color=color, linewidth=1.8)
            else:
                ax.axvline(values[0], color=color, linestyle="--")
        padding = (edges[-1] - edges[0]) * .05
        ax.set_xlim(max(0., edges[0] - padding), edges[-1] + padding)
        ax.set_xlabel(metric.unit)
        ax.set_ylabel("share of grids")
        style.tidy_value_axes(ax, "y")
    _legend(fig, groups, colors, lines=outlined)
    return fig


def plot_correlations(groups: Mapping[str, pd.DataFrame], metrics: Sequence[Metric] = METRICS,
                      method: str = "spearman"):
    """Rank correlations between the metrics over the grids of reference and candidate (``groups`` in this order),
    and their difference, candidate minus reference. Each matrix shows its lower triangle."""
    if len(groups) != 2:
        raise ValueError("plot_correlations compares two datasets: the reference and one candidate")
    (reference_name, reference), (candidate_name, candidate) = groups.items()
    keys = [m.key for m in metrics]
    matrices = [frame[keys].apply(pd.to_numeric, errors="coerce").corr(method=method).to_numpy()
                for frame in (reference, candidate)]
    matrices.append(matrices[1] - matrices[0])
    titles = [f"reference {reference_name}", f"candidate {candidate_name}",
              f"difference {candidate_name} − {reference_name}"]
    names = [m.name for m in metrics]
    fig, axes = plt.subplots(1, 3, figsize=(17, 5.6), layout="constrained")
    for k, (ax, matrix, panel_title) in enumerate(zip(axes, matrices, titles)):
        shown = np.where(np.tril(np.ones_like(matrix, dtype=bool), k=-1), matrix, np.nan)[1:, :-1]
        image = ax.imshow(shown, cmap=DIVERGING, vmin=-1, vmax=1)
        for (i, j), value in np.ndenumerate(shown):
            if np.isfinite(value):
                ax.text(j, i, f"{value:.2f}", ha="center", va="center", fontsize=7.5,
                        color="white" if abs(value) > .6 else style.INK)
        ax.set_xticks(range(len(names) - 1), names[:-1], rotation=40, ha="right", fontsize=8)
        ax.set_yticks(range(len(names) - 1), names[1:] if k == 0 else [""] * (len(names) - 1), fontsize=8)
        ax.spines[:].set_visible(False)
        ax.tick_params(length=0)
        ax.set_title(panel_title)
    fig.colorbar(image, ax=axes, shrink=.7, label=f"{method.title()} correlation (difference: same scale)")
    return fig


def plot_cable_shares(tables: Mapping[str, pd.DataFrame], colors: Mapping[str, str]):
    """Share of installed cable length per equivalent aluminium cross-section, for feeders and house services
    (tables of :func:`pylovo.analysis.comparison.cable_table`)."""
    kinds = [k for k in ("feeders", "house services") if any((t["kind"] == k).any() for t in tables.values())]
    sections = [row[0] for row in REFERENCE_CABLES]
    fig, axes = plt.subplots(1, len(kinds), figsize=(7.2 * len(kinds), 3.6), layout="constrained", squeeze=False)
    width = .8 / len(tables)
    for ax, kind in zip(axes.flat, kinds):
        for i, (name, table) in enumerate(tables.items()):
            part = table[table["kind"] == kind]
            shares = part.groupby("cross_section")["km"].sum().reindex(sections, fill_value=0) / part["km"].sum()
            ax.bar(np.arange(len(sections)) + (i - (len(tables) - 1) / 2) * width, 100 * shares, width=width,
                   color=to_rgba(colors[name], .75), edgecolor=colors[name], label=name)
        ax.set_xticks(range(len(sections)), sections)
        ax.set_xlabel("cross-section of the aluminium cable with the same resistance (mm²)")
        ax.set_ylabel("share of installed length (%)")
        ax.set_title(kind.capitalize())
        style.tidy_value_axes(ax, "y")
    axes.flat[0].legend(loc="upper left")
    return fig


def plot_score_matrix(scores: Mapping[str, pd.DataFrame], metrics: Sequence[Metric] = METRICS,
                      title: Optional[str] = None):
    """W̃1 of every candidate (columns, ``scores`` of :func:`pylovo.analysis.comparison.compare`) and metric (rows),
    coloured as in :func:`plot_distributions`."""
    names = list(scores)
    fig, ax = plt.subplots(figsize=(1.5 * len(names) + 3.4, .42 * len(metrics) + 1.4), layout="constrained")
    for j, name in enumerate(names):
        for i, metric in enumerate(metrics):
            s = scores[name].loc[metric.key]
            fill = _w1_color(s)
            ax.add_patch(Rectangle((j - .5, i - .5), 1, 1, facecolor=fill, edgecolor=style.SURFACE, linewidth=2))
            ax.text(j, i, f"{s['normalized']:.2f}", ha="center", va="center", fontsize=9,
                    color="white" if fill == W1_CLASSES[-1][1] else style.INK)
    ax.set_xlim(-.5, len(names) - .5)
    ax.set_ylim(len(metrics) - .5, -.5)
    ax.set_xticks(range(len(names)), names)
    ax.xaxis.tick_top()
    ax.set_yticks(range(len(metrics)), [m.name for m in metrics])
    ax.spines[:].set_visible(False)
    ax.tick_params(length=0)
    if title:
        ax.set_title(title, pad=24)
    keys = [Patch(facecolor=NOISE_COLOR, label="within sampling noise")]
    keys += [Patch(facecolor=color, label=label) for _, color, label in W1_CLASSES]
    fig.legend(handles=keys, loc="outside lower center", ncols=4, fontsize=8)
    return fig
