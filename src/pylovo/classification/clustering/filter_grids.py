"""Mark grids that are excluded from clustering (column ``filtered`` of ``clustering_parameters``)."""
from pylovo.classification.database_communication.database_communication import DatabaseCommunication


def apply_filter_to_grids(additional_filtering: bool = False) -> None:
    """Apply the thresholds of ``config_clustering.yaml`` to the clustering parameters.

    Grids above the maximum transformer distance or with too many households per
    building are always filtered; all remaining grids get ``filtered = false``.

    Args:
        additional_filtering: Also filter grids below the thresholds of
            ``avg_trafo_dis``, ``no_house_connections``, ``vsw_per_branch`` and
            ``no_households`` (removes small "filling" grids).
    """
    dc = DatabaseCommunication()
    dc.apply_max_trafo_dis_threshold()
    dc.apply_households_per_building_threshold()
    if additional_filtering:
        dc.apply_list_of_clustering_parameters_thresholds()
    dc.set_remaining_filter_values_false()
