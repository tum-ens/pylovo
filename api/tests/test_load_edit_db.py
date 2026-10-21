"""Load editing against the test database (opt-in, see conftest.py).

WRITES: the round-trip tests apply load edits and undo them again, so the grids end byte-identical
to generation, but audit rows (``pylovo.load_edit``, all undone) and the table itself remain. Only
run it against a throwaway test database (``tests/db/build_test_database.py``: the toy town, versions 1-3).
The edited grid and buildings are chosen by query (see ``picks``); the expected values are read from the
database or compared before and after an edit.

The first tests are the equivalence proof of the replicated generation code: for every stored grid
the loads rebuilt from ``buildings_result`` and the version snapshot equal the stored loads, the
rebuilt network JSON is byte-identical, and the validation power flow reproduces the stored status
and voltage drops.
"""
from __future__ import annotations

import time

import psycopg2
import pytest
from conftest import q, requires_db

pytestmark = requires_db

# grids of a version with a purely residential building of at least two households and a single-household
# building: voltage violations first, then the most households
GRID_SQL = """
    SELECT g.grid_result_id, g.plz FROM pylovo.grid_result g
    WHERE g.version_id = %s
      AND EXISTS (SELECT 1 FROM pylovo.buildings_result b WHERE b.grid_result_id = g.grid_result_id
                  AND b.households >= 2 AND COALESCE(b.nonresidential_floor_area, 0) = 0)
      AND EXISTS (SELECT 1 FROM pylovo.buildings_result b WHERE b.grid_result_id = g.grid_result_id
                  AND b.households = 1 AND b.residential_floor_area > 0)
    ORDER BY g.power_flow_status = 'voltage_violation' DESC,
             (SELECT sum(households) FROM pylovo.buildings_result b WHERE b.grid_result_id = g.grid_result_id) DESC,
             g.grid_result_id
    LIMIT 1
"""


@pytest.fixture(scope="module")
def picks(conn) -> dict:
    """The edited grid of version 1 (``gid``, ``plz``) and its purely residential building with the most
    households (``building``, ``households``); a version-3 building with a residential and a commercial part
    of more than 200 m2 (``v3_mixed``)."""
    rows = q(conn, GRID_SQL, ("1",))
    if not rows:
        pytest.skip("no grid of version 1 with a multi-household and a single-household building")
    gid, plz = rows[0]
    building, households = q(conn, """SELECT objectid, households FROM pylovo.buildings_result
                                      WHERE grid_result_id = %s AND COALESCE(nonresidential_floor_area, 0) = 0
                                      ORDER BY households DESC, objectid LIMIT 1""", (gid,))[0]
    mixed = q(conn, """SELECT objectid FROM pylovo.buildings_result WHERE version_id = '3'
                       AND residential_floor_area > 0 AND nonresidential_floor_area > 200
                       AND nonresidential_use = 'Commercial' ORDER BY objectid LIMIT 1""")
    return {"gid": gid, "plz": plz, "building": building, "households": households,
            "v3_mixed": mixed[0][0] if mixed else None}


def fingerprint(conn, gid, version="1", plz=None):
    """Everything an edit writes, in comparable form (jsonb comparisons are exact)."""
    return {
        "net": q(conn, "SELECT md5(grid::text), power_flow_status, max_feeder_voltage_drop_pu, max_service_voltage_drop_pu, "
                       "max_total_lv_voltage_drop_pu FROM pylovo.grid_result WHERE grid_result_id=%s", (gid,))[0],
        "loads": q(conn, "SELECT md5(string_agg((to_jsonb(l) - 'pandapower_load_id')::text, '|' ORDER BY pp_index)) "
                         "FROM pylovo.pandapower_load l WHERE grid_result_id=%s", (gid,))[0][0],
        "buses": q(conn, "SELECT md5(string_agg(pp_index || ':' || COALESCE(zone, ''), '|' ORDER BY pp_index)) "
                         "FROM pylovo.pandapower_bus WHERE grid_result_id=%s", (gid,))[0][0],
        "buildings": q(conn, "SELECT md5(string_agg(to_jsonb(b)::text, '|' ORDER BY objectid)) FROM pylovo.buildings_result b "
                             "WHERE grid_result_id=%s", (gid,))[0][0],
        "plz_parameters": q(conn, "SELECT to_jsonb(p)::text FROM pylovo.plz_parameters p WHERE version_id=%s AND plz=%s",
                            (version, plz)),
        "clustering": q(conn, "SELECT to_jsonb(c)::text FROM pylovo.clustering_parameters c WHERE grid_result_id=%s", (gid,)),
    }


