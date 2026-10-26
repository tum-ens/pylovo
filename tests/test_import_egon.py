"""Import of the eGon-data HV/MV substations from the Open Energy Platform (no network, no database)."""
from __future__ import annotations

import json

import pytest
import requests

from pylovo.data_import import import_egon


def record(bus_id, point="0101000000", polygon="0103000000", **values):
    base = {"id": 1, "bus_id": bus_id, "lon": 7.0, "lat": 51.0, "point": point, "polygon": polygon,
            "voltage": "110000", "power_type": "substation", "substation": "transmission", "osm_id": "w1",
            "osm_www": "http://www.osm.org/way/1", "frequency": "NA", "subst_name": "UW", "ref": "NA",
            "operator": "NA", "dbahn": "no", "status": 1}
    return {**base, **values}


def test_rows_map_na_to_null_and_skip_unusable_records():
    rows = import_egon.egon_rows([record(30901), record(30902, voltage="NA", osm_id=" NA ", status="NA"),
                                  record(30901, subst_name="duplicate"), record(None), record(30903, point="NA")])
    assert [r[0] for r in rows] == [30901, 30902]                       # first of a duplicate bus id, no point: out
    first, second = rows
    assert first[1] == 1 and first[2:11] == ("110000", "substation", "transmission", "w1", "UW", None, None, None,
                                             "no")
    assert second[1] is None and second[2] is None and second[5] is None
    assert first[-2:] == ("0101000000", "0103000000")                  # WKB hex of the point and the footprint


def test_load_replaces_the_table_and_keeps_only_polygonal_footprints(monkeypatch):
    statements, inserted = [], []

    class Cursor:
        def execute(self, query, params=None):
            statements.append(query)

    monkeypatch.setattr(import_egon, "execute_values",
                        lambda cur, query, rows, **kw: inserted.append((query, list(rows), kw["template"])))
    assert import_egon.load_egon_substations(Cursor(), [record(1), record(2)]) == 2
    assert statements == ["DELETE FROM pylovo.egon_hvmv_substation"]
    query, rows, template = inserted[0]
    assert "INSERT INTO pylovo.egon_hvmv_substation" in query and len(rows) == 2
    assert f"ST_Transform(ST_GeomFromWKB(decode(%s, 'hex'), 4326), {import_egon.TARGET_EPSG})" in template
    assert "CASE WHEN ST_Dimension(f.g) = 2 THEN ST_Multi" in template   # node substations carry a point
    assert template.count("%s") == len(rows[0])


class Response:
    def __init__(self, rows, status=200):
        self.rows, self.status_code = rows, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.rows


def test_download_pages_through_the_api_and_writes_the_file_at_the_end(monkeypatch, tmp_path):
    monkeypatch.setattr(import_egon, "PAGE_ROWS", 2)
    monkeypatch.setattr(import_egon.time, "sleep", lambda s: None)
    pages = {0: [record(1), record(2)], 2: [record(3), record(4)], 4: [record(5)]}
    calls = []

    def get(url, params, headers, timeout):
        calls.append(params["offset"])
        if len(calls) == 2:                                             # one transient error is retried
            return Response([], status=503)
        return Response(pages[params["offset"]])

    monkeypatch.setattr(import_egon.requests, "get", get)
    path = import_egon.download_egon_substations(tmp_path / "egon" / "subst.json")
    assert calls == [0, 2, 2, 4] and [r["bus_id"] for r in json.loads(path.read_text())] == [1, 2, 3, 4, 5]
    assert not path.with_suffix(".part").exists()


def test_a_failed_download_leaves_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(import_egon.time, "sleep", lambda s: None)
    monkeypatch.setattr(import_egon.requests, "get", lambda *a, **k: Response([], status=500))
    target = tmp_path / "subst.json"
    with pytest.raises(requests.HTTPError):
        import_egon.download_egon_substations(target)
    assert not target.exists()


def test_import_uses_the_cached_file_unless_refreshed(monkeypatch, tmp_path):
    cache = tmp_path / "egon_data" / "egon_hvmv_substation.json"
    monkeypatch.setattr(import_egon, "get_user_data_dir", lambda: tmp_path)
    downloads = []

    def download(path):
        downloads.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([record(7)]))
        return path

    monkeypatch.setattr(import_egon, "download_egon_substations", download)
    monkeypatch.setattr(import_egon, "load_egon_substations", lambda cur, records: len(records))
    assert import_egon.import_egon_substations(None) == 1 and downloads == [cache]
    assert import_egon.import_egon_substations(None) == 1 and len(downloads) == 1        # cached
    assert import_egon.import_egon_substations(None, refresh=True) == 1 and len(downloads) == 2
