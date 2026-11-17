"""LoD2 building models in the map view from the ``citydb`` schema (3DCityDB v5), as one binary mesh.

The buildings of the version in the view are linked by ``objectid`` (``pylovo.buildings_result`` =
``citydb.feature`` of class Building). Triangulation and the wire format are those of pylovo-api
(:mod:`pylovo_api.lod2`, decoded in the browser by ``static/js/lod2.js``); only the query runs here,
on the viewer's read-only connection. Without citydb the mesh is empty and the map extrudes the
2D footprints instead.
"""
from __future__ import annotations

import time
from typing import Any

from pylovo_api.lod2 import MAX_BUILDINGS, build_mesh, encode, surface_rows, surfaces_query

from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database
from pylovo_viewer.layers import Query

def view_mesh(db: Database, catalog: Catalog, q: Query) -> bytes:
    """The encoded LoD2 mesh of the version's buildings in the view (empty when there is no LoD2)."""
    started = time.time()
    if q.version is None or q.bbox is None:
        raise ValueError("LoD2 needs version and bbox")
    classes = catalog.citydb_classes
    header: dict[str, Any] = {"requested": 0}
    if classes is None:
        return encode({**header, "available": False, "reason": "no LoD2 data (citydb) in this database"})
    epsg = db.settings.target_epsg
    rows = db.query(f"""SELECT DISTINCT objectid FROM pylovo.buildings_result
                        WHERE version_id = %(version)s AND objectid IS NOT NULL
                          AND geom && ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326), {epsg})
                        ORDER BY 1 LIMIT {MAX_BUILDINGS + 1}""", q.params())
    ids = [str(r["objectid"]) for r in rows]
    query = surfaces_query(ids[:MAX_BUILDINGS], classes)
    surfaces = surface_rows(db.query(*query), classes) if query else []
    mesh = build_mesh(surfaces)
    header.update(requested=min(len(ids), MAX_BUILDINGS), available=True, with_lod2=len(mesh["buildings"]),
                  truncated=len(ids) > MAX_BUILDINGS, ms=round((time.time() - started) * 1000))
    return encode(header, mesh)
