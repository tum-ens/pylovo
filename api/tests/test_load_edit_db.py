"""Load editing against a sandbox database (opt-in, see conftest.py).

WRITES: the round-trip tests apply load edits and undo them again, so the grids end byte-identical
to generation, but audit rows (``pylovo.load_edit``, all undone) and the table itself remain. Only
run it against a throwaway copy of the sandbox (``pylovo_api_template``: PLZ 85653, versions 1-3).

The first tests are the equivalence proof of the replicated generation code: for every stored grid
the loads rebuilt from ``buildings_result`` and the version snapshot equal the stored loads, the
rebuilt network JSON is byte-identical, and the validation power flow reproduces the stored status
and voltage drops.
"""
from __future__ import annotations

import time

import psycopg2
import pytest
from conftest import requires_db

pytestmark = requires_db

GRID_V1 = (1, 1, 3)          # version, kcid, bcid of the 630 kVA grid with a voltage violation
BUILDING = "DEMO_OSM_1076061690"  # AB, Obere Dorfstraße 7, 10 households, 168.25 kW
V3_MIXED = "DEMO_OSM_234352121"   # v3 (residential-only) mixed AB with a Commercial part


@pytest.fixture(scope="module")
def conn():
    from pylovo import config_loader as cl

    c = psycopg2.connect(dbname=cl.DBNAME, user=cl.DBUSER, password=cl.PASSWORD, host=cl.HOST, port=cl.PORT,
                         options="-c search_path=pylovo,public -c extra_float_digits=3")
    c.autocommit = True
    yield c
    c.close()


def q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else None


def grid_id(conn, version="1", kcid=1, bcid=3, plz=None):
    return q(conn, "SELECT grid_result_id FROM pylovo.grid_result WHERE version_id=%s AND kcid=%s AND bcid=%s", (version, kcid, bcid))[0][0]


def fingerprint(conn, gid, version="1", plz=85653):
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


def context(client, gid, objectid):
    res = client.get(f"/api/grids/{gid}/load-edit/building", params={"objectid": objectid})
    assert res.status_code == 200, res.text
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
    assert len(ids) >= 27
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
def test_context_and_preview_are_read_only(client, conn):
    undo_everything(client, conn)
    gid = grid_id(conn)
    before = fingerprint(conn, gid)
    ctx = context(client, gid, BUILDING)
    assert ctx["building"]["households"] == 10 and ctx["checks"]["reproduction"]["tier"] == "exact"
    assert ctx["parameters"]["residential_peak_kw"] == 16.825
    for households in (4, 9, 11):
        res = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": BUILDING, "changes": {"households": households}})
        assert res.status_code == 200 and res.json()["valid"], res.text
    p = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": BUILDING, "changes": {"households": 4}}).json()
    assert p["building"]["after"]["peaks"]["peak_load_in_kw"] == 4 * 16.825
    assert p["loads"]["changed"] == 159 and p["loads"]["total"] == 171
    assert round(p["grid"]["coincident_kw"][0], 2) == 473.80 and round(p["grid"]["coincident_kw"][1], 2) == 466.37
    assert round(p["power_flow"]["min_vm_pu"][0], 4) == 0.8833 and round(p["power_flow"]["min_vm_pu"][1], 4) == 0.8868
    assert p["power_flow"]["status"] == ["voltage_violation", "voltage_violation"]
    invalid = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": BUILDING, "changes": {"households": 0}}).json()
    assert not invalid["valid"] and invalid["errors"][0]["field"] == "households"
    missing = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": "nope", "changes": {"households": 3}})
    assert missing.status_code == 404
    assert fingerprint(conn, gid) == before


# --------------------------------------------------------------------------- write paths
def test_apply_and_undo_round_trip(client, conn):
    undo_everything(client, conn)
    gid = grid_id(conn)
    before = fingerprint(conn, gid)
    assert before["plz_parameters"] and before["clustering"]
    res = apply(client, gid, BUILDING, {"households": 4}, reason="=cmd audit test")
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["removed_analysis"] == {"plz_parameters": True, "clustering_parameters": True, "grid_parameters": False}
    assert out["grid"]["load_edit"]["modified"] and out["grid"]["load_edit"]["edited_buildings"] == 1
    after = fingerprint(conn, gid)
    assert after["net"][0] != before["net"][0] and after["loads"] != before["loads"]
    assert not after["plz_parameters"] and not after["clustering"]
    row = q(conn, "SELECT households, peak_load_in_kw FROM pylovo.buildings_result WHERE version_id='1' AND objectid=%s", (BUILDING,))[0]
    assert row == (4, 4 * 16.825)
    edit = q(conn, "SELECT after_net_md5, before_net_md5, jsonb_array_length(before_loads), changes FROM pylovo.load_edit "
                   "WHERE load_edit_id=%s", (out["load_edit_id"],))[0]
    assert edit[0] == after["net"][0] and edit[1] == before["net"][0] and edit[2] == 171
    assert edit[3] == {"households": [10, 4]}
    changed = q(conn, """SELECT count(*) FROM pylovo.pandapower_load l
                         JOIN jsonb_populate_recordset(NULL::pylovo.pandapower_load,
                              (SELECT before_loads FROM pylovo.load_edit WHERE load_edit_id=%s)) b USING (pp_index)
                         WHERE l.grid_result_id=%s AND l.p_mw <> b.p_mw""", (out["load_edit_id"], gid))[0][0]
    assert changed == 159
    status = client.get("/api/status").json()["load_edit"]
    assert status["schema"] == "ok" and status["views_stale"] == 0
    csv = client.get("/api/versions/1/load-edits", params={"format": "csv"}).text
    assert "'=cmd audit test" in csv
    undo = client.post(f"/api/load-edits/{out['load_edit_id']}/undo", json={"if_match": out["etag"]})
    assert undo.status_code == 200, undo.text
    assert undo.json()["analysis_restored"] == {"plz_parameters": True, "clustering_parameters": True, "grid_parameters": False}
    assert fingerprint(conn, gid) == before  # bitwise: network, loads, zones, buildings, analysis rows
    again = client.post(f"/api/load-edits/{out['load_edit_id']}/undo", json={})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "already_undone"


