import warnings
import numpy as np
from factor_analyzer import FactorAnalyzer

from pylovo.classification.database_communication.database_communication import DatabaseCommunication
from pylovo.plotting.classification import get_parameters_for_clustering as select_clustering_parameters

warnings.filterwarnings('ignore')

def get_parameters_for_clustering() -> list:
    """Return optimal clustering parameters for the active classification version."""
    # get grid data
    dc = DatabaseCommunication()
    df = dc.get_clustering_parameters_for_classification_version()

    # Dropping unnecessary columns
    df.drop(['version_id', 'plz', 'bcid', 'kcid', 'ratio', 'osm_trafo', 'house_distance_km', 'no_connection_buses',
             'resistance', 'reactance', 'simultaneous_peak_load_mw',
             'no_household_equ', 'max_power_mw'], axis=1, inplace=True)

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

    # Check Eigenvalues
    ev = fa.get_eigenvalues()

    # get the eigenvalues larger than 1.
    # --> This is the appropriate number of factors
    no_of_factors = int((ev[0] > 1).sum())
    if no_of_factors <= 0:
        no_of_factors = 1

    return select_clustering_parameters(df_plz_parameters=df, n_comps=no_of_factors)


def main():
    print(get_parameters_for_clustering())


if __name__ == '__main__':
    main()