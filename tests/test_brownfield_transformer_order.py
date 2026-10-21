"""The existing stations of a kcid: each vertex once, in a fixed order (``get_included_transformers``, no database)."""

from pylovo.database.clustering_mixin import ClusteringMixin


class Cursor:
    def __init__(self, rows):
        self.rows, self.queries = rows, []

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))

    def fetchall(self):
        return self.rows


def test_a_station_vertex_is_listed_once_and_in_ascending_order():
    client = ClusteringMixin.__new__(ClusteringMixin)
    client.cur = Cursor([(100,), (205,)])
    assert client.get_included_transformers(3) == [100, 205]
    query = client.cur.queries[0]
    # two transformer rows at one point share a vertex and must give one brownfield grid, not two;
    # the order numbers the brownfield bcids (-1, -2, ...) the same in every run
    assert query.startswith("SELECT DISTINCT vertice_id FROM buildings_tem")
    assert query.endswith("ORDER BY vertice_id;")


def test_a_kcid_without_stations_gives_an_empty_list():
    client = ClusteringMixin.__new__(ClusteringMixin)
    client.cur = Cursor([])
    assert client.get_included_transformers(3) == []
