"""``USE_EXTERNAL_BASEDATA``: the InfDB source schemas are read through the ``INFDB_*`` connection, never the pylovo one.

Fakes stand in for psycopg2: a connection records how it was made and a cursor records every statement.
"""
from __future__ import annotations

import psycopg2
import pytest
from pylovo import config_loader as cl
from pylovo_api import db, queries
from pylovo_api.coverage import InputCoverage

INFDB = {"INFDB_HOST": "infdb.example", "INFDB_PORT": "5060", "INFDB_DBNAME": "infdb", "INFDB_USER": "reader",
         "INFDB_PASSWORD": "secret", "INFDB_SOURCE_SCHEMA": "basedata", "INFDB_OPENDATA_SCHEMA": "opendata"}


class Cursor:
    """Records the statements and answers by marker (tuples, or dicts like a RealDictCursor)."""

    def __init__(self, answers=None, as_dict=False):
        self.answers, self.as_dict, self.statements, self.params = answers or {}, as_dict, [], []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params)

    @property
    def description(self):
        return None if self.statements[-1].startswith("SET") else [("column",)]

    def _rows(self):
        return next((rows for marker, rows in self.answers.items() if marker in self.statements[-1]), [])

    def fetchall(self):
        return [dict(r) for r in self._rows()] if self.as_dict else list(self._rows())

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Connection:
    closed = 0

    def __init__(self, cursor, kwargs=None):
        self._cursor, self.kwargs, self.session, self.commits, self.rollbacks = cursor, kwargs or {}, {}, 0, 0

    def cursor(self, **kwargs):
        return self._cursor

    def set_session(self, **kwargs):
        self.session.update(kwargs)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = 1


class Made(list):
    """The connections made, in order; ``cursors`` maps a database name to the cursor that answers for it."""

    def __init__(self):
        super().__init__()
        self.cursors: dict[str, Cursor] = {}


@pytest.fixture()
def connections(monkeypatch):
    made = Made()

    def connect(**kwargs):
        made.append(Connection(made.cursors.setdefault(kwargs["dbname"], Cursor()), kwargs))
        return made[-1]

    monkeypatch.setattr(psycopg2, "connect", connect)
    return made


def configure(monkeypatch, external: bool):
    values = dict(INFDB) if external else {"INFDB_HOST": cl.HOST, "INFDB_PORT": cl.PORT, "INFDB_DBNAME": cl.DBNAME,
                                           "INFDB_USER": cl.DBUSER, "INFDB_PASSWORD": cl.PASSWORD,
                                           "INFDB_SOURCE_SCHEMA": "basedata", "INFDB_OPENDATA_SCHEMA": "opendata"}
    for name, value in {**values, "USE_INFDB": True, "USE_EXTERNAL_BASEDATA": external}.items():
        monkeypatch.setattr(cl, name, value, raising=False)


@pytest.fixture()
def external(monkeypatch):
    configure(monkeypatch, True)


@pytest.fixture()
def local(monkeypatch):
    configure(monkeypatch, False)


# --------------------------------------------------------------------------- connection selection
def test_the_infdb_is_external_only_with_the_switch_and_is_shown_in_the_settings(monkeypatch):
    configure(monkeypatch, False)
    assert not db.external_basedata() and db.settings()["infdb_target"] is None
    configure(monkeypatch, True)
    assert db.external_basedata() and db.settings()["use_external_basedata"]
    assert db.settings()["infdb_target"] == "infdb.example:5060/infdb" and "secret" not in str(db.settings())
    monkeypatch.setattr(cl, "USE_INFDB", False)  # file mode has no InfDB at all
    assert not db.external_basedata()


def test_the_infdb_connection_is_made_like_infdb_client_does(external, connections):
    conn = db.connect_source()
    assert connections[0].kwargs == {"dbname": "infdb", "user": "reader", "password": "secret", "host": "infdb.example",
                                     "port": "5060", "connect_timeout": 5}  # no startup options: PgBouncer < 1.20
    assert conn.session == {"readonly": True}


def test_source_cursor_reads_the_infdb_in_a_read_only_transaction(external, connections, monkeypatch):
    monkeypatch.setattr(db, "_connect", lambda *a, **k: pytest.fail("the pylovo connection must not be used"))
    connections.cursors["infdb"] = Cursor({"citydb": [{"ok": True}]}, as_dict=True)
    with db.source_cursor() as cur:
        assert db.fetch_one("SELECT citydb", None, cur) == {"ok": True}
    infdb = connections.cursors["infdb"]
    assert infdb.statements[0].startswith("SET LOCAL search_path = %s, public;") and infdb.params[0][0] == "basedata"
    assert (connections[0].commits, connections[0].closed) == (1, 1)
    with pytest.raises(RuntimeError), db.source_cursor():
        raise RuntimeError("boom")
    assert connections[1].rollbacks == 1 and connections[1].closed


def test_a_local_infdb_is_read_through_the_cursor_of_the_pylovo_connection(local, connections, monkeypatch):
    own = Cursor()
    monkeypatch.setattr(db, "_connect", lambda readonly, **kwargs: Connection(own))
    with db.source_cursor() as cur:
        assert cur is own
    reused = Cursor()
    with db.source_cursor(reused) as cur:
        assert cur is reused
    assert connections == []  # no second connection


