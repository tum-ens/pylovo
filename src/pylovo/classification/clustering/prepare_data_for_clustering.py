"""Steps 1-5 of the classification: sample PLZ, import buildings, generate and analyse grids, filter.

Configure the process in ``config/config_classification.yaml`` and
``config/config_clustering.yaml``; see also the classification documentation.
"""
import time

import pylovo.database.database_client as dbc

from pylovo.classification.clustering.filter_grids import apply_filter_to_grids
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo.data_import.import_buildings import import_buildings_for_multiple_plz
from pylovo.classification.sampling.sample import get_sample_set, create_sample_set
from pylovo.config_loader import USE_INFDB
from pylovo.grid_generator import GridGenerator


def prepare_data_for_clustering(
    additional_filtering: bool = False,
    sample_from_postcode_result_only: bool = False,
) -> None:
    """Prepare the grid parameters of a new sample set for clustering.

    Creates the sample set of ``CLASSIFICATION_VERSION``, imports the building
    shapefiles (only with ``USE_INFDB=False``), generates and analyses the grids of all sampled PLZ, computes the clustering
    parameters and applies the filters. This can take hours for a full sample set.

    Args:
        additional_filtering: Also apply the lower thresholds of the four
            clustering parameters, see :func:`apply_filter_to_grids`.
        sample_from_postcode_result_only: Only sample PLZ that already have a
            ``postcode_result`` entry for ``VERSION_ID``.
    """
    # %% 1. create a sample set of PLZ for your classification
    # This takes a few seconds
    create_sample_set(restrict_to_postcode_result=sample_from_postcode_result_only)
    samples = get_sample_set()

    # %% 2. file-based data path only (USE_INFDB=False): import the building shapefiles
    # importing a single shape file takes a few minutes. Importing the buildings for a whole set will take a few hours
    # With InfDB the grid generation reads the buildings directly (same as pylovo-generate).
    if not USE_INFDB:
        start_time = time.time()
        with dbc.DatabaseClient() as dbc_client:
            import_buildings_for_multiple_plz(samples, dbc_client)
        print("--- %s seconds for step 2: building import---" % (time.time() - start_time))

    # %% 3. generate the grids for your set
    # this takes around a quarter of an hour for a grid and might take a whole day for an entire set.
    start_time = time.time()
    gg = GridGenerator()
    gg.generate_grid_for_multiple_plz(df_plz=samples, analyze_grids=True)
    print("--- %s seconds for step 3: grid generation---" % (time.time() - start_time))

    # %% 4. calculate grid parameters
    start_time = time.time()
    # create single ParameterCalculator instance for all PLZ
    pc = ParameterCalculator()
    for plz_index in samples['plz']:
        pc.analyze_grid_parameters_for_plz(plz=plz_index)
    print("--- %s seconds parameter calculation---" % (time.time() - start_time))

    # %% 5. filter values
    apply_filter_to_grids(additional_filtering=additional_filtering)


def main():
    prepare_data_for_clustering()


if __name__ == "__main__":
    main()
