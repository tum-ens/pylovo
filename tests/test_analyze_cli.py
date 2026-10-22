"""``pylovo-analyze``: what is reported and the exit code for PLZ without grids or already analysed (no database)."""
import json
import logging
import sys

import numpy as np
import pandas as pd
import pytest

from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo.cli import analyze


class FakeDb:
    logger = logging.getLogger("analyze-test")

    class conn:
        @staticmethod
        def commit():
            pass

    def __init__(self, generated=True, analysed=False):
        self.generated, self.analysed = generated, analysed

    def is_grid_generated(self, plz):
        return self.generated

    def is_grid_analyzed(self, plz):
        return self.analysed

    def get_list_from_plz(self, plz):
        return []


def calculator(**state) -> ParameterCalculator:
    pc = ParameterCalculator.__new__(ParameterCalculator)
    pc.dbc = FakeDb(**state)
    for step in ("analyze_basic_parameters_for_plz", "analyze_cable_lengths_for_plz",
                 "analyze_transformer_parameters_for_plz"):
        setattr(pc, step, lambda plz: None)
    return pc


def test_the_calculator_reports_what_it_did():
    assert calculator(generated=False).analyze_parameters_for_plz(80331) == "no_grids"
    assert calculator(analysed=True).analyze_parameters_for_plz(80331) == "already_analysed"
    assert calculator().analyze_parameters_for_plz(80331) == "analysed"
    assert calculator(analysed=False, generated=False).analyze_grid_parameters_for_plz(80331) == "not_analysed"
    assert calculator(analysed=True).analyze_grid_parameters_for_plz(80331) == "analysed"


def run(monkeypatch, capsys, statuses: dict, *argv):
    """Run main() with a calculator that answers ``statuses[plz]`` and return (exit code, output)."""
    class Fake:
        def analyze_parameters_for_plz(self, plz):
            return statuses[plz]

        def analyze_grid_parameters_for_plz(self, plz):
            return "not_analysed" if statuses[plz] == "no_grids" else "analysed"

    monkeypatch.setattr(analyze, "ParameterCalculator", Fake)
    monkeypatch.setattr(sys, "argv", ["pylovo-analyze", *argv])
    code = 0
    try:
        analyze.main()
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().out


def test_a_plz_without_grids_is_not_reported_as_calculated_and_exits_1(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, {80331: "analysed", 80333: "no_grids"}, "--plz", "80331", "80333")
    assert code == 1
    assert "✓ Calculated parameters for PLZ 80331" in out
    assert "✗ PLZ 80333 has no grids" in out and "Calculated parameters for PLZ 80333" not in out
    assert "Nothing was calculated for PLZ [80333]" in out


def test_an_already_analysed_plz_is_skipped_with_exit_0(monkeypatch, capsys):
    code, out = run(monkeypatch, capsys, {80331: "already_analysed"}, "--plz", "80331")
    assert code == 0
    assert "- PLZ 80331 is already analysed, skipped" in out and "✓" not in out


@pytest.mark.parametrize("option", ["--per-grid", "--all"])
def test_per_grid_modes_fail_for_a_plz_without_grids(monkeypatch, capsys, option):
    code, out = run(monkeypatch, capsys, {80333: "no_grids"}, "--plz", "80333", option)
    assert code == 1 and "✓" not in out


@pytest.mark.parametrize("argv, expected", [
    (["--version-id", "2"], "--version-id is ignored"),
    (["--output", "reports"], "--output is ignored"),
    (["--coverage", "--all"], "--per-grid and --all are ignored"),
    (["--border-diagnostics", "--per-grid"], "--per-grid and --all are ignored"),
])
def test_ignored_option_combinations_are_reported(monkeypatch, capsys, argv, expected):
    monkeypatch.setattr(analyze, "run_diagnostics", lambda *args: 0)
    monkeypatch.setattr(analyze, "ParameterCalculator", lambda: type(
        "Fake", (), {"analyze_parameters_for_plz": lambda self, plz: "analysed"})())
    monkeypatch.setattr(sys, "argv", ["pylovo-analyze", "--plz", "80331", *argv])
    analyze.main()
    assert f"Warning: {expected}" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [], ["--per-grid"], ["--all"], ["--coverage"], ["--border-diagnostics", "--version-id", "2", "--output", "reports"],
])
def test_valid_option_combinations_are_not_reported(monkeypatch, capsys, argv):
    monkeypatch.setattr(analyze, "run_diagnostics", lambda *args: 0)
    monkeypatch.setattr(analyze, "ParameterCalculator", lambda: type(
        "Fake", (), {"analyze_parameters_for_plz": lambda self, plz: "analysed",
                     "analyze_grid_parameters_for_plz": lambda self, plz: "analysed"})())
    monkeypatch.setattr(sys, "argv", ["pylovo-analyze", "--plz", "80331", *argv])
    analyze.main()
    assert "Warning" not in capsys.readouterr().err


def test_the_border_report_is_strict_json_and_closes_the_infdb_client(monkeypatch, tmp_path):
    """Without a scope table the shares are NaN, which json writes as the token NaN that strict parsers reject."""
    import pylovo.database.database_client as database_client
    import pylovo.infdb.infdb_client as infdb_client
    from pylovo.analysis import border_diagnostics

    class FakeDatabase:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeInfdb:
        closed = False

        def close(self):
            FakeInfdb.closed = True

    context = pd.DataFrame({"plz": [80331], "area_km2": [1.2345678], "scope_share": [np.nan],
                            "context_share": [np.nan], "streets_inside_km": [3.0], "streets_ring_km": [1.5]})
    grids = pd.DataFrame({"n_buildings": [5], "small": [False], "generated_border_m": [10.0],
                          "open_border_m": [np.nan], "outer_border_m": [np.nan], "scope_edge_m": [np.nan]})
    monkeypatch.setattr(database_client, "DatabaseClient", FakeDatabase)
    monkeypatch.setattr(infdb_client, "InfdbClient", FakeInfdb)
    monkeypatch.setattr(border_diagnostics, "postcode_context", lambda *args: context)
    monkeypatch.setattr(border_diagnostics, "grid_edges", lambda *args: grids)

    analyze.run_diagnostics([80331], coverage=False, border=True, version_id="v", output=tmp_path)

    def refuse(constant):
        raise ValueError(f"the report contains {constant}")

    report = json.loads((tmp_path / "diagnostics_v.json").read_text(), parse_constant=refuse)
    assert report["postcodes"] == [{"plz": 80331, "area_km2": 1.2346, "scope_share": None, "context_share": None,
                                    "streets_inside_km": 3.0, "streets_ring_km": 1.5}]
    assert FakeInfdb.closed
