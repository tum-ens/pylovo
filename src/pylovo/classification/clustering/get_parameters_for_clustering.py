"""Select the clustering parameters with a varimax-rotated factor analysis."""
import warnings

import numpy as np
from factor_analyzer import FactorAnalyzer

from pylovo.classification.database_communication.database_communication import DatabaseCommunication
from pylovo.plotting.classification import get_parameters_for_clustering as select_clustering_parameters

# Identifiers and parameters that are excluded from the factor analysis.
EXCLUDED_COLUMNS = [
    'version_id', 'plz', 'bcid', 'kcid', 'ratio', 'osm_trafo', 'house_distance_km', 'no_connection_buses',
    'resistance', 'reactance', 'simultaneous_peak_load_mw', 'no_household_equ', 'max_power_mw',
]


def get_parameters_for_clustering() -> list:
    """Return the clustering parameters for the active classification version.

    The number of factors is the number of eigenvalues above 1 (at least one).
    For each factor of the varimax-rotated factor analysis the parameter with the
    highest absolute loading is selected. Warnings are suppressed.

    Returns:
        list: Names of the selected parameters (one per factor).
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # get grid data
        dc = DatabaseCommunication()
        df = dc.get_clustering_parameters_for_classification_version()
        df = df.drop(EXCLUDED_COLUMNS, axis=1)

        # Remove perfectly collinear features to avoid singular correlation matrices in factor analysis.
        corr = df.corr().abs()
        upper = corr.where(np.triu(np.ones(corr.shape), k=1).astype(bool))
        collinear_cols = [col for col in upper.columns if any(upper[col] >= 0.999999999)]
        if collinear_cols:
            df = df.drop(columns=collinear_cols)

        # Create factor analysis object and perform factor analysis
        fa = FactorAnalyzer()
        try:
            fa.fit(df)
        except np.linalg.LinAlgError:
            # Fallback when the correlation matrix is still singular due to numerical issues.
            fa = FactorAnalyzer(use_smc=False)
            fa.fit(df)

        # The number of eigenvalues larger than 1 is the appropriate number of factors.
        ev = fa.get_eigenvalues()
        no_of_factors = max(int((ev[0] > 1).sum()), 1)

        return select_clustering_parameters(df_plz_parameters=df, n_comps=no_of_factors)


def main():
    print(get_parameters_for_clustering())


if __name__ == '__main__':
    main()
