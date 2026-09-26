"""Read queries of the UI (regions, transformers, versions, grid results).

All geometry leaves the database as GeoJSON in EPSG:4326 with six decimals (~0.1 m).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any

from pylovo_api import cabinets, db, grid_metrics, topology

# Same classification the generator uses (preprocessing_mixin.insert_transformers).
TRAFO_SOURCE_SQL = """CASE
    WHEN t.type IN ('dso', 'dso_validation') OR t.osm_id LIKE 'dso/%%' OR t.osm_id LIKE 'dso_validation/%%' THEN 'dso'
    WHEN t.osm_id LIKE 'manual/%%' THEN 'manual'
    WHEN NOT t.osm AND t.lod2 THEN 'lod2'
    ELSE 'osm' END"""

LINE_ROLE_SQL = """CASE
    WHEN l.service_sizing_basis IS NOT NULL THEN 'service'
    WHEN l.feeder_section_id IS NULL AND COALESCE(l.length_km, 0) <= 0.0011 THEN 'link'
    ELSE 'feeder' END"""


def epsg() -> int:
    return db.settings()["target_epsg"]


def envelope_sql(alias: str = "geom") -> str:
    return f"{alias} && ST_Transform(ST_MakeEnvelope(%(minx)s, %(miny)s, %(maxx)s, %(maxy)s, 4326), {epsg()})"


def parse_bbox(bbox: str | None) -> dict[str, float] | None:
    if not bbox:
        return None
    parts = [float(p) for p in bbox.split(",")]
    if len(parts) != 4 or not all(math.isfinite(p) for p in parts):
        raise ValueError("bbox must be minLon,minLat,maxLon,maxLat")
    return dict(zip(("minx", "miny", "maxx", "maxy"), parts))


def feature_collection(rows: list[dict], geometry_key: str = "geometry", id_key: str | None = None) -> dict:
    features = []
    for row in rows:
        geometry = row.pop(geometry_key, None)
        if geometry is None:
            continue
        feature = {"type": "Feature", "geometry": geometry, "properties": row}
        if id_key and row.get(id_key) is not None:
            feature["id"] = row[id_key]
        features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def ags_text(ags: Any) -> str | None:
    return None if ags is None else str(int(ags)).zfill(8)


def num(value: Any, digits: int | None = None) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits) if digits is not None else value


# --------------------------------------------------------------------------- status
def status() -> dict[str, Any]:
    """Database reachability, schema state and table counts."""
    info: dict[str, Any] = {"settings": None, "connected": False}
    try:
        info["settings"] = db.settings()
    except Exception as exc:  # noqa: BLE001 - e.g. a config_generation.yaml pylovo rejects
        info["error"] = f"pylovo cannot load its configuration: {type(exc).__name__}: {exc}"
        info["config_error"] = True
        return info
    try:
        with db.cursor(timeout_s=3) as cur:
            cur.execute("SELECT current_setting('server_version') AS version")
            info["server_version"] = cur.fetchone()["version"]
            cur.execute("SELECT extname, extversion FROM pg_extension")
            info["extensions"] = {r["extname"]: r["extversion"] for r in cur.fetchall()}
            info["connected"] = True
            tables = db.existing_tables(cur)
            cur.execute("SELECT to_regnamespace('pylovo') IS NOT NULL AS ok")
            info["schema_exists"] = cur.fetchone()["ok"]
            required = ["version", "postcode", "transformers", "municipal_register", "grid_result",
                        "lines_result", "buildings_result", "postcode_result", "consumer_categories"]
            info["missing_tables"] = [t for t in required if t not in tables]
            counts: dict[str, int | None] = {}
            for table in ("transformers", "postcode", "municipal_register", "version", "postcode_result",
                          "grid_result"):
                if table in tables:
                    cur.execute(f"SELECT count(*) AS n FROM pylovo.{table}")
                    counts[table] = cur.fetchone()["n"]
            for table in ("buildings_result", "lines_result", "pandapower_bus"):
                if table in tables:  # large tables: planner estimate is good enough
                    cur.execute("SELECT GREATEST(reltuples, 0)::bigint AS n FROM pg_class WHERE oid = %s::regclass",
                                (f"pylovo.{table}",))
                    counts[table] = cur.fetchone()["n"]
            info["counts"] = counts
            cur.execute("SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                        "WHERE n.nspname = 'pylovo' AND p.proname = 'segment_intersecting_ways'")
            functions_loaded = cur.fetchone()["n"] > 0
            info["functions_loaded"] = functions_loaded
            info["setup_complete"] = bool(info["schema_exists"] and not info["missing_tables"] and functions_loaded
                                          and counts.get("transformers") and counts.get("postcode")
                                          and counts.get("municipal_register"))
            s = info["settings"]
            if s["use_infdb"] and s["infdb_source_schema"]:
                cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{s['infdb_source_schema']}.buildings",))
                if cur.fetchone()["ok"]:
                    cur.execute('SELECT GREATEST(reltuples, 0)::bigint AS n FROM pg_class WHERE oid = %s::regclass',
                                (f"{s['infdb_source_schema']}.buildings",))
                    estimate = cur.fetchone()["n"]
                    if estimate < 200_000:
                        cur.execute(f'SELECT count(*) AS n FROM "{s["infdb_source_schema"]}".buildings')
                        estimate = cur.fetchone()["n"]
                    info["infdb_buildings"] = estimate
                else:
                    info["infdb_buildings"] = None
    except db.DatabaseUnavailable as exc:
        info["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001 - any database error is shown in the UI
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def version_exists(version_id: str) -> dict[str, Any]:
    try:
        row = db.fetch_one(
            """SELECT v.version_id, v.version_comment, v.created_at,
                      (SELECT count(*) FROM pylovo.grid_result g WHERE g.version_id = v.version_id) AS grids,
                      (SELECT array_agg(pr.postcode_result_plz ORDER BY pr.postcode_result_plz)
                         FROM pylovo.postcode_result pr WHERE pr.version_id = v.version_id) AS plz
               FROM pylovo.version v WHERE v.version_id = %s""", (str(version_id),))
    except Exception:  # noqa: BLE001 - schema may be missing
        return {"exists": False}
    if not row:
        return {"exists": False}
    return {"exists": True, "comment": row["version_comment"], "created_at": row["created_at"],
            "grids": row["grids"], "plz": row["plz"] or []}


# --------------------------------------------------------------------------- regions
def search_regions(q: str, limit: int = 60, sel: list[int] | None = None) -> list[dict]:
    """Search PLZ, AGS or municipality names in the municipal register and ``pylovo.postcode``.

    ``sel`` (the PLZ the region gate lets the user select) are ranked first before the hit list
    is cut, so selectable regions are never crowded out by a long register match.
    """
    q = q.strip()
    if not q:
        return []
    digits = re.sub(r"\D", "", q)
    params = {"q": q, "digits": digits.lstrip("0") or digits, "limit": limit, "sel": list(sel or [])}
    conditions = ["mr.name_city ILIKE '%%' || %(q)s || '%%'"]
    if digits:
        conditions += ["mr.plz::text LIKE %(digits)s || '%%'", "mr.ags::text LIKE %(digits)s || '%%'"]
    rows = db.fetch_all(
        f"""
        WITH hits AS (
            SELECT mr.plz, mr.ags, mr.name_city, mr.pop, mr.area, mr.fed_state
            FROM pylovo.municipal_register mr
            WHERE {' OR '.join(conditions)}
            ORDER BY (mr.plz::text = %(digits)s) DESC, (mr.ags::text = %(digits)s) DESC,
                     (mr.plz = ANY(%(sel)s::int[])) DESC, mr.name_city, mr.plz
            LIMIT %(limit)s * 3
        ), extra AS (
            SELECT p.plz, NULL::bigint AS ags, p.note AS name_city, p.population::bigint AS pop, p.qkm AS area,
                   NULL::int AS fed_state
            FROM pylovo.postcode p
            WHERE (p.plz::text LIKE %(digits)s || '%%' AND %(digits)s <> '') OR p.note ILIKE '%%' || %(q)s || '%%'
        )
        SELECT h.*, (p.plz IS NOT NULL) AS available, p.note,
               (SELECT array_agg(pr.version_id ORDER BY pr.version_id) FROM pylovo.postcode_result pr
                 WHERE pr.postcode_result_plz = h.plz) AS versions
        FROM (SELECT * FROM hits UNION ALL SELECT * FROM extra WHERE plz NOT IN (SELECT plz FROM hits)) h
        LEFT JOIN pylovo.postcode p ON p.plz = h.plz
        ORDER BY available DESC, h.name_city, h.plz
        LIMIT %(limit)s
        """, params)
    for row in rows:
        row["ags"] = ags_text(row["ags"])
        row["versions"] = row["versions"] or []
        row["area"] = num(row["area"], 2)
    return rows


def regions_for_plz(plz_list: list[int], limit: int = 200, offset: int = 0) -> tuple[list[dict], int]:
    """Rows in the shape of :func:`search_regions` for the given PLZ, sorted by municipality and PLZ.

    A PLZ that spans several municipalities gets one row per municipality; PLZ missing from the
    municipal register fall back to their ``pylovo.postcode`` note. Returns ``(rows, total)``.
    """
    if not plz_list:
        return [], 0
    rows = db.fetch_all(
        """
        WITH sel AS (SELECT DISTINCT unnest(%(sel)s::int[]) AS plz),
        hits AS (
            SELECT mr.plz, mr.ags, mr.name_city, mr.pop, mr.area, mr.fed_state
            FROM pylovo.municipal_register mr JOIN sel USING (plz)
            UNION ALL
            SELECT p.plz, NULL::bigint, p.note, p.population::bigint, p.qkm, NULL::int
            FROM pylovo.postcode p JOIN sel USING (plz)
            WHERE NOT EXISTS (SELECT 1 FROM pylovo.municipal_register mr WHERE mr.plz = p.plz)
        )
        SELECT h.*, (p.plz IS NOT NULL) AS available, p.note,
               (SELECT array_agg(pr.version_id ORDER BY pr.version_id) FROM pylovo.postcode_result pr
                 WHERE pr.postcode_result_plz = h.plz) AS versions,
               count(*) OVER () AS total
        FROM hits h
        LEFT JOIN pylovo.postcode p ON p.plz = h.plz
        ORDER BY h.name_city NULLS LAST, h.plz, h.ags
        LIMIT %(limit)s OFFSET %(offset)s
        """, {"sel": sorted(int(p) for p in plz_list), "limit": limit, "offset": offset})
    total = int(rows[0]["total"]) if rows else 0
    for row in rows:
        row.pop("total", None)
        row["ags"] = ags_text(row["ags"])
        row["versions"] = row["versions"] or []
        row["area"] = num(row["area"], 2)
    return rows, total


def regions_overview() -> dict[str, Any]:
    """Extent of all postcode polygons and the PLZ that have results (for the initial map view)."""
    row = db.fetch_one(
        """SELECT s.n, ST_XMin(s.e) AS minx, ST_YMin(s.e) AS miny, ST_XMax(s.e) AS maxx, ST_YMax(s.e) AS maxy
           FROM (SELECT ST_Transform(ST_SetSRID(ST_Extent(geom)::geometry, %s), 4326) AS e, count(*) AS n
                 FROM pylovo.postcode) s""", (epsg(),))
    generated = db.fetch_all("SELECT DISTINCT postcode_result_plz AS plz FROM pylovo.postcode_result ORDER BY 1")
    bounds = None
    if row and row["minx"] is not None:
        bounds = [num(row["minx"], 6), num(row["miny"], 6), num(row["maxx"], 6), num(row["maxy"], 6)]
    return {"postcodes": row["n"] if row else 0, "bounds": bounds, "generated": [r["plz"] for r in generated]}


def postcodes_geojson(bbox: dict | None, zoom: float | None, plz: list[int] | None = None,
                      first: list[int] | None = None, only_first: bool = False) -> dict:
    """Postcode polygons in a bounding box (simplified to the zoom level).

    ``first`` (the selectable PLZ of the region gate) and PLZ with results come first under the
    4000-feature cap; ``only_first`` drops all other PLZ in SQL.
    """
    tolerance = 2.0
    if zoom is not None:
        tolerance = max(0.5, min(400.0, 40_000_000 / (256 * 2 ** zoom) * 1.5))
    where, params = [], {"tol": tolerance, "first": list(first or [])}
    has_results = "EXISTS (SELECT 1 FROM pylovo.postcode_result pr WHERE pr.postcode_result_plz = p.plz)"
    if only_first:
        where.append(f"(p.plz = ANY(%(first)s::int[]) OR {has_results})")
    if bbox:
        where.append(envelope_sql("p.geom"))
        params.update(bbox)
    if plz:
        where.append("p.plz = ANY(%(plz)s)")
        params["plz"] = plz
    rows = db.fetch_all(
        f"""SELECT p.plz, p.note, p.qkm, p.population,
                   (SELECT array_agg(pr.version_id ORDER BY pr.version_id) FROM pylovo.postcode_result pr
                     WHERE pr.postcode_result_plz = p.plz) AS versions,
                   round(ST_X(ST_Transform(ST_PointOnSurface(p.geom), 4326))::numeric, 6)::float AS label_lon,
                   round(ST_Y(ST_Transform(ST_PointOnSurface(p.geom), 4326))::numeric, 6)::float AS label_lat,
                   ST_AsGeoJSON(ST_Transform(ST_SimplifyPreserveTopology(p.geom, %(tol)s), 4326), 6)::json AS geometry
            FROM pylovo.postcode p {'WHERE ' + ' AND '.join(where) if where else ''}
            ORDER BY (p.plz = ANY(%(first)s::int[]) OR {has_results}) DESC, p.plz LIMIT 4000""", params)
    for row in rows:
        row["versions"] = row["versions"] or []
        row["generated"] = bool(row["versions"])
    return feature_collection(rows, id_key="plz")


def region_detail(plz: int) -> dict[str, Any] | None:
    """Everything the UI shows about one PLZ."""
    with db.cursor() as cur:
        cur.execute(
            """SELECT p.plz, p.note, p.qkm, p.population,
                      ST_XMin(b) AS minx, ST_YMin(b) AS miny, ST_XMax(b) AS maxx, ST_YMax(b) AS maxy
               FROM pylovo.postcode p, LATERAL (SELECT ST_Transform(p.geom, 4326) AS b) t WHERE p.plz = %s""", (plz,))
        postcode = cur.fetchone()
        cur.execute("SELECT plz, ags, name_city, pop, area, pop_den, regio7, fed_state FROM pylovo.municipal_register "
                    "WHERE plz = %s ORDER BY pop DESC NULLS LAST", (plz,))
        municipalities = [dict(r, ags=ags_text(r["ags"]), area=num(r["area"], 2), pop_den=num(r["pop_den"], 1))
                          for r in cur.fetchall()]
        if not postcode and not municipalities:
            return None
        detail: dict[str, Any] = {"plz": plz, "available": bool(postcode), "municipalities": municipalities}
        if postcode:
            detail.update(note=postcode["note"], qkm=num(postcode["qkm"], 3), population=postcode["population"],
                          bounds=[num(postcode[k], 6) for k in ("minx", "miny", "maxx", "maxy")])
            cur.execute(
                f"""SELECT {TRAFO_SOURCE_SQL} AS source, count(*) AS n,
                           count(t.transformer_rated_power) AS with_capacity
                    FROM pylovo.transformers t JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
                    WHERE p.plz = %(plz)s GROUP BY 1""", {"plz": plz})
            detail["transformers"] = {r["source"]: {"count": r["n"], "with_capacity": r["with_capacity"]}
                                      for r in cur.fetchall()}
            cur.execute(
                """SELECT pr.version_id, v.version_comment, pr.settlement_type, pr.house_distance,
                          pr.avg_households_per_building,
                          (SELECT count(*) FROM pylovo.grid_result g WHERE g.version_id = pr.version_id AND g.plz = pr.postcode_result_plz) AS grids,
                          EXISTS (SELECT 1 FROM pylovo.plz_parameters pp WHERE pp.version_id = pr.version_id AND pp.plz = pr.postcode_result_plz) AS analysed
                   FROM pylovo.postcode_result pr JOIN pylovo.version v USING (version_id)
                   WHERE pr.postcode_result_plz = %s ORDER BY v.created_at""", (plz,))
            detail["versions"] = [dict(r, house_distance=num(r["house_distance"], 1),
                                       avg_households_per_building=num(r["avg_households_per_building"], 2))
                                  for r in cur.fetchall()]
            # InfDB building counts come from the region gate (routers/regions.py): a count by
            # postcode here would be a full scan of basedata.buildings on a real InfDB.
    return detail


def ags_regions(ags: int) -> list[dict]:
    rows = db.fetch_all(
        """SELECT mr.plz, mr.name_city, mr.pop, (p.plz IS NOT NULL) AS available
           FROM pylovo.municipal_register mr LEFT JOIN pylovo.postcode p ON p.plz = mr.plz
           WHERE mr.ags = %s ORDER BY mr.plz""", (ags,))
    return rows


# --------------------------------------------------------------------------- transformers
def transformers(plz: int | None = None, bbox: dict | None = None) -> dict:
    """Transformer candidates of a PLZ (spatial intersection) or of a bounding box as GeoJSON."""
    params: dict[str, Any] = {}
    if plz is not None:
        join = "JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)"
        where = "p.plz = %(plz)s"
        params["plz"] = plz
    elif bbox is not None:
        join, where = "", envelope_sql("t.geom")
        params.update(bbox)
    else:
        raise ValueError("plz or bbox required")
    rows = db.fetch_all(
        f"""SELECT t.osm_id, t.type, t.transformer_rated_power, t.geom_type, t.within_shopping, t.osm, t.lod2,
                   t.lod2_objectid, round(t.area::numeric, 1)::float AS area, {TRAFO_SOURCE_SQL} AS source,
                   (SELECT count(*) FROM pylovo.transformer_positions tp WHERE tp.osm_id = t.osm_id) AS used_by_grids,
                   (SELECT array_agg(DISTINCT tp.version_id) FROM pylovo.transformer_positions tp
                     WHERE tp.osm_id = t.osm_id) AS used_in_versions,
                   ST_AsGeoJSON(ST_Transform(ST_Centroid(t.geom), 4326), 6)::json AS geometry
            FROM pylovo.transformers t {join} WHERE {where} ORDER BY t.osm_id LIMIT 5000""", params)
    for row in rows:
        row["used_in_versions"] = row["used_in_versions"] or []
    return feature_collection(rows, id_key=None)


def transformer_usage(osm_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        """SELECT t.osm_id, t.transformer_rated_power,
                  (SELECT count(*) FROM pylovo.transformer_positions tp WHERE tp.osm_id = t.osm_id) AS used_by_grids
           FROM pylovo.transformers t WHERE t.osm_id = %s""", (osm_id,))


# --------------------------------------------------------------------------- versions & results
def versions() -> list[dict]:
    rows = db.fetch_all(
        """SELECT v.version_id, v.version_comment, v.created_at, v.generation_parameters IS NOT NULL AS has_parameters,
                  (SELECT count(*) FROM pylovo.postcode_result pr WHERE pr.version_id = v.version_id) AS plz_count,
                  (SELECT array_agg(pr.postcode_result_plz ORDER BY pr.postcode_result_plz)
                     FROM pylovo.postcode_result pr WHERE pr.version_id = v.version_id) AS plz,
                  (SELECT count(*) FROM pylovo.grid_result g WHERE g.version_id = v.version_id) AS grid_count,
                  (SELECT count(*) FROM pylovo.plz_parameters pp WHERE pp.version_id = v.version_id) AS analysed_plz,
                  (SELECT count(*) FROM pylovo.clustering_parameters cp JOIN pylovo.grid_result g USING (grid_result_id)
                    WHERE g.version_id = v.version_id) AS clustering_rows
           FROM pylovo.version v ORDER BY v.created_at, v.version_id""")
    for row in rows:
        row["plz"] = row["plz"] or []
    from pylovo_api.preflight import attach_version_flags

    return attach_version_flags(rows)  # badges from generation_parameters (greenfield, sizes, …)


def version_parameters(version_id: str) -> dict[str, Any] | None:
    row = db.fetch_one("SELECT version_id, version_comment, created_at, generation_parameters FROM pylovo.version "
                       "WHERE version_id = %s", (version_id,))
    return row


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, sub in value.items():
            out.update(_flatten(sub, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list) and value and all(isinstance(v, dict) and "name" in v for v in value):
        for item in value:
            out.update(_flatten({k: v for k, v in item.items() if k != "name"}, f"{prefix}[{item['name']}]"))
    else:
        out[prefix] = value
    return out


def compare_parameters(a: str, b: str) -> dict[str, Any]:
    """Flattened generation parameters of two versions and the keys that differ."""
    pa, pb = version_parameters(a), version_parameters(b)
    if not pa or not pb:
        raise KeyError("version not found")
    fa, fb = _flatten(pa["generation_parameters"] or {}), _flatten(pb["generation_parameters"] or {})
    keys = sorted(set(fa) | set(fb))
    rows = [{"key": k, "a": fa.get(k), "b": fb.get(k), "differs": fa.get(k) != fb.get(k)} for k in keys]
    return {"a": a, "b": b, "rows": rows, "differences": sum(r["differs"] for r in rows)}


def _grid_rows(version_id: str, plz: list[int] | None) -> list[dict]:
    params: dict[str, Any] = {"v": version_id}
    where = "g.version_id = %(v)s"
    if plz:
        where += " AND g.plz = ANY(%(plz)s)"
        params["plz"] = plz
    return db.fetch_all(
        f"""
        WITH grids AS (SELECT g.* FROM pylovo.grid_result g WHERE {where}),
        b AS (SELECT br.grid_result_id, count(*) AS buildings, sum(br.households) AS households,
                     sum(br.peak_load_in_kw) AS installed_kw
              FROM pylovo.buildings_result br WHERE br.grid_result_id IN (SELECT grid_result_id FROM grids)
              GROUP BY 1),
        ld AS (SELECT l.grid_result_id, count(*) AS loads, sum(l.p_mw) AS p_mw,
                      sum(sqrt(l.p_mw ^ 2 + COALESCE(l.q_mvar, 0) ^ 2)) AS s_mva
               FROM pylovo.pandapower_load l WHERE l.grid_result_id IN (SELECT grid_result_id FROM grids) GROUP BY 1),
        tr AS (SELECT t.grid_result_id, max(t.sn_mva) AS unit_mva, sum(COALESCE(t.parallel, 1)) AS units
               FROM pylovo.pandapower_trafo t WHERE t.grid_result_id IN (SELECT grid_result_id FROM grids) GROUP BY 1),
        ln AS (SELECT l.grid_result_id,
                      sum(l.length_km) FILTER (WHERE {LINE_ROLE_SQL} = 'feeder') AS feeder_km,
                      sum(l.length_km) FILTER (WHERE {LINE_ROLE_SQL} = 'service') AS service_km,
                      count(*) AS lines
               FROM pylovo.pandapower_line l WHERE l.grid_result_id IN (SELECT grid_result_id FROM grids) GROUP BY 1),
        bs AS (SELECT grid_result_id, count(*) AS buses FROM pylovo.pandapower_bus
               WHERE grid_result_id IN (SELECT grid_result_id FROM grids) GROUP BY 1),
        st AS (SELECT b.grid_result_id, b.pp_index FROM pylovo.pandapower_bus b
               WHERE b.name LIKE 'LVbus%%' AND b.grid_result_id IN (SELECT grid_result_id FROM grids)
               UNION
               SELECT l.grid_result_id, CASE WHEN l.from_bus = b.pp_index THEN l.to_bus ELSE l.from_bus END
               FROM pylovo.pandapower_line l
               JOIN pylovo.pandapower_bus b ON b.grid_result_id = l.grid_result_id AND b.name LIKE 'LVbus%%'
                    AND b.pp_index IN (l.from_bus, l.to_bus)
               WHERE {LINE_ROLE_SQL} = 'link' AND l.grid_result_id IN (SELECT grid_result_id FROM grids)),
        fd AS (SELECT l.grid_result_id,  -- a service cable at the station is a direct connection, not a feeder
                      count(DISTINCT l.pp_index) FILTER (WHERE {LINE_ROLE_SQL} = 'feeder') AS feeders,
                      count(DISTINCT l.pp_index) FILTER (WHERE {LINE_ROLE_SQL} = 'service') AS direct_connections
               FROM pylovo.pandapower_line l JOIN st ON st.grid_result_id = l.grid_result_id
                    AND st.pp_index IN (l.from_bus, l.to_bus)
               WHERE {LINE_ROLE_SQL} <> 'link' AND l.grid_result_id IN (SELECT grid_result_id FROM grids) GROUP BY 1)
        SELECT g.grid_result_id, g.version_id, g.plz, g.kcid, g.bcid, g.transformer_rated_power AS kva,
               g.transformer_description AS description, g.power_flow_status, g.model_status,
               g.max_total_lv_voltage_drop_pu, g.max_feeder_voltage_drop_pu, g.max_service_voltage_drop_pu,
               g.feeder_voltage_drop_limit_met, g.service_voltage_drop_limit_met,
               g.selected_max_feeder_voltage_drop_percent, g.selected_max_service_voltage_drop_percent,
               g.max_total_design_voltage_drop_percent, g.service_voltage_upgraded_count,
               g.long_service_connection_count,
               tp.osm_id AS station_osm_id, tp.comment AS station_comment, tp.osm AS station_osm, tp.lod2 AS station_lod2,
               ST_X(ST_Transform(tp.geom, 4326)) AS lon, ST_Y(ST_Transform(tp.geom, 4326)) AS lat,
               b.buildings, b.households, b.installed_kw, ld.loads, ld.p_mw, ld.s_mva, tr.unit_mva, tr.units,
               ln.feeder_km, ln.service_km, ln.lines, bs.buses, fd.feeders, fd.direct_connections,
               cp.no_branches, cp.max_trafo_dis, cp.avg_trafo_dis, cp.no_house_connections, cp.cable_len_per_house
        FROM grids g
        LEFT JOIN pylovo.transformer_positions tp ON tp.grid_result_id = g.grid_result_id
        LEFT JOIN b ON b.grid_result_id = g.grid_result_id
        LEFT JOIN ld ON ld.grid_result_id = g.grid_result_id
        LEFT JOIN tr ON tr.grid_result_id = g.grid_result_id
        LEFT JOIN ln ON ln.grid_result_id = g.grid_result_id
        LEFT JOIN bs ON bs.grid_result_id = g.grid_result_id
        LEFT JOIN fd ON fd.grid_result_id = g.grid_result_id
        LEFT JOIN pylovo.clustering_parameters cp ON cp.grid_result_id = g.grid_result_id
        ORDER BY g.plz, g.kcid, g.bcid""", params)


def size_label(unit_mva: Any, units: Any, kva: Any) -> str:
    """Station size: ``"630 kVA"`` or ``"2 × 400 kVA"`` for parallel transformer units."""
    unit_kva = round(float(unit_mva or 0) * 1000) or None
    units = int(units or 1)
    return f"{units} × {unit_kva} kVA" if units > 1 else f"{unit_kva or kva} kVA"


def _grid_record(row: dict) -> dict:
    kva = row["kva"]
    s_kva = (row["s_mva"] or 0) * 1000
    unit_kva = round((row["unit_mva"] or 0) * 1000) or None
    units = int(row["units"] or 1)
    station_source = ("osm" if row["station_osm"] else "lod2" if row["station_lod2"] else
                      "open" if row["station_osm_id"] else "greenfield")
    if row["station_osm_id"] and str(row["station_osm_id"]).startswith("dso"):
        station_source = "dso"
    elif row["station_osm_id"] and str(row["station_osm_id"]).startswith("manual/"):
        station_source = "manual"
    return {
        "grid_result_id": row["grid_result_id"], "version_id": row["version_id"], "plz": row["plz"],
        "kcid": row["kcid"], "bcid": row["bcid"], "kva": kva, "unit_kva": unit_kva, "units": units,
        "size_label": size_label(row["unit_mva"], row["units"], kva),
        "description": row["description"], "power_flow_status": row["power_flow_status"] or "unknown",
        "max_total_drop_pct": num(row["max_total_lv_voltage_drop_pu"] and row["max_total_lv_voltage_drop_pu"] * 100, 2),
        "max_feeder_drop_pct": num(row["max_feeder_voltage_drop_pu"] and row["max_feeder_voltage_drop_pu"] * 100, 2),
        "max_service_drop_pct": num(row["max_service_voltage_drop_pu"] and row["max_service_voltage_drop_pu"] * 100, 2),
        "design_feeder_drop_pct": num(row["selected_max_feeder_voltage_drop_percent"], 2),
        "design_service_drop_pct": num(row["selected_max_service_voltage_drop_percent"], 2),
        "design_total_drop_pct": num(row["max_total_design_voltage_drop_percent"], 2),
        "feeder_limit_met": row["feeder_voltage_drop_limit_met"],
        "service_limit_met": row["service_voltage_drop_limit_met"],
        "service_upgrades": row["service_voltage_upgraded_count"],
        "long_services": row["long_service_connection_count"],
        "station_osm_id": row["station_osm_id"], "station_comment": row["station_comment"],
        "station_source": station_source,
        "lon": num(row["lon"], 6), "lat": num(row["lat"], 6),
        "buildings": row["buildings"] or 0, "households": int(row["households"] or 0),
        "installed_kw": num(row["installed_kw"], 1), "loads": row["loads"] or 0,
        "coincident_kw": num((row["p_mw"] or 0) * 1000, 1), "coincident_kva": num(s_kva, 1),
        "utilisation": num(s_kva / kva, 3) if kva else None,
        "feeder_km": num(row["feeder_km"] or 0, 3), "service_km": num(row["service_km"] or 0, 3),
        "lines": row["lines"] or 0, "buses": row["buses"] or 0, "feeders": row["feeders"] or 0,
        "direct_connections": row["direct_connections"] or 0,
        "pylovo_branches": row["no_branches"],
        "max_trafo_dist_m": num(row["max_trafo_dis"] and row["max_trafo_dis"] * 1000, 0),
        "avg_trafo_dist_m": num(row["avg_trafo_dis"] and row["avg_trafo_dis"] * 1000, 0),
    }


def results_summary(version_id: str, plz: list[int] | None) -> dict[str, Any]:
    """KPIs and chart data for one version (optionally restricted to some PLZ)."""
    grids = [_grid_record(r) for r in _grid_rows(version_id, plz)]
    params: dict[str, Any] = {"v": version_id}
    plz_filter = ""
    if plz:
        plz_filter = " AND g.plz = ANY(%(plz)s)"
        params["plz"] = plz
    cables = db.fetch_all(
        f"""SELECT l.std_type, {LINE_ROLE_SQL} AS role, count(*) AS n, sum(l.length_km) AS km,
                   sum(l.length_km * COALESCE(l.parallel, 1)) AS conductor_km
            FROM pylovo.pandapower_line l JOIN pylovo.grid_result g ON g.grid_result_id = l.grid_result_id
            WHERE g.version_id = %(v)s{plz_filter} GROUP BY 1, 2 ORDER BY 2, 1""", params)
    cables = [dict(c, km=num(c["km"], 3), conductor_km=num(c["conductor_km"], 3)) for c in cables if c["role"] != "link"]
    plz_rows = db.fetch_all(
        f"""SELECT pr.postcode_result_plz AS plz, pr.settlement_type, pr.house_distance, pr.avg_households_per_building,
                   pp.trafo_num, pp.cable_length, pp.load_count_per_trafo, pp.bus_count_per_trafo,
                   pp.sim_peak_load_per_trafo, pp.max_distance_per_trafo, pp.avg_distance_per_trafo
            FROM pylovo.postcode_result pr
            LEFT JOIN pylovo.plz_parameters pp ON pp.version_id = pr.version_id AND pp.plz = pr.postcode_result_plz
            WHERE pr.version_id = %(v)s{plz_filter.replace('g.plz', 'pr.postcode_result_plz')}
            ORDER BY 1""", params)
    version = version_parameters(version_id)
    gp = (version or {}).get("generation_parameters") or {}
    cable_params = gp.get("cable_dimensioning") or {}
    pf_params = gp.get("power_flow_assessment") or {}

    sizes: dict[str, dict[str, Any]] = {}
    for g in grids:
        entry = sizes.setdefault(g["size_label"], {"label": g["size_label"], "kva": g["kva"] or 0, "count": 0})
        entry["count"] += 1
    status_counts: dict[str, int] = defaultdict(int)
    for g in grids:
        status_counts[g["power_flow_status"]] += 1
    distances = {"max": [g["max_trafo_dist_m"] for g in grids if g["max_trafo_dist_m"] is not None],
                 "avg": [g["avg_trafo_dist_m"] for g in grids if g["avg_trafo_dist_m"] is not None]}
    if not distances["max"]:
        for row in plz_rows:
            for key, target in (("max_distance_per_trafo", "max"), ("avg_distance_per_trafo", "avg")):
                for values in (row.get(key) or {}).values():
                    distances[target] += [round(v) for v in values]
    total_kva = sum(g["kva"] or 0 for g in grids)
    kpis = {
        "grids": len(grids), "plz": len({g["plz"] for g in grids}),
        "transformer_units": sum(g["units"] for g in grids), "total_kva": total_kva,
        "buildings": sum(g["buildings"] for g in grids), "households": sum(g["households"] for g in grids),
        "loads": sum(g["loads"] for g in grids),
        "installed_kw": num(sum(g["installed_kw"] or 0 for g in grids), 0),
        "coincident_kw": num(sum(g["coincident_kw"] or 0 for g in grids), 0),
        "feeder_km": num(sum(g["feeder_km"] for g in grids), 2),
        "service_km": num(sum(g["service_km"] for g in grids), 2),
        "feeders": sum(g["feeders"] for g in grids),
        "converged": status_counts.get("converged", 0),
        "max_total_drop_pct": max((g["max_total_drop_pct"] for g in grids if g["max_total_drop_pct"] is not None),
                                  default=None),
        "mean_utilisation": num(sum(g["coincident_kva"] or 0 for g in grids) / total_kva, 3) if total_kva else None,
        "limits_met": sum(1 for g in grids if g["feeder_limit_met"] and g["service_limit_met"] is not False),
    }
    limits = {
        "feeder_drop_pct": cable_params.get("max_end_to_end_feeder_voltage_drop_percent"),
        "service_drop_pct": cable_params.get("max_service_design_voltage_drop_percent"),
        "min_vm_pu": pf_params.get("min_vm_pu"), "max_vm_pu": pf_params.get("max_vm_pu"),
        "planning_utilisation": (gp.get("transformer_placement") or {}).get("transformer_planning_utilization"),
    }
    kpis.update(grid_metrics.extend_records(grids, limits))  # stored validation power flow, design figures
    return {
        "version_id": version_id,
        "version_comment": (version or {}).get("version_comment"),
        "plz": sorted({g["plz"] for g in grids}) or (plz or []),
        "kpis": kpis,
        "grids": grids,
        "transformer_sizes": sorted(sizes.values(), key=lambda s: (s["kva"], s["label"])),
        "cables": cables,
        "power_flow_status": [{"status": k, "count": v} for k, v in sorted(status_counts.items())],
        "distances_m": distances,
        "plz_rows": [dict(r, house_distance=num(r["house_distance"], 1),
                          avg_households_per_building=num(r["avg_households_per_building"], 2)) for r in plz_rows],
        "limits": limits,
        "analysed": any(g["pylovo_branches"] is not None for g in grids),
    }


def overview_geojson(version_id: str, plz: int, offsets: bool = True) -> dict[str, Any]:
    """All grids of one PLZ: cables (with grid id), stations and building footprints."""
    params = {"v": version_id, "plz": plz}
    lines = db.fetch_all(_line_sql("g.version_id = %(v)s AND g.plz = %(plz)s", offsets), params)
    stations = db.fetch_all(
        """SELECT g.grid_result_id, g.kcid, g.bcid, g.transformer_rated_power AS kva, g.power_flow_status,
                  g.transformer_description AS description, tp.osm_id, tp.comment,
                  (SELECT max(t.sn_mva) FROM pylovo.pandapower_trafo t WHERE t.grid_result_id = g.grid_result_id) AS unit_mva,
                  (SELECT sum(COALESCE(t.parallel, 1)) FROM pylovo.pandapower_trafo t WHERE t.grid_result_id = g.grid_result_id) AS units,
                  ST_AsGeoJSON(ST_Transform(tp.geom, 4326), 6)::json AS geometry
           FROM pylovo.grid_result g JOIN pylovo.transformer_positions tp ON tp.grid_result_id = g.grid_result_id
           WHERE g.version_id = %(v)s AND g.plz = %(plz)s ORDER BY g.kcid, g.bcid""", params)
    for st in stations:  # "2 × 400 kVA" for a station with parallel units, as everywhere else
        st["size_label"] = size_label(st.pop("unit_mva"), st.pop("units"), st["kva"])
    buildings = db.fetch_all(
        """SELECT br.grid_result_id, br.objectid, br.type, br.households, round(br.peak_load_in_kw::numeric, 1)::float AS peak_kw,
                  br.street, br.house_number, br.building_use, round(br.height::numeric, 1)::float AS height,
                  ST_AsGeoJSON(ST_Transform(br.geom, 4326), 6)::json AS geometry
           FROM pylovo.buildings_result br JOIN pylovo.grid_result g ON g.grid_result_id = br.grid_result_id
           WHERE g.version_id = %(v)s AND g.plz = %(plz)s""", params)
    bounds = db.fetch_one(
        """SELECT ST_XMin(e) minx, ST_YMin(e) miny, ST_XMax(e) maxx, ST_YMax(e) maxy FROM (
             SELECT ST_Transform(ST_SetSRID(ST_Extent(l.geom)::geometry, %(epsg)s), 4326) e
             FROM pylovo.lines_result l JOIN pylovo.grid_result g ON g.grid_result_id = l.grid_result_id
             WHERE g.version_id = %(v)s AND g.plz = %(plz)s) s""", dict(params, epsg=epsg()))
    # cable distribution cabinets: pylovo's feeder split points (the station itself is excluded)
    splits = db.fetch_all(
        """SELECT sp.grid_result_id, sp.split_bus, sp.outgoing_count, sp.split_type,
                  ST_AsGeoJSON(ST_Transform(sp.geom, 4326), 6)::json AS geometry
           FROM pylovo.split_points sp JOIN pylovo.grid_result g ON g.grid_result_id = sp.grid_result_id
           WHERE g.version_id = %(v)s AND g.plz = %(plz)s""", params)
    return {
        "lines": feature_collection(lines),
        "stations": feature_collection(stations, id_key="grid_result_id"),
        "buildings": feature_collection(buildings),
        "splits": feature_collection(splits),
        "bounds": [num(bounds[k], 6) for k in ("minx", "miny", "maxx", "maxy")] if bounds and bounds["minx"] else None,
    }


def _line_sql(where: str, offsets: bool) -> str:
    geometry = ("COALESCE(ST_Transform(COALESCE(h.geom, lr.geom), 4326), ST_GeomFromGeoJSON(l.geo::text))"
                if offsets else "COALESCE(ST_Transform(lr.geom, 4326), ST_GeomFromGeoJSON(l.geo::text))")
    helper_join = """LEFT JOIN LATERAL (SELECT v.geom FROM pylovo.lines_result_view v
                         WHERE v.source_lines_result_id = lr.lines_result_id AND v.is_helper LIMIT 1) h ON true""" \
        if offsets else ""
    return f"""
        SELECT l.grid_result_id, l.pp_index, l.name, l.std_type, l.from_bus, l.to_bus,
               round((l.length_km * 1000)::numeric, 1)::float AS length_m, COALESCE(l.parallel, 1) AS parallel,
               l.max_i_ka, l.feeder_section_id, l.feeder_sizing_basis, l.service_sizing_basis,
               round(l.service_selected_voltage_drop_percent::numeric, 2)::float AS service_drop_pct,
               round(l.total_design_voltage_drop_percent::numeric, 2)::float AS design_drop_pct,
               {LINE_ROLE_SQL} AS role, {'(h.geom IS NOT NULL)' if offsets else 'false'} AS offset_drawn,
               ST_AsGeoJSON({geometry}, 6)::json AS geometry
        FROM pylovo.pandapower_line l
        JOIN pylovo.grid_result g ON g.grid_result_id = l.grid_result_id
        LEFT JOIN pylovo.lines_result lr ON lr.grid_result_id = l.grid_result_id
             AND lr.line_name = regexp_replace(l.name, '^Line to ', 'L')
        {helper_join}
        WHERE {where}
        ORDER BY l.grid_result_id, l.pp_index"""


def grid_detail(grid_result_id: int, offsets: bool = True) -> dict[str, Any] | None:
    """One grid: KPIs, GeoJSON layers (cables, buses, buildings, station) and tables."""
    head = db.fetch_one("SELECT version_id, plz FROM pylovo.grid_result WHERE grid_result_id = %s", (grid_result_id,))
    if not head:
        return None
    record = next((_grid_record(r) for r in _grid_rows(head["version_id"], [head["plz"]])
                   if r["grid_result_id"] == grid_result_id), None)
    params = {"g": grid_result_id}
    lines = db.fetch_all(_line_sql("l.grid_result_id = %(g)s", offsets), params)
    buses = db.fetch_all(
        """SELECT b.pp_index, b.name, b.vn_kv, b.zone,
                  ld.loads, ld.p_kw, ld.design_kw, ld.installed_kw, ld.load_units, ld.category,
                  ST_AsGeoJSON(ST_GeomFromGeoJSON(b.geo::text), 6)::json AS geometry
           FROM pylovo.pandapower_bus b
           LEFT JOIN (SELECT bus, count(*) AS loads, round(sum(p_mw * 1000)::numeric, 2)::float AS p_kw,
                             round(sum(service_design_p_mw * 1000)::numeric, 2)::float AS design_kw,
                             round(sum(max_p_mw * 1000)::numeric, 2)::float AS installed_kw,
                             sum(load_units) AS load_units, string_agg(DISTINCT category, ', ') AS category
                      FROM pylovo.pandapower_load WHERE grid_result_id = %(g)s GROUP BY bus) ld ON ld.bus = b.pp_index
           WHERE b.grid_result_id = %(g)s ORDER BY b.pp_index""", params)
    buildings = db.fetch_all(
        """SELECT br.objectid, br.type, br.households, round(br.peak_load_in_kw::numeric, 1)::float AS peak_kw,
                  br.street, br.house_number, br.building_use, round(br.height::numeric, 1)::float AS height,
                  br.floor_number, round(br.floor_area::numeric, 0)::float AS floor_area, br.vertice_id,
                  ST_AsGeoJSON(ST_Transform(br.geom, 4326), 6)::json AS geometry
           FROM pylovo.buildings_result br WHERE br.grid_result_id = %(g)s""", params)
    trafo = db.fetch_all("SELECT pp_index, name, std_type, sn_mva, vn_hv_kv, vn_lv_kv, vk_percent, vkr_percent, "
                         "parallel, hv_bus, lv_bus FROM pylovo.pandapower_trafo WHERE grid_result_id = %(g)s", params)
    station = db.fetch_one(
        """SELECT tp.osm_id, tp.comment, tp.osm, tp.lod2, ST_AsGeoJSON(ST_Transform(tp.geom, 4326), 6)::json AS geometry
           FROM pylovo.transformer_positions tp WHERE tp.grid_result_id = %(g)s""", params)
    splits = db.fetch_all(
        """SELECT split_bus, outgoing_count, split_type, ST_AsGeoJSON(ST_Transform(geom, 4326), 6)::json AS geometry
           FROM pylovo.split_points WHERE grid_result_id = %(g)s""", params)

    topo = topology.analyse(buses, [{**ln, "length_km": (ln["length_m"] or 0) / 1000} for ln in lines])
    by_vertex = {}
    for building in buildings:
        if building.get("vertice_id") is not None:
            by_vertex.setdefault(int(building["vertice_id"]), []).append(building)
    for bus in buses:
        bus["role"] = topology.bus_role(bus["name"])
        bus["feeder"] = topo["bus_feeder"].get(bus["pp_index"])
        bus["distance_m"] = num(topo["distance_km"].get(bus["pp_index"], 0) * 1000, 1) \
            if bus["pp_index"] in topo["distance_km"] else None
        if bus["role"] == "consumer":
            match = re.search(r"(\d+)$", bus["name"] or "")
            linked = by_vertex.get(int(match.group(1)), []) if match else []
            bus["households"] = sum(b["households"] or 0 for b in linked) or None
            bus["address"] = ", ".join(f"{b['street']} {b['house_number'] or ''}".strip() for b in linked if b["street"]) or None
            bus["building_type"] = ", ".join(sorted({b["type"] for b in linked if b["type"]})) or None
    for line in lines:
        line["feeder"] = topo["line_feeder"].get(line["pp_index"])
    # K1…Kn: the cabinet names of the diagnostics as well (pylovo_api.cabinets)
    cabinet_list = cabinets.number_cabinets(splits, cabinets.connection_vertices(buses), set(topo["station"]),
                                            {b: d * 1000 for b, d in topo["distance_km"].items()})
    detail = {
        "grid": record,
        "feeders": topo["feeders"],
        "station_buses": topo["station"],
        "direct_connections": topo["direct"],
        "lines": feature_collection(lines),
        "buses": feature_collection(buses),
        "buildings": feature_collection(buildings),
        "splits": feature_collection(splits),
        "cabinets": cabinet_list,
        "station": feature_collection([station]) if station else feature_collection([]),
        "trafo": trafo,
    }
    gp = (version_parameters(head["version_id"]) or {}).get("generation_parameters")
    detail.update(grid_metrics.grid_extras(detail, record, grid_metrics.version_limits(gp)))
    return detail


def grid_json(grid_result_id: int) -> tuple[dict, dict] | None:
    """The stored pandapower JSON of one grid and its identifiers."""
    row = db.fetch_one("SELECT version_id, plz, kcid, bcid, grid FROM pylovo.grid_result WHERE grid_result_id = %s",
                       (grid_result_id,))
    if not row or row["grid"] is None:
        return None
    grid = row.pop("grid")
    return grid, row
