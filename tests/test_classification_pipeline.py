"""Steps 1-4 of the classification and the sample set: which PLZ stay in it (fakes, no database)."""
import logging

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
        def generate_grid_for_multiple_plz(self, df_plz, analyze_grids):
            record["generated"] = list(df_plz["plz"])
            return outcomes

    class FakeParameterCalculator:
        def analyze_grid_parameters_for_plz(self, plz):
            record["analysed"].append(plz)

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
    monkeypatch.setattr(pdc, "ParameterCalculator", FakeParameterCalculator)
    monkeypatch.setattr(pdc.dbc, "DatabaseClient", FakeClient)
    return record


def test_plz_without_grids_leave_the_sample_and_are_not_analysed(pipeline):
    pdc.prepare_data_for_clustering()
    assert pipeline["generated"] == [80001, 80002, 80003, 80004]
    assert sorted(pipeline["deleted"]) == [("7", 80002), ("7", 80003)]
    assert pipeline["analysed"] == [80001, 80004], "an already generated PLZ stays in the sample"
