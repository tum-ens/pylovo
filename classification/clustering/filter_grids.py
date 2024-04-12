from classification.database_communication.DatabaseCommunication import DatabaseCommunication


def apply_filter_to_grids():
    """apply thresholds set in config_clustering to clustering parameters.
    according to those thresholds the value in column 'filtered' of 'clustering_parameters'
    is set true or false
    """
    dc = DatabaseCommunication()
    dc.apply_max_trafo_dis_threshold()
    dc.apply_households_per_building_threshold()
    dc.set_remaining_filter_values_false()
