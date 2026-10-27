"""The on-demand power flow without a database: the key of its cache, the wait for a slot, the download.

The stored net is a three-bus pandapower net, ``queries.grid_json`` is replaced by a function returning it.
"""
from __future__ import annotations

import threading

import pytest
from pylovo_api import diagnostics_data, powerflow, queries

IDENT = {"version_id": "1", "plz": 80803, "kcid": 2, "bcid": -3, "fingerprint": "md5-of-the-stored-net"}


def stored_net() -> str:
    import pandapower as pp

    net = pp.create_empty_network(name="three buses")
    mv = pp.create_bus(net, 10.0, name="MVbus 1")
    lv = pp.create_bus(net, 0.4, name="LVbus 1")
    consumer = pp.create_bus(net, 0.4, name="Consumer Nodebus 1")
    pp.create_ext_grid(net, mv)
    pp.create_transformer_from_parameters(net, mv, lv, sn_mva=0.4, vn_hv_kv=10.0, vn_lv_kv=0.4, vk_percent=4.0,
                                          vkr_percent=1.0, pfe_kw=0.5, i0_percent=0.1)
    pp.create_line_from_parameters(net, lv, consumer, length_km=0.1, r_ohm_per_km=0.2, x_ohm_per_km=0.08,
                                   c_nf_per_km=0.0, max_i_ka=0.3)
    pp.create_load(net, consumer, p_mw=0.01)
    return pp.to_json(net)


@pytest.fixture()
def stored(monkeypatch):
    text = stored_net()
    monkeypatch.setattr(queries, "grid_json", lambda grid_result_id: (text, dict(IDENT)))
    return text


def test_the_result_carries_the_fingerprint_that_was_read_with_the_net(stored):
    result = powerflow.run(7)
    assert result["converged"] and result["fingerprint"] == IDENT["fingerprint"] and result["kcid"] == 2
    assert 0.9 < result["min_vm_pu"] < 1.0


def test_the_diagnostics_cache_key_is_not_read_again_after_the_power_flow(stored, monkeypatch):
    """A load edit that commits while the power flow runs must not get the results of the net it replaced."""
    monkeypatch.setattr(diagnostics_data, "grid_diagnostics", lambda *args, **kwargs: {"findings": []})
    result = powerflow.run(7)
    assert diagnostics_data.after_powerflow(result) == {"findings": []}  # no database is needed to file the result
    assert diagnostics_data.cached_powerflow(7, 1.0, IDENT["fingerprint"]) is result
    assert diagnostics_data.cached_powerflow(7, 1.0, "md5-of-an-edited-net") is None


def test_a_request_that_finds_no_free_slot_is_refused_with_503(client, stored, monkeypatch):
    monkeypatch.setattr(powerflow, "_PF_LOCK", threading.Semaphore(0))
    monkeypatch.setattr(powerflow, "SLOT_WAIT_S", 0.05)
    with pytest.raises(powerflow.Busy):
        powerflow.run(7)
    res = client.post("/api/grids/7/powerflow")
    assert res.status_code == 503 and res.headers["retry-after"] == "5" and "busy" in res.json()["detail"]


def test_a_free_slot_is_released_after_the_run(stored):
    powerflow.run(7)
    powerflow.run(7)
    powerflow.run(7)
    assert powerflow._PF_LOCK.acquire(blocking=False) and powerflow._PF_LOCK.acquire(blocking=False)
    powerflow._PF_LOCK.release()
    powerflow._PF_LOCK.release()


def test_the_download_is_the_stored_text_and_not_a_serialisation_of_it(client, monkeypatch):
    text = '{"_module": "pandapower.auxiliary",   "_object": {"x": [1.0E-5, 2]}}\n'
    monkeypatch.setattr(queries, "grid_json", lambda grid_result_id: (text, dict(IDENT)))
    res = client.get("/api/grids/7/pandapower.json")
    assert res.status_code == 200 and res.text == text
    assert res.headers["content-disposition"] == 'attachment; filename="pylovo_v1_80803_k2_b-3.json"'


def test_a_grid_of_the_opendss_backend_is_reported_not_solved(monkeypatch):
    altdss_json = '{"$schema": "https://dss-extensions.org/altdss-schema/2023-12-13.schema.json", "Name": "plz1"}'
    monkeypatch.setattr(queries, "grid_json", lambda grid_result_id: (altdss_json, dict(IDENT)))
    result = powerflow.run(7)
    assert not result["converged"] and "OpenDSS backend" in result["error"]
