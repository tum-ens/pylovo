"""
Feature analysis plotting functions.

This module contains functions for visualizing feature analysis, including correlation matrices,
factor analysis, PCA, and sample distributions.

The matplotlib functions return their figure and never call ``plt.show()``. A notebook with the inline backend
shows the open figure at the end of the cell: assign the result (``fig = ...``), so that it is shown once.
"""

from collections.abc import Sequence
from math import ceil
from typing import Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.express as px
import seaborn as sns
from matplotlib.figure import Figure
from sklearn.decomposition import FactorAnalysis, PCA
from sklearn.preprocessing import StandardScaler

from pylovo.config_loader import TUMPalette, TUMPalette1

#: The RegioStaR 7 classes of the Federal Ministry for Digital and Transport (BMDV), English names.
REGIOSTAR7_NAMES = {
    71: "Urban region: metropolis",
    72: "Urban region: regiopolis, large city",
    73: "Urban region: medium-sized city, urban area",
    74: "Urban region: small town, village area",
    75: "Rural region: central city",
    76: "Rural region: urban area",
    77: "Rural region: small town, village area",
}

# Identifiers of a grid in the key-figure tables: no key figures themselves.
_IDENTIFIER_COLUMNS = ("version_id", "plz", "bcid", "kcid", "grid_result_id")


def regiostar7_color(regio7) -> str:
    """Return the colour of a RegioStaR 7 class: ``TUMPalette1`` in the order of the classes 71 to 77."""
    return TUMPalette1[(int(regio7) - 71) % len(TUMPalette1)]


def plot_correlation_matrix(corr: pd.DataFrame, ax: Optional[plt.Axes] = None, figsize: Tuple[int, int] = (9, 9),
        save_path: Optional[str] = None) -> Figure:
    """
    Plot correlation matrix as a heatmap.

    Args:
        corr: Correlation matrix to visualize.
        ax: Axes object for subplot integration. If None, creates a new figure.
        figsize: Figure size in inches (width, height). Default: (9, 9).
        save_path: Path to save the figure (600 dpi). If None, the figure is not saved.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the correlation matrix plot.
    """
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()

    sns.heatmap(corr, annot=True, fmt='.2f', cmap=plt.get_cmap('coolwarm'), cbar=True, ax=ax, vmin=-1, vmax=1)
    ax.set_yticklabels(ax.get_yticklabels(), rotation='horizontal')

    if save_path:
        fig.savefig(save_path, dpi=600)

    return fig


def plot_samples_per_regiostarclass(df_samples: pd.DataFrame, ax: Optional[plt.Axes] = None,
        figsize: Tuple[int, int] = (9, 6)) -> Figure:
    """
    Plot bar chart showing number of samples per regiostar 7 class.

    Every class has the colour of :func:`regiostar7_color`, whatever classes the sample contains.

    Args:
        df_samples: DataFrame containing sample data with 'regio7' column.
        ax: Axes object for subplot integration.
        figsize: Figure size in inches (width, height). Default: (9, 6).

    Returns:
        matplotlib.figure.Figure: The Figure object containing the bar chart.
    """
    if ax is None:
        fig = plt.figure(figsize=figsize)
        ax = fig.gca()
    else:
        fig = ax.get_figure()

    counts = df_samples["regio7"].dropna().astype(int).value_counts().sort_index()
    bars = ax.bar([str(regio7) for regio7 in counts.index], counts.to_numpy(),
                  color=[regiostar7_color(regio7) for regio7 in counts.index])
    ax.bar_label(bars)
    ax.set_xlabel('RegioStaR 7 class', fontsize=18)
    ax.set_ylabel('Sampled PLZ', fontsize=18)
    ax.tick_params(axis='both', which='major', labelsize=16)

    return fig


def plot_samples_on_map(df_samples: pd.DataFrame) -> None:
    """
    Plot PLZ samples on an interactive plotly map.

    The background tiles (``carto-positron``) are loaded from the internet and need no access token.
    :func:`plot_sample_map` draws a static map without tiles.

    Args:
        df_samples: DataFrame containing sample data with 'lat', 'lon', and 'regio7' columns.
    """
    df_samples = df_samples.copy()
    df_samples['regio7_str'] = df_samples['regio7'].astype("str")
    fig = px.scatter_map(df_samples, lat="lat", lon="lon", color="regio7_str", size="regio7", size_max=10, zoom=7,
                         map_style="carto-positron")
    fig.update_layout(width=1000, height=900, margin={"r": 5, "t": 0, "l": 5, "b": 0})
    fig.show()


#: Credit line of the PLZ positions of the municipal register (suche-postleitzahl.org, derived from OpenStreetMap).
PLZ_ATTRIBUTION = "PLZ positions: © OpenStreetMap contributors (ODbL)"


