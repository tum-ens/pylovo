"""Audit export for synthetic/real grid-comparison inputs."""
from pathlib import Path

import pandas as pd

from validations.grid_comparison.common import metric_filename


AUDIT_COLUMNS = [
    "metric_status",
    "metric_error",
    "uses_synthetic_naming",
    "root_bus",
    "bus_count",
    "line_count",
    "active_line_count",
    "consumer_bus_count",
    "load_count",
    "trafo_count",
    "ext_grid_count",
    "negative_length_count",
    "zero_length_count",
    "missing_length_count",
    "feeder_lines",
    "feeder_lines_first_hop",
    "feeder_lines_label_aware",
    "feeder_lines_terminal_topology",
    "feeder_lines_terminal_backbone",
    "feeder_lines_expand_all",
    "feeder_lines_collapse_non_kvs",
    "feeder_count_delta_label_aware",
    "feeder_count_delta_terminal_topology",
    "feeder_count_delta_expand_all",
    "feeder_count_delta_collapse_non_kvs",
    "buildings_per_feeder",
]


def write_input_audit(
    synthetic_df: pd.DataFrame,
    real_df: pd.DataFrame,
    output_dir: Path,
    output_suffix: str = "",
) -> pd.DataFrame:
    """Write a compact source-level audit for the comparison inputs."""
    frames = []
    if not synthetic_df.empty:
        synth = synthetic_df.copy()
        synth["source"] = "Synthetic"
        frames.append(synth)
    if not real_df.empty:
        real = real_df.copy()
        real["source"] = "Real"
        frames.append(real)
    if not frames:
        return pd.DataFrame()

    audit_df = pd.concat(frames, ignore_index=True, sort=False)
    id_cols = [
        col
        for col in ["source", "grid_result_id", "kcid", "bcid", "grid_name", "file_name", "power_flow_status"]
        if col in audit_df.columns
    ]
    selected_cols = id_cols + [col for col in AUDIT_COLUMNS if col in audit_df.columns]
    audit_df = audit_df[selected_cols]
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / metric_filename("comparison_input_audit.csv", output_suffix)
    audit_df.to_csv(csv_path, index=False)
    print(f"Saved comparison input audit to {csv_path}")
    return audit_df


__all__ = ["write_input_audit"]
