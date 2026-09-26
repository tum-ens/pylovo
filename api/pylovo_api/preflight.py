"""Pre-flight check of a generation config against the stored snapshot of its version.

``pylovo-generate`` stores the generation parameters of a version once
(``pylovo.version.generation_parameters``) and refuses to run when the configuration no longer
matches that snapshot (``PreprocessingMixin.insert_version_if_not_exists``: "Increment
VERSION_ID before generating grids with the changed configuration"). The UI checks this
*before* a job starts:

1. The snapshot of a config text is built exactly the way pylovo builds it, by pylovo's own
   ``_generation_parameters_snapshot`` - but in a **subprocess** against a temporary copy of
   ``config/`` (like :func:`pylovo_api.config_io._pylovo_import_check`). The server process never
   imports pylovo with a changed configuration.
2. The snapshot is compared with the stored one (same JSON round trip as pylovo), and the
   differing keys are listed with :func:`pylovo_api.queries._flatten`.

Snapshots are cached per config content, so the subprocess only runs after a config change.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import threading
import uuid
from collections import OrderedDict
from typing import Any

from pylovo_api import db
from pylovo_api.settings import paths

_MARK = "PYLOVO_API_SNAPSHOT="
_PROBE = f"""
import json
import pylovo.config_loader as c
from pylovo.database.preprocessing_mixin import PreprocessingMixin
# The snapshot only reads configuration constants; no database connection is opened.
snapshot = PreprocessingMixin._generation_parameters_snapshot(object.__new__(PreprocessingMixin))
snapshot = json.loads(json.dumps(snapshot, allow_nan=False, sort_keys=True))
print({_MARK!r} + json.dumps({{"version_id": str(c.VERSION_ID), "version_comment": c.VERSION_COMMENT,
                                "snapshot": snapshot}}))
"""
_CACHE: OrderedDict[str, dict] = OrderedDict()
_CACHE_SIZE = 24
_LOCK = threading.Lock()        # guards _CACHE
_BUILD_LOCK = threading.Lock()  # one probe at a time: concurrent requests then share the cached result


class SnapshotError(RuntimeError):
    """pylovo could not build the parameter snapshot of a configuration."""


def _config_key(text: str | None) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths().config_dir.glob("*")):
        if path.is_file() and not path.name.endswith(".bak"):
            digest.update(path.name.encode())
            content = text.encode() if text is not None and path.name == "config_generation.yaml" else path.read_bytes()
            digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def config_snapshot(text: str | None = None) -> dict[str, Any]:
    """Generation-parameter snapshot of the saved config, or of ``text`` as config_generation.yaml.

    Returns:
        ``{"version_id", "version_comment", "snapshot"}``.

    Raises:
        SnapshotError: If pylovo rejects the configuration or the probe times out.
    """
    key = _config_key(text)
    with _BUILD_LOCK:
        with _LOCK:
            if key in _CACHE:
                _CACHE.move_to_end(key)
                return _CACHE[key]
        return _build_snapshot(key, text)


def _build_snapshot(key: str, text: str | None) -> dict[str, Any]:
    # The subprocess inherits the database settings of this project from os.environ (pylovo's
    # config_loader needs them to import; the snapshot itself never connects).
    try:
        import pylovo.config_loader  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken saved file is reported by the probe below
        pass
    work = paths().tmp_dir / f"snapshot-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(paths().config_dir, work / "config", ignore=shutil.ignore_patterns("*.bak", "backups"))
        if text is not None:
            (work / "config" / "config_generation.yaml").write_text(text, encoding="utf-8")
        proc = subprocess.run([sys.executable, "-c", _PROBE], cwd=work, capture_output=True, text=True,
                              timeout=180, check=False)
    except subprocess.TimeoutExpired as exc:
        raise SnapshotError("Building the parameter snapshot timed out.") from exc
    finally:
        shutil.rmtree(work, ignore_errors=True)
    line = next((ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith(_MARK)), None)
    if proc.returncode != 0 or line is None:
        errors = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
        raise SnapshotError("pylovo rejects this configuration: " + (errors[-1] if errors else "unknown error"))
    result = json.loads(line[len(_MARK):])
    with _LOCK:
        _CACHE[key] = result
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return result


# --------------------------------------------------------------------------- comparison
def _stored(version_id: str) -> dict[str, Any] | None:
    try:
        row = db.fetch_one("SELECT version_id, version_comment, generation_parameters FROM pylovo.version "
                           "WHERE version_id = %s", (str(version_id),))
    except Exception:  # noqa: BLE001 - schema missing
        return None
    if row and isinstance(row.get("generation_parameters"), str):
        row["generation_parameters"] = json.loads(row["generation_parameters"])
    return row


def differences(a: dict | None, b: dict | None) -> list[dict[str, Any]]:
    """Flattened keys whose values differ between two snapshots (``a`` = stored, ``b`` = config)."""
    from pylovo_api.queries import _flatten

    fa, fb = _flatten(a or {}), _flatten(b or {})
    return [{"key": k, "stored": fa.get(k), "config": fb.get(k)} for k in sorted(set(fa) | set(fb))
            if fa.get(k) != fb.get(k)]


def version_differences(stored: dict | None, snapshot: dict | None) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Differences between a stored snapshot and a config snapshot, as ``pylovo-generate`` sees them.

    Parameters recorded only in newer snapshots (``pylovo.version_snapshot.ADDED_LATER``) do not count
    when an older snapshot lacks them. Returns ``(rows, blocking, not_recorded)``: the rows of
    :func:`differences` that count, the dotted paths that block the version, and the added-later
    paths whose old value is unknown.
    """
    from pylovo.version_snapshot import ADDED_LATER, compare_snapshots

    blocking, not_recorded = compare_snapshots(stored or {}, snapshot or {})
    tolerated = {".".join(path) for path in ADDED_LATER} - set(blocking)
    return [d for d in differences(stored, snapshot) if d["key"] not in tolerated], blocking, not_recorded


