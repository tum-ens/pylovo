"""File-based street input: segment_intersecting_ways re-reads ways_tem (SQL text; tests/test_postgis_fixtures.py runs it)."""
from pathlib import Path

SQL = (Path(__file__).resolve().parents[1] / "src" / "pylovo" / "ways_preprocessing_functions" / "core"
       / "segment_intersecting_ways.sql")


def test_every_pass_reads_the_current_ways_until_no_crossing_is_left():
    text = " ".join(SQL.read_text().split())
    assert "for current_id in SELECT way_id FROM ways_tem ORDER BY way_id" in text
    assert "SELECT geom, clazz, way_id INTO way FROM ways_tem WHERE way_id = current_id;" in text
    assert "IF NOT FOUND OR ST_Length(way.geom) = 0 THEN continue;" in text      # deleted earlier in the pass
    assert "EXIT WHEN splits = 0;" in text
    assert "for way in SELECT geom, clazz, way_id FROM ways_tem loop" not in text  # no stale snapshot of the rows


def test_only_a_crossing_in_one_point_is_split():
    text = " ".join(SQL.read_text().split())
    assert ("AND ST_GeometryType(ST_Intersection(ST_LineSubstring(way.geom, 0.01, 0.99), w.geom)) = 'ST_Point'"
            in text)
