"""Distribution-distance scores for grid-comparison metrics."""

import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance


def _classify_normalized_wasserstein(normalized_distance: float) -> str:
    """Map normalized Wasserstein distance to a qualitative fit category."""
    if normalized_distance <= 0.10:
        return "excellent"
    if normalized_distance <= 0.25:
        return "good"
    if normalized_distance <= 0.50:
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
                "quality": _classify_normalized_wasserstein(normalized),
            }
        )

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    return result.sort_values("normalized_wasserstein", ascending=True).reset_index(drop=True)


__all__ = ["compute_wasserstein_summary"]
