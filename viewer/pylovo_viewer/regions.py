"""Versions, MV runs, regions and the search box: what can be selected and where it lies.

A region is a postcode (``plz``), an MV supply area (``area``) or one LV grid (``grid``). Bounds are
``[west, south, east, north]`` in degrees, so the map can fly to a selection.
"""
from __future__ import annotations

import re
from typing import Any

from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database

BOUNDS = """ARRAY[ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)]"""


def _bounds(db: Database, sql: str, params: Any) -> list[float] | None:
    row = db.one(f"SELECT {BOUNDS} AS b FROM (SELECT ST_Transform(ST_SetSRID(ST_Extent(geom)::geometry, "
                 f"{db.settings.target_epsg}), 4326) AS e FROM ({sql}) s) x", params)
    bounds = row and row["b"]
    return [round(float(v), 6) for v in bounds] if bounds and None not in bounds else None


def versions(db: Database, catalog: Catalog) -> list[dict[str, Any]]:
    """LV versions (newest first) with their counts and MV runs."""
    if not catalog.has("pylovo.version"):
        return []
    rows = db.query("""SELECT v.version_id, v.version_comment AS comment, v.created_at,
                              (SELECT count(*) FROM pylovo.grid_result g WHERE g.version_id = v.version_id) AS grids,
                              (SELECT count(*) FROM pylovo.postcode_result p WHERE p.version_id = v.version_id)
                                AS postcodes
                       FROM pylovo.version v ORDER BY v.created_at DESC NULLS LAST, v.version_id""")
    runs: dict[str, list[dict[str, Any]]] = {}
    if catalog.has_rows.get("pylovo.mv_variant") and catalog.has("pylovo.mv_grid"):
        for run in db.query("""SELECT v.version_id, v.mv_variant_id AS id, v.mv_variant_comment AS comment, v.status,
                                      v.created_at, v.finished_at,
                                      (SELECT count(*) FROM pylovo.mv_grid g WHERE g.version_id = v.version_id
                                         AND g.mv_variant_id = v.mv_variant_id) AS areas
                               FROM pylovo.mv_variant v ORDER BY v.created_at DESC NULLS LAST"""):
            runs.setdefault(run.pop("version_id"), []).append(run)
    for row in rows:
        row["runs"] = runs.get(row["version_id"], [])
    return rows


def postcodes(db: Database, version: str) -> list[dict[str, Any]]:
    """Postcodes of a version with name, number of grids and bounds, sorted by postcode."""
    return db.query(f"""
        SELECT pr.postcode_result_plz AS plz, COALESCE(m.name_city, p.note) AS name, pr.settlement_type AS settlement,
               (SELECT count(*) FROM pylovo.grid_result g WHERE g.version_id = pr.version_id
                  AND g.plz = pr.postcode_result_plz) AS grids,
               (SELECT {BOUNDS} FROM (SELECT ST_Transform(ST_Envelope(pr.geom), 4326) AS e) b) AS bounds
        FROM pylovo.postcode_result pr
        LEFT JOIN pylovo.postcode p ON p.plz = pr.postcode_result_plz
        LEFT JOIN LATERAL (SELECT name_city FROM pylovo.municipal_register mr WHERE mr.plz = pr.postcode_result_plz
                           ORDER BY mr.pop DESC NULLS LAST LIMIT 1) m ON true
        WHERE pr.version_id = %s ORDER BY pr.postcode_result_plz""", (version,))


def areas(db: Database, version: str, mv: str) -> list[dict[str, Any]]:
    """MV supply areas of a run with their substation name and bounds."""
    return db.query(f"""
        SELECT g.mv_grid_id AS area, s.name AS substation, g.vn_kv, g.n_stations AS stations, g.complete,
               g.power_flow_status AS pf,
               (SELECT {BOUNDS} FROM (SELECT ST_Transform(ST_Envelope(g.geom), 4326) AS e) b) AS bounds
        FROM pylovo.mv_grid g LEFT JOIN pylovo.mv_substation s ON s.substation_id = g.substation_id
        WHERE g.version_id = %s AND g.mv_variant_id = %s ORDER BY g.mv_grid_id""", (version, mv))