def test_cross_grid_analysis_restore(client, conn):
    undo_everything(client, conn)
    g1, g2 = grid_id(conn, "1", 1, 1), grid_id(conn, "1", 1, 2)
    before1, before2 = fingerprint(conn, g1), fingerprint(conn, g2)
    b1 = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households > 1 ORDER BY objectid LIMIT 1", (g1,))[0][0]
    b2 = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households > 1 ORDER BY objectid LIMIT 1", (g2,))[0][0]
    e1 = apply(client, g1, b1, {"households": 1}).json()
    e2 = apply(client, g2, b2, {"households": 1}).json()
    assert e2["removed_analysis"]["plz_parameters"] is False  # already removed by the first edit
    u1 = client.post(f"/api/load-edits/{e1['load_edit_id']}/undo", json={}).json()
    assert u1["analysis_restored"]["clustering_parameters"] and not u1["analysis_restored"]["plz_parameters"]
    u2 = client.post(f"/api/load-edits/{e2['load_edit_id']}/undo", json={}).json()
    assert u2["analysis_restored"]["clustering_parameters"] and u2["analysis_restored"]["plz_parameters"]
    assert fingerprint(conn, g1) == before1 and fingerprint(conn, g2) == before2


def test_residential_only_version_nonresidential_edit(client, conn):
    undo_everything(client, conn)
    gid = q(conn, "SELECT grid_result_id FROM pylovo.buildings_result WHERE version_id='3' AND objectid=%s", (V3_MIXED,))[0][0]
    before = fingerprint(conn, gid, version="3")
    ctx = context(client, gid, V3_MIXED)
    assert ctx["parameters"]["residential_only"] and ctx["building"]["peak_load_in_kw"] == 168.25
    p = client.post(f"/api/grids/{gid}/load-edit/preview",
                    json={"objectid": V3_MIXED, "changes": {"nonresidential_floor_area": 200.0}}).json()
    assert p["valid"] and any(w["code"] == "residential_only_nonres" for w in p["warnings"])
    assert p["building"]["after"]["peaks"]["peak_load_in_kw"] == 168.25
    edited = {load["name"]: load for load in p["loads"]["edited"]}
    commercial = [n for n in edited if n.endswith("Commercial")]
    assert commercial and edited[commercial[0]]["p_kw"][1] < edited[commercial[0]]["p_kw"][0]
    res = apply(client, gid, V3_MIXED, {"nonresidential_floor_area": 200.0})
    assert res.status_code == 201, res.text
    assert client.post(f"/api/load-edits/{res.json()['load_edit_id']}/undo", json={}).status_code == 200
    assert fingerprint(conn, gid, version="3") == before


def test_undo_all_and_revert(client, conn):
    undo_everything(client, conn)
    gid = grid_id(conn)
    before = fingerprint(conn, gid)
    other = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households = 1 AND "
                    "residential_floor_area > 0 ORDER BY objectid LIMIT 1", (gid,))[0][0]
    for objectid, households in ((BUILDING, 4), (other, 3), (BUILDING, 7)):
        assert apply(client, gid, objectid, {"households": households}).status_code == 201
    history = client.get(f"/api/grids/{gid}/load-edits").json()
    assert [h["undoable"] for h in history[:3]] == [True, False, False]
    blocked = client.post(f"/api/load-edits/{history[1]['load_edit_id']}/undo", json={})
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "not_latest"
    res = client.post(f"/api/grids/{gid}/load-edits/undo-all", json={}).json()
    assert len(res["undone"]) == 3 and res["net_equals_generated"]
    assert fingerprint(conn, gid) == before
    # revert (non-LIFO): generated inputs again, loads equal the generated ones, JSON not byte-identical
    apply(client, gid, BUILDING, {"households": 5})
    apply(client, gid, other, {"households": 2})
    ctx = context(client, gid, BUILDING)
    rev = client.post(f"/api/grids/{gid}/load-edit/revert", json={"objectid": BUILDING, "if_match": ctx["etag"]})
    assert rev.status_code == 201, rev.text
    assert q(conn, "SELECT households FROM pylovo.buildings_result WHERE version_id='1' AND objectid=%s", (BUILDING,))[0][0] == 10
    not_edited = client.post(f"/api/grids/{gid}/load-edit/revert", json={"objectid": BUILDING, "if_match": rev.json()["etag"]})
    assert not_edited.status_code == 409 and not_edited.json()["detail"]["code"] == "not_edited"
    assert client.post(f"/api/grids/{gid}/load-edits/undo-all", json={}).json()["net_equals_generated"]
    assert fingerprint(conn, gid) == before


