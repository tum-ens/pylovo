"""Input coverage: which postcodes (PLZ) have the input data ``pylovo-generate`` needs.

The region gate of the UI (Regions step, map, Generate step and ``POST /api/jobs/generate``)
asks this module whether a PLZ can be generated. With ``USE_INFDB=True`` that needs

* a postcode polygon (``pylovo.postcode``, or ``<INFDB_OPENDATA_SCHEMA>.postcodes_germany``
  which generation copies on demand),
* at least two importable buildings in ``basedata.buildings`` (the filters of
  ``InfdbClient.fetch_buildings_from_infdb`` and, if enabled, ``RESIDENTIAL_ONLY_GENERATION``;
  the settlement type needs two buildings and at least one residential building),
* street segments in ``ways_per_connection`` and building connection lines in
  ``connection_lines`` (both resolved through ``search_path = INFDB_SOURCE_SCHEMA, public``).

**Performance on a real InfDB.** The InfDB tables have no index on ``postcode``, so a
per-PLZ count is a full table scan. The InfDB DDL does index the municipality key
(``basedata.buildings.gemeindeschluessel``, ``ways_per_connection.ags``,
``connection_lines.ags``). The engine therefore works per municipality key: a loose index scan
lists the keys of each table in milliseconds, short background statements count the buildings,
streets and connection lines per ``(key, postcode)`` for a batch of keys, and the result is kept
in memory and in ``.pylovo-api/cache/input-coverage.json``. Keys of PLZ the user asks about
(``ensure``) are computed first, so a click is answered within about a second. Small tables
(the sandbox) are read with one ``GROUP BY``; large tables without a key index are read in the
background with one long statement at most every 15 minutes (and the UI suggests the index).

**Change detection** works without ``pg_stat`` counters (they are reset by an unclean restart):
the key set is listed again, ``public.changelog`` (written by the InfDB tools per municipality)
is compared with the last seen id per key, a table rewrite changes ``relfilenode``, and every key
is recounted after ``PYLOVO_API_COVERAGE_TTL_S`` (24 h). Change checks only run while a browser
uses the UI.

**Fail modes.** Only verified negatives block a PLZ. A PLZ whose keys are not counted yet is
``pending`` and one whose check failed is ``unknown``; both stay selectable ("not verified").
The gate is necessary, not sufficient: it says "input present", never "generation succeeds".

The filter rules below replicate the library (``infdb_client.py`` and the
``preprocessing_mixin`` building steps), which this module must not import SQL from;
``api/tests/test_coverage_db.py`` checks the equivalence against the sandbox data.
"""
from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.errors
import yaml

log = logging.getLogger("pylovo_api.coverage")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


# --------------------------------------------------------------------------- settings
TTL_S = _env_int("PYLOVO_API_COVERAGE_TTL_S", 86_400)          # rolling recount age of a key
CHECK_INTERVAL_S = 60                                            # change detection interval
CLIENT_IDLE_S = 300                                              # no checks without a browser
SMALL_TABLE_ROWS = _env_int("PYLOVO_API_COVERAGE_SMALL_ROWS", 200_000)
SMALL_TABLE_BYTES = 128 * 2 ** 20                                # when reltuples is unknown (-1)
CHUNK_KEYS, CHUNK_MAX = 25, 200                                  # keys per statement (adaptive)
CHUNK_TIMEOUT_MS = 60_000
FULL_TIMEOUT_S = 1_800                                           # one statement on a large table
FULL_MIN_INTERVAL_S = 900
LOCK_TIMEOUT_MS = 2_000
ENSURE_MAX_WAIT_S = 15.0
SAVE_INTERVAL_S = 30
SPARSE = 10                                                      # warn below this many buildings
MIN_BUILDINGS = 2                                                # house-distance metric needs two
FORMAT_VERSION = 1
FULL = "__full__"                                                # work item of the one-statement strategies
ENFORCE = os.getenv("PYLOVO_API_COVERAGE_ENFORCE", "1").strip().lower() not in ("0", "false", "no", "off")
CHANGELOG_TOOLS: dict[str, str] = {"buildings": "infdb-basedata-buildings", "ways": "infdb-basedata-ways",
                                   "lines": "infdb-basedata-ways"}
try:
    CHANGELOG_TOOLS.update(json.loads(os.getenv("PYLOVO_API_COVERAGE_TOOLS") or "{}"))
except ValueError:
    log.warning("PYLOVO_API_COVERAGE_TOOLS is not valid JSON; using the default InfDB tool names")

# --------------------------------------------------------------------------- filter rules
# InfdbClient.fetch_buildings_from_infdb (consumer candidates, optional address filter).
CONSUMER_USES = ("Commercial", "Public", "Residential", "Mixed")
STATION_USE_ID = "31001_2523"
CAND_SQL = ("b.building_use IN ('Commercial', 'Public', 'Residential', 'Mixed') "
            "AND COALESCE(b.building_use_id, '') <> '31001_2523'")
ADDR_SQL = "COALESCE(b.street, '') <> '' AND COALESCE(b.house_number, '') <> ''"
# PreprocessingMixin.remove_non_residential_buildings_from_buildings_tem keeps exactly these rows
# (type = COALESCE(building_type, building_use) as selected by fetch_buildings_from_infdb);
# calculate_avg_households_per_building uses the same rule.
RESIDENTIAL_TYPES = ("SFH", "TH", "MFH", "AB")
RES_SQL = ("(COALESCE(b.residential_floor_area, 0) > 0 "
           "OR COALESCE(b.building_type, b.building_use) IN ('SFH', 'TH', 'MFH', 'AB'))")
# The raw-row part of the checks in PreprocessingMixin.set_building_peak_load that abort a PLZ.
BAD_SQL = ("(b.residential_floor_area < 0 OR b.nonresidential_floor_area < 0 "
           "OR (b.residential_floor_area IS NULL) <> (b.nonresidential_floor_area IS NULL) "
           "OR (b.residential_floor_area IS NOT NULL AND b.nonresidential_floor_area IS NOT NULL "
           "AND b.floor_area IS NOT NULL AND b.floor_number IS NOT NULL "
           "AND abs(b.residential_floor_area + b.nonresidential_floor_area - b.floor_area * b.floor_number) > 0.01) "
           "OR (COALESCE(b.residential_floor_area, 0) > 0 AND b.households IS NOT NULL AND b.households <= 0))")
# InfdbClient.fetch_ways_from_infdb drops klasse 'Rad- und Fußweg' (clazz 72) from both tables.
EXCLUDED_KLASSE = "Rad- und Fußweg"
WAY_SQL = "btrim(t.klasse) IS DISTINCT FROM 'Rad- und Fußweg'"
RULES_HASH = hashlib.sha1("|".join((CAND_SQL, ADDR_SQL, RES_SQL, BAD_SQL, WAY_SQL)).encode()).hexdigest()[:12]

# Building counters per (key, postcode), in this order.
BC = ("total", "cand", "cand_addr", "res", "res_addr", "stations", "bad", "bad_addr", "bad_res", "bad_res_addr")

# The three InfDB inputs: relation (resolved with the InfDB search_path) and possible key columns.
TABLES: dict[str, dict[str, Any]] = {
    "buildings": {"rel": "basedata.buildings", "keys": ("gemeindeschluessel", "ags"), "label": "buildings",
                  "tool": "infdb-basedata-buildings"},
    "ways": {"rel": "ways_per_connection", "keys": ("ags", "gemeindeschluessel"), "label": "street segments",
             "tool": "infdb-basedata-ways"},
    "lines": {"rel": "connection_lines", "keys": ("ags", "gemeindeschluessel"), "label": "connection lines",
              "tool": "infdb-basedata-ways"},
}
KEY_COLUMNS = {"ags", "gemeindeschluessel"}

SEVERITY_SELECTABLE = {"ok", "warn", "pending", "unknown"}


# --------------------------------------------------------------------------- helpers
def key_to_ags(key: Any) -> int | None:
    """Map an InfDB municipality key to the integer AGS of ``pylovo.municipal_register``.

    ``'09184137'`` and ``'9184137'`` give 9184137; a 12-digit regional key (ARS) gives
    ``int(ARS[:5] + ARS[9:])``; anything else (``'*'`` for tables without a key column) ``None``.
    """
    text = str(key).strip()
    if not text.isdigit():
        return None
    if len(text) in (7, 8):
        return int(text)
    if len(text) == 12:
        return int(text[:5] + text[9:])
    return None


def ags8(ags: int) -> str:
    return str(int(ags)).zfill(8)


