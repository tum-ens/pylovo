"""eTraGo context layer: WKB decoding, GeoJSON conversion, DE filter and the fetch/cache cycle (no network)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import requests
import shapely
from pylovo_viewer import etrago

SCN = "eGon2035"
# geom of eTraGo line 174 as the OEP returns it: a one-part MultiLineString with repeated points
OEP_LINE_GEOM = ("01050000000100000001020000000400000095C78965E9811A4044BEF0EFD8A0494095C78965E9811A4044BEF0EFD8A04940"
                 "7E7CF8E8C3811A4024F664598BA049407E7CF8E8C3811A4024F664598BA04940")
LS, MLS = shapely.LineString, shapely.MultiLineString


def _hex(geom: Any) -> str | None:
    return geom if geom is None or isinstance(geom, str) else geom.wkb_hex


def _bus(bus_id: int, country: str | None, v_nom: float, x: float | None = None, y: float | None = None,
         geom: str | None = None, scn: str = SCN, carrier: str = "AC") -> dict[str, Any]:
    return {"scn_name": scn, "carrier": carrier, "bus_id": bus_id, "v_nom": float(v_nom), "type": None,
            "x": x, "y": y, "geom": geom, "country": country}


def _line(line_id: int, bus0: int, bus1: int, v_nom: float, geom: Any = None, topo: Any = None,
          scn: str = SCN) -> dict[str, Any]:
    return {"scn_name": scn, "carrier": "AC", "line_id": line_id, "bus0": bus0, "bus1": bus1, "v_nom": float(v_nom),
            "s_nom": "1790.0", "length": 8.21234, "num_parallel": 2.0, "cables": 6, "type": None,
            "geom": _hex(geom), "topo": _hex(topo)}


BUSES = [_bus(1, "DE", 380, 11.0, 48.0), _bus(2, "DE", 380, 11.1, 48.0),
         _bus(3, "DE", 110, geom=shapely.Point(11.2, 48.1).wkb_hex),            # no x/y: position from geom
         _bus(4, "AT", 220, 13.0, 47.5), _bus(5, None, 110, 9.0, 50.0),
         _bus(6, "DE", 110, 9.5, 50.5, scn="eGon100RE"), _bus(7, "DE", 110, 9.6, 50.5, carrier="DC")]
LINES = [_line(10, 1, 2, 380, MLS([[(11.0, 48.0), (11.05, 48.00001), (11.1, 48.0), (11.1, 48.0)]])),
         _line(11, 2, 3, 110, topo=LS([(11.1, 48.0), (11.2, 48.1)])),           # no OSM route: straight topo
         _line(12, 3, 4, 220, LS([(11.2, 48.1), (13.0, 47.5)])),                 # to Austria
         _line(13, 1, 3, 110, MLS([[(11.0, 48.0), (11.05, 48.05)], [(11.15, 48.08), (11.2, 48.1)]])),   # a gap
         _line(14, 1, 2, 380),                                                    # no geometry at all
         _line(15, 2, 5, 110, LS([(11.1, 48.0), (9.0, 50.0)])),                   # bus without country
         _line(16, 1, 2, 380, LS([(11.0, 48.0), (11.1, 48.0)]), scn="eGon100RE"),
         _line(17, 1, 2, 380, MLS([[(11.0, 48.0), (11.05, 48.03)], [(11.05, 48.03), (11.1, 48.0)]])),  # joins up
         _line(18, 2, 3, 220, LS([(11.1000049, 48.0000051), (11.123456789, 48.0987654321)]))]


class FakeOEP:
    """Answers the OEP rows API from :data:`BUSES` and :data:`LINES` (``where`` equality, columns, order, paging).

    ``failures`` are served first, one per request: an exception is raised, a number becomes that HTTP status.
    """

    def __init__(self, failures: list[Any] | None = None, ignore_offset: bool = False) -> None:
        self.tables = {etrago.TABLES["buses"]: BUSES, etrago.TABLES["lines"]: LINES}
        self.failures = list(failures or [])
        self.ignore_offset = ignore_offset
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: float | None = None) -> requests.Response:
        params = params or {}
        self.calls.append({"url": url, **params})
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return _response(failure, {"reason": "fake failure"})
        table = url.rstrip("/").split("/")[-2]
        if table not in self.tables:
            return _response(404, {"reason": "Table does not exist"})
        where = dict(w.split("=", 1) for w in params.get("where", []))
        rows = [r for r in self.tables[table] if all(str(r.get(k)) == v for k, v in where.items())]
        if "orderby" in params:
            rows = sorted(rows, key=lambda r: r[params["orderby"]])
        offset = 0 if self.ignore_offset else int(params.get("offset", 0))
        rows = rows[offset:offset + int(params["limit"]) if "limit" in params else None]
        columns = params.get("column")
        if columns is not None:
            rows = [{c: r[c] for c in ([columns] if isinstance(columns, str) else columns)} for r in rows]
        return _response(200, rows)


def _response(status: int, payload: Any) -> requests.Response:
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    return response


def _quiet(_: str) -> None:
    pass


@pytest.fixture(autouse=True)
def _small_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(etrago, "PAGE_SIZE", 2)
    monkeypatch.setattr(etrago, "BACKOFF_S", 0.0)


def _same_line(coords: list[list[float]], expected: list[list[float]]) -> bool:
    """Equal up to direction (merging may reverse a line)."""
    return coords in (expected, expected[::-1])


def test_decode_wkb():
    assert [etrago.decode_wkb(v) for v in (None, "", "zz", shapely.LineString().wkb_hex)] == [None] * 4
    ewkb = shapely.to_wkb(shapely.set_srid(shapely.Point(11.0, 48.0), 4326), hex=True, include_srid=True)
    assert etrago.decode_wkb(ewkb).equals(shapely.Point(11.0, 48.0))
    assert etrago.decode_wkb(OEP_LINE_GEOM).geom_type == "MultiLineString"


def test_number():
    assert etrago.number("260") == 260 and isinstance(etrago.number("260"), int)
    assert etrago.number(110.0) == 110 and isinstance(etrago.number(110.0), int)
    assert etrago.number("0.70146241") == pytest.approx(0.70146241) and etrago.number(8.21234, 3) == 8.212
    assert [etrago.number(v) for v in (None, "", "abc", "Infinity", float("nan"), float("-inf"))] == [None] * 6


def test_german_buses_and_the_lines_between_them():
    buses = etrago.german_buses(b for b in BUSES if b["scn_name"] == SCN and b["carrier"] == "AC")
    assert sorted(buses) == [1, 2, 3]
    lines = etrago.german_lines((r for r in LINES if r["scn_name"] == SCN), buses)
    assert [r["line_id"] for r in lines] == [10, 11, 13, 14, 17, 18]


def test_bus_feature():
    assert etrago.bus_feature(_bus(1, "DE", 380, 11.123456789, 48.0)) == {
        "type": "Feature", "id": 1, "properties": {"fid": 1, "v_nom": 380},
        "geometry": {"type": "Point", "coordinates": [11.12346, 48.0]}}
    assert etrago.bus_feature(BUSES[2])["geometry"]["coordinates"] == [11.2, 48.1]          # from geom
    assert etrago.bus_feature({**BUSES[0], "type": "PQ"})["properties"]["type"] == "PQ"
    assert etrago.bus_feature(_bus(9, "DE", 110)) is None                                    # no position


def test_line_feature_from_an_oep_row():
    feature = etrago.line_feature(_line(174, 1739, 1733, 220, OEP_LINE_GEOM))
    assert feature["id"] == 174 and feature["geometry"]["type"] == "LineString"
    assert _same_line(feature["geometry"]["coordinates"], [[6.62687, 51.25662], [6.62672, 51.25425]])
    assert feature["properties"] == {"fid": 174, "v_nom": 220, "s_nom": 1790, "length_km": 8.212, "num_parallel": 2,
                                     "cables": 6, "type": None, "bus0": 1739, "bus1": 1733}


def test_line_geometry_merges_simplifies_and_rounds():
    rows = {r["line_id"]: r for r in LINES}
    one = etrago.line_geometry(rows[10])                     # one-part MultiLineString, 1 m kink, repeated end
    assert one["type"] == "LineString" and _same_line(one["coordinates"], [[11.0, 48.0], [11.1, 48.0]])
    assert etrago.line_geometry(rows[11]) == {"type": "LineString", "coordinates": [[11.1, 48.0], [11.2, 48.1]]}
    gap = etrago.line_geometry(rows[13])
    assert gap["type"] == "MultiLineString" and len(gap["coordinates"]) == 2
    joined = etrago.line_geometry(rows[17])
    assert joined["type"] == "LineString"
    assert _same_line(joined["coordinates"], [[11.0, 48.0], [11.05, 48.03], [11.1, 48.0]])
    assert etrago.line_geometry(rows[18])["coordinates"] == [[11.1, 48.00001], [11.12346, 48.09877]]
    assert etrago.line_geometry(rows[14]) is None
    detour = {"geom": LS([(11.0, 48.0), (11.05, 48.0002), (11.1, 48.0)]).wkb_hex}      # about 20 m: kept
    assert len(etrago.line_geometry(detour)["coordinates"]) == 3
    tiny = {"geom": LS([(11.0, 48.0), (11.000001, 48.000001)]).wkb_hex}                # below the rounding
    assert etrago.line_geometry(tiny) == {"type": "LineString", "coordinates": [[11.0, 48.0], [11.0, 48.0]]}


def test_fetch_writes_geojson_and_meta(tmp_path: Path):
    oep, logs = FakeOEP(), []
    assert etrago.status(tmp_path)["available"] is False and etrago.read(tmp_path, "lines") is None
    meta = etrago.fetch(tmp_path, log=logs.append, session=oep)
    out = etrago.dataset_dir(tmp_path)
    assert out == tmp_path / "etrago" / SCN
    assert sorted(p.name for p in out.iterdir()) == ["buses.geojson", "lines.geojson", "meta.json"]  # no temp files
    assert meta["counts"] == {"lines": 5, "buses": 3, "lines_by_kv": {"110": 2, "220": 1, "380": 2},
                              "buses_by_kv": {"110": 1, "380": 2}}
    assert meta["dropped"] == {"buses_outside_de": 2, "buses_without_position": 0, "lines_outside_de": 2,
                               "lines_without_geometry": 1}
    assert (meta["dataset"], meta["scenario"], meta["attribution"]) == ("etrago", SCN, etrago.ATTRIBUTION)
    assert meta["api"] == etrago.API and meta["simplify_tolerance_deg"] == etrago.SIMPLIFY_TOLERANCE_DEG
    assert {"buses: 2/5", "buses: 4/5", "buses: 5/5", "lines: 8/8"} <= set(logs)
    lines, buses = etrago.read(tmp_path, "lines"), etrago.read(tmp_path, "buses")
    assert meta["bytes"] == {"lines": len(lines), "buses": len(buses)}
    assert [f["id"] for f in json.loads(lines)["features"]] == [10, 11, 13, 17, 18]
    assert [f["properties"]["fid"] for f in json.loads(buses)["features"]] == [1, 2, 3]
    assert json.loads((out / "meta.json").read_text(encoding="utf-8")) == meta
    state = etrago.status(tmp_path)
    assert state["available"] is True and state["path"] == str(out) and state["counts"] == meta["counts"]
    # the keys first (one column), then ordered pages of the needed columns only
    assert all(c["where"] == [f"scn_name={SCN}", "carrier=AC"] for c in oep.calls)
    assert [c["column"] for c in oep.calls if "limit" not in c] == ["bus_id", "line_id"]
    pages = [c for c in oep.calls if "limit" in c]
    assert all(c["limit"] == 2 and c["column"] == etrago.COLUMNS[{"bus_id": "buses", "line_id": "lines"}[c["orderby"]]]
               for c in pages)
    assert [c["offset"] for c in pages if c["orderby"] == "line_id"] == [0, 2, 4, 6]


def test_fetch_retries_transient_errors(tmp_path: Path):
    oep = FakeOEP(failures=[requests.ConnectionError("connection reset"), 503])
    assert etrago.fetch(tmp_path, log=_quiet, session=oep)["counts"]["buses"] == 3
    assert len(oep.calls) == 2 + 1 + 3 + 1 + 4      # two failures, then keys and pages of buses and lines


def test_fetch_gives_up_naming_the_url(tmp_path: Path):
    oep = FakeOEP(failures=[502] * etrago.RETRIES)
    with pytest.raises(RuntimeError, match=r"failed 4 times \(HTTP 502\): https://openenergyplatform\.org/api/v0/"
                                           r"schema/data/tables/edut_00_056/rows/\?where=scn_name%3DeGon2035"):
        etrago.fetch(tmp_path, log=_quiet, session=oep)
    assert len(oep.calls) == etrago.RETRIES and etrago.status(tmp_path)["available"] is False


def test_unexpected_answers_fail_at_once(tmp_path: Path):
    for code in (200, 400, 404):       # 200 with an object instead of rows, client errors
        oep = FakeOEP(failures=[code])
        with pytest.raises(RuntimeError, match=rf"unexpected OEP answer \(HTTP {code}\) to https://.*fake failure"):
            etrago.fetch(tmp_path, log=_quiet, session=oep)
        assert len(oep.calls) == 1


def test_unknown_scenario_and_broken_paging(tmp_path: Path):
    with pytest.raises(RuntimeError, match="no AC buses of scenario 'eGon2099'"):
        etrago.fetch(tmp_path, "eGon2099", log=_quiet, session=FakeOEP())
    with pytest.raises(RuntimeError, match="incomplete download of buses"):
        etrago.fetch(tmp_path, log=_quiet, session=FakeOEP(ignore_offset=True))
    assert not (tmp_path / "etrago").exists()


def test_read_and_status_reject_bad_input(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown eTraGo part"):
        etrago.read(tmp_path, "transformers")
    for scenario in ("../etc", "a/b", "", ".hidden", None):
        with pytest.raises(ValueError, match="invalid eTraGo scenario"):
            etrago.read(tmp_path, "lines", scenario)
    with pytest.raises(ValueError, match="invalid eTraGo scenario"):
        etrago.status(tmp_path, "..")
    assert etrago.status(tmp_path, "eGon100RE") == {"available": False, "scenario": "eGon100RE",
                                                     "path": str(tmp_path / "etrago" / "eGon100RE")}
    out = etrago.dataset_dir(tmp_path)
    out.mkdir(parents=True)
    (out / "meta.json").write_text("{", encoding="utf-8")
    assert etrago.status(tmp_path)["available"] is False
