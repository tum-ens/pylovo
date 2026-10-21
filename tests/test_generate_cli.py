"""``pylovo-generate``: summary line, exit code and MV stage for PLZ that failed or have no grids (no database)."""
import pytest

from pylovo.cli import generate
from pylovo.grid_generator import GenerationOutcome as Outcome

GENERATED, EXISTS, EMPTY, FAILED = Outcome.GENERATED, Outcome.EXISTS, Outcome.EMPTY, Outcome.FAILED


@pytest.fixture()
def lv(monkeypatch):
    """Fake LV generation answering ``lv.outcomes[plz]``; ``lv.mv`` records the PLZ the MV stage runs for."""
    class Run:
        outcomes: dict = {}
        mv: list = []

    Run.outcomes, Run.mv = {}, []
    monkeypatch.setattr(generate, "MV_GRID_GENERATION", False)
    monkeypatch.setattr(generate, "create_grid_single_plz", lambda plz: {plz: Run.outcomes[plz]})
    monkeypatch.setattr(generate, "create_grid_multiple_plz",
                        lambda plz, parallel=True: {p: Run.outcomes[p] for p in plz})
    monkeypatch.setattr(generate, "create_grids_for_ags", lambda ags, parallel=True: dict(Run.outcomes))
    monkeypatch.setattr(generate, "run_mv_stage_after_lv", lambda plz, parallel, params: Run.mv.append(plz))
    return Run


def run(argv, capsys):
    """Return (exit code, stdout) of ``pylovo-generate argv``."""
    try:
        generate.main(argv)
        code = 0
    except SystemExit as stopped:
        code = stopped.code
    return code, capsys.readouterr().out


def test_a_run_where_every_plz_has_grids_exits_0(lv, capsys):
    lv.outcomes = {80803: GENERATED, 80804: EXISTS}
    code, out = run(["--plz", "80803", "80804"], capsys)
    assert code == 0 and "✗" not in out


def test_failed_and_empty_plz_give_one_summary_line_and_exit_code_4(lv, capsys):
    lv.outcomes = {80803: FAILED, 80804: EMPTY, 80805: GENERATED, 80806: EXISTS, 80807: GENERATED}
    code, out = run(["--plz", "80807", "80806", "80805", "80804", "80803"], capsys)
    assert code == generate.EXIT_PLZ_NOT_GENERATED == 4
    summary = [line for line in out.splitlines() if line.startswith("✗")]
    assert summary == ["✗ 2 of 5 PLZ not generated: 80803 (failed), 80804 (empty)"]


@pytest.mark.parametrize("outcome", [FAILED, EMPTY])
def test_a_single_plz_without_grids_exits_with_code_4(lv, capsys, outcome):
    lv.outcomes = {80803: outcome}
    code, out = run(["--plz", "80803"], capsys)
    assert code == 4 and f"✗ 1 of 1 PLZ not generated: 80803 ({outcome.value})" in out


def test_a_municipality_run_reports_its_plz_too(lv, capsys):
    lv.outcomes = {80803: GENERATED, 80804: FAILED}
    code, out = run(["--ags", "9162000"], capsys)
    assert code == 4 and "✗ 1 of 2 PLZ not generated: 80804 (failed)" in out


def test_the_mv_stage_runs_only_for_the_plz_with_lv_grids(lv, capsys):
    lv.outcomes = {80805: GENERATED, 80803: FAILED, 80804: EMPTY, 80802: EXISTS}
    code, _ = run(["--plz", "80805", "80803", "80804", "80802", "--mv"], capsys)
    assert lv.mv == [[80802, 80805]]            # the PLZ that failed or are empty are left out
    assert code == 4                            # and the run still reports them


def test_the_mv_stage_is_skipped_when_no_plz_has_lv_grids(lv, capsys):
    lv.outcomes = {80803: FAILED, 80804: EMPTY}
    code, out = run(["--plz", "80803", "80804", "--mv"], capsys)
    assert lv.mv == [] and code == 4
    assert "MV stage skipped: no PLZ of this run has LV grids" in out


def test_the_mv_stage_is_not_mentioned_without_mv(lv, capsys):
    lv.outcomes = {80803: FAILED}
    _, out = run(["--plz", "80803", "--no-mv"], capsys)
    assert lv.mv == [] and "MV stage" not in out


def test_a_failing_mv_stage_stays_exit_code_1(lv, monkeypatch, capsys):
    lv.outcomes = {80803: GENERATED, 80804: FAILED}

    def failing_stage(plz, parallel, params):
        raise SystemExit(1)

    monkeypatch.setattr(generate, "run_mv_stage_after_lv", failing_stage)
    code, _ = run(["--plz", "80803", "80804", "--mv"], capsys)
    assert code == 1


def test_the_create_functions_return_the_outcome_of_every_plz(monkeypatch):
    class FakeGenerator:
        def __init__(self, plz=None):
            pass

        def generate_grid_for_single_plz(self, plz, analyze_grids):
            return EMPTY

        def generate_grid_for_multiple_plz(self, df_plz, analyze_grids, parallel):
            return {int(p): FAILED for p in df_plz["plz"]}

    monkeypatch.setattr(generate, "GridGenerator", FakeGenerator)
    monkeypatch.setattr(generate, "USE_INFDB", True)
    assert generate.create_grid_single_plz(80803) == {80803: EMPTY}
    assert generate.create_grid_multiple_plz([80803, 80804]) == {80803: FAILED, 80804: FAILED}
