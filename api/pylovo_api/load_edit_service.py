"""Load editing in the UI: database adapter, shared caches and read annotations.

The edit logic itself is :class:`pylovo.load_editing.LoadEditor`; its queries are
:class:`pylovo.database.load_edit_mixin.LoadEditMixin`. The UI does not construct a
``DatabaseClient`` per request (that re-creates the shared ``DatabaseClient`` logger handlers and
an SQLAlchemy engine); :class:`EditDb` puts the mixin methods on one of the UI's own short-lived
connections instead.

The ``annotate_*`` helpers add the load-edit flags to the existing read endpoints. They are
no-ops while the audit table ``pylovo.load_edit`` does not exist.
"""
from __future__ import annotations

import csv
import getpass
import io
import logging
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras

from pylovo_api import __version__, db

log = logging.getLogger("pylovo_api.load_edits")
_CACHE = None
_CACHE_LOCK = threading.Lock()


def _classes():
    from pylovo.database.analysis_mixin import AnalysisMixin
    from pylovo.database.load_edit_mixin import LoadEditMixin

    return LoadEditMixin, AnalysisMixin


_EDIT_DB = None


def _edit_db_class():
    global _EDIT_DB
    if _EDIT_DB is None:
        load_edit_mixin, analysis_mixin = _classes()

        class EditDb(load_edit_mixin, analysis_mixin):
            """The load-edit and pandapower-persistence queries on a UI connection."""

            def __init__(self, conn):
                self.conn = conn
                self.cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                self.logger = log
                # Shortest-exact float output (bit-exact round trips through text and jsonb).
                self.cur.execute("SET extra_float_digits = 3")
                conn.commit()

        _EDIT_DB = EditDb
    return _EDIT_DB


def cache():
    """The parsed-grid cache shared by all requests."""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            from pylovo.load_editing import NetCache

            _CACHE = NetCache(6)
        return _CACHE


def client_label() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "?"
    return f"pylovo-api {__version__} {user}@{socket.gethostname()}"


@contextmanager
def edit_db(readonly: bool = True) -> Iterator[Any]:
    """An :class:`EditDb` on a fresh UI connection (closed afterwards)."""
    conn = db._connect(readonly)
    try:
        yield _edit_db_class()(conn)
    finally:
        conn.close()


@contextmanager
def editor(readonly: bool = True):
    """A :class:`pylovo.load_editing.LoadEditor` sharing the UI's power-flow lock and grid cache."""
    from pylovo.load_editing import LoadEditor
    from pylovo_api.powerflow import _PF_LOCK

    with edit_db(readonly) as dbx:
        yield LoadEditor(dbx, pf_lock=_PF_LOCK, cache=cache(), client=client_label())


def _table_exists(cur) -> bool:
    cur.execute("SELECT to_regclass('pylovo.load_edit') IS NOT NULL AS ok")
    row = cur.fetchone()
    return bool(row["ok"] if isinstance(row, dict) else row[0])


# --------------------------------------------------------------------------- annotations
def grid_flags(version_id: str | None = None, grid_ids: list[int] | None = None) -> dict[int, dict]:
    """Per edited grid: active edits, edited buildings, whether the stored net differs from generation."""
    try:
        with edit_db() as dbx:
            if not dbx.load_edit_table_exists():
                return {}
            rows = dbx.load_edit_grid_summary(version_id=version_id, grid_ids=grid_ids)
            ids = [r["grid_result_id"] for r in rows]
            analysed: set[int] = set()
            if ids:
                dbx.cur.execute("SELECT grid_result_id FROM pylovo.clustering_parameters WHERE grid_result_id = ANY(%s)",
                                (ids,))
                analysed = {r["grid_result_id"] for r in dbx.cur.fetchall()}
    except psycopg2.errors.UndefinedTable:
        return {}
    out = {}
    for r in rows:
        out[r["grid_result_id"]] = {
            "active_edits": int(r["active_edits"]), "all_edits": int(r["all_edits"]), "revision": int(r["revision"]),
            "edited_buildings": int(r["edited_buildings"]), "edited_objectids": list(r["edited_objectids"] or []),
            "modified": bool(r["modified"]), "last_edit_at": r["last_edit_at"],
            "analysis_removed": bool(r["modified"]) and r["grid_result_id"] not in analysed,
            "plz": r["plz"], "version_id": r["version_id"],
        }
    return out


def _public_flags(flags: dict | None) -> dict | None:
    if not flags:
        return None
    return {k: v for k, v in flags.items() if k not in ("edited_objectids", "plz", "version_id")}


_EXTRA_BUILDING_COLUMNS = ("residential_floor_area", "nonresidential_floor_area", "nonresidential_use",
                           "residential_peak_load_in_kw", "nonresidential_peak_load_in_kw", "nonresidential_mv_direct",
                           "peak_load_in_kw")


