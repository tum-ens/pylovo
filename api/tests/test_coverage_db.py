"""Region gate against the sandbox database (opt-in, see conftest.py).

The equivalence tests run the library's own code (``InfdbClient`` fetches and the
``PreprocessingMixin`` residential filter inside a rolled-back transaction) and compare the row
counts with the counters of :mod:`pylovo_api.coverage`, which replicates those filters in SQL.
"""
from __future__ import annotations

import pytest
from conftest import requires_db
from pylovo_api import db
from pylovo_api.coverage import InputCoverage, building_counts

pytestmark = requires_db
PLZ = 85653  # the OSM demo region of every sandbox database


@pytest.fixture(scope="module")
def engine():
    cov = InputCoverage()
    cov.start()
    cov.ensure([PLZ], wait_s=15)
    yield cov
    cov.shutdown()


def counters(cov: InputCoverage, table: str, plz: int = PLZ) -> list[int]:
    rows = cov.tables[table].plz_rows.get(plz, {})
    total = None
    for c in rows.values():
        total = c if total is None else [a + b for a, b in zip(total, c)]
    return total


def test_demo_region_counts(engine):
    st = engine.status_of(PLZ, {"exclude_buildings_without_address": True, "residential_only": False})
    assert st["status"] == "ready" and st["exact"]
    assert st["counts"] == {"buildings_total": 522, "buildings_importable": 472, "residential": 443,
                            "station_buildings": 1, "invalid_area_rows": 0, "street_segments": 961,
                            "connection_lines": 522}
    assert {t.strategy for t in engine.tables.values()} == {"full"}   # small tables: one GROUP BY each


@pytest.mark.parametrize("require_address", [False, True])
def test_building_filter_matches_the_library(engine, monkeypatch, require_address):
    import pylovo.infdb.infdb_client as ic
    from pylovo.database.database_client import DatabaseClient

    monkeypatch.setattr(ic, "EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", require_address)
    flags = {"exclude_buildings_without_address": require_address, "residential_only": False}
    counts = building_counts(counters(engine, "buildings"), flags)
    rows = ic.InfdbClient().fetch_buildings_from_infdb(PLZ)
    assert len(rows) == counts["importable"]
    # RESIDENTIAL_ONLY_GENERATION: the library's own delete on its working table, rolled back
    client = DatabaseClient()
    try:
        client.create_temp_tables(PLZ)
        client.set_buildings_table(rows, PLZ)
        removed = client.remove_non_residential_buildings_from_buildings_tem()
        assert len(rows) - removed == counts["residential"]
        ro = building_counts(counters(engine, "buildings"), flags | {"residential_only": True})
        assert ro["importable"] == len(rows) - removed
    finally:
        client.conn.rollback()
        client.close()


def test_way_filter_matches_the_library(engine):
    import pylovo.infdb.infdb_client as ic

    ways = ic.InfdbClient().fetch_ways_from_infdb(PLZ)
    lines = sum(1 for w in ways if w[0] == 110)                     # klasse 'connection_line'
    assert len(ways) - lines == counters(engine, "ways")[0]
    assert lines == counters(engine, "lines")[0]


def test_per_municipality_strategy_equals_the_single_statement(engine):
    """The real-InfDB path (key listing + per-AGS chunks) gives the same counters."""
    has_key = db.fetch_one("SELECT count(*) AS n FROM information_schema.columns WHERE table_schema = 'basedata' "
                           "AND table_name = 'buildings' AND column_name = 'gemeindeschluessel'")["n"]
    if not has_key:
        pytest.skip("basedata.buildings has no gemeindeschluessel column")
    cov = InputCoverage(small_rows=0, conn_options="-c enable_seqscan=off")
    cov.start()
    try:
        cov.ensure([PLZ], wait_s=15)
        assert cov.tables["buildings"].strategy == "ags" or cov.tables["buildings"].reason
        if cov.tables["buildings"].strategy == "ags":
            assert counters(cov, "buildings") == counters(engine, "buildings")
    finally:
        cov.shutdown()


