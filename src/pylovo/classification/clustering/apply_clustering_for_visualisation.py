"""Step 7 of the classification: cluster the sample grids and store the result.

The result in ``pylovo.transformer_classified`` (clusters and representative
grids per transformer position) can be visualised in QGIS.
"""
from pylovo.classification.database_communication.database_communication import DatabaseCommunication


def apply_clustering_for_visualisation(
    list_of_clustering_parameters: list | None = None,
    n_clusters_kmeans: int | None = None,
    n_clusters_gmm: int | None = None,
) -> None:
    """Cluster the grids of the current classification version and write ``transformer_classified``.

    ``None`` uses the value of ``config_clustering.yaml`` loaded at start-up, see
    :meth:`DatabaseCommunication.save_transformers_with_classification_info`.

    Args:
        list_of_clustering_parameters: Columns used for clustering.
        n_clusters_kmeans: Number of k-means clusters.
        n_clusters_gmm: Number of GMM components.
    """
    dc = DatabaseCommunication()
    dc.save_transformers_with_classification_info(
        list_of_clustering_parameters=list_of_clustering_parameters,
        n_clusters_kmeans=n_clusters_kmeans,
        n_clusters_gmm=n_clusters_gmm,
    )


def main():
    apply_clustering_for_visualisation()


if __name__ == "__main__":
    main()
