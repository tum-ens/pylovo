"""Tests of the layer registry: scopes, filters, availability, GeoJSON and attributes (fake database)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pylovo_viewer.catalog import Catalog
from pylovo_viewer.layers import LayerService, Query, build_layers, feature_collection

SCOPES = {"version", "region", "run", "run_bbox", "bbox", "germany", "file"}


@pytest.fixture()
def service(fake_db, tmp_path: Path) -> LayerService:
    return LayerService(fake_db, Catalog(fake_db), tmp_path)


def test_registry():
    layers = build_layers("opendata", "osm", "basedata")
    ids = [layer.id for layer in layers]
    assert len(ids) == len(set(ids))
    for layer in layers:
        assert layer.scope in SCOPES
        assert layer.geometry in {"point", "line", "polygon"}
        if layer.scope != "file":
            assert "AS fid" in layer.sql and "AS geometry" in layer.sql
        if layer.scope == "region":
            assert "{filter}" in layer.sql and layer.region_filter and layer.bbox_filter
        if layer.scope in ("bbox", "run_bbox"):
            assert "{env}" in layer.sql


def test_region_and_view_filters(service):
    cables = service.layers["lv_cables"]
    by_plz = service.sql(cables, Query(version="v1", plz=12345))
    assert "c.grid_result_id IN (SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(version)s " \
           "AND plz = %(plz)s)" in by_plz
    by_area = service.sql(cables, Query(version="v1", mv="d1", area=7))
    assert "mv_grid_id = %(area)s" in by_area
    by_view = service.sql(cables, Query(version="v1", bbox=(11.0, 48.0, 11.1, 48.1)))
    assert "ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326), 25832)" in by_view
    assert "{" not in by_plz + by_area + by_view


@pytest.mark.parametrize("layer, query, message", [
    ("lv_stations", Query(), "needs version"),
    ("lv_cables", Query(version="v1"), "plz, area, grid or bbox"),
    ("lv_cables", Query(version="v1", area=3), "needs mv"),
    ("mv_lines", Query(version="v1"), "needs mv"),
    ("in_buildings", Query(), "needs bbox"),
])
def test_missing_parameters(service, layer, query, message):
    with pytest.raises(ValueError, match=message):
        service.geojson(layer, query)


def test_availability(service):
    layers = {d["id"]: d for d in service.describe()}
    assert layers["lv_cables"]["available"] and layers["lv_stations"]["available"]
    assert layers["mv_lines"]["reason"] == "no table pylovo.mv_line in this database"
    assert "not downloaded yet" in layers["ctx_etrago_lines"]["reason"]


def test_geojson_with_colour_slots(service, fake_db):
    fake_db.rows = {
        "ST_X(tp.geom) AS x": [{"id": 1, "x": 0.0, "y": 0.0}, {"id": 2, "x": 50.0, "y": 0.0}],
        "AS origin": [{"fid": 1, "kva": 400, "geometry": '{"type":"Point","coordinates":[11,48]}'},
                      {"fid": 2, "kva": 630, "geometry": '{"type":"Point","coordinates":[11.001,48]}'}],
    }
    fc = json.loads(service.geojson("lv_stations", Query(version="v1")))
    slots = [f["properties"]["slot"] for f in fc["features"]]
    assert len(fc["features"]) == 2 and slots[0] != slots[1] and fc["truncated"] is False
    sql, params = fake_db.calls[-2]
    assert "LIMIT 60001" in sql and params["version"] == "v1"


def test_feature_collection():
    rows = [{"fid": "a", "n": Decimal("1.5"), "d": date(2026, 1, 2), "geometry": '{"type":"Point","coordinates":[1,2]}'},
            {"fid": "b", "geometry": None}]
    fc = json.loads(feature_collection(rows, truncated=True))
    assert fc["truncated"] is True and len(fc["features"]) == 1
    assert fc["features"][0]["properties"] == {"fid": "a", "n": 1.5, "d": "2026-01-02"}


def test_attributes_skip_geometry_and_nets(service, fake_db):
    fake_db.rows = {"FROM pylovo.buildings_result WHERE version_id": [{"objectid": "X", "type": "SFH"}]}
    row = service.feature("lv_buildings", "X", Query(version="v1"))
    assert row == {"objectid": "X", "type": "SFH"}
    sql = fake_db.calls[-1][0]
    assert '"objectid"' in sql and '"geom"' not in sql and '"centroid"' not in sql and "*" not in sql
