"""Tests of the links from the HV/MV substations to the drawn eTraGo grid (pure, no database)."""
from __future__ import annotations

import json
import math

import pytest
from pylovo_viewer import hv_links


def _fc(features: list[dict]) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": features}).encode()


def bus(fid: int, lon: float, lat: float) -> dict:
    return {"type": "Feature", "properties": {"fid": fid, "v_nom": 110}, "geometry": {"type": "Point", "coordinates": [lon, lat]}}


def line(fid: int, v_nom: int, coords: list) -> dict:
    kind = "MultiLineString" if isinstance(coords[0][0], list) else "LineString"
    return {"type": "Feature", "properties": {"fid": fid, "v_nom": v_nom}, "geometry": {"type": kind, "coordinates": coords}}


# a 110 kV line along 48.5° N east of 11.0° E, a 380 kV line closer to the test point, and a multi-part 110 kV line
GRID = hv_links.EtragoGrid.from_geojson(
    _fc([bus(7, 11.0, 48.5), bus(8, 11.2, 48.6)]),
    _fc([line(1, 110, [[11.0, 48.5], [11.2, 48.5]]), line(2, 380, [[10.9, 48.45], [10.95, 48.45]]),
         line(3, 110, [[[12.0, 49.0], [12.1, 49.0]], [[12.2, 49.0], [12.3, 49.0]]])]))


def test_a_substation_with_a_bus_is_linked_to_it():
    out = hv_links.link(GRID, 11.001, 48.501, 7)
    assert out["link"] == "bus" and out["target"] == 7
    assert json.loads(out["geometry"]) == {"type": "LineString", "coordinates": [[11.001, 48.501], [11.0, 48.5]]}
    assert out["length_m"] == pytest.approx(134, abs=2)        # 0.001° = 74 m east-west, 111 m north-south


def test_a_substation_without_a_bus_gets_an_artificial_link_to_the_nearest_110_kv_line():
    out = hv_links.link(GRID, 11.1, 48.48, None)               # 2.2 km south of line 1; the 380 kV line is ignored
    assert out["link"] == "nearest_line" and out["target"] == 1
    end = json.loads(out["geometry"])["coordinates"][1]
    assert end == pytest.approx([11.1, 48.5], abs=1e-6)        # the foot of the perpendicular
    assert out["length_m"] == pytest.approx(0.02 * math.pi / 180 * 6_371_000, rel=0.01)
    unknown = hv_links.link(GRID, 12.25, 49.01, 99)            # a bus that is not in the grid: the nearest line too
    assert unknown["link"] == "nearest_line" and unknown["target"] == 3


def test_no_110_kv_lines_and_no_bus_give_no_link():
    empty = hv_links.EtragoGrid.from_geojson(_fc([]), _fc([line(2, 380, [[10.9, 48.45], [10.95, 48.45]])]))
    assert empty.tree is None and hv_links.link(empty, 11.0, 48.5, None) is None
