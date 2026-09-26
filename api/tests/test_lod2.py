"""LoD2 building models of a grid (pylovo_api.lod2): triangulation, mesh, binary format, endpoint.

The unit tests use synthetic geometry. The endpoint test needs the opt-in sandbox database; it
passes with or without a ``citydb`` schema there (without one the mesh is empty, never an error).
"""
from __future__ import annotations

import json
import struct

import numpy as np
import pytest
import shapely
from conftest import requires_db
from pylovo_api import lod2


def area3d(v: np.ndarray, t: np.ndarray) -> float:
    return float(np.sum(np.linalg.norm(np.cross(v[t[:, 1]] - v[t[:, 0]], v[t[:, 2]] - v[t[:, 0]]), axis=1)) / 2)


def decode(blob: bytes) -> tuple[dict, dict]:
    assert blob[:4] == lod2.MAGIC
    version, hlen = struct.unpack("<II", blob[4:12])
    assert version == lod2.FORMAT_VERSION and hlen % 4 == 0
    head = json.loads(blob[12:12 + hlen])
    o = 12 + hlen
    nv, nt = head["vertices"], head["triangles"]
    out: dict = {}
    if nv:
        out["pos"] = np.frombuffer(blob, "<f4", nv * 3, o).reshape(-1, 3); o += nv * 12
        out["idx"] = np.frombuffer(blob, "<u4", nt * 3, o).reshape(-1, 3); o += nt * 12
        width = head["building_index_bytes"]
        out["bld"] = np.frombuffer(blob, "<u2" if width == 2 else "<u4", nv, o); o += nv * width
        out["cls"] = np.frombuffer(blob, "u1", nv, o); o += nv
        assert len(blob) == o + (-o % 4)
    return head, out


def gable_building(x0: float, y0: float, base: float) -> list[tuple[int, list]]:
    """(class code, rings) of a 10 x 6 m house: two long walls, two gable walls, two roof planes, ground."""
    p = [(x0, y0), (x0 + 10, y0), (x0 + 10, y0 + 6), (x0, y0 + 6)]
    he, hr = 3.0, 6.0
    P = lambda i, z: (*p[i], base + z)  # noqa: E731
    r1, r0 = (x0 + 10, y0 + 3, base + hr), (x0, y0 + 3, base + hr)
    walls = [[P(0, 0), P(1, 0), P(1, he), P(0, he)], [P(2, 0), P(3, 0), P(3, he), P(2, he)],
             [P(1, 0), P(2, 0), P(2, he), r1, P(1, he)], [P(3, 0), P(0, 0), P(0, he), r0, P(3, he)]]
    roofs = [[P(0, he), P(1, he), r1, r0], [P(2, he), P(3, he), r0, r1]]
    ground = [[P(0, 0), P(3, 0), P(2, 0), P(1, 0)]]
    return [(2, walls), (1, roofs), (0, ground)]


def surfaces_of(objectid: str, parts: list[tuple[int, list]]) -> list[tuple[str, int, bytes]]:
    return [(objectid, code, shapely.MultiPolygon([shapely.Polygon(ring)]).wkb) for code, rings in parts for ring in rings]


def test_triangulation_keeps_area_and_vertices():
    wall = shapely.Polygon([(0, 0, 0), (10, 0, 0), (10, 0, 3), (5, 0, 6), (0, 0, 3)])          # vertical gable wall
    roof = shapely.Polygon([(0, 0, 3), (10, 0, 3), (10, 3, 6), (0, 3, 6)])                    # sloped plane
    ell = shapely.Polygon([(0, 0, 5), (8, 0, 5), (8, 3, 5), (3, 3, 5), (3, 8, 5), (0, 8, 5)])  # concave flat roof
    court = shapely.Polygon([(0, 0, 9), (10, 0, 9), (10, 10, 9), (0, 10, 9)],
                            [[(3, 3, 9), (7, 3, 9), (7, 7, 9), (3, 7, 9)]])                     # courtyard (hole)
    expected = [10 * 3 + 0.5 * 10 * 3, 10 * np.hypot(3, 3), 8 * 3 + 3 * 5, 100 - 16]
    for poly, area in zip((wall, roof, ell, court), expected):
        verts, tris = lod2.triangulate(poly)
        assert area3d(verts, tris) == pytest.approx(area, rel=1e-9)
        ring = {tuple(c) for c in poly.exterior.coords} | {tuple(c) for r in poly.interiors for c in r.coords}
        assert {tuple(v) for v in verts} <= ring                                              # no new vertices
    assert lod2.triangulate(shapely.Polygon([(0, 0, 0), (1, 1, 1), (2, 2, 2)])) is None     # degenerate


def test_mesh_drops_ground_and_sits_every_building_on_its_ground():
    surfaces = surfaces_of("B", gable_building(100, 200, 512.5)) + surfaces_of("A", gable_building(0, 0, 480.0))
    mesh = lod2.build_mesh(surfaces)
    assert mesh["buildings"] == ["A", "B"]
    assert set(np.unique(mesh["surface_class"])) == {1, 2}                                   # no ground surfaces
    for bi in (0, 1):
        z = mesh["vertices"][mesh["building_index"] == bi][:, 2]
        assert z.min() == pytest.approx(0) and z.max() == pytest.approx(6)
    walls = mesh["surface_class"] == 2
    assert walls.sum() == 2 * (4 + 4 + 5 + 5)
    tri = mesh["triangles"]
    assert area3d(mesh["vertices"], tri) == pytest.approx(2 * (2 * 30 + 2 * (18 + 9) + 2 * 10 * np.hypot(3, 3)))


def test_encoding_round_trip_and_empty_mesh():
    mesh = lod2.build_mesh(surfaces_of("A", gable_building(1_300_000, 6_100_000, 400.0)))
    head, buf = decode(lod2.encode({"available": True, "requested": 3}, mesh))
    assert head["available"] and head["requested"] == 3 and head["buildings"] == ["A"]
    assert head["vertices"] == len(mesh["vertices"]) and head["triangles"] == len(mesh["triangles"])
    lon, lat = head["origin"]
    assert 11 < lon < 12 and 47 < lat < 49                                                   # EPSG:3857 -> degrees
    assert np.abs(buf["pos"][:, :2]).max() < 20                                              # relative to the origin
    assert buf["idx"].max() < head["vertices"] and set(buf["cls"]) == {1, 2} and set(buf["bld"]) == {0}
    head, buf = decode(lod2.encode({"available": False, "reason": "no citydb"}))
    assert head["vertices"] == 0 and head["buildings"] == [] and buf == {}
    assert lod2.build_mesh([("X", 1, b"not wkb")])["buildings"] == []                        # broken geometry is skipped


@requires_db
def test_lod2_endpoint_never_fails(client):
    from pylovo_api import db
    grid = db.fetch_one("SELECT grid_result_id FROM pylovo.grid_result ORDER BY grid_result_id LIMIT 1")
    r = client.get(f"/api/grids/{grid['grid_result_id']}/lod2")
    assert r.status_code == 200 and r.headers["content-type"] == "application/octet-stream"
    head, buf = decode(r.content)
    has_citydb = db.fetch_one("SELECT to_regclass('citydb.feature') IS NOT NULL AS ok")["ok"]
    assert head["available"] is bool(has_citydb) and head["requested"] > 0
    if has_citydb:
        assert 0 <= len(head["buildings"]) <= head["requested"]
        assert (head["vertices"] > 0) is bool(head["buildings"])
    missing = client.get("/api/grids/999999999/lod2")
    assert missing.status_code == 200 and decode(missing.content)[0]["vertices"] == 0
