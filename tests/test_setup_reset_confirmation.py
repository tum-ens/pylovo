"""The confirmation in front of the CASCADE drop of the ``pylovo`` schema (``pylovo-setup reset``): typed database
name, abort on a non-interactive stdin, end of input and a mismatch (no database)."""
import builtins
import sys

import pytest

from pylovo.cli import setup


class Stdin:
    def __init__(self, tty):
        self.tty = tty

    def isatty(self):
        return self.tty


def no_input(prompt=""):
    raise AssertionError("a non-interactive stdin must not be asked")


def confirm(monkeypatch, stdin, answer=None):
    monkeypatch.setattr(sys, "stdin", stdin)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(builtins, "input", fake_input if stdin is not None and stdin.tty else no_input)
    return setup.confirm_schema_reset("sandbox", "db.example", "5432"), prompts


def test_the_warning_names_the_target_before_anything_is_asked(monkeypatch, capsys):
    confirm(monkeypatch, Stdin(tty=False))
    shown = capsys.readouterr().out
    assert "database: sandbox" in shown and "host:     db.example" in shown and "port:     5432" in shown
    assert "CASCADE" in shown


@pytest.mark.parametrize("stdin", [Stdin(tty=False), None])
def test_a_non_interactive_stdin_aborts_without_asking(monkeypatch, capsys, stdin):
    confirmed, _ = confirm(monkeypatch, stdin)
    assert confirmed is False and "not interactive" in capsys.readouterr().out


@pytest.mark.parametrize("answer, expected", [("sandbox", True), ("  sandbox \n", True), ("Sandbox", False),
                                              ("sandbox2", False), ("", False), ("yes", False)])
def test_only_the_exact_database_name_confirms(monkeypatch, answer, expected):
    confirmed, prompts = confirm(monkeypatch, Stdin(tty=True), answer)
    assert confirmed is expected and "'sandbox'" in prompts[0]


@pytest.mark.parametrize("error", [EOFError(), KeyboardInterrupt()])
def test_end_of_input_and_interrupt_abort(monkeypatch, error):
    confirmed, _ = confirm(monkeypatch, Stdin(tty=True), error)
    assert confirmed is False


@pytest.fixture
def reset_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(setup, "DBNAME", "sandbox")
    monkeypatch.setattr(setup, "HOST", "db.example")
    monkeypatch.setattr(setup, "PORT", "5432")
    monkeypatch.setattr(setup, "run_setup", lambda reset=False: calls.append(reset))
    return calls


def test_a_reset_without_yes_on_a_non_interactive_stdin_changes_nothing(monkeypatch, reset_calls, capsys):
    monkeypatch.setattr(sys, "stdin", Stdin(tty=False))
    with pytest.raises(SystemExit) as aborted:
        setup.main(["reset", "--database", "sandbox"])
    assert aborted.value.code == 1 and reset_calls == []
    assert "Reset aborted. Nothing was changed." in capsys.readouterr().out


def test_a_reset_with_the_typed_name_runs_and_with_a_wrong_name_does_not(monkeypatch, reset_calls):
    monkeypatch.setattr(sys, "stdin", Stdin(tty=True))
    monkeypatch.setattr(builtins, "input", lambda prompt="": "other")
    with pytest.raises(SystemExit):
        setup.main(["reset", "--database", "sandbox"])
    assert reset_calls == []
    monkeypatch.setattr(builtins, "input", lambda prompt="": "sandbox")
    setup.main(["reset", "--database", "sandbox"])
    assert reset_calls == [True]


def test_yes_skips_the_question(monkeypatch, reset_calls):
    monkeypatch.setattr(setup, "confirm_schema_reset", lambda *args: pytest.fail("--yes must not ask"))
    setup.main(["reset", "--database", "sandbox", "--yes"])
    assert reset_calls == [True]


def test_the_only_reset_is_the_one_of_pylovo_setup_with_its_backup():
    from pylovo.database import transformer_sources
    from pylovo.database.database_constructor import DatabaseConstructor

    assert not hasattr(DatabaseConstructor, "reset_schema")      # it dropped the schema without a backup
    assert hasattr(DatabaseConstructor, "move_schema_to_backup") and hasattr(DatabaseConstructor, "drop_backup")
    assert not hasattr(transformer_sources, "any_source")