def test_coverage_api(client):
    info = client.get("/api/regions/coverage")
    assert info.status_code == 200                                   # was swallowed by /{plz} (422)
    body = info.json()
    assert body["mode"] == "infdb" and body["suggested_sql"] == []
    assert set(body["tables"]) == {"buildings", "ways", "lines"}
    res = client.get("/api/regions/input", params={"plz": [PLZ], "ensure": 1, "wait_s": 10}).json()
    assert res["regions"][str(PLZ)]["status"] == "ready"
    detail = client.get(f"/api/regions/{PLZ}").json()
    assert detail["selectable"] and detail["input"]["counts"]["buildings_total"] == 522
    assert detail["available"] and detail["infdb_buildings"] == detail["input"]["counts"]["buildings_importable"]


def test_search_and_postcodes_carry_the_verdict(client):
    client.get("/api/regions/input", params={"plz": [PLZ], "ensure": 1, "wait_s": 10})
    hits = client.get("/api/regions/search", params={"q": "8565"}).json()
    assert hits[0]["plz"] == PLZ and hits[0]["selectable"] and hits[0]["input"]["status"] == "ready"
    assert all(h["available"] == h["has_geometry"] for h in hits)
    missing = [h for h in hits if not h["has_geometry"]]
    assert missing and all(not h["selectable"] and h["input"]["status"] == "no_geometry" for h in missing)
    overview = client.get("/api/regions/overview").json()
    fc = client.get("/api/regions/postcodes", params={"bbox": ",".join(map(str, overview["bounds"])),
                                                      "zoom": 12}).json()
    props = {f["properties"]["plz"]: f["properties"] for f in fc["features"]}
    assert props[PLZ]["selectable"] and props[PLZ]["n_buildings"] == 472 and "texts" in fc["input"]


def test_available_lists_the_checked_postcodes_with_input(client):
    client.get("/api/regions/input", params={"plz": [PLZ, 85656], "ensure": 1, "wait_s": 10})
    page = client.get("/api/regions/available").json()          # not swallowed by /{plz}
    rows = {r["plz"]: r for r in page["rows"]}
    assert PLZ in rows and 85656 not in rows
    assert all(r["selectable"] and r["input"]["status"] not in ("pending", "unknown") for r in page["rows"])
    assert rows[PLZ]["has_geometry"] and rows[PLZ]["name_city"] and rows[PLZ]["versions"] == ["1", "2", "3"]
    assert page["total"] >= page["plz_total"] >= 1 and {"pending", "state"} <= set(page)
    assert set(rows[PLZ]) >= {"plz", "ags", "note", "pop", "input"}          # the /search row shape
    second = client.get("/api/regions/available", params={"limit": 1, "offset": 1}).json()
    assert second["offset"] == 1 and len(second["rows"]) == min(1, page["total"] - 1)
    only = client.get("/api/regions/search", params={"q": "8565", "only_selectable": 1}).json()
    assert only and all(h["selectable"] or h["versions"] for h in only)


def test_generate_check_blocks_plz_without_input(client):
    client.get("/api/regions/input", params={"plz": [PLZ, 85656], "ensure": 1, "wait_s": 10})
    check = client.get("/api/jobs/generate/check", params={"plz": [PLZ, 85656]}).json()
    states = {r["plz"]: r for r in check["regions"]}
    assert states[85656]["state"] == "blocked" and states[85656]["input"]["status"] == "no_geometry"
    assert check["blocked"] == [85656] and check["input_verified"]
    assert states[PLZ]["buildings"] == 472 and states[PLZ]["state"] in ("new", "exists")
    refused = client.post("/api/jobs/generate", json={"plz": [85656]})
    assert refused.status_code == 409 and refused.json()["detail"]["blocked"][0]["plz"] == 85656
    nothing = client.post("/api/jobs/generate", json={"plz": [85656], "skip_blocked": True})
    assert nothing.status_code == 422                                # no job was started
