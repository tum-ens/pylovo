"""Does the running server still match the code on disk?

After a ``git checkout`` or ``git pull`` the running server still has the old Python code (and
lacks new endpoints) while the UI may already expect the new ones. The server notes the newest
modification time of its Python and SQL files when it starts; ``/api/status`` reports ``stale`` once
newer files appear, and the UI asks for a restart.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pylovo
import pylovo_api

_SUFFIXES = {".py", ".sql"}
_TTL_S = 10.0


def _roots() -> list[Path]:
    return [Path(pylovo_api.__file__).resolve().parent, Path(pylovo.__file__).resolve().parent]


def newest_mtime(roots: list[Path] | None = None) -> float:
    """Newest modification time of the server's Python and SQL files (0 if none can be read)."""
    newest = 0.0
    for root in roots or _roots():
        for path in root.rglob("*"):
            if path.suffix in _SUFFIXES and "__pycache__" not in path.parts:
                try:
                    newest = max(newest, path.stat().st_mtime)
                except OSError:
                    continue
    return newest


def git_revision() -> str | None:
    """Commit of the running code: ``$PYLOVO_REVISION`` (set in the image), else ``git rev-parse HEAD``.

    Returns ``None`` outside a git checkout or without ``git``.
    """
    value = os.getenv("PYLOVO_REVISION", "").strip()
    if not value:
        try:
            done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(pylovo_api.__file__).resolve().parent,
                                  capture_output=True, text=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        value = done.stdout.strip() if done.returncode == 0 else ""
    return value if re.fullmatch(r"[0-9a-f]{7,64}", value) else None


STARTED_AT = time.time()
STARTED_CODE_MTIME = newest_mtime()
STARTED_REVISION = git_revision()
_cache: dict[str, float | dict | None] = {"at": 0.0, "value": None}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def state() -> dict:
    """``{"stale", "server_started_at", "code_changed_at"}`` (checked at most every few seconds)."""
    now = time.time()
    if _cache["value"] is None or now - float(_cache["at"]) > _TTL_S:
        newest = newest_mtime()
        stale = newest > STARTED_CODE_MTIME + 1.0
        _cache.update(at=now, value={"stale": stale, "server_started_at": _iso(STARTED_AT),
                                     "code_changed_at": _iso(newest) if stale else None})
    return dict(_cache["value"])  # type: ignore[arg-type]
