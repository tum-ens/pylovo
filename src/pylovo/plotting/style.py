"""The visual style of pylovo's figures: colours, matplotlib defaults and map helpers.

One style for the documentation figures, the tutorial notebooks and the plotting functions:

* colour tokens of a light surface, legible in the light and the dark documentation theme,
* :data:`CATEGORICAL`, a palette in fixed order for unordered categories (grids, feeders, building uses),
* sequential ramps for ordered values: :data:`SEQUENTIAL_BLUE` (cable cross-sections) and
  :data:`SEQUENTIAL_ORANGE` (station ratings),
* :data:`STATUS` colours, always used together with a text label,
* maps drawn from vector layers (streets, buildings) instead of basemap tiles, with a scale bar and the
  attribution of the data sources.

:func:`style` applies the matplotlib defaults inside a ``with`` block, :func:`apply_style` for the rest of a
session (a notebook). Importing this module changes no global setting.

Needs the ``plots`` extra.
"""
from __future__ import annotations

import math
from contextlib import contextmanager
from typing import Iterable, Iterator, Mapping, Optional, Sequence

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

# Chart chrome (light surface)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

#: Categorical slots in fixed order.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

#: Sequential blue ramp (light to dark) for ordered categories such as cable cross-sections.
SEQUENTIAL_BLUE = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]

#: Sequential orange ramp (light to dark) for station ratings.
SEQUENTIAL_ORANGE = ["#f7c3a5", "#f29a6c", "#eb6834", "#c6501f", "#9a3d15", "#6c2a0d"]

#: Status colours, used only together with a text label.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

# Map layers
STREET = "#c3c2b7"
BUILDING_NEUTRAL = "#e1e0d9"
BUILDING_EDGE = "#b9b7ae"

#: Building uses of pylovo's consumer categories (``building_use``).
USE_COLORS = {"Residential": CATEGORICAL[0], "Commercial": CATEGORICAL[1], "Public": CATEGORICAL[2],
              "Mixed": CATEGORICAL[3]}

#: Settlement types of a postcode area (1 rural, 2 semi-urban, 3 urban), also the bands of the settlement chart.
SETTLEMENT_COLORS = {1: CATEGORICAL[2], 2: CATEGORICAL[3], 3: CATEGORICAL[0]}

#: The voltage levels in the figures that connect them: transmission (220/380 kV), HV (110 kV), MV and LV.
LEVEL_COLORS = {"EHV": "#2b2a29", "HV": CATEGORICAL[6], "MV": CATEGORICAL[1], "LV": CATEGORICAL[0]}

#: Power-flow status of a grid (``grid_result.power_flow_status``) as a status colour.
POWER_FLOW_STATUS_COLORS = {"converged": STATUS["good"], "voltage_violation": STATUS["warning"],
                            "loading_violation": STATUS["serious"], "not_converged": STATUS["critical"]}

#: Short credit line of the data behind the maps of the Bavarian example areas (InfDB inputs). Maps of other
#: data pass their own text to the plotting functions (``attribution=``).
DATA_ATTRIBUTION = ("Data: © Bayerische Vermessungsverwaltung (LoD2), © basemap.de / BKG, "
                    "© OpenStreetMap contributors")

#: Credit line of the HV/EHV grid (eGon-data eTraGo, built by osmTGmod from OpenStreetMap).
HV_ATTRIBUTION = "HV grid: eGon-data eTraGo (osmTGmod), © OpenStreetMap contributors (ODbL)"

#: Matplotlib defaults of all pylovo figures.
RC = {
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "font.family": "DejaVu Sans",
    "font.size": 9,
    "axes.titlesize": 10,
    "axes.titleweight": "bold",
    "axes.titlelocation": "left",
    "axes.edgecolor": BASELINE,
    "axes.labelcolor": INK_SECONDARY,
    "axes.linewidth": 0.8,
    "axes.grid": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "grid.color": GRID,
    "grid.linewidth": 0.6,
    "xtick.color": INK_MUTED,
    "ytick.color": INK_MUTED,
    "xtick.labelcolor": INK_SECONDARY,
    "ytick.labelcolor": INK_SECONDARY,
    "text.color": INK,
    "legend.frameon": False,
    "legend.fontsize": 8,
    "figure.dpi": 110,
    "savefig.dpi": 160,
    "svg.fonttype": "path",
}


def apply_style() -> None:
    """Set the pylovo matplotlib defaults for the rest of the session (e.g. at the top of a notebook)."""
    plt.rcParams.update(RC)


@contextmanager
def style() -> Iterator[None]:
    """Apply the pylovo matplotlib defaults inside a ``with`` block only."""
    with plt.rc_context(RC):
        yield


