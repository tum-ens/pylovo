"""LoD2 building models of a region from the ``citydb`` schema (3DCityDB v5), as one binary mesh.

The buildings of the selected region are linked by ``objectid`` (``pylovo.buildings_result`` =
``citydb.feature`` of class Building). Triangulation and the wire format are those of pylovo-api
(:mod:`pylovo_api.lod2`, decoded in the browser by ``static/js/lod2.js``); only the query runs here,
on the viewer's read-only connection. Without citydb the mesh is empty and the map extrudes the
2D footprints instead.
"""
from __future__ import annotations

import time
from typing import Any

from pylovo_api.lod2 import CLASS_CODES, MAX_BUILDINGS, build_mesh, encode

from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database
from pylovo_viewer.layers import REGION_GRIDS, Query

SURFACES = """
SELECT b.objectid, s.objectclass_id AS cls, ST_AsBinary(ST_Transform(g.geometry, 3857)) AS geom
FROM citydb.feature b
JOIN citydb.property p ON p.feature_id = b.id AND p.name = 'boundary'
JOIN citydb.feature s ON s.id = p.val_feature_id AND s.objectclass_id = ANY(%(classes)s)
JOIN citydb.geometry_data g ON g.feature_id = s.id
WHERE b.objectid = ANY(%(ids)s) AND b.objectclass_id = %(building)s AND g.geometry IS NOT NULL"""


def region_mesh(db: Database, catalog: Catalog, q: Query) -> bytes:
    """The encoded LoD2 mesh of the region's buildings (an empty mesh when there is no LoD2)."""
    started = time.time()
    region = q.region()
    if region is None:
        raise ValueError("LoD2 needs a region (plz, area or grid)")
    classes = catalog.citydb_classes
    header: dict[str, Any] = {"region": region, "requested": 0}
    if classes is None:
        return encode({**header, "available": False, "reason": "no LoD2 data (citydb) in this database"})
    rows = db.query(f"""SELECT DISTINCT objectid FROM pylovo.buildings_result
                        WHERE version_id = %(version)s AND objectid IS NOT NULL
                          AND grid_result_id IN ({REGION_GRIDS[region]})
                        ORDER BY 1 LIMIT {MAX_BUILDINGS + 1}""", q.params())
    ids = [str(r["objectid"]) for r in rows]
    wanted = {classes[name]: code for name, code in CLASS_CODES.items() if name in classes}
    if "GroundSurface" in classes:
        wanted[classes["GroundSurface"]] = 0
    surfaces = []
    if ids and wanted:
        surfaces = [(str(r["objectid"]), wanted[int(r["cls"])], bytes(r["geom"])) for r in db.query(
            SURFACES, {"ids": ids[:MAX_BUILDINGS], "classes": list(wanted), "building": classes["Building"]})]
    mesh = build_mesh(surfaces)
    header.update(requested=min(len(ids), MAX_BUILDINGS), available=True, with_lod2=len(mesh["buildings"]),
                  truncated=len(ids) > MAX_BUILDINGS, ms=round((time.time() - started) * 1000))
    return encode(header, mesh)
