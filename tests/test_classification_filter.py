"""The filters of the classification run in the SQL that reads the parameters: semantics on an SQLite stand-in."""
import pytest

import pylovo.classification.database_communication.database_communication as dcm
from classification_helpers import make_sqlite_dc

# (plz, kcid, bcid) of the grids that classification 1 keeps: grid 2 is too long, grid 3 has a building with 200
# households, grid 4 is the small filling grid, grid 6 has no max_trafo_dis (never filters)
DEFAULT_KEPT = {(80001, 1, 1), (80001, 1, 4), (80002, 1, 1), (80002, 1, 2)}
ADDITIONAL_KEPT = {(80001, 1, 1), (80002, 1, 1), (80002, 1, 2)}


@pytest.fixture()
def dc(monkeypatch):
    return make_sqlite_dc(monkeypatch)


def kept(frame):
    return set(zip(frame["plz"], frame["kcid"], frame["bcid"]))


def test_default_filters_leave_out_long_and_implausible_grids(dc):
    assert kept(dc.get_clustering_parameters_for_classification_version(False)) == DEFAULT_KEPT


def test_additional_filtering_also_leaves_out_the_small_filling_grid(dc):
    assert kept(dc.get_clustering_parameters_for_classification_version(True)) == ADDITIONAL_KEPT


def test_a_missing_value_never_filters(dc):
    """Grid 6 has no max_trafo_dis: the old UPDATE did not flag it either."""
    assert (80002, 1, 2) in kept(dc.get_clustering_parameters_for_classification_version(False))


def test_reading_without_an_argument_follows_the_config(dc, monkeypatch):
    """Without a stored choice (clustered before migration 0014, here no row) the configuration applies."""
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", True)
    assert kept(dc.get_clustering_parameters_for_classification_version()) == ADDITIONAL_KEPT
    assert kept(dc.get_clustering_parameters_for_classification_version(False)) == DEFAULT_KEPT
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", False)
    assert kept(dc.get_clustering_parameters_for_classification_version()) == DEFAULT_KEPT


@pytest.mark.parametrize("reader", ["get_clustering_parameters_for_classification_version",
                                    "municipal_register_with_clustering_parameters_for_classification_version"])
def test_reading_without_an_argument_follows_the_choice_stored_with_the_classification(dc, monkeypatch, reader):
    """A later run writes its ADDITIONAL_FILTERING to the configuration: the grids of this classification stay."""
    def read(stored):
        dc.dbc.cur.fetchone_values = [(["no_branches"], 4, 4, stored)]
        return kept(getattr(dc, reader)())

    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", True)
    assert read(False) == DEFAULT_KEPT
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", False)
    assert read(True) == ADDITIONAL_KEPT
    assert read(None) == DEFAULT_KEPT


def test_the_register_reader_applies_the_same_filters(dc):
    frame = dc.municipal_register_with_clustering_parameters_for_classification_version(True)
    assert kept(frame) == ADDITIONAL_KEPT and {"pop", "regio7", "name_city", "ags"} <= set(frame.columns)
    assert kept(dc.municipal_register_with_clustering_parameters_for_classification_version(False)) == DEFAULT_KEPT


def test_only_the_grids_of_the_classification_are_read(dc, monkeypatch):
    monkeypatch.setattr(dcm, "CLASSIFICATION_VERSION", 2)  # samples PLZ 80001 only
    assert kept(dc.get_clustering_parameters_for_classification_version(False)) == {(80001, 1, 1), (80001, 1, 4)}


def test_earlier_runs_leave_no_trace(dc, monkeypatch):
    """CLASSIF-04: the old flag was global and sticky, so a run with other filters changed the grids of every other run."""
    def read(additional):
        return kept(dc.get_clustering_parameters_for_classification_version(additional))

    first = read(False)
    monkeypatch.setattr(dcm, "CLASSIFICATION_VERSION", 2)
    assert read(True) == {(80001, 1, 1)}  # another classification on shared grids, with additional filtering
    monkeypatch.setattr(dcm, "CLASSIFICATION_VERSION", 1)
    assert read(False) == first == DEFAULT_KEPT and read(True) == ADDITIONAL_KEPT
