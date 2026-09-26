"""
Plots of grid statistics per postcode area (PLZ) and of synthetic/real metric comparisons.

The PLZ plots read the results of ``VERSION_ID`` from the database: transformer
sizes, cable types and the per-transformer parameters of ``plz_parameters``.
The Plotly comparison plots take a metrics table with a ``source`` column.
"""

from typing import Tuple, Optional, List

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from pylovo.config_loader import VERSION_ID
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.utils import get_color_map


def plot_pie_of_trafo_cables(plz: int, figsize: Tuple[int, int] = (16, 4)) -> Figure:
    """Plot pie charts of the transformer sizes and installed cable types of a PLZ.

    Args:
        plz: Postal code.
        figsize: Figure size in inches (width, height).

    Returns:
        The figure with the two pie charts.
    """
    with DatabaseClient() as dbc_client:
        _, _, trafo_dict = dbc_client.read_per_trafo_dict(plz=plz)
        cable_dict = dbc_client.read_cable_dict(plz)

    fig, axs = plt.subplots(nrows=1, ncols=2, figsize=figsize)

    # Plot Transformer size distribution
    axs[0].pie(trafo_dict.values(), labels=trafo_dict.keys(), autopct='%1.1f%%',
               pctdistance=1.15, labeldistance=.6)
    axs[0].set_title('Transformer Size Distribution', fontsize=14)

    # Plot cable length distribution
    axs[1].pie(cable_dict.values(), labels=cable_dict.keys(), autopct="%.1f%%")
    axs[1].set_title("Installed Cable Length", fontsize=14)
    plt.show()

    return fig


def plot_hist_trafos(plz: int, figsize: Tuple[int, int] = (10, 6)) -> Figure:
    """Plot a bar chart of the number of transformers per size in a PLZ.

    Args:
        plz: Postal code.
        figsize: Figure size in inches (width, height).

    Returns:
        The figure with the bar chart.
    """
    with DatabaseClient() as dbc_client:
        _, _, trafo_dict = dbc_client.read_per_trafo_dict(plz=plz)

    fig, ax = plt.subplots(figsize=figsize)
    ax.bar(trafo_dict.keys(), height=trafo_dict.values(), width=0.3)
    ax.set_title('Transformer Size Distribution', fontsize=14)
    ax.set_xlabel("Trafo size")
    ax.set_ylabel("Count")
    plt.show()

    return fig


def plot_boxplot_plz(plz: int, figsize: Tuple[int, int] = (16, 4)) -> Figure:
    """Create boxplots of grid parameters grouped by transformer size.

    Shows the distribution of load numbers, bus numbers, simultaneous peak load and
    maximum/average transformer distance for each transformer size of the PLZ.

    Args:
        plz: Postal code.
        figsize: Figure size in inches (width, height).

    Returns:
        The figure with one boxplot panel per parameter.
    """
    with DatabaseClient() as dbc_client:
        data_list, data_labels, _ = dbc_client.read_per_trafo_dict(plz=plz)
    trafo_sizes = list(data_list[0].keys())
    values = [list(d.values()) for d in data_list]

    # Create the figure and axes objects
    fig, axs = plt.subplots(nrows=1, ncols=len(data_list), figsize=figsize, sharey=True)

    for i, data_label in enumerate(data_labels):
        axs[i].boxplot(values[i], tick_labels=trafo_sizes, orientation='horizontal',
                       showfliers=False, patch_artist=True, notch=False)
        axs[i].set_title(data_label, fontsize=12)

    fig.supxlabel('Values', fontsize=12)
    fig.supylabel('Transformer Size (kVA)', fontsize=12)
    plt.tight_layout()
    plt.show()

    return fig


def plot_cable_length_of_types(plz: int, figsize: Tuple[int, int] = (10, 6)) -> Figure:
    """Plot the installed cable length per cable type in a PLZ.

    Parallel cables count with their multiplicity; lines out of service are ignored.

    Args:
        plz: Postal code.
        figsize: Figure size in inches (width, height).

    Returns:
        The figure with the bar chart (lengths in km).
    """
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
    with DatabaseClient() as dbc_client:
        dbc_client.cur.execute(query, {"v": VERSION_ID, "p": plz})
        cable_length_dict = {std_type: float(length) for std_type, length in dbc_client.cur.fetchall()}

    fig, ax = plt.subplots(figsize=figsize)
    ax.bar(cable_length_dict.keys(), height=cable_length_dict.values(), width=0.3)
    ax.set_title('Cable Type Distribution', fontsize=14)
    ax.set_xlabel("Cable type")
    ax.set_ylabel("Length in km")
    plt.show()

    return fig


def get_trafo_dicts(plz: int) -> Tuple[dict, dict, dict, dict]:
    """Retrieve load count, bus count and cable length per transformer size for a PLZ.

    Args:
        plz: Postal code.

    Returns:
        Tuple ``(load_count_dict, bus_count_dict, cable_length_dict, trafo_dict)``. The
        keys are transformer sizes in kVA; the first three map to one value per grid
        (loads, buses with loads, cable length in km) and ``trafo_dict`` to the
        number of transformers.
    """
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
            ROUND(pt.sn_mva * COALESCE(pt.parallel, 1) * 1000.0)::integer AS capacity,  -- station, not unit
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
    with DatabaseClient() as dbc_client:
        dbc_client.cur.execute(query, {"v": VERSION_ID, "p": plz})
        rows = dbc_client.cur.fetchall()

    for capacity, load_count, bus_count, cable_length in rows:
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