def bounds(db: Database, version: str | None, mv: str | None = None, plz: int | None = None,
           area: int | None = None, grid: int | None = None) -> list[float] | None:
    """Bounds of a region, or of the whole version / MV run when no region is given."""
    params = {"version": version, "mv": mv, "plz": plz, "area": area, "grid": grid}
    if grid is not None:
        return _bounds(db, """SELECT geom FROM pylovo.buildings_result WHERE grid_result_id = %(grid)s
                              UNION ALL SELECT geom FROM pylovo.transformer_positions WHERE grid_result_id = %(grid)s""",
                       params)
    if area is not None:
        return _bounds(db, "SELECT geom FROM pylovo.mv_grid WHERE mv_grid_id = %(area)s", params)
    if plz is not None:
        return _bounds(db, """SELECT geom FROM pylovo.postcode_result
                              WHERE version_id = %(version)s AND postcode_result_plz = %(plz)s""", params)
    if mv is not None:
        return _bounds(db, """SELECT geom FROM pylovo.mv_grid
                              WHERE version_id = %(version)s AND mv_variant_id = %(mv)s""", params)
    if version is not None:
        return _bounds(db, "SELECT geom FROM pylovo.postcode_result WHERE version_id = %(version)s", params)
    return None


def search(db: Database, catalog: Catalog, text: str, version: str | None, mv: str | None,
           limit: int = 12) -> list[dict[str, Any]]:
    """Hits for the search box: postcodes, places, municipality keys, grids, supply areas and buildings.

    Each hit has ``kind``, ``label``, ``detail`` and the region parameters (``plz``, ``area``,
    ``grid`` or ``objectid``) the map selects.
    """
    text = text.strip()
    if not text:
        return []
    hits: list[dict[str, Any]] = []
    digits = text.isdigit()
    if version and digits and len(text) <= 5:
        for row in db.query("""SELECT pr.postcode_result_plz AS plz, p.note FROM pylovo.postcode_result pr
                               LEFT JOIN pylovo.postcode p ON p.plz = pr.postcode_result_plz
                               WHERE pr.version_id = %s AND pr.postcode_result_plz::text LIKE %s
                               ORDER BY 1 LIMIT %s""", (version, text + "%", limit)):
            hits.append({"kind": "postcode", "label": str(row["plz"]), "detail": row["note"], "plz": row["plz"]})
    if version and catalog.has("pylovo.municipal_register"):
        if digits and len(text) >= 6:
            where, arg = "mr.ags::text LIKE %s", text.lstrip("0") + "%"
        elif not digits and len(text) >= 2:
            where, arg = "mr.name_city ILIKE %s", "%" + text + "%"
        else:
            where = None
        if where:
            for row in db.query(f"""SELECT mr.plz, mr.name_city, mr.ags FROM pylovo.municipal_register mr
                                    JOIN pylovo.postcode_result pr ON pr.postcode_result_plz = mr.plz
                                     AND pr.version_id = %s
                                    WHERE {where} ORDER BY mr.name_city, mr.plz LIMIT %s""", (version, arg, limit)):
                hits.append({"kind": "place", "label": row["name_city"], "detail": f"PLZ {row['plz']} · AGS "
                             f"{int(row['ags']):08d}", "plz": row["plz"]})
    grid = re.fullmatch(r"(?:grid\s*)?#?(\d+)", text, re.IGNORECASE)
    if version and grid and (not digits or len(text) != 5):
        row = db.one("""SELECT grid_result_id, plz, kcid, bcid, transformer_rated_power AS kva FROM pylovo.grid_result
                        WHERE version_id = %s AND grid_result_id = %s""", (version, int(grid.group(1))))
        if row:
            hits.append({"kind": "grid", "label": f"Grid {row['grid_result_id']}",
                         "detail": f"PLZ {row['plz']} · {row['kcid']}/{row['bcid']} · {row['kva']} kVA",
                         "grid": row["grid_result_id"]})
    if version and mv and catalog.has("pylovo.mv_grid"):
        area = re.fullmatch(r"(?:area\s*)?#?(\d+)", text, re.IGNORECASE)
        rows = db.query("""SELECT g.mv_grid_id, s.name FROM pylovo.mv_grid g
                           LEFT JOIN pylovo.mv_substation s ON s.substation_id = g.substation_id
                           WHERE g.version_id = %s AND g.mv_variant_id = %s
                             AND (s.name ILIKE %s OR g.mv_grid_id = %s) ORDER BY g.mv_grid_id LIMIT %s""",
                        (version, mv, "%" + text + "%", int(area.group(1)) if area else -1, limit))
        for row in rows:
            hits.append({"kind": "area", "label": f"Supply area {row['mv_grid_id']}",
                         "detail": row["name"] or "HV/MV substation", "area": row["mv_grid_id"]})
    if version and len(text) >= 6 and not digits:
        row = db.one("""SELECT b.objectid, b.grid_result_id, b.street, b.house_number FROM pylovo.buildings_result b
                        WHERE b.version_id = %s AND b.objectid = %s""", (version, text))
        if row:
            hits.append({"kind": "building", "label": row["objectid"],
                         "detail": " ".join(str(v) for v in (row["street"], row["house_number"]) if v) or None,
                         "grid": row["grid_result_id"], "objectid": row["objectid"]})
    return hits[:limit]
