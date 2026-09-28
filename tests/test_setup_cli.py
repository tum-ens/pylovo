"""Setup command must never reset without an explicit target and command."""
import pytest

from pylovo.cli import setup


def test_setup_defaults_to_non_destructive_migration(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "run_setup", lambda reset=False: calls.append(reset))
    setup.main([])
    assert calls == [False]


def test_reset_requires_matching_database(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "DBNAME", "sandbox")
    monkeypatch.setattr(setup, "run_setup", lambda reset=False: calls.append(reset))
    with pytest.raises(SystemExit) as exc:
        setup.main(["reset", "--database", "other", "--yes"])
    assert exc.value.code == 2
    assert calls == []
    setup.main(["reset", "--database", "sandbox", "--yes"])
    assert calls == [True]


def test_help_and_old_yes_flag_cannot_reset(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup, "run_setup", lambda reset=False: calls.append(reset))
    with pytest.raises(SystemExit) as help_exit:
        setup.main(["--help"])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as invalid_exit:
        setup.main(["--yes"])
    assert invalid_exit.value.code == 2
    assert calls == []
    assert "usage: pylovo-setup" in capsys.readouterr().out