def version_ids() -> list[str]:
    try:
        return [r["version_id"] for r in db.fetch_all("SELECT version_id FROM pylovo.version ORDER BY created_at")]
    except Exception:  # noqa: BLE001
        return []


def next_free_version_id(existing: list[str] | None = None, current: str | None = None) -> str:
    """The next unused VERSION_ID (max numeric id + 1; otherwise ``<current>_2``, ``_3``, …)."""
    existing = set(existing if existing is not None else version_ids())
    numeric = [int(v) for v in existing if re.fullmatch(r"\d{1,9}", v)]
    if numeric or not current or re.fullmatch(r"\d{1,9}", current):
        candidate = max(numeric, default=0) + 1
        while str(candidate) in existing:
            candidate += 1
        return str(candidate)
    base = re.sub(r"_\d+$", "", current)[:7]
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def compare(snapshot: dict[str, Any], version_id: str) -> dict[str, Any]:
    """Compare a snapshot with the stored one of ``version_id``.

    Returns:
        ``exists``, ``has_parameters``, ``matches`` (``None`` when there is nothing to compare),
        ``differences`` and the stored ``version_comment``.
    """
    stored = _stored(version_id)
    if not stored:
        return {"exists": False, "has_parameters": False, "matches": None, "differences": []}
    gp = stored.get("generation_parameters")
    if gp is None:  # old version without a snapshot: pylovo backfills it on the next run
        return {"exists": True, "has_parameters": False, "matches": None, "differences": [],
                "version_comment": stored.get("version_comment")}
    # the same rule as pylovo-generate: parameters recorded only in newer snapshots do not block
    diff, blocking, not_recorded = version_differences(gp, snapshot)
    return {"exists": True, "has_parameters": True, "matches": not blocking, "differences": diff,
            "not_recorded": not_recorded, "version_comment": stored.get("version_comment")}


def check(text: str | None = None, base_version_id: str | None = None) -> dict[str, Any]:
    """Pre-flight of the saved config (or of a candidate ``text``) for its own VERSION_ID.

    Args:
        text: Candidate ``config_generation.yaml``; ``None`` checks the saved file.
        base_version_id: Version to describe the candidate against (for the comment of a new
            version). Default: the config's own version if it exists, else the version the
            saved config points at, else the newest version.

    Returns:
        ``version_id``, ``comparison`` (see :func:`compare`), ``next_free_version_id``,
        ``base_version_id`` / ``base_differences`` / ``suggested_comment`` and ``error`` if
        pylovo rejects the configuration.
    """
    from pylovo_api.config_io import current_values

    ids = version_ids()
    try:
        snap = config_snapshot(text)
    except SnapshotError as exc:
        return {"version_id": None, "error": str(exc), "comparison": None,
                "next_free_version_id": next_free_version_id(ids), "versions": ids}
    version_id = snap["version_id"]
    comparison = compare(snap["snapshot"], version_id)
    saved_id = str(current_values().get("VERSION_ID", ""))
    base_id = base_version_id if base_version_id in ids else (
        version_id if comparison["exists"] else saved_id if saved_id in ids else (ids[-1] if ids else None))
    base = _stored(base_id) if base_id else None
    base_diff = version_differences(base.get("generation_parameters"), snap["snapshot"])[0] if base and base.get(
        "generation_parameters") else []
    return {
        "version_id": version_id, "version_comment": snap["version_comment"], "error": None,
        "comparison": comparison, "next_free_version_id": next_free_version_id(ids, version_id), "versions": ids,
        "base_version_id": base_id, "base_differences": base_diff,
        "suggested_comment": summarise(base_diff) if base_diff else None,
    }


