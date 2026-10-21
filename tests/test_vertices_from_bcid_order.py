"""get_vertices_from_bcid orders vertices of equal routed distance by vertex ID (no database)."""

import logging

from pylovo.database.grid_mixin import GridMixin


class Cursor:
    """Answers the five queries of ``get_vertices_from_bcid`` and records their text."""

    def __init__(self):
        self.queries, self.rows = [], []

    def execute(self, query, params=None):
        text = " ".join(query.split())
        self.queries.append(text)
        if "FROM pylovo.grid_result" in text:
            self.rows = [(7, 400)]                          # ont_vertice_id, transformer_rated_power
        elif "SELECT vertice_id" in text:
            self.rows = [(11,), (12,)]                      # consumer vertices
        elif "SELECT DISTINCT COALESCE" in text:
            self.rows = [(21,), (22,)]                      # connection points
        elif "SELECT DISTINCT node, agg_cost" in text:
            self.rows = [(7, 0.0), (21, 30.0), (22, 30.0), (11, 35.0), (12, 35.0)]
        else:
            self.rows = []                                  # paths

    def fetchall(self):
        return self.rows


def test_equal_distances_are_ordered_by_vertex_id():
    client = GridMixin.__new__(GridMixin)
    client.cur, client.logger = Cursor(), logging.getLogger("vertices-from-bcid-test")
    costs, ont, _paths = client.get_vertices_from_bcid(91301, 1, 2)
    assert ont == 7 and list(costs) == [21, 22, 11, 12]     # the transformer vertex itself is no target
    # the feeder planning takes "the first one on ties", so the SQL must not leave the tie order open
    routing = next(q for q in client.cur.queries if "SELECT DISTINCT node, agg_cost" in q)
    assert routing.endswith("ORDER BY agg_cost, node;")
