"""Opt-in PostGIS fixtures for the neighboring-street and exclusion SQL; run only on a disposable database.

    PYLOVO_FIXTURE_DB="host=127.0.0.1 port=55440 dbname=pylovo_fixture user=sandbox password=sandbox" \\
        pytest tests/test_postgis_fixtures.py

The database needs PostGIS and pgRouting. The tests create the pylovo schema and SQL functions there and
work in session temp tables of a made-up PLZ; nothing is committed except the schema. Coordinates are metres.
"""
from __future__ import annotations

import os

import pytest

from pylovo import postcode_border

DSN = os.getenv("PYLOVO_FIXTURE_DB")
pytestmark = pytest.mark.skipif(not DSN, reason="set PYLOVO_FIXTURE_DB to a disposable PostGIS/pgRouting database")

PLZ = 99001


@pytest.fixture(scope="module")
def client():
    from psycopg2.extensions import parse_dsn

    from pylovo.config_loader import TARGET_EPSG
    from pylovo.database.database_client import DatabaseClient
    from pylovo.database.database_constructor import DatabaseConstructor

    assert TARGET_EPSG == 25832
    params = parse_dsn(DSN)
    db = DatabaseClient(dbname=params["dbname"], user=params.get("user"), pw=params.get("password", ""),
                        host=params.get("host"), port=int(params.get("port", 5432)))
    constructor = DatabaseConstructor(db)
    constructor.migrate_schema()
    constructor.load_ways_preprocessing_functions()
    yield db
    db.close()


@pytest.fixture()
def tem(client):
    client.create_temp_tables(PLZ)
    yield client
    client.rollback_changes()
    client.drop_temp_tables(PLZ)
    client.commit_changes()


def way(db, way_id, wkt, clazz=41):
    db.cur.execute("INSERT INTO ways_tem (clazz, geom, way_id) VALUES (%s, ST_GeomFromText(%s, 25832), %s)",
                   (clazz, wkt, way_id))


def building(db, objectid, x, y, half=5.0, peak=10.0):
    db.cur.execute(
        """INSERT INTO buildings_tem (objectid, geom, centroid, type, peak_load_in_kw, plz)
           VALUES (%s, ST_MakeEnvelope(%s, %s, %s, %s, 25832), ST_SetSRID(ST_MakePoint(%s, %s), 25832), 'SFH', %s, %s)""",
        (objectid, x - half, y - half, x + half, y + half, x, y, peak, PLZ))


def test_saving_keeps_own_ways_and_records_every_left_out_building(tem, monkeypatch):
    from pylovo.config_loader import VERSION_ID

    # inside the test transaction: insert_version_if_not_exists would commit a snapshot that a later
    # configuration contradicts
    tem.cur.execute("""INSERT INTO pylovo.version (version_id, version_comment)
                       VALUES (%s, 'fixture') ON CONFLICT DO NOTHING""", (VERSION_ID,))
    tem.cur.execute("""INSERT INTO pylovo.postcode (plz, geom)
                       VALUES (%s, ST_Multi(ST_MakeEnvelope(0, 0, 100, 100, 25832))) ON CONFLICT (plz) DO NOTHING""",
                    (PLZ,))
    tem.copy_postcode_result_table(PLZ)
    way(tem, 1, "LINESTRING(10 10, 90 10)")              # own
    way(tem, 2, "LINESTRING(150 10, 190 10)")            # neighboring street, split part without any marker column
    tem.cur.execute("UPDATE ways_tem SET source = way_id, target = way_id + 100")
    building(tem, "isolated", 50, 50)
    building(tem, "mv_dropped", 60, 60, peak=0.0)      # MV-direct building in the same dropped component
    tem.cur.execute("UPDATE buildings_tem SET vertice_id = 7 WHERE objectid IN ('isolated', 'mv_dropped')")
    tem.delete_transformers_from_buildings_tem([7])
    building(tem, "mv", 20, 20, peak=0.0)
    monkeypatch.setattr(postcode_border, "enabled", lambda: True)
    tem.save_tables(PLZ)
    tem.cur.execute("SELECT way_id FROM pylovo.ways_result WHERE version_id = %s AND plz = %s", (VERSION_ID, PLZ))
    assert [row[0] for row in tem.cur.fetchall()] == [1]
    tem.cur.execute("""SELECT objectid, reason FROM pylovo.building_exclusion
                       WHERE version_id = %s AND plz = %s ORDER BY objectid""", (VERSION_ID, PLZ))
    assert tem.cur.fetchall() == [("isolated", "isolated_component"), ("mv", "mv_direct"), ("mv_dropped", "mv_direct")]


def transformer(db, osm_id, x, y):
    db.cur.execute(
        """INSERT INTO pylovo.transformers (osm_id, area, type, transformer_rated_power, geom_type, within_shopping,
                                             osm, lod2, lod2_objectid, geom)
           VALUES (%s, 0, 'Transformer', NULL, 'point', false, true, false, NULL,
                   ST_Multi(ST_SetSRID(ST_MakePoint(%s, %s), 25832)))""", (osm_id, x, y))


def test_a_building_with_a_transformer_inside_is_recorded_as_mv_direct_if_it_is_an_mv_customer(tem):
    transformer(tem, "node/990001", 20, 20)
    transformer(tem, "node/990002", 60, 60)
    building(tem, "hall", 20, 20)
    building(tem, "shop", 60, 60)
    tem.cur.execute("""UPDATE buildings_tem SET type = 'Commercial', residential_floor_area = 0,
                                               nonresidential_mv_direct = (objectid = 'hall')""")
    assert tem.remove_non_residential_buildings_overlapping_transformers() == 2
    tem.cur.execute("SELECT objectid, reason FROM excluded_tem ORDER BY objectid")
    assert tem.cur.fetchall() == [("hall", "mv_direct"), ("shop", "overlaps_transformer")]


def test_a_transformer_on_a_street_becomes_a_vertex_of_that_street(tem):
    way(tem, 1, "LINESTRING(0 0, 100 0)")
    way(tem, 2, "LINESTRING(0 30, 100 30)")
    for objectid, x, y in (("node/1", 50, 0.05), ("node/2", 20, 10)):     # on street 1 / 10 m beside it
        tem.cur.execute(
            """INSERT INTO buildings_tem (objectid, geom, centroid, type, peak_load_in_kw, plz)
               VALUES (%s, ST_SetSRID(ST_MakePoint(%s, %s), 25832), ST_SetSRID(ST_MakePoint(%s, %s), 25832),
                       'Transformer', -1, %s)""", (objectid, x, y, x, y, PLZ))
    tem.cur.execute("SELECT generate_transformer_to_way_connections_infdb()")
    tem.cur.execute("""SELECT clazz, ST_AsText(ST_SnapToGrid(geom, 0.001)) FROM ways_tem
                       ORDER BY clazz, ST_XMin(geom), ST_YMin(geom)""")
    assert tem.cur.fetchall() == [
        (41, "LINESTRING(0 30,100 30)"),
        (103, "LINESTRING(0 0,20 0)"), (103, "LINESTRING(20 0,50 0)"), (103, "LINESTRING(50 0,100 0)"),
        (110, "LINESTRING(20 10,20 0)"),                                  # only the station beside the street
    ]
    tem.cur.execute("SELECT objectid, ST_AsText(ST_SnapToGrid(centroid, 0.001)) FROM buildings_tem ORDER BY objectid")
    assert tem.cur.fetchall() == [("node/1", "POINT(50 0)"), ("node/2", "POINT(20 10)")]
