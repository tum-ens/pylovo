"""``DatabaseConstructor.ogr_to_db`` and ``row_count`` with a fake ogr2ogr and a fake cursor (no database)."""
from types import SimpleNamespace

import pytest

from pylovo.database import database_constructor
from pylovo.database.database_constructor import DUPLICATE_KEY_ERROR, DatabaseConstructor

STDERR = (b"0...10...20...100 - done.\n"
          b"ERROR 1: INSERT command for new feature failed. ERROR:  duplicate key value violates unique "
          b'constraint "transformers_pkey" DETAIL:  Key (osm_id)=(node/1) already exists.\n'
          b"ERROR 1: INSERT command for new feature failed. ERROR:  duplicate key value violates unique "
          b'constraint "transformers_pkey" DETAIL:  Key (osm_id)=(node/2) already exists.\n'
          b'ERROR 1: INSERT command for new feature failed. ERROR:  invalid input syntax for type integer: "x" '
          b"DETAIL:  Row 3.\n")


@pytest.fixture()
def constructor(monkeypatch, tmp_path):
    runs = []

    def fake_run(command, **options):
        runs.append((command, options))
        return SimpleNamespace(stderr=STDERR)

    monkeypatch.setattr(database_constructor.subprocess, "run", fake_run)
    built = DatabaseConstructor(SimpleNamespace())
    monkeypatch.setattr(built, "table_exists", lambda table_name: True)
    built.runs, built.layer = runs, [{"path": tmp_path / "trafos.geojson", "table_name": "transformers"}]
    (tmp_path / "trafos.geojson").write_text("{}")
    return built


def test_the_distinct_errors_of_the_skipped_rows_are_returned(constructor, capsys):
    errors = constructor.ogr_to_db(constructor.layer, skip_failures=True)
    assert len(errors) == 2                                    # the two duplicate keys are one message
    assert sum(DUPLICATE_KEY_ERROR in error for error in errors) == 1
    assert any("invalid input syntax for type integer" in error for error in errors)
    assert "-skipfailures" in constructor.runs[0][0]
    assert "likely due to importing already existing data" in capsys.readouterr().out


def test_without_skip_failures_nothing_is_skipped_and_nothing_is_returned(constructor):
    assert constructor.ogr_to_db(constructor.layer) == []
    assert "-skipfailures" not in constructor.runs[0][0]


class Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.log.append(str(query))

    def fetchone(self):
        return (7,)


def test_row_count_is_zero_for_a_missing_table_and_counts_otherwise(monkeypatch):
    log = []
    conn = SimpleNamespace(cursor=lambda: Cursor(log), commit=lambda: log.append("COMMIT"))
    counter = DatabaseConstructor(SimpleNamespace(conn=conn))
    monkeypatch.setattr(counter, "table_exists", lambda table_name: False)
    assert counter.row_count("transformers") == 0 and log == []
    monkeypatch.setattr(counter, "table_exists", lambda table_name: True)
    assert counter.row_count("transformers") == 7 and log[-1] == "COMMIT"
