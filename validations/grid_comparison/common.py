"""Shared settings and small helpers for grid-comparison validation."""
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

COMPARISON_SCOPES = {"both", "synthetic", "real"}

def validation_grid_data_path(data_path: str | Path | None = None) -> Path:
    if data_path is not None:
        return Path(data_path).expanduser()
    return Path(GRID_DATA_PATH).expanduser()


def validation_grid_data_name(data_path: str | Path | None = None) -> str:
    """Short label for the configured real-grid directory, used in output filenames."""
    return clean_suffix(validation_grid_data_path(data_path).name)


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


def metric_output_suffixes(
    output_suffix: str | None,
    data_path: str | Path | None = None,
) -> tuple[str, str, str]:
    if output_suffix is not None and output_suffix.strip():
        suffix = clean_suffix(output_suffix)
        return suffix, suffix, suffix

    synthetic_suffix = clean_suffix(VERSION_COMMENT)
    real_suffix = validation_grid_data_name(data_path)
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
    "validation_grid_data_name",
    "validation_grid_data_path",
]
