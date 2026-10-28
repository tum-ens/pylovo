"""Shared style and helpers for the documentation figure scripts.

The colours and map helpers come from :mod:`pylovo.plotting.style`: one fixed
categorical order and one sequential blue ramp for ordered values such as cable
cross-sections, on a light surface that stays legible in the light and dark
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

# The colours and map helpers are those of the package (pylovo.plotting.style), so the documentation figures
# and the figures of the plotting functions and notebooks look alike.
from pylovo.plotting import style as _style  # noqa: E402
from pylovo.plotting.style import (  # noqa: E402,F401
    BASELINE,
    BUILDING_EDGE,
    BUILDING_NEUTRAL,
    CATEGORICAL,
    GRID,
    INK,
    INK_MUTED,
    INK_SECONDARY,
    SEQUENTIAL_BLUE,
    STATUS,
    STREET,
    SURFACE,
    add_scalebar,
    map_axes,
)

OSM_ATTRIBUTION = "Demo extract PLZ 85653 (Aying) · © OpenStreetMap contributors, ODbL"


def apply_style() -> None:
    """Set the pylovo matplotlib defaults, with deterministic element ids in SVG output."""
    _style.apply_style()
    plt.rcParams["svg.hashsalt"] = "pylovo-docs"


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


def add_attribution(ax, text: str = OSM_ATTRIBUTION) -> None:
    """Write the data attribution (by default that of the OSM demo extract) into the lower right corner of a map."""
    _style.add_attribution(ax, text)
