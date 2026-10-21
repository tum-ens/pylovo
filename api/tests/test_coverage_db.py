"""Region gate against the test database (opt-in, see conftest.py).

The equivalence tests run the library's own code (``InfdbClient`` fetches and the
``PreprocessingMixin`` residential filter inside a rolled-back transaction) and compare the row
counts with the counters of :mod:`pylovo_api.coverage`, which replicates those filters in SQL. The
postcodes come from the ``region`` fixture (99001 and 99003 in the toy town).
"""
from __future__ import annotations

import pytest
from conftest import q, requires_db
from pylovo_api import db
from pylovo_api.coverage import InputCoverage, building_counts

pytestmark = requires_db
ADDRESS_ONLY = {"exclude_buildings_without_address": True, "residential_only": False}


@pytest.fixture(scope="module")
def plz(region) -> int:
    return region["plz"]


@pytest.fixture(scope="module")
def no_geometry(region) -> int:
    if region["no_geometry"] is None:
        pytest.skip("no registered postcode without a polygon next to the test postcode")
    return region["no_geometry"]


@pytest.fixture(scope="module")
def library(plz):
    """What the library reads for ``plz``: importable buildings (address required), ways, station buildings."""
    import pylovo.infdb.infdb_client as ic

    client = ic.InfdbClient()
    saved = ic.EXCLUDE_BUILDINGS_WITHOUT_ADDRESS
    ic.EXCLUDE_BUILDINGS_WITHOUT_ADDRESS = True
    try:
        result = {"buildings": client.fetch_buildings_from_infdb(plz), "ways": client.fetch_ways_from_infdb(plz),
                  "stations": client.fetch_transformer_station_buildings_from_infdb(plz)}
    finally:
        ic.EXCLUDE_BUILDINGS_WITHOUT_ADDRESS = saved
        client.close()
    return result


@pytest.fixture(scope="module")
def engine(plz):
    cov = InputCoverage()
    cov.start()
    cov.ensure([plz], wait_s=15)
    yield cov
    cov.shutdown()


def counters(cov: InputCoverage, table: str, plz: int) -> list[int]:
    rows = cov.tables[table].plz_rows.get(plz, {})
    total = None
    for c in rows.values():
        total = c if total is None else [a + b for a, b in zip(total, c)]
    return total


def source_buildings(conn, plz: int) -> int:
    from psycopg2 import sql
    from pylovo import config_loader as cl

    query = sql.SQL("SELECT count(*) FROM {} WHERE postcode = %s").format(
        sql.Identifier(cl.INFDB_SOURCE_SCHEMA, "buildings"))
    return q(conn, query, (plz,))[0][0]


def test_region_counts_equal_the_library(engine, conn, plz, library):
    st = engine.status_of(plz, ADDRESS_ONLY)
    assert st["status"] == "ready" and st["exact"]
    lines = sum(1 for w in library["ways"] if w[0] == 110)     # klasse 'connection_line'
    counts = st["counts"]
    assert counts == {"buildings_total": source_buildings(conn, plz), "buildings_importable": len(library["buildings"]),
                      "residential": counts["residential"], "station_buildings": len(library["stations"]),
                      "invalid_area_rows": 0,                  # the PLZ was generated: no row aborted it
                      "street_segments": len(library["ways"]) - lines, "connection_lines": lines}
    assert 0 < counts["residential"] <= counts["buildings_importable"]
    assert {t.strategy for t in engine.tables.values()} == {"full"}   # small tables: one GROUP BY each


@pytest.mark.parametrize("require_address", [False, True])
def test_building_filter_matches_the_library(engine, monkeypatch, require_address, plz):
    import pylovo.infdb.infdb_client as ic
    from pylovo.database.database_client import DatabaseClient

    monkeypatch.setattr(ic, "EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", require_address)
    flags = {"exclude_buildings_without_address": require_address, "residential_only": False}
    counts = building_counts(counters(engine, "buildings", plz), flags)
    rows = ic.InfdbClient().fetch_buildings_from_infdb(plz)
    assert len(rows) == counts["importable"]
    # RESIDENTIAL_ONLY_GENERATION: the library's own delete on its working table, rolled back
    client = DatabaseClient()
    try:
        client.create_temp_tables(plz)
        client.set_buildings_table(rows)
        removed = client.remove_non_residential_buildings_from_buildings_tem()
        assert len(rows) - removed == counts["residential"]
        ro = building_counts(counters(engine, "buildings", plz), flags | {"residential_only": True})
        assert ro["importable"] == len(rows) - removed
    finally:
        client.conn.rollback()
        client.close()


def test_way_filter_matches_the_library(engine, plz, library):
    ways = library["ways"]
    lines = sum(1 for w in ways if w[0] == 110)                     # klasse 'connection_line'
    assert len(ways) - lines == counters(engine, "ways", plz)[0]
    assert lines == counters(engine, "lines", plz)[0]


