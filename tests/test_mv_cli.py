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
from pylovo.mv_grid.options import MV_STAGE_VERSION
from pylovo.infdb.source_reader import SourceReader, source_is_remote
from pylovo.mv_grid.parameters import MvParameters, MvStageError

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


class _SourceClient:
    """A DatabaseClient whose check of the InfDB source tables returns ``messages`` or raises ``error``."""

    def __init__(self, error: Exception | None = None, messages: tuple[str, ...] = ()):
        self.error, self.messages, self.closed = error, list(messages), False

    def check_mv_sources(self):
        if self.error is not None:
            raise self.error
        return self.messages

    def ensure_grid_persistence_schema(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture()
def source_check(monkeypatch):
    """``pylovo-generate --mv`` checks the InfDB source tables on a fake client (no database)."""
    clients = []

    def client(*args, **kwargs):
        clients.append(_SourceClient(messages=("no osm.landuse_polygon in the pylovo database: MV settlements from "
                                               "address buildings only",)))
        return clients[-1]

    monkeypatch.setattr(generate.dbc, "DatabaseClient", client)
    return clients


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


def test_set_takes_yaml_keys():
    parser = cli.build_parser()
    args = parser.parse_args(["generate", "--version", "mv1", "--all", "--set", "MV_MAX_STATIONS_PER_RING=15",
                              "MV_LINE_PLANNING_UTILIZATION=0.5", "--set", "MV_STANDARD_CABLE=NA2XS2Y 1x150 RM/25 12/20 kV"])
    assert args.assignments == [("MV_MAX_STATIONS_PER_RING", 15), ("MV_LINE_PLANNING_UTILIZATION", 0.5),
                                ("MV_STANDARD_CABLE", "NA2XS2Y 1x150 RM/25 12/20 kV")]


@pytest.mark.parametrize("assignment, message", [
    ("MV_GRID_GENERATION=true", "pylovo-generate --mv"),
    ("MV_VARIANT_ID=x", "use --variant"),
    ("MV_VARIANT_COMMENT=x", "use --comment"),
    ("MV_MAX_STATIONS_PER_RING=many", "MV_MAX_STATIONS_PER_RING must be an integer"),
    ("MV_MAX_STATION_PER_RING=3", "did you mean MV_MAX_STATIONS_PER_RING"),
    ("stub_max_kva=3", "stub_max_kva is a constant of the MV stage"),
    ("MV_STUBS=rural", "unknown MV key 'MV_STUBS'"),
    ("MV_MAX_STATIONS_PER_RING", "KEY=VALUE"),
])
def test_set_rejects_execution_keys_constants_and_bad_values(assignment, message, capsys):
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
              "--comment", "cable rings", "--set", "MV_MAX_STATIONS_PER_RING=15", "MV_LINE_PLANNING_UTILIZATION=0.5",
              "--no-parallel",
              "--jobs", "2", "--replace"])
    (version_id, plz_list, params, kwargs), = calls
    assert version_id == "mv1" and plz_list == [80802, 80803]
    assert (params.variant_id, params.comment, params.max_stations_per_ring, params.line_utilization) == (
        "d0", "cable rings", 15, 0.5)
    assert kwargs == {"parallel": False, "jobs": 2, "replace": True, "osm_coverage": None}   # None: MV_OSM_COVERAGE
    out = capsys.readouterr().out.splitlines()
    assert out and all(line.startswith(("✓", "✗")) for line in out)
    for flag, value in (("--no-osm-coverage", False), ("--osm-coverage", True)):
        cli.main(["generate", "--version", "mv1", "--all", flag])
        assert calls[-1][3]["osm_coverage"] is value
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["generate", "--version", "mv1", "--all", "--osm-coverage", "--no-osm-coverage"])
    capsys.readouterr()

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


@pytest.mark.parametrize("variant_args, which", [(["--variant", "typo"], "typo"), ([], "all")])
def test_deleting_a_variant_that_does_not_exist_is_an_error(monkeypatch, capsys, no_grid_generator, variant_args, which):
    client = FakeClient(delete_variants=lambda v, ids: 0, ensure_grid_persistence_schema=lambda: None)
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    with pytest.raises(SystemExit) as exc:
        cli.main(["delete", "--version", "mv1", *variant_args, "--yes"])
    assert exc.value.code == 1
    assert capsys.readouterr().out.strip() == f"✗ No MV variant of version mv1 to delete ({which})"


