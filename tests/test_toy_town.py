"""End-to-end smoke test of the LV generation on the toy-town test database (opt-in; only reads).

    PYLOVO_TOY_TOWN_DATABASE=pylovo_test uv run pytest tests/test_toy_town.py

Build the database first with ``tests/db/build_test_database.py`` (section "Tests" of the development guide). The
variable must name the database pylovo's configuration points to.
"""
from __future__ import annotations

import os

import pytest

DATABASE = os.getenv("PYLOVO_TOY_TOWN_DATABASE")
pytestmark = pytest.mark.skipif(
    not DATABASE, reason="set PYLOVO_TOY_TOWN_DATABASE to the toy-town test database (tests/db/build_test_database.py)")


@pytest.fixture(scope="module")
def q():
    from pylovo import config_loader as cl
    from pylovo.database.database_client import DatabaseClient

    if cl.DBNAME != DATABASE:
        pytest.skip(f"PYLOVO_TOY_TOWN_DATABASE={DATABASE} does not match the configured database {cl.DBNAME}")
    with DatabaseClient() as client:
        def query(sql, params=None):
            client.cur.execute(sql, params)
            return client.cur.fetchall()

        yield query
        client.conn.rollback()


@pytest.fixture(scope="module")
def postcodes(q) -> list[int]:
    from pylovo.config_loader import INFDB_OPENDATA_SCHEMA

    return [plz for (plz,) in q(f'SELECT plz FROM "{INFDB_OPENDATA_SCHEMA}".postcodes_germany ORDER BY plz')]


def accounted(q, version: str) -> set[str]:
    """Buildings of a version that are in a grid or left out with a reason."""
    return {o for (o,) in q("""SELECT objectid FROM pylovo.buildings_result WHERE version_id = %(v)s
                               UNION SELECT objectid FROM pylovo.building_exclusion WHERE version_id = %(v)s""",
                            {"v": version})}


def test_every_postcode_has_converged_grids_in_every_version(q, postcodes):
    assert postcodes == [99001, 99002]
    assert [v for (v,) in q("SELECT version_id FROM pylovo.version ORDER BY version_id")] == ["1", "2", "3"]
    rows = q("""SELECT version_id, plz, count(*), count(*) FILTER (WHERE power_flow_status = 'converged'),
                       count(*) FILTER (WHERE bcid < 0)
                FROM pylovo.grid_result GROUP BY 1, 2 ORDER BY 1, 2""")
    assert [(v, plz) for v, plz, *_ in rows] == [(v, plz) for v in "123" for plz in postcodes]
    assert all(grids == converged > 0 for _, _, grids, converged, _ in rows), rows
    # version 2 uses the existing (LoD2) station of the town, the others are greenfield
    assert {(v, plz): existing for v, plz, _, _, existing in rows if existing} == {("2", 99001): 1}


def test_every_building_is_accounted_for(q, postcodes, tmp_path):
    from pylovo.cli.analyze import run_diagnostics

    for version in ("1", "2", "3"):
        assert run_diagnostics(postcodes, coverage=True, border=False, version_id=version,
                               output=tmp_path / version) == 0
    # the same candidates in every version, only the reasons differ
    assert accounted(q, "1") == accounted(q, "2") == accounted(q, "3")
    reasons = {(v, r): n for v, r, n in q("""SELECT version_id, reason, count(*) FROM pylovo.building_exclusion
                                             GROUP BY 1, 2""")}
    # the plant is MV-direct and the building of unknown use has no load; residential-only leaves out both, the four
    # shops and the school
    assert reasons == {("1", "mv_direct"): 1, ("1", "unloaded"): 1, ("2", "mv_direct"): 1, ("2", "unloaded"): 1,
                       ("3", "residential_only"): 7}
    # residential-only leaves out exactly the buildings without a residential part
    residential_only = q("""SELECT count(*) FROM pylovo.buildings_result r JOIN pylovo.building_exclusion e
                                USING (objectid)
                            WHERE r.version_id = '1' AND e.version_id = '3' AND COALESCE(r.residential_floor_area, 0) > 0""")
    assert residential_only == [(0,)]
