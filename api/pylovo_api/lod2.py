"""LoD2 building models of one grid from the InfDB ``citydb`` schema (3DCityDB v5), as a mesh.

The Grid inspector can show the buildings of the selected grid as their LoD2 models. The
buildings are linked by ``objectid`` (``pylovo.buildings_result.objectid`` =
``citydb.feature.objectid`` of the Building feature). Each building's roof and wall surfaces
(the ``boundary`` features and their ``lod2MultiSurface`` in ``citydb.geometry_data``) are
triangulated here and sent as one small binary mesh (:func:`encode`). Buildings without LoD2
data, or a database without ``citydb``, simply give an empty mesh: the map then shows those
buildings in 2D as usual.

Coordinates: x and y in EPSG:3857 metres relative to ``origin``; z in metres above the
building's lowest ground point (the map has no terrain). The browser turns them into
MapLibre's mercator units (``js/panels/map_lod2.js`` of the GridPlanner UI).
"""
from __future__ import annotations

import gzip
import json
import logging
import math
import struct
import threading
import time
from collections import OrderedDict
from typing import Any

import numpy as np
import shapely
from shapely import wkb

from pylovo_api import db

log = logging.getLogger(__name__)

MAGIC = b"LOD2"
FORMAT_VERSION = 1
MAX_BUILDINGS = 5000                 # one grid; larger requests are cut
CLASS_CODES = {"RoofSurface": 1, "WallSurface": 2}   # ground surfaces are not drawn (seen from above only)
EARTH_RADIUS = 6378137.0             # EPSG:3857 sphere
_SCHEMA_TTL_S = 300.0

_schema_lock = threading.Lock()
_schema: dict[str, Any] = {"checked_at": 0.0, "value": None}
_cache: OrderedDict[int, bytes] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 24


# --------------------------------------------------------------------------- schema
def citydb_classes() -> dict[str, int] | None:
    """Object class ids of Building, RoofSurface, WallSurface and GroundSurface, or ``None``.

    ``None`` means the database has no usable ``citydb`` schema (checked every few minutes, so a
    later LoD2 import is picked up without a restart).
    """
    with _schema_lock:
        if time.time() - _schema["checked_at"] < _SCHEMA_TTL_S:
            return _schema["value"]
    value = None
    try:
        row = db.fetch_one(
            """SELECT to_regclass('citydb.feature') IS NOT NULL AND to_regclass('citydb.property') IS NOT NULL
                      AND to_regclass('citydb.geometry_data') IS NOT NULL
                      AND to_regclass('citydb.objectclass') IS NOT NULL AS ok""")
        if row and row["ok"]:
            rows = db.fetch_all("""SELECT classname, id FROM citydb.objectclass
                                   WHERE classname IN ('Building', 'RoofSurface', 'WallSurface', 'GroundSurface')""")
            classes = {r["classname"]: int(r["id"]) for r in rows}
            if "Building" in classes:
                value = classes
    except Exception as exc:  # noqa: BLE001 - no citydb (or no rights): LoD2 is simply unavailable
        log.info("citydb not usable for LoD2 buildings: %s", exc)
    with _schema_lock:
        _schema.update(checked_at=time.time(), value=value)
    return value


# --------------------------------------------------------------------------- data
def grid_objectids(grid_result_id: int) -> list[str]:
    rows = db.fetch_all("""SELECT DISTINCT objectid FROM pylovo.buildings_result
                           WHERE grid_result_id = %s AND objectid IS NOT NULL ORDER BY 1""", (grid_result_id,))
    return [str(r["objectid"]) for r in rows]


