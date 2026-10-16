"""Tests of the pylovo-viewer command line (no database, no network)."""
from __future__ import annotations

import socket

import pytest
from pylovo_viewer import cli, etrago


def test_help_touches_nothing(capsys, monkeypatch):
    monkeypatch.setattr("pylovo_viewer.db.Database.__init__", lambda *a, **k: pytest.fail("database opened"))
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--help"])
    assert exit_.value.code == 0 and "read-only" in capsys.readouterr().out.lower()


def test_fetch_etrago(monkeypatch, tmp_path, capsys):
    seen = {}

    def fake_fetch(data_dir, scenario, log):
        seen.update(data_dir=data_dir, scenario=scenario)
        return {"counts": {"lines": 3, "buses": 4}}

    monkeypatch.setattr(etrago, "fetch", fake_fetch)
    cli.main(["--data-dir", str(tmp_path), "fetch", "etrago", "--scenario", "eGon100RE"])
    assert seen == {"data_dir": tmp_path, "scenario": "eGon100RE"}
    assert "3 lines and 4 buses" in capsys.readouterr().out


def test_free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        busy = s.getsockname()[1]
        with pytest.raises(SystemExit, match="in use"):
            cli.free_port("127.0.0.1", busy)
    assert cli.free_port("127.0.0.1", busy) == busy
