"""The transformer connection lines get their way ids in a fixed order (brownfield runs are reproducible)."""
from pathlib import Path

SQL = (Path(__file__).resolve().parents[1] / "src" / "pylovo" / "ways_preprocessing_functions" / "core"
       / "generate_transformer_to_way_connections_infdb.sql")


def test_connection_loops_are_ordered():
    text = SQL.read_text()
    assert "FOR r IN SELECT * FROM temp_transformer_connection_candidates_infdb ORDER BY objectid" in text
    assert "FOR s IN SELECT * FROM grouped_splits ORDER BY old_way_id LOOP" in text
