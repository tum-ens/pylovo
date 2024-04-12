Cluster the Grids
=================

With the clustering configuration set in :code:`config_clustering` the clustering is performed in the three functions:

.. autofunction:: classification.clustering.clustering_algorithms.kmedoids_clustering

.. autofunction:: classification.clustering.clustering_algorithms.gmm_tied_clustering

.. autofunction:: classification.clustering.clustering_algorithms.kmeans_clustering

In each of the functions above the following function is called:

.. autofunction:: classification.clustering.clustering_algorithms.reindex_cluster_indices

This is a feature for better interpretability of the returned representative grids and grid parameters.

The cluster results can be made visiualised in QGIS by running:

.. autofunction:: classification.database_communication.DatabaseCommunication.DatabaseCommunication.save_transformers_with_classification_info()
