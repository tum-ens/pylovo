"""Download the eGon-data HV/MV substations from the Open Energy Platform and load them into ``pylovo``.

The MV stage matches each OSM HV/MV substation it uses with the nearest eGon-data HV/MV substation within
``egon_match_m`` and stores its ``bus_id``, the id of its 110 kV bus in the eTraGo grid model (``pylovo-mv
boundary``). eGon-data publishes the table ``grid.egon_hvmv_substation`` for Germany (about 3,900 rows); the Open
Energy Platform (OEP) serves it as ``data.edut_00_077`` through its API, in pages of :data:`PAGE_ROWS` rows.

The rows are cached as served in ``egon_data/egon_hvmv_substation.json`` of the data directory
(:func:`pylovo.utils.get_user_data_dir`): text columns, the point and the footprint as WKB hex in EPSG:4326, a
missing value as ``NA``. ``pylovo-setup`` downloads the file once and loads it into ``pylovo.egon_hvmv_substation``
(``NA`` becomes NULL, the geometries are transformed to ``TARGET_EPSG``); ``pylovo-import egon-substations``
loads it again, with ``--refresh`` after a new download. eGon-data derives the substations from OpenStreetMap;
keep the source and the date of the download when the bus ids are compared with another grid model.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

import requests
from psycopg2.extras import execute_values

from pylovo.config_loader import TARGET_EPSG
from pylovo.utils import PYLOVO_USER_AGENT, get_user_data_dir

#: Rows endpoint of the OEP API (version 0) and the OEP name of eGon-data's ``grid.egon_hvmv_substation``.
OEP_ROWS_URL = "https://openenergyplatform.org/api/v0/schema/data/tables/{table}/rows/"
OEP_TABLE = "edut_00_077"
#: Rows per request, attempts per page and the timeout of one request (s).
PAGE_ROWS = 500
ATTEMPTS = 4
TIMEOUT_S = 120
#: Columns of ``pylovo.egon_hvmv_substation`` taken from the OEP rows (the geometries come from ``point`` and
#: ``polygon``).
TEXT_COLUMNS: tuple[str, ...] = ("voltage", "power_type", "substation", "osm_id", "subst_name", "operator",
                                 "frequency", "ref", "dbahn")

INSERT_SQL = """
    INSERT INTO pylovo.egon_hvmv_substation (bus_id, status, voltage, power_type, substation, osm_id, subst_name,
                                             operator, frequency, ref, dbahn, geom, footprint)
    VALUES %s
"""


def egon_file() -> Path:
    """The cached OEP rows of the eGon-data HV/MV substations in the data directory."""
    return get_user_data_dir() / "egon_data" / "egon_hvmv_substation.json"


def download_egon_substations(path: Path | None = None) -> Path:
    """Download every row of the eGon-data HV/MV substations from the OEP API and write them as JSON.

    A page that fails is requested again up to :data:`ATTEMPTS` times. The file is written only when every page
    has arrived (through a temporary file), so an interrupted download never leaves a partial file.

    Args:
        path: File to write (default :func:`egon_file`).

    Returns:
        The written file.

    Raises:
        requests.RequestException: A page could not be fetched.
        ValueError: The API answered without rows.
    """
    path = Path(path) if path is not None else egon_file()
    url = OEP_ROWS_URL.format(table=OEP_TABLE)
    rows: list[dict] = []
    while True:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = requests.get(url, params={"limit": PAGE_ROWS, "offset": len(rows)},
                                        headers={"User-Agent": PYLOVO_USER_AGENT}, timeout=TIMEOUT_S)
                response.raise_for_status()
                page = response.json()
                break
            except (requests.RequestException, ValueError) as exc:
                if attempt == ATTEMPTS:
                    raise
                print(f"OEP answered {type(exc).__name__}: {exc} (attempt {attempt} of {ATTEMPTS}), trying again")
                time.sleep(5 * attempt)
        rows.extend(page)
        if len(page) < PAGE_ROWS:
            break
    if not rows:
        raise ValueError(f"the OEP API returned no rows of {OEP_TABLE}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_text(json.dumps(rows), encoding="utf-8")
    os.replace(partial, path)
    return path


def _value(record: dict, name: str) -> Any:
    """A text column of an OEP row; eGon-data writes a missing value as ``NA``."""
    value = record.get(name)
    if value is None:
        return None
    text = str(value).strip()
    return None if text in ("", "NA") else text


def egon_rows(records: Sequence[dict]) -> list[tuple]:
    """The rows of :data:`INSERT_SQL` from the OEP records (``bus_id`` first, the geometries as WKB hex in
    EPSG:4326 last); records without a bus id or point are left out, and of two records with one bus id the first
    is kept."""
    out, seen = [], set()
    for record in records:
        bus_id, point = record.get("bus_id"), _value(record, "point")
        if bus_id is None or point is None or int(bus_id) in seen:
            continue
        seen.add(int(bus_id))
        status = record.get("status")
        out.append((int(bus_id), int(status) if status not in (None, "", "NA") else None,
                    *(_value(record, name) for name in TEXT_COLUMNS), point, _value(record, "polygon")))
    return out


def load_egon_substations(cur: Any, records: Sequence[dict]) -> int:
    """Replace the rows of ``pylovo.egon_hvmv_substation`` by the OEP records (in the caller's transaction).

    Returns:
        The number of stored substations.
    """
    rows = egon_rows(records)
    epsg = int(TARGET_EPSG)
    cur.execute("DELETE FROM pylovo.egon_hvmv_substation")
    if rows:
        # the footprint column holds a point for the substations mapped as nodes: only polygons are kept
        execute_values(cur, INSERT_SQL, rows, page_size=1000, template=(
            f"(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            f"ST_Transform(ST_GeomFromWKB(decode(%s, 'hex'), 4326), {epsg}), "
            f"(SELECT CASE WHEN ST_Dimension(f.g) = 2 THEN ST_Multi(ST_Transform(f.g, {epsg})) END "
            f"FROM (SELECT ST_GeomFromWKB(decode(%s, 'hex'), 4326) AS g) f))"))
    return len(rows)


def import_egon_substations(cur: Any, refresh: bool = False) -> int:
    """Load the eGon-data HV/MV substations into ``pylovo.egon_hvmv_substation`` (in the caller's transaction),
    downloading them first if there is no cached file (or with ``refresh``).

    Returns:
        The number of stored substations.
    """
    path = egon_file()
    if refresh or not path.exists():
        print(f"Downloading the eGon-data HV/MV substations from the Open Energy Platform ({OEP_TABLE})...")
        download_egon_substations(path)
    records = json.loads(path.read_text(encoding="utf-8"))
    return load_egon_substations(cur, records)
