import numpy as np

from pylovo.database.clustering_mixin import ClusteringMixin


def test_connected_components_are_canonical_independent_of_database_row_order():
    first_result = [(20, 9), (10, 4), (20, 7), (10, 3)]
    second_result = [(101, 7), (55, 3), (101, 9), (55, 4)]

    first_component, first_nodes = ClusteringMixin._canonical_connected_components(first_result)
    second_component, second_nodes = ClusteringMixin._canonical_connected_components(second_result)

    np.testing.assert_array_equal(first_component, second_component)
    np.testing.assert_array_equal(first_nodes, second_nodes)
