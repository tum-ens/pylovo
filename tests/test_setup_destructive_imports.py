"""The setup imports do not delete stored results: postcodes are upserted, never deleted (fake connections)."""
import inspect
from types import SimpleNamespace

import pytest

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