# --------------------------------------------------------------------------- colours
def cross_section(std_type: str) -> float:
    """The conductor cross-section in mm² of a cable type name such as ``NAYY_4_150`` (0 if unknown)."""
    for part in reversed(str(std_type).replace("x", "_").replace(" ", "_").split("_")):
        try:
            return float(part)
        except ValueError:
            continue
    return 0.0


def _ramp(values: Sequence, ramp: Sequence[str]) -> dict:
    """Spread ``ramp`` evenly over the ordered ``values``, so that neighbouring values stay distinguishable."""
    values = list(values)
    if not values:
        return {}
    if len(values) == 1:
        return {values[0]: ramp[len(ramp) // 2]}
    cmap = LinearSegmentedColormap.from_list("ramp", list(ramp))
    return {value: cmap(i / (len(values) - 1)) for i, value in enumerate(values)}


def cable_colors(std_types: Iterable[str]) -> dict:
    """``{std_type: colour}``: the sequential blue ramp by cross-section (thin cables light, thick ones dark)."""
    return _ramp(sorted(set(std_types), key=cross_section), SEQUENTIAL_BLUE)


def rating_colors(ratings: Iterable[float]) -> dict:
    """``{rating: colour}``: the sequential orange ramp by station rating (kVA)."""
    return _ramp(sorted({float(r) for r in ratings if r is not None and not math.isnan(float(r))}), SEQUENTIAL_ORANGE)


def categorical_colors(keys: Iterable) -> dict:
    """``{key: colour}`` in the fixed :data:`CATEGORICAL` order of the sorted keys (repeats after eight)."""
    return {key: CATEGORICAL[index % len(CATEGORICAL)] for index, key in enumerate(sorted(set(keys)))}


def neighbour_colors(points: Mapping, k: int = 6) -> dict:
    """``{key: colour}`` for many map features: neighbours get different :data:`CATEGORICAL` colours.

    A greedy colouring in a fixed order (by x, then y): every feature takes the first colour that none of its
    ``k`` nearest, already coloured neighbours has, so adjacent grids stay distinguishable although the eight
    colours repeat.

    Args:
        points: ``{key: (x, y)}``, e.g. the stations of the grids.
        k: Number of neighbours that must differ (at most seven).
    """
    keys = sorted(points, key=lambda key: (points[key][0], points[key][1]))
    if not keys:
        return {}
    xy = np.array([points[key] for key in keys], dtype=float)
    k = min(k, len(CATEGORICAL) - 1, len(keys) - 1)
    colors: dict = {}
    for index, key in enumerate(keys):
        taken = set()
        if k > 0:
            distance = np.hypot(*(xy - xy[index]).T)
            for other in np.argsort(distance)[1:]:
                if keys[other] in colors:
                    taken.add(colors[keys[other]])
                if len(taken) >= k:
                    break
        colors[key] = next((c for c in CATEGORICAL if c not in taken), CATEGORICAL[index % len(CATEGORICAL)])
    return colors


def mix_with_surface(color: str, share: float) -> tuple:
    """``color`` mixed into the surface colour: ``share`` 0 gives the surface, 1 the colour (light fills)."""
    from matplotlib.colors import to_rgb

    surface, value = np.array(to_rgb(SURFACE)), np.array(to_rgb(color))
    return tuple(surface + share * (value - surface))


def sequential_cmap(name: str = "blue") -> LinearSegmentedColormap:
    """A continuous colormap from the surface to the dark end of a ramp: ``"blue"``, ``"orange"`` or ``"load"``.

    ``"load"`` runs from a light grey over orange to red, for peak loads; ``"loading"`` starts at a visible grey,
    for lines coloured by their loading.
    """
    if name == "orange":
        colors = ["#fbe9de", *SEQUENTIAL_ORANGE[1:]]
    elif name == "load":
        colors = ["#efeee9", "#f7c3a5", "#eb6834", "#d03b3b", "#7a1f1f"]
    elif name == "loading":
        colors = [BASELINE, "#eda100", "#eb6834", "#d03b3b", "#7a1f1f"]
    else:
        colors = ["#e3eefb", *SEQUENTIAL_BLUE]
    return LinearSegmentedColormap.from_list(f"pylovo_{name}", colors)


def voltage_cmap() -> LinearSegmentedColormap:
    """Colormap of bus voltages: dark blue near the reference, orange and red towards the lower limit."""
    return LinearSegmentedColormap.from_list("pylovo_voltage", ["#d03b3b", "#eb6834", "#f7c3a5", "#86b6ef",
                                                                 "#2a78d6", "#104281"])


# --------------------------------------------------------------------------- maps
def map_axes(ax: plt.Axes, bounds: Optional[Sequence[float]] = None) -> None:
    """Prepare an axis for a map: equal aspect, no ticks or spines, optional extent ``(minx, miny, maxx, maxy)``."""
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if bounds is not None:
        minx, miny, maxx, maxy = bounds
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)


def padded_bounds(bounds: Sequence[float], pad: float = 0.05, min_size: float = 0.0) -> tuple[float, float, float, float]:
    """``bounds`` enlarged by ``pad`` (a share of the larger side) and to at least ``min_size`` in both directions."""
    minx, miny, maxx, maxy = (float(v) for v in bounds)
    size = max(maxx - minx, maxy - miny, min_size)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    half_x = max(maxx - minx, min_size) / 2 + pad * size
    half_y = max(maxy - miny, min_size) / 2 + pad * size
    return cx - half_x, cy - half_y, cx + half_x, cy + half_y


def nice_length(span: float, share: float = 0.2) -> float:
    """A round scale-bar length (1, 2 or 5 times a power of ten) of about ``share`` of ``span``."""
    target = max(span * share, 1e-9)
    power = 10 ** math.floor(math.log10(target))
    return max(step * power for step in (1, 2, 5) if step * power <= target)


def add_scalebar(ax: plt.Axes, length_m: Optional[float] = None, loc: str = "lower left") -> None:
    """Draw a scale bar in map units (metres); ``length_m=None`` picks a round length of about a fifth of the map."""
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    if length_m is None:
        length_m = nice_length(x1 - x0)
    pad_x = (x1 - x0) * 0.04
    pad_y = (y1 - y0) * 0.04
    start = x1 - pad_x - length_m if loc == "lower right" else x0 + pad_x
    y = y0 + pad_y
    ax.plot([start, start + length_m], [y, y], color=INK_SECONDARY, lw=2, solid_capstyle="butt", zorder=20)
    text = f"{length_m / 1000:g} km" if length_m >= 1000 else f"{length_m:g} m"
    ax.text(start + length_m / 2, y + (y1 - y0) * 0.012, text, ha="center", va="bottom", fontsize=7,
            color=INK_SECONDARY, zorder=20,
            bbox={"boxstyle": "square,pad=0.15", "fc": SURFACE, "ec": "none", "alpha": 0.8})


def add_attribution(ax: plt.Axes, text: Optional[str] = None) -> None:
    """Write the data attribution into the lower right corner of a map (:data:`DATA_ATTRIBUTION` by default)."""
    text = DATA_ATTRIBUTION if text is None else text
    if not text:
        return
    ax.text(0.995, 0.006, text, transform=ax.transAxes, ha="right", va="bottom", fontsize=6.3, color=INK_SECONDARY,
            zorder=20, bbox={"boxstyle": "square,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.85})


def legend_below(ax: plt.Axes, handles: list, ncol: int = 3, title: Optional[str] = None) -> None:
    """A legend below the axis, without frame."""
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=ncol, title=title,
              title_fontsize=8, fontsize=7.5, columnspacing=1.4, handlelength=1.8)


def label(ax: plt.Axes, x: float, y: float, text: str, fontsize: float = 7.5) -> None:
    """A small boxed label next to a point of a map."""
    ax.annotate(text, (x, y), xytext=(6, 6), textcoords="offset points", fontsize=fontsize, color=INK, zorder=12,
                bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": BASELINE, "lw": 0.6, "alpha": 0.92})


def place_labels(ax: plt.Axes, points: Sequence[tuple[float, float, str]], min_distance: Optional[float] = None,
                 fontsize: float = 7.5) -> int:
    """Label points of a map, skipping a label whose point lies closer than ``min_distance`` to a labelled one.

    Args:
        points: ``(x, y, text)``; earlier points win.
        min_distance: In map units; default 6 % of the larger side of the current map extent.

    Returns:
        The number of labels drawn.
    """
    if min_distance is None:
        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        min_distance = 0.06 * max(abs(x1 - x0), abs(y1 - y0))
    placed: list[tuple[float, float]] = []
    for x, y, text in points:
        if all(math.hypot(x - px, y - py) >= min_distance for px, py in placed):
            label(ax, x, y, text, fontsize=fontsize)
            placed.append((x, y))
    return len(placed)


def station_marker(ax: plt.Axes, x, y, size: float = 150, zorder: int = 10, **kwargs) -> None:
    """Draw MV/LV stations as black stars with a white edge."""
    ax.scatter(x, y, marker="*", s=size, color=INK, edgecolor="white", lw=1.0, zorder=zorder, **kwargs)


def tidy_value_axes(ax: plt.Axes, grid_axis: str = "y") -> None:
    """Light grid lines along ``grid_axis`` (``"x"``, ``"y"`` or ``"both"``) behind the data."""
    ax.grid(True, axis=grid_axis, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