def fetch_surfaces(objectids: list[str], classes: dict[str, int]) -> list[tuple[str, int, bytes]]:
    """``(objectid, class code, WKB in EPSG:3857 with Z)`` of the roof, wall and ground surfaces."""
    if not objectids:
        return []
    wanted = {classes[name]: code for name, code in CLASS_CODES.items() if name in classes}
    if "GroundSurface" in classes:
        wanted[classes["GroundSurface"]] = 0
    if not wanted:
        return []
    rows = db.fetch_all(
        """SELECT b.objectid, s.objectclass_id AS cls, ST_AsBinary(ST_Transform(g.geometry, 3857)) AS geom
           FROM citydb.feature b
           JOIN citydb.property p ON p.feature_id = b.id AND p.name = 'boundary'
           JOIN citydb.feature s ON s.id = p.val_feature_id AND s.objectclass_id = ANY(%(classes)s)
           JOIN citydb.geometry_data g ON g.feature_id = s.id
           WHERE b.objectid = ANY(%(ids)s) AND b.objectclass_id = %(building)s AND g.geometry IS NOT NULL""",
        {"ids": objectids, "classes": list(wanted), "building": classes["Building"]})
    return [(str(r["objectid"]), wanted[int(r["cls"])], bytes(r["geom"])) for r in rows]


# --------------------------------------------------------------------------- geometry
def _polygons(geom: Any) -> list[Any]:
    if geom is None or geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):   # MultiPolygon, PolyhedralSurface/TIN (read as MultiPolygon), collections
        return [p for g in geom.geoms for p in _polygons(g)]
    return []


def _newell(ring: np.ndarray) -> np.ndarray:
    """Newell normal of a closed-or-open 3D ring (not normalised)."""
    x, y, z = ring[:, 0], ring[:, 1], ring[:, 2]
    xn, yn, zn = np.roll(x, -1), np.roll(y, -1), np.roll(z, -1)
    return np.array([np.sum((y - yn) * (z + zn)), np.sum((z - zn) * (x + xn)), np.sum((x - xn) * (y + yn))])


def triangulate(polygon: Any) -> tuple[np.ndarray, np.ndarray] | None:
    """Triangles of one planar 3D polygon (holes allowed): ``(vertices (n, 3), triangles (m, 3))``.

    The polygon is projected onto the coordinate plane its normal is closest to, triangulated there
    (constrained Delaunay, no new vertices) and mapped back to its own 3D vertices.
    """
    rings = [np.asarray(polygon.exterior.coords, dtype=float)] + \
            [np.asarray(r.coords, dtype=float) for r in polygon.interiors]
    rings = [r[:-1] if len(r) > 1 and np.allclose(r[0], r[-1]) else r for r in rings]
    if len(rings[0]) < 3 or rings[0].shape[1] < 3:
        return None
    normal = _newell(rings[0])
    if not np.any(normal):
        return None
    keep = [a for a in range(3) if a != int(np.argmax(np.abs(normal)))]
    verts = np.vstack(rings)
    flat = shapely.Polygon(rings[0][:, keep], [r[:, keep] for r in rings[1:] if len(r) >= 3])
    if not flat.is_valid:
        flat = shapely.make_valid(flat)
    lookup = {(round(a, 3), round(b, 3)): i for i, (a, b) in enumerate(verts[:, keep])}
    tris: list[list[int]] = []
    for tri in _polygons(shapely.constrained_delaunay_triangles(flat)):
        idx = [lookup.get((round(a, 3), round(b, 3))) for a, b in list(tri.exterior.coords)[:3]]
        if None in idx or len(set(idx)) < 3:
            continue   # a vertex that make_valid introduced: skip the sliver rather than guess its height
        tris.append(idx)
    if not tris:
        return None
    return verts, np.asarray(tris, dtype=np.int64)


def build_mesh(surfaces: list[tuple[str, int, bytes]]) -> dict[str, Any]:
    """Vertices, triangles, per-vertex building index and surface class of all surfaces."""
    by_building: dict[str, list[tuple[int, Any]]] = {}
    for objectid, code, blob in surfaces:
        try:
            geom = wkb.loads(blob)
        except Exception:  # noqa: BLE001 - a broken geometry is skipped, not fatal
            continue
        if not geom.has_z:
            continue
        by_building.setdefault(objectid, []).extend((code, p) for p in _polygons(geom))
    buildings: list[str] = []
    parts_v, parts_t, parts_b, parts_c = [], [], [], []
    offset = 0
    for objectid in sorted(by_building):
        polys = by_building[objectid]
        grounds = [p for code, p in polys if code == 0]
        base = min((c[2] for p in (grounds or [p for _, p in polys]) for c in p.exterior.coords), default=0.0)
        bi = len(buildings)   # only buildings that produce triangles get an index
        produced = False
        for code, poly in polys:
            if code == 0:
                continue
            tri = triangulate(poly)
            if tri is None:
                continue
            v, t = tri
            v = v.copy()
            v[:, 2] -= base
            parts_v.append(v)
            parts_t.append(t + offset)
            parts_b.append(np.full(len(v), bi, dtype=np.uint32))
            parts_c.append(np.full(len(v), code, dtype=np.uint8))
            offset += len(v)
            produced = True
        if produced:
            buildings.append(objectid)
    if not parts_v:
        return {"buildings": [], "vertices": np.zeros((0, 3)), "triangles": np.zeros((0, 3), dtype=np.int64),
                "building_index": np.zeros(0, dtype=np.uint32), "surface_class": np.zeros(0, dtype=np.uint8)}
    return {"buildings": buildings, "vertices": np.vstack(parts_v), "triangles": np.vstack(parts_t),
            "building_index": np.concatenate(parts_b), "surface_class": np.concatenate(parts_c)}