def plot_sample_map(df_samples: pd.DataFrame, df_pool: Optional[pd.DataFrame] = None,
                    ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (8, 7),
                    attribution: Optional[str] = None) -> Figure:
    """
    Plot the sampled PLZ on a static map, coloured by RegioStaR 7 class.

    Every PLZ is a point at the coordinates of the municipal register (``lat``, ``lon``). The axes are longitude and
    latitude with the aspect of the mean latitude; no tiles are loaded, so the map works offline.

    Args:
        df_samples: Sampled PLZ with ``lat``, ``lon`` and ``regio7`` columns, e.g. from
            :func:`pylovo.classification.sampling.sample.get_sample_set`.
        df_pool: PLZ the sample was drawn from (same columns but ``regio7``), drawn as light grey points behind the
            sample. None draws the sample only.
        ax: Axes to draw into. If None, creates a new figure.
        figsize: Figure size in inches if a new figure is created.
        attribution: Credit line in the lower right corner of the map; default :data:`PLZ_ATTRIBUTION`, ``""`` for
            none.

    Returns:
        matplotlib.figure.Figure: The figure that contains the map.
    """
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()

    if df_pool is not None:
        ax.scatter(df_pool["lon"], df_pool["lat"], s=5, color="#c8c8c8", linewidths=0, zorder=1,
                   label=f"PLZ of the region ({len(df_pool)})")
    samples = df_samples.dropna(subset=["regio7"])
    for regio7, group in samples.groupby(samples["regio7"].astype(int)):
        ax.scatter(group["lon"], group["lat"], s=24, color=regiostar7_color(regio7), edgecolors="#333333",
                   linewidths=0.5, zorder=2,
                   label=f"{regio7} {REGIOSTAR7_NAMES.get(regio7, '')} ({len(group)})")

    latitudes = (df_pool if df_pool is not None and len(df_pool) else samples)["lat"]
    ax.set_aspect(1 / np.cos(np.radians(latitudes.mean())))
    ax.set_xlabel("Longitude (°)")
    ax.set_ylabel("Latitude (°)")
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.1), frameon=False, fontsize=9)
    text = PLZ_ATTRIBUTION if attribution is None else attribution
    if text:
        ax.text(0.995, 0.006, text, transform=ax.transAxes, ha="right", va="bottom", fontsize=6.3, color="#555555")

    return fig


