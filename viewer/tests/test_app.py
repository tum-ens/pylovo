"""Tests of the viewer's web app with the fake database (FastAPI TestClient, needs httpx)."""
from __future__ import annotations

import gzip
import json

import pytest

pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from pylovo_viewer.app import create_app, parse_bbox


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
    response = client.get("/api/layer/lv_stations?version=v1", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200 and response.headers["content-encoding"] == "gzip"
    assert len(response.json()["features"]) == 40          # the client decompresses
    calls = len(fake_db.calls)
    raw = client.get("/api/layer/lv_stations?version=v1", headers={"Accept-Encoding": "identity"})
    assert len(fake_db.calls) == calls                     # served from the cache
    assert "content-encoding" not in raw.headers and json.loads(raw.content)["features"]
    assert gzip.decompress(gzip.compress(raw.content)) == raw.content


def test_catalog_refresh(client):
    layers = client.get("/api/catalog?refresh=1").json()["layers"]
    assert {layer["id"] for layer in layers if layer["available"]} >= {"lv_cables", "lv_stations", "postcodes"}


@pytest.mark.parametrize("text, expected", [
    ("11,48,11.2,48.1", (11.0, 48.0, 11.2, 48.1)), ("11.123456,48,12,49", (11.12346, 48.0, 12.0, 49.0)), (None, None)])
def test_parse_bbox(text, expected):
    assert parse_bbox(text) == expected


@pytest.mark.parametrize("text", ["11,48,10,49", "a,b,c,d", "0,95,1,96"])
def test_parse_bbox_rejects(text):
    with pytest.raises(ValueError):
        parse_bbox(text)
