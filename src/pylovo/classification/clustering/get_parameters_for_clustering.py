"""Select the clustering parameters with a varimax-rotated factor analysis."""
import warnings

import numpy as np
import pandas as pd
from scipy import stats

from pylovo.classification.clustering.transform import transform_key_figures
from pylovo.classification.database_communication.database_communication import DatabaseCommunication
from pylovo.plotting.classification import get_parameters_for_clustering as select_clustering_parameters

# Identifiers and parameters that are excluded from the factor analysis.
EXCLUDED_COLUMNS = [
    'version_id', 'plz', 'bcid', 'kcid', 'ratio', 'osm_trafo', 'house_distance_km', 'no_connection_buses',
    'resistance', 'reactance', 'simultaneous_peak_load_mw', 'no_household_equ', 'max_power_mw',
]


def factor_analysis_input(df_parameters: pd.DataFrame) -> pd.DataFrame:
    """Return the candidate key figures of the factor analysis.

    Drops ``EXCLUDED_COLUMNS`` and every column that is perfectly collinear with an earlier one (such a column makes
    the correlation matrix singular).

    Args:
        df_parameters: Key figures of the grids, e.g. from
            ``DatabaseCommunication.get_clustering_parameters_for_classification_version``.

    Returns:
        pd.DataFrame: The candidate columns.
    """
    df = df_parameters.drop(columns=EXCLUDED_COLUMNS, errors="ignore")
    corr = df.corr().abs()
    upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
    collinear_cols = [col for col in upper.columns if any(upper[col] >= 0.999999999)]
    return df.drop(columns=collinear_cols)


def _complete_values(df_candidates: pd.DataFrame) -> np.ndarray:
    """Return the candidates as a float array; a missing value is an error, never imputed.

    Raises:
        ValueError: If a candidate has a missing value (NaN), naming the candidates.
    """
    values = df_candidates.to_numpy(dtype=float)
    missing = np.isnan(values).any(axis=0)
    if missing.any():
        raise ValueError("The factor analysis needs complete data, missing values (NaN) in: "
                         + ", ".join(map(str, df_candidates.columns[missing])))
    return values


def correlation_matrix(df_candidates: pd.DataFrame) -> np.ndarray:
    """Return the Pearson correlation matrix of the candidates.

    Raises:
        ValueError: If a candidate has a missing value (NaN) or the same value in every grid.
    """
    values = _complete_values(df_candidates)
    std = values.std(axis=0)
    if (std == 0).any():
        raise ValueError("A candidate has the same value in every grid: "
                         + ", ".join(map(str, df_candidates.columns[std == 0])))
    z = (values - values.mean(axis=0)) / std
    return z.T @ z / len(z)


def candidate_eigenvalues(df_candidates: pd.DataFrame) -> np.ndarray:
    """Return the eigenvalues of the correlation matrix of the candidates, largest first.

    Args:
        df_candidates: Candidate key figures (:func:`factor_analysis_input`).

    Raises:
        ValueError: If the data cannot be analysed: a missing value (NaN) or a candidate that is the same in every
            grid.
    """
    return np.linalg.eigvalsh(correlation_matrix(df_candidates))[::-1]


def bartlett_sphericity(df_candidates: pd.DataFrame) -> tuple[float, float]:
    """Return Bartlett's test of sphericity: chi-square and p-value of the hypothesis of uncorrelated candidates.

    ``chi2 = -(n - 1 - (2p + 5) / 6) * ln det R`` with ``p (p - 1) / 2`` degrees of freedom (n grids, p candidates,
    R the correlation matrix). A singular R gives an infinite chi-square and p = 0.

    Raises:
        ValueError: As :func:`correlation_matrix`.
    """
    n, p = df_candidates.shape
    sign, logdet = np.linalg.slogdet(correlation_matrix(df_candidates))
    chi2 = np.inf if sign <= 0 else float(-(n - 1 - (2 * p + 5) / 6) * logdet)
    return chi2, float(stats.chi2.sf(chi2, p * (p - 1) / 2))


def kmo(df_candidates: pd.DataFrame) -> tuple[pd.Series, float]:
    """Return the Kaiser-Meyer-Olkin criterion per candidate and overall.

    KMO compares the squared correlations with the squared partial correlations (from the inverse of the correlation
    matrix; the pseudo-inverse if it is singular): ``sum r^2 / (sum r^2 + sum q^2)`` over the pairs.

    Raises:
        ValueError: As :func:`correlation_matrix`.
    """
    r = correlation_matrix(df_candidates)
    try:
        inverse = np.linalg.inv(r) if np.linalg.det(r) > np.finfo(np.float32).eps else np.linalg.pinv(r)
    except np.linalg.LinAlgError:
        inverse = np.linalg.pinv(r)
    scale = np.sqrt(np.outer(np.diag(inverse), np.diag(inverse)))
    partial = -inverse / scale
    np.fill_diagonal(r, 0)
    np.fill_diagonal(partial, 0)
    r2, q2 = r ** 2, partial ** 2
    per_candidate = r2.sum(axis=0) / (r2.sum(axis=0) + q2.sum(axis=0))
    return pd.Series(per_candidate, index=df_candidates.columns), float(r2.sum() / (r2.sum() + q2.sum()))


def number_of_factors(df_candidates: pd.DataFrame) -> int:
    """Return the number of factors: the eigenvalues of the correlation matrix above 1 (Kaiser), at least one.

    Args:
        df_candidates: Candidate key figures (:func:`factor_analysis_input`).

    Raises:
        ValueError: If the data cannot be analysed, e.g. a missing value.
    """
    return max(int((candidate_eigenvalues(df_candidates) > 1).sum()), 1)


def get_parameters_for_clustering(additional_filtering: bool | None = None, transform: str | None = None) -> list:
    """Return the clustering parameters for the active classification version.

    The candidates (:func:`factor_analysis_input`) are transformed like the clustering parameters
    (:func:`~pylovo.classification.clustering.transform.transform_key_figures`). The number of factors is the number
    of eigenvalues above 1 (at least one, :func:`number_of_factors`). For each factor of the varimax-rotated factor
    analysis of the candidates the parameter with the highest absolute loading is selected. Warnings are suppressed.

    Args:
        additional_filtering: Also leave out the grids below the four lower thresholds.
            ``None`` uses the choice of the classification (``DatabaseCommunication.get_classification_choices``).
        transform: Transform of the key figures. ``None`` uses the choice of the classification.

    Returns:
        list: Names of the selected parameters (one per factor).

    Raises:
        ValueError: If the factor analysis fails, e.g. for a very small sample or a parameter that is the
            same in every grid.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # get grid data
        dc = DatabaseCommunication()
        if transform is None:
            transform = dc.get_classification_choices()["transform"]
        df = transform_key_figures(
            factor_analysis_input(dc.get_clustering_parameters_for_classification_version(additional_filtering)),
            transform)
        try:
            return select_clustering_parameters(df_plz_parameters=df, n_comps=number_of_factors(df))
        except (ValueError, np.linalg.LinAlgError) as exc:
            raise ValueError(
                f"Automatic parameter selection failed for {len(df)} grids and {df.shape[1]} parameters: {exc}. "
                "A very small sample or a parameter that is the same in every grid cannot be analysed. "
                "Use a larger sample (N_SAMPLES) or set LIST_OF_CLUSTERING_PARAMETERS in config_clustering.yaml."
            ) from exc


def main():
    print(get_parameters_for_clustering())


if __name__ == '__main__':
    main()