def test_per_municipality_strategy_equals_the_single_statement(engine, plz):
    """The real-InfDB path (key listing + per-AGS chunks) gives the same counters."""
    has_key = db.fetch_one("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = 'basedata' "
                           "AND table_name = 'buildings' AND column_name = 'gemeindeschluessel'")["n"]
    if not has_key:
        pytest.skip("basedata.buildings has no gemeindeschluessel column")
    cov = InputCoverage(small_rows=0, conn_options="-c enable_seqscan=off")
    cov.start()
    try:
        cov.ensure([plz], wait_s=15)
        assert cov.tables["buildings"].strategy == "ags" or cov.tables["buildings"].reason
        if cov.tables["buildings"].strategy == "ags":
            assert counters(cov, "buildings", plz) == counters(engine, "buildings", plz)
    finally:
        cov.shutdown()


def test_coverage_api(client, conn, plz):
    info = client.get("/api/regions/coverage")
    assert info.status_code == 200                                   # was swallowed by /{plz} (422)
    body = info.json()
    assert body["mode"] == "infdb" and body["suggested_sql"] == []
    assert set(body["tables"]) == {"buildings", "ways", "lines"}
    res = client.get("/api/regions/input", params={"plz": [plz], "ensure": 1, "wait_s": 10}).json()
    assert res["regions"][str(plz)]["status"] == "ready"
    detail = client.get(f"/api/regions/{plz}").json()
    assert detail["selectable"] and detail["input"]["counts"]["buildings_total"] == source_buildings(conn, plz)
    assert detail["available"] and detail["infdb_buildings"] == detail["input"]["counts"]["buildings_importable"]


def test_search_and_postcodes_carry_the_verdict(client, plz, no_geometry, library):
    client.get("/api/regions/input", params={"plz": [plz], "ensure": 1, "wait_s": 10})
    hits = client.get("/api/regions/search", params={"q": str(plz)[:4]}).json()
    hit = next(h for h in hits if h["plz"] == plz)
    assert hit["selectable"] and hit["input"]["status"] == "ready"
    assert all(h["available"] == h["has_geometry"] for h in hits)
    missing = [h for h in hits if not h["has_geometry"]]
    assert no_geometry in {h["plz"] for h in missing}
    assert all(not h["selectable"] and h["input"]["status"] == "no_geometry" for h in missing)
    overview = client.get("/api/regions/overview").json()
    fc = client.get("/api/regions/postcodes", params={"bbox": ",".join(map(str, overview["bounds"])),
                                                      "zoom": 12}).json()
    props = {f["properties"]["plz"]: f["properties"] for f in fc["features"]}
    assert props[plz]["selectable"] and props[plz]["n_buildings"] == len(library["buildings"]) and "texts" in fc["input"]


def test_available_lists_the_checked_postcodes_with_input(client, conn, plz, no_geometry):
    client.get("/api/regions/input", params={"plz": [plz, no_geometry], "ensure": 1, "wait_s": 10})
    page = client.get("/api/regions/available").json()          # not swallowed by /{plz}
    rows = {r["plz"]: r for r in page["rows"]}
    assert plz in rows and no_geometry not in rows
    assert all(r["selectable"] and r["input"]["status"] not in ("pending", "unknown") for r in page["rows"])
    versions = [v for (v,) in q(conn, "SELECT DISTINCT version_id FROM pylovo.grid_result WHERE plz = %s ORDER BY 1", (plz,))]
    assert rows[plz]["has_geometry"] and rows[plz]["name_city"] and rows[plz]["versions"] == versions
    assert page["total"] >= page["plz_total"] >= 1 and {"pending", "state"} <= set(page)
    assert set(rows[plz]) >= {"plz", "ags", "note", "pop", "input"}          # the /search row shape
    second = client.get("/api/regions/available", params={"limit": 1, "offset": 1}).json()
    assert second["offset"] == 1 and len(second["rows"]) == min(1, page["total"] - 1)
    only = client.get("/api/regions/search", params={"q": str(plz)[:4], "only_selectable": 1}).json()
    assert only and all(h["selectable"] or h["versions"] for h in only)


def test_generate_check_blocks_plz_without_input(client, plz, no_geometry, library):
    client.get("/api/regions/input", params={"plz": [plz, no_geometry], "ensure": 1, "wait_s": 10})
    check = client.get("/api/jobs/generate/check", params={"plz": [plz, no_geometry]}).json()
    states = {r["plz"]: r for r in check["regions"]}
    assert states[no_geometry]["state"] == "blocked" and states[no_geometry]["input"]["status"] == "no_geometry"
    assert check["blocked"] == [no_geometry] and check["input_verified"]
    assert states[plz]["buildings"] == len(library["buildings"]) and states[plz]["state"] in ("new", "exists")
    refused = client.post("/api/jobs/generate", json={"plz": [no_geometry]})
    assert refused.status_code == 409 and refused.json()["detail"]["blocked"][0]["plz"] == no_geometry
    nothing = client.post("/api/jobs/generate", json={"plz": [no_geometry], "skip_blocked": True})
    assert nothing.status_code == 422                                # no job was started