def test_delete_and_list(monkeypatch, capsys, no_grid_generator):
    import pandas as pd

    deleted = []
    rows = pd.DataFrame([
        {"version_id": "mv1", "mv_variant_id": "d0", "mv_variant_comment": "base", "status": "done", "n_plz": 2,
         "n_grids": 3, "n_done": 3, "n_failed": 0, "n_electrical_pass": 2, "n_electrical_violations": 1,
         "n_electrical_not_checked": 0, "mv_stage_version": MV_STAGE_VERSION, "circuit_km": 120.4, "stale": False},
        {"version_id": "mv1", "mv_variant_id": "d1", "mv_variant_comment": "real", "status": "partial", "n_plz": 2,
         "n_grids": 3, "n_done": 2, "n_failed": 1, "n_electrical_pass": 0, "n_electrical_violations": 1,
         "n_electrical_not_checked": 2, "mv_stage_version": 12, "circuit_km": None,
         "stale": True},
    ])
    client = FakeClient(delete_variants=lambda v, ids: deleted.append((v, ids)) or 2,
                        list_variants=lambda v: rows, ensure_grid_persistence_schema=lambda: None)
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    cli.main(["delete", "--version", "mv1", "--variant", "d0", "d1", "--yes"])
    cli.main(["list", "--version", "mv1"])
    out = capsys.readouterr().out.splitlines()
    assert deleted == [("mv1", ["d0", "d1"])]
    assert out[0] == "✓ Deleted 2 MV variants of version mv1 (d0, d1)"
    assert out[1].startswith("✓ mv1/d0: generation done, 2 PLZ, 3 grids (3 done, 0 failed); "
                             "electrical: 2 pass, 1 violation, 0 not checked, 120 km")
    assert out[2].startswith("✗ mv1/d1: generation partial")
    assert ("(2 done, 1 failed), MV stage version 12 (read only); electrical: 0 pass, 1 violation, "
            "2 not checked") in out[2] and ", stale" in out[2]


def test_prune_graphs_reports_the_deleted_graphs(monkeypatch, capsys):
    deleted = [["g1", "g2"], []]
    client = FakeClient(ensure_grid_persistence_schema=lambda: None, prune_graphs=lambda: deleted.pop(0))
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    cli.main(["prune-graphs"])
    cli.main(["prune-graphs"])
    assert capsys.readouterr().out.splitlines() == [
        "✓ Deleted 2 MV routing graphs that no variant references: g1, g2",
        "✓ Deleted 0 MV routing graphs that no variant references"]


def test_a_variant_of_an_older_stage_version_is_displayed(monkeypatch, capsys):
    import shapely

    from pylovo.mv_grid.display import DisplayLine

    lines = [DisplayLine(1, shapely.LineString([(0, 0), (300, 0), (300, 200)]), 10, 11),
             DisplayLine(2, shapely.LineString([(0, 0), (300, 0), (300, -200)]), 10, 12)]
    saved = []
    client = FakeClient(ensure_grid_persistence_schema=lambda: None, display_input=lambda v, var: (lines, {10: (0, 0)}),
                        save_display_geometry=lambda v, var, done, busbars: saved.append((v, var, sorted(done))),
                        variant_row=lambda v, var: {"mv_parameters": {"mv_stage_version": 12}})
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    cli.main(["display", "--version", "mv1", "--variant", "d1"])
    assert saved == [("mv1", "d1", [1, 2])]
    assert capsys.readouterr().out.startswith("✓ MV variant d1 of version mv1: 2 lines in lanes")


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
    client = FakeClient(hv_boundary=lambda v, x: calls.append((v, x)) or table, rollback_changes=lambda: None,
                        ensure_grid_persistence_schema=lambda: None)
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
def test_generate_hook_runs_only_with_mv(monkeypatch, capsys, config_params, source_check):
    hook = []
    monkeypatch.setattr(generate, "create_grid_multiple_plz",
                        lambda plz, parallel=True: {p: grid_generator.GenerationOutcome.GENERATED for p in plz})
    monkeypatch.setattr(generate, "create_grid_single_plz", lambda plz: {plz: grid_generator.GenerationOutcome.GENERATED})
    monkeypatch.setattr(generate, "run_mv_stage_after_lv", lambda plz, parallel, params: hook.append((plz, parallel)))
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
    assert len(source_check) == 2 and all(c.closed for c in source_check)   # checked once per run with the MV stage
    assert "MV stage: no osm.landuse_polygon in the pylovo database" in capsys.readouterr().out