def as_plz(value: Any) -> int | None:
    """PLZ of a ``postcode`` value (integer or text column); ``None`` for anything else."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text) if text.isdigit() and len(text) <= 5 else None


def _vadd(a: list[int] | None, b: list[int]) -> list[int]:
    return list(b) if a is None else [x + y for x, y in zip(a, b)]


def _plan_has_seq_scan(plan: Any, relname: str) -> bool:
    """Whether an ``EXPLAIN (FORMAT JSON)`` plan scans ``relname`` sequentially."""
    stack = [plan]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, dict):
            if node.get("Node Type") == "Seq Scan" and node.get("Relation Name") == relname:
                return True
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return False


_flag_cache: dict[str, Any] = {}


def read_flags(config_file: Path | None = None) -> dict[str, bool]:
    """``EXCLUDE_BUILDINGS_WITHOUT_ADDRESS`` / ``RESIDENTIAL_ONLY_GENERATION`` of the current config.

    Read from ``config_generation.yaml`` at request time (cached by mtime), because the gate must
    follow config edits without a restart. A file that does not parse falls back to the values
    ``pylovo.config_loader`` loaded, instead of silently turning both filters off.
    """
    if config_file is None:
        from pylovo_api.settings import paths

        config_file = paths().config_file
    try:
        st = config_file.stat()
        sig = (str(config_file), st.st_mtime_ns, st.st_size)
        if _flag_cache.get("sig") != sig:
            values = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            if not isinstance(values, dict):
                raise ValueError("not a mapping")
            _flag_cache.update(sig=sig, flags={
                "exclude_buildings_without_address": bool(values.get("EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", False)),
                "residential_only": bool(values.get("RESIDENTIAL_ONLY_GENERATION", False))})
        return dict(_flag_cache["flags"])
    except (OSError, ValueError, yaml.YAMLError):
        try:
            from pylovo import config_loader as cl

            return {"exclude_buildings_without_address": bool(cl.EXCLUDE_BUILDINGS_WITHOUT_ADDRESS),
                    "residential_only": bool(cl.RESIDENTIAL_ONLY_GENERATION)}
        except Exception:  # noqa: BLE001 - config broken everywhere: pylovo-generate fails anyway
            return {"exclude_buildings_without_address": False, "residential_only": False}


def flags_key(flags: dict[str, bool]) -> str:
    return f"{int(flags['exclude_buildings_without_address'])}{int(flags['residential_only'])}"


def building_counts(bc: list[int] | None, flags: dict[str, bool]) -> dict[str, int]:
    """Effective building counts of one PLZ for the current filter flags."""
    if bc is None:
        bc = [0] * len(BC)
    c = dict(zip(BC, bc))
    ra, ro = flags["exclude_buildings_without_address"], flags["residential_only"]
    return {
        "total": c["total"],
        "plain": c["res"] if ro else c["cand"],                      # without the address filter
        "importable": (c["res_addr"] if ra else c["res"]) if ro else (c["cand_addr"] if ra else c["cand"]),
        "residential": c["res_addr"] if ra else c["res"],
        "residential_plain": c["res"],
        "candidates": c["cand"],
        "stations": c["stations"],
        "bad": (c["bad_res_addr"] if ra else c["bad_res"]) if ro else (c["bad_addr"] if ra else c["bad"]),
    }


def ui_data_dir(root: Path) -> Path:
    """The data directory of file mode, as ``pylovo.utils.get_user_data_dir`` sees it in a job."""
    if os.getenv("PYLOVO_DATA_DIR"):
        return Path(os.environ["PYLOVO_DATA_DIR"])
    if os.getenv("PYLOVO_ROOT"):
        return Path(os.environ["PYLOVO_ROOT"]) / "data"
    return Path(root) / "data"


# --------------------------------------------------------------------------- state
@dataclass
class TableState:
    """Coverage of one InfDB input table."""

    name: str
    rel: str | None = None                 # schema-qualified, quoted relation name
    relname: str | None = None
    strategy: str = "unknown"              # ags | full | full_large | missing | unknown (not probed)
    reason: str | None = None              # why 'missing' or why the key index was rejected
    key_col: str | None = None
    key_type: str | None = None
    index: str | None = None
    est_rows: int | None = None
    bytes: int | None = None
    relfilenode: int | None = None
    keys: set[str] = field(default_factory=set)
    by_ags: dict[int, set[str]] = field(default_factory=dict)
    rows: dict[str, dict[int, list[int]]] = field(default_factory=dict)       # key -> plz -> counters
    plz_rows: dict[int, dict[str, list[int]]] = field(default_factory=dict)   # plz -> key -> counters
    computed: dict[str, float] = field(default_factory=dict)                  # key (or FULL) -> time
    markers: dict[str, int] = field(default_factory=dict)                     # key -> changelog id
    pending: dict[str, int] = field(default_factory=dict)                     # key (or FULL) -> priority
    backoff: dict[str, tuple[int, float, str]] = field(default_factory=dict)  # key -> (fails, retry_at, error)
    error: str | None = None
    stale: bool = False
    last_check: float | None = None
    _complete: tuple[int, bool] | None = field(default=None, repr=False)   # (engine version, value)

    @property
    def full(self) -> bool:
        return self.strategy in ("full", "full_large")

    def index_keys(self) -> None:
        self.by_ags = {}
        for key in self.keys:
            ags = key_to_ags(key)
            if ags is not None:
                self.by_ags.setdefault(ags, set()).add(key)

    def done(self) -> int:
        if self.full:
            return int(FULL in self.computed)
        return sum(1 for k in self.keys if k in self.computed)

    def total(self) -> int:
        return 1 if self.full else len(self.keys)

    def complete(self, version: int | None = None) -> bool:
        """Every key of the table is counted (cached per engine ``version``; O(keys) otherwise)."""
        if version is not None and self._complete and self._complete[0] == version:
            return self._complete[1]
        if self.strategy in ("missing", "unknown"):
            value = False
        else:
            value = FULL in self.computed if self.full else all(k in self.computed for k in self.keys)
        if version is not None:
            self._complete = (version, value)
        return value

    def merge(self, keys: list[str], result: dict[str, dict[int, list[int]]], now: float) -> None:
        """Replace the counters of ``keys`` by ``result`` (keys without rows become empty)."""
        for key in keys:
            for plz in self.rows.pop(key, {}):
                per_plz = self.plz_rows.get(plz)
                if per_plz is not None:
                    per_plz.pop(key, None)
                    if not per_plz:
                        del self.plz_rows[plz]
            new = result.get(key) or {}
            if new:
                self.rows[key] = new
            for plz, counters in new.items():
                self.plz_rows.setdefault(plz, {})[key] = counters
            if not self.full:
                self.computed[key] = now

    def reset(self) -> None:
        self.keys, self.by_ags, self.rows, self.plz_rows = set(), {}, {}, {}
        self.computed, self.markers, self.pending, self.backoff = {}, {}, {}, {}


@dataclass
class View:
    """What one table says about one PLZ."""

    counts: list[int] | None      # summed counters of the computed keys (None: nothing computed)
    pending: bool                 # needed keys are not computed yet
    exact: bool                   # all keys of the PLZ are computed and the table pass is complete
    preliminary: bool             # negative from the key set alone while the pass is incomplete
    keys: list[str]


# --------------------------------------------------------------------------- engine
class InputCoverage:
    """Background engine that knows which PLZ have generation input (see the module docstring).

    Args:
        cache_file: JSON cache of the counters (``.pylovo-api/cache/input-coverage.json``).
        jobs: The :class:`~pylovo_api.jobs.JobManager` (throttling while ``generate`` runs).
        root: Project root (file mode reads ``<data dir>/buildings`` relative to it).
        small_rows: Row estimate below which a table is read with one statement.
        conn_options: Extra ``-c`` options for the worker connection (tests).
    """

    def __init__(self, cache_file: Path | None = None, jobs: Any = None, root: Path | None = None,
                 small_rows: int = SMALL_TABLE_ROWS, conn_options: str = ""):
        self.cache_file = Path(cache_file) if cache_file else None
        self.jobs = jobs
        self.root = Path(root) if root else Path.cwd()
        self.small_rows = small_rows
        self.conn_options = conn_options
        self.mode = "infdb"
        self.tables = {name: TableState(name) for name in TABLES}
        self.register: dict[int, list[int]] = {}
        self.ags_names: dict[int, str] = {}
        self.local_bbox: dict[int, tuple] = {}
        self.infdb_polygons: set[str] = set()
        self.tokens: dict[str, str] = {}
        self.changelog: dict[tuple[str, int], tuple[int, float | None]] = {}
        self.changelog_present = False
        self.files: dict[str, Any] = {}
        self.sources_error: str | None = None
        self.state = "starting"
        self.error: str | None = None
        self.version = 0
        self.published_at: float | None = None
        self.initialized = False
        self.chunk = CHUNK_KEYS
        self.settings: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None
        self._last_seen = time.time()
        self._last_check = 0.0
        self._last_save = 0.0
        self._dirty = False
        self._check_requested = False
        self._requeue_all = False
        self._summary_cache: tuple | None = None
        self._pass_started: float | None = None
        self._pass_done0 = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Start the worker thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="coverage", daemon=True)
        self._thread.start()

    def shutdown(self, wait_s: float = 2.0) -> None:
        """Cancel the running statement, save the cache and stop the worker."""
        self._stop.set()
        self._wake.set()
        conn = self._conn
        if conn is not None:
            try:
                conn.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(wait_s)
        self._save(force=True)

    def touch(self) -> None:
        """Record browser activity (change checks only run while the UI is used)."""
        self._last_seen = time.time()

    def request_refresh(self, scope: str = "changes") -> None:
        """Run change detection now (``changes``) or recount every key (``all``)."""
        with self._lock:
            self._check_requested = True
            self._requeue_all = self._requeue_all or scope == "all"
        self.touch()
        self._wake.set()

    def on_job_finished(self, job: Any) -> None:
        """Setup, reset and generate change pylovo.postcode (and the register): re-read those sources."""
        if getattr(job, "kind", None) in ("setup", "reset", "generate"):
            with self._lock:
                self.tokens = {}
                self._check_requested = True
            self._wake.set()

    # ------------------------------------------------------------------ public queries
    def hint(self, plz_list: list[int]) -> None:
        """Count the keys of these PLZ early (search hits, basket), without waiting."""
        with self._lock:
            for table, key in self._wanted([int(p) for p in plz_list][:500]):
                if key not in table.computed:
                    table.pending[key] = min(table.pending.get(key, 9), 1)
        self._wake.set()

    def ensure(self, plz_list: list[int], wait_s: float = 3.0, fresh: bool = False) -> None:
        """Compute the keys of ``plz_list`` first and wait up to ``wait_s`` seconds for them.

        Never runs a full-table statement in the calling thread; the worker does all SQL.
        """
        plz_list = [int(p) for p in plz_list][:500]
        self.touch()
        deadline = time.monotonic() + max(0.0, min(float(wait_s), ENSURE_MAX_WAIT_S))
        since = time.time() if fresh else None
        with self._cond:
            if fresh and self.mode == "files":
                self.tokens = {}
                self._check_requested = True
            wanted: list[tuple[TableState, str]] | None = None
            while True:
                if wanted is None and self.initialized:
                    wanted = self._wanted(plz_list, fresh=fresh)
                    for table, key in wanted:
                        if fresh or key not in table.computed:
                            table.pending[key] = 0
                            table.backoff.pop(key, None)
                    self._wake.set()
                if wanted is not None and not (fresh and self._check_requested) and all(
                        self._item_done(t, k, since) for t, k in wanted):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._cond.wait(remaining)

    def statuses(self, plz_list: list[int], flags: dict[str, bool] | None = None,
                 detail: bool = True) -> dict[int, dict]:
        """InputStatus of every PLZ; ``detail=False`` skips the per-municipality matrix (``by_ags``)."""
        flags = flags or read_flags()
        with self._lock:
            return {int(plz): self._evaluate(int(plz), flags, detail=detail) for plz in plz_list}

    def status_of(self, plz: int, flags: dict[str, bool] | None = None) -> dict:
        return self.statuses([plz], flags)[int(plz)]

    def selectable_set(self, flags: dict[str, bool] | None = None) -> set[int]:
        """All known PLZ that can be selected now (for SQL-side ordering and filtering)."""
        return self._summary(flags or read_flags())["selectable"]

    def ready_set(self, flags: dict[str, bool] | None = None) -> set[int]:
        """The selectable PLZ whose input has been checked (``selectable_set`` minus *not verified*)."""
        return self._summary(flags or read_flags())["ready"]

    def brief_status(self) -> dict:
        """Small engine status for ``/api/status``."""
        flags = read_flags()
        s = self._summary(flags)
        with self._lock:
            return {"mode": self.mode, "state": self.state, "version": self.version, "flags_key": flags_key(flags),
                    "building": self.state == "building", "stale": any(t.stale for t in self.tables.values()),
                    "published_at": self.published_at, "error": self.error, **s["counts"],
                    "progress": self._progress(), "buildings_total": s["buildings_total"]}

    def coverage(self) -> dict:
        """Full engine status for ``GET /api/regions/coverage``."""
        flags = read_flags()
        s = self._summary(flags)
        with self._lock:
            tables = {}
            suggested = []
            for name, t in self.tables.items():
                tables[name] = {"table": t.rel or TABLES[name]["rel"], "exists": t.strategy not in ("missing", "unknown"),
                                "strategy": t.strategy, "reason": t.reason, "index": t.index, "key_column": t.key_col,
                                "est_rows": t.est_rows, "keys": len(t.keys), "done": t.done(), "total": t.total(),
                                "complete": t.complete(), "last_check_at": t.last_check, "error": t.error,
                                "stale": t.stale}
                if t.strategy == "full_large" and t.rel:
                    col = t.key_col or TABLES[name]["keys"][0]
                    idx = f"{(t.relname or name)}_{col}_idx"
                    suggested.append(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {idx} ON {t.rel} ({col});")
            src = self.settings.get("infdb_source_schema")
            return {
                "mode": self.mode, "state": self.state, "version": self.version, "published_at": self.published_at,
                "age_s": round(time.time() - self.published_at, 1) if self.published_at else None,
                "progress": self._progress(), "error": self.error or self.sources_error,
                "counts": s["counts"], "ready_bounds": s["ready_bounds"], "tables": tables,
                "changelog": {"present": self.changelog_present, "tools": sorted(set(CHANGELOG_TOOLS.values()))},
                "filters": flags, "flags_key": flags_key(flags), "suggested_sql": suggested,
                "schema_note": (f"INFDB_SOURCE_SCHEMA is '{src}': buildings are still read from basedata.buildings, "
                                "streets and connection lines from that schema (as pylovo-generate does).")
                if self.mode == "infdb" and src and src != "basedata" else None,
                "enforce": ENFORCE, "files": self.files if self.mode == "files" else None,
            }

    # ------------------------------------------------------------------ evaluation
    def _register_keys(self, t: TableState, plz: int) -> set[str]:
        keys: set[str] = set()
        for ags in self.register.get(plz, ()):
            keys |= t.by_ags.get(ags, set())
        return keys

    def _view(self, t: TableState, plz: int) -> View:
        observed = t.plz_rows.get(plz, {})
        total = None
        for counters in observed.values():
            total = _vadd(total, counters)
        if t.full:
            done = FULL in t.computed
            return View(total if done else None, not done, done, False, sorted(observed))
        reg = self._register_keys(t, plz)
        needed = reg | set(observed)
        uncomputed = [k for k in needed if k not in t.computed]
        complete = t.complete(self.version)
        if not needed:
            # none of the PLZ's municipalities is in this table: zero, exact once every key is counted
            return View(None, False, complete, not complete, [])
        return View(total, bool(uncomputed), complete and not uncomputed, False, sorted(needed))

    def _evaluate(self, plz: int, flags: dict[str, bool], detail: bool = True) -> dict:
        """InputStatus of one PLZ from the in-memory counters (caller holds the lock)."""
        if self.mode == "files":
            return self._evaluate_files(plz, flags, detail)
        warnings: list[dict] = []
        checked = [t.last_check for t in self.tables.values() if t.last_check]
        stale = any(t.stale for t in self.tables.values())
        base = {"plz": plz, "warnings": warnings, "actions": [], "counts": None, "by_ags": None, "exact": False,
                "preliminary": False, "stale": stale, "checked_at": max(checked) if checked else None, "missing": []}

        def result(status: str, **extra) -> dict:
            out = dict(base, status=status, **extra)
            return _finish(out, flags)

        if not self.initialized:
            if self.error:
                return result("unknown", error=self.error)
            return result("pending")
        if self.sources_error:
            return result("no_setup", error=self.sources_error)
        missing = [name for name, t in self.tables.items() if t.strategy == "missing"]
        if missing:
            return result("no_source", missing=missing, tables=[self.tables[m].rel or TABLES[m]["rel"] for m in missing],
                          reasons={m: self.tables[m].reason for m in missing}, source_schema=self.settings.get(
                              "infdb_source_schema"))
        local = plz in self.local_bbox
        infdb_polygon = str(plz) in self.infdb_polygons or str(plz).zfill(5) in self.infdb_polygons
        if not local and not infdb_polygon:
            return result("no_geometry", opendata_schema=self.settings.get("infdb_opendata_schema"))
        if not local:
            warnings.append({"code": "geometry_infdb", "text": "The postcode polygon is fetched from InfDB during "
                             "generation (it is not in pylovo.postcode yet)."})

        vb = self._view(self.tables["buildings"], plz)
        vs = self._view(self.tables["ways"], plz)
        vl = self._view(self.tables["lines"], plz)
        bcounts = building_counts(vb.counts, flags)
        streets = (vs.counts or [0])[0]
        lines = (vl.counts or [0])[0]
        base["counts"] = {"buildings_total": bcounts["total"], "buildings_importable": bcounts["importable"],
                          "residential": bcounts["residential"], "station_buildings": bcounts["stations"],
                          "invalid_area_rows": bcounts["bad"], "street_segments": streets,
                          "connection_lines": lines}
        base["exact"] = vb.exact and vs.exact and vl.exact
        if detail:
            base["by_ags"] = self._by_ags(plz)
        errors = [t.error for t in self.tables.values() if t.error]

        # warnings that do not depend on the outcome
        reg = self.register.get(plz, [])
        for name, t in self.tables.items():
            if t.key_col is None or not t.keys:
                continue
            absent = [a for a in reg if a not in t.by_ags]
            # all municipalities absent is a block (no buildings / streets / lines), not a warning,
            # unless rows of this PLZ come from a municipality the register does not list for it
            if absent and (len(absent) < len(reg) or plz in t.plz_rows):
                names = ", ".join(f"{self.ags_names.get(a, '?')} (AGS {ags8(a)})" for a in absent)
                warnings.append({"code": f"partial:{name}", "table": name, "ags": [ags8(a) for a in absent],
                                 "tool": TABLES[name]["tool"],
                                 "text": f"No {TABLES[name]['label']} for {names} in InfDB: that municipality is not "
                                         "processed, so this part of the PLZ is missing."})

        def blocked_or_pending(view: View, status: str, **extra) -> dict:
            if view.pending:
                return result("pending", waiting_for=status)
            return result(status, preliminary=view.preliminary, **extra)

        # 3 buildings chain (only the first missing requirement is reported)
        if vb.pending and vb.counts is None:
            return result("pending") if not errors else result("unknown", error="; ".join(errors))
        if bcounts["total"] == 0:
            return blocked_or_pending(vb, "no_buildings", ags=self._missing_ags(plz, "buildings"))
        if bcounts["importable"] == 0:
            return blocked_or_pending(vb, "no_consumers", total=bcounts["total"], plain=bcounts["plain"],
                                      candidates=bcounts["candidates"])
        if bcounts["importable"] < MIN_BUILDINGS:
            return blocked_or_pending(vb, "too_few_buildings")
        if bcounts["residential"] == 0:
            return blocked_or_pending(vb, "no_residential", residential_plain=bcounts["residential_plain"])
        # 4 streets, 5 connection lines
        if vs.pending and vs.counts is None:
            return result("pending")
        if streets == 0:
            return blocked_or_pending(vs, "no_streets", ags=self._missing_ags(plz, "ways"))
        if vl.pending and vl.counts is None:
            return result("pending")
        if lines == 0:
            return blocked_or_pending(vl, "no_connection_lines", ags=self._missing_ags(plz, "lines"))
        if bcounts["importable"] < SPARSE:
            warnings.append({"code": "sparse", "text": f"Only {bcounts['importable']} importable buildings."})
        if bcounts["bad"]:
            warnings.append({"code": "data_quality", "text": f"{bcounts['bad']} buildings have an inconsistent "
                             "floor-area split or household count; pylovo-generate aborts the PLZ in "
                             "set_building_peak_load unless they are removed earlier (duplicates, transformer "
                             "overlap)."})
        if vb.pending or vs.pending or vl.pending:
            base["exact"] = False
        return result("ready")

    def _missing_ags(self, plz: int, table: str) -> list[str]:
        t = self.tables[table]
        reg = self.register.get(plz, [])
        absent = [ags8(a) for a in reg if a not in t.by_ags]
        return absent or [ags8(a) for a in reg]

    def _by_ags(self, plz: int) -> list[dict]:
        """Municipality × table matrix of one PLZ (register AGS plus keys only seen in the data)."""
        rows = []
        seen: set[int] = set()
        agss = list(self.register.get(plz, []))
        for t in self.tables.values():
            for key in t.plz_rows.get(plz, {}):
                ags = key_to_ags(key)
                if ags is not None and ags not in agss:
                    agss.append(ags)
        for ags in agss:
            if ags in seen:
                continue
            seen.add(ags)
            entry: dict[str, Any] = {"ags": ags8(ags), "name_city": self.ags_names.get(ags),
                                     "in_register": ags in self.register.get(plz, []), "processed_at": None}
            for name, t in self.tables.items():
                if t.key_col is None:
                    counters = [sum((c or [0])[0] for c in t.plz_rows.get(plz, {}).values())] if t.full else None
                    entry[name] = {"state": "ok" if counters and counters[0] else "unknown",
                                   "n": counters[0] if counters else None}
                    continue
                keys = t.by_ags.get(ags, set())
                if not keys:
                    entry[name] = {"state": "not_processed" if t.keys or t.complete(self.version) else "pending", "n": 0}
                    continue
                n = sum((t.rows.get(k, {}).get(plz) or [0])[0] for k in keys)
                computed = FULL in t.computed if t.full else all(k in t.computed for k in keys)
                entry[name] = {"state": "pending" if not computed else "ok" if n else "none", "n": n}
                tool = CHANGELOG_TOOLS.get(name)
                marker = self.changelog.get((tool, ags)) if tool else None
                if marker and marker[1] and (entry["processed_at"] is None or marker[1] > entry["processed_at"]):
                    entry["processed_at"] = marker[1]
            rows.append(entry)
        return rows

    def _evaluate_files(self, plz: int, flags: dict[str, bool], detail: bool) -> dict:
        warnings: list[dict] = []
        base = {"plz": plz, "warnings": warnings, "actions": [], "counts": None, "by_ags": None, "exact": True,
                "preliminary": False, "stale": False, "checked_at": self.files.get("checked_at"), "missing": []}

        def result(status: str, **extra) -> dict:
            return _finish(dict(base, status=status, **extra), flags)

        if not self.initialized:
            return result("unknown", error=self.error) if self.error else result("pending")
        if self.sources_error:
            return result("no_setup", error=self.sources_error)
        if plz not in self.local_bbox:
            return result("no_geometry", files=True)
        reg = self.register.get(plz, [])
        if not reg:
            return result("not_in_register")
        res = set(self.files.get("res_ags", []))
        oth = set(self.files.get("oth_ags", []))
        logged = set(self.files.get("ags_log", []))
        covered = [a for a in reg if a in res or a in logged]
        if detail:
            base["by_ags"] = [{"ags": ags8(a), "name_city": self.ags_names.get(a), "in_register": True,
                               "buildings": {"state": "ok" if a in res or a in logged else "not_processed",
                                             "n": None, "imported": a in logged, "res_file": a in res,
                                             "oth_file": a in oth}} for a in reg]
        if not covered:
            return result("no_buildings", files=True, ags=[ags8(a) for a in reg],
                          data_dir=str(self.files.get("data_dir")))
        absent = [a for a in reg if a not in covered]
        if absent:
            warnings.append({"code": "partial:buildings", "table": "buildings", "ags": [ags8(a) for a in absent],
                             "text": "No building shapefile for " + ", ".join(
                                 f"{self.ags_names.get(a, '?')} (AGS {ags8(a)})" for a in absent) + "."})
        if not flags["residential_only"]:
            no_oth = [a for a in covered if a not in oth and a not in logged]
            if no_oth:
                warnings.append({"code": "no_other_buildings", "text": "No Oth_* shapefile for AGS "
                                 + ", ".join(ags8(a) for a in no_oth) + ": commercial and public buildings are missing."})
        if not self.files.get("ways_rows"):
            return result("no_streets", files=True)
        return result("ready")

    # ------------------------------------------------------------------ summaries
    def _summary(self, flags: dict[str, bool]) -> dict:
        """Counts over all PLZ with a polygon, the selectable set and the extent of ready PLZ (cached)."""
        with self._lock:
            key = (self.version, flags_key(flags), self.initialized)
            cached = self._summary_cache
            if cached and cached[0] == key:
                return cached[1]
            universe = set(self.local_bbox) | set(self.register)
            counts = {"postcodes": len(self.local_bbox), "ready": 0, "partial": 0, "pending": 0, "blocked": 0}
            selectable: set[int] = set()
            ready: set[int] = set()
            minx = miny = float("inf")
            maxx = maxy = float("-inf")
            for plz in universe:
                st = self._evaluate(plz, flags, detail=False)
                if st["selectable"]:
                    selectable.add(plz)
                    if st["severity"] not in ("pending", "unknown"):
                        ready.add(plz)
                if plz not in self.local_bbox:
                    continue
                if st["severity"] in ("pending", "unknown"):
                    counts["pending"] += 1
                elif not st["selectable"] or st["severity"] == "block":
                    counts["blocked"] += 1
                else:
                    counts["ready"] += 1
                    if any(w["code"].startswith("partial") for w in st["warnings"]):
                        counts["partial"] += 1
                    b = self.local_bbox[plz]
                    minx, miny, maxx, maxy = min(minx, b[0]), min(miny, b[1]), max(maxx, b[2]), max(maxy, b[3])
            ready_bounds = [round(v, 6) for v in (minx, miny, maxx, maxy)] if counts["ready"] else None
            total_buildings = sum(sum((c or [0])[0] for c in rows.values())
                                  for rows in self.tables["buildings"].plz_rows.values()) if self.mode == "infdb" else None
            summary = {"counts": counts, "selectable": selectable, "ready": ready, "ready_bounds": ready_bounds,
                       "buildings_total": total_buildings}
            self._summary_cache = (key, summary)
            return summary

    def _progress(self) -> dict | None:
        if self.mode != "infdb":
            return None
        tables = {n: {"done": t.done(), "total": t.total()} for n, t in self.tables.items()}
        done = sum(v["done"] for v in tables.values())
        total = sum(v["total"] for v in tables.values())
        eta = None
        if self._pass_started and done > self._pass_done0 and total > done:
            rate = (done - self._pass_done0) / max(1e-6, time.time() - self._pass_started)
            eta = round((total - done) / rate) if rate > 0 else None
        return {"tables": tables, "done": done, "total": total, "eta_s": eta}

    # ------------------------------------------------------------------ worker: scheduling
    def _wanted(self, plz_list: list[int], fresh: bool = False) -> list[tuple[TableState, str]]:
        out = []
        for t in self.tables.values():
            if t.strategy == "full":
                if fresh or FULL not in t.computed:
                    out.append((t, FULL))
                continue
            if t.strategy != "ags":
                continue
            for plz in plz_list:
                for key in self._register_keys(t, plz) | set(t.plz_rows.get(plz, {})):
                    out.append((t, key))
        return out

    @staticmethod
    def _item_done(t: TableState, key: str, since: float | None) -> bool:
        if t.strategy not in ("ags", "full"):
            return True
        if key in t.backoff:
            return True
        at = t.computed.get(key)
        return at is not None and (since is None or at >= since)

    def _next_batch(self) -> tuple[TableState, list[str]] | None:
        now = time.time()
        generating = self._generate_running()
        best: tuple[int, TableState] | None = None
        for t in self.tables.values():
            if t.strategy not in ("ags", "full", "full_large") or not t.pending:
                continue
            if t.strategy == "full_large":
                last = t.computed.get(FULL, 0)
                if generating or now - last < FULL_MIN_INTERVAL_S and FULL in t.computed:
                    continue
            for key, prio in t.pending.items():
                back = t.backoff.get(key)
                if back and back[1] > now:
                    continue
                if best is None or prio < best[0]:
                    best = (prio, t)
                    if prio == 0:
                        break
        if best is None:
            return None
        prio, t = best
        if t.full:
            return t, [FULL]
        keys = [k for k, p in t.pending.items() if p == prio and not (t.backoff.get(k) and t.backoff[k][1] > now)]
        return t, sorted(keys)[: self.chunk]

    def _generate_running(self) -> bool:
        try:
            job = self.jobs.running_writer() if self.jobs else None
        except Exception:  # noqa: BLE001
            return False
        return bool(job and getattr(job, "kind", None) == "generate")

    # ------------------------------------------------------------------ worker: loop
    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                if not self.initialized:
                    self._initialize()
                    backoff = 5.0
                elif self._check_due():
                    self._check()
                batch = self._next_batch() if self.mode == "infdb" else None
                if batch:
                    self._process(*batch)
                    if self._generate_running():
                        time.sleep(0.1)
                    continue
                self._finish_pass()
                self._save()
                self._wake.wait(timeout=2.0)
                self._wake.clear()
            except psycopg2.OperationalError as exc:
                self._fail(f"Database not reachable: {str(exc).strip().splitlines()[0] if str(exc).strip() else exc}")
                self._close()
                self._stop.wait(backoff)
                backoff = min(300.0, backoff * 2)
            except Exception as exc:  # noqa: BLE001 - never let the worker die
                log.exception("coverage worker error")
                self._fail(f"{type(exc).__name__}: {exc}")
                self._close()
                self._stop.wait(backoff)
                backoff = min(300.0, backoff * 2)
        self._close()

    def _fail(self, message: str) -> None:
        with self._cond:
            self._check_requested = True
            self.error = message
            self.state = "error"
            for t in self.tables.values():
                if t.rows or t.computed:
                    t.stale = True
            self._publish()

    def _connect(self) -> None:
        from pylovo import config_loader as cl

        self.settings = {"host": cl.HOST, "port": str(cl.PORT), "dbname": cl.DBNAME, "use_infdb": bool(cl.USE_INFDB),
                         "infdb_source_schema": cl.INFDB_SOURCE_SCHEMA, "infdb_opendata_schema": cl.INFDB_OPENDATA_SCHEMA}
        self.mode = "infdb" if cl.USE_INFDB else "files"
        search_path = f"{cl.INFDB_SOURCE_SCHEMA},public" if cl.USE_INFDB and cl.INFDB_SOURCE_SCHEMA else "pylovo,public"
        options = (f"-c search_path={search_path} -c statement_timeout={CHUNK_TIMEOUT_MS} "
                   f"-c lock_timeout={LOCK_TIMEOUT_MS} -c default_transaction_read_only=on {self.conn_options}")
        conn = psycopg2.connect(dbname=cl.DBNAME, user=cl.DBUSER, password=cl.PASSWORD, host=cl.HOST, port=cl.PORT,
                                connect_timeout=5, application_name="pylovo-api-coverage", options=options.strip())
        conn.autocommit = True
        self._conn = conn

    def _close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _query(self, sql: str, params: Any = None) -> list[tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def _signature(self) -> dict:
        s = self.settings
        return {"format": FORMAT_VERSION, "mode": self.mode, "host": s.get("host"), "port": s.get("port"),
                "dbname": s.get("dbname"), "source_schema": s.get("infdb_source_schema"),
                "opendata_schema": s.get("infdb_opendata_schema"), "rules": RULES_HASH,
                "data_dir": str(ui_data_dir(self.root)) if self.mode == "files" else None}

    def _initialize(self) -> None:
        cached = self._load() if self.mode == "infdb" else None
        self._read_sources(force=True)
        if self.mode == "files":
            with self._cond:
                self.initialized = True
                self.error = None
                self.state = "ready"
                self._last_check = time.time()
                self._publish()
            return
        probes = self._probe()
        with self._cond:
            for name, t in self.tables.items():
                self._apply_probe(t, probes.get(name), cached.get(name) if cached else None)
        self._refresh_keys(initial=True)
        self._read_changelog()
        with self._cond:
            self._apply_ttl()
            for t in self.tables.values():
                if t.strategy == "full":      # cheap: serve the cache and recount right away
                    t.pending[FULL] = min(t.pending.get(FULL, 9), 1 if FULL not in t.computed else 3)
            self.initialized = True
            self.error = None
            self.state = "building" if self._has_backlog() else "ready"
            self._pass_started, self._pass_done0 = time.time(), sum(t.done() for t in self.tables.values())
            self._last_check = time.time()
            self._publish()

    def _check_due(self) -> bool:
        if self._check_requested:
            return True
        now = time.time()
        return now - self._last_check >= CHECK_INTERVAL_S and now - self._last_seen < CLIENT_IDLE_S

    def _check(self) -> None:
        """Change detection: catalog, key sets, changelog, TTL and the cheap pylovo sources."""
        with self._lock:
            self._check_requested = False
            requeue_all = self._requeue_all
            self._requeue_all = False
        self._read_sources(force=requeue_all)
        if self.mode == "infdb":
            probes = self._probe()
            with self._cond:
                for name, t in self.tables.items():
                    self._apply_probe(t, probes.get(name), None)
            self._refresh_keys()
            self._read_changelog()
            with self._cond:
                self._apply_ttl()
                for t in self.tables.values():
                    if t.strategy == "full":          # one cheap statement: always recount
                        t.pending[FULL] = min(t.pending.get(FULL, 9), 3)
                    if requeue_all:
                        for key in (t.keys if not t.full else [FULL]):
                            t.pending[key] = min(t.pending.get(key, 9), 3)
                        t.backoff.clear()
                if self._has_backlog() and self.state != "building":
                    self.state = "building"
                    self._pass_started, self._pass_done0 = time.time(), sum(t.done() for t in self.tables.values())
        with self._cond:
            self._last_check = time.time()
            self.error = None
            if self.state == "error":
                self.state = "building" if self._has_backlog() else "ready"
            self._publish()

    def _has_backlog(self) -> bool:
        return any(t.pending for t in self.tables.values() if t.strategy in ("ags", "full", "full_large"))

    def _finish_pass(self) -> None:
        with self._cond:
            if self.state == "building" and not self._next_batch_possible():
                self.state = "ready"
                self._dirty = True
                self._publish()

    def _next_batch_possible(self) -> bool:
        now = time.time()
        for t in self.tables.values():
            if t.strategy == "full_large" and t.pending and (FULL not in t.computed):
                return True
            if t.strategy in ("ags", "full") and any(not (t.backoff.get(k) and t.backoff[k][1] > now) for k in t.pending):
                return True
        return False

    def _publish(self) -> None:
        """New data is visible: bump the version and wake ``ensure`` callers (lock held)."""
        self.version += 1
        self.published_at = time.time()
        self._cond.notify_all()

    # ------------------------------------------------------------------ worker: catalog
    def _probe(self) -> dict[str, dict | None]:
        out: dict[str, dict | None] = {}
        for name, spec in TABLES.items():
            rows = self._query(
                """SELECT quote_ident(n.nspname) || '.' || quote_ident(c.relname), c.relname, c.reltuples::bigint,
                          pg_relation_size(c.oid), c.relfilenode, has_table_privilege(c.oid, 'SELECT'),
                          (SELECT json_object_agg(a.attname, format_type(a.atttypid, a.atttypmod)) FROM pg_attribute a
                            WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
                          (SELECT json_object_agg(a.attname, i.indexrelid::regclass::text) FROM pg_index i
                             JOIN pg_class ic ON ic.oid = i.indexrelid JOIN pg_am am ON am.oid = ic.relam
                             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0]
                            WHERE i.indrelid = c.oid AND am.amname = 'btree' AND i.indisvalid AND i.indisready
                              AND i.indpred IS NULL AND i.indkey[0] <> 0)
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.oid = to_regclass(%s)""", (spec["rel"],))
            if not rows:
                out[name] = None
                continue
            rel, relname, est, size, filenode, can_select, cols, indexes = rows[0]
            out[name] = {"rel": rel, "relname": relname, "est_rows": int(est), "bytes": int(size),
                         "relfilenode": int(filenode), "can_select": bool(can_select), "cols": cols or {},
                         "indexes": indexes or {}}
        return out

    def _apply_probe(self, t: TableState, probe: dict | None, cached: dict | None) -> None:
        """Pick the strategy of a table; a changed relation (or strategy) drops its counters."""
        now = time.time()
        t.last_check = now
        if probe is None or not probe["can_select"] or "postcode" not in probe["cols"]:
            reason = ("not found" if probe is None else "no SELECT privilege" if not probe["can_select"]
                      else "no postcode column")
            if t.strategy != "missing":
                t.reset()
            t.strategy, t.reason, t.rel = "missing", reason, probe["rel"] if probe else None
            return
        key_col = next((c for c in TABLES[t.name]["keys"] if c in probe["cols"]), None)
        index = probe["indexes"].get(key_col) if key_col else None
        small = probe["est_rows"] < self.small_rows if probe["est_rows"] >= 0 else probe["bytes"] < SMALL_TABLE_BYTES
        strategy = "full" if small else "ags" if index else "full_large"
        reason = None
        if strategy == "ags" and not self._index_plan_ok(t.name, probe, key_col):
            strategy, reason = "full_large", f"the planner does not use {index} for the per-municipality statements"
        elif strategy == "full_large":
            reason = f"no btree index on {key_col or 'a municipality key column'}"
        changed = t.strategy != "unknown" and (t.relfilenode, t.strategy, t.key_col) != (
            probe["relfilenode"], strategy, key_col)
        t.rel, t.relname, t.est_rows, t.bytes = probe["rel"], probe["relname"], probe["est_rows"], probe["bytes"]
        t.key_col, t.key_type, t.index, t.reason = key_col, probe["cols"].get(key_col) if key_col else None, index, reason
        if cached is not None:
            if (cached.get("relfilenode") == probe["relfilenode"] and cached.get("strategy") == strategy
                    and cached.get("key_col") == key_col):
                self._restore(t, cached)
            else:
                log.info("coverage cache of %s ignored (table changed)", t.name)
        elif changed:
            log.info("coverage: %s changed (relfilenode or strategy); recounting", t.name)
            # keep the key set: the next key listing diffs against it and drops vanished keys
            t.stale = bool(t.rows)
            t.computed, t.markers, t.backoff, t.pending = {}, {}, {}, {}
        elif not t.error:
            t.stale = False
        t.strategy = strategy
        t.relfilenode = probe["relfilenode"]
        if t.full and FULL not in t.computed:
            t.pending[FULL] = min(t.pending.get(FULL, 9), 1)

    def _index_plan_ok(self, table: str, probe: dict, key_col: str) -> bool:
        """EXPLAIN guard: the key enumeration and a chunk statement must not seq-scan the table."""
        rel = probe["rel"]
        try:
            sample = [r[0] for r in self._query(f"SELECT {key_col}::text FROM {rel} WHERE {key_col} IS NOT NULL LIMIT 1")]
            plans = [self._query("EXPLAIN (FORMAT JSON) " + _enumerate_sql(rel, key_col))[0][0]]
            if sample:
                sql = _chunk_sql(table, rel, key_col, probe["cols"][key_col], True)
                plans.append(self._query("EXPLAIN (FORMAT JSON) " + sql, {"keys": sample * 3})[0][0])
        except psycopg2.Error as exc:
            log.warning("coverage: EXPLAIN of %s failed: %s", rel, exc)
            return False
        return not any(_plan_has_seq_scan(p, probe["relname"]) for p in plans)

    def _refresh_keys(self, initial: bool = False) -> None:
        """List the key set of every 'ags' table and diff it with the known keys."""
        for t in list(self.tables.values()):
            if t.strategy != "ags":
                continue
            try:
                keys = {str(r[0]) for r in self._query(_enumerate_sql(t.rel, t.key_col))}
            except psycopg2.errors.QueryCanceled:
                with self._cond:
                    t.error = "listing the municipality keys timed out"
                continue
            with self._cond:
                added = keys - t.keys
                removed = t.keys - keys
                if removed:
                    t.merge(sorted(removed), {}, time.time())
                    for key in removed:
                        t.computed.pop(key, None)
                        t.pending.pop(key, None)
                        t.markers.pop(key, None)
                t.keys = keys
                t.index_keys()
                for key in keys:
                    if key not in t.computed:
                        t.pending[key] = min(t.pending.get(key, 9), 3)
                if (added or removed) and not initial:
                    log.info("coverage: %s keys +%d -%d", t.name, len(added), len(removed))
                t.error = None
                self._publish()

    def _read_changelog(self) -> None:
        """Requeue keys whose municipality has a new ``public.changelog`` row of its InfDB tool."""
        try:
            present = self._query("SELECT to_regclass('public.changelog') IS NOT NULL")[0][0]
            rows = self._query(
                """SELECT tool, ags::text, max(id), max(modified_at) FROM public.changelog
                   WHERE tool = ANY(%s) AND ags IS NOT NULL GROUP BY 1, 2""",
                (sorted(set(CHANGELOG_TOOLS.values())),)) if present else []
        except psycopg2.Error as exc:
            log.info("coverage: public.changelog not usable: %s", str(exc).splitlines()[0])
            present, rows = False, []
        marks: dict[tuple[str, int], tuple[int, float | None]] = {}
        for tool, ags, last_id, last_at in rows:
            a = key_to_ags(ags)
            if a is not None:
                marks[(tool, a)] = (int(last_id), last_at.timestamp() if hasattr(last_at, "timestamp") else None)
        with self._cond:
            self.changelog_present = bool(present)
            self.changelog = marks
            for name, t in self.tables.items():
                tool = CHANGELOG_TOOLS.get(name)
                if t.strategy != "ags" or not tool:
                    continue
                for (mtool, ags), (last_id, _) in marks.items():
                    if mtool != tool:
                        continue
                    for key in t.by_ags.get(ags, ()):
                        seen = t.markers.get(key)
                        if seen is None and key in t.computed:
                            t.markers[key] = last_id          # first sight: counters are newer than the row
                        elif seen is not None and last_id > seen:
                            t.pending[key] = min(t.pending.get(key, 9), 2)

    def _apply_ttl(self) -> None:
        cutoff = time.time() - TTL_S
        for t in self.tables.values():
            if t.strategy == "ags":
                for key, at in t.computed.items():
                    if at < cutoff:
                        t.pending[key] = min(t.pending.get(key, 9), 3)
            elif t.strategy == "full_large" and t.computed.get(FULL, cutoff) < cutoff:
                t.pending[FULL] = min(t.pending.get(FULL, 9), 3)

    # ------------------------------------------------------------------ worker: sources
    def _read_sources(self, force: bool = False) -> None:
        """pylovo.postcode, the municipal register and the InfDB postcode list (cheap, token-checked)."""
        try:
            tok = self._query(
                """SELECT (SELECT count(*) || ':' || COALESCE(sum(plz), 0) FROM pylovo.postcode WHERE geom IS NOT NULL),
                          (SELECT count(*) || ':' || COALESCE(sum(plz::bigint * 31 + ags), 0) FROM pylovo.municipal_register)""")[0]
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.InvalidSchemaName) as exc:
            with self._cond:
                self.sources_error = f"The pylovo schema is not set up ({str(exc).splitlines()[0]})."
                self.local_bbox, self.register, self.tokens = {}, {}, {}
                self._publish()
            return
        tokens = {"postcode": tok[0], "register": tok[1]}
        od = self.settings.get("infdb_opendata_schema")
        if self.mode == "infdb" and od:
            rel = f'"{od}".postcodes_germany'
            try:
                if self._query("SELECT to_regclass(%s) IS NOT NULL", (rel,))[0][0]:
                    tokens["opendata"] = self._query(f"SELECT count(*) || ':' || max(plz) FROM {rel} WHERE geom IS NOT NULL")[0][0]
            except psycopg2.Error:
                pass
        if self.mode == "files":
            tokens["files"] = self._files_token()
        if not force and tokens == self.tokens:
            return
        bbox = {int(r[0]): (float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in self._query(
            """SELECT plz, ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e)
               FROM (SELECT plz, ST_Transform(ST_Envelope(geom), 4326) AS e FROM pylovo.postcode WHERE geom IS NOT NULL) s""")}
        register: dict[int, list[int]] = {}
        names: dict[int, str] = {}
        for plz, ags, name in self._query("SELECT plz, ags, name_city FROM pylovo.municipal_register ORDER BY plz, ags"):
            register.setdefault(int(plz), []).append(int(ags))
            names[int(ags)] = name
        polygons: set[str] = set()
        if "opendata" in tokens:
            polygons = {str(r[0]).strip() for r in self._query(
                f'SELECT plz FROM "{od}".postcodes_germany WHERE geom IS NOT NULL')}
        files = self._read_files(register) if self.mode == "files" else {}
        with self._cond:
            self.local_bbox, self.register, self.ags_names, self.infdb_polygons = bbox, register, names, polygons
            self.tokens = tokens
            self.sources_error = None
            if self.mode == "files":
                self.files = files
            self._publish()

    def _files_token(self) -> str:
        directory = ui_data_dir(self.root) / "buildings"
        try:
            entries = sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in directory.glob("*.shp"))
        except OSError:
            entries = []
        try:
            ways = self._query("SELECT to_regclass('pylovo.ways') IS NOT NULL")[0][0]
            ways_tok = self._query("SELECT c.relfilenode || ':' || pg_relation_size(c.oid) FROM pg_class c "
                                   "WHERE c.oid = 'pylovo.ways'::regclass")[0][0] if ways else "none"
            log_tok = self._query("SELECT count(*) || ':' || COALESCE(sum(ags), 0) FROM pylovo.ags_log")[0][0] \
                if self._query("SELECT to_regclass('pylovo.ags_log') IS NOT NULL")[0][0] else "none"
        except psycopg2.Error:
            ways_tok = log_tok = "error"
        return hashlib.sha1(json.dumps([entries, ways_tok, log_tok]).encode()).hexdigest()

    def _read_files(self, register: dict[int, list[int]] | None = None) -> dict:
        """File mode: which AGS have building shapefiles, which are imported, whether ways exist."""
        data_dir = ui_data_dir(self.root)
        files = glob.glob(str(data_dir / "buildings" / "*.shp"))
        register_ags = {a for agss in (register if register is not None else self.register).values() for a in agss}
        res_ags, oth_ags = set(), set()
        for path in files:
            base = os.path.basename(path)
            # import_buildings: a file belongs to an AGS if str(ags) is in its name; 'Oth' before 'Res'
            kind = "oth" if "Oth" in path else "res" if "Res" in path else None
            if kind is None:
                continue
            for ags in register_ags:
                if str(ags) in base:
                    (oth_ags if kind == "oth" else res_ags).add(ags)
        try:
            logged = [int(r[0]) for r in self._query("SELECT ags FROM pylovo.ags_log")] \
                if self._query("SELECT to_regclass('pylovo.ags_log') IS NOT NULL")[0][0] else []
            ways_rows = self._query("SELECT EXISTS (SELECT 1 FROM pylovo.ways)")[0][0] \
                if self._query("SELECT to_regclass('pylovo.ways') IS NOT NULL")[0][0] else False
        except psycopg2.Error:
            logged, ways_rows = [], False
        return {"data_dir": str(data_dir), "shapefiles": len(files), "res_ags": sorted(res_ags),
                "oth_ags": sorted(oth_ags), "ags_log": sorted(set(logged)), "ags_with_res": len(res_ags),
                "ags_logged": len(set(logged)), "ways_rows": bool(ways_rows), "checked_at": time.time()}

    # ------------------------------------------------------------------ worker: counting
    def _process(self, t: TableState, keys: list[str]) -> None:
        full = t.full
        sql = _chunk_sql(t.name, t.rel, t.key_col, t.key_type, not full)
        started = time.monotonic()
        try:
            with self._conn.cursor() as cur:
                if t.strategy == "full_large":
                    cur.execute("SET statement_timeout = %s", (FULL_TIMEOUT_S * 1000,))
                try:
                    cur.execute(sql, {"keys": keys} if not full else None)
                    rows = cur.fetchall()
                finally:
                    if t.strategy == "full_large":
                        cur.execute("RESET statement_timeout")
        except (psycopg2.errors.QueryCanceled, psycopg2.errors.LockNotAvailable,
                psycopg2.errors.SerializationFailure, psycopg2.errors.DeadlockDetected) as exc:
            self._batch_failed(t, keys, str(exc).strip().splitlines()[0])
            return
        except (psycopg2.errors.UndefinedTable, psycopg2.errors.UndefinedColumn, psycopg2.errors.UndefinedFunction,
                psycopg2.errors.InsufficientPrivilege, psycopg2.errors.DatatypeMismatch) as exc:
            with self._cond:
                t.error = str(exc).strip().splitlines()[0]
                t.stale = bool(t.rows)
                for key in keys:
                    t.backoff[key] = (99, time.time() + 900, t.error)
                self._check_requested = True
                self._publish()
            return
        took = time.monotonic() - started
        result: dict[str, dict[int, list[int]]] = {}
        for row in rows:
            plz = as_plz(row[1])
            if plz is None:
                continue
            per_key = result.setdefault(str(row[0]), {})
            per_key[plz] = _vadd(per_key.get(plz), [int(v or 0) for v in row[2:]])
        now = time.time()
        with self._cond:
            if full:
                t.merge(sorted(set(t.rows) | set(result)), result, now)
                t.keys = set(result)
                t.index_keys()
                t.computed[FULL] = now
                t.pending.pop(FULL, None)
            else:
                t.merge(keys, result, now)
                for key in keys:
                    if t.pending.get(key) is not None:
                        t.pending.pop(key, None)
                    t.backoff.pop(key, None)
                    tool = CHANGELOG_TOOLS.get(t.name)
                    ags = key_to_ags(key)
                    mark = self.changelog.get((tool, ags)) if tool and ags is not None else None
                    if mark:
                        t.markers[key] = mark[0]
                self.chunk = max(1, self.chunk // 2) if took > 5 else min(CHUNK_MAX, self.chunk * 2) if took < 0.5 \
                    else self.chunk
            t.error = None
            if t.stale and t.complete():
                t.stale = False
            self._dirty = True
            self._publish()

    def _batch_failed(self, t: TableState, keys: list[str], message: str) -> None:
        with self._cond:
            if len(keys) > 1:
                self.chunk = max(1, len(keys) // 2)     # retry the keys in smaller statements
            else:
                fails = t.backoff.get(keys[0], (0, 0, ""))[0] + 1
                t.backoff[keys[0]] = (fails, time.time() + min(900, 60 * 2 ** (fails - 1)), message)
            t.error = message if len(keys) == 1 else None
            self._publish()

    # ------------------------------------------------------------------ persistence
    def _save(self, force: bool = False) -> None:
        if not self.cache_file or self.mode != "infdb" or not self.initialized:
            return
        if not force and (not self._dirty or time.time() - self._last_save < SAVE_INTERVAL_S):
            return
        with self._lock:
            data = {"signature": self._signature(), "saved_at": time.time(), "tables": {
                name: {"strategy": t.strategy, "relfilenode": t.relfilenode, "key_col": t.key_col,
                       "keys": sorted(t.keys), "computed": t.computed, "markers": t.markers,
                       "rows": {k: {str(p): c for p, c in v.items()} for k, v in t.rows.items()}}
                for name, t in self.tables.items() if t.strategy in ("ags", "full", "full_large")}}
            self._dirty = False
            self._last_save = time.time()
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.cache_file)
        except OSError as exc:
            log.warning("coverage cache not written: %s", exc)

    def _load(self) -> dict | None:
        if not self.cache_file or not self.cache_file.exists():
            return None
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if data.get("signature") != self._signature():
                log.info("coverage cache ignored (different database, schema or rules)")
                return None
            return data.get("tables") or {}
        except (OSError, ValueError, AttributeError) as exc:
            log.warning("coverage cache ignored: %s", exc)
            return None

    @staticmethod
    def _restore(t: TableState, cached: dict) -> None:
        t.reset()
        t.keys = set(cached.get("keys") or [])
        t.index_keys()
        t.computed = {str(k): float(v) for k, v in (cached.get("computed") or {}).items()}
        t.markers = {str(k): int(v) for k, v in (cached.get("markers") or {}).items()}
        rows = {str(k): {int(p): list(c) for p, c in v.items()} for k, v in (cached.get("rows") or {}).items()}
        keys = sorted(rows)
        full = cached.get("strategy") in ("full", "full_large")
        computed = dict(t.computed)
        t.merge(keys, rows, time.time())
        t.computed = computed if not full else {k: v for k, v in computed.items() if k == FULL}


# --------------------------------------------------------------------------- SQL builders
def _enumerate_sql(rel: str, col: str) -> str:
    """Loose index scan: the distinct keys of an indexed column in about one index probe per key."""
    return f"""WITH RECURSIVE k AS (
        (SELECT t.{col} AS key FROM {rel} t WHERE t.{col} IS NOT NULL ORDER BY t.{col} LIMIT 1)
        UNION ALL
        SELECT (SELECT t.{col} FROM {rel} t WHERE t.{col} > k.key ORDER BY t.{col} LIMIT 1) FROM k WHERE k.key IS NOT NULL)
      SELECT key FROM k WHERE key IS NOT NULL"""


def _chunk_sql(table: str, rel: str, key_col: str | None, key_type: str | None, by_keys: bool) -> str:
    """Counters per (key, postcode) of a table, for a batch of keys (``%(keys)s``) or the whole table."""
    if key_col is not None and key_col not in KEY_COLUMNS:
        raise ValueError(f"unexpected key column {key_col}")
    alias = "b" if table == "buildings" else "t"
    key_expr = f"{alias}.{key_col}::text" if key_col else "'*'"
    where = f"{alias}.postcode IS NOT NULL"
    if by_keys:
        where = f"{alias}.{key_col} = ANY(CAST(%(keys)s AS text[])::{key_type}[]) AND " + where
    if table == "buildings":
        return f"""SELECT {key_expr} AS key, b.postcode AS plz, count(*),
                   count(*) FILTER (WHERE c.cand), count(*) FILTER (WHERE c.cand AND c.addr),
                   count(*) FILTER (WHERE c.cand AND c.res), count(*) FILTER (WHERE c.cand AND c.res AND c.addr),
                   count(*) FILTER (WHERE b.building_use_id = '{STATION_USE_ID}'),
                   count(*) FILTER (WHERE c.cand AND c.bad), count(*) FILTER (WHERE c.cand AND c.addr AND c.bad),
                   count(*) FILTER (WHERE c.cand AND c.res AND c.bad),
                   count(*) FILTER (WHERE c.cand AND c.res AND c.addr AND c.bad)
            FROM {rel} b
            CROSS JOIN LATERAL (SELECT {CAND_SQL} AS cand, {ADDR_SQL} AS addr, {RES_SQL} AS res, {BAD_SQL} AS bad) c
            WHERE {where} GROUP BY 1, 2"""
    return f"""SELECT {key_expr} AS key, t.postcode AS plz, count(*) FILTER (WHERE {WAY_SQL})
            FROM {rel} t WHERE {where} GROUP BY 1, 2"""


# --------------------------------------------------------------------------- status texts
TEXTS: dict[str, tuple[str, str]] = {
    # status: (severity, short)
    "ready": ("ok", "input ok"),
    "pending": ("pending", "checking…"),
    "unknown": ("unknown", "not verified"),
    "no_setup": ("block", "setup needed"),
    "no_source": ("block", "InfDB missing"),
    "no_geometry": ("block", "no polygon"),
    "not_in_register": ("block", "not in register"),
    "no_buildings": ("block", "no buildings"),
    "no_consumers": ("block", "no consumers"),
    "too_few_buildings": ("block", "1 building"),
    "no_residential": ("block", "no residential"),
    "no_streets": ("block", "no streets"),
    "no_connection_lines": ("block", "no connections"),
}


def status_texts() -> dict[str, dict[str, str]]:
    """Short labels of every status (sent once with the postcode layer)."""
    return {status: {"short": short, "severity": severity} for status, (severity, short) in TEXTS.items()}


def _finish(out: dict, flags: dict[str, bool]) -> dict:
    """Add severity, short text, reason and actions to an InputStatus."""
    status = out["status"]
    severity, short = TEXTS[status]
    plz = out["plz"]
    counts = out.get("counts") or {}
    ags = out.get("ags") or []
    ra, ro = flags["exclude_buildings_without_address"], flags["residential_only"]
    actions = out["actions"]
    reason = ""
    if status == "ready":
        reason = (f"{counts.get('buildings_importable', 0):,} buildings · {counts.get('street_segments', 0):,} street "
                  f"segments · {counts.get('connection_lines', 0):,} connection lines").replace(",", " ")
        short = f"{counts.get('buildings_importable', 0):,} buildings".replace(",", " ")
        if any(w["code"].startswith("partial") for w in out["warnings"]):
            severity, short = "warn", "partial"
        elif any(w["code"] == "data_quality" for w in out["warnings"]):
            severity, short = "warn", "data warning"
    elif status == "pending":
        reason = "The input data of this PLZ is being checked."
    elif status == "unknown":
        reason = (f"The input data could not be checked ({out.get('error') or 'no data'}); pylovo-generate fails for "
                  "PLZ without buildings or streets.")
        actions.append({"kind": "retry", "label": "Check again"})
    elif status == "no_setup":
        reason = out.get("error") or "The pylovo schema is not set up."
        actions.append({"kind": "step", "step": "database", "label": "Open the Database step"})
    elif status == "no_source":
        tables = ", ".join(out.get("tables") or [])
        reason = (f"InfDB input table {tables} not found or not readable (search_path "
                  f"{out.get('source_schema')},public). Every PLZ is blocked.")
        actions.append({"kind": "step", "step": "database", "label": "Open the Database step"})
    elif status == "no_geometry":
        if out.get("files"):
            reason = f"PLZ {plz} is not in pylovo.postcode."
        else:
            reason = f"PLZ {plz} is neither in pylovo.postcode nor in {out.get('opendata_schema')}.postcodes_germany."
        actions.append({"kind": "step", "step": "database", "label": "Run the database setup to load all postcodes"})
    elif status == "not_in_register":
        reason = (f"PLZ {plz} is not in pylovo.municipal_register; in file mode pylovo-generate needs the register "
                  "to find the building shapefiles.")
    elif status == "no_buildings":
        if out.get("files"):
            reason = (f"No building shapefile for AGS {', '.join(ags)} in {out.get('data_dir')}/buildings and none "
                      "imported yet (pylovo.ags_log).")
            actions.append({"kind": "files", "label": f"Copy Res_{ags[0] if ags else '<ags>'}.shp (and Oth_…) into "
                            f"{out.get('data_dir')}/buildings", "path": f"{out.get('data_dir')}/buildings", "ags": ags})
        else:
            reason = f"No building with postcode {plz} in basedata.buildings."
            if ags:
                reason += f" Its municipalities ({', '.join(ags)}) are not processed in InfDB"
                reason += " (preliminary: the full check is still running)." if out.get("preliminary") else "."
            actions.append(_infdb_action("infdb-basedata-buildings", ags))
    elif status == "no_consumers":
        filters = "use Residential/Commercial/Public/Mixed"
        if ra:
            filters += ", street and house number required by EXCLUDE_BUILDINGS_WITHOUT_ADDRESS"
        if ro:
            filters += ", residential only (RESIDENTIAL_ONLY_GENERATION)"
        reason = f"{out.get('total', 0)} buildings, none passes the import filter ({filters})."
        if ra and out.get("plain", 0) > 0:
            actions.append(_config_action("EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", False, f"{out['plain']} buildings"))
        if ro and out.get("candidates", 0) > 0:
            actions.append(_config_action("RESIDENTIAL_ONLY_GENERATION", False, f"{out['candidates']} buildings"))
    elif status == "too_few_buildings":
        reason = ("Only one importable building: pylovo needs at least two to classify the settlement type "
                  "(house-distance metric).")
    elif status == "no_residential":
        reason = ("No residential building" + (" with an address" if ra else "")
                  + ": the settlement type and the transformer sizes cannot be determined.")
        if ra and out.get("residential_plain", 0) > 0:
            actions.append(_config_action("EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", False,
                                          f"{out['residential_plain']} residential buildings"))
    elif status == "no_streets":
        reason = ("No street network: pylovo.ways is empty." if out.get("files") else
                  f"No street segments with postcode {plz} in ways_per_connection (cycle and footpaths are not used).")
        if not out.get("files"):
            actions.append(_infdb_action("infdb-basedata-ways", ags))
    elif status == "no_connection_lines":
        reason = f"No building connection lines with postcode {plz} in connection_lines."
        actions.append(_infdb_action("infdb-basedata-ways", ags))
    for w in out["warnings"]:
        if w["code"].startswith("partial:") and w.get("tool"):
            actions.append(_infdb_action(w["tool"], w.get("ags") or []))
    if severity == "block" and not ENFORCE:
        out["enforced"] = False
        severity = "warn"
    out.update(severity=severity, short=short, reason=reason, selectable=severity in SEVERITY_SELECTABLE,
               verified=severity not in ("pending", "unknown"))
    return out


def _infdb_action(tool: str, ags: list[str]) -> dict:
    label = f"Process in InfDB: {tool}" + (f" for AGS {', '.join(ags)}" if ags else "")
    return {"kind": "infdb", "tool": tool, "ags": ags, "label": label, "copy": " ".join(ags) if ags else tool}


def _config_action(key: str, value: bool, gain: str) -> dict:
    return {"kind": "config", "key": key, "value": value,
            "label": f"Set {key} = {'True' if value else 'False'} ({gain} would pass)"}


def brief(status: dict) -> dict:
    """The short InputStatus embedded in search rows and map features."""
    return {"status": status["status"], "severity": status["severity"], "selectable": status["selectable"],
            "verified": status["verified"], "short": status["short"]}
