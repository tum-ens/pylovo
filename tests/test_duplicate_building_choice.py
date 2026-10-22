"""Of several buildings with one objectid the one with the lowest id is kept, not the first by physical row order (no database)."""

from psycopg2 import sql

from pylovo.database.preprocessing_mixin import PreprocessingMixin
from pylovo.database.results_mixin import ResultsMixin


def render(query) -> str:
    """The text of a plain or composed statement, without a connection (identifiers quoted by hand)."""
    if isinstance(query, str):
        return " ".join(query.split())
    if isinstance(query, sql.Composed):
        return " ".join("".join(render(part) for part in query.seq).split())
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return ".".join(f'"{part}"' for part in query.strings)
    raise TypeError(type(query))


class Cursor:
    rowcount = 0

    def __init__(self):
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append(render(query))


def test_save_tables_deletes_the_duplicates_of_a_building_except_the_lowest_id():
    client = ResultsMixin.__new__(ResultsMixin)
    client.cur = Cursor()
    client.save_tables(91301)
    dedupe = client.cur.statements[0]
    assert dedupe.startswith("DELETE FROM")
    assert "(array_agg(ctid ORDER BY id, ctid))[1] as ctid" in dedupe
    assert "MIN(ctid)" not in dedupe      # the lowest physical position follows the update history, not a key


def test_mv_customers_keep_the_lowest_id_of_a_duplicate_building():
    client = PreprocessingMixin.__new__(PreprocessingMixin)
    client.cur = Cursor()
    client.save_mv_customers(91301)
    insert = client.cur.statements[1]
    assert "SELECT DISTINCT ON (objectid)" in insert
    assert "ORDER BY objectid, id ON CONFLICT" in insert
