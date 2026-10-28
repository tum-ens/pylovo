"""Select the clustering parameters with a varimax-rotated factor analysis."""
import warnings

import numpy as np
import pandas as pd
from factor_analyzer import FactorAnalyzer

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


def number_of_factors(df_candidates: pd.DataFrame) -> int:
    """Return the number of factors: the eigenvalues of the correlation matrix above 1, at least one.

    Args:
        df_candidates: Candidate key figures (:func:`factor_analysis_input`).

    Raises:
        ValueError: If the data cannot be analysed, e.g. a missing value.
        numpy.linalg.LinAlgError: If the correlation matrix stays singular.
    """
    with warnings.catch_warnings():
        # factor-analyzer 0.5.1 passes an argument that scikit-learn 1.6 renamed; nothing to act on here.
        warnings.filterwarnings("ignore", message="'force_all_finite' was renamed", category=FutureWarning)
        fa = FactorAnalyzer()
        try:
            fa.fit(df_candidates)
        except np.linalg.LinAlgError:
            # Fallback when the correlation matrix is still singular due to numerical issues.
            fa = FactorAnalyzer(use_smc=False)
            fa.fit(df_candidates)
    eigenvalues = fa.get_eigenvalues()[0]
    return max(int((eigenvalues > 1).sum()), 1)


def get_parameters_for_clustering(additional_filtering: bool | None = None) -> list:
    """Return the clustering parameters for the active classification version.

    The number of factors is the number of eigenvalues above 1 (at least one, :func:`number_of_factors`).
    For each factor of the varimax-rotated factor analysis of the candidates (:func:`factor_analysis_input`) the
    parameter with the highest absolute loading is selected. Warnings are suppressed.

    Args:
        additional_filtering: Also leave out the grids below the four lower thresholds.
            ``None`` uses the choice of the classification (``DatabaseCommunication.get_classification_choices``).

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
        df = factor_analysis_input(dc.get_clustering_parameters_for_classification_version(additional_filtering))
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
