"""Tests of the layer registry: scopes, filters, availability, GeoJSON and attributes (fake database)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pylovo_viewer import summary
from pylovo_viewer.catalog import Catalog
from pylovo_viewer.layers import GROUPS, LayerService, Query, build_layers, feature_collection

SCOPES = {"version", "view", "run", "file"}


@pytest.fixture()
def service(fake_db, tmp_path: Path) -> LayerService:
    return LayerService(fake_db, Catalog(fake_db), tmp_path)


def test_registry():
    layers = build_layers()
    ids = [layer.id for layer in layers]
    assert len(ids) == len(set(ids))
    for layer in layers:
        assert layer.scope in SCOPES and layer.group in GROUPS and set(layer.also) <= set(GROUPS) - {layer.group}
        assert layer.geometry in {"point", "line", "polygon"}
        assert layer.parent is None or layer.parent in ids
        if layer.scope != "file":
            assert "AS fid" in layer.sql and "AS geometry" in layer.sql
        if layer.scope == "view":
            assert "{env}" in layer.sql


def test_view_filter_and_station_mv_data(service, fake_db):
    by_view = service.sql(service.layers["lv_cables"], Query(version="v1", bbox=(11.0, 48.0, 11.1, 48.1)))
    assert "c.version_id = %(version)s AND c.geom && ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, " \
           "%(maxy)s, 4326), 25832)" in by_view
    stations = service.layers["mv_stations"]
    plain = service.sql(stations, Query(version="v1", mv="d1"))   # the fake database has no MV tables
    assert "mv_node" not in plain and "{" not in plain
    service.catalog.columns["pylovo.mv_node"] = {"grid_result_id": "int8", "ring_id": "int4"}
    with_mv = service.sql(stations, Query(version="v1", mv="d1"))
    assert "LEFT JOIN pylovo.mv_node n" in with_mv and "n.ring_id AS ring" in with_mv
    assert "mv_node" not in service.sql(stations, Query(version="v1"))   # no run: no MV data


@pytest.mark.parametrize("layer, query, message", [
    ("mv_stations", Query(), "needs version"),
    ("lv_cables", Query(version="v1"), "needs bbox"),
    ("mv_lines", Query(version="v1"), "needs mv"),
])
def test_missing_parameters(service, layer, query, message):
    with pytest.raises(ValueError, match=message):
        service.geojson(layer, query)


def test_availability(service):
    layers = {d["id"]: d for d in service.describe()}
    assert layers["lv_cables"]["available"] and layers["mv_stations"]["available"]
    assert layers["mv_lines"]["reason"] == "no table pylovo.mv_line in this database"
    assert "not downloaded yet" in layers["hv_lines"]["reason"]
    assert layers["lv_cabinets"]["parent"] == "lv_cables" and layers["mv_stations"]["with_run"]
    assert [g["id"] for g in service.groups()] == ["regions", "hv", "mv", "lv"]
    assert layers["hv_substations"]["groups"] == ["hv", "mv"] and layers["mv_stations"]["groups"] == ["mv", "lv"]


def test_cable_into_cabinet(service, fake_db):
    # cables run away from the station: the one that ends at a cabinet's bus supplies it
    fake_db.rows = {"FROM pylovo.split_points": [{"g": 7, "b": 5}]}
    rows = [{"fid": 1, "grid": 7, "to_bus": 5}, {"fid": 2, "grid": 7, "to_bus": 6}, {"fid": 3, "grid": 8, "to_bus": 5}]
    service._cabinet_cables(rows, Query(version="v1"))
    assert rows == [{"fid": 1, "grid": 7, "to_cabinet": True}, {"fid": 2, "grid": 7}, {"fid": 3, "grid": 8}]
    assert fake_db.calls[-1][1] == ([7, 8],)


def test_feeder_section(service, fake_db):
    # cable 10 (station bus 1 -> 2) and cable 11 (2 -> cabinet bus 3) form section 3 of grid 7
    line = '{"type":"LineString","coordinates":[[11,48],[11.001,48]]}'
    fake_db.rows = {
        "FROM pylovo.lines_result_cache me": [
            {"grid": 7, "section": 3, "std_type": "NAYY_4_150", "length_km": 0.05, "from_bus": 1, "to_bus": 2, "geometry": line},
            {"grid": 7, "section": 3, "std_type": "NAYY_4_95", "length_km": 0.02, "from_bus": 2, "to_bus": 3, "geometry": line}],
        "SELECT ont_vertice_id FROM": [{"ont_vertice_id": 1}],
        "FROM pylovo.split_points": [{"bus": 3, "id": 42}],
        "feeder_section_id IS NULL AND from_bus": [{"n": 4}],
        "SELECT grid_result_id, ont_vertice_id": [{"grid_result_id": 7, "ont_vertice_id": 1}],
        "AS g, id, from_bus": [{"g": 7, "id": 10, "from_bus": 1, "to_bus": 2, "length_km": 0.05},
                               {"g": 7, "id": 11, "from_bus": 2, "to_bus": 3, "length_km": 0.02}],
    }
    out = service.section(10)
    assert out["from"] == {"kind": "station", "id": 7} and out["to"] == {"kind": "cabinet", "id": 42}
    assert out["section_m"] == 70.0 and out["types"] == [["NAYY_4_150", 50.0], ["NAYY_4_95", 20.0]]
    assert out["connections"] == 4 and out["feeder"] == 1 and out["section_cables"] == 2
    assert out["geometry"]["type"] == "MultiLineString" and len(out["geometry"]["coordinates"]) == 2
    connections = next(params for sql, params in fake_db.calls if "feeder_section_id IS NULL AND from_bus" in sql)
    assert connections == (7, [2, 3])   # not at the start bus: those belong upstream
    fake_db.rows = {}
    with pytest.raises(LookupError):
        service.section(99)


def test_mv_lines_draw_their_display_geometry_where_the_database_has_one(service, fake_db):
    lines, opens = service.layers["mv_lines"], service.layers["mv_open_points"]
    q = Query(version="v1", mv="d1")
    assert "ST_Simplify(l.geom, 0.3)" in service.sql(lines, q) and "{" not in service.sql(opens, q)
    assert service.unavailable(service.layers["hv_busbars"]) == "no table pylovo.mv_substation in this database"
    service.catalog.columns["pylovo.mv_line"] = {"display_geom": "geometry", "geom": "geometry"}
    assert "ST_Simplify(COALESCE(l.display_geom, l.geom), 0.3)" in service.sql(lines, q)
    assert "ST_LineInterpolatePoint(COALESCE(l.display_geom, l.geom)" in service.sql(opens, q)


def test_overhead_parts_of_mixed_lines(service, fake_db):
    rows = [{"fid": 1, "kind": "mixed"}, {"fid": 2, "kind": "cable"}, {"fid": 3, "kind": "mixed"}]
    service._overhead_parts(rows, Query(version="v1", mv="d1"))   # no routing graph in the database: left out
    assert all("overhead" not in row for row in rows) and not any("osm_mv_line" in sql for sql, _ in fake_db.calls)
    service.catalog.columns["pylovo.mv_graph_edge"] = {"graph_id": "int8", "klasse": "text", "geom": "geometry"}
    service.catalog.columns["pylovo.mv_variant"] = {"graph_id": "int8"}
    fake_db.rows = {"klasse = 'osm_mv_line'": [
        {"id": 1, "geometry": '{"type":"MultiLineString","coordinates":[[[11,48],[11.001,48]]]}'}, {"id": 3, "geometry": None}]}
    service._overhead_parts(rows, Query(version="v1", mv="d1"))
    assert rows[0]["overhead"] == [[[11, 48], [11.001, 48]]] and "overhead" not in rows[1] and "overhead" not in rows[2]
    assert fake_db.calls[-1][1]["mv"] == "d1"
    assert "NULL::geometry IS NULL" in fake_db.calls[-1][0]                # no display geometry: the stored route
    service.catalog.columns["pylovo.mv_line"] = {"display_geom": "geometry"}
    service._overhead_parts([{"fid": 1, "kind": "mixed"}], Query(version="v1", mv="d1"))
    assert "ST_LineLocatePoint(l.display_geom, ST_StartPoint(d.geom))" in fake_db.calls[-1][0]


def test_mv_layers_and_figures_read_no_generation_results(service, fake_db):
    # older variants still hold generator lines and feed-in loadings: their lines are drawn, those results are not read
    service.catalog.columns["pylovo.mv_grid"] = service.catalog.columns["pylovo.mv_line"] = {"mv_grid_id": "int8"}
    fake_db.calls.clear()
    summary.mv(fake_db, service.catalog, Query(version="v1", mv="d1"))
    sqls = [sql for sql, _ in fake_db.calls] + [layer.sql for layer in build_layers()]
    assert len(fake_db.calls) == 2 and not any("feed_in" in sql or "generator" in sql for sql in sqls)


def test_geojson_with_colour_slots(service, fake_db):
    fake_db.rows = {
        "ST_X(tp.geom) AS x": [{"id": 1, "x": 0.0, "y": 0.0}, {"id": 2, "x": 50.0, "y": 0.0}],
        "count(*) AS buildings": [{"fid": 1, "kva": 400, "geometry": '{"type":"Point","coordinates":[11,48]}'},
                                  {"fid": 2, "kva": 630, "geometry": '{"type":"Point","coordinates":[11.001,48]}'}],
    }
    fc = json.loads(service.geojson("lv_areas", Query(version="v1")))
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