def annotate_grid_detail(detail: dict | None) -> dict | None:
    """Grid detail: ``grid.load_edit`` flags and the editable inputs of every building feature."""
    if not detail or not detail.get("grid"):
        return detail
    gid = detail["grid"]["grid_result_id"]
    rows = db.fetch_all(f"SELECT objectid, {', '.join(_EXTRA_BUILDING_COLUMNS)} FROM pylovo.buildings_result "
                        "WHERE grid_result_id = %s", (gid,))
    extra = {r.pop("objectid"): r for r in rows}
    flags = grid_flags(grid_ids=[gid]).get(gid)
    edited = set((flags or {}).get("edited_objectids") or [])
    loads_by_vertex: dict[int, float] = {}
    for f in detail["buses"]["features"]:
        p = f["properties"]
        if p.get("role") == "consumer" and p.get("installed_kw") is not None:
            match = (p.get("name") or "").rsplit(" ", 1)[-1]
            if match.isdigit():
                loads_by_vertex[int(match)] = p["installed_kw"]
    for f in detail["buildings"]["features"]:
        p = f["properties"]
        p.update(extra.get(p["objectid"], {}))
        p["installed_kw"] = round((p.get("residential_peak_load_in_kw") or 0) + (
            0 if p.get("nonresidential_mv_direct") else (p.get("nonresidential_peak_load_in_kw") or 0)), 3)
        p["edited"] = p["objectid"] in edited
    detail["grid"]["load_edit"] = _public_flags(flags)
    detail["grid"]["load_edit_revision"] = flags["revision"] if flags else 0  # grows with every edit and undo
    return detail


def annotate_summary(summary: dict, version_id: str) -> dict:
    """Results summary: ``load_edit`` per grid, ``kpis.edited_grids`` and ``analysis_removed``."""
    flags = grid_flags(version_id=version_id)
    for g in summary.get("grids", []):
        g["load_edit"] = _public_flags(flags.get(g["grid_result_id"]))
    shown = {g["grid_result_id"] for g in summary.get("grids", [])}
    modified = [gid for gid, f in flags.items() if f["modified"] and gid in shown]
    summary.setdefault("kpis", {})["edited_grids"] = len(modified)
    missing_plz = [r["plz"] for r in summary.get("plz_rows", []) if r.get("trafo_num") is None
                   and any(f["plz"] == r["plz"] and f["modified"] for f in flags.values())]
    summary["analysis_removed"] = {"plz": missing_plz,
                                   "grids": [gid for gid in modified if flags[gid]["analysis_removed"]]}
    return summary


def annotate_versions(rows: list[dict]) -> list[dict]:
    """Versions: ``edited_grids`` (stored nets that differ from generation) and ``load_edits`` (audit rows)."""
    try:
        with edit_db() as dbx:
            if not dbx.load_edit_table_exists():
                for r in rows:
                    r["edited_grids"], r["load_edits"] = 0, 0
                return rows
            dbx.cur.execute(
                """SELECT e.version_id, count(*) AS load_edits,
                          count(DISTINCT e.grid_result_id) FILTER (WHERE e.undone_at IS NULL) AS grids_with_active
                   FROM pylovo.load_edit e GROUP BY 1""")
            counts = {r["version_id"]: r for r in dbx.cur.fetchall()}
            modified = {}
            for r in dbx.load_edit_grid_summary():
                if r["modified"]:
                    modified[r["version_id"]] = modified.get(r["version_id"], 0) + 1
    except psycopg2.errors.UndefinedTable:
        return rows
    for r in rows:
        r["load_edits"] = int(counts.get(r["version_id"], {}).get("load_edits", 0))
        r["edited_grids"] = modified.get(r["version_id"], 0)
    return rows


def annotate_overview(data: dict, version_id: str, plz: int) -> dict:
    """Overview map: ``edited`` on every building feature."""
    flags = grid_flags(version_id=version_id)
    edited = {o for f in flags.values() if f["plz"] == plz for o in f["edited_objectids"]}
    for f in data.get("buildings", {}).get("features", []):
        f["properties"]["edited"] = f["properties"].get("objectid") in edited
    data["edited_grids"] = [gid for gid, f in flags.items() if f["plz"] == plz and f["modified"]]
    return data


# --------------------------------------------------------------------------- status and GIS view
def status_info() -> dict[str, Any]:
    """Report load-edit schema status; the building view reads current base rows."""
    info: dict[str, Any] = {
        "schema": "missing", "views_stale": 0,
        "refreshing_views": False, "refresh_error": None,
    }
    try:
        with db.cursor(timeout_s=3) as cur:
            if _table_exists(cur):
                info["schema"] = "ok"
    except Exception as exc:  # noqa: BLE001 - status must never fail
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def views_stale(cur) -> int:
    """The regular building view cannot lag behind its source rows."""
    return 0


# --------------------------------------------------------------------------- export
CSV_COLUMNS = ("load_edit_id", "version_id", "plz", "kcid", "bcid", "grid_result_id", "objectid", "address", "action",
               "field", "old", "new", "reason", "status_before", "status_after", "db_user", "client", "created_at",
               "undone_at", "undone_by")


def csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formulas: cells starting with = + - @ get a leading apostrophe."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def history_csv(rows: list[dict]) -> str:
    """One CSV line per changed field of every audit row."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        for fld, (old, new) in (r.get("changes") or {}).items():
            line = {**r, "field": fld, "old": old, "new": new,
                    "status_before": (r.get("before_grid") or {}).get("power_flow_status"),
                    "status_after": (r.get("after_grid") or {}).get("power_flow_status")}
            writer.writerow([csv_safe(line.get(c)) for c in CSV_COLUMNS])
    return out.getvalue()


def public_history_row(r: dict) -> dict:
    """History row for the API (without the stored before/after building values)."""
    return {k: v for k, v in r.items() if k not in ("before_building", "after_building")} | {
        "status": [(r.get("before_grid") or {}).get("power_flow_status"), (r.get("after_grid") or {}).get("power_flow_status")],
        "peak_kw": [(r.get("before_building") or {}).get("peak_load_in_kw"), (r.get("after_building") or {}).get("peak_load_in_kw")],
    }
