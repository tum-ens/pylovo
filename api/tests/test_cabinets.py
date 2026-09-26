"""Cable cabinets (feeder split points): one naming rule for inspector, map and diagnostics."""
from __future__ import annotations

import pytest
from conftest import requires_db
from pylovo_api.cabinets import connection_vertices, number_cabinets, vertex_of


def test_numbering_by_cable_distance_without_the_station():
    buses = [{"pp_index": 0, "name": "LVbus 1", "role": "lv_busbar"}, {"pp_index": 2, "name": "Connection Nodebus 377", "role": "connection"},
             {"pp_index": 5, "name": "Connection Nodebus 40", "role": "connection"},
             {"pp_index": 7, "name": "Connection Nodebus 41", "role": "connection"},
             {"pp_index": 9, "name": "Consumer Nodebus 42", "role": "consumer"}]
    vb = connection_vertices(buses)
    assert vb == {377: 2, 40: 5, 41: 7} and vertex_of("Connection Nodebus 371") == 371
    splits = [{"split_bus": 41, "outgoing_count": 3}, {"split_bus": 40, "outgoing_count": 2},
              {"split_bus": 377, "outgoing_count": 4}, {"split_bus": 42, "outgoing_count": 2}]
    out = number_cabinets(splits, vb, station={2}, distance_m={5: 120.0, 7: 80.04, 2: 0.0})
    assert [(c["name"], c["split_bus"], c["outgoing"]) for c in out] == [("K1", 41, 3), ("K2", 40, 2)]
    assert out[0]["distance_m"] == 80.0      # station bus 377 and the consumer vertex 42 are no cabinets


@requires_db
def test_cabinet_names_match_the_diagnostics(client):
    versions = client.get("/api/versions").json()
    if not versions:
        pytest.skip("no generated grids in the sandbox")
    checked = 0
    for v in versions:
        for g in client.get(f"/api/results/{v['version_id']}/summary").json()["grids"]:
            detail = client.get(f"/api/grids/{g['grid_result_id']}").json()
            diag = client.get(f"/api/grids/{g['grid_result_id']}/diagnostics").json()
            mine = [(c["name"], c["split_bus"], c["outgoing"]) for c in detail["cabinets"]]
            assert mine == [(c["name"], c["split_bus"], c["outgoing"]) for c in diag["cabinets"]]
            assert not {c["bus"] for c in detail["cabinets"]} & set(detail["station_buses"])
            checked += len(mine)
    assert checked > 0
