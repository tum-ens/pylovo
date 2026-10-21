"""set_vertice_id picks vertex and connection point with a full ORDER BY, not by physical row order (no database)."""

from pylovo.database.preprocessing_mixin import PreprocessingMixin


class Cursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append(" ".join(query.split()))

    def fetchone(self):
        return (0,)


def test_vertex_and_connection_point_selections_have_no_open_ties():
    client = PreprocessingMixin.__new__(PreprocessingMixin)
    client.cur = Cursor()
    assert client.set_vertice_id() == 0
    vertex, connection_point = client.cur.queries[:2]
    # a centroid within 1e-6 m of two vertices: the nearer one, then the lower vertex id
    assert "ORDER BY ST_Distance(v.geom, b.centroid), v.id LIMIT 1" in vertex
    # several edges starting at one vertex: the connection line (clazz 110) first, then the lower way_id
    assert "FROM ways_tem WHERE source = b.vertice_id ORDER BY clazz IS DISTINCT FROM 110, way_id LIMIT 1" in connection_point
