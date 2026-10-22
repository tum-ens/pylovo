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

    def __init__(self, empty=(), fail_on=None, infdb_problem=None):
        self.calls, self.empty, self.fail_on, self.infdb_problem = [], set(empty), fail_on, infdb_problem
        self.backup = "pylovo_backup_x"

    def _record(self, name):
        self.calls.append(name)
        if name == self.fail_on:
            raise RuntimeError(f"{name} failed")

    def table_is_empty_or_missing(self, table):
        return table in self.empty

    def infdb_postcodes_problem(self):
        return self.infdb_problem

    def migrate_schema(self):
        self._record("migrate")

    def transformers_to_db(self, clear_existing):
        self._record("transformers")

    def load_postcode_from_infdb(self):
        self._record("postcode")

    def load_ways_preprocessing_functions(self):
        self._record("functions")

    def backup_schemas(self):
        return []

    def acquire_setup_lock(self):
        self._record("lock")

    def release_setup_lock(self):
        self._record("unlock")

    def move_schema_to_backup(self):
        self._record("backup")
        return self.backup

    def restore_backup(self, backup):
        self._record(f"restore {backup}")

    def drop_backup(self, backup):
        self._record(f"drop {backup}")


def _fake_setup(monkeypatch, tmp_path, fake):
    monkeypatch.chdir(tmp_path)  # run_setup logs to ./log/log.txt
    monkeypatch.setattr(setup, "DatabaseConstructor", lambda: fake)
    monkeypatch.setattr(setup, "USE_INFDB", True)
    monkeypatch.setattr(setup, "create_municipal_register", lambda: fake._record("register"))
    monkeypatch.setattr(setup, "missing_input_files", lambda: [])


def test_setup_imports_every_empty_reference_table(monkeypatch, tmp_path):
    fake = FakeConstructor(empty={"transformers", "postcode", "municipal_register"})
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["lock", "migrate", "transformers", "postcode", "register", "functions", "unlock"]


def test_rerun_completes_an_interrupted_setup_and_keeps_filled_tables(monkeypatch, tmp_path):
    fake = FakeConstructor(empty={"postcode", "municipal_register"})
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["lock", "migrate", "postcode", "register", "functions", "unlock"]
    fake = FakeConstructor()
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["lock", "migrate", "functions", "unlock"]


def test_missing_inputs_stop_setup_and_reset_before_any_change(monkeypatch, tmp_path):
    fake = FakeConstructor(empty={"postcode"}, infdb_problem="the InfDB table opendata.postcodes_germany does not exist")
    _fake_setup(monkeypatch, tmp_path, fake)
    with pytest.raises(RuntimeError, match="nothing was changed.*postcodes_germany does not exist"):
        setup.run_setup()
    with pytest.raises(RuntimeError, match="postcodes_germany"):
        setup.run_setup(reset=True)  # a reset imports every table, so every input is checked
    assert fake.calls == []
    fake = FakeConstructor(infdb_problem="unreachable")  # nothing to import: inputs are not needed
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup()
    assert fake.calls == ["lock", "migrate", "functions", "unlock"]


ALL = {"transformers", "postcode", "municipal_register"}


def test_reset_drops_the_backup_after_a_complete_rebuild(monkeypatch, tmp_path):
    fake = FakeConstructor(empty=ALL)
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup(reset=True)
    assert fake.calls == ["lock", "backup", "migrate", "transformers", "postcode", "register", "functions",
                          "drop pylovo_backup_x", "unlock"]


def test_failed_reset_restores_the_previous_schema(monkeypatch, tmp_path):
    fake = FakeConstructor(empty=ALL, fail_on="postcode")
    _fake_setup(monkeypatch, tmp_path, fake)
    with pytest.raises(RuntimeError, match="postcode failed"):
        setup.run_setup(reset=True)
    assert fake.calls == ["lock", "backup", "migrate", "transformers", "postcode", "restore pylovo_backup_x", "unlock"]


def test_reset_of_a_database_without_schema_has_no_backup(monkeypatch, tmp_path):
    fake = FakeConstructor(empty=ALL)
    fake.backup = None
    _fake_setup(monkeypatch, tmp_path, fake)
    setup.run_setup(reset=True)
    assert "drop None" not in fake.calls and fake.calls[-2:] == ["functions", "unlock"]
