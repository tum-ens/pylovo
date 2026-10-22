"""Tests of the database switch: which databases are offered, and one site per database (fake server)."""
from __future__ import annotations

import dataclasses

import pytest
from pylovo_viewer.databases import Sites

pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from pylovo_viewer.app import create_app

MV = {"pylovo.mv_variant": ["version_id", "mv_variant_id", "mv_parameters", "graph_id"],
      "pylovo.mv_grid": ["mv_grid_id", "version_id", "mv_variant_id", "substation_id", "geom"],
      "pylovo.mv_substation": ["substation_id", "version_id", "mv_variant_id", "geom"],
      "pylovo.mv_line": ["mv_line_id", "version_id", "mv_variant_id", "geom"],
      "pylovo.mv_node": ["mv_node_id", "grid_result_id", "ring_id"]}


@pytest.fixture()
def server(fake_server):
    # the LV database has no MV tables (an older schema); postgres has no pylovo schema at all
    lv = fake_server.LV
    return fake_server({"infdb_mv": {**lv, **MV}, "infdb": lv, "infdb_test": {**lv, **MV}, "postgres": {}},
                       default="infdb_mv")


def sites(server, tmp_path, **settings) -> Sites:
    return Sites(dataclasses.replace(server.default.settings, **settings), tmp_path, db=server.default,
                 connect=server.connect)


def test_discovery_offers_the_databases_with_pylovo(server, tmp_path):
    offered = sites(server, tmp_path)
    assert offered.names() == ["infdb_mv", "infdb", "infdb_test"]        # the .env database first
    assert {db.settings.dbname for db in server.opened} == {"infdb", "infdb_test", "postgres"}
    assert all(db.closed for db in server.opened)                           # the probes are closed again


def test_listed_databases_that_the_server_has(server, tmp_path):
    offered = sites(server, tmp_path, databases=("infdb_test", "gone", "infdb_mv", "postgres"))
    assert offered.names() == ["infdb_mv", "infdb_test", "postgres"] and not server.opened   # nothing probed


def test_the_list_follows_the_server(server, tmp_path):
    offered = sites(server, tmp_path)
    site = offered.get("infdb")
    server.default.server.remove("infdb")
    assert "infdb" in offered.names()                                      # read less than a minute ago
    assert "infdb" not in offered.names(refresh=True) and site.db.closed
    with pytest.raises(LookupError, match="no database infdb"):
        offered.get("infdb")
    server.default.server.append("infdb_new")
    server.tables["infdb_new"] = server.LV
    assert offered.get("infdb_new").db.settings.dbname == "infdb_new"     # an unknown name reads the list again
    assert offered.get(None) is offered.get("infdb_mv")


def test_requests_name_their_database(server, tmp_path):
    server.default.rows = {"AS origin": [{"fid": 1, "geometry": '{"type":"Point","coordinates":[11,48]}'}]}
    server.rows["infdb"] = {"AS origin": [{"fid": 2, "geometry": '{"type":"Point","coordinates":[12,49]}'}]}
    client = TestClient(create_app(server.default.settings, tmp_path, db=server.default, connect=server.connect),
                        base_url="http://127.0.0.1:8770")
    assert client.get("/api/databases").json() == {"default": "infdb_mv", "databases": ["infdb_mv", "infdb", "infdb_test"]}
    fid = lambda url: [f["properties"]["fid"] for f in client.get(url).json()["features"]]   # noqa: E731
    assert fid("/api/layer/mv_stations?version=v1") == [1]
    assert fid("/api/layer/mv_stations?version=v1&db=infdb") == [2]          # its own pool and cache
    assert fid("/api/layer/mv_stations?version=v1&db=infdb_mv") == [1]
    mv, lv = client.get("/api/catalog").json(), client.get("/api/catalog?db=infdb").json()
    assert mv["mv_variants"] and not lv["mv_variants"] and lv["database"] == "infdb"
    assert {layer["id"]: layer for layer in lv["layers"]}["mv_lines"]["reason"] == "no table pylovo.mv_line in this database"
    assert client.get("/api/versions?db=postgres").status_code == 404     # not offered: never opened
    assert "postgres" not in {db.settings.dbname for db in server.opened if not db.closed}
