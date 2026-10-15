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
from pylovo.mv_grid.parameters import MvParameters

REPO = Path(__file__).resolve().parents[1]


def summary(status: str = "done") -> MvRunSummary:
    return MvRunSummary("mv1", "d0", status, [1], [1], [], 1, 1 if status == "done" else 0,
                        0 if status == "done" else 1, 0, 3, 0, 0, 1.5, 0.6, 0, 0, [], 2.0)


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
    args = parser.parse_args(["generate", "--version", "mv1", "--all", "--set", "MV_STUBS=none",
                              "local_search_rounds=0", "--set", "MV_STANDARD_CABLE=NA2XS2Y 1x150 RM/25 12/20 kV"])
    assert args.assignments == [("MV_STUBS", "none"), ("local_search_rounds", 0),
                                ("MV_STANDARD_CABLE", "NA2XS2Y 1x150 RM/25 12/20 kV")]


@pytest.mark.parametrize("assignment, message", [
    ("MV_GRID_GENERATION=true", "pylovo-generate --mv"),
    ("MV_VARIANT_ID=x", "use --variant"),
    ("MV_VARIANT_COMMENT=x", "use --comment"),
    ("MV_STUBS=sometimes", "MV_STUBS must be one of rural, none"),
    ("stub_max_kwa=3", "did you mean stub_max_kva"),
    ("MV_STUBS", "KEY=VALUE"),
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
              "--comment", "cable rings", "--set", "MV_STUBS=none", "local_search_rounds=0", "--no-parallel",
              "--jobs", "2", "--replace"])
    (version_id, plz_list, params, kwargs), = calls
    assert version_id == "mv1" and plz_list == [80802, 80803]
    assert (params.variant_id, params.comment, params.stubs, params.local_search_rounds) == (
        "d0", "cable rings", "none", 0)
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
         "n_grids": 3, "n_done": 3, "n_failed": 0, "circuit_km": 120.4, "stale": False},
        {"version_id": "mv1", "mv_variant_id": "d1", "mv_variant_comment": "real", "status": "partial", "n_plz": 2,
         "n_grids": 3, "n_done": 2, "n_failed": 1, "circuit_km": None, "stale": True},
    ])
    client = FakeClient(delete_variants=lambda v, ids: deleted.append((v, ids)) or 2,
                        list_variants=lambda v: rows)
    monkeypatch.setattr(cli, "_database_client", lambda: client)
    cli.main(["delete", "--version", "mv1", "--variant", "d0", "d1"])
    cli.main(["list", "--version", "mv1"])
    out = capsys.readouterr().out.splitlines()
    assert deleted == [("mv1", ["d0", "d1"])]
    assert out[0] == "✓ Deleted 2 MV variants of version mv1 (d0, d1)"
    assert out[1].startswith("✓ mv1/d0: done, 2 PLZ, 3 grids (3 done, 0 failed), 120 km")
    assert out[2].startswith("✗ mv1/d1: partial") and ", stale" in out[2]


def test_couple_and_coupled_export_need_the_coupling_module(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "pylovo.mv_grid.coupling", None)       # import fails
    for argv in (["couple", "--version", "mv1", "--variant", "d1"],
                 ["export", "--version", "mv1", "--variant", "d1", "--grid", "3", "--output", "x.json",
                  "--coupled"]):
        with pytest.raises(SystemExit) as exc:
            cli.main(argv)
        assert exc.value.code == 1
    out = capsys.readouterr().out.splitlines()
    assert out == ["✗ pylovo-mv couple is not available in this build",
                   "✗ pylovo-mv export --coupled is not available in this build"]


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
