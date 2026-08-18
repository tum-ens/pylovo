"""Shared settings and small helpers for grid-comparison validation."""
import os
from pathlib import Path

from pylovo.config_loader import GRID_DATA_PATH, VERSION_COMMENT

# Synthetic/real subnets with fewer LV buses are excluded from regular comparison metrics.
MINI_GRID_BUS_THRESHOLD = 5

COMPARISON_METRIC_COLUMNS = [
    "feeder_lines",
    "graph_length",
    "avg_trafo_distance",
    "max_trafo_distance",
    "transformer_mva",
    "graph_resistance",
]

DEFAULT_REAL_GRID_SPLIT_SUBDIR = "swf_split_hybrid"
COMPARISON_SCOPES = {"both", "synthetic", "real"}

def validation_grid_data_path(data_path: str | Path | None = None) -> Path:
    if data_path is not None:
        return Path(data_path).expanduser()
    return Path(GRID_DATA_PATH).expanduser()


def validation_grid_split_subdir(split_subdir: str | None = None) -> str:
    if split_subdir:
        return split_subdir.strip().strip("/")
    return os.getenv("GRID_SPLIT_SUBDIR", DEFAULT_REAL_GRID_SPLIT_SUBDIR).strip().strip("/")


def clean_suffix(value: str | None) -> str:
    if value is None:
        return ""
    return "_".join(str(value).strip().strip("_").split())


def metric_filename(filename: str, output_suffix: str = "") -> str:
    if not output_suffix:
        return filename
    clean = output_suffix.strip().strip("_")
    if not clean:
        return filename
    path = Path(filename)
    return f"{path.stem}_{clean}{path.suffix}"


def metric_output_suffixes(output_suffix: str | None, split_subdir: str) -> tuple[str, str, str]:
    if output_suffix is not None and output_suffix.strip():
        suffix = clean_suffix(output_suffix)
        return suffix, suffix, suffix

    synthetic_suffix = clean_suffix(VERSION_COMMENT)
    real_suffix = clean_suffix(split_subdir.replace("/", "_"))
    audit_suffix = (
        synthetic_suffix
        if synthetic_suffix == real_suffix
        else clean_suffix(f"{synthetic_suffix}__{real_suffix}")
    )
    return synthetic_suffix, real_suffix, audit_suffix


__all__ = [
    "COMPARISON_METRIC_COLUMNS",
    "COMPARISON_SCOPES",
    "MINI_GRID_BUS_THRESHOLD",
    "clean_suffix",
    "metric_filename",
    "metric_output_suffixes",
    "validation_grid_data_path",
    "validation_grid_split_subdir",
]
