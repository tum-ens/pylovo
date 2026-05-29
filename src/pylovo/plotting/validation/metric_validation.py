"""
Spatial/geographic plotting functions.

This module contains functions for visualizing geographic data related to
postal codes (PLZ), including transformer distributions, cable types, and
grid statistics.
"""

import json
from pathlib import Path
from typing import Tuple, Optional, List

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import numpy as np
import pandas as pd
import pandapower as pp
import plotly
import plotly.express as px
import plotly.graph_objects as go
from pandapower.plotting.plotly import vlevel_plotly
from pandapower.plotting.plotly.mapbox_plot import set_mapbox_token
from scipy import stats


from pylovo.config_loader import RESULT_DIR, VERSION_ID
from pylovo.plotting.utils import get_color_map

# Try to import config, but don't fail if it doesn't exist
try:
    from pylovo.plotting import ACCESS_TOKEN_PLOTLY, PLOT_COLOR_DICT
    px.set_mapbox_access_token(ACCESS_TOKEN_PLOTLY)
except ImportError:
    ACCESS_TOKEN_PLOTLY = None
    PLOT_COLOR_DICT = {}


def plot_pie_of_trafo_cables(plz: int, figsize: Tuple[int, int] = (16, 4)) -> Figure:
    """
    Plot pie charts showing transformer size and cable type distributions for a postal code.

    Parameters
    ----------
    plz : int
        Postal code.
    figsize : tuple of int, optional
        Figure size in inches (width, height). Default: (16, 4).

    Returns
    -------
    matplotlib.figure.Figure
        The Figure object containing the pie charts.
    """
    from pylovo.grid_generator import GridGenerator
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc
    data_list, data_labels, trafo_dict = dbc_client.read_per_trafo_dict(plz=plz)

    fig, axs = plt.subplots(nrows=1, ncols=2, figsize=figsize)

    # Plot Transformer size distribution
    axs[0].pie(trafo_dict.values(), labels=trafo_dict.keys(), autopct='%1.1f%%',
               pctdistance=1.15, labeldistance=.6)
    axs[0].set_title('Transformer Size Distribution', fontsize=14)

    # Plot cable length distribution
    cable_dict = dbc_client.read_cable_dict(plz)
    axs[1].pie(cable_dict.values(), labels=cable_dict.keys(), autopct="%.1f%%")
    axs[1].set_title("Installed Cable Length", fontsize=14)
    plt.show()

    return fig


def plot_hist_trafos(plz: int, figsize: Tuple[int, int] = (10, 6)) -> Figure:
    """
    Plot histogram of transformer sizes in a postal code.

    Parameters
    ----------
    plz : int
        Postal code.
    figsize : tuple of int, optional
        Figure size in inches (width, height). Default: (10, 6).

    Returns
    -------
    matplotlib.figure.Figure
        The Figure object containing the histogram.
    """
    from pylovo.grid_generator import GridGenerator
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc
    data_list, data_labels, trafo_dict = dbc_client.read_per_trafo_dict(plz=plz)

    fig, ax = plt.subplots(figsize=figsize)
    ax.bar(trafo_dict.keys(), height=trafo_dict.values(), width=0.3)
    ax.set_title('Transformer Size Distribution', fontsize=14)
    ax.set_xlabel("Trafo size")
    ax.set_ylabel("Count")
    plt.show()

    return fig


def plot_boxplot_plz(plz: int, figsize: Tuple[int, int] = (16, 4)) -> Figure:
    """
    Create boxplots of grid parameters grouped by transformer size.

    Shows distribution of load numbers, bus numbers, simultaneous load peak,
    max/avg transformer distance for each transformer size category.

    Parameters
    ----------
    plz : int
        Postal code.
    figsize : tuple of int, optional
        Figure size in inches (width, height). Default: (16, 4).

    Returns
    -------
    matplotlib.figure.Figure
        The Figure object containing the boxplots.
    """
    from pylovo.grid_generator import GridGenerator
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc
    data_list, data_labels, trafo_dict = dbc_client.read_per_trafo_dict(plz=plz)
    trafo_sizes = list(data_list[0].keys())
    values = [list(d.values()) for d in data_list]

    # Create the figure and axes objects
    fig, axs = plt.subplots(nrows=1, ncols=len(data_list), figsize=figsize, sharey=True)

    for i, data_label in enumerate(data_labels):
        axs[i].boxplot(values[i], labels=trafo_sizes, vert=False,
                       showfliers=False, patch_artist=True, notch=False)
        axs[i].set_title(data_label, fontsize=12)

    fig.supxlabel('Values', fontsize=12)
    fig.supylabel('Transformer Size (kVA)', fontsize=12)
    plt.tight_layout()
    plt.show()

    return fig


