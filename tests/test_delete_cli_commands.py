"""``pylovo-delete`` subcommands call the right ``DatabaseClient`` method, and the delete queries of ``ResultsMixin``
run in the documented order (fake client and fake cursor, no database). ``delete classification`` has its own tests in
``test_delete_cli.py``."""
import logging
import sys

import pytest

from pylovo.cli import delete
from pylovo.database.results_mixin import ResultsMixin


class Calls(list):
    """``open``, the client call and ``close`` of each command; ``client`` is the fake client class."""

    client = None


@pytest.fixture
def calls(monkeypatch):
    """Replace ``DatabaseClient`` by a recorder."""
    recorded = Calls()

    class Client:
        error = None

        def __enter__(self):
            recorded.append("open")
            return self

        def __exit__(self, *exc):
            recorded.append("close")
            return False

        def _call(self, *call):
            recorded.append(call)
            if Client.error:
                raise Client.error

        def delete_plz_from_all_tables(self, plz, version_id):
            self._call("networks", plz, version_id)

        def delete_versions_from_all_tables(self, version_ids):
            self._call("versions", version_ids)
            return len(version_ids)

        def delete_transformers(self):
            self._call("transformers")

    recorded.client = Client
    monkeypatch.setattr(delete, "DatabaseClient", Client)
    return recorded


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pylovo-delete", *args])
    delete.main()


@pytest.mark.parametrize("args, expected", [
    (["--version", "1"], ("versions", ["1"])),
    (["--version", "1", "2", "3"], ("versions", ["1", "2", "3"])),
    (["version", "--version", "4", "5"], ("versions", ["4", "5"])),
    (["networks", "--plz", "80803", "--version", "1"], ("networks", 80803, "1")),
    (["transformers"], ("transformers",)),
])
def test_each_command_calls_its_client_method_with_the_parsed_arguments(monkeypatch, calls, args, expected):
    run(monkeypatch, *args)
    assert calls == ["open", expected, "close"]


@pytest.mark.parametrize("args", [["networks", "--version", "1"], ["networks", "--plz", "80803"],
                                  ["networks", "--plz", "abc", "--version", "1"], ["version"], ["unknown"]])
def test_incomplete_commands_are_refused_before_any_database_access(monkeypatch, calls, args):
    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, *args)
    assert refused.value.code == 2 and calls == []


def test_without_arguments_the_help_is_shown_and_nothing_is_deleted(monkeypatch, calls, capsys):
    run(monkeypatch)
    assert calls == [] and "usage: pylovo-delete" in capsys.readouterr().out


@pytest.mark.parametrize("error", [ValueError("Version(s) not found in database: 9"), RuntimeError("connection lost")])
def test_a_failing_delete_exits_with_one_and_still_closes_the_client(monkeypatch, calls, capsys, error):
    calls.client.error = error
    with pytest.raises(SystemExit) as failed:
        run(monkeypatch, "version", "--version", "9")
    assert failed.value.code == 1 and calls[-1] == "close"
    assert str(error) in capsys.readouterr().out


# --- the queries behind the commands -----------------------------------------------------------------------------------
class Cursor:
    def __init__(self, rows=(), rowcount=1):
        self.calls, self.rows, self.rowcount = [], list(rows), rowcount

    def execute(self, query, params=None):
        self.calls.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.rows.pop(0) if self.rows else []

    def fetchone(self):  # the MV schema probe of the version delete: the schema has MV tables
        return (True,)


def results_client(cursor):
    events = []
    cursor.calls = events

    class Client(ResultsMixin):
        def refresh_materialized_views(self):
            events.append(("refresh",))

    client = object.__new__(Client)
    client.cur, client.logger = cursor, logging.getLogger("results-delete-test")
    client.conn = type("Connection", (), {"commit": lambda self: events.append(("commit",)),
                                          "rollback": lambda self: events.append(("rollback",))})()
    return client, events


def table_of(call):
    return call[0].split()[2] if call[0].startswith("DELETE FROM") else call[0]


def test_deleting_versions_checks_that_all_exist_then_deletes_refreshes_and_commits():
    client, events = results_client(Cursor(rows=[[("1",), ("2",)]], rowcount=2))

    assert client.delete_versions_from_all_tables(["1", "2"]) == 2
    # versions, MV schema probe, busy MV variants (none), then the delete
    assert [event[0].split()[0] for event in events] == ["SELECT", "SELECT", "SELECT", "DELETE", "refresh", "commit"]
    assert "pg_try_advisory_xact_lock" in events[2][0]
    assert events[3] == ("DELETE FROM pylovo.version WHERE version_id = ANY(%(versions)s);", {"versions": ["1", "2"]})


def test_deleting_a_missing_version_deletes_nothing():
    client, events = results_client(Cursor(rows=[[("1",)]]))

    with pytest.raises(ValueError, match="not found in database: 2"):
        client.delete_versions_from_all_tables(["1", "2"])
    assert [event[0].split()[0] for event in events] == ["SELECT"]    # no DELETE, no refresh, no commit


def test_deleting_no_version_is_an_error_without_any_query():
    client, events = results_client(Cursor())
    with pytest.raises(ValueError, match="At least one version"):
        client.delete_versions_from_all_tables([])
    assert events == []


def test_deleting_a_plz_removes_its_mv_variants_first_and_commits_at_the_end():
    client, events = results_client(Cursor(rows=[[], [("mv_a",), ("mv_b",)]]))   # no busy variant, two deleted

    client.delete_plz_from_all_tables(80803, "1")
    assert "pg_try_advisory_xact_lock" in events[0][0]                  # busy MV variants are refused first
    assert [table_of(event) for event in events[1:-2]] == [
        "pylovo.mv_variant", "pylovo.ways_result", "pylovo.mv_customers", "pylovo.postcode_result"]
    assert "RETURNING mv_variant_id" in events[1][0]
    assert events[-2:] == [("refresh",), ("commit",)]
    assert all(event[1] == {"v": "1", "p": 80803} for event in events[:-2])
