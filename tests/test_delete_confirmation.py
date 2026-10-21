"""The typed confirmation in front of ``pylovo-delete`` and ``pylovo-mv delete``: the database name is typed on a
terminal, ``--yes`` skips it, and a non-interactive stdin without ``--yes`` deletes nothing (no database)."""
import builtins
import sys

import pytest

from pylovo import config_loader
from pylovo.cli import delete
from pylovo.cli import mv as mv_cli
from pylovo.cli.confirm import confirm_deletion


class Stdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def terminal(monkeypatch, tty, answer=None):
    """Make stdin a terminal (or not); ``answer`` is typed (an exception is raised as end of input)."""
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def no_input(prompt=""):
        raise AssertionError("nothing may be asked here")

    monkeypatch.setattr(sys, "stdin", Stdin(tty))
    monkeypatch.setattr(builtins, "input", fake_input if tty else no_input)
    return prompts


@pytest.fixture(autouse=True)
def target(monkeypatch):
    for module in (delete, config_loader):
        monkeypatch.setattr(module, "DBNAME", "sandbox")
        monkeypatch.setattr(module, "HOST", "db.example")
        monkeypatch.setattr(module, "PORT", "5432")


# --- the question itself ------------------------------------------------------------------------------------------
def test_the_question_names_the_database_and_what_is_deleted(monkeypatch, capsys):
    prompts = terminal(monkeypatch, True, "sandbox")
    assert confirm_deletion(["all of it", "and more"], "sandbox", "db.example", "5432") is True
    shown = capsys.readouterr().out
    assert "sandbox (host db.example, port 5432)" in shown and "- all of it" in shown and "- and more" in shown
    assert "'sandbox'" in prompts[0]


@pytest.mark.parametrize("answer, expected", [("sandbox", True), ("  sandbox \n", True), ("Sandbox", False),
                                              ("", False), ("yes", False), (EOFError(), False),
                                              (KeyboardInterrupt(), False)])
def test_only_the_exact_database_name_confirms(monkeypatch, capsys, answer, expected):
    terminal(monkeypatch, True, answer)
    assert confirm_deletion(["x"], "sandbox", "db.example", "5432") is expected
    assert ("Aborted" in capsys.readouterr().out) is (not expected)


@pytest.mark.parametrize("stdin", [Stdin(False), None])
def test_a_non_interactive_stdin_is_refused_without_a_question(monkeypatch, capsys, stdin):
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(builtins, "input", lambda prompt="": pytest.fail("a non-interactive stdin must not be asked"))
    assert confirm_deletion(["x"], "sandbox", "db.example", "5432") is False
    assert capsys.readouterr().out.startswith("✗ stdin is not interactive")


# --- pylovo-delete ------------------------------------------------------------------------------------------------
COMMANDS = [["networks", "--plz", "80803", "--version", "1"], ["version", "--version", "1", "2"], ["transformers"],
            ["classification", "--version", "3"]]


@pytest.fixture
def opened(monkeypatch):
    """The commands that reached the database client."""
    recorded = []

    class Client:
        def __enter__(self):
            recorded.append("open")
            return self

        def __exit__(self, *exc):
            return False

        def delete_plz_from_all_tables(self, plz, version_id):
            pass

        def delete_versions_from_all_tables(self, version_ids):
            return len(version_ids)

        def delete_transformers(self):
            pass

        def delete_classification_version_from_related_tables(self, classification_id):
            pass

    monkeypatch.setattr(delete, "DatabaseClient", Client)
    return recorded


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pylovo-delete", *args])
    delete.main()


@pytest.mark.parametrize("command", COMMANDS)
def test_without_yes_on_a_non_interactive_stdin_nothing_is_deleted(monkeypatch, opened, capsys, command):
    terminal(monkeypatch, False)
    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, *command)
    assert refused.value.code == 1 and opened == []
    assert "add --yes" in capsys.readouterr().out


@pytest.mark.parametrize("command", COMMANDS)
def test_typing_the_database_name_deletes(monkeypatch, opened, capsys, command):
    terminal(monkeypatch, True, "sandbox")
    run(monkeypatch, *command)
    assert opened == ["open"] and "✓ Deleted" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "yes", "other", EOFError()])
def test_a_wrong_answer_deletes_nothing(monkeypatch, opened, answer):
    terminal(monkeypatch, True, answer)
    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, "version", "--version", "1")
    assert refused.value.code == 1 and opened == []


@pytest.mark.parametrize("command", COMMANDS)
def test_yes_skips_the_question(monkeypatch, opened, command):
    terminal(monkeypatch, False)          # asking would fail the test
    run(monkeypatch, *command, "--yes")
    assert opened == ["open"]


def test_the_transformers_question_says_that_manual_and_dso_positions_go_too(monkeypatch, opened, capsys):
    terminal(monkeypatch, True, "sandbox")
    run(monkeypatch, "transformers")
    shown = capsys.readouterr().out
    assert "ALL rows of pylovo.transformers" in shown and "manual and DSO positions" in shown


# --- pylovo-mv delete ---------------------------------------------------------------------------------------------
@pytest.fixture
def mv_deleted(monkeypatch):
    deleted = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def ensure_grid_persistence_schema(self):
            pass

        def delete_variants(self, version, variants):
            deleted.append((version, variants))
            return 1

    monkeypatch.setattr(mv_cli, "_database_client", Client)
    return deleted


def test_mv_delete_without_yes_on_a_non_interactive_stdin_deletes_nothing(monkeypatch, mv_deleted, capsys):
    terminal(monkeypatch, False)
    with pytest.raises(SystemExit) as refused:
        mv_cli.main(["delete", "--version", "mv1", "--variant", "d0"])
    assert refused.value.code == 1 and mv_deleted == []
    assert capsys.readouterr().out.startswith("✗ stdin is not interactive")


def test_mv_delete_asks_for_the_database_name_and_names_the_variants(monkeypatch, mv_deleted, capsys):
    terminal(monkeypatch, True, "sandbox")
    mv_cli.main(["delete", "--version", "mv1"])
    assert mv_deleted == [("mv1", None)]
    assert "the MV variants of version mv1 (all) with all their results" in capsys.readouterr().out


def test_mv_delete_with_a_wrong_answer_deletes_nothing(monkeypatch, mv_deleted):
    terminal(monkeypatch, True, "nope")
    with pytest.raises(SystemExit) as refused:
        mv_cli.main(["delete", "--version", "mv1", "--variant", "d0"])
    assert refused.value.code == 1 and mv_deleted == []


def test_mv_delete_with_yes_does_not_ask(monkeypatch, mv_deleted):
    terminal(monkeypatch, False)
    mv_cli.main(["delete", "--version", "mv1", "--variant", "d0", "--yes"])
    assert mv_deleted == [("mv1", ["d0"])]
