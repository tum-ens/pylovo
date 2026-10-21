"""SQL of ``InfdbClient`` that the diagnostics use, checked on a fake connection (no database)."""
import pylovo.infdb.infdb_client as infdb_client
from pylovo.infdb.infdb_client import InfdbClient


class FakeConnection:
    closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = True


class FakeCursor:
    closed = False

    def __init__(self):
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return (1.5, 0.5)

    def close(self):
        self.closed = True


class FakeIdentifier:
    def __init__(self, *names):
        self.names = names

    def as_string(self, context):
        return ".".join(f'"{name}"' for name in self.names)


def client(monkeypatch):
    monkeypatch.setattr(infdb_client.sql, "Identifier", FakeIdentifier)
    infdb = InfdbClient.__new__(InfdbClient)
    infdb.conn, infdb.cur = FakeConnection(), FakeCursor()
    infdb.source_srid = lambda table, schema=None: 25832
    return infdb


def test_the_ring_street_length_counts_only_the_part_of_a_street_in_the_ring(monkeypatch):
    infdb = client(monkeypatch)
    assert infdb.fetch_street_length_km(80331, b"polygon", 25832, 500.0) == (1.5, 0.5)
    (query, params), = infdb.cur.executed
    # a street of another PLZ that crosses the border is cut at the polygon and at the ring width, not counted whole
    assert "ST_Difference(ST_Intersection(w.geom, a.ring_area), a.g)" in query
    assert "ST_Buffer(g, %(ring)s) AS ring_area" in query
    assert "FILTER (WHERE w.postcode IS DISTINCT FROM %(plz)s)" in query
    assert "ST_Length(w.geom)" not in query
    assert params["plz"] == 80331 and params["ring"] == 500.0


def test_close_closes_the_cursor_and_the_connection_and_may_be_repeated(monkeypatch):
    infdb = client(monkeypatch)
    infdb.close()
    infdb.close()
    assert infdb.cur.closed and infdb.conn.closed


def test_close_is_safe_when_the_constructor_failed_before_the_connection_existed():
    InfdbClient.__new__(InfdbClient).close()
