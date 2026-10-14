"""Import generating units of the Marktstammdatenregister (MaStR) for the MV stage.

The MaStR bulk export is downloaded and parsed with the open-mastr package (a separate tool,
not a pylovo dependency), which writes an SQLite database (default
``~/.open-MaStR/data/sqlite/open-mastr.db``)::

    uv run --no-project --with open-mastr python -c "from open_mastr import Mastr; \
        Mastr().download(data=['wind', 'solar', 'biomass', 'hydro', 'gsgk', 'combustion', 'storage', 'grid', 'location'])"

This module reads that SQLite file (read-only) and fills two reference tables:

* ``pylovo.mastr_units``: units in operation with coordinates inside the bounding box of
  ``pylovo.postcode`` (plus a margin), with technology, net power and the connection voltage
  level (``Spannungsebene`` of the grid connection point, if registered).
* ``pylovo.mastr_postcode_aggregates``: units in operation without coordinates (small units,
  usually LV-connected rooftop PV), summed per postcode and technology.

Both tables are replaced by each import. Catalogue fields may be stored as cleansed text
("In Betrieb") or as raw MaStR codes ("35"); both are understood.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from psycopg2.extras import execute_values

import pylovo.database.database_client as dbc
from pylovo.config_loader import TARGET_EPSG

DEFAULT_SQLITE_PATH = Path.home() / ".open-MaStR" / "data" / "sqlite" / "open-mastr.db"

# technology label -> open-mastr view or table with the unit records
UNIT_SOURCES = {
    "wind": "wind_extended",
    "solar": "solar_extended",
    "biomass": "biomass_extended",
    "hydro": "hydro_extended",
    "gsgk": "gsgk_extended",
    "combustion": "combustion_extended",
    "storage": "storage_extended",
}
IN_OPERATION = {"In Betrieb", "35"}
# ArtDerSolaranlage: cleansed text or MaStR catalogue code
SOLAR_KINDS = {
    "852": "ground", "Freiflächenanlage": "ground",
    "853": "building", "Bauliche Anlagen (Hausdach, Gebäude und Fassade)": "building",
    "2961": "plug_in", "Steckerfertige Solaranlage (sog. Balkonkraftwerk)": "plug_in",
    "2484": "water", "Gewässer": "water",
}
UNIT_COLUMNS = ("mastr_id", "location_id", "technology", "solar_kind", "status", "net_power_kw",
                "gross_power_kw", "voltage_level", "commissioning_date", "gemeindeschluessel", "postcode",
                "lon", "lat")


def _float(value) -> float | None:
    try:
        return None if value in (None, "") else float(value)
    except (TypeError, ValueError):
        return None


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in con.execute(f"PRAGMA table_info({table})")}


def _study_bbox(margin_m: float) -> tuple[float, float, float, float]:
    """Return ``(lon_min, lat_min, lon_max, lat_max)`` of ``pylovo.postcode`` buffered by ``margin_m``."""
    with dbc.DatabaseClient() as client:
        client.cur.execute(
            """SELECT ST_XMin(b), ST_YMin(b), ST_XMax(b), ST_YMax(b)
               FROM (SELECT ST_Transform(ST_SetSRID(ST_Expand(ST_Extent(geom)::geometry, %(m)s), %(srid)s), 4326) AS b
                     FROM pylovo.postcode) e""",
            {"m": margin_m, "srid": int(TARGET_EPSG)},
        )
        bbox = client.cur.fetchone()
    if bbox is None or bbox[0] is None:
        raise ValueError("pylovo.postcode is empty; run pylovo-setup first")
    return bbox


def read_units(sqlite_path: Path, bbox: tuple[float, float, float, float]) -> tuple[list[dict], list[dict]]:
    """Read units in operation from the open-mastr SQLite file.

    Args:
        sqlite_path: open-mastr SQLite database.
        bbox: ``(lon_min, lat_min, lon_max, lat_max)`` in EPSG:4326 for units with coordinates.

    Returns:
        ``(units, aggregates)``: units with coordinates inside ``bbox`` (dicts with the keys of
        ``UNIT_COLUMNS``) and per-``(postcode, technology)`` sums of units without coordinates.
    """
    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    units, aggregates = [], {}
    lon_min, lat_min, lon_max, lat_max = bbox
    for technology, table in UNIT_SOURCES.items():
        cols = _columns(con, table)
        if not cols:
            continue
        solar_col = "ArtDerSolaranlage" if "ArtDerSolaranlage" in cols else None
        query = f"""SELECT EinheitMastrNummer, LokationMastrNummer, EinheitBetriebsstatus, Nettonennleistung,
                           Bruttoleistung, Inbetriebnahmedatum, Gemeindeschluessel, Postleitzahl,
                           Laengengrad, Breitengrad, {solar_col or 'NULL'}
                    FROM {table}"""
        for (mastr_id, location_id, status, net, gross, start, ags, postcode, lon, lat, solar) in con.execute(query):
            if str(status) not in IN_OPERATION:
                continue
            lon, lat = _float(lon), _float(lat)
            net_kw = _float(net)
            if lon is None or lat is None:
                key = (str(postcode or ""), technology)
                n, kw = aggregates.get(key, (0, 0.0))
                aggregates[key] = (n + 1, kw + (net_kw or 0.0))
                continue
            if not (lon_min <= lon <= lon_max and lat_min <= lat <= lat_max):
                continue
            units.append({
                "mastr_id": mastr_id, "location_id": location_id, "technology": technology,
                "solar_kind": SOLAR_KINDS.get(str(solar)) if solar is not None else None,
                "status": "in_operation", "net_power_kw": net_kw, "gross_power_kw": _float(gross),
                "voltage_level": None, "commissioning_date": (str(start)[:10] if start else None),
                "gemeindeschluessel": ags, "postcode": postcode, "lon": lon, "lat": lat,
            })
    _attach_voltage_levels(con, units)
    con.close()
    agg_rows = [{"postcode": pc, "technology": tech, "n_units": n, "net_power_kw": kw}
                for (pc, tech), (n, kw) in aggregates.items() if pc]
    return units, agg_rows


def _attach_voltage_levels(con: sqlite3.Connection, units: list[dict]) -> None:
    """Set ``voltage_level`` from the grid connection points (``grid_connections.Spannungsebene``)."""
    if not units or not _columns(con, "grid_connections"):
        return
    con.execute("CREATE TEMP TABLE IF NOT EXISTS wanted_locations (loc TEXT PRIMARY KEY)")
    con.execute("DELETE FROM wanted_locations")
    con.executemany("INSERT OR IGNORE INTO wanted_locations VALUES (?)",
                    [(u["location_id"],) for u in units if u["location_id"]])
    levels = {}
    for loc, level in con.execute(
            """SELECT g.LokationMastrNummer, g.Spannungsebene
               FROM grid_connections g JOIN wanted_locations w ON w.loc = g.LokationMastrNummer"""):
        if level and loc not in levels:
            levels[loc] = level
    for unit in units:
        unit["voltage_level"] = levels.get(unit["location_id"])


def import_mastr_units(sqlite_path: str | Path = DEFAULT_SQLITE_PATH, margin_m: float = 5000.0) -> tuple[int, int]:
    """Replace ``pylovo.mastr_units`` and ``pylovo.mastr_postcode_aggregates`` from an open-mastr SQLite file.

    Args:
        sqlite_path: open-mastr SQLite database (read-only access).
        margin_m: Margin around the extent of ``pylovo.postcode`` for units with coordinates.

    Returns:
        ``(number of units, number of postcode aggregates)``.

    Raises:
        FileNotFoundError: If ``sqlite_path`` does not exist.
    """
    sqlite_path = Path(sqlite_path)
    if not sqlite_path.exists():
        raise FileNotFoundError(sqlite_path)
    units, aggregates = read_units(sqlite_path, _study_bbox(margin_m))
    with dbc.DatabaseClient() as client:
        cur = client.cur
        cur.execute("TRUNCATE pylovo.mastr_units, pylovo.mastr_postcode_aggregates")
        execute_values(
            cur,
            f"""INSERT INTO pylovo.mastr_units
                    (mastr_id, location_id, technology, solar_kind, status, net_power_kw, gross_power_kw,
                     voltage_level, commissioning_date, gemeindeschluessel, postcode, geom)
                SELECT v.mastr_id, v.location_id, v.technology, v.solar_kind, v.status, v.net_power_kw,
                       v.gross_power_kw, v.voltage_level, v.commissioning_date::date, v.gemeindeschluessel,
                       v.postcode, ST_Transform(ST_SetSRID(ST_MakePoint(v.lon, v.lat), 4326), {int(TARGET_EPSG)})
                FROM (VALUES %s) AS v(mastr_id, location_id, technology, solar_kind, status, net_power_kw,
                                      gross_power_kw, voltage_level, commissioning_date, gemeindeschluessel,
                                      postcode, lon, lat)
                ON CONFLICT (mastr_id) DO NOTHING""",
            [tuple(u[c] for c in UNIT_COLUMNS) for u in units],
            page_size=5000,
        )
        execute_values(
            cur,
            """INSERT INTO pylovo.mastr_postcode_aggregates (postcode, technology, n_units, net_power_kw)
               SELECT v.postcode, v.technology, v.n_units, v.net_power_kw
               FROM (VALUES %s) AS v(postcode, technology, n_units, net_power_kw)
               WHERE v.postcode ~ '^[0-9]{5}$'
                 AND v.postcode::integer IN (SELECT plz FROM pylovo.postcode)""",
            [(a["postcode"], a["technology"], a["n_units"], a["net_power_kw"]) for a in aggregates],
            page_size=5000,
        )
        cur.execute("SELECT count(*) FROM pylovo.mastr_units")
        n_units = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pylovo.mastr_postcode_aggregates")
        n_agg = cur.fetchone()[0]
        client.conn.commit()
    return n_units, n_agg
