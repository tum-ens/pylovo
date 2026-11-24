"""The steps after the districts without a database: the OSM coverage report and the display geometry."""
from __future__ import annotations

import logging

import pandas as pd
import shapely

from pylovo.mv_grid import post_steps


class CoverageDb:
    """The reads of the OSM coverage report."""

    def __init__(self):
        self.rolled_back = 0

    def settlement_polygons(self, graph_id, region_wkb):
        return pd.DataFrame({"settlement_id": [1], "urban": [True], "geom_wkb": [shapely.box(0, 0, 50, 50).wkb]})

    def osm_landuse(self, region_wkb):
        return pd.DataFrame({"osm_type": ["residential"], "geom_wkb": [shapely.box(0, 0, 100, 50).wkb]})

    def variant_node_table(self, version_id, variant_id):
        return pd.DataFrame({"kind": ["busbar", "station", "station"], "x": [0.0, 10.0, 80.0], "y": [0.0, 10.0, 80.0]})

    def osm_line_geometries(self, graph_id, region_wkb):
        return [shapely.LineString([(0, 90), (100, 90)]).wkb]

    def osm_power_objects(self, region_wkb):
        return pd.DataFrame({"source_id": ["node/1"], "kind": ["substation"], "tags": ['{"substation": "minor_distribution"}'],
                             "x": [12.0], "y": [10.0], "area_m2": [0.0]})

    def rollback_changes(self):
        self.rolled_back += 1


def test_osm_coverage_report_and_its_fallback(caplog):
    region = shapely.box(0, 0, 100, 100).wkb
    db = CoverageDb()
    record = post_steps.osm_coverage_report(db, "mv1", "d0", region, "g1", logging.getLogger("t"))
    town = next(c for c in record["settlement"] if c["class"] == "town")
    assert (town["stations"], town["osm_stations"]) == (1, 1) and db.rolled_back == 1
    total = next(c for c in record["settlement"] if c["class"] == "all")
    assert (total["stations"], total["osm_stations"], total["osm_line_km"]) == (2, 1, 0.1)
    assert [c["class"] for c in record["land_use"]][:3] == ["industrial", "commercial", "residential"]
    broken = CoverageDb()
    broken.osm_power_objects = lambda region_wkb: (_ for _ in ()).throw(RuntimeError("no OSM schema"))
    with caplog.at_level("WARNING"):
        assert post_steps.osm_coverage_report(broken, "mv1", "d0", region, "g1", logging.getLogger("t")) == {}
    assert "OSM coverage skipped: RuntimeError: no OSM schema" in caplog.text and broken.rolled_back == 1


class DisplayDb:
    def __init__(self, fail=False):
        self.saved, self.rollbacks, self.fail = None, 0, fail

    def display_input(self, version_id, variant_id):
        if self.fail:
            raise RuntimeError("no column display_geom")
        a = shapely.LineString([(0, 0), (300, 0), (300, 200)])
        b = shapely.LineString([(0, 0), (300, 0), (300, -200)])
        from pylovo.mv_grid.display import DisplayLine
        return [DisplayLine(1, a, 10, 11), DisplayLine(2, b, 10, 12)], {10: (0.0, 0.0)}

    def save_display_geometry(self, version_id, variant_id, lines, busbars):
        self.saved = (version_id, variant_id, sorted(lines), sorted(busbars))

    def rollback_changes(self):
        self.rollbacks += 1


def test_display_geometry_is_stored_and_its_errors_only_logged():
    import logging

    db = DisplayDb()
    assert post_steps.display_geometry(db, "mv1", "d1", logging.getLogger("t")) == (2, 1)
    assert db.saved == ("mv1", "d1", [1, 2], [10])
    failing = DisplayDb(fail=True)
    assert post_steps.display_geometry(failing, "mv1", "d1", logging.getLogger("t")) is None
    assert failing.saved is None and failing.rollbacks == 1