def plot_key_figure_distributions(df_parameters: pd.DataFrame, columns: Optional[Sequence[str]] = None,
                                  ncols: int = 4, bins: int = 40, upper_quantile: Optional[float] = 0.99,
                                  figsize: Optional[Tuple[float, float]] = None) -> Figure:
    """
    Plot one histogram per key figure (small multiples) with the median as a dashed line.

    Args:
        df_parameters: Key figures, one row per grid.
        columns: Key figures to plot. None plots every numeric column except the identifiers
            (``version_id``, ``plz``, ``bcid``, ``kcid``, ``grid_result_id``).
        ncols: Number of panels per row.
        bins: Number of bins of each histogram.
        upper_quantile: The x axis ends at this quantile, so that a few extreme grids do not squeeze the rest; the
            number of grids above it is written in the panel. None shows the whole range.
        figsize: Figure size in inches. None gives about 3.2 x 2.4 inches per panel.

    Returns:
        matplotlib.figure.Figure: The figure with the histograms.
    """
    if columns is None:
        columns = [column for column in df_parameters.select_dtypes(include="number").columns
                   if column not in _IDENTIFIER_COLUMNS]
    columns = list(columns)
    nrows = max(1, ceil(len(columns) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=figsize or (3.2 * ncols, 2.4 * nrows), squeeze=False)

    for ax, column in zip(axes.flat, columns):
        values = pd.to_numeric(df_parameters[column], errors="coerce").dropna()
        upper = values.quantile(upper_quantile) if upper_quantile is not None and len(values) else None
        shown = values if upper is None else values[values <= upper]
        ax.hist(shown, bins=bins, color=TUMPalette[0])
        note = ""
        if len(values):
            median = values.median()
            ax.axvline(median, color="black", linewidth=1, linestyle="--")
            note = f"median {median:.3g}"
        if len(shown) < len(values):
            note += f"\n{len(values) - len(shown)} above {upper:.3g} not shown"
        ax.text(0.98, 0.95, note, transform=ax.transAxes, ha="right", va="top", fontsize=8)
        ax.set_title(column, fontsize=10)
        ax.tick_params(labelsize=8)
    for ax in axes.flat[len(columns):]:
        ax.set_visible(False)
    for ax in axes[:, 0]:
        ax.set_ylabel("Grids")
    fig.tight_layout()

    return fig


#: Iteration limit of the factor analyses (:func:`varimax_factor_analysis`, :func:`plot_factor_analysis`)
FACTOR_ANALYSIS_MAX_ITER = 10_000


def plot_factor_analysis(df_plz_parameters: pd.DataFrame, n_comps: int, figsize: Tuple[int, int] = (10, 8)) -> Figure:
    """
    Plot factor analysis comparison for different methods.

    One panel per method (PCA, factor analysis without and with varimax rotation) with the loadings of every
    parameter on every factor (1 to ``n_comps``), red positive and blue negative.

    Args:
        df_plz_parameters: Set of parameters for grids.
        n_comps: Number of components for factor analysis.
        figsize: Figure size in inches (width, height). Default: (10, 8).

    Returns:
        matplotlib.figure.Figure: The Figure object containing the factor analysis plots.
    """
    # Scale data
    data = df_plz_parameters
    X = StandardScaler().fit_transform(data)
    feature_names = data.columns

    # Define methods
    methods = [("PCA", PCA()), ("Unrotated FA", FactorAnalysis(max_iter=FACTOR_ANALYSIS_MAX_ITER)),
               ("Varimax FA", FactorAnalysis(rotation="varimax", max_iter=FACTOR_ANALYSIS_MAX_ITER))]
    fig, axes = plt.subplots(ncols=len(methods), figsize=figsize, sharey=True, layout="constrained")

    # Plot each method
    image = None
    for ax, (method, fa) in zip(axes, methods):
        fa.set_params(n_components=n_comps)
        fa.fit(X)
        components = fa.components_.T

        vmax = 1
        image = ax.imshow(components, cmap="RdBu_r", vmax=vmax, vmin=-vmax)
        ax.set_yticks(np.arange(len(feature_names)))
        ax.set_yticklabels(feature_names)
        ax.set_title(str(method))
        ax.set_xticks(range(0, n_comps), [str(factor) for factor in range(1, n_comps + 1)])
        ax.set_xlabel("Factor")

    fig.colorbar(image, ax=list(axes), shrink=0.6, label="Loading")
    fig.suptitle("Factors")

    return fig


def varimax_factor_analysis(df_plz_parameters: pd.DataFrame, n_comps: int) -> tuple[pd.DataFrame, FactorAnalysis]:
    """
    Fit the varimax-rotated factor analysis of the standardised parameters.

    The exact SVD (``svd_method="lapack"``) gives the loadings of the randomized default, only faster. With a
    parameter that the factors explain fully (a Heywood case) the fit needs some 1,600 iterations, more than
    scikit-learn's default limit of 1,000, so the limit is :data:`FACTOR_ANALYSIS_MAX_ITER`.

    Args:
        df_plz_parameters: Set of parameters for grids.
        n_comps: Number of factors.

    Returns:
        tuple: The loadings (one row per parameter, one column per factor) and the fitted model (``noise_variance_``
        holds the uniqueness of every parameter, ``n_iter_`` the iterations).
    """
    fa = FactorAnalysis(n_components=n_comps, rotation="varimax", svd_method="lapack", max_iter=FACTOR_ANALYSIS_MAX_ITER)
    fa.fit(StandardScaler().fit_transform(df_plz_parameters))
    return pd.DataFrame(fa.components_.T, index=df_plz_parameters.columns), fa


def get_parameters_for_clustering(df_plz_parameters: pd.DataFrame, n_comps: int) -> list:
    """
    Calculate mathematically ideal set of parameters using varimax rotated factor analysis.

    Args:
        df_plz_parameters: Set of parameters for grids.
        n_comps: Number of components for factor analysis.

    Returns:
        list: The parameter with the largest absolute loading of every factor (:func:`varimax_factor_analysis`).
    """
    df_components_fa, _ = varimax_factor_analysis(df_plz_parameters, n_comps)
    parameters = []
    for column in df_components_fa:
        parameter = df_components_fa[column].abs().idxmax()
        parameters.append(parameter)
        print(parameter)

    return parameters


def plot_eigendecomposition(df_plz_parameters: pd.DataFrame, figsize: Tuple[int, int] = (10, 6),
                            ax: Optional[plt.Axes] = None, kaiser_line: bool = True) -> Figure:
    """
    Plot explained variance of principal components.

    The share of a component is its eigenvalue of the correlation matrix divided by the number of parameters, so the
    dashed line at ``1 / number of parameters`` marks the eigenvalue 1: ``pylovo-classify`` keeps one factor per
    eigenvalue above it.

    Args:
        df_plz_parameters: Set of parameters for grids.
        figsize: Figure size in inches (width, height). Default: (10, 6).
        ax: Axes to draw into. If None, creates a new figure.
        kaiser_line: Draw the line of the eigenvalue 1.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the eigendecomposition plot.
    """
    X_train = df_plz_parameters
    sc = StandardScaler()
    sc.fit(X_train)
    X_train_std = sc.transform(X_train)

    pca = PCA()
    pca.fit(X_train_std)
    exp_var_pca = pca.explained_variance_ratio_
    cum_sum_eigenvalues = np.cumsum(exp_var_pca)
    components = np.arange(1, len(exp_var_pca) + 1)

    own_figure = ax is None
    if own_figure:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()
    ax.bar(components, exp_var_pca, alpha=0.5, align='center', label='Explained variance of each component')
    ax.step(components, cum_sum_eigenvalues, where='mid', label='Cumulative explained variance')
    if kaiser_line:
        ax.axhline(1 / len(exp_var_pca), color="black", linewidth=1, linestyle="--",
                   label="Eigenvalue 1 (one factor per component above)")
    ax.set_xticks(components)
    ax.set_ylabel('Share of the explained variance')
    ax.set_xlabel('Principal component')
    ax.legend(loc='best')
    if own_figure:
        fig.tight_layout()

    return fig
