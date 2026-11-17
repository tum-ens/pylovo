"""The transformer connection lines get their way ids in a fixed order (brownfield runs are reproducible)."""
from pathlib import Path

SQL = (Path(__file__).resolve().parents[1] / "src" / "pylovo" / "ways_preprocessing_functions" / "core"
       / "generate_transformer_to_way_connections_infdb.sql")


def test_connection_loops_are_ordered():
    text = SQL.read_text()
    assert "FOR r IN SELECT * FROM temp_transformer_connection_candidates_infdb ORDER BY objectid" in text
    assert "FOR s IN SELECT * FROM grouped_splits ORDER BY old_way_id LOOP" in text


def test_a_transformer_on_a_street_is_placed_on_it_instead_of_linked_to_a_farther_street():
    infdb = " ".join(SQL.read_text().split())
    assert "ST_Distance(t.centroid, w.geom) > 0.1" not in infdb       # the street it stands on is not skipped
    assert "IF ST_Distance(r.centroid, r.old_geom) <= 0.1 THEN" in infdb
    assert "UPDATE buildings_tem SET centroid = r.connection_point WHERE objectid = r.objectid AND type = 'Transformer';" in infdb


def test_a_transformer_on_a_street_is_its_own_connection_point():
    from pylovo.database.preprocessing_mixin import PreprocessingMixin

    class Cursor:
        def __init__(self):
            self.statements = []

        def execute(self, query, params=None):
            self.statements.append(" ".join(query.split()))

        def fetchone(self):
            return (0,)

    client = PreprocessingMixin.__new__(PreprocessingMixin)
    client.cur = Cursor()
    client.set_vertice_id()
    assert ("WHEN b.type = 'Transformer' THEN COALESCE((SELECT target FROM ways_tem WHERE source = b.vertice_id "
            "AND clazz = 110 ORDER BY way_id LIMIT 1), b.vertice_id)") in client.cur.statements[1]
