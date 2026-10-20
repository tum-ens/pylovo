"""``pylovo-mv`` and the ``pylovo-generate --mv/--no-mv`` hook without a database."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pylovo.grid_generator as grid_generator
from pylovo.cli import generate
from pylovo.cli import mv as cli
from pylovo.mv_grid import stage
from pylovo.mv_grid.model import MvRunSummary
from pylovo.mv_grid.parameters import MvParameters, MvStageError, require_local_source_data

REPO = Path(__file__).resolve().parents[1]


def summary(status: str = "done") -> MvRunSummary:
    return MvRunSummary("mv1", "d0", status, [1], [1], [], 1, 1 if status == "done" else 0,
                        0 if status == "done" else 1, 0, 3, 0, 1.5, 0.6, 0, 0, [], 2.0)


@pytest.fixture()
def no_grid_generator(monkeypatch):
    """Any construction of GridGenerator fails (the MV commands work on stored versions)."""
    def refuse(*args, **kwargs):
        raise AssertionError("GridGenerator must not be constructed")

    monkeypatch.setattr(grid_generator.GridGenerator, "__init__", refuse)


@pytest.fixture()
def config_params(monkeypatch):
    """MvParameters.from_config without the configuration and pandapower types."""
    monkeypatch.setattr(MvParameters, "from_config", classmethod(lambda cls: cls()))
    monkeypatch.setattr(MvParameters, "check_types", lambda self: None)


# ---------------------------------------------------------------------------------------------- parser
def test_parser_regions_and_options():
    parser = cli.build_parser()
    args = parser.parse_args(["generate", "--version", "mv1", "--plz", "80803", "80802", "--variant", "d0",
                              "--no-parallel", "--jobs", "2", "--replace"])
    assert (args.version, args.plz, args.variant, args.parallel, args.jobs, args.replace) == (
        "mv1", [80803, 80802], "d0", False, 2, True)
    assert parser.parse_args(["generate", "--version", "mv1", "--all"]).all is True
    assert parser.parse_args(["generate", "--version", "mv1", "--ags", "9162000"]).ags == [9162000]
    assert parser.parse_args(["generate", "--version", "mv1", "--all"]).parallel is None
    for argv in (["generate", "--version", "mv1"], ["generate", "--version", "mv1", "--all", "--plz", "1"],
                 ["generate", "--plz", "1"], ["export", "--version", "mv1", "--variant", "d0", "--grid", "1",
                                               "--output", "x.json", "--case", "peak"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(argv)
        assert exc.value.code == 2
    args = parser.parse_args(["couple", "--version", "mv1", "--variant", "d1", "--grid", "3", "4", "--no-merged"])
    assert (args.grids, args.jobs, args.merged) == ([3, 4], 2, False)
    args = parser.parse_args(["delete", "--version", "mv1"])
    assert args.variants is None
    args = parser.parse_args(["export", "--version", "mv1", "--variant", "d1", "--grid", "3", "--output", "x.json"])
    assert args.case == "load" and args.coupled is False


def test_set_takes_yaml_keys_and_constants():
    parser = cli.build_parser()
    args = parser.parse_args(["generate", "--version", "mv1", "--all", "--set", "MV_MAX_STATIONS_PER_RING=15",
                              "local_search_rounds=0", "--set", "MV_STANDARD_CABLE=NA2XS2Y 1x150 RM/25 12/20 kV"])
    assert args.assignments == [("MV_MAX_STATIONS_PER_RING", 15), ("local_search_rounds", 0),
                                ("MV_STANDARD_CABLE", "NA2XS2Y 1x150 RM/25 12/20 kV")]


@pytest.mark.parametrize("assignment, message", [
    ("MV_GRID_GENERATION=true", "pylovo-generate --mv"),
    ("MV_VARIANT_ID=x", "use --variant"),
    ("MV_VARIANT_COMMENT=x", "use --comment"),
    ("MV_MAX_STATIONS_PER_RING=many", "MV_MAX_STATIONS_PER_RING must be an integer"),
    ("stub_max_kwa=3", "did you mean stub_max_kva"),
    ("MV_MAX_STATIONS_PER_RING", "KEY=VALUE"),
])
def test_set_rejects_execution_keys_and_bad_values(assignment, message, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(["generate", "--version", "mv1", "--all", "--set", assignment])
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


# ---------------------------------------------------------------------------------------------- commands
def test_generate_runs_the_stage_with_the_variant(monkeypatch, capsys, no_grid_generator, config_params):
    calls = []

    def fake_run(version_id, plz_list, params, **kwargs):
        calls.append((version_id, plz_list, params, kwargs))
        return summary("done")

    monkeypatch.setattr(stage, "run_mv_stage", fake_run)
    cli.main(["generate", "--version", "mv1", "--plz", "80803", "80802", "80803", "--variant", "d0",
              "--comment", "cable rings", "--set", "MV_MAX_STATIONS_PER_RING=15", "local_search_rounds=0", "--no-parallel",
              "--jobs", "2", "--replace"])
    (version_id, plz_list, params, kwargs), = calls
    assert version_id == "mv1" and plz_list == [80802, 80803]
    assert (params.variant_id, params.comment, params.max_stations_per_ring, params.local_search_rounds) == (
        "d0", "cable rings", 15, 0)
    assert kwargs == {"parallel": False, "jobs": 2, "replace": True}
    out = capsys.readouterr().out.splitlines()
    assert out and all(line.startswith(("✓", "✗")) for line in out)

    monkeypatch.setattr(stage, "run_mv_stage", lambda *a, **k: summary("partial"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["generate", "--version", "mv1", "--all"])
    assert exc.value.code == 1


def test_generate_errors_exit_with_a_cross(monkeypatch, capsys, no_grid_generator, config_params):
    from pylovo.mv_grid.parameters import MvStageError

    def busy(*args, **kwargs):
        raise MvStageError("another MV run of variant 1 of version mv1 is active")

    monkeypatch.setattr(stage, "run_mv_stage", busy)
    with pytest.raises(SystemExit) as exc:
        cli.main(["generate", "--version", "mv1", "--all"])
    assert exc.value.code == 1
    assert capsys.readouterr().out.strip() == "✗ another MV run of variant 1 of version mv1 is active"


class FakeClient:
    def __init__(self, **methods):
        self.__dict__.update(methods)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_delete_and_list(monkeypatch, capsys, no_grid_generator):
    import pandas as pd

    deleted = []
    rows = pd.DataFrame([
        {"version_id": "mv1", "mv_variant_id": "d0", "mv_variant_comment": "base", "status": "done", "n_plz": 2,
         "n_grids": 3, "n_done": 3, "n_failed": 0, "n_electrical_pass": 2, "n_electrical_violations": 1,
         "n_electrical_not_checked": 0, "retired": [], "circuit_km": 120.4, "stale": False},
        {"version_id": "mv1", "mv_variant_id": "d1", "mv_variant_comment": "real", "status": "partial", "n_plz": 2,
         "n_grids": 3, "n_done": 2, "n_failed": 1, "n_electrical_pass": 0, "n_electrical_violations": 1,
         "n_electrical_not_checked": 2, "retired": ["generation.include_generation"], "circuit_km": None,
         "stale": True},
    ])
    client = FakeClient(delete_variants=lambda v, ids: deleted.append((v, ids)) or 2,
                        list_variants=lambda v: rows)
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    cli.main(["delete", "--version", "mv1", "--variant", "d0", "d1"])
    cli.main(["list", "--version", "mv1"])
    out = capsys.readouterr().out.splitlines()
    assert deleted == [("mv1", ["d0", "d1"])]
    assert out[0] == "✓ Deleted 2 MV variants of version mv1 (d0, d1)"
    assert out[1].startswith("✓ mv1/d0: generation done, 2 PLZ, 3 grids (3 done, 0 failed); "
                             "electrical: 2 pass, 1 violation, 0 not checked, 120 km")
    assert out[2].startswith("✗ mv1/d1: generation partial")
    assert ("(2 done, 1 failed), retired options generation.include_generation; electrical: 0 pass, 1 violation, "
            "2 not checked") in out[2] and ", stale" in out[2]


def test_couple_and_coupled_export_delegate_to_the_coupling_module(monkeypatch, capsys, tmp_path):
    from pylovo.mv_grid import coupling

    calls = []

    def run_coupling(version_id, variant_id, mv_grid_ids=None, *, jobs, merged):
        calls.append(("couple", version_id, variant_id, mv_grid_ids, jobs, merged))
        status = "done" if merged else "partial"
        return coupling.CouplingSummary(version_id, variant_id, status, 2, 2 if merged else 1, 0, 1.0,
                                        partial=0 if merged else 1, warnings=[] if merged else ["MV grid 4 partial"])

    monkeypatch.setattr(coupling, "run_coupling", run_coupling)                # never the database
    monkeypatch.setattr(coupling, "export_coupled_net",
                        lambda *a, **k: calls.append(("export", a, k)) or Path(a[3]))
    cli.main(["couple", "--version", "mv1", "--variant", "d1"])
    with pytest.raises(SystemExit) as exc:
        cli.main(["couple", "--version", "mv1", "--variant", "d1", "--grid", "3", "4", "--no-merged"])
    assert exc.value.code == 1
    cli.main(["export", "--version", "mv1", "--variant", "d1", "--grid", "3", "--output", str(tmp_path / "c.json"),
              "--coupled", "--case", "n1b"])
    assert calls == [("couple", "mv1", "d1", None, 2, True), ("couple", "mv1", "d1", [3, 4], 2, False),
                     ("export", ("mv1", "d1", 3, tmp_path / "c.json"), {"case": "n1b"})]
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("✓ Coupling of MV variant d1 of version mv1: done, 2 of 2 grids done")
    assert out[1].startswith("✗ Coupling of MV variant d1 of version mv1: partial, 1 of 2 grids done, 1 partial")
    assert out[2] == "✗ MV grid 4 partial"
    assert out[3].startswith("✓ Wrote MV grid 3 (n1b)")


def test_boundary_writes_one_row_per_substation(monkeypatch, capsys, tmp_path, no_grid_generator):
    import pandas as pd

    table = pd.DataFrame([
        {"substation_id": 7, "name": "A", "operator": None, "source": "osm", "etrago_bus_id": 33316,
         "egon_distance_m": 0.0, "flags": [], "n_grids": 1, "vn_kv": 20.0, "p_mw": 14.5, "q_mvar": -0.5,
         "peak_load_mw": 13.3, "n_transformers": 2, "transformer_mva": 25.0, "loading_n0_percent": 30.0,
         "loading_n1_percent": 60.0, "lon": 11.5, "lat": 48.5},
        {"substation_id": 8, "name": "B", "operator": "x", "source": "osm", "etrago_bus_id": None,
         "egon_distance_m": None, "flags": ["no_egon_bus"], "n_grids": 1, "vn_kv": 20.0, "p_mw": 6.0,
         "q_mvar": 0.3, "peak_load_mw": 5.5, "n_transformers": 2, "transformer_mva": 25.0,
         "loading_n0_percent": 12.0, "loading_n1_percent": 24.0, "lon": 11.6, "lat": 48.6},
    ])
    calls = []
    client = FakeClient(hv_boundary=lambda v, x: calls.append((v, x)) or table, rollback_changes=lambda: None)
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    out_file = tmp_path / "b" / "boundary.csv"
    cli.main(["boundary", "--version", "mv1", "--variant", "d1", "--output", str(out_file)])
    assert calls == [("mv1", "d1")]
    written = pd.read_csv(out_file)
    assert written["etrago_bus_id"].isna().tolist() == [False, True] and written["flags"].fillna("").tolist() == [
        "", "no_egon_bus"]
    assert capsys.readouterr().out.strip() == (f"✓ Wrote the HV boundary of 2 substations (1 with an eTraGo 110 kV bus, "
                                               f"20.5 MW) to {out_file}")


def test_export_delegates_to_the_stage(monkeypatch, capsys, tmp_path):
    calls = []
    monkeypatch.setattr(stage, "export_net", lambda *a, **k: calls.append((a, k)) or Path(a[3]))
    cli.main(["export", "--version", "mv1", "--variant", "d1", "--grid", "3", "--output",
              str(tmp_path / "g.json"), "--case", "n1a"])
    assert calls == [(("mv1", "d1", 3, tmp_path / "g.json"), {"case": "n1a"})]
    assert capsys.readouterr().out.startswith("✓ Wrote MV grid 3 (n1a)")


# ---------------------------------------------------------------------------------------------- pylovo-generate
def test_generate_hook_runs_only_with_mv(monkeypatch):
    hook = []
    monkeypatch.setattr(generate, "create_grid_multiple_plz", lambda plz, parallel=True: sorted(plz))
    monkeypatch.setattr(generate, "create_grid_single_plz", lambda plz: [plz])
    monkeypatch.setattr(generate, "run_mv_stage_after_lv", lambda plz, parallel: hook.append((plz, parallel)))
    monkeypatch.setattr(generate, "MV_GRID_GENERATION", False)
    generate.main(["--plz", "80803", "80802", "--mv", "--no-parallel"])
    generate.main(["--plz", "80803", "--no-mv"])
    generate.main(["--plz", "80803"])
    assert hook == [([80802, 80803], False)]
    monkeypatch.setattr(generate, "MV_GRID_GENERATION", True)
    generate.main(["--plz", "80803"])
    generate.main(["--plz", "80803", "--no-mv"])
    assert hook[1:] == [([80803], generate.PARALLEL)]
    with pytest.raises(SystemExit):
        generate.main(["--plz", "80803", "--mv", "--no-mv"])


def test_generate_hook_runs_the_configured_variant(monkeypatch, capsys, config_params):
    calls = []
    monkeypatch.setattr(stage, "run_mv_stage", lambda *a, **k: calls.append((a, k)) or summary("done"))
    generate.run_mv_stage_after_lv([80803], True)
    (args, kwargs), = calls
    assert args[0] == generate.VERSION_ID and args[1] == [80803] and isinstance(args[2], MvParameters)
    assert kwargs == {"parallel": True, "jobs": min(4, generate.N_JOBS)}
    monkeypatch.setattr(stage, "run_mv_stage", lambda *a, **k: summary("failed"))
    with pytest.raises(SystemExit) as exc:
        generate.run_mv_stage_after_lv([80803], True)
    assert exc.value.code == 1

    def broken(*a, **k):
        raise RuntimeError("no substations")

    monkeypatch.setattr(stage, "run_mv_stage", broken)
    with pytest.raises(RuntimeError):
        generate.run_mv_stage_after_lv([80803], True)
    assert "MV stage failed: no substations" in capsys.readouterr().out


def test_lv_only_runs_do_not_import_the_mv_stage():
    # config_loader always imports pylovo.mv_grid.options (and the MV mixins import the light contract modules);
    # the stage and the planning modules must stay unloaded in LV-only runs
    code = (
        "import sys\n"
        "from pylovo.cli import generate\n"
        "generate.create_grid_multiple_plz = lambda plz_list, parallel=True: list(plz_list)\n"
        "generate.MV_GRID_GENERATION = False\n"
        "generate.main(['--plz', '1', '2', '--no-mv'])\n"
        "generate.main(['--plz', '1', '2'])\n"
        "print('LOADED', [m for m in ('pylovo.mv_grid.stage', 'pylovo.mv_grid.planner', 'pylovo.mv_grid.topology',"
        " 'pylovo.mv_grid.electrical', 'pylovo.mv_grid.solvers') if m in sys.modules])\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "LOADED []" in result.stdout


def test_mv_cli_never_constructs_a_grid_generator():
    source = Path(cli.__file__).read_text(encoding="utf-8") + Path(stage.__file__).read_text(encoding="utf-8")
    assert "GridGenerator(" not in source and "grid_generator" not in source
    assert not re.search(r"config_loader import[^\n]*\bVERSION_ID\b", source) and ".VERSION_ID" not in source


def test_pylovo_mv_script_is_registered():
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'pylovo-mv = "pylovo.cli.mv:main"' in text
    assert SimpleNamespace(main=cli.main).main is cli.main


def test_display_stores_the_display_geometry_of_each_variant(monkeypatch, capsys):
    checked = []
    client = FakeClient(ensure_grid_persistence_schema=lambda: checked.append(True))
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    monkeypatch.setattr(stage, "display_geometry", lambda dbc, v, var, logger: {"d1": 12, "d2": None}[var])
    with pytest.raises(SystemExit) as exc:
        cli.main(["display", "--version", "mv1", "--variant", "d1", "d2"])
    assert exc.value.code == 1 and checked == [True]
    out = capsys.readouterr().out.splitlines()
    assert out == ["✓ MV variant d1 of version mv1: 12 lines with display geometry",
                   "✗ MV variant d2 of version mv1: no display geometry (no lines, or an error)"]


def test_source_data_of_another_database_stop_the_mv_stage_before_any_grid(monkeypatch, capsys, no_grid_generator,
                                                                           config_params):
    from pylovo import config_loader

    monkeypatch.setattr(config_loader, "USE_EXTERNAL_BASEDATA", True)
    monkeypatch.setattr(config_loader, "INFDB_DBNAME", "central_infdb")
    lv = []
    monkeypatch.setattr(generate, "create_grid_single_plz", lambda plz: lv.append(plz) or [plz])
    with pytest.raises(SystemExit) as stopped:
        generate.main(["--plz", "80803", "--mv"])
    assert stopped.value.code == 1 and lv == []                          # no LV grids before the MV stage fails
    assert "central_infdb" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["graph", "--version", "v1", "--plz", "80803"])
    assert capsys.readouterr().out.startswith("✗ the MV stage reads the InfDB source data on the pylovo connection")
    with pytest.raises(MvStageError, match="central_infdb"):
        stage.run_mv_stage("v1", [80803], MvParameters())


def test_external_source_data_in_the_pylovo_database_keep_the_mv_stage():
    pylovo = {"DBNAME": "pylovo", "HOST": "localhost", "PORT": "5432"}
    same = SimpleNamespace(USE_EXTERNAL_BASEDATA=True, INFDB_DBNAME="pylovo", INFDB_HOST="localhost", INFDB_PORT=5432,
                           **pylovo)
    require_local_source_data(same)                                    # unset INFDB_* values fall back to pylovo's
    require_local_source_data(SimpleNamespace(USE_EXTERNAL_BASEDATA=False, INFDB_DBNAME="x", INFDB_HOST="y",
                                              INFDB_PORT=1, **pylovo))
    with pytest.raises(MvStageError, match="infdb@remote:6432"):
        require_local_source_data(SimpleNamespace(USE_EXTERNAL_BASEDATA=True, INFDB_DBNAME="infdb", INFDB_HOST="remote",
                                                  INFDB_PORT="6432", **pylovo))