def _lonlat(x: float, y: float) -> tuple[float, float]:
    """EPSG:3857 metres to longitude and latitude (degrees)."""
    return math.degrees(x / EARTH_RADIUS), math.degrees(2 * math.atan(math.exp(y / EARTH_RADIUS)) - math.pi / 2)


# --------------------------------------------------------------------------- encoding
def encode(header: dict[str, Any], mesh: dict[str, Any] | None = None) -> bytes:
    """``LOD2`` · version · header length (uint32 LE) · header JSON (padded to 4 bytes) · buffers.

    Buffers, in this order: positions float32 (3 per vertex, relative to ``origin``), triangle
    indices uint32 (3 per triangle), building index uint16 or uint32 (per vertex, see
    ``building_index_bytes``), surface class uint8 (per vertex; 1 roof, 2 wall), padded to 4 bytes.
    """
    body = b""
    if mesh is not None and len(mesh["vertices"]):
        verts = mesh["vertices"]
        ox, oy = float(verts[:, 0].mean()), float(verts[:, 1].mean())
        rel = np.column_stack([verts[:, 0] - ox, verts[:, 1] - oy, verts[:, 2]]).astype("<f4")
        wide = len(mesh["buildings"]) > 65535
        bidx = mesh["building_index"].astype("<u4" if wide else "<u2")
        cls = mesh["surface_class"].astype("u1")
        body = rel.tobytes() + mesh["triangles"].astype("<u4").tobytes() + bidx.tobytes() + cls.tobytes()
        body += b"\0" * (-len(body) % 4)
        header = {**header, "origin": list(_lonlat(ox, oy)), "vertices": int(len(verts)),
                  "triangles": int(len(mesh["triangles"])), "building_index_bytes": 4 if wide else 2,
                  "buildings": mesh["buildings"]}
    else:
        header = {**header, "vertices": 0, "triangles": 0, "buildings": header.get("buildings", [])}
    head = json.dumps(header, separators=(",", ":")).encode()
    head += b" " * (-len(head) % 4)
    return MAGIC + struct.pack("<II", FORMAT_VERSION, len(head)) + head + body


def grid_lod2(grid_result_id: int) -> bytes:
    """The encoded LoD2 mesh of one grid (cached per grid; an empty mesh when there is no LoD2)."""
    with _cache_lock:
        if grid_result_id in _cache:
            _cache.move_to_end(grid_result_id)
            return _cache[grid_result_id]
    started = time.time()
    objectids = grid_objectids(grid_result_id)
    header: dict[str, Any] = {"grid_result_id": grid_result_id, "requested": len(objectids)}
    classes = citydb_classes()
    if classes is None:
        blob = encode({**header, "available": False, "reason": "no citydb schema in this database"})
        return blob   # not cached: an import may add it
    mesh = build_mesh(fetch_surfaces(objectids[:MAX_BUILDINGS], classes))
    header.update(available=True, with_lod2=len(mesh["buildings"]), truncated=len(objectids) > MAX_BUILDINGS,
                  ms=round((time.time() - started) * 1000))
    blob = encode(header, mesh)
    with _cache_lock:
        _cache[grid_result_id] = blob
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return blob


def gzipped(blob: bytes) -> bytes:
    return gzip.compress(blob, compresslevel=5, mtime=0)