def plot_cable_length_of_types(plz: int, figsize: Tuple[int, int] = (10, 6)) -> Figure:
    """
    Plot distribution of cable length by cable type.

    Parameters
    ----------
    plz : int
        Postal code.
    figsize : tuple of int, optional
        Figure size in inches (width, height). Default: (10, 6).

    Returns
    -------
    matplotlib.figure.Figure
        The Figure object containing the cable type distribution plot.
    """
    from pylovo.grid_generator import GridGenerator
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc
    query = """
        SELECT
            pl.std_type,
            COALESCE(SUM(COALESCE(pl.parallel, 1) * COALESCE(pl.length_km, 0.0)), 0.0) AS cable_length
        FROM pylovo.pandapower_line pl
        JOIN pylovo.grid_result gr
          ON gr.grid_result_id = pl.grid_result_id
        WHERE gr.version_id = %(v)s
          AND gr.plz = %(p)s
          AND COALESCE(pl.in_service, TRUE)
          AND pl.std_type IS NOT NULL
        GROUP BY pl.std_type
        ORDER BY pl.std_type
    """
    dbc_client.cur.execute(query, {"v": VERSION_ID, "p": plz})
    cable_length_dict = {std_type: float(length) for std_type, length in dbc_client.cur.fetchall()}

    fig, ax = plt.subplots(figsize=figsize)
    ax.bar(cable_length_dict.keys(), height=cable_length_dict.values(), width=0.3)
    ax.set_title('Cable Type Distribution', fontsize=14)
    ax.set_xlabel("Cable type")
    ax.set_ylabel("Length in m")
    plt.show()

    return fig


