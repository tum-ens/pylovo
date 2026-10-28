"""Steps 1-4 of the classification and the sample set: which PLZ stay in it (fakes, no database)."""
import logging
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest

import pylovo.classification.clustering.prepare_data_for_clustering as pdc
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo.grid_generator import GenerationOutcome, GridGenerator


class RecordingDb:
    """Every call is recorded and answers ``None``, except the few queries the code under test needs."""

    def __init__(self, calls=None):
        self.calls = [] if calls is None else calls
        self.logger = logging.getLogger("classification-pipeline-test")

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args))

        return record

    def get_list_from_plz(self, plz):
        return [(1, 1)]

    def is_grid_generated(self, plz):
        return True

    def is_grid_analyzed(self, plz):
        return False


def test_a_failing_generation_leaves_the_sample_set_alone():
    gg = GridGenerator.__new__(GridGenerator)
    gg.dbc, gg.logger = RecordingDb(), logging.getLogger("classification-pipeline-test")

    def fail():
        raise RuntimeError("boom")

    gg.generate_grid = fail
    assert gg.generate_grid_for_single_plz(80331) == GenerationOutcome.FAILED
    assert "delete_plz_from_sample_set_table" not in [name for name, _ in gg.dbc.calls]


def test_a_failing_analysis_leaves_the_sample_set_alone():
    pc = ParameterCalculator.__new__(ParameterCalculator)
    pc.dbc = RecordingDb()

    def fail(plz):
        raise RuntimeError("boom")

    pc.analyze_basic_parameters_for_plz = fail
    with pytest.raises(RuntimeError, match="boom"):
        pc.analyze_parameters_for_plz(80331)
    assert "delete_plz_from_sample_set_table" not in [name for name, _ in pc.dbc.calls]


@pytest.fixture()
def pipeline(monkeypatch):
    """Run ``prepare_data_for_clustering`` on four sampled PLZ with scripted generation outcomes."""
    record = {"deleted": [], "analysed": []}
    samples = pd.DataFrame({"plz": [80001, 80002, 80003, 80004], "ags": [1, 2, 3, 4]})
    outcomes = {80001: GenerationOutcome.GENERATED, 80002: GenerationOutcome.FAILED,
                80003: GenerationOutcome.EMPTY, 80004: GenerationOutcome.EXISTS}

    class FakeGenerator:
        def generate_grid_for_multiple_plz(self, df_plz, analyze_grids, parallel):
            record["generated"] = list(df_plz["plz"])
            record["generation_parallel"] = parallel
            return outcomes

    def fake_key_figures(plz_list, parallel):
        record["analysed"] += list(plz_list)
        record["key_figures_parallel"] = parallel

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def delete_plz_from_sample_set_table(self, classification_id, plz):
            record["deleted"].append((classification_id, plz))

    monkeypatch.setattr(pdc, "create_sample_set", lambda restrict_to_postcode_result: None)
    monkeypatch.setattr(pdc, "get_sample_set", lambda: samples)
    monkeypatch.setattr(pdc, "USE_INFDB", True)
    monkeypatch.setattr(pdc, "CLASSIFICATION_VERSION", 7)
    monkeypatch.setattr(pdc, "GridGenerator", FakeGenerator)
    monkeypatch.setattr(pdc, "analyze_grid_parameters_for_multiple_plz", fake_key_figures)
    monkeypatch.setattr(pdc.dbc, "DatabaseClient", FakeClient)
    return record


def test_plz_without_grids_leave_the_sample_and_are_not_analysed(pipeline):
    assert pdc.prepare_data_for_clustering() == [80002, 80003], "the PLZ removed from the sample set"
    assert pipeline["generated"] == [80001, 80002, 80003, 80004]
    assert sorted(pipeline["deleted"]) == [("7", 80002), ("7", 80003)]
    assert pipeline["analysed"] == [80001, 80004], "an already generated PLZ stays in the sample"


