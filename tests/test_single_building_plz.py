"""A PLZ with a single loaded building gets a grid; elsewhere one-building street components are dropped (no database)."""

import logging

import numpy as np
import pytest

from pylovo.grid_generator import GridGenerator


class Db:
    """Street components with two vertices each; the buildings of a component sit at its first vertex."""

    def __init__(self, buildings_per_component):
        self.buildings = {10 * c: n for c, n in buildings_per_component.items()}
        self.component = np.repeat(list(buildings_per_component), 2)
        self.vertices = np.array([10 * c + k for c in buildings_per_component for k in (0, 1)])
        self.kept, self.dropped = [], []

    def get_connected_component(self):
        return self.component, self.vertices

    def count_connected_buildings(self, vertices):
        return sum(self.buildings.get(int(v), 0) for v in vertices)

    def delete_ways(self, vertices):
        self.dropped.append(int(vertices[0]) // 10)

    def delete_transformers_from_buildings_tem(self, vertices):
        pass

    def update_kmeans_cluster(self, vertices):
        self.kept.append(int(vertices[0]) // 10)

    def count_no_kmean_buildings(self):
        return 0


@pytest.mark.parametrize("buildings_per_component, kept, dropped", [
    ({1: 1}, [1], []),               # the only building of the PLZ, one street component
    ({1: 1, 2: 0}, [1], [2]),        # the only building, a second component without buildings
    ({1: 1, 2: 1}, [], [1, 2]),      # two isolated buildings: dropped as before
    ({1: 3, 2: 1}, [1], [2]),        # an isolated building next to a grid: dropped as before
])
def test_only_the_single_building_of_a_plz_keeps_its_component(buildings_per_component, kept, dropped):
    gg = GridGenerator.__new__(GridGenerator)
    gg.dbc, gg.logger = Db(buildings_per_component), logging.getLogger("single-building-test")
    gg.apply_kmeans_clustering()
    assert (gg.dbc.kept, gg.dbc.dropped) == (kept, dropped)