def get_trafo_dicts(plz: int) -> Tuple[dict, dict, dict, dict]:
    """
    Retrieve load count, bus count, and cable length per transformer type for a postal code.

    Parameters
    ----------
    plz : int
        Postal code.

    Returns
    -------
    tuple of dict
        (load_count_dict, bus_count_dict, cable_length_dict, trafo_dict)
    """
    from pylovo.grid_generator import GridGenerator
    gg = GridGenerator(plz=plz)
    dbc_client = gg.dbc

    load_count_dict = {}
    bus_count_dict = {}
    cable_length_dict = {}
    trafo_dict = {}

    print("Starting basic parameter counting")
    query = """
        WITH grid_scope AS (
            SELECT grid_result_id
            FROM pylovo.grid_result
            WHERE version_id = %(v)s
              AND plz = %(p)s
        ),
        load_counts AS (
            SELECT grid_result_id, COUNT(*)::integer AS load_count
            FROM pylovo.pandapower_load
            GROUP BY grid_result_id
        ),
        load_bus_counts AS (
            SELECT grid_result_id, COUNT(DISTINCT bus)::integer AS bus_count
            FROM pylovo.pandapower_load
            GROUP BY grid_result_id
        ),
        line_lengths AS (
            SELECT grid_result_id, COALESCE(SUM(length_km), 0.0) AS cable_length
            FROM pylovo.pandapower_line
            GROUP BY grid_result_id
        )
        SELECT
            ROUND(pt.sn_mva * 1000.0)::integer AS capacity,
            COALESCE(lc.load_count, 0) AS load_count,
            COALESCE(lbc.bus_count, 0) AS bus_count,
            COALESCE(ll.cable_length, 0.0) AS cable_length
        FROM grid_scope gs
        JOIN pylovo.pandapower_trafo pt
          ON pt.grid_result_id = gs.grid_result_id
        LEFT JOIN load_counts lc
          ON lc.grid_result_id = gs.grid_result_id
        LEFT JOIN load_bus_counts lbc
          ON lbc.grid_result_id = gs.grid_result_id
        LEFT JOIN line_lengths ll
          ON ll.grid_result_id = gs.grid_result_id
        WHERE pt.sn_mva IS NOT NULL
        ORDER BY capacity
    """
    dbc_client.cur.execute(query, {"v": VERSION_ID, "p": plz})

    for capacity, load_count, bus_count, cable_length in dbc_client.cur.fetchall():
        if capacity in trafo_dict:
            trafo_dict[capacity] += 1
            load_count_dict[capacity].append(load_count)
            bus_count_dict[capacity].append(bus_count)
            cable_length_dict[capacity].append(cable_length)
        else:
            trafo_dict[capacity] = 1
            load_count_dict[capacity] = [load_count]
            bus_count_dict[capacity] = [bus_count]
            cable_length_dict[capacity] = [cable_length]

    return load_count_dict, bus_count_dict, cable_length_dict, trafo_dict

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
    """
    Generate a distribution plot (Box, Violin, or Strip) for a given metric.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing metrics. Must have 'source' column.
    metric_col : str
        Column name of the metric to plot.
    title : str, optional
        Chart title. Defaults to metric name.
    hover_data : List[str], optional
        Additional columns to show on hover (e.g. ['kcid', 'bcid']).
    plot_type : str, optional
        'box', 'violin', or 'strip'. Default: 'box'.
        
    Returns
    -------
    plotly.graph_objects.Figure
        The Plotly Figure object.
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
    title: Optional[str] = None
) -> go.Figure:
    """
    Generate an overlaid histogram/KDE using Plotly.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing metrics.
    metric_col : str
        Column name to plot.
    title : str, optional
        Plot title.
        
    Returns
    -------
    plotly.graph_objects.Figure
        The Plotly Figure object.
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
        opacity=0.6
    )

    fig.update_layout(
        xaxis_title=metric_col.replace("_", " ").title(),
        yaxis_title="Count",
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
    """
    Generate a scatter plot for exploring correlations.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing data.
    x_col : str
        X-axis column.
    y_col : str
        Y-axis column.
    size_col : str, optional
        Column determining marker size.
    title : str, optional
        Plot title.
        
    Returns
    -------
    plotly.graph_objects.Figure
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


def plot_comparison_pdf_plotly(
    df: pd.DataFrame, 
    metric_col: str, 
    title: Optional[str] = None
) -> go.Figure:
    """
    Generate a Probability Density Function (PDF) plot using KDE.
    
    Parameters
    ----------
    df : pd.DataFrame
        DataFrame containing metrics.
    metric_col : str
        Column name to plot.
    title : str, optional
        Plot title.
        
    Returns
    -------
    plotly.graph_objects.Figure
    """
    if df.empty:
        return go.Figure().add_annotation(text="No Data Available", showarrow=False)

    sources = df["source"].unique()
    color_discrete_map = get_color_map(sources)

    fig = go.Figure()

    for source in sources:
        subset = df[df["source"] == source]
        data = subset[metric_col].dropna()
        
        if len(data) > 1:
            try:
                # Calculate KDE
                kde = stats.gaussian_kde(data)
                
                # Create x range for plotting
                min_val = data.min()
                max_val = data.max()
                pad = (max_val - min_val) * 0.2
                x_grid = np.linspace(min_val - pad, max_val + pad, 200)
                y_grid = kde(x_grid)
                
                color = color_discrete_map.get(source, "black")
                
                fig.add_trace(go.Scatter(
                    x=x_grid, 
                    y=y_grid,
                    mode='lines',
                    name=source,
                    line=dict(color=color, width=2),
                    fill='tozeroy', # Optional: fill area under curve
                    fillcolor=f"rgba{tuple(int(color.lstrip('#')[i:i+2], 16) for i in (0, 2, 4)) + (0.1,)}" if color.startswith('#') else None
                ))
            except Exception as e:
                print(f"Could not calculate KDE for {source}: {e}")
                pass

    fig.update_layout(
        title=title or f"PDF Comparison of {metric_col}",
        xaxis_title=metric_col.replace("_", " ").title(),
        yaxis_title="Density",
        legend_title="Source",
        font=dict(family="Arial", size=14),
        template="plotly_white",
        hovermode="x unified"
    )

    return fig

