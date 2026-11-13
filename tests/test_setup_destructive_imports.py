"""The setup imports do not delete stored results: postcodes are upserted, never deleted (fake connections)."""
import inspect
from contextlib import contextmanager
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy.dialects import postgresql

from pylovo.database import database_constructor
from pylovo.database.database_constructor import DatabaseConstructor


class Cursor:
    def __init__(self, log, fail_on=None, found=True):
        self.log, self.fail_on, self.found = log, fail_on, found

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.log.append(("execute", str(query)))

    def executemany(self, query, rows):
        self.log.append(("executemany", str(query)))
        if self.fail_on:
            raise self.fail_on

    def fetchone(self):
        return (self.found,)


def fake_psycopg2_connection(log, fail_on=None, found=True):
    return SimpleNamespace(cursor=lambda: Cursor(log, fail_on, found),
                           commit=lambda: log.append(("commit",)), rollback=lambda: log.append(("rollback",)))


def statements(log, kind="execute"):
    return [entry[1] for entry in log if entry[0] == kind]


def constructor_with(log, *, tables, fail_on=None, found=True):
    built = DatabaseConstructor(SimpleNamespace(conn=fake_psycopg2_connection(log, fail_on, found)))
    built.table_exists = lambda table_name: table_name in tables
    return built


@pytest.fixture
def infdb_rows(monkeypatch):
    class Client:
        def fetch_all_postcodes_from_infdb(self):
            return [(80331, "Munich", 1.0, 100, "POLYGON EMPTY")]

    monkeypatch.setattr(database_constructor, "InfdbClient", Client)


def test_postcodes_from_infdb_are_upserted_in_one_commit_without_a_delete(infdb_rows):
    log = []
    constructor_with(log, tables={"postcode"}).load_postcode_from_infdb()

    assert not any("DELETE" in entry[1].upper() for entry in log if len(entry) > 1)
    assert "ON CONFLICT (plz) DO UPDATE" in statements(log, "executemany")[0]
    assert [entry for entry in log if entry[0] == "commit"] == [("commit",)]


def test_a_failing_postcode_insert_rolls_back_and_raises(infdb_rows):
    log = []
    constructor = constructor_with(log, tables={"postcode"}, fail_on=RuntimeError("insert failed"))

    with pytest.raises(RuntimeError, match="insert failed"):
        constructor.load_postcode_from_infdb()

    assert ("rollback",) in log and ("commit",) not in log


class SqlaConnection:
    dialect = postgresql.dialect()

    def __init__(self, log):
        self.log = log

    def execute(self, statement):
        self.log.append(("sqla", str(statement)))


def csv_constructor(log, monkeypatch, tmp_path, *, tables, result_has_rows):
    @contextmanager
    def begin():
        log.append(("begin",))
        yield SqlaConnection(log)
        log.append(("end",))

    monkeypatch.setattr(pd.DataFrame, "to_sql", lambda self, name, con, **kwargs: log.append(("to_sql", name, con)))
    built = constructor_with(log, tables=tables, found=result_has_rows)
    built.dbc.sqla_engine = SimpleNamespace(begin=begin)
    return built


def write_csv(tmp_path, name):
    path = tmp_path / f"{name}.csv"
    path.write_text("plz,einwohner\n80331,100\n")
    return {"path": path}


def test_the_postcodes_are_not_replaced_while_results_exist(monkeypatch, tmp_path):
    log = []
    constructor = csv_constructor(log, monkeypatch, tmp_path, tables={"postcode", "postcode_result"},
                                  result_has_rows=True)

    with pytest.raises(RuntimeError, match="postcode_result has rows"):
        constructor.csv_to_db([write_csv(tmp_path, "postcode")])

    assert not any(entry[0] in {"sqla", "begin", "to_sql"} for entry in log)


@pytest.mark.parametrize("table, tables, result_has_rows", [
    ("postcode", {"postcode", "postcode_result"}, False),
    ("postcode", {"postcode"}, True),
    ("other", {"other", "postcode_result"}, True),
])
def test_delete_and_insert_run_on_one_connection(monkeypatch, tmp_path, table, tables, result_has_rows):
    log = []
    constructor = csv_constructor(log, monkeypatch, tmp_path, tables=tables, result_has_rows=result_has_rows)

    with pytest.warns(UserWarning, match="overwritten"):
        constructor.csv_to_db([write_csv(tmp_path, table)])

    kinds = [entry[0] for entry in log if entry[0] in {"begin", "sqla", "to_sql", "end"}]
    assert kinds == ["begin", "sqla", "to_sql", "end"]
    assert statements(log, "sqla") == [f'DELETE FROM pylovo."{table}"']
    assert isinstance(next(entry for entry in log if entry[0] == "to_sql")[2], SqlaConnection)


def test_transformers_are_only_imported_and_never_deleted(monkeypatch):
    log = []
    constructor = constructor_with(log, tables={"transformers"})
    imported = []
    constructor.ogr_to_db = imported.extend
    monkeypatch.setattr(database_constructor, "get_processed_trafos", lambda relation_id: "trafos.geojson")

    constructor.transformers_to_db()

    assert log == []                                            # no statement of its own, in particular no DELETE
    assert imported == [{"path": "trafos.geojson", "table_name": "transformers"}]
    assert "clear_existing" not in inspect.signature(constructor.transformers_to_db).parameters
    with pytest.raises(TypeError):
        constructor.transformers_to_db(clear_existing=True)