def peak_per_household(conn, version="1"):
    """The residential peak per household (kW) of the version snapshot."""
    import pylovo.load_editing as le

    gp = q(conn, "SELECT generation_parameters FROM pylovo.version WHERE version_id=%s", (version,))[0][0]
    return le.LoadParameters.from_generation_parameters(version, gp).residential_peak_kw


def context(client, gid, objectid):
    res = client.get(f"/api/grids/{gid}/load-edit/building", params={"objectid": objectid})
    assert res.status_code == 200, res.text
    return res.json()


def preview(client, gid, objectid, households):
    res = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": objectid, "changes": {"households": households}})
    assert res.status_code == 200 and res.json()["valid"], res.text
    return res.json()


def apply(client, gid, objectid, changes, **extra):
    ctx = context(client, gid, objectid)
    body = {"objectid": objectid, "changes": changes, "if_match": ctx["etag"], "first_version_edit_confirm": ctx["version_id"]}
    body.update(extra)
    return client.post(f"/api/grids/{gid}/load-edits", json=body)


def undo_everything(client, conn):
    for (gid,) in q(conn, "SELECT DISTINCT grid_result_id FROM pylovo.load_edit WHERE undone_at IS NULL") \
            if q(conn, "SELECT to_regclass('pylovo.load_edit') IS NOT NULL")[0][0] else []:
        assert client.post(f"/api/grids/{gid}/load-edits/undo-all", json={}).status_code == 200


# --------------------------------------------------------------------------- equivalence with generation
def test_every_stored_grid_is_reproduced(client, conn):
    """The replicated LoadSpec mapping and save_net validation reproduce all stored grids."""
    ids = [r[0] for r in q(conn, "SELECT grid_result_id FROM pylovo.grid_result ORDER BY 1")]
    assert ids
    for gid in ids:
        check = client.get(f"/api/grids/{gid}/load-edit/check").json()
        assert check["storage"]["consistent"], (gid, check["storage"])
        assert check["reproduction"]["tier"] == "exact", (gid, check["reproduction"])
        assert check["reproduction"]["buildings_checked"] > 0
        pf = check["pf_baseline"]
        assert pf["status_recomputed"] == pf["status_stored"] and pf["max_drop_diff_pu"] <= 1e-9, (gid, pf)
        assert check["editable"]


def test_rebuilt_network_json_is_byte_identical(conn):
    """Rebuilding net.load through PandapowerBackend gives the stored JSON back, byte for byte."""
    import pandapower as pp

    import pylovo.load_editing as le

    rows = q(conn, "SELECT g.grid_result_id, g.version_id, g.grid::text, v.generation_parameters FROM pylovo.grid_result g "
                   "JOIN pylovo.version v USING (version_id) ORDER BY 1")
    cols = ", ".join(le.BUILDING_FRAME_COLUMNS)
    for gid, version, text, gp in rows:
        params = le.LoadParameters.from_generation_parameters(version, gp)
        with conn.cursor() as cur:
            cur.execute(f"SELECT {cols} FROM pylovo.buildings_result WHERE grid_result_id=%s", (gid,))
            buildings = le.buildings_frame([dict(zip(le.BUILDING_FRAME_COLUMNS, r)) for r in cur.fetchall()])
        net = pp.from_json_string(text)
        assert le.replace_net_loads(net, le.snapshot_loads(buildings, params)) == []
        assert pp.to_json(net) == text, gid