def test_generate_hook_runs_the_configured_variant(monkeypatch, capsys, config_params):
    calls = []
    params = MvParameters()
    monkeypatch.setattr(stage, "run_mv_stage", lambda *a, **k: calls.append((a, k)) or summary("done"))
    generate.run_mv_stage_after_lv([80803], True, params)
    (args, kwargs), = calls
    assert args[0] == generate.VERSION_ID and args[1] == [80803] and args[2] is params
    assert kwargs == {"parallel": True, "jobs": min(4, generate.N_JOBS)}
    monkeypatch.setattr(stage, "run_mv_stage", lambda *a, **k: summary("failed"))
    with pytest.raises(SystemExit) as exc:
        generate.run_mv_stage_after_lv([80803], True, params)
    assert exc.value.code == 1

    def broken(*a, **k):
        raise RuntimeError("no substations")

    monkeypatch.setattr(stage, "run_mv_stage", broken)
    with pytest.raises(RuntimeError):
        generate.run_mv_stage_after_lv([80803], True, params)
    assert "MV stage failed: no substations" in capsys.readouterr().out


def test_a_bad_mv_parameter_stops_pylovo_generate_before_the_lv_grids(monkeypatch, capsys):
    from pylovo import config_loader

    def bad_config(cls):
        raise ValueError("MV_STANDARD_CABLE must name a pandapower cable line type for 20 kV, got 'NA2XS2Y typo'")

    lv = []
    monkeypatch.setattr(config_loader, "USE_EXTERNAL_BASEDATA", False)
    monkeypatch.setattr(MvParameters, "from_config", classmethod(bad_config))
    monkeypatch.setattr(generate, "create_grid_single_plz", lambda plz: lv.append(plz) or [plz])
    with pytest.raises(SystemExit) as stopped:
        generate.main(["--plz", "80803", "--mv"])
    assert stopped.value.code == 1 and lv == []
    assert "MV stage cannot run: MV_STANDARD_CABLE must name" in capsys.readouterr().out


