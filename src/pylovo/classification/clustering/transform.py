"""Transform of the key figures before the factor analysis and the clustering (``TRANSFORM`` of config_clustering.yaml).

The key figures are heavy-tailed: a few long or large grids dominate the correlations of the factor analysis and the
distances of the clustering if the values are only standardised. ``yeo-johnson`` fits one power transform per key
figure (scikit-learn's ``PowerTransformer``, which also takes zeros), so that each one is close to symmetric. For
values of 0 or more it is a Box-Cox transform of ``x + 1``, so its result depends on the unit of the key figure (km or
m). ``median-yeo-johnson`` divides every key figure by its median first, which makes it free of the unit
(:func:`median_divisors`). ``log1p`` is ``log(1 + x)`` in the unit of the key figure: figures in km hardly change. The
standardisation follows in every case.
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import PowerTransformer

from pylovo.config_loader import TRANSFORMS


def median_divisors(df: pd.DataFrame) -> tuple[dict[str, float], dict[str, float], dict[str, str]]:
    """Return the divisor of every key figure of ``median-yeo-johnson``: its median, with a fallback.

    A key figure whose median is 0 (more than half of the grids have 0, e.g. a count that most grids lack) is divided
    by the mean of its absolute values instead, and one that is 0 in every grid by 1 (it stays 0). Missing values are
    ignored.

    Returns:
        tuple: The medians, the divisors and the rule of every divisor (``median``, ``mean`` or ``one``), each keyed by
        column.
    """
    medians, divisors, rules = {}, {}, {}
    for column in df.columns:
        values = df[column].astype(float)
        median = float(values.median())
        medians[column] = median
        if median != 0 and np.isfinite(median):
            divisors[column], rules[column] = abs(median), "median"
            continue
        mean = float(values.abs().mean())
        if mean != 0 and np.isfinite(mean):
            divisors[column], rules[column] = mean, "mean"
        else:
            divisors[column], rules[column] = 1.0, "one"
    return medians, divisors, rules


def transform_key_figures(df: pd.DataFrame, transform: str) -> pd.DataFrame:
    """Return the key figures of ``df`` transformed column by column, not standardised.

    Args:
        df: Key figures, one column each (not negative for ``log1p``).
        transform: One of :data:`~pylovo.config_loader.TRANSFORMS`.

    Returns:
        pd.DataFrame: The same index and columns. For ``yeo-johnson`` and ``median-yeo-johnson``, ``attrs["lambdas"]``
        holds the fitted exponent of every column, and for ``median-yeo-johnson`` ``attrs["medians"]``,
        ``attrs["divisors"]`` and ``attrs["divisor_rules"]`` hold the scaling of :func:`median_divisors`.

    Raises:
        ValueError: If ``transform`` is unknown.
    """
    values = df.astype(float)
    if transform == "none":
        return values
    if transform == "log1p":
        return np.log1p(values)
    if transform in ("yeo-johnson", "median-yeo-johnson"):
        scaling = None
        if transform == "median-yeo-johnson":
            scaling = median_divisors(values)
            values = values / pd.Series(scaling[1])
        power = PowerTransformer(method="yeo-johnson", standardize=False)
        out = pd.DataFrame(power.fit_transform(values), index=df.index, columns=df.columns)
        out.attrs["lambdas"] = {column: float(value) for column, value in zip(df.columns, power.lambdas_)}
        if scaling is not None:
            out.attrs["medians"], out.attrs["divisors"], out.attrs["divisor_rules"] = scaling
        return out
    raise ValueError(f"Unknown transform {transform!r}, use one of: {', '.join(TRANSFORMS)}")
