"""
Power flow validation plotting functions.

This module contains plotting functions for visualizing power flow results,
including voltage distributions, line loading, and grid performance metrics.
The networks need power flow results (``pandapower.runpp``).
"""

from typing import Tuple, Optional

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
import pandas as pd
import pandapower as pp

from pylovo.plotting.utils import setup_axes, add_statistics_box, add_limit_lines


def plot_load_and_voltage_distribution(
    net: pp.pandapowerNet,
    figsize: Tuple[int, int] = (12, 6),
    bins: int = 20,
    show_stats: bool = True,
    ax: Optional[plt.Axes] = None
) -> Figure:
    """
    Plot histograms of load (p_mw) and bus voltage (vm_pu) distributions.

    Args:
        net: The pandapower network from which to extract load and voltage data.
        figsize: Figure size in inches (width, height). Default: (12, 6).
        bins: Number of histogram bins. Default: 20.
        show_stats: Whether to display statistics on the plots. Default: True.
        ax: Axes object for subplot integration. If None, creates a new figure.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the subplots.

    Raises:
        ValueError: If power flow has not been run on the network.
    """
    if net.res_load.empty or net.res_bus.empty:
        raise ValueError("No power flow results found. Run pp.runpp(net) first.")

    loads = net.res_load['p_mw']
    voltages = net.res_bus['vm_pu']

    if ax is None:
        fig, axes = plt.subplots(1, 2, figsize=figsize)
    else:
        fig = ax.get_figure()
        axes = [ax, ax]

    # Plot histogram of loads
    axes[0].hist(loads, bins=bins, color='skyblue', edgecolor='black', alpha=0.7)
    setup_axes(axes[0], xlabel='Load (MW)', ylabel='Frequency',
               title='Load Distribution', grid=True)

    if show_stats:
        stats_text = f'Mean: {loads.mean():.4f} MW\nStd: {loads.std():.4f} MW'
        add_statistics_box(axes[0], stats_text, position='upper right')

    # Plot histogram of bus voltages
    axes[1].hist(voltages, bins=bins, color='salmon', edgecolor='black', alpha=0.7)
    setup_axes(axes[1], xlabel='Voltage (p.u.)', ylabel='Frequency',
               title='Voltage Distribution', grid=True)

    # Add voltage limit lines
    voltage_limits = {
        0.9: {'label': 'Min limit (0.9)', 'color': 'red', 'linestyle': '--', 'linewidth': 1},
        1.1: {'label': 'Max limit (1.1)', 'color': 'red', 'linestyle': '--', 'linewidth': 1}
    }
    add_limit_lines(axes[1], voltage_limits, orientation='vertical')
    axes[1].legend()

    if show_stats:
        stats_text = f'Mean: {voltages.mean():.4f} p.u.\nStd: {voltages.std():.4f} p.u.'
        add_statistics_box(axes[1], stats_text, position='upper left')

    fig.tight_layout()
    return fig


def plot_all_voltages_for_plz(
    voltage_df: pd.DataFrame,
    figsize: Tuple[int, int] = (12, 6),
    bins: int = 100,
    show_violations: bool = True,
    ax: Optional[plt.Axes] = None
) -> Figure:
    """
    Plot histogram of bus voltage distributions for all networks in a postal code area.

    Args:
        voltage_df: DataFrame containing 'vm_pu' column with voltage magnitudes.
        figsize: Figure size in inches (width, height). Default: (12, 6).
        bins: Number of histogram bins. Default: 100.
        show_violations: Whether to highlight voltage violations. Default: True.
        ax: Axes object for subplot integration. If None, creates a new figure.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the plot.

    Raises:
        ValueError: If voltage_df is empty or doesn't contain 'vm_pu' column.
    """
    if voltage_df.empty:
        raise ValueError("voltage_df is empty")
    if 'vm_pu' not in voltage_df.columns:
        raise ValueError("voltage_df must contain 'vm_pu' column")

    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=figsize)
    else:
        fig = ax.get_figure()

    # Plot histogram of bus voltages
    ax.hist(voltage_df['vm_pu'], bins=bins, color='black', edgecolor='black', alpha=0.7)
    setup_axes(ax, xlabel='Voltage (p.u.)', ylabel='Frequency',
               title='AC Power Flow Bus Voltage Distribution', grid=True)

    # Add voltage limit lines
    voltage_limits = {
        0.9: {'label': 'Min limit (0.9 p.u.)', 'color': 'red', 'linestyle': '--', 'linewidth': 2},
        1.1: {'label': 'Max limit (1.1 p.u.)', 'color': 'red', 'linestyle': '--', 'linewidth': 2}
    }
    add_limit_lines(ax, voltage_limits, orientation='vertical')
    ax.legend(fontsize=10)

    # Add statistics
    mean_v = voltage_df['vm_pu'].mean()
    std_v = voltage_df['vm_pu'].std()
    min_v = voltage_df['vm_pu'].min()
    max_v = voltage_df['vm_pu'].max()

    if show_violations:
        violations_low = (voltage_df['vm_pu'] < 0.9).sum()
        violations_high = (voltage_df['vm_pu'] > 1.1).sum()
        total_buses = len(voltage_df)
        violation_pct = (violations_low + violations_high) / total_buses * 100

        stats_text = (f'Total buses: {total_buses}\n'
                      f'Mean: {mean_v:.4f} p.u.\n'
                      f'Std: {std_v:.4f} p.u.\n'
                      f'Range: [{min_v:.4f}, {max_v:.4f}]\n'
                      f'Violations: {violations_low + violations_high} ({violation_pct:.2f}%)\n'
                      f'  Low: {violations_low}\n'
                      f'  High: {violations_high}')
    else:
        stats_text = (f'Mean: {mean_v:.4f} p.u.\n'
                      f'Std: {std_v:.4f} p.u.\n'
                      f'Range: [{min_v:.4f}, {max_v:.4f}]')

    add_statistics_box(ax, stats_text, position='upper left')
    fig.tight_layout()

    return fig


