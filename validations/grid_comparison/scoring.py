"""Distribution-distance scores for grid-comparison metrics."""

from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance


def iter_real_grid_files(data_path: str) -> list[Path]:
    """Return LV subnet files for real-vs-synthetic comparison.

    The current DSO preprocessing writes two explicit variants: ``logical/`` for
    traceability and ``radialized/`` for comparison against PyLoVo's radial
    synthetic grids.  When the new layout is present, only regular radialized
    grids with an LV comparison load marker are used.  Older JSON layouts remain
    readable for transition periods.
    """
    path = Path(data_path)
    radialized = sorted((path / "radialized").glob("LV_*__radialized__regular__lvload.xlsx"))
    if radialized:
        return radialized

    legacy_radialized = sorted((path / "radialized").glob("LV_*__radialized__regular__ns.json"))
    if legacy_radialized:
        return legacy_radialized

    candidates = (
        sorted(path.glob("LV_*.xlsx"))
        + sorted(path.glob("LV_*.json"))
        + sorted((path / "regular").glob("LV_*.json"))
    )
    return [file_path for file_path in candidates if file_path.stem.startswith("LV_")]


CALIBRATED_WASSERSTEIN_THRESHOLDS: dict[str, tuple[float, float, float]] = {
    "transformer_mva": (0.1383, 0.1624, 0.2061),
    "avg_trafo_distance": (0.1803, 0.2097, 0.2774),
    "max_trafo_distance": (0.2240, 0.2608, 0.3433),
    "feeder_lines": (0.2720, 0.3140, 0.3975),
    "graph_length": (0.2423, 0.2824, 0.3618),
    "graph_resistance": (0.3289, 0.3790, 0.4816),
}

def get_wasserstein_thresholds(metric: str) -> tuple[float, float, float]:
    """Return the (excellent, good, acceptable) normalized-Wasserstein cutoffs for a metric.

    Used both for classification and for plotting per-metric threshold markers. There is
    deliberately no generic fallback: a new metric needs its own calibration run (natural
    real-vs-real variance differs per metric), so a made-up generic threshold would look
    legitimate in the output while never having been validated against real data.
    """
    try:
        return CALIBRATED_WASSERSTEIN_THRESHOLDS[metric]
    except KeyError:
        raise KeyError(
            f"No calibrated Wasserstein thresholds for metric '{metric}'. "
            "Run calibrate_wasserstein_thresholds.py to derive them, or add an entry to "
            "CALIBRATED_WASSERSTEIN_THRESHOLDS in scoring.py."
        ) from None


def _classify_normalized_wasserstein(metric: str, normalized_distance: float) -> str:
    """Map normalized Wasserstein distance to a qualitative fit category, using thresholds
    calibrated per metric against the natural real-vs-real variance (see
    CALIBRATED_WASSERSTEIN_THRESHOLDS above)."""
    excellent, good, acceptable = get_wasserstein_thresholds(metric)
    if normalized_distance <= excellent:
        return "excellent"
    if normalized_distance <= good:
        return "good"
    if normalized_distance <= acceptable:
        return "acceptable"
    return "poor"


def compute_wasserstein_summary(
    df: pd.DataFrame,
    metrics: list[str],
    source_col: str = "source",
    synthetic_label: str = "Synthetic",
    real_label: str = "Real",
) -> pd.DataFrame:
    """Compute Earth Mover's Distance (Wasserstein-1) per metric for synthetic vs real data.

    Returns one row per metric with:
    - `wasserstein_distance`: absolute EMD in metric units
    - `normalized_wasserstein`: EMD divided by pooled IQR (scale-free)
    - `quality`: qualitative interpretation based on normalized distance
    """
    if df.empty:
        return pd.DataFrame(
            columns=[
                "metric",
                "synthetic_n",
                "real_n",
                "wasserstein_distance",
                "pooled_iqr",
                "normalized_wasserstein",
                "quality",
            ]
        )

    if source_col not in df.columns:
        raise KeyError(f"Required source column '{source_col}' is missing.")

    rows = []
    for metric in metrics:
        if metric not in df.columns:
            continue

        synth_vals = pd.to_numeric(
            df.loc[df[source_col] == synthetic_label, metric], errors="coerce"
        ).dropna()
        real_vals = pd.to_numeric(
            df.loc[df[source_col] == real_label, metric], errors="coerce"
        ).dropna()

        if synth_vals.empty or real_vals.empty:
            rows.append(
                {
                    "metric": metric,
                    "synthetic_n": int(len(synth_vals)),
                    "real_n": int(len(real_vals)),
                    "wasserstein_distance": np.nan,
                    "pooled_iqr": np.nan,
                    "normalized_wasserstein": np.nan,
                    "quality": "insufficient_data",
                }
            )
            continue

        pooled = pd.concat([synth_vals, real_vals], ignore_index=True)
        q25, q75 = np.nanpercentile(pooled, [25, 75])
        pooled_iqr = float(q75 - q25)
        if pooled_iqr <= 1e-6:
            pooled_iqr = float(np.nanstd(pooled))
        if pooled_iqr <= 1e-6:
            pooled_iqr = 1.0

        emd = float(wasserstein_distance(synth_vals.to_numpy(), real_vals.to_numpy()))
        normalized = emd / pooled_iqr

        rows.append(
            {
                "metric": metric,
                "synthetic_n": int(len(synth_vals)),
                "real_n": int(len(real_vals)),
                "wasserstein_distance": emd,
                "pooled_iqr": pooled_iqr,
                "normalized_wasserstein": normalized,
                "quality": _classify_normalized_wasserstein(metric, normalized),
            }
        )

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    return result.sort_values("normalized_wasserstein", ascending=True).reset_index(drop=True)


__all__ = ["compute_wasserstein_summary", "get_wasserstein_thresholds", "iter_real_grid_files"]