def test_component_peaks_match_the_sql(conn):
    """component_peaks and the SQL of set_building_peak_load give bit-identical floats."""
    import pylovo.load_editing as le

    gp = q(conn, "SELECT generation_parameters FROM pylovo.version WHERE version_id='1'")[0][0]
    p = le.LoadParameters.from_generation_parameters("1", gp)
    cases = [(h, 100.0 + 7.3 * h, a, "Commercial" if a else None) for h in range(1, 13) for a in (0.0, 85.37, 1266.0)]
    cases += [(1, 0.0, 1265.8228, "Commercial"), (1, 0.0, 1265.8229, "Commercial"), (3, 211.1, 333.33, "Public")]
    for h, res, nonres, use in cases:
        sql = q(conn, """SELECT CASE WHEN COALESCE(%(res)s::float8, 0) > 0 THEN %(h)s::int * %(pres)s::float8 ELSE 0 END,
                                CASE WHEN COALESCE(%(nonres)s::float8, 0) > 0 THEN %(nonres)s::float8 * %(w)s::float8 / 1000 ELSE 0 END""",
                {"res": res, "h": h, "pres": p.residential_peak_kw, "nonres": nonres,
                 "w": p.peak_load_per_m2_w.get(use or "Commercial")})[0]
        mine = le.component_peaks(le.BuildingInputs(h, res, nonres, use), p)
        assert (mine.residential_peak_load_in_kw, mine.nonresidential_peak_load_in_kw) == (float(sql[0]), float(sql[1]))


# --------------------------------------------------------------------------- read-only paths
def test_context_and_preview_are_read_only(client, conn, picks):
    undo_everything(client, conn)
    gid, building = picks["gid"], picks["building"]
    before = fingerprint(conn, gid, plz=picks["plz"])
    ctx = context(client, gid, building)
    assert ctx["building"]["households"] == picks["households"] and ctx["checks"]["reproduction"]["tier"] == "exact"
    kw = ctx["parameters"]["residential_peak_kw"]
    assert kw == peak_per_household(conn)
    for households in (1, picks["households"] + 1, 2 * picks["households"]):
        preview(client, gid, building, households)
    # one household: less coincident load (the coincidence factor of the other loads rises, so the weakest
    # voltage can still fall slightly)
    p = preview(client, gid, building, 1)
    assert p["building"]["after"]["peaks"]["peak_load_in_kw"] == kw
    total = q(conn, "SELECT count(*) FROM pylovo.pandapower_load WHERE grid_result_id=%s", (gid,))[0][0]
    assert 0 < p["loads"]["changed"] <= p["loads"]["total"] == total
    assert p["grid"]["coincident_kw"][1] < p["grid"]["coincident_kw"][0]
    assert all(0.8 < vm <= 1.1 for vm in p["power_flow"]["min_vm_pu"])
    stored = q(conn, "SELECT power_flow_status FROM pylovo.grid_result WHERE grid_result_id=%s", (gid,))[0][0]
    assert p["power_flow"]["status"][0] == stored
    invalid = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": building, "changes": {"households": 0}}).json()
    assert not invalid["valid"] and invalid["errors"][0]["field"] == "households"
    missing = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": "nope", "changes": {"households": 3}})
    assert missing.status_code == 404
    assert fingerprint(conn, gid, plz=picks["plz"]) == before


