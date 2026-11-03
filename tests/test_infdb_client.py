"""SQL of ``InfdbClient`` that the diagnostics use, checked on a fake connection (no database)."""
import inspect
import logging

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
        self.rows = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return (1.5, 0.5)

    def fetchall(self):
        return self.rows

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


def test_the_ways_of_a_plz_hold_the_connection_lines_of_its_buildings_filed_under_another_postcode(monkeypatch):
    infdb = client(monkeypatch)
    infdb.cur.rows = [(110, None, None, 12.0, 12.0, "line", 1)]
    assert infdb.fetch_ways_from_infdb(85465) == infdb.cur.rows
    (query, params), = infdb.cur.executed
    buildings = FakeIdentifier(infdb_client.INFDB_SOURCE_SCHEMA, "buildings").as_string(None)
    # the InfDB may file the line of a border building under the neighbouring postcode; it starts at the centroid
    # of a building that generation loads (same filter as fetch_buildings_from_infdb)
    assert f"FROM {buildings}" in query and "COALESCE(centroid, ST_Centroid(geom)) AS c" in query
    assert f"WHERE postcode = %(plz)s AND {infdb_client._consumer_buildings_sql()}" in query
    assert "ST_DWithin(ST_StartPoint(l.geom), bu.c, 1e-6)" in query
    # the lines filed under the PLZ come once, through their postcode; a line at two buildings counts once
    assert "WHERE l.postcode IS DISTINCT FROM %(plz)s" in query and "SELECT DISTINCT l.id" in query
    assert params == {"plz": 85465}


def test_the_building_postcodes_come_largest_first_without_buildings_lacking_a_postcode():
    infdb = InfdbClient.__new__(InfdbClient)
    infdb.conn, infdb.cur = FakeConnection(), FakeCursor()
    infdb.cur.rows = [(91126,), (80331,)]
    assert infdb.fetch_building_postcodes() == [91126, 80331]
    (query, params), = infdb.cur.executed
    text = "".join(part.string for part in query.seq if isinstance(part, infdb_client.sql.SQL))
    tables = [part.strings for part in query.seq if isinstance(part, infdb_client.sql.Identifier)]
    assert tables == [(infdb_client.INFDB_SOURCE_SCHEMA, "buildings")] and params is None
    assert "WHERE postcode IS NOT NULL" in text and "GROUP BY postcode" in text
    assert "ORDER BY count(*) DESC, postcode" in text and "gemeindeschluessel" not in text


def test_the_building_postcodes_of_federal_states_come_from_the_municipality_key_of_the_buildings():
    infdb = InfdbClient.__new__(InfdbClient)
    infdb.conn, infdb.cur = FakeConnection(), FakeCursor()
    infdb.fetch_building_postcodes(["05", "09"])
    (query, params), = infdb.cur.executed
    text = "".join(part.string for part in query.seq if isinstance(part, infdb_client.sql.SQL))
    assert "WHERE postcode IS NOT NULL AND left(gemeindeschluessel, 2) = ANY(%(states)s)" in text
    assert params == {"states": ["05", "09"]}


def test_the_consumer_buildings_need_an_address_only_with_the_address_filter(monkeypatch):
    monkeypatch.setattr(infdb_client, "EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", True)
    assert "COALESCE(house_number, '') <> ''" in infdb_client._consumer_buildings_sql()
    monkeypatch.setattr(infdb_client, "EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", False)
    assert infdb_client._consumer_buildings_sql() == (
        "building_use IN ('Commercial', 'Public', 'Residential', 'Mixed', 'Unknown') "
        "AND COALESCE(building_use_id, '') != '31001_2523'")


