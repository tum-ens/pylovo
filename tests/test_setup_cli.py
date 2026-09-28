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


class FakeConstructor:
    """Records the setup steps instead of touching a database."""

    def __init__(self, empty=(), fail_on=None):
        self.calls, self.empty, self.fail_on = [], set(empty), fail_on

    def _record(self, name):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError(f"{name} failed")

    def table_is_empty_or_missing(self, table):
        return table in self.empty

    def migrate_schema(self):
        self._record("migrate")

    def transformers_to_db(self, clear_existing):
        self._record("transformers")

    def load_postcode_from_infdb(self):
        self._record("postcode")

    def load_ways_preprocessing_functions(self):
        self._record("functions")

    def reset_schema(self):
        self._record("reset")


def _fake_setup(monkeypatch, tmp_path, fake):
    monkeypatch.chdir(tmp_path)  # run_setup logs to ./log/log.txt
    monkeypatch.setattr(setup, "DatabaseConstructor", lambda: fake)
    monkeypatch.setattr(setup, "USE_INFDB", True)
    monkeypatch.setattr(setup, "create_municipal_register", lambda: fake._record("register"))


def test_setup_imports_every_empty_reference_table(monkeypatch, tmp_path):
    fake = FakeConstructor(empty={"transformers", "postcode", "municipal_register"})
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["migrate", "transformers", "postcode", "register", "functions"]


def test_rerun_completes_an_interrupted_setup_and_keeps_filled_tables(monkeypatch, tmp_path):
    fake = FakeConstructor(empty={"postcode", "municipal_register"})
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["migrate", "postcode", "register", "functions"]
    fake = FakeConstructor()
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["migrate", "functions"]
