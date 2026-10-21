"""The summary at the end of a classification run: grids per stage and a warning for every loss (SQLite stand-in)."""
import logging

import pytest

import pylovo.classification.clustering.report_grid_counts as report
import pylovo.classification.database_communication.database_communication as dcm
from classification_helpers import make_sqlite_dc

MAX_DIS = f"max_trafo_dis > {dcm.THRESHOLD_MAX_TRAFO_DIS}"
BUILDING = f"a building with more than {dcm.THRESHOLD_HOUSEHOLDS_PER_BUILDING} households"


@pytest.fixture()
def dc(monkeypatch):
    return make_sqlite_dc(monkeypatch)


def summary(dc, additional=False, plz_without_grids=()):
    grids = dc.get_grid_status_of_sample_set(additional)
    return report.summarize_grid_counts(grids, list(dcm.filter_reasons(additional)), plz_without_grids)


def test_counts_per_stage_with_the_default_filters(dc):
    counts, _ = summary(dc, plz_without_grids=[80099])
    assert counts == {"plz_drawn": 6, "plz_in_sample": 5, "grids": 9, "analysed": 6, "filtered": 2,
                      "filtered_by": {MAX_DIS: 1, BUILDING: 1}, "clustered": 2}


def test_additional_filtering_counts_the_small_grid_under_every_lower_bound(dc):
    counts, _ = summary(dc, additional=True)
    assert counts["filtered"] == 3
    assert counts["filtered_by"] == {MAX_DIS: 1, BUILDING: 1, **{f"{name} < {value}": 1 for name, value in dcm.LOWER_FILTER_THRESHOLDS.items()}}


def test_every_loss_is_named_in_a_warning(dc):
    _, warnings = summary(dc, plz_without_grids=[80099, 80098])
    text = "\n".join(warnings)
    assert "2 of 7 sampled PLZ have no grids and were removed from the sample set: [80098, 80099]" in text
    assert "Sampled PLZ without any grid" in text and "[80005]" in text
    assert "3 of 9 grids have no key figures and are not clustered: 2 in 1 PLZ without PLZ analysis" in text
    assert "run pylovo-analyze --plz 80003" in text and "1 whose calculation failed" in text
    assert "4 grids pass the filters, but 2 are in transformer_classified" in text


def test_a_complete_result_raises_no_warning_about_the_result_table(dc):
    dc.sqlite.executemany("INSERT INTO pylovo.transformer_classified VALUES (?, 1)", [(4,), (6,)])  # grids 1 and 5 exist
    counts, warnings = summary(dc)
    assert counts["clustered"] == 4 == counts["analysed"] - counts["filtered"]
    assert not any("transformer_classified" in text for text in warnings)


def test_a_run_without_losses_has_no_warning(dc):
    dc.sqlite.execute("DELETE FROM pylovo.sample_set WHERE plz IN (80003, 80004, 80005)")
    dc.sqlite.executemany("INSERT INTO pylovo.transformer_classified VALUES (?, 1)", [(4,), (6,)])
    _, warnings = summary(dc)
    assert warnings == []


def test_report_prints_the_stages_and_logs_every_warning(dc, monkeypatch, capsys):
    messages = []

    class Collect(logging.Handler):
        def emit(self, record):
            messages.append((record.levelname, record.getMessage()))

    logger = logging.getLogger("classification-report-test")
    logger.handlers, logger.propagate = [Collect()], False
    dc.dbc.logger = logger
    monkeypatch.setattr(report, "DatabaseCommunication", lambda: dc)
    counts = report.report_grid_counts(additional_filtering=False, plz_without_grids=[80099])
    printed = capsys.readouterr().out
    for label in ("PLZ drawn", "PLZ in the sample set", "Grids of these PLZ", "Grids with key figures", "Grids filtered",
                  f"by {MAX_DIS}", "Grids clustered"):
        assert label in printed
    assert counts["grids"] == 9 and counts["plz_drawn"] == 6
    assert len(messages) == 4 and {level for level, _ in messages} == {"WARNING"}
