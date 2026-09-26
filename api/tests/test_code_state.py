"""The server notices when the code on disk is newer than itself (pylovo_api.code_state)."""
from __future__ import annotations

import os
import time

from pylovo_api import code_state


def test_newest_mtime_and_stale_flag(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython-312.pyc").write_text("")
    (tmp_path / "notes.txt").write_text("")
    old = time.time() - 3600
    os.utime(tmp_path / "a.py", (old, old))
    assert abs(code_state.newest_mtime([tmp_path]) - old) < 1                 # only .py/.sql outside __pycache__

    monkeypatch.setattr(code_state, "_roots", lambda: [tmp_path])
    monkeypatch.setattr(code_state, "STARTED_CODE_MTIME", old)
    monkeypatch.setattr(code_state, "_cache", {"at": 0.0, "value": None})
    assert code_state.state()["stale"] is False
    (tmp_path / "b.sql").write_text("SELECT 1;")                 # a newer backend file appears
    monkeypatch.setattr(code_state, "_cache", {"at": 0.0, "value": None})
    now = code_state.state()
    assert now["stale"] is True and now["code_changed_at"] and now["server_started_at"]


def test_status_reports_the_code_state(client):
    ui = client.get("/api/status").json()["ui"]
    assert set(ui["code"]) == {"stale", "server_started_at", "code_changed_at"}
