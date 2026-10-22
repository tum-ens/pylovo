"""Edges that collapse in the 1e-6 m snap are removed before the pgRouting topology is built (no database)."""

from psycopg2 import sql

from pylovo.database.preprocessing_mixin import PreprocessingMixin


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
    if isinstance(query, sql.Literal):
        return repr(query.wrapped)
    raise TypeError(type(query))


class Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, query, params=None):
        self.statements.append(render(query))


def test_collapsed_edges_are_deleted_between_the_snap_and_the_vertex_extraction(monkeypatch):
    # the method renders the edge query itself, which needs a connection for the identifier quoting
    monkeypatch.setattr(sql.Identifier, "as_string", lambda self, context: ".".join(f'"{p}"' for p in self.strings))
    client = PreprocessingMixin.__new__(PreprocessingMixin)
    client.cur = Cursor()
    client.build_pgr_network_topology(91301)

    statements = client.cur.statements
    cleanup = [i for i, text in enumerate(statements) if text.startswith("DELETE FROM")]
    assert len(cleanup) == 1, statements      # NULL and EMPTY alike: source and target would stay NULL
    cleanup = cleanup[0]
    assert statements[cleanup] == 'DELETE FROM "pg_temp"."ways_tem_91301" WHERE geom IS NULL OR ST_IsEmpty(geom);'
    snap = next(i for i, text in enumerate(statements) if "ST_SnapToGrid" in text)
    extract = next(i for i, text in enumerate(statements) if "pgr_extractVertices" in text)
    link = next(i for i, text in enumerate(statements) if "SET source = v.id" in text)
    assert snap < cleanup < extract < link
