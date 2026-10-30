"""Straight links from the HV/MV substations of an MV run to the eTraGo grid as the map draws it (layer ``hv_links``).

A substation with an eTraGo bus (``mv_substation.egon_bus_id``, its 110 kV bus in eGon-data) is linked to that bus,
where its eTraGo lines end. A substation without one (flag ``no_egon_bus``) gets an artificial link to the nearest
point of an eTraGo 110 kV line: eTraGo has no line to it, so the link only shows where it would join the drawn grid.

Positions in EPSG:4326 (the downloaded eTraGo GeoJSON, :mod:`pylovo_viewer.etrago`). The nearest line is found in
Web Mercator, which keeps local distances in proportion, and the length is scaled back by the cosine of the
latitude: exact to a fraction of a percent for links of a few kilometres.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import shapely
from shapely.strtree import STRtree

EARTH_RADIUS_M = 6_378_137.0
#: Voltage (kV) of the eTraGo lines that a substation without a bus is linked to.
HV_KV = 110.0
DECIMALS = 6


def _to_mercator(xy: np.ndarray) -> np.ndarray:
    """Web Mercator (m) of lon, lat rows."""
    return np.column_stack([EARTH_RADIUS_M * np.radians(xy[:, 0]),
                            EARTH_RADIUS_M * np.log(np.tan(np.pi / 4 + np.radians(xy[:, 1]) / 2))])


def _mercator(lon: float, lat: float) -> tuple[float, float]:
    x, y = _to_mercator(np.array([[lon, lat]], dtype=float))[0]
    return float(x), float(y)


def _lonlat(x: float, y: float) -> list[float]:
    return [round(math.degrees(x / EARTH_RADIUS_M), DECIMALS),
            round(math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS_M)) - math.pi / 2), DECIMALS)]


@dataclass(frozen=True)
class EtragoGrid:
    """The eTraGo buses (``bus_id`` -> lon, lat) and the 110 kV lines in Web Mercator, with a search tree."""

    buses: dict[int, tuple[float, float]]
    line_ids: np.ndarray
    lines: np.ndarray
    tree: STRtree | None

    @classmethod
    def from_geojson(cls, buses: bytes | str, lines: bytes | str) -> EtragoGrid:
        """From the ``buses.geojson`` and ``lines.geojson`` of :func:`pylovo_viewer.etrago.read`."""
        points = {}
        for feature in json.loads(buses)["features"]:
            fid, coords = feature["properties"].get("fid"), (feature.get("geometry") or {}).get("coordinates")
            if fid is not None and coords:
                points[int(fid)] = (float(coords[0]), float(coords[1]))
        ids, geoms = [], []
        for feature in json.loads(lines)["features"]:
            props, geometry = feature["properties"], feature.get("geometry")
            if geometry is None or props.get("v_nom") is None or float(props["v_nom"]) != HV_KV:
                continue
            ids.append(int(props["fid"]))
            geoms.append(shapely.from_geojson(json.dumps(geometry)))
        mercator = shapely.transform(np.array(geoms, dtype=object), _to_mercator)
        return cls(points, np.array(ids, dtype=np.int64), mercator, STRtree(mercator) if len(geoms) else None)


def _length_m(a: list[float], b: list[float]) -> float:
    """Length (m) of the straight link from ``a`` to ``b`` (lon, lat): Mercator distance times cos(mean latitude)."""
    (x0, y0), (x1, y1) = _mercator(*a), _mercator(*b)
    return math.hypot(x1 - x0, y1 - y0) * math.cos(math.radians((a[1] + b[1]) / 2))


def link(grid: EtragoGrid, lon: float, lat: float, bus: Any) -> dict[str, Any] | None:
    """The link of one substation at ``lon``, ``lat`` with eTraGo bus ``bus`` (None without one).

    Returns:
        ``geometry`` (GeoJSON text, a LineString from the substation), ``link`` (``bus``: to its eTraGo bus;
        ``nearest_line``: artificial, to the nearest eTraGo 110 kV line, also when its bus is not in the grid),
        ``target`` (the bus or line id) and ``length_m``; None without a bus and without 110 kV lines.
    """
    start = [round(float(lon), DECIMALS), round(float(lat), DECIMALS)]
    if bus is not None and int(bus) in grid.buses:
        end, kind, target = [round(v, DECIMALS) for v in grid.buses[int(bus)]], "bus", int(bus)
    elif grid.tree is not None:
        point = shapely.Point(_mercator(*start))
        nearest = int(np.atleast_1d(grid.tree.query_nearest(point))[0])
        end = _lonlat(*shapely.get_coordinates(shapely.shortest_line(point, grid.lines[nearest]))[-1])
        kind, target = "nearest_line", int(grid.line_ids[nearest])
    else:
        return None
    geometry = json.dumps({"type": "LineString", "coordinates": [start, end]}, separators=(",", ":"))
    return {"geometry": geometry, "link": kind, "target": target, "length_m": round(_length_m(start, end))}
