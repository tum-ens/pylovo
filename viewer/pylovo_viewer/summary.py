"""Key figures of the selection: LV grids of a version or region, and an MV run or supply area."""
from __future__ import annotations

from typing import Any

from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database
from pylovo_viewer.layers import REGION_GRIDS, Query, num


def lv(db: Database, q: Query) -> dict[str, Any]:
    """Grids, buildings, load, cable lengths and stations by size."""
    region = q.region()
    grids = REGION_GRIDS[region] if region else \
        "SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(version)s"
    params = q.params()
    head = db.one(f"""
        WITH g AS ({grids})
        SELECT (SELECT count(*) FROM g) AS grids,
               (SELECT count(*) FROM pylovo.buildings_result b WHERE b.grid_result_id IN (SELECT * FROM g)) AS buildings,
               (SELECT sum(b.households) FROM pylovo.buildings_result b
                 WHERE b.grid_result_id IN (SELECT * FROM g)) AS households,
               (SELECT {num("sum(b.peak_load_in_kw) / 1000", 2)} FROM pylovo.buildings_result b
                 WHERE b.grid_result_id IN (SELECT * FROM g)) AS peak_mw,
               (SELECT {num("sum(c.length_km * COALESCE(c.parallel, 1))", 1)} FROM pylovo.lines_result_cache c
                 WHERE c.grid_result_id IN (SELECT * FROM g) AND c.feeder_section_id IS NOT NULL) AS feeder_km,
               (SELECT {num("sum(c.length_km * COALESCE(c.parallel, 1))", 1)} FROM pylovo.lines_result_cache c
                 WHERE c.grid_result_id IN (SELECT * FROM g) AND c.feeder_section_id IS NULL) AS service_km""", params)
    sizes = db.query(f"""SELECT transformer_rated_power AS kva, count(*) AS n FROM pylovo.grid_result
                         WHERE grid_result_id IN ({grids}) GROUP BY 1 ORDER BY 1""", params)
    return {**(head or {}), "stations_by_kva": sizes}


def mv(db: Database, catalog: Catalog, q: Query) -> dict[str, Any] | None:
    """Supply areas, rings, stations, circuit length by kind, ties, links and open points."""
    if q.mv is None or not catalog.has("pylovo.mv_grid"):
        return None
    area = "AND mv_grid_id = %(area)s" if q.area is not None else ""
    params = q.params()
    head = db.one(f"""
        SELECT count(*) AS areas, sum(n_stations) AS stations, sum(n_customers) AS customers,
               sum(n_generators) AS generators, sum(n_rings) AS rings, {num("sum(peak_load_mw)", 1)} AS peak_mw,
               count(*) FILTER (WHERE vn_kv = 10) AS areas_10kv, count(*) FILTER (WHERE vn_kv = 20) AS areas_20kv,
               count(*) FILTER (WHERE NOT complete) AS incomplete,
               {num("max(max_line_loading_percent)", 1)} AS max_loading,
               {num("max(max_voltage_drop_percent)", 2)} AS max_drop
        FROM pylovo.mv_grid WHERE version_id = %(version)s AND mv_variant_id = %(mv)s {area}""", params)
    lines = db.one(f"""
        SELECT {num("sum(cable_km * COALESCE(parallel, 1)) FILTER (WHERE role NOT IN ('tie', 'link'))", 1)} AS cable_km,
               {num("sum(overhead_km * COALESCE(parallel, 1)) FILTER (WHERE role NOT IN ('tie', 'link'))", 1)}
                 AS overhead_km,
               count(*) FILTER (WHERE role = 'tie') AS ties,
               {num("sum(length_km) FILTER (WHERE role = 'tie')", 1)} AS tie_km,
               count(*) FILTER (WHERE role = 'link') AS links,
               {num("sum(length_km) FILTER (WHERE role = 'link')", 1)} AS link_km,
               count(*) FILTER (WHERE normally_open AND role NOT IN ('tie', 'link')) AS open_points
        FROM pylovo.mv_line WHERE version_id = %(version)s AND mv_variant_id = %(mv)s {area}""", params)
    return {**(head or {}), **(lines or {})}