def test_the_coverage_engine_gets_a_second_connection_only_for_an_external_infdb(connections, monkeypatch):
    configure(monkeypatch, False)
    cov = InputCoverage()
    cov._connect()
    assert len(connections) == 1 and cov._src is cov._conn and "search_path=basedata,public" in connections[0].kwargs["options"]
    assert "infdb_target" not in cov._signature()
    connections.clear()
    configure(monkeypatch, True)
    cov = InputCoverage()
    cov._connect()
    pylovo, infdb = connections
    assert pylovo.kwargs["dbname"] == cl.DBNAME and "default_transaction_read_only=on" in pylovo.kwargs["options"]
    assert infdb.kwargs["dbname"] == "infdb" and infdb.kwargs["host"] == "infdb.example" and "options" not in infdb.kwargs
    assert cov._conn is pylovo and cov._src is infdb and infdb.session == {"readonly": True}
    assert cov._signature()["infdb_target"] == "infdb.example:5060/infdb"  # the cached counts belong to this InfDB
    cov._close()
    assert pylovo.closed and infdb.closed and cov._conn is cov._src is None


# --------------------------------------------------------------------------- what is read where
def engine_on(pylovo: Cursor, infdb: Cursor | None) -> InputCoverage:
    cov = InputCoverage()
    cov.mode = "infdb"
    cov.settings = {"infdb_source_schema": "basedata", "infdb_opendata_schema": "opendata",
                    "infdb_target": "infdb.example:5060/infdb" if infdb else None}
    cov._conn = Connection(pylovo)
    cov._src = Connection(infdb) if infdb else cov._conn
    return cov


def test_the_engine_reads_source_tables_from_the_infdb_and_pylovo_tables_from_pylovo():
    pylovo = Cursor({"COALESCE(sum(plz), 0)": [("1:80803", "1:80803")]})
    infdb = Cursor({"to_regclass('public.changelog')": [(True,)], "SELECT to_regclass(%s) IS NOT NULL": [(True,)],
                    "count(*) || ':' || max(plz)": [("1:80803",)], "postcodes_germany WHERE": [("80803",)]})
    cov = engine_on(pylovo, infdb)
    assert cov._probe() == {"buildings": None, "ways": None, "lines": None}
    cov._read_changelog()
    cov._read_sources(force=True)
    assert cov.changelog_present and cov.infdb_polygons == {"80803"}
    assert pylovo.statements and all("pylovo." in sql and "postcodes_germany" not in sql for sql in pylovo.statements)
    queries_on_infdb = [sql for sql in infdb.statements if not sql.startswith("SET")]
    assert queries_on_infdb and not any("pylovo." in sql for sql in queries_on_infdb)
    assert infdb.statements[0] == ("SET LOCAL search_path = %s, public; SET LOCAL statement_timeout = %s; "
                                   "SET LOCAL lock_timeout = %s")  # per transaction, no startup options
    assert cov._src.commits == len(queries_on_infdb) and cov._src.rollbacks == 0


def test_a_slow_statement_on_the_infdb_sets_its_own_timeout_and_a_failure_rolls_back():
    infdb = Cursor()
    cov = engine_on(Cursor(), infdb)
    cov._source_query("SELECT 1", None, 1_800_000)
    assert infdb.params[0] == ("basedata", 1_800_000, 2_000)
    cov._source_query("SELECT 1")
    assert infdb.params[2][1] == 60_000  # the default of the chunk statements

    def failing(sql, params=None):
        raise psycopg2.errors.QueryCanceled("statement timeout")

    infdb.execute = failing
    with pytest.raises(psycopg2.errors.QueryCanceled):
        cov._source_query("SELECT 1")
    assert cov._src.rollbacks == 1


def test_a_local_engine_keeps_its_one_connection_and_its_session_options():
    pylovo = Cursor()
    cov = engine_on(pylovo, None)
    cov._source_query("SELECT 1")
    cov._source_query("SELECT 2", None, 5_000)
    assert pylovo.statements == ["SELECT 1", "SET statement_timeout = %s", "SELECT 2", "RESET statement_timeout"]


def test_status_counts_the_infdb_buildings_through_the_infdb_connection(external, connections, monkeypatch):
    pylovo = Cursor({"current_setting": [{"version": "16"}], "to_regnamespace": [{"ok": False}], "pg_proc": [{"n": 0}]},
                    as_dict=True)
    monkeypatch.setattr(db, "_connect", lambda readonly, **kwargs: Connection(pylovo))
    connections.cursors["infdb"] = Cursor({"to_regclass": [{"ok": True}], "reltuples": [{"n": 1234}],
                                           "count(*)": [{"n": 1500}]}, as_dict=True)
    info = queries.status()
    assert info["connected"] and info["infdb_buildings"] == 1500 and "error" not in info
    assert info["settings"]["infdb_target"] == "infdb.example:5060/infdb"
    assert not any("basedata" in sql for sql in pylovo.statements)  # the pylovo database has no basedata schema
    assert any('"basedata".buildings' in sql for sql in connections.cursors["infdb"].statements)
    connections.cursors["infdb"].execute = lambda *a: (_ for _ in ()).throw(psycopg2.OperationalError("refused"))
    info = queries.status()  # an unreachable InfDB is reported on its own, the pylovo database is fine
    assert info["connected"] and "error" not in info and "refused" in info["infdb_error"] and info["infdb_buildings"] is None
