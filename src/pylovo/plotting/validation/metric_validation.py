"""
Plots of synthetic/real metric comparisons (plotly).

They take a metrics table with a ``source`` column. The statistics of the grids of postcode areas are in
:mod:`pylovo.plotting.lv_grid` (``plot_rating_counts``, ``plot_cable_inventory``, ``plot_grid_distributions``).
"""

from typing import Optional, List

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from pylovo.plotting.utils import get_color_map


# -----------------------------------------------------------------------------
# PLOTLY / INTERACTIVE PLOTS
# -----------------------------------------------------------------------------

def plot_comparison_distribution_plotly(
    df: pd.DataFrame,
    metric_col: str,
    title: Optional[str] = None,
    hover_data: Optional[List[str]] = None,
    plot_type: str = "box"
) -> go.Figure:
    """Generate a distribution plot (box, violin or strip) of a metric per source.

    Args:
        df: Metrics table with a ``source`` column.
        metric_col: Column of the metric to plot.
        title: Chart title. Defaults to the metric name.
        hover_data: Columns shown on hover. Defaults to those of
            ``grid_result_id``, ``kcid`` and ``bcid`` that exist.
        plot_type: ``"box"``, ``"violin"`` or ``"strip"``.

    Returns:
        The Plotly figure (a "No Data Available" annotation if ``df`` is empty).

    Raises:
        ValueError: If ``plot_type`` is unknown.
    """
    if df.empty:
        return go.Figure().add_annotation(text="No Data Available", showarrow=False)

    if hover_data is None:
        hover_data = ["grid_result_id", "kcid", "bcid"]
        # Filter to only existing columns
        hover_data = [c for c in hover_data if c in df.columns]

    sources = df["source"].unique()
    color_discrete_map = get_color_map(sources)

    common_args = {
        "data_frame": df,
        "x": "source",
        "y": metric_col,
        "color": "source",
        "color_discrete_map": color_discrete_map,
        "hover_data": hover_data,
        "title": title or f"Distribution of {metric_col}",
        "template": "plotly_white"
    }

    if plot_type == "box":
        fig = px.box(**common_args, points="all") # points="all" adds strip plot next to box
    elif plot_type == "violin":
        fig = px.violin(**common_args, box=True, points="all")
    elif plot_type == "strip":
        fig = px.strip(**common_args)
    else:
        raise ValueError(f"Unknown plot_type: {plot_type}")

    # Layout improvements
    y_max = df[metric_col].max()
    fig.update_layout(
        xaxis_title="Grid Source",
        yaxis_title=metric_col.replace("_", " ").title(),
        yaxis_range=[0, y_max * 1.1],
        legend_title="Source",
        font=dict(family="Arial", size=14),
        hovermode="closest"
    )

    return fig


def plot_comparison_histogram_plotly(
    df: pd.DataFrame,
    metric_col: str,
    title: Optional[str] = None,
    histnorm: str = "probability",
) -> go.Figure:
    """Generate overlaid histograms of a metric per source, with marginal boxplots.

    Args:
        df: Metrics table with a ``source`` column.
        metric_col: Column of the metric to plot.
        title: Plot title. Defaults to the metric name.
        histnorm: Plotly histogram normalization. The default ``"probability"``
            shows each source as share of grids rather than raw grid count.

    Returns:
        The Plotly figure (a "No Data Available" annotation if ``df`` is empty).
    """
    if df.empty:
        return go.Figure().add_annotation(text="No Data Available", showarrow=False)

    sources = df["source"].unique()
    color_discrete_map = get_color_map(sources)

    fig = px.histogram(
        df,
        x=metric_col,
        color="source",
        barmode="overlay",
        marginal="box", # Adds small boxplot on top
        color_discrete_map=color_discrete_map,
        title=title or f"Histogram of {metric_col}",
        template="plotly_white",
        opacity=0.6,
        histnorm=histnorm,
    )

    yaxis_title = "Share of Grids" if histnorm == "probability" else "Density" if histnorm else "Count"
    fig.update_layout(
        xaxis_title=metric_col.replace("_", " ").title(),
        yaxis_title=yaxis_title,
        legend_title="Source",
        font=dict(family="Arial", size=14)
    )

    return fig


def plot_comparison_scatter_plotly(
    df: pd.DataFrame,
    x_col: str,
    y_col: str,
    size_col: Optional[str] = None,
    title: Optional[str] = None
) -> go.Figure:
    """Generate a scatter plot of two metrics, colored by source.

    Args:
        df: Metrics table with a ``source`` column.
        x_col: Column on the x axis.
        y_col: Column on the y axis.
        size_col: Column that sets the marker size.
        title: Plot title. Defaults to ``"<y_col> vs <x_col>"``.

    Returns:
        The Plotly figure (a "No Data Available" annotation if ``df`` is empty).
    """
    if df.empty:
        return go.Figure().add_annotation(text="No Data Available", showarrow=False)

    sources = df["source"].unique()
    color_discrete_map = get_color_map(sources)

    hover_data = [c for c in ["grid_result_id", "kcid", "bcid"] if c in df.columns]

    fig = px.scatter(
        df,
        x=x_col,
        y=y_col,
        size=size_col,
        color="source",
        color_discrete_map=color_discrete_map,
        hover_data=hover_data,
        title=title or f"{y_col} vs {x_col}",
        template="plotly_white",
        opacity=0.7
    )

    return fig