@pytest.mark.parametrize("argument, configured, expected", [(None, True, True), (None, False, False),
                                                             (False, True, False), (True, False, True)])
def test_steps_3_and_4_run_in_parallel_as_configured_or_as_asked(pipeline, monkeypatch, argument, configured,
                                                                  expected):
    monkeypatch.setattr(pdc, "PARALLEL", configured)
    pdc.prepare_data_for_clustering(parallel=argument)
    assert pipeline["generation_parallel"] is expected and pipeline["key_figures_parallel"] is expected


# ---------------------------------------------------------------------------------------- step 4 per PLZ
import pylovo.analysis.parameter_calculation as pcm  # noqa: E402
from pylovo.analysis.parameter_calculation import GridParameterCounts  # noqa: E402


class KeyFigureDb:
    """The queries of the per-grid analysis: three grids, the second one already has parameters."""

    def __init__(self, analysed=True):
        self.analysed = analysed
        self.logger = logging.getLogger("classification-pipeline-test")
        self.rollbacks = 0
        self.closed = False

    def is_grid_analyzed(self, plz):
        return self.analysed

    def get_list_from_plz(self, plz):
        return [(1, 1), (1, 2), (2, 1)]

    def has_clustering_parameters(self, plz, kcid, bcid, key_figures_version):
        return (kcid, bcid) == (1, 2)

    def rollback_changes(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def calculator(db, fail=()):
    pc = ParameterCalculator.__new__(ParameterCalculator)
    pc.dbc, pc.computed = db, []

    def analyze_single_grid(bcid, kcid):
        if (kcid, bcid) in fail:
            raise RuntimeError("no net")
        pc.computed.append((pc.plz, kcid, bcid))

    pc.analyze_single_grid = analyze_single_grid
    return pc


def test_the_per_grid_analysis_counts_and_rolls_a_failed_grid_back(capsys):
    db = KeyFigureDb()
    pc = calculator(db, fail={(1, 1)})
    assert pc.calculate_grid_parameters(80001) == GridParameterCounts("analysed", calculated=1, skipped=1, failed=1)
    assert pc.computed == [(80001, 2, 1)], "the failed grid does not stop the next one"
    assert db.rollbacks == 1, "the aborted transaction is rolled back"
    assert capsys.readouterr().out == "", "quiet without progress"


def test_the_single_plz_analysis_keeps_its_answer_and_its_lines(capsys):
    pc = calculator(KeyFigureDb())
    assert pc.analyze_grid_parameters_for_plz(80001) == "analysed"
    out = capsys.readouterr().out
    assert "Checking 3 grids for PLZ 80001" in out and "Calculating parameters for grid kcid=2, bcid=1" in out
    assert "Calculated new: 2, Skipped existing: 1." in out
    assert calculator(KeyFigureDb(analysed=False)).analyze_grid_parameters_for_plz(80001) == "not_analysed"


class FakeCalculator:
    """Stands in for ParameterCalculator: scripted counts, a later PLZ finishes first."""

    instances: list = []
    order: list = []

    def __init__(self):
        self.dbc = KeyFigureDb()
        FakeCalculator.instances.append(self)

    def calculate_grid_parameters(self, plz, progress=False):
        import time

        if plz == 80003:
            raise ConnectionError("server closed the connection")
        time.sleep(0.05 if plz == 80001 else 0)  # the first PLZ finishes last in the workers
        FakeCalculator.order.append(plz)
        return GridParameterCounts("analysed", calculated=plz % 10, skipped=1)


class RecordingPool(ThreadPoolExecutor):
    """ProcessPoolExecutor stand-in (threads, same interface) that records the number of workers."""

    workers: list = []

    def __init__(self, max_workers):
        RecordingPool.workers.append(max_workers)
        super().__init__(max_workers=max_workers)


@pytest.fixture()
def fake_step_4(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # the log of the run
    FakeCalculator.instances, FakeCalculator.order, RecordingPool.workers = [], [], []
    monkeypatch.setattr(pcm, "ParameterCalculator", FakeCalculator)
    monkeypatch.setattr(pcm, "ProcessPoolExecutor", RecordingPool)
    monkeypatch.setattr(pcm, "N_JOBS", 2)
    return FakeCalculator


EXPECTED = {80001: GridParameterCounts("analysed", 1, 1), 80002: GridParameterCounts("analysed", 2, 1),
            80003: GridParameterCounts("failed"), 80004: GridParameterCounts("analysed", 4, 1)}


def test_workers_compute_whole_plz_with_their_own_connection_and_answer_in_the_input_order(fake_step_4, capsys):
    result = pcm.analyze_grid_parameters_for_multiple_plz([80001, 80002, 80003, 80004, 80002], parallel=True)
    assert list(result) == [80001, 80002, 80003, 80004], "input order, every PLZ once"
    assert result == EXPECTED, "a PLZ that fails is counted, the others go on"
    assert RecordingPool.workers == [2], "N_JOBS workers"
    assert len(fake_step_4.instances) == 4 and all(c.dbc.closed for c in fake_step_4.instances), "one per PLZ"
    assert fake_step_4.order[-1] == 80001, "the PLZ really finished in another order"
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Per-grid parameters of 4 PLZ in 2 worker processes"
    progress = [line for line in lines if line.startswith("Per-grid parameters of PLZ")]
    assert [line.split(" (")[1].split(")")[0] for line in progress] == ["1/4", "2/4", "3/4", "4/4"]
    assert any(line.startswith("Per-grid parameters of PLZ 80003 (") and line.endswith("): 0 calculated, 0 existing "
                                                                                    "(failed)") for line in progress)
    assert lines[-1] == "Per-grid parameters of 4 PLZ: 7 calculated, 3 existing, 0 failed"


@pytest.mark.parametrize("parallel, n_jobs, plz_list", [(False, 2, [80001, 80002, 80003, 80004]),
                                                        (True, 1, [80001, 80002, 80003, 80004]),
                                                        (None, 2, [80001])])
def test_without_workers_one_calculator_runs_the_plz_in_order(fake_step_4, monkeypatch, parallel, n_jobs,
                                                              plz_list):
    monkeypatch.setattr(pcm, "N_JOBS", n_jobs)
    monkeypatch.setattr(pcm, "PARALLEL", True)
    result = pcm.analyze_grid_parameters_for_multiple_plz(plz_list, parallel=parallel)
    assert RecordingPool.workers == [], "no worker process"
    assert len(fake_step_4.instances) == 1 and fake_step_4.instances[0].dbc.closed
    assert fake_step_4.order == [plz for plz in plz_list if plz != 80003], "the given order"
    assert result == {plz: EXPECTED[plz] for plz in plz_list}


def test_parallel_none_takes_the_configuration(fake_step_4, monkeypatch):
    monkeypatch.setattr(pcm, "PARALLEL", False)
    pcm.analyze_grid_parameters_for_multiple_plz([80001, 80002])
    assert RecordingPool.workers == []
    monkeypatch.setattr(pcm, "PARALLEL", True)
    pcm.analyze_grid_parameters_for_multiple_plz([80001, 80002])
    assert RecordingPool.workers == [2]


def test_the_shipped_configuration_runs_steps_3_and_4_in_worker_processes():
    """PARALLEL and N_JOBS_PERCENT of config_generation_lv.yaml are the only knobs of pylovo-generate and steps 3 and 4."""
    from pathlib import Path

    import yaml

    shipped = yaml.safe_load((Path(__file__).resolve().parents[1] / "config" / "config_generation_lv.yaml").read_text())
    assert shipped["PARALLEL"] is True and shipped["N_JOBS_PERCENT"] == 50
    assert pdc.PARALLEL is pcm.PARALLEL, "the classification reads the knob of the grid generation"
