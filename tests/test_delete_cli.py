"""pylovo-delete classification: integer id, refreshed GIS view, unknown ids reported (no database)."""
import logging
import sys

import pytest

from pylovo.cli import delete
from pylovo.database.results_mixin import ResultsMixin


def _client(rowcount: int):
    calls = []

    class Cursor:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))
            self.rowcount = rowcount

    class Connection:
        def commit(self):
            calls.append(("commit", None))

        def rollback(self):
            calls.append(("rollback", None))

    class Client(ResultsMixin):
        def refresh_materialized_views(self):
            calls.append(("refresh", None))

    client = object.__new__(Client)
    client.cur, client.conn, client.logger = Cursor(), Connection(), logging.getLogger("delete-cli-test")
    return client, calls


def test_deleting_a_classification_refreshes_the_materialized_view():
    """transformer_classified_with_grid (QGIS layer) kept the deleted rows until another command refreshed it."""
    client, calls = _client(rowcount=1)
    client.delete_classification_version_from_related_tables(3)
    assert calls == [("DELETE FROM pylovo.classification_version WHERE classification_id = %(cid)s;", {"cid": 3}),
                     ("refresh", None), ("commit", None)]


def test_deleting_an_unknown_classification_is_an_error():
    client, calls = _client(rowcount=0)
    with pytest.raises(ValueError, match="not found in database: 7"):
        client.delete_classification_version_from_related_tables(7)
    assert ("commit", None) not in calls and ("refresh", None) not in calls


def test_classification_id_is_an_integer(monkeypatch, capsys):
    deleted = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def delete_classification_version_from_related_tables(self, classification_id):
            deleted.append(classification_id)

    monkeypatch.setattr(delete, "DatabaseClient", Client)
    monkeypatch.setattr(sys, "argv", ["pylovo-delete", "classification", "--version", "v1.0"])
    with pytest.raises(SystemExit) as invalid:
        delete.main()
    assert invalid.value.code == 2 and deleted == []
    monkeypatch.setattr(sys, "argv", ["pylovo-delete", "classification", "--version", "1", "--yes"])
    delete.main()
    assert deleted == [1] and "Deleted classification version 1" in capsys.readouterr().out
