"""OSM completeness per settlement and land-use class (pylovo.mv_grid.osm_coverage). DB-free."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest
import shapely

from pylovo.mv_grid import osm_coverage as oc


def _objects(rows):
    return pd.DataFrame(rows, columns=["source_id", "kind", "tags", "x", "y", "area_m2"])


def test_station_objects_keep_mv_lv_sites_and_drop_higher_levels():
    objects = _objects([
        ("node/1", "substation", {"substation": "minor_distribution"}, 0.0, 0.0, 0.0),
        ("way/2", "substation", {"voltage": "110000;20000"}, 1000.0, 0.0, 6000.0),           # HV/MV site
        ("way/3", "substation", None, 2000.0, 0.0, 1500.0),                                  # large: HV/MV site
        ("way/4", "substation", json.dumps({"voltage": "20000"}), 3000.0, 0.0, 1500.0),      # large but MV only
        ("way/5", "substation", {"substation": "traction"}, 4000.0, 0.0, 300.0),
        ("node/6", "transformer", {"voltage": "20000;400"}, 5000.0, 0.0, 0.0),
        ("node/7", "transformer", {"voltage": "20000"}, 1010.0, 0.0, 0.0),                   # inside site way/2
        ("way/8", "transformer_tower", {"building": "transformer_tower"}, 6000.0, 0.0, 20.0),
        ("node/9", "substation", {"substation": "minor_distribution"}, 6008.0, 0.0, 0.0),    # the same site
        ("node/10", "pole", {"transformer": "distribution"}, 7000.0, 0.0, 0.0),
        ("node/11", "pole", {}, 8000.0, 0.0, 0.0),
        ("node/12", "substation", {"disused:power": "substation"}, 9000.0, 0.0, 0.0),
        ("node/13", "transformer", {"voltage": "110000"}, 10000.0, 0.0, 0.0),
        ("node/14", "substation", {"substation": "minor_distribution"}, math.nan, 0.0, 0.0),
    ])
    stations, hv = oc.station_objects(objects)
    assert sorted(stations["source_id"]) == ["node/1", "node/10", "node/6", "way/4", "way/8"]
    assert sorted(hv["source_id"]) == ["way/2", "way/3"]
    assert stations.set_index("source_id").at["way/8", "kind"] == "transformer_tower"   # the tower wins the merge


def test_station_objects_merge_close_features_once():
    objects = _objects([(f"node/{i}", "substation", {}, 10.0 * i, 0.0, 0.0) for i in range(5)])
    stations, _ = oc.station_objects(objects, merge_m=15.0)
    assert stations["source_id"].tolist() == ["node/0", "node/2", "node/4"]
    assert oc.station_objects(objects.iloc[0:0])[0].empty


def test_classes_have_no_overlaps_and_follow_the_priority():
    region = shapely.box(0, 0, 1000, 1000)
    landuse = pd.DataFrame({"osm_type": ["residential", "industrial", "farmland", "cemetery"],
                            "geometry": [shapely.box(0, 0, 600, 1000), shapely.box(500, 0, 700, 500),
                                         shapely.box(700, 0, 1000, 500), shapely.box(800, 800, 900, 900)]})
    geoms = oc.land_use_geometries(landuse, region)
    assert list(geoms) == list(oc.LAND_USE_ORDER)
    assert geoms["industrial"].area == pytest.approx(200 * 500)
    assert geoms["residential"].area == pytest.approx(600 * 1000 - 100 * 500)     # minus the industrial overlap
    assert geoms["other"].area == pytest.approx(100 * 100)
    assert sum(g.area for g in geoms.values()) == pytest.approx(region.area)
    classes = oc.classify_points(np.array([[550, 100], [100, 900], [850, 850], [900, 700]]), geoms)
    assert classes.tolist() == ["industrial", "residential", "other", "none"]
    settlements = pd.DataFrame({"urban": [True, False], "geometry": [shapely.box(0, 0, 300, 300),
                                                                    shapely.box(200, 200, 500, 500)]})
    sgeoms = oc.settlement_geometries(settlements, region)
    assert list(sgeoms) == ["town", "village", "open_field"]
    assert sgeoms["village"].area == pytest.approx(300 * 300 - 100 * 100)
    assert oc.classify_points(np.array([[250, 250], [400, 400], [900, 900]]), sgeoms).tolist() == [
        "town", "village", "open_field"]


def test_coverage_table_record_and_line():
    region = shapely.box(0, 0, 2000, 1000)
    settlements = pd.DataFrame({"urban": [True], "geometry": [shapely.box(0, 0, 1000, 1000)]})
    geoms = oc.settlement_geometries(settlements, region)
    stations = np.array([[100, 100], [200, 200], [300, 300], [1500, 500], [5000, 5000]])   # the last one outside
    osm = np.array([[110, 100], [1510, 500], [1600, 500]])
    lines = [shapely.LineString([(500, 500), (1500, 500)]), shapely.LineString([(1500, 500), (2500, 500)])]
    table = oc.coverage_table(region, geoms, stations, osm, lines)
    assert table.loc["town", ["stations", "osm_stations"]].tolist() == [3, 1]
    assert table.loc["open_field", ["stations", "osm_stations"]].tolist() == [1, 2]
    assert table.at["village", "stations"] == 0 and math.isnan(table.at["village", "osm_per_station"])
    assert table.at["all", "osm_per_station"] == pytest.approx(3 / 4)
    assert table.at["town", "osm_line_km"] == pytest.approx(0.5) and table.at["all", "osm_line_km"] == pytest.approx(1.5)
    assert table.at["all", "area_km2"] == pytest.approx(2.0)
    record = oc.coverage_record(table, table, n_hv_sites=2)
    json.dumps(record, allow_nan=False)
    assert record["settlement"][-1]["class"] == "all" and record["n_hv_sites"] == 2
    line = oc.coverage_line(record)
    assert line.startswith("✓ OSM coverage: 3 mapped MV/LV station sites for 4 planned stations (75 %")
    assert "town 33 %" in line and "open field 200 %" in line and "village" not in line
    assert oc.coverage_line({}) == "✗ OSM coverage: not computed"


def test_summarise_runs_both_tables():
    region = shapely.box(0, 0, 1000, 1000)
    objects = _objects([("node/1", "substation", {"substation": "minor_distribution"}, 100.0, 100.0, 0.0)])
    settlements = pd.DataFrame({"urban": [False], "geometry": [shapely.box(0, 0, 500, 500)]})
    landuse = pd.DataFrame({"osm_type": ["residential"], "geometry": [shapely.box(0, 0, 500, 500)]})
    by_settlement, by_land_use, record = oc.summarise(region, objects, np.array([[120.0, 100.0]]), settlements,
                                                      landuse)
    assert by_settlement.at["village", "osm_per_station"] == 1.0
    assert by_land_use.at["residential", "osm_stations"] == 1
    assert [c["class"] for c in record["land_use"]] == [*oc.LAND_USE_ORDER, "all"]
