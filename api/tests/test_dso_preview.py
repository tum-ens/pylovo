"""The DSO CSV preview judges the file like ``pylovo-import transformers-dso-csv`` (no database)."""
from __future__ import annotations

import pytest

from pylovo_api import db


@pytest.fixture()
def preview(client, monkeypatch):
    monkeypatch.setattr(db, "fetch_one", lambda *args, **kwargs: {"n": 0})      # no stored transformer has these ids

    def post(csv: str, **params):
        response = client.post("/api/transformers/dso-csv/preview", params=params,
                               files={"file": ("transformers.csv", csv.encode(), "text/csv")})
        assert response.status_code == 200, response.text
        return response.json()

    return post


def test_blank_ids_and_coordinates_are_reported_as_invalid_rows(preview):
    body = preview("external_id,lon,lat\nA,11.7,48.0\n,11.8,48.1\nC,11.9,\n")
    assert body["rows"] == 3 and body["valid_rows"] == 1
    assert body["invalid_rows"] == [3, 4]
    assert body["sample"][0]["osm_id"] == "dso/csv/A"


def test_numeric_ids_and_blank_sources_are_read_like_the_import_does(preview):
    body = preview("external_id,lon,lat,source\n00123,11.7,48.0,Aying\n1001,11.8,48.1,\n")
    assert body["invalid_rows"] == []
    assert sorted(row["osm_id"] for row in body["sample"]) == ["dso/aying/00123", "dso/csv/1001"]
    assert body["sources"] == ["aying", "csv"]


def test_duplicate_ids_and_unusable_ratings_are_invalid_rows_too(preview):
    body = preview("external_id,lon,lat,transformer_rated_power\nA,11.7,48.0,400\nA,11.8,48.1,630\n"
                   "B,11.9,48.2,400kVA\nC,12.0,48.3,250\n")
    assert body["invalid_rows"] == [2, 3, 4] and body["valid_rows"] == 1
    assert body["capacities"] == [{"kva": 250, "count": 1}]


def test_coordinates_outside_epsg_4326_are_invalid_rows_of_the_import_check(preview):
    body = preview("external_id,lon,lat\nA,11.7,48.0\nB,691000,5330000\nC,11.8,95\n")
    assert body["invalid_rows"] == [3, 4] and body["valid_rows"] == 1
    assert body["bounds"] == [11.7, 48.0, 11.7, 48.0]
