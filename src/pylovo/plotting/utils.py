"""Shared helpers for the plotting modules: source colours, axis setup, text boxes, limit lines,
map tile headers and the pandapower on-map workaround."""

from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional
from unittest import mock

import matplotlib.axes as mpl_axes
import pandapower.plotting.plotly.traces as pp_plotly_traces
import plotly.colors

from pylovo.utils import PYLOVO_USER_AGENT

# Request headers for OpenStreetMap tiles (contextily); the tile usage policy requires a User-Agent.
OSM_TILE_HEADERS = {"User-Agent": PYLOVO_USER_AGENT}


@contextmanager
def pandapower_on_map() -> Iterator[None]:
    """Let pandapower's plotly functions draw ``on_map=True`` without their geodata check.

    pandapower 3.4 checks whether the geodata are WGS84 by reverse-geocoding one bus, but it passes
    "lon, lat" where the geocoder expects "lat, lon" (and the lookup needs internet access), so the
    check fails for pylovo grids. pylovo stores bus and line geodata in WGS84 (EPSG:4326), so the
    check is skipped inside this context.
    """
    with mock.patch.object(pp_plotly_traces, "_on_map_test", return_value=True):
        yield

# Colours of the data sources in real-vs-synthetic comparison plots
COLOR_MAP = {
    "Real": "#2c3e50", # Dark Slate Blue/Grey for Real
    "Real (SWF)": "#2c3e50",
    "Synthetic v1": "#e67e22", # Orange for V1
    "Synthetic v2": "#27ae60", # Green for V2
}
# Fallback colors for Plotly
FALLBACK_COLORS = plotly.colors.qualitative.Plotly

def get_color_map(sources: List[str]) -> Dict[str, str]:
    """Build a plotly colour map for the given source names.

    A source gets the ``COLOR_MAP`` colour of its exact name, else of the first ``COLOR_MAP`` key
    contained in its name, else the next plotly default colour.

    Args:
        sources: Source names, e.g. ``['Real', 'Synthetic v1']``.

    Returns:
        Dictionary ``{source: hex colour}``.
    """
    cmap = {}
    fallback_idx = 0
    for s in sources:
        # Check explicit keys
        if s in COLOR_MAP:
            cmap[s] = COLOR_MAP[s]
            continue
        
        # Check partial matches
        found = False
        for k, v in COLOR_MAP.items():
            if k in s:
                cmap[s] = v
                found = True
                break
        
        if not found:
            cmap[s] = FALLBACK_COLORS[fallback_idx % len(FALLBACK_COLORS)]
            fallback_idx += 1
    return cmap


def setup_axes(
    ax: mpl_axes.Axes,
    xlabel: Optional[str] = None,
    ylabel: Optional[str] = None,
    title: Optional[str] = None,
    grid: bool = True,
    grid_alpha: float = 0.3
) -> mpl_axes.Axes:
    """Set axis labels (size 12), a bold title (size 14) and grid lines.

    Args:
        ax: Axes to configure.
        xlabel: Label of the x-axis.
        ylabel: Label of the y-axis.
        title: Plot title.
        grid: Show grid lines.
        grid_alpha: Transparency of the grid lines.

    Returns:
        The same axes.
    """
    if xlabel:
        ax.set_xlabel(xlabel, fontsize=12)
    if ylabel:
        ax.set_ylabel(ylabel, fontsize=12)
    if title:
        ax.set_title(title, fontsize=14, fontweight='bold')
    if grid:
        ax.grid(True, alpha=grid_alpha)

    return ax


def add_statistics_box(
    ax: mpl_axes.Axes,
    stats_text: str,
    position: str = 'upper right',
    fontsize: int = 10,
    **kwargs
) -> None:
    """Add a monospace text box (e.g. summary statistics) in a corner of the axes.

    Args:
        ax: Axes to draw into.
        stats_text: Text to display.
        position: ``'upper right'`` (default, also used for unknown values), ``'upper left'``,
            ``'lower right'`` or ``'lower left'``.
        fontsize: Font size.
        **kwargs: Passed to ``ax.text``; ``bbox`` replaces the default white rounded box.
    """
    position_map = {
        'upper right': (0.98, 0.98, 'top', 'right'),
        'upper left': (0.02, 0.98, 'top', 'left'),
        'lower right': (0.98, 0.02, 'bottom', 'right'),
        'lower left': (0.02, 0.02, 'bottom', 'left')
    }

    x, y, va, ha = position_map.get(position, position_map['upper right'])

    default_bbox = dict(boxstyle='round', facecolor='white', alpha=0.9)
    bbox = kwargs.pop('bbox', default_bbox)

    ax.text(
        x, y, stats_text,
        transform=ax.transAxes,
        verticalalignment=va,
        horizontalalignment=ha,
        bbox=bbox,
        fontsize=fontsize,
        family='monospace',
        **kwargs
    )


def add_limit_lines(
    ax: mpl_axes.Axes,
    limits: dict,
    orientation: str = 'horizontal'
) -> None:
    """Draw horizontal or vertical limit lines (e.g. voltage limits, loading thresholds).

    Args:
        ax: Axes to draw into.
        limits: ``{value: properties}`` with optional ``label``, ``color`` (default red),
            ``linestyle`` (default ``--``) and ``linewidth`` (default 1.5), e.g.
            ``{0.95: {'label': 'Min limit', 'color': 'red'}}``.
        orientation: ``'horizontal'`` (axhline) or ``'vertical'`` (axvline).
    """
    line_func = ax.axhline if orientation == 'horizontal' else ax.axvline

    for value, properties in limits.items():
        label = properties.get('label', f'Limit: {value}')
        color = properties.get('color', 'red')
        linestyle = properties.get('linestyle', '--')
        linewidth = properties.get('linewidth', 1.5)

        line_func(
            **{('y' if orientation == 'horizontal' else 'x'): value},
            color=color,
            linestyle=linestyle,
            linewidth=linewidth,
            label=label
        )
