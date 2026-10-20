"""Tests of the viewer's web app with the fake database (FastAPI TestClient, needs httpx)."""
from __future__ import annotations

import gzip
import json

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from pylovo_viewer.app import create_app, parse_bbox
from pylovo_viewer.db import SchemaError


@pytest.fixture()
def client(fake_db, tmp_path) -> TestClient:
    return TestClient(create_app(fake_db.settings, tmp_path, db=fake_db))


def test_page_and_info(client):
    assert "pylovo viewer" in client.get("/").text
    info = client.get("/api/info").json()
    assert info["read_only"] is True and info["database"]["dbname"] == "fake"
    assert "password" not in json.dumps(info) and "secret" not in json.dumps(info)
    assert client.get("/static/js/main.js").headers["cache-control"] == "no-cache"


def test_layer_errors(client):
    assert client.get("/api/layer/nope").status_code == 404
    assert client.get("/api/layer/mv_lines?version=v1&mv=d1").status_code == 409
    assert client.get("/api/layer/lv_cables?version=v1").status_code == 400
    response = client.get("/api/layer/lv_cables?version=v1&bbox=1,2,3")
    assert response.status_code == 400 and "bbox" in response.json()["detail"]


def test_layer_gzip_and_cache(client, fake_db):
    fake_db.rows = {"AS origin": [{"fid": 1, "kva": 400, "geometry": '{"type":"Point","coordinates":[11,48]}'}] * 40}
    response = client.get("/api/layer/mv_stations?version=v1", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200 and response.headers["content-encoding"] == "gzip"
    assert len(response.json()["features"]) == 40          # the client decompresses
    calls = len(fake_db.calls)
    raw = client.get("/api/layer/mv_stations?version=v1", headers={"Accept-Encoding": "identity"})
    assert len(fake_db.calls) == calls                     # served from the cache
    assert "content-encoding" not in raw.headers and json.loads(raw.content)["features"]
    assert gzip.decompress(gzip.compress(raw.content)) == raw.content


def test_catalog_refresh(client):
    catalog = client.get("/api/catalog?refresh=1").json()
    assert {layer["id"] for layer in catalog["layers"] if layer["available"]} >= {"lv_cables", "mv_stations", "postcodes"}
    assert catalog["groups"][1]["title"] == "HV grid"


def test_database_without_mv_tables(client, fake_db):
    # the fake database has LV tables only (no migration 0009, no municipal register): the MV requests answer empty
    catalog = client.get("/api/catalog").json()
    layers = {layer["id"]: layer for layer in catalog["layers"]}
    assert catalog["mv_variants"] is False and layers["lv_cables"]["available"]
    assert layers["mv_areas"]["reason"] == "no table pylovo.mv_grid in this database"
    calls = len(fake_db.calls)
    assert client.get("/api/areas?version=v1&mv=d1").json() == []
    assert client.get("/api/bounds?version=v1&mv=d1&area=3").json() == {"bounds": None}
    assert client.get("/api/summary?version=v1&mv=d1").json()["mv"] is None
    client.get("/api/postcodes?version=v1")
    client.get("/api/search?q=Bahnhof&version=v1&mv=d1")
    sqls = " ".join(sql for sql, _ in fake_db.calls[calls:])
    assert "mv_grid" not in sqls and "mv_line" not in sqls and "municipal_register" not in sqls


def test_missing_column_is_not_a_server_error(client, fake_db):
    fake_db.errors = {"FROM pylovo.lines_result_cache c WHERE": SchemaError('column c.to_bus does not exist')}
    response = client.get("/api/layer/lv_cables?version=v1&bbox=11,48,11.1,48.1")
    assert response.status_code == 409 and response.json()["detail"] == "not in this database: column c.to_bus does not exist"


@pytest.mark.parametrize("text, expected", [
    ("11,48,11.2,48.1", (11.0, 48.0, 11.2, 48.1)), ("11.123456,48,12,49", (11.12346, 48.0, 12.0, 49.0)), (None, None)])
def test_parse_bbox(text, expected):
    assert parse_bbox(text) == expected


@pytest.mark.parametrize("text", ["11,48,10,49", "a,b,c,d", "0,95,1,96"])
def test_parse_bbox_rejects(text):
    with pytest.raises(ValueError):
        parse_bbox(text)
