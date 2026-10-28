"""End-to-end smoke test of the LV generation on the toy-town test database (opt-in; only reads).

    uv run --extra api --extra plots python tests/db/run_database_tests.py

Or build the database with ``tests/db/build_test_database.py`` and set ``PYLOVO_TOY_TOWN_DATABASE`` to it (section
"Database tests" of the development guide); the variable must name the database pylovo's configuration points to.

The key figures of the generated grids are compared with the snapshot ``tests/db/toy_town_results.json``, so a
change of the results shows up in a test. After an intended change, write the snapshot again and commit it with the
change and a changelog line::

    PYLOVO_UPDATE_TOY_TOWN_SNAPSHOT=1 uv run --extra api --extra plots python tests/db/run_database_tests.py -- -k snapshot
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).parent / "db" / "toy_town_results.json"

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


def toy_town_results(q) -> dict:
    """Key figures per version and postcode: grids, transformers, cables, connected buildings and voltage drop."""
    results: dict = {}

    def entry(version: str, plz: int) -> dict:
        return results.setdefault(version, {}).setdefault(str(plz), {})

    for version, plz, grids, converged, drop in q("""
            SELECT version_id, plz, count(*), count(*) FILTER (WHERE power_flow_status = 'converged'),
                   max(max_total_lv_voltage_drop_pu)
            FROM pylovo.grid_result GROUP BY 1, 2"""):
        entry(version, plz).update(grids=grids, converged=converged, max_lv_voltage_drop_pu=round(drop, 6))
    for version, plz, rating, count in q("""
            SELECT version_id, plz, transformer_rated_power, count(*) FROM pylovo.grid_result GROUP BY 1, 2, 3"""):
        entry(version, plz).setdefault("transformers_kva", {})[str(rating)] = count
    for version, plz, std_type, parallel, km in q("""
            SELECT g.version_id, g.plz, l.std_type, l.parallel, sum(l.length_km)
            FROM pylovo.lines_result l JOIN pylovo.grid_result g USING (grid_result_id) GROUP BY 1, 2, 3, 4"""):
        entry(version, plz).setdefault("cable_km", {})[f"{std_type} x{parallel}"] = round(km, 6)
    for version, plz, buildings, peak in q("""
            SELECT b.version_id, g.plz, count(*), sum(b.peak_load_in_kw)
            FROM pylovo.buildings_result b JOIN pylovo.grid_result g USING (grid_result_id) GROUP BY 1, 2"""):
        entry(version, plz).update(buildings=buildings, peak_load_kw=round(peak, 6))
    return results


def differences(expected, actual, path: str = "") -> list[str]:
    """The paths whose values differ; numbers count as equal within 1e-6 (relative) or 1e-5 (absolute)."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        return [line for key in sorted(expected.keys() | actual.keys())
                for line in differences(expected.get(key), actual.get(key), f"{path}/{key}")]
    numbers = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (expected, actual))
    if expected == actual or numbers and math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-5):
        return []
    return [f"{path.lstrip('/')}: {expected} -> {actual}"]


def test_the_results_equal_the_snapshot(q):
    results = toy_town_results(q)
    if os.getenv("PYLOVO_UPDATE_TOY_TOWN_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"wrote {SNAPSHOT}: check its diff and commit it with the change")
    changed = differences(json.loads(SNAPSHOT.read_text(encoding="utf-8")), results)
    assert not changed, (
        f"The toy-town results differ from {SNAPSHOT.name} (version/postcode/figure: snapshot -> now):\n  "
        + "\n  ".join(changed)
        + "\nIf the change is intended, write the snapshot again (see the docstring of this module) and commit it.")