def plot_voltage_profile(
    net: pp.pandapowerNet,
    figsize: Tuple[int, int] = (12, 6),
    show_limits: bool = True,
    ax: Optional[plt.Axes] = None
) -> Figure:
    """
    Plot voltage profile across all buses.

    Args:
        net: Pandapower network with completed power flow results.
        figsize: Figure size in inches (width, height). Default: (12, 6).
        show_limits: Whether to show voltage limit lines. Default: True.
        ax: Axes object for subplot integration. If None, creates a new figure.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the voltage profile plot.

    Raises:
        ValueError: If power flow has not been run on the network.
    """
    if net.res_bus.empty:
        raise ValueError("No power flow results found. Run pp.runpp(net) first.")

    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=figsize)
    else:
        fig = ax.get_figure()

    bus_indices = net.res_bus.index.tolist()
    voltages = net.res_bus['vm_pu'].tolist()

    # Plot voltage profile
    ax.plot(bus_indices, voltages, 'bo-', linewidth=2, markersize=4, label='Bus Voltage')

    if show_limits:
        voltage_limits = {
            0.9: {'label': 'Min limit (0.9 p.u.)', 'color': 'red', 'linestyle': '--', 'linewidth': 1.5},
            1.1: {'label': 'Max limit (1.1 p.u.)', 'color': 'red', 'linestyle': '--', 'linewidth': 1.5},
            1.0: {'label': 'Nominal (1.0 p.u.)', 'color': 'green', 'linestyle': ':', 'linewidth': 1}
        }
        add_limit_lines(ax, voltage_limits, orientation='horizontal')

    setup_axes(ax, xlabel='Bus Index', ylabel='Voltage (p.u.)',
               title='Bus Voltage Profile', grid=True)
    ax.legend()
    fig.tight_layout()

    return fig


def plot_line_loading_distribution(
    net: pp.pandapowerNet,
    figsize: Tuple[int, int] = (12, 6),
    bins: int = 30,
    ax: Optional[plt.Axes] = None
) -> Figure:
    """
    Plot distribution of line loading percentages.

    Args:
        net: Pandapower network with completed power flow results.
        figsize: Figure size in inches (width, height). Default: (12, 6).
        bins: Number of histogram bins. Default: 30.
        ax: Axes object for subplot integration. If None, creates a new figure.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the line loading distribution plot.

    Raises:
        ValueError: If power flow has not been run on the network.
    """
    if net.res_line.empty:
        raise ValueError("No line results found. Run pp.runpp(net) first.")

    if ax is None:
        fig, ax = plt.subplots(1, 1, figsize=figsize)
    else:
        fig = ax.get_figure()

    loading = net.res_line['loading_percent']

    ax.hist(loading, bins=bins, color='steelblue', edgecolor='black', alpha=0.7)

    # Add 100% loading limit line
    loading_limits = {
        100: {'label': '100% loading limit', 'color': 'red', 'linestyle': '--', 'linewidth': 2}
    }
    add_limit_lines(ax, loading_limits, orientation='vertical')

    setup_axes(ax, xlabel='Line Loading (%)', ylabel='Frequency',
               title='Line Loading Distribution', grid=True)
    ax.legend()

    # Add statistics
    stats_text = (f'Mean: {loading.mean():.2f}%\n'
                  f'Std: {loading.std():.2f}%\n'
                  f'Max: {loading.max():.2f}%\n'
                  f'Overloaded: {(loading > 100).sum()}')
    add_statistics_box(ax, stats_text, position='upper right')

    fig.tight_layout()
    return fig

