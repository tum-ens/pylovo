"""Shared style and helpers for the documentation figure scripts.

The colours follow one fixed categorical order (never cycled) and one
sequential blue ramp for ordered values such as cable cross-sections. All
figures use a light surface so that they stay legible in the light and dark
documentation themes.

Database-backed figure scripts connect with pylovo's own connection settings
(``.env`` next to the repository, see ``pylovo.config_loader``). They only read.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE_DIR = REPO_ROOT / "docs" / "source" / "images"

# Chart chrome (light surface)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"

#: Categorical slots in fixed order.
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]

#: Sequential blue ramp (steps 250 ... 700) for ordered categories.
SEQUENTIAL_BLUE = ["#86b6ef", "#5598e7", "#2a78d6", "#1c5cab", "#104281", "#0d366b"]

#: Status colours, used only together with a text label.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}

# Map layers
STREET = "#c3c2b7"
BUILDING_NEUTRAL = "#e1e0d9"
BUILDING_EDGE = "#b9b7ae"

OSM_ATTRIBUTION = "Demo extract PLZ 85653 (Aying) · © OpenStreetMap contributors, ODbL"


def apply_style() -> None:
    """Set matplotlib defaults for all documentation figures."""
    plt.rcParams.update(
        {
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
            "xtick.color": INK_MUTED,
            "ytick.color": INK_MUTED,
            "text.color": INK,
            "legend.frameon": False,
            "legend.fontsize": 8,
            "svg.fonttype": "path",
            "svg.hashsalt": "pylovo-docs",  # deterministic element ids in SVG output
        }
    )


def image_path(subdir: str, name: str) -> Path:
    """Return the output path ``docs/source/images/<subdir>/<name>`` and create the folder."""
    folder = IMAGE_DIR / subdir
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


def save(fig, subdir: str, stem: str, formats: tuple[str, ...] = ("png",), dpi: int = 160) -> None:
    """Save a figure in the given formats and close it."""
    for fmt in formats:
        path = image_path(subdir, f"{stem}.{fmt}")
        fig.savefig(path, dpi=dpi, bbox_inches="tight", pad_inches=0.08, metadata={"Date": None} if fmt == "svg" else None)
        print(f"wrote {path.relative_to(REPO_ROOT)}")
    plt.close(fig)


def sql_engine():
    """Return an SQLAlchemy engine for the pylovo database configured in ``.env``."""
    from sqlalchemy import create_engine

    from pylovo import config_loader as cfg

    print(f"Reading from {cfg.HOST}:{cfg.PORT}/{cfg.DBNAME}")
    return create_engine(
        f"postgresql+psycopg2://{cfg.DBUSER}:{cfg.PASSWORD}@{cfg.HOST}:{cfg.PORT}/{cfg.DBNAME}",
        connect_args={"options": "-c search_path=pylovo,public"},
    )


def map_axes(ax, bounds=None) -> None:
    """Prepare an axis for a map: equal aspect, no ticks, optional extent."""
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    if bounds is not None:
        minx, miny, maxx, maxy = bounds
        ax.set_xlim(minx, maxx)
        ax.set_ylim(miny, maxy)


def add_scalebar(ax, length_m: float = 200, loc: str = "lower left") -> None:
    """Draw a simple scale bar in map units (metres)."""
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    pad_x = (x1 - x0) * 0.04
    pad_y = (y1 - y0) * 0.04
    if loc == "lower right":
        start = x1 - pad_x - length_m
    else:
        start = x0 + pad_x
    y = y0 + pad_y
    ax.plot([start, start + length_m], [y, y], color=INK_SECONDARY, lw=2, solid_capstyle="butt")
    ax.text(start + length_m / 2, y + (y1 - y0) * 0.012, f"{length_m:.0f} m", ha="center", va="bottom",
            fontsize=7, color=INK_SECONDARY)


def add_attribution(ax, text: str = OSM_ATTRIBUTION) -> None:
    """Write the data attribution into the lower right corner of a map."""
    ax.text(0.995, 0.006, text, transform=ax.transAxes, ha="right", va="bottom", fontsize=6.3, color=INK_SECONDARY,
            zorder=20, bbox={"boxstyle": "square,pad=0.2", "fc": SURFACE, "ec": "none", "alpha": 0.85})
