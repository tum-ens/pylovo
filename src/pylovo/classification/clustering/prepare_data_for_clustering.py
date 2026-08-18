# This script performs all processes of representative grid generation
# Enter the settings for the clustering process in config_classification and clustering.config_clustering
# also refer to the documentation of the classification

import os
import sys
import time

import pylovo.database.database_client as dbc

from pylovo.classification.clustering.filter_grids import apply_filter_to_grids
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo.data_import.import_buildings import import_buildings_for_multiple_plz
from pylovo.classification.sampling.sample import get_sample_set   , create_sample_set
from pylovo.grid_generator import GridGenerator

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

def prepare_data_for_clustering(
    additional_filtering: bool = False,
    sample_from_postcode_result_only: bool = False,
) -> None:
    # %% 1. create a sample set of PLZ for your classification
    # This takes a few seconds

    create_sample_set(restrict_to_postcode_result=sample_from_postcode_result_only)
    samples = get_sample_set()

    # %% 2. import the buildings for the grid generation from the building database
    # importing a single shape file takes a few minutes. Importing the buildings for a whole set will take a few hours
    start_time = time.time()

    with dbc.DatabaseClient() as dbc_client:
        import_buildings_for_multiple_plz(samples, dbc_client)
    print("--- %s seconds for step 2: building import---" % (time.time() - start_time))

    # %% 3. generate the grids for your set
    # create new version if config_version, grid parameter
    # this takes around a quarter of an hour for a grid and might take a whole day for an entire set.
    # check whether grid was already generated

    start_time = time.time()
    # initialize GridGenerator with the provided postal code (PLZ)
    gg = GridGenerator()
    gg.generate_grid_for_multiple_plz(df_plz=samples, analyze_grids=True)
    print("--- %s seconds for step 3: grid generation---" % (time.time() - start_time))

    # %% 4. calculate grid parameters

    # timing of the script
    start_time = time.time()

    # create single ParameterCalculator instance for all PLZ
    pc = ParameterCalculator()
    # calculate network parameter for all plz
    for plz_index in samples['plz']:
        # compute parameters for plz
        pc.analyze_grid_parameters_for_plz(plz=plz_index)

    # end timing
    print("--- %s seconds parameter calculation---" % (time.time() - start_time))

    # %% 5. filter values
    apply_filter_to_grids(additional_filtering=additional_filtering)


def main():
    prepare_data_for_clustering()


if __name__ == "__main__":
    main()