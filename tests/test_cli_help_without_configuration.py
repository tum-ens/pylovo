"""``--help`` and usage errors of every ``pylovo-*`` command work without ``.env`` and configuration files: the
arguments are parsed before ``pylovo.config_loader`` is imported (no database)."""
import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from pylovo.cli import launch

SCRIPTS = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["scripts"]
#: Help of subcommands, too: their parsers are built before the command runs.
SUBCOMMAND_HELP = [("pylovo-delete", "networks"), ("pylovo-import", "transformers-ui"), ("pylovo-mv", "generate")]

#: Runs the entry points in a fresh interpreter in which every import of the configuration fails
#: (``None`` in ``sys.modules``), like a missing ``DBNAME`` or ``config/`` would, and prints what each one did.
PROBE = """
import contextlib, importlib, io, json, sys
sys.modules["pylovo.config_loader"] = None
results = {}
for label, target, argv in json.loads(sys.argv[1]):
    module, function = target.split(":")
    sys.argv = [label.split()[0], *argv, "--help"]
    out, code, error = io.StringIO(), None, ""
    try:
        with contextlib.redirect_stdout(out):
            getattr(importlib.import_module(module), function)()
    except SystemExit as stop:
        code = stop.code
    except BaseException as exc:
        error = f"{type(exc).__name__}: {exc}"
    results[label] = [code, f"usage: {sys.argv[0]}" in out.getvalue(), error]
print(json.dumps(results))
"""


def test_help_of_every_command_and_subcommand_needs_no_configuration():
    cases = [(name, target, []) for name, target in SCRIPTS.items()]
    cases += [(f"{name} {sub}", SCRIPTS[name], [sub]) for name, sub in SUBCOMMAND_HELP]
    done = subprocess.run([sys.executable, "-c", PROBE, json.dumps(cases)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    results = json.loads(done.stdout.splitlines()[-1])
    assert {label: result for label, result in results.items() if result != [0, True, ""]} == {}


@pytest.mark.parametrize("function", [target.split(":")[1] for target in SCRIPTS.values()
                                      if target.startswith("pylovo.cli.launch:")])
def test_every_launcher_entry_runs_the_main_of_a_command_module(function):
    assert callable(importlib.import_module(f"pylovo.cli.{function}").main)


def test_the_arguments_are_parsed_before_the_command_module_is_imported(monkeypatch, capsys):
    imported = []
    command = type("Command", (), {"main": staticmethod(lambda: imported.append("main"))})
    monkeypatch.setattr(importlib, "import_module", lambda name: imported.append(name) or command)

    monkeypatch.setattr(sys, "argv", ["pylovo-delete", "networks", "--version", "1"])      # --plz is missing
    with pytest.raises(SystemExit) as refused:
        launch.delete()
    assert refused.value.code == 2 and imported == []

    monkeypatch.setattr(sys, "argv", ["pylovo-delete"])               # no command: the help, without the module
    launch.delete()
    assert imported == [] and "usage: pylovo-delete" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["pylovo-delete", "transformers"])
    launch.delete()
    assert imported == ["pylovo.cli.delete", "main"]

    imported.clear()
    monkeypatch.setattr(sys, "argv", ["pylovo-delete", "--version", "1"])  # the version option without a subcommand
    launch.delete()
    assert imported == ["pylovo.cli.delete", "main"]