# --------------------------------------------------------------------------- write paths
def test_apply_and_undo_round_trip(client, conn, picks):
    undo_everything(client, conn)
    gid, building, plz = picks["gid"], picks["building"], picks["plz"]
    before = fingerprint(conn, gid, plz=plz)
    assert before["plz_parameters"] and before["clustering"]
    predicted = preview(client, gid, building, 1)["loads"]
    res = apply(client, gid, building, {"households": 1}, reason="=cmd audit test")
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["removed_analysis"] == {"plz_parameters": True, "clustering_parameters": True}
    assert out["grid"]["load_edit"]["modified"] and out["grid"]["load_edit"]["edited_buildings"] == 1
    after = fingerprint(conn, gid, plz=plz)
    assert after["net"][0] != before["net"][0] and after["loads"] != before["loads"]
    assert not after["plz_parameters"] and not after["clustering"]
    row = q(conn, "SELECT households, peak_load_in_kw FROM pylovo.buildings_result WHERE version_id='1' AND objectid=%s",
            (building,))[0]
    assert row == (1, peak_per_household(conn))
    edit = q(conn, "SELECT after_net_md5, before_net_md5, jsonb_array_length(before_loads), changes FROM pylovo.load_edit "
                   "WHERE load_edit_id=%s", (out["load_edit_id"],))[0]
    assert edit[0] == after["net"][0] and edit[1] == before["net"][0] and edit[2] == predicted["total"]
    assert edit[3] == {"households": [picks["households"], 1]}
    changed = q(conn, """SELECT count(*) FROM pylovo.pandapower_load l
                         JOIN jsonb_populate_recordset(NULL::pylovo.pandapower_load,
                              (SELECT before_loads FROM pylovo.load_edit WHERE load_edit_id=%s)) b USING (pp_index)
                         WHERE l.grid_result_id=%s AND l.p_mw <> b.p_mw""", (out["load_edit_id"], gid))[0][0]
    assert changed == predicted["changed"]  # the preview predicted the written loads
    status = client.get("/api/status").json()["load_edit"]
    assert status["schema"] == "ok" and status["views_stale"] == 0
    csv = client.get("/api/versions/1/load-edits", params={"format": "csv"}).text
    assert "'=cmd audit test" in csv
    undo = client.post(f"/api/load-edits/{out['load_edit_id']}/undo", json={"if_match": out["etag"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["analysis_restored"] == {"plz_parameters": True, "clustering_parameters": True}
    assert fingerprint(conn, gid, plz=plz) == before  # bitwise: network, loads, zones, buildings, analysis rows
    again = client.post(f"/api/load-edits/{out['load_edit_id']}/undo", json={})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_undone"


def test_cross_grid_analysis_restore(client, conn):
    undo_everything(client, conn)
    # two grids of one PLZ, each with a building of several households
    rows = q(conn, """SELECT g.grid_result_id, g.plz FROM pylovo.grid_result g
                      WHERE g.version_id = '1' AND EXISTS (SELECT 1 FROM pylovo.buildings_result b
                            WHERE b.grid_result_id = g.grid_result_id AND b.households > 1)
                      ORDER BY g.plz, g.grid_result_id""")
    pairs = [(a, b) for a, b in zip(rows, rows[1:]) if a[1] == b[1]]
    if not pairs:
        pytest.skip("no PLZ with two grids that have a multi-household building")
    (g1, plz), (g2, _) = pairs[0]
    before1, before2 = fingerprint(conn, g1, plz=plz), fingerprint(conn, g2, plz=plz)
    b1 = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households > 1 ORDER BY objectid LIMIT 1", (g1,))[0][0]
    b2 = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households > 1 ORDER BY objectid LIMIT 1", (g2,))[0][0]
    e1 = apply(client, g1, b1, {"households": 1}).json()
    e2 = apply(client, g2, b2, {"households": 1}).json()
    assert e2["removed_analysis"]["plz_parameters"] is False  # already removed by the first edit
    u1 = client.post(f"/api/load-edits/{e1['load_edit_id']}/undo", json={}).json()
    assert u1["analysis_restored"]["clustering_parameters"] and not u1["analysis_restored"]["plz_parameters"]
    u2 = client.post(f"/api/load-edits/{e2['load_edit_id']}/undo", json={}).json()
    assert u2["analysis_restored"]["clustering_parameters"] and u2["analysis_restored"]["plz_parameters"]
    assert fingerprint(conn, g1, plz=plz) == before1 and fingerprint(conn, g2, plz=plz) == before2


def test_residential_only_version_nonresidential_edit(client, conn, picks):
    undo_everything(client, conn)
    mixed = picks["v3_mixed"]
    if mixed is None:
        pytest.skip("version 3 has no building with a residential and a large commercial part")
    gid, plz, peak = q(conn, "SELECT b.grid_result_id, g.plz, b.peak_load_in_kw FROM pylovo.buildings_result b "
                             "JOIN pylovo.grid_result g USING (grid_result_id) WHERE b.version_id='3' AND b.objectid=%s",
                       (mixed,))[0]
    before = fingerprint(conn, gid, version="3", plz=plz)
    ctx = context(client, gid, mixed)
    assert ctx["parameters"]["residential_only"] and ctx["building"]["peak_load_in_kw"] == peak
    assert peak == ctx["building"]["households"] * ctx["parameters"]["residential_peak_kw"]  # the residential part only
    p = client.post(f"/api/grids/{gid}/load-edit/preview",
                    json={"objectid": mixed, "changes": {"nonresidential_floor_area": 200.0}}).json()
    assert p["valid"] and any(w["code"] == "residential_only_nonres" for w in p["warnings"])
    assert p["building"]["after"]["peaks"]["peak_load_in_kw"] == peak
    edited = {load["name"]: load for load in p["loads"]["edited"]}
    commercial = [n for n in edited if n.endswith("Commercial")]
    assert commercial and edited[commercial[0]]["p_kw"][1] < edited[commercial[0]]["p_kw"][0]
    res = apply(client, gid, mixed, {"nonresidential_floor_area": 200.0})
    assert res.status_code == 201, res.text
    assert client.post(f"/api/load-edits/{res.json()['load_edit_id']}/undo", json={}).status_code == 200
    assert fingerprint(conn, gid, version="3", plz=plz) == before


def test_undo_all_and_revert(client, conn, picks):
    undo_everything(client, conn)
    gid, building = picks["gid"], picks["building"]
    before = fingerprint(conn, gid, plz=picks["plz"])
    other = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households = 1 AND "
                    "residential_floor_area > 0 ORDER BY objectid LIMIT 1", (gid,))[0][0]
    for objectid, households in ((building, 1), (other, 3), (building, picks["households"] + 2)):
        assert apply(client, gid, objectid, {"households": households}).status_code == 201
    history = client.get(f"/api/grids/{gid}/load-edits").json()
    assert [h["undoable"] for h in history[:3]] == [True, False, False]
    blocked = client.post(f"/api/load-edits/{history[1]['load_edit_id']}/undo", json={})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "not_latest"
    res = client.post(f"/api/grids/{gid}/load-edits/undo-all", json={}).json()
    assert len(res["undone"]) == 3 and res["net_equals_generated"]
    assert fingerprint(conn, gid, plz=picks["plz"]) == before
    # revert (non-LIFO): generated inputs again, loads equal the generated ones, JSON not byte-identical
    apply(client, gid, building, {"households": picks["households"] + 1})
    apply(client, gid, other, {"households": 2})
    ctx = context(client, gid, building)
    rev = client.post(f"/api/grids/{gid}/load-edit/revert", json={"objectid": building, "if_match": ctx["etag"]})
    assert rev.status_code == 201, rev.text
    assert q(conn, "SELECT households FROM pylovo.buildings_result WHERE version_id='1' AND objectid=%s",
             (building,))[0][0] == picks["households"]
    not_edited = client.post(f"/api/grids/{gid}/load-edit/revert", json={"objectid": building, "if_match": rev.json()["etag"]})
    assert not_edited.status_code == 409 and not_edited.json()["detail"]["code"] == "not_edited"
    assert client.post(f"/api/grids/{gid}/load-edits/undo-all", json={}).json()["net_equals_generated"]
    assert fingerprint(conn, gid, plz=picks["plz"]) == before


def test_conflicts(client, conn, picks):
    undo_everything(client, conn)
    gid, building = picks["gid"], picks["building"]
    ctx = context(client, gid, building)
    stale = client.post(f"/api/grids/{gid}/load-edits", json={"objectid": building, "changes": {"households": 3},
                                                               "if_match": "0" * 32 + "-0", "first_version_edit_confirm": "1"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "etag_mismatch"
    # another transaction holds the grid row: NOWAIT -> 409 locked, without waiting
    from pylovo import config_loader as cl

    other = psycopg2.connect(dbname=cl.DBNAME, user=cl.DBUSER, password=cl.PASSWORD, host=cl.HOST, port=cl.PORT)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT 1 FROM pylovo.grid_result WHERE grid_result_id=%s FOR UPDATE", (gid,))
            started = time.time()
            locked = client.post(f"/api/grids/{gid}/load-edits", json={"objectid": building, "changes": {"households": 3},
                                                                        "if_match": ctx["etag"], "first_version_edit_confirm": "1"})
            assert locked.status_code == 409 and locked.json()["detail"]["code"] == "locked"
            assert time.time() - started < 10
    finally:
        other.rollback()
        other.close()
    # a conflicting job blocks writes, but not previews
    from pylovo_api.jobs import Job

    jobs = client.app.state.jobs
    jobs._jobs["fake"] = Job(id="fake", kind="delete", title="Delete version 9", argv=[], writes_db=True, status="running")
    try:
        blocked = client.post(f"/api/grids/{gid}/load-edits", json={"objectid": building, "changes": {"households": 3},
                                                                     "if_match": ctx["etag"], "first_version_edit_confirm": "1"})
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "writer_job"
        previewed = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": building, "changes": {"households": 3}})
        assert previewed.status_code == 200
    finally:
        jobs._jobs.pop("fake")
    # the first edit of a version needs the typed version id (only while the version has no edits)
    if not q(conn, "SELECT EXISTS (SELECT 1 FROM pylovo.load_edit WHERE version_id='2')")[0][0]:
        g2, b = q(conn, "SELECT grid_result_id, objectid FROM pylovo.buildings_result WHERE version_id='2' AND households > 1 "
                        "ORDER BY grid_result_id, objectid LIMIT 1")[0]
        c2 = context(client, g2, b)
        need = client.post(f"/api/grids/{g2}/load-edits", json={"objectid": b, "changes": {"households": 1}, "if_match": c2["etag"]})
        assert need.status_code == 409 and need.json()["detail"]["code"] == "ack_required"
        assert "first_version_edit" in need.json()["detail"]["acknowledge"]


def test_non_convergence_needs_acknowledgement(client, conn, picks):
    undo_everything(client, conn)
    gid, building = picks["gid"], picks["building"]
    before = fingerprint(conn, gid, plz=picks["plz"])
    p = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": building, "changes": {"households": 5000}}).json()
    if p["power_flow"]["status"][1] != "not_converged":
        pytest.skip("5000 households still converge in this grid")
    res = apply(client, gid, building, {"households": 5000})
    assert res.status_code == 409 and res.json()["detail"]["acknowledge"] == ["non_convergence"]
    res = apply(client, gid, building, {"households": 5000}, acknowledge=["non_convergence"])
    assert res.status_code == 201, res.text
    row = q(conn, "SELECT power_flow_status, max_total_lv_voltage_drop_pu, "
                  "(grid::jsonb->'_object'->'res_bus'->>'_object') FROM pylovo.grid_result WHERE grid_result_id=%s", (gid,))[0]
    assert row[0] == "not_converged" and row[1] is None
    import json

    res_bus = json.loads(row[2])
    assert all(v is None for r in (res_bus.get("data") or []) for v in r)  # no stale results from the loaded JSON
    assert client.post(f"/api/load-edits/{res.json()['load_edit_id']}/undo", json={}).status_code == 200
    assert fingerprint(conn, gid, plz=picks["plz"]) == before


def test_building_view_is_current_without_refresh(client, conn, picks):
    undo_everything(client, conn)
    gid, building = picks["gid"], picks["building"]
    edit = apply(client, gid, building, {"households": picks["households"] + 3}).json()
    info = client.get("/api/status").json()["load_edit"]
    assert info["views_stale"] == 0 and not info["refreshing_views"]
    assert q(conn, "SELECT households FROM pylovo.buildings_result_with_grid "
                   "WHERE version_id=%s AND objectid=%s", ("1", building))[0][0] == picks["households"] + 3
    response = client.post("/api/maintenance/refresh-views", json={})
    assert response.status_code == 202 and response.json() == {"started": False, "current": True}
    client.post(f"/api/load-edits/{edit['load_edit_id']}/undo", json={})


def test_schema_creation_is_idempotent(conn):
    from pylovo_api.load_edit_service import edit_db

    with edit_db(readonly=False) as dbx:
        dbx.ensure_load_edit_schema()
        dbx.ensure_load_edit_schema()
        assert dbx.load_edit_table_exists()
    from pylovo.database.config_table_structure import CREATE_QUERIES

    keys = list(CREATE_QUERIES)
    assert keys.index("load_edit") == keys.index("pandapower_load") + 1
