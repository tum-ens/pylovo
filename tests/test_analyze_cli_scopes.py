"""``pylovo-analyze`` runs the PLZ analysis, the per-grid analysis or both for every PLZ, and hands the diagnostics
options on (no database; calculator and diagnostics are recorders)."""
import sys
from pathlib import Path

import pytest

from pylovo.cli import analyze


@pytest.fixture
def events(monkeypatch):
    recorded = []

    class Calculator:
        def analyze_parameters_for_plz(self, plz):
            recorded.append(("plz", plz))

        def analyze_grid_parameters_for_plz(self, plz):
            recorded.append(("grid", plz))

    monkeypatch.setattr(analyze, "ParameterCalculator", Calculator)
    monkeypatch.setattr(analyze, "run_diagnostics", lambda *args: recorded.append(("diagnostics", *args)) or 0)
    return recorded


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pylovo-analyze", *args])
    analyze.main()


@pytest.mark.parametrize("options, expected", [
    ([], [("plz", 80331), ("plz", 80333)]),
    (["--per-grid"], [("grid", 80331), ("grid", 80333)]),
    (["--all"], [("plz", 80331), ("grid", 80331), ("plz", 80333), ("grid", 80333)]),   # per PLZ: parameters, then grids
])
def test_the_analysis_runs_per_plz_in_the_requested_scope(monkeypatch, events, options, expected):
    run(monkeypatch, "--plz", "80331", "80333", *options)
    assert events == expected


def test_the_diagnostics_get_the_plz_the_switches_the_version_and_the_output_directory(monkeypatch, events):
    run(monkeypatch, "--plz", "80331", "--coverage", "--border-diagnostics", "--version-id", "7", "--output", "out/dir")
    assert events == [("diagnostics", [80331], True, True, "7", Path("out/dir"))]

    events.clear()
    run(monkeypatch, "--plz", "80331", "--border-diagnostics")
    assert events == [("diagnostics", [80331], False, True, None, Path("results/diagnostics"))]   # defaults


def test_a_missing_plz_is_refused(monkeypatch, events):
    with pytest.raises(SystemExit) as refused:
        run(monkeypatch)
    assert refused.value.code == 2 and events == []
