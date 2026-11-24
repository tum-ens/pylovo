"""End-to-end smoke test of the LV generation and the MV stage on the toy-town test database (opt-in; only reads).

    uv run --extra api --extra plots python tests/db/run_database_tests.py

Or build the database with ``tests/db/build_test_database.py`` and set ``PYLOVO_TOY_TOWN_DATABASE`` to it (section
"Database tests" of the development guide); the variable must name the database pylovo's configuration points to.

The key figures of the generated LV grids and MV variants are compared with the snapshot
``tests/db/toy_town_results.json``, so a
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

from db import toy_town  # tests/db/toy_town.py, the input of the test database

SNAPSHOT = Path(__file__).parent / "db" / "toy_town_results.json"

DATABASE = os.getenv("PYLOVO_TOY_TOWN_DATABASE")
pytestmark = pytest.mark.skipif(
    not DATABASE, reason="set PYLOVO_TOY_TOWN_DATABASE to the toy-town test database (tests/db/build_test_database.py)")
#: the versions of tests/db/build_test_database.py
VERSIONS = ["1", "2", "3", "4", "5"]
#: the MV variants of tests/db/build_test_database.py (of version 1)
MV_VARIANTS = ["parallel", "rings", "serial"]


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
    assert [v for (v,) in q("SELECT version_id FROM pylovo.version ORDER BY version_id")] == VERSIONS
    rows = q("""SELECT version_id, plz, count(*), count(*) FILTER (WHERE power_flow_status = 'converged'),
                       count(*) FILTER (WHERE bcid < 0)
                FROM pylovo.grid_result GROUP BY 1, 2 ORDER BY 1, 2""")
    assert [(v, plz) for v, plz, *_ in rows] == [(v, plz) for v in VERSIONS for plz in postcodes]
    assert all(grids == converged > 0 for _, _, grids, converged, _ in rows), rows
    # version 2 uses the existing (LoD2) station of the town, version 5 the manual one, the others are greenfield
    assert {(v, plz): existing for v, plz, _, _, existing in rows if existing} == {("2", 99001): 1, ("5", 99001): 1}


def test_every_building_is_accounted_for(q, postcodes, tmp_path):
    from pylovo.cli.analyze import run_diagnostics

    for version in VERSIONS:
        assert run_diagnostics(postcodes, coverage=True, border=False, version_id=version,
                               output=tmp_path / version) == 0
    # the same candidates in every version, only the reasons differ
    assert all(accounted(q, version) == accounted(q, "1") for version in VERSIONS)
    reasons = {(v, r): n for v, r, n in q("""SELECT version_id, reason, count(*) FROM pylovo.building_exclusion
                                             GROUP BY 1, 2""")}
    # the plant is MV-direct and the building of unknown use has no load; residential-only leaves out both, the four
    # shops and the school
    assert reasons == {**{(v, r): 1 for v in ("1", "2", "4", "5") for r in ("mv_direct", "unloaded")},
                       ("3", "residential_only"): 7}
    # residential-only leaves out exactly the buildings without a residential part
    residential_only = q("""SELECT count(*) FROM pylovo.buildings_result r JOIN pylovo.building_exclusion e
                                USING (objectid)
                            WHERE r.version_id = '1' AND e.version_id = '3' AND COALESCE(r.residential_floor_area, 0) > 0""")
    assert residential_only == [(0,)]


def test_missing_household_counts_follow_the_building_type(q):
    # the fallback of PreprocessingMixin.set_building_peak_load for buildings without an InfDB count:
    # MFH max(2, round(588 m2 / 181)), AB max(5, round(1280 m2 / 146)), untyped max(1, round(240 m2 / 181))
    expected = {"MFH_NO_COUNT": 3, "AB_NO_COUNT": 9, "UNTYPED_NO_COUNT": 1}
    kinds = {b.objectid: name for b in toy_town.buildings() for name in expected if b.kind is toy_town.KINDS[name]}
    rows = q("SELECT objectid, households FROM pylovo.buildings_result WHERE version_id = '1' AND objectid = ANY(%s)",
             (list(kinds),))
    assert {kinds[objectid]: households for objectid, households in rows} == expected


def test_auxiliary_buildings_are_never_consumers(q):
    # the InfDB class Auxiliary (garage, farm shed, transformer station) is left out by its use, whatever its
    # address: in no grid and without an exclusion reason, in every version
    auxiliary = [b.objectid for b in toy_town.buildings() if b.kind.building_use == "Auxiliary"]
    assert len(auxiliary) == 3
    assert q("""SELECT count(*) FROM (SELECT objectid FROM pylovo.buildings_result
                                     UNION ALL SELECT objectid FROM pylovo.building_exclusion) x
                WHERE objectid = ANY(%s)""", (auxiliary,)) == [(0,)]


def test_a_leisure_building_gets_one_household_load_without_residents(q):
    # InfDB leisure buildings (1310 to 1312) have one household and no census residents
    from pylovo.config_loader import PEAK_LOAD_HOUSEHOLD

    leisure = [b.objectid for b in toy_town.buildings() if b.kind is toy_town.KINDS["LEISURE"]]
    rows = q("""SELECT version_id, households, occupants, residential_peak_load_in_kw, nonresidential_peak_load_in_kw
                FROM pylovo.buildings_result WHERE objectid = ANY(%s) ORDER BY version_id""", (leisure,))
    assert [row[:3] for row in rows] == [(version, 1, 0) for version in VERSIONS]
    assert all(math.isclose(res, PEAK_LOAD_HOUSEHOLD) and nonres == 0 for *_, res, nonres in rows)


def test_a_house_without_address_is_a_consumer(q):
    # the toy town runs without the address filter (EXCLUDE_BUILDINGS_WITHOUT_ADDRESS: False): with the class
    # Auxiliary the InfDB has left out the outbuildings already, and in states without addresses no house has one
    house = [b.objectid for b in toy_town.buildings() if b.kind is toy_town.KINDS["HOUSE_NO_ADDRESS"]]
    assert q("SELECT version_id FROM pylovo.buildings_result WHERE objectid = ANY(%s) ORDER BY 1", (house,)) \
        == [(version,) for version in VERSIONS]


def test_the_manual_station_keeps_its_rated_power(q):
    # version 5 uses only the manual positions; the rating entered for the station wins over the catalogue
    # (ClusteringMixin.get_existing_transformer_capacity_trafo_ui), which would choose 400 or 630 kVA here
    rated_power = toy_town.MANUAL_STATION[2]
    assert q("SELECT transformer_rated_power FROM pylovo.grid_result WHERE version_id = '5' AND bcid < 0") \
        == [(rated_power,)]


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
    results["mv"] = mv_results(q)
    return results


def mv_results(q) -> dict:
    """Key figures per MV variant of version 1: the run, the ties and links, per district (by the name of its
    substation) the structure, the HV/MV sizing and the power flow extremes, and the line length per role, type and
    number of parallel cables."""
    results: dict = {}
    for variant, summary in q("SELECT mv_variant_id, summary FROM pylovo.mv_variant WHERE version_id = '1'"):
        ties, links = summary.get("ties") or {}, summary.get("links") or {}
        results[variant] = {
            "status": summary["status"], "grids_done": summary["grids_done"], "n_stations": summary["n_stations"],
            "n_customers": summary["n_customers"],
            "ties": {"n": ties.get("n_ties", 0), "km": round(ties.get("tie_km", 0.0), 6)},
            "links": {"n": links.get("n_links", 0), "km": round(links.get("link_km", 0.0), 6)}}
    for variant, name, *figures in q("""
            SELECT g.mv_variant_id, s.name, g.vn_kv, g.power_flow_status, g.coupled_status, g.n_stations,
                   g.n_customers, g.n_rings, g.n_stub_nodes, g.n_joints, g.hv_mv_transformers, g.hv_mv_transformer_mva,
                   g.oltc_tap_pos, g.peak_load_mw, g.circuit_km, g.max_voltage_drop_percent,
                   g.max_voltage_drop_n1_percent, g.max_line_loading_percent, jsonb_array_length(g.repairs)
            FROM pylovo.mv_grid g JOIN pylovo.mv_substation s USING (substation_id)
            WHERE g.version_id = '1'"""):
        keys = ["vn_kv", "power_flow_status", "coupled_status", "n_stations", "n_customers", "n_rings", "n_stub_nodes",
                "n_joints", "hv_mv_transformers", "hv_mv_transformer_mva", "oltc_tap_pos", "peak_load_mw", "circuit_km",
                "max_voltage_drop_percent", "max_voltage_drop_n1_percent", "max_line_loading_percent", "n_repairs"]
        results[variant].setdefault("districts", {})[name] = {
            key: round(value, 6) if isinstance(value, float) else value for key, value in zip(keys, figures)}
    for variant, role, std_type, parallel, km in q("""
            SELECT mv_variant_id, role, std_type, parallel, sum(length_km) FROM pylovo.mv_line
            WHERE version_id = '1' GROUP BY 1, 2, 3, 4"""):
        results[variant].setdefault("line_km", {}).setdefault(role, {})[f"{std_type} x{parallel}"] = round(km, 6)
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


def test_parallel_workers_give_the_grids_of_the_serial_run(q):
    # version 4 is version 1 generated in worker processes, one per postcode
    results = toy_town_results(q)
    assert differences(results["1"], results["4"]) == []


def test_parallel_mv_workers_give_the_districts_of_the_serial_run(q):
    mv = mv_results(q)
    assert sorted(mv) == MV_VARIANTS
    assert all(variant["status"] == "done" for variant in mv.values())
    assert differences(mv["serial"], mv["parallel"]) == []


def test_the_mv_variant_with_small_rings_has_ties_links_and_coupled_districts(q):
    rings = mv_results(q)["rings"]
    assert rings["ties"]["n"] > 0 and rings["links"]["n"] > 0
    assert sorted(rings["line_km"]) == ["link", "ring", "tie"]
    assert {district["coupled_status"] for district in rings["districts"].values()} == {"done"}


@pytest.mark.parametrize("case", ["load", "n1a"])
def test_a_stored_mv_net_is_exported_and_solves_again(q, tmp_path, case):
    import pandapower as pp

    from pylovo.mv_grid.export import export_net

    [(grid,)] = q("SELECT min(mv_grid_id) FROM pylovo.mv_grid WHERE version_id = '1' AND mv_variant_id = 'rings'")
    net = pp.from_json(str(export_net("1", "rings", grid, tmp_path / f"{case}.json", case=case)))
    stored = net.res_bus.vm_pu.copy()
    pp.runpp(net)
    assert len(stored) == len(net.bus) and (net.res_bus.vm_pu - stored).abs().max() < 1e-6


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