def test_the_auxiliary_class_is_recognised_by_its_column_in_the_source_schema(monkeypatch):
    infdb = client(monkeypatch)
    infdb.cur.fetchone = lambda: (True,)
    assert infdb.has_auxiliary_class() is True
    (query, params), = infdb.cur.executed
    assert "information_schema.columns" in query and "table_name = 'buildings'" in query
    assert params == {"schema": infdb_client.INFDB_SOURCE_SCHEMA, "column": "auxiliary_rule"}
    infdb.cur.fetchone = lambda: (False,)
    assert infdb.has_auxiliary_class() is False


def test_the_address_filter_warning_names_the_mismatch_with_the_infdb_data():
    # suitable: the filter off for InfDB data with the class Auxiliary, on for older data
    assert infdb_client.address_filter_warning(False, True) is None
    assert infdb_client.address_filter_warning(True, False) is None
    # off without the class: outbuildings become consumers
    assert "garages, sheds and barns" in infdb_client.address_filter_warning(False, False)
    # on with the class: every consumer without a house number is lost
    assert "Set EXCLUDE_BUILDINGS_WITHOUT_ADDRESS: False" in infdb_client.address_filter_warning(True, True)


def test_close_closes_the_cursor_and_the_connection_and_may_be_repeated(monkeypatch):
    infdb = client(monkeypatch)
    infdb.close()
    infdb.close()
    assert infdb.cur.closed and infdb.conn.closed


def test_close_is_safe_when_the_constructor_failed_before_the_connection_existed():
    InfdbClient.__new__(InfdbClient).close()


def test_the_farm_part_of_a_farmstead_carries_no_load():
    source = inspect.getsource(InfdbClient.fetch_buildings_from_infdb)
    # InfDB step 08_z moves the barns of a farmstead to the non-residential floor area; Unknown has no load
    farm = source.index("WHEN mix_rule = 'farmstead' THEN 'Unknown'")
    assert farm < source.index("WHEN building_use = 'Residential' THEN 'Commercial'")



def _selected_load_class(infdb):
    """The SQL part that ``fetch_buildings_from_infdb`` selects as ``load_class``."""
    infdb.fetch_buildings_from_infdb(80331)
    query, params = infdb.cur.executed[-1]
    assert params == {"p": 80331}
    parts = list(query.seq)
    after = next(i for i, p in enumerate(parts) if isinstance(p, infdb_client.sql.SQL)
                 and p.string.lstrip().startswith("AS load_class"))
    return parts[after - 1]


def test_the_load_class_is_read_only_with_the_class_model_and_its_column(monkeypatch, caplog):
    def fresh():
        infdb = InfdbClient.__new__(InfdbClient)
        infdb.conn, infdb.cur, infdb.logger = FakeConnection(), FakeCursor(), logging.getLogger("load-class-read")
        return infdb

    infdb = fresh()
    # the model of the versions before the classes: NULL, without asking for the column
    monkeypatch.setattr(infdb_client, "NONRESIDENTIAL_LOAD_MODEL", "building_use")
    assert not infdb.reads_load_class() and infdb.cur.executed == []
    assert _selected_load_class(infdb) == infdb_client.sql.SQL("NULL::text")
    # the class model with the column: the column
    monkeypatch.setattr(infdb_client, "NONRESIDENTIAL_LOAD_MODEL", "load_class")
    infdb = fresh()
    infdb.cur.fetchone = lambda: (True,)
    assert infdb.reads_load_class()
    assert infdb.cur.executed[-1][1] == {"schema": infdb_client.INFDB_SOURCE_SCHEMA, "column": "load_class"}
    assert _selected_load_class(infdb) == infdb_client.sql.Identifier("load_class")
    # an older InfDB without the column: NULL and one warning
    infdb = fresh()
    infdb.cur.fetchone = lambda: (False,)
    with caplog.at_level(logging.WARNING, logger="load-class-read"):
        assert not infdb.reads_load_class() and not infdb.reads_load_class()
    assert len(infdb.cur.executed) == 1 and "has no column load_class" in caplog.text
    assert _selected_load_class(infdb) == infdb_client.sql.SQL("NULL::text")
