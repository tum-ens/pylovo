"""API tests against a sandbox database (opt-in, see conftest.py)."""
from __future__ import annotations

from conftest import requires_db

pytestmark = requires_db


def test_status(client):
    status = client.get("/api/status").json()
    assert status["connected"] and status["schema_exists"]
    assert status["config"]["version_id"]


def test_regions(client):
    overview = client.get("/api/regions/overview").json()
    assert overview["postcodes"] >= 1
    plz = client.get("/api/regions/search", params={"q": str(overview["generated"][0] if overview["generated"] else "8")}).json()
    assert plz
    fc = client.get("/api/regions/postcodes", params={"bbox": ",".join(map(str, overview["bounds"])), "zoom": 12}).json()
    assert fc["type"] == "FeatureCollection" and fc["features"]
    detail = client.get(f"/api/regions/{fc['features'][0]['properties']['plz']}").json()
    assert detail["available"] and len(detail["bounds"]) == 4


def test_transformer_edit_roundtrip(client):
    plz = client.get("/api/regions/overview").json()
    region = client.get(f"/api/regions/{client.get('/api/regions/postcodes', params={'bbox': ','.join(map(str, plz['bounds'])), 'zoom': 12}).json()['features'][0]['properties']['plz']}").json()
    lon = (region["bounds"][0] + region["bounds"][2]) / 2
    lat = (region["bounds"][1] + region["bounds"][3]) / 2
    created = client.post("/api/transformers", json={"plz": region["plz"], "lon": lon, "lat": lat, "transformer_rated_power": 250})
    assert created.status_code == 201
    osm_id = created.json()["osm_id"]
    try:
        listed = client.get("/api/transformers", params={"plz": region["plz"]}).json()
        mine = [f for f in listed["features"] if f["properties"]["osm_id"] == osm_id]
        assert mine and mine[0]["properties"]["source"] == "manual" and mine[0]["properties"]["transformer_rated_power"] == 250
        assert client.patch(f"/api/transformers/{osm_id}", json={"transformer_rated_power": 400}).status_code == 200
    finally:
        assert client.delete(f"/api/transformers/{osm_id}").status_code == 200
    assert client.delete(f"/api/transformers/{osm_id}").status_code == 404


def test_results_grid_and_powerflow(client):
    versions = client.get("/api/versions").json()
    if not versions or not versions[0]["plz"]:
        import pytest
        pytest.skip("no generated grids in the sandbox")
    v = versions[0]
    summary = client.get(f"/api/results/{v['version_id']}/summary").json()
    assert summary["kpis"]["grids"] == v["grid_count"]
    overview = client.get(f"/api/results/{v['version_id']}/{v['plz'][0]}/map").json()
    assert overview["lines"]["features"] and overview["stations"]["features"]
    gid = summary["grids"][0]["grid_result_id"]
    detail = client.get(f"/api/grids/{gid}").json()
    assert detail["feeders"] and detail["buses"]["features"]
    roles = {f["properties"]["role"] for f in detail["lines"]["features"]}
    assert {"feeder", "service"} <= roles
    pp_json = client.get(f"/api/grids/{gid}/pandapower.json")
    assert pp_json.status_code == 200 and pp_json.headers["content-disposition"].startswith("attachment")
    pf = client.post(f"/api/grids/{gid}/powerflow").json()
    assert pf["converged"] and 0.8 < pf["min_vm_pu"] <= 1.05 and pf["profile"]
    check = client.get("/api/jobs/generate/check", params={"plz": v["plz"][0]}).json()
    assert check["regions"][0]["plz"] == v["plz"][0]
