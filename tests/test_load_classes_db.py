"""Opt-in PostGIS test of the load-class and connection-level SQL; runs only on a disposable database.

    PYLOVO_FIXTURE_DB="host=127.0.0.1 port=55440 dbname=pylovo_fixture user=sandbox password=sandbox" \\
        pytest tests/test_load_classes_db.py

Like ``tests/test_postgis_fixtures.py`` it creates the pylovo schema and works in session temp tables of a made-up
PLZ; nothing is committed except the schema and the consumer categories of the configuration.
"""
from __future__ import annotations

import os

import pytest

from pylovo import config_loader, load_classes
from pylovo.database import preprocessing_mixin

DSN = os.getenv("PYLOVO_FIXTURE_DB")
pytestmark = pytest.mark.skipif(not DSN, reason="set PYLOVO_FIXTURE_DB to a disposable PostGIS/pgRouting database")

PLZ = 99002
THRESHOLDS = {"station_busbar": 100.0, "mv": 200.0, "hv": 20000.0}
SIM = config_loader.SIM_FACTOR
# objectid: (use, nonresidential use, load class, floor area, storeys, residential area, households, type)
BUILDINGS = {
    "office": ("Commercial", "Commercial", "office", 2500.0, 4, 0.0, None, "Commercial"),
    "school_in_commercial": ("Commercial", "Commercial", "education", 1500.0, 2, 0.0, None, "Commercial"),
    "barn_unknown": ("Unknown", "Unknown", "agriculture", 400.0, 1, 0.0, None, "Unknown"),
    "unconfigured": ("Commercial", "Commercial", "spaceport", 500.0, 2, 0.0, None, "Commercial"),
    "no_class": ("Public", "Public", None, 1000.0, 2, 0.0, None, "Public"),
    "shop_in_block": ("Mixed", "Commercial", "retail", 500.0, 6, 2500.0, 50, "MFH"),
    "plant": ("Commercial", "Commercial", "production", 200000.0, 4, 0.0, None, "Commercial"),
}


@pytest.fixture(scope="module")
def client():
    from psycopg2.extensions import parse_dsn

    from pylovo.database.database_client import DatabaseClient
    from pylovo.database.database_constructor import DatabaseConstructor

    params = parse_dsn(DSN)
    db = DatabaseClient(dbname=params["dbname"], user=params.get("user"), pw=params.get("password", ""),
                        host=params.get("host"), port=int(params.get("port", 5432)))
    DatabaseConstructor(db).migrate_schema()
    db.insert_consumer_categories_from_config(consumer_categories=config_loader.CONSUMER_CATEGORIES)
    yield db
    db.close()


@pytest.fixture()
def tem(client):
    client.create_temp_tables(PLZ)
    for objectid, (use, nonres_use, load_class, floor_area, storeys, res_area, households, kind) in BUILDINGS.items():
        client.cur.execute(
            """INSERT INTO buildings_tem (objectid, building_use, nonresidential_use, load_class, floor_area,
                                          floor_number, residential_floor_area, nonresidential_floor_area,
                                          households, type, geom, centroid, plz)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, ST_MakeEnvelope(0, 0, 10, 10, 25832),
                       ST_SetSRID(ST_MakePoint(5, 5), 25832), %s)""",
            (objectid, use, nonres_use, load_class, floor_area, storeys, res_area,
             floor_area * storeys - res_area, households, kind, PLZ))
    yield client
    client.rollback_changes()
    client.drop_temp_tables(PLZ)
    client.commit_changes()


def rows(db) -> dict:
    db.cur.execute("""SELECT objectid, nonresidential_use, nonresidential_peak_load_in_kw, peak_load_in_kw,
                             nonresidential_mv_direct, connection_level FROM buildings_tem""")
    return {r[0]: r[1:] for r in db.cur.fetchall()}


