Filter Grids
=============

Threshold values for grid parameters:
filter grids that are unrealistic and should thus not be considered for clustering.

This function checks which grids exceed the threshold values:

.. autofunction:: classification.clustering.filter_grids.apply_filter_to_grids

When the grids' parameters are retrieved via this function, the grids exceeding the thresholds are not returned:

.. autofunction:: classification.database_communication.DatabaseCommunication.DatabaseCommunication.get_clustering_parameters_for_classification_version

