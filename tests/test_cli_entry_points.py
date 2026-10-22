"""Every ``[project.scripts]`` entry point of pylovo resolves, and the ones that take arguments print their help
without touching the database (a broken import or an argparse typo would otherwise ship unnoticed)."""
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

SCRIPTS = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]["scripts"]
PYLOVO_SCRIPTS = {name: target for name, target in SCRIPTS.items() if target.startswith("pylovo.")}
WITH_HELP = ["pylovo-analyze", "pylovo-delete", "pylovo-export", "pylovo-generate", "pylovo-import", "pylovo-setup"]


@pytest.mark.parametrize("name", sorted(PYLOVO_SCRIPTS))
def test_the_entry_point_resolves_to_a_callable(name):
    module, function = PYLOVO_SCRIPTS[name].split(":")
    assert callable(getattr(importlib.import_module(module), function))


@pytest.mark.parametrize("name", WITH_HELP)
def test_help_prints_the_usage_and_exits_with_zero_without_running_anything(monkeypatch, capsys, name):
    module, function = PYLOVO_SCRIPTS[name].split(":")
    imported = importlib.import_module(module)
    if hasattr(imported, "run_setup"):
        monkeypatch.setattr(imported, "run_setup", lambda *args, **kwargs: pytest.fail("--help must not run the setup"))
    monkeypatch.setattr(sys, "argv", [name, "--help"])
    with pytest.raises(SystemExit) as shown:
        getattr(imported, function)()
    assert shown.value.code == 0 and f"usage: {name}" in capsys.readouterr().out