def test_the_classes_set_the_load_and_the_levels_follow_the_own_peak(tem, monkeypatch):
    table = load_classes.load_class_table(config_loader.CONFIG_GENERATION["LOAD_CLASSES"], SIM)
    by_class = table.set_index("load_class")
    monkeypatch.setattr(preprocessing_mixin, "NONRESIDENTIAL_LOAD_MODEL", "load_class")
    monkeypatch.setattr(preprocessing_mixin, "LOAD_CLASS_TABLE", table)
    monkeypatch.setattr(preprocessing_mixin, "CONNECTION_LEVEL_THRESHOLDS_KW", THRESHOLDS)
    assert tem.set_building_peak_load() == 1                         # the Unknown barn keeps no load
    tem.update_too_large_consumers_to_zero()
    levels = tem.set_connection_levels()
    result = rows(tem)

    def expected(name, area, category):
        return load_classes.class_load_kw(by_class.loc[name].to_dict() | {"load_class": name}, area, category, SIM)

    use, nonres_kw, _, mv_direct, level = result["office"]
    assert (use, mv_direct, level) == ("Commercial", True, "mv")
    assert nonres_kw == pytest.approx(expected("office", 10000.0, "Commercial")) == pytest.approx(285.5, abs=0.1)
    # a school in a Commercial building counts as Public
    use, nonres_kw, peak, _, level = result["school_in_commercial"]
    assert use == "Public" and nonres_kw == pytest.approx(expected("education", 3000.0, "Public")) == peak
    assert level == "lv_feeder"
    assert "barn_unknown" not in result
    # a class that LOAD_CLASSES does not list and no class: the W/m² of the use
    assert result["unconfigured"][1] == pytest.approx(1000.0 * 79.0 / 1000.0)
    assert result["no_class"][1] == pytest.approx(2000.0 * 29.0 / 1000.0)
    # the shop of a block with 50 households: the large retail value (425 m² net); households with pylovo's law
    use, nonres_kw, peak, mv_direct, level = result["shop_in_block"]
    assert nonres_kw == pytest.approx(expected("retail", 500.0, "Commercial"))
    assert nonres_kw == pytest.approx(500.0 * 60 * 0.85 / 4370 / 0.5)
    households = 50 * config_loader.PEAK_LOAD_HOUSEHOLD * (SIM["Residential"] + (1 - SIM["Residential"]) * 50 ** -0.75)
    assert households + nonres_kw > 100 and level == "station_busbar" and not mv_direct
    assert peak == pytest.approx(50 * config_loader.PEAK_LOAD_HOUSEHOLD + nonres_kw)
    # 800,000 m² of production: an HV customer
    assert result["plant"][4] == "hv" and result["plant"][1] >= 20000
    assert levels == {"hv": 1, "mv": 1, "lv_feeder": 3, "station_busbar": 1}


def test_a_part_without_a_class_takes_the_unclassified_class(tem, monkeypatch):
    table = load_classes.load_class_table(config_loader.CONFIG_GENERATION["LOAD_CLASSES"], SIM)
    monkeypatch.setattr(preprocessing_mixin, "NONRESIDENTIAL_LOAD_MODEL", "load_class")
    monkeypatch.setattr(preprocessing_mixin, "LOAD_CLASS_TABLE", table)
    monkeypatch.setattr(preprocessing_mixin, "UNCLASSIFIED_LOAD_CLASS", "other")
    tem.set_building_peak_load()
    result = rows(tem)
    other = table.set_index("load_class").loc["other"].to_dict() | {"load_class": "other"}
    assert result["no_class"][:2] == ("Public", pytest.approx(load_classes.class_load_kw(other, 2000.0, "Public", SIM)))
    assert result["unconfigured"][1] == pytest.approx(1000.0 * 79.0 / 1000.0)    # a class it does not list: the use
    tem.cur.execute("SELECT load_class FROM buildings_tem WHERE objectid = 'no_class'")
    assert tem.cur.fetchone() == (None,)


def test_the_model_building_use_ignores_the_classes(tem, monkeypatch):
    monkeypatch.setattr(preprocessing_mixin, "NONRESIDENTIAL_LOAD_MODEL", "building_use")
    tem.set_building_peak_load()
    tem.update_too_large_consumers_to_zero()
    assert tem.set_connection_levels() == {}
    result = rows(tem)
    assert result["office"][:2] == ("Commercial", pytest.approx(10000.0 * 79.0 / 1000.0))
    assert result["school_in_commercial"][:2] == ("Commercial", pytest.approx(3000.0 * 79.0 / 1000.0))
    assert result["shop_in_block"][1] == pytest.approx(500.0 * 79.0 / 1000.0)
    assert {r[4] for r in result.values()} == {None}