# --------------------------------------------------------------------------- labels
def _sizes(mapping: Any) -> str:
    if not isinstance(mapping, dict):
        return str(mapping)
    parts = []
    for key in sorted(mapping, key=str):
        sizes = sorted(mapping[key] or [])
        parts.append(f"{key}:{sizes[0]}-{sizes[-1]}" if len(sizes) > 1 else f"{key}:{sizes[0] if sizes else '-'}")
    return " ".join(parts)


_PHRASES = {
    "transformer_placement.use_open_transformer_positions": lambda v: "open positions " + ("on" if v else "off"),
    "transformer_placement.use_dso_transformer_positions": lambda v: "DSO positions " + ("on" if v else "off"),
    "residential_only_generation": lambda v: "residential only" if v else "all consumers",
    "exclude_buildings_without_address": lambda v: "no address-less buildings" if v else "incl. address-less",
    "transformer_placement.transformer_mapping": lambda v: "sizes " + _sizes(v),
    "transformer_placement.max_buildings_per_kcid": lambda v: f"≤{v} buildings/cluster",
    "transformer_placement.transformer_planning_utilization": lambda v: f"planning {v}",
    "load_calculation.peak_load_household": lambda v: f"{v} kW/household",
    "cable_dimensioning.max_end_to_end_feeder_voltage_drop_percent": lambda v: f"feeder drop {v} %",
    "cable_dimensioning.max_service_design_voltage_drop_percent": lambda v: f"service drop {v} %",
    "connection_point_aggregation.enabled": lambda v: "CP aggregation " + ("on" if v else "off"),
}


def summarise(diff: list[dict[str, Any]], limit: int = 120) -> str:
    """Short comment for a new version from the keys that differ, e.g. ``open positions on; sizes 1:100-250``."""
    parts: list[str] = []
    mapping_prefix = "transformer_placement.transformer_mapping."
    mapping = {row["key"][len(mapping_prefix):]: row["config"] for row in diff if row["key"].startswith(mapping_prefix)}
    if mapping:
        diff = [row for row in diff if not row["key"].startswith(mapping_prefix)] + [
            {"key": "transformer_placement.transformer_mapping", "config": mapping}]
    for row in diff:
        key, value = row["key"], row["config"]
        phrase = _PHRASES.get(key)
        text = phrase(value) if phrase else f"{key.split('.')[-1].split('[')[0]} {json.dumps(value) if isinstance(value, (dict, list)) else value}"
        if text not in parts:
            parts.append(text)
    out = "; ".join(parts)
    return out if len(out) <= limit else out[: limit - 1].rstrip("; ") + "…"


def version_flags(gp: dict | None) -> list[str]:
    """Two or three badges that tell versions apart (positions, consumers, transformer sizes)."""
    if not gp:
        return []
    placement = gp.get("transformer_placement") or {}
    flags = []
    if placement.get("use_dso_transformer_positions"):
        flags.append("DSO positions")
    if placement.get("use_open_transformer_positions"):
        flags.append("open positions")
    if not flags:
        flags.append("greenfield")
    if gp.get("residential_only_generation"):
        flags.append("residential only")
    sizes = sorted({s for v in (placement.get("transformer_mapping") or {}).values() for s in (v or [])})
    if sizes:
        flags.append(f"{sizes[0]}–{sizes[-1]} kVA" if len(sizes) > 1 else f"{sizes[0]} kVA")
    return flags


def attach_version_flags(rows: list[dict]) -> list[dict]:
    """Add ``flags`` (see :func:`version_flags`) to the rows of :func:`pylovo_api.queries.versions`."""
    if not rows:
        return rows
    stored = {r["version_id"]: r["generation_parameters"] for r in db.fetch_all(
        "SELECT version_id, generation_parameters FROM pylovo.version WHERE version_id = ANY(%s)",
        ([r["version_id"] for r in rows],))}
    for row in rows:
        row["flags"] = version_flags(stored.get(row["version_id"]))
    return rows
