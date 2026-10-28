"""Transform of the key figures before the factor analysis and the clustering (``TRANSFORM`` of config_clustering.yaml).

The key figures are heavy-tailed: a few long or large grids dominate the correlations of the factor analysis and the
distances of the clustering if the values are only standardised. ``yeo-johnson`` fits one power transform per key
figure (scikit-learn's ``PowerTransformer``, which also takes zeros), so that each one is close to symmetric. ``log1p``
is ``log(1 + x)`` in the unit of the key figure: figures in km hardly change. The standardisation follows in every case.
"""
import numpy as np
import pandas as pd
from sklearn.preprocessing import PowerTransformer

from pylovo.config_loader import TRANSFORMS


def transform_key_figures(df: pd.DataFrame, transform: str) -> pd.DataFrame:
    """Return the key figures of ``df`` transformed column by column, not standardised.

    Args:
        df: Key figures, one column each (not negative for ``log1p``).
        transform: One of :data:`~pylovo.config_loader.TRANSFORMS`.

    Returns:
        pd.DataFrame: The same index and columns. For ``yeo-johnson``, ``attrs["lambdas"]`` holds the fitted exponent
        of every column.

    Raises:
        ValueError: If ``transform`` is unknown.
    """
    values = df.astype(float)
    if transform == "none":
        return values
    if transform == "log1p":
        return np.log1p(values)
    if transform == "yeo-johnson":
        power = PowerTransformer(method="yeo-johnson", standardize=False)
        out = pd.DataFrame(power.fit_transform(values), index=df.index, columns=df.columns)
        out.attrs["lambdas"] = {column: float(value) for column, value in zip(df.columns, power.lambdas_)}
        return out
    raise ValueError(f"Unknown transform {transform!r}, use one of: {', '.join(TRANSFORMS)}")