def test_lv_only_runs_do_not_import_the_mv_stage():
    # config_loader always imports pylovo.mv_grid.options (and the MV mixins import the light contract modules);
    # the stage and the planning modules must stay unloaded in LV-only runs
    code = (
        "import sys\n"
        "from pylovo.cli import generate\n"
        "from pylovo.grid_generator import GenerationOutcome\n"
        "generate.create_grid_multiple_plz = lambda plz_list, parallel=True: {p: GenerationOutcome.GENERATED for p in plz_list}\n"
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


def test_every_command_on_the_mv_tables_stops_with_pending_migrations(monkeypatch, capsys, config_params):
    from pylovo import config_loader

    pending = "PyLovo schema has pending migrations (0009_mv_grids, 0010_mv_display_geometry); run pylovo-setup first"

    def gate():
        raise RuntimeError(pending)

    def refuse(*args, **kwargs):
        raise AssertionError("the MV tables must not be read before the migration check")

    client = FakeClient(ensure_grid_persistence_schema=gate, delete_variants=refuse, list_variants=refuse,
                        generated_plz=refuse, hv_boundary=refuse)
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    monkeypatch.setattr(stage, "_database_client", lambda log_file: client)
    monkeypatch.setattr(config_loader, "USE_EXTERNAL_BASEDATA", False)
    for argv in (["delete", "--version", "mv1", "--variant", "d0", "--yes"], ["list"], ["graph", "--version", "mv1", "--all"],
                 ["boundary", "--version", "mv1", "--variant", "d1", "--output", "b.csv"],
                 ["export", "--version", "mv1", "--variant", "d1", "--grid", "3", "--output", "g.json"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 1 and capsys.readouterr().out.strip() == f"✗ {pending}", argv


def test_display_stores_the_display_geometry_of_each_variant(monkeypatch, capsys):
    checked = []
    client = FakeClient(ensure_grid_persistence_schema=lambda: checked.append(True))
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    stored = {"d1": (12, 2), "d2": None, "d3": (0, 1), "d4": (0, 0)}       # d3: no shared route, d4: no lines
    monkeypatch.setattr(stage, "display_geometry", lambda dbc, v, var, logger: stored[var])
    with pytest.raises(SystemExit) as exc:
        cli.main(["display", "--version", "mv1", "--variant", "d1", "d2", "d3", "d4"])
    assert exc.value.code == 1 and checked == [True]
    out = capsys.readouterr().out.splitlines()
    assert out == ["✓ MV variant d1 of version mv1: 12 lines in lanes, 2 busbar circles",
                   "✗ MV variant d2 of version mv1: no display geometry (no lines, or an error)",
                   "✓ MV variant d3 of version mv1: 0 lines in lanes, 1 busbar circles",
                   "✗ MV variant d4 of version mv1: no display geometry (no lines, or an error)"]
    cli.main(["display", "--version", "mv1", "--variant", "d3"])           # no line in a lane is no failure


def test_missing_source_tables_stop_the_mv_stage_before_any_grid(monkeypatch, capsys, no_grid_generator,
                                                                  config_params):
    missing = MvStageError("the MV stage needs basedata.ways_per_junction in central_infdb@remote:6432")
    monkeypatch.setattr(generate.dbc, "DatabaseClient", lambda *a, **k: _SourceClient(missing))
    lv = []
    monkeypatch.setattr(generate, "create_grid_single_plz",
                        lambda plz: lv.append(plz) or {plz: grid_generator.GenerationOutcome.GENERATED})
    with pytest.raises(SystemExit) as stopped:
        generate.main(["--plz", "80803", "--mv"])
    assert stopped.value.code == 1 and lv == []                          # no LV grids before the MV stage fails
    assert "MV stage cannot run: the MV stage needs basedata.ways_per_junction in central_infdb" in \
        capsys.readouterr().out
    clients = []
    monkeypatch.setattr(cli, "_database_client", lambda: clients.append(_SourceClient(missing)) or clients[-1])
    with pytest.raises(SystemExit):
        cli.main(["graph", "--version", "v1", "--plz", "80803"])
    assert capsys.readouterr().out.startswith("✗ the MV stage needs basedata.ways_per_junction")
    monkeypatch.setattr(stage, "_database_client", lambda log_file: clients.append(_SourceClient(missing))
                        or clients[-1])
    with pytest.raises(MvStageError, match="central_infdb"):
        stage.run_mv_stage("v1", [80803], MvParameters())
    assert all(c.closed for c in clients)


def test_source_data_are_remote_only_in_another_database():
    pylovo = {"USE_INFDB": True, "DBNAME": "pylovo", "HOST": "localhost", "PORT": "5432"}
    same = SimpleNamespace(USE_EXTERNAL_BASEDATA=True, INFDB_DBNAME="pylovo", INFDB_HOST="localhost", INFDB_PORT=5432,
                           **pylovo)
    assert not source_is_remote(same)                                  # unset INFDB_* values fall back to pylovo's
    assert not source_is_remote(SimpleNamespace(USE_EXTERNAL_BASEDATA=False, INFDB_DBNAME="x", INFDB_HOST="y",
                                                INFDB_PORT=1, **pylovo))
    other = SimpleNamespace(USE_EXTERNAL_BASEDATA=True, INFDB_DBNAME="infdb", INFDB_HOST="remote", INFDB_PORT="6432",
                            INFDB_USER="u", INFDB_PASSWORD="p", **pylovo)
    assert source_is_remote(other)
    reader = SourceReader.from_config(lambda: None, other)              # no connection before the first query
    assert reader.remote and reader.where == "infdb@remote:6432"
    assert not SourceReader.from_config(lambda: None, same).remote
