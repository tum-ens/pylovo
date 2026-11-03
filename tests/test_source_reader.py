"""``SourceReader``: the InfDB source data of the MV stage, local or through a separate connection (no database)."""
from __future__ import annotations

import io

import pytest
from psycopg2 import sql

from pylovo.infdb.source_reader import SourceReader, regclass_text


def _render(query) -> str:
    if isinstance(query, str):
        return query
    if isinstance(query, sql.Composed):
        return "".join(_render(part) for part in query.seq)
    if isinstance(query, sql.SQL):
        return query.string
    if isinstance(query, sql.Identifier):
        return ".".join('"' + s + '"' for s in query.strings)
    raise TypeError(type(query))


class Cursor:
    """Records the statements; ``answers`` maps a text fragment to the rows of a matching query."""

    def __init__(self, answers=None, copy_out=b""):
        self.answers = answers or {}
        self.copy_out = copy_out
        self.statements: list[tuple[str, object]] = []
        self.copied_in = b""
        self.rows: list = []
        self.connection = None

    def execute(self, query, params=None):
        text = _render(query)
        self.statements.append((text, params))
        self.rows = next((rows for fragment, rows in self.answers.items() if fragment in text), [])

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def mogrify(self, query, params=None):
        return f"{_render(query)} -- {params}".encode()

    def copy_expert(self, query, file):
        query = _render(query)
        self.statements.append((query, None))
        if "TO STDOUT" in query:
            file.write(self.copy_out)
        else:
            self.copied_in = file.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Connection:
    """A read-only connection: every ``with conn:`` block is one transaction."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = 0
        self.transactions = 0

    def cursor(self):
        return self._cursor

    def __enter__(self):
        self.transactions += 1
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = 1


def test_regclass_text_quotes_both_parts():
    assert regclass_text("osm", 'odd"name') == '"osm"."odd""name"'


def test_the_local_reader_runs_on_the_pylovo_cursor_in_its_transaction():
    local = Cursor({"SELECT ST_SRID": [(25832,)], "to_regclass": [(True,)], "count(*)": [(7,)]})
    reader = SourceReader(lambda: local)
    assert not reader.remote and reader.where == "the pylovo database"
    assert reader.srid("basedata", "buildings") == 25832
    assert reader.srid("basedata", "buildings") == 25832                 # cached: one query
    assert sum("ST_SRID" in t for t, _ in local.statements) == 1
    assert reader.table_exists("osm", "tags") and not reader.table_exists(None, "tags")
    query = sql.SQL("SELECT w.ags, w.geom FROM {} w WHERE w.ags = ANY(%(ags)s)").format(
        sql.Identifier("basedata", "ways_per_junction"))
    columns = []
    assert reader.stage("mvg_x", query, {"ags": ["1"]}, lambda: columns.append(1) or [("ags", "text")]) == 7
    texts = [t for t, _ in local.statements]
    assert 'DROP TABLE IF EXISTS pg_temp."mvg_x"' in texts
    assert ('CREATE TEMP TABLE "mvg_x" ON COMMIT DROP AS SELECT w.ags, w.geom FROM "basedata"."ways_per_junction" w '
            "WHERE w.ags = ANY(%(ags)s)") in texts
    assert columns == []                                                  # the column types are not needed locally
    reader.close()                                                        # nothing to close


def test_the_remote_reader_stages_rows_through_copy_in_one_read_only_transaction_each():
    remote_cur = Cursor({"SELECT ST_SRID": [(4326,)], "pg_attribute": [("geom", "geometry"), ("ags", "text")]},
                        copy_out=b"09\t0101000020E6100000\n")
    opened = []

    def connect():
        opened.append(Connection(remote_cur))
        return opened[-1]

    local = Cursor({"count(*)": [(1,)]})
    reader = SourceReader(lambda: local, connect, "infdb@central:5432")
    assert reader.remote and reader.where == "infdb@central:5432" and opened == []      # opened on first use
    assert reader.srid("osm", "landuse_polygon") == 4326 and len(opened) == 1
    assert reader.column_types("basedata", "ways", ("ags", "geom")) == [("ags", "text"), ("geom", "geometry")]
    with pytest.raises(LookupError, match="has no column klasse"):
        reader.column_types("basedata", "ways", ("klasse",))
    query = sql.SQL("SELECT w.ags, w.geom FROM {} w").format(sql.Identifier("basedata", "ways"))
    assert reader.stage("mvg_y", query, {"ags": ["09"]},
                        lambda: reader.column_types("basedata", "ways", ("ags", "geom"))) == 1
    local_texts = [t for t, _ in local.statements]
    assert 'CREATE TEMP TABLE "mvg_y" ("ags" text, "geom" geometry) ON COMMIT DROP' in local_texts
    assert local.copied_in == b"09\t0101000020E6100000\n"               # the rows reach the temporary table as copied
    copy_out = next(t for t, _ in remote_cur.statements if t.startswith("COPY ("))
    assert copy_out.endswith(") TO STDOUT") and "\"basedata\".\"ways\"" in copy_out
    assert not any("CREATE" in t or "SET " in t for t, _ in remote_cur.statements)  # the remote side only reads
    assert opened[0].transactions == 4                                    # srid, two column lookups, copy
    reader.close()
    assert opened[0].closed and reader.srid("osm", "landuse_polygon") == 4326          # cached after close
    reader.table_exists("osm", "tags")
    assert len(opened) == 2                                               # reopened on the next query


def test_stage_takes_the_columns_as_a_list_too():
    remote_cur = Cursor(copy_out=b"")
    local = Cursor({"count(*)": [(0,)]})
    reader = SourceReader(lambda: local, lambda: Connection(remote_cur))
    reader.stage("mvg_z", sql.SQL("SELECT 1"), None, [("pt", "geometry")])
    assert 'CREATE TEMP TABLE "mvg_z" ("pt" geometry) ON COMMIT DROP' in [t for t, _ in local.statements]


def test_copy_buffer_is_a_file():
    # the remote copy writes into a temporary file, not into memory
    remote_cur = Cursor(copy_out=b"x\n")
    seen = []
    original = remote_cur.copy_expert

    def copy_expert(query, file):
        seen.append(type(file))
        original(query, file)

    remote_cur.copy_expert = copy_expert
    local = Cursor({"count(*)": [(1,)]})
    SourceReader(lambda: local, lambda: Connection(remote_cur)).stage("t", sql.SQL("SELECT 1"), None, [("a", "int")])
    assert seen and not issubclass(seen[0], io.BytesIO)