def test_conflicts(client, conn):
    undo_everything(client, conn)
    gid = grid_id(conn)
    ctx = context(client, gid, BUILDING)
    stale = client.post(f"/api/grids/{gid}/load-edits", json={"objectid": BUILDING, "changes": {"households": 3},
                                                               "if_match": "0" * 32 + "-0", "first_version_edit_confirm": "1"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "etag_mismatch"
    # another transaction holds the grid row: NOWAIT -> 409 locked, without waiting
    from pylovo import config_loader as cl

    other = psycopg2.connect(dbname=cl.DBNAME, user=cl.DBUSER, password=cl.PASSWORD, host=cl.HOST, port=cl.PORT)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT 1 FROM pylovo.grid_result WHERE grid_result_id=%s FOR UPDATE", (gid,))
            started = time.time()
            locked = client.post(f"/api/grids/{gid}/load-edits", json={"objectid": BUILDING, "changes": {"households": 3},
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
        blocked = client.post(f"/api/grids/{gid}/load-edits", json={"objectid": BUILDING, "changes": {"households": 3},
                                                                     "if_match": ctx["etag"], "first_version_edit_confirm": "1"})
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "writer_job"
        preview = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": BUILDING, "changes": {"households": 3}})
        assert preview.status_code == 200
    finally:
        jobs._jobs.pop("fake")
    # the first edit of a version needs the typed version id (only while the version has no edits)
    if not q(conn, "SELECT EXISTS (SELECT 1 FROM pylovo.load_edit WHERE version_id='2')")[0][0]:
        g2 = grid_id(conn, "2", 1, -1)
        b = q(conn, "SELECT objectid FROM pylovo.buildings_result WHERE grid_result_id=%s AND households > 1 LIMIT 1", (g2,))[0][0]
        c2 = context(client, g2, b)
        need = client.post(f"/api/grids/{g2}/load-edits", json={"objectid": b, "changes": {"households": 1}, "if_match": c2["etag"]})
        assert need.status_code == 409 and need.json()["detail"]["code"] == "ack_required"
        assert "first_version_edit" in need.json()["detail"]["acknowledge"]


def test_non_convergence_needs_acknowledgement(client, conn):
    undo_everything(client, conn)
    gid = grid_id(conn)
    before = fingerprint(conn, gid)
    p = client.post(f"/api/grids/{gid}/load-edit/preview", json={"objectid": BUILDING, "changes": {"households": 5000}}).json()
    if p["power_flow"]["status"][1] != "not_converged":
        pytest.skip("5000 households still converge in this grid")
    res = apply(client, gid, BUILDING, {"households": 5000})
    assert res.status_code == 409 and res.json()["detail"]["acknowledge"] == ["non_convergence"]
    res = apply(client, gid, BUILDING, {"households": 5000}, acknowledge=["non_convergence"])
    assert res.status_code == 201, res.text
    row = q(conn, "SELECT power_flow_status, max_total_lv_voltage_drop_pu, "
                  "(grid::jsonb->'_object'->'res_bus'->>'_object') FROM pylovo.grid_result WHERE grid_result_id=%s", (gid,))[0]
    assert row[0] == "not_converged" and row[1] is None
    import json

    res_bus = json.loads(row[2])
    assert all(v is None for r in (res_bus.get("data") or []) for v in r)  # no stale results from the loaded JSON
    assert client.post(f"/api/load-edits/{res.json()['load_edit_id']}/undo", json={}).status_code == 200
    assert fingerprint(conn, gid) == before


def test_building_view_is_current_without_refresh(client, conn):
    undo_everything(client, conn)
    gid = grid_id(conn)
    edit = apply(client, gid, BUILDING, {"households": 8}).json()
    info = client.get("/api/status").json()["load_edit"]
    assert info["views_stale"] == 0 and not info["refreshing_views"]
    assert q(conn, "SELECT households FROM pylovo.buildings_result_with_grid "
                   "WHERE version_id=%s AND objectid=%s", ("1", BUILDING))[0][0] == 8
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
