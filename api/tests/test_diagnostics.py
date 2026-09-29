"""Grid diagnostics: rules, attribution and endpoints.

The unit tests run without a database on a fixture exported from the sandbox (OSM demo region
PLZ 85653; derived from OpenStreetMap data, (c) OpenStreetMap contributors, ODbL 1.0):

* grid 3 (version 1, 630 kVA): the voltage and overload case (0.883 p.u., S8 at 100.1 %);
* grid 1 (version 1, 400 kVA): one dominant load at the feeder end (Schieferweg 29);
* grid 25 (version 3, 100 kVA): a grid without problems.

The endpoint tests at the end need the opt-in sandbox database (see conftest.py).
"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import pytest
from conftest import requires_db
from pylovo_api import diagnostics as D

FIXTURE = Path(__file__).parent / "fixtures" / "diagnostics_sandbox.json.gz"


@pytest.fixture(scope="module")
def sandbox() -> dict:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    for version in data["versions"].values():
        version["context"]["sections"] = [tuple(row) for row in version["context"]["sections"]]
    return data


def run(sandbox: dict, gid: int, with_pf: bool = False, **overrides) -> dict:
    grid = sandbox["grids"][str(gid)]
    version = sandbox["versions"][grid["inputs"]["grid"]["version_id"]]
    return D.diagnose(grid["inputs"], version["gp"], pf=grid["pf"] if with_pf else None, context=version["context"],
                      thresholds=overrides or None)


def by_id(result: dict) -> dict[str, dict]:
    return {f["id"]: f for f in result["findings"]}


# --------------------------------------------------------------------------- building blocks
def test_household_capacity_and_design_current_follow_pylovo(sandbox):
    from pylovo.utils import category_simultaneous_load

    params = D.Params(sandbox["versions"]["1"]["gp"])
    assert [params.household_capacity(i) for i in (270, 313, 357, 425)] == [108, 130, 152, 188]
    cats = {"Residential": [16.825 * 40, 40.0], "Commercial": [120.0, 2.0]}
    expected = (category_simultaneous_load(16.825 * 40, 40, 0.07) + category_simultaneous_load(120.0, 2, 0.5))
    assert params.coincident_kw(cats) == pytest.approx(expected)
    assert params.design_current_a(100.0) == pytest.approx(100_000 / (math.sqrt(3) * 400 * 0.95))


def test_design_currents_reproduce_pylovos_cable_sizing(sandbox):
    """The replicated design current never exceeds the rating pylovo chose, except where it was tight."""
    grid = sandbox["grids"]["3"]
    model = D.GridModel(grid["inputs"], D.Params(sandbox["versions"]["1"]["gp"]))
    head = model.lines[model.feeders[1]["head"]]
    assert head["I_d"] == pytest.approx(658, abs=1)  # 2 x NAYY_4_300 at the station exit
    for sec in model.sections.values():
        assert sec["u_d"] <= 1.0 + 1e-6  # pylovo sized every section for its own design current


def test_load_contributions_add_up_to_the_linear_drop(sandbox):
    grid = sandbox["grids"]["3"]
    model = D.GridModel(grid["inputs"], D.Params(sandbox["versions"]["1"]["gp"]))
    target = max(model.consumers, key=lambda b: model.lin[b])
    rows = model.load_contributions(target)
    assert sum(r["c_lin"] for r in rows) == pytest.approx(model.lin[target], rel=1e-9)
    # The anchored replay reproduces the stored generation check at the weakest consumer.
    assert model.kappa * model.lin[target] == pytest.approx(grid["inputs"]["grid"]["max_total_drop_pct"], abs=1e-6)
    assert 1.0 < model.kappa < 1.15


def test_estimate_before_the_power_flow_is_close_to_it(sandbox):
    for gid in ("1", "3", "25"):
        grid = sandbox["grids"][gid]
        gp = sandbox["versions"][grid["inputs"]["grid"]["version_id"]]["gp"]
        before = D.GridModel(grid["inputs"], D.Params(gp))
        after = D.GridModel(grid["inputs"], D.Params(gp), grid["pf"])
        assert abs(before.dU_T - after.dU_T) < 0.2
        worst = min(before.consumers, key=lambda b: after.vm(b))
        assert abs(before.vm(worst) - after.vm(worst)) < 0.0025


# --------------------------------------------------------------------------- grid 3
def test_grid3_before_the_power_flow(sandbox):
    result = run(sandbox, 3)
    f = by_id(result)
    assert result["source"]["basis"] == "stored_check"
    vt = f["VT-01:f1"]
    assert vt["severity"] == "critical" and vt["basis"] == "stored_check" and vt["estimated"]
    assert vt["metrics"]["address"] == "Am Wagnerberg 12" and vt["metrics"]["vm_pu"] == pytest.approx(0.883, abs=0.003)
    assert f["DE-01:f1"]["severity"] == "critical"
    assert f["DE-01:f1"]["metrics"]["connections_over"] == 32 and f["DE-01:f1"]["metrics"]["catalogue_exhausted"]
    spread = f["VT-04:f1"]
    assert spread["severity"] == "critical" and spread["metrics"]["n50"] == 16
    assert spread["metrics"]["largest_share"] == pytest.approx(0.09, abs=0.01)
    assert "VT-01:f1" in spread["explains"] and "DE-01:f1" in spread["explains"]
    assert f["LD-04:f1"]["metrics"]["I_head_a"] == pytest.approx(658, abs=1)
    assert f["LD-06:f1:hh"]["metrics"]["households"] == 225 and f["LD-06:f1:hh"]["metrics"]["capacity"] == 188
    assert f["TP-01:f1"]["metrics"]["reach_m"] == pytest.approx(875, abs=2)
    assert f["TP-03:grid"]["metrics"]["eccentricity"] == pytest.approx(1.25, abs=0.02)
    assert f["TP-02:grid"]["metrics"]["share"] == pytest.approx(0.91, abs=0.01)
    assert f["DE-02:f1"]["severity"] == "warning" and len(f["DE-02:f1"]["metrics"]["sections"]) == 25
    de03 = f["DE-03:f1"]["metrics"]  # the station share of the band split (4 pp) plus the design drop
    assert de03["dU_station"] == pytest.approx(4.0) and de03["budget_pct"] == pytest.approx(13.94, abs=0.02)
    assert f["DE-05:f1"]["metrics"]["no_single_outlet"] and f["DE-05:f1"]["metrics"]["ik1_min_a"] == pytest.approx(880, abs=30)
    ld01 = f["LD-01:s8"]
    assert ld01["estimated"] and ld01["severity"] == "warning"  # at most a warning before the power flow
    assert f["LD-02:s8"]["metrics"]["u_d"] == pytest.approx(0.987, abs=0.002)
    assert f["LD-03:s8"]["metrics"]["parent"] == 2 and f["LD-03:s8"]["severity"] == "warning"
    assert f["TR-02:grid"]["metrics"]["dU_T"] == pytest.approx(2.76, abs=0.2)
    # Causes are linked to their symptom and ranked by contribution.
    assert "VT-04:f1" in vt["causes"] and "TR-02:grid" in vt["causes"]
    assert [x["severity"] for x in result["findings"]][:3] == ["critical"] * 3


def test_grid3_with_the_power_flow(sandbox):
    result = run(sandbox, 3, with_pf=True)
    f = by_id(result)
    vt = f["VT-01:f1"]
    assert vt["basis"] == "power_flow" and not vt["estimated"]
    assert vt["metrics"]["buses_below"] == 94 and vt["metrics"]["connections_below"] == 47
    assert vt["metrics"]["vm_pu"] == pytest.approx(0.8833, abs=1e-4)
    budget = result["budget"][0]
    total = sum(bar["pp"] for bar in budget["bars"])
    assert total == pytest.approx(100 * (1 - budget["vm_pu"]), abs=0.01)  # telescoping sum
    trafo = next(bar for bar in budget["bars"] if bar["key"] == "trafo")
    assert trafo["pp"] == pytest.approx(2.76, abs=0.01)
    ld01 = f["LD-01:s8"]
    assert ld01["severity"] == "critical" and not ld01["estimated"]
    m = ld01["metrics"]
    assert m["loading"] == pytest.approx(1.0012, abs=5e-4)
    assert (m["u_d"], m["coincidence"], m["voltage_loss"]) == (pytest.approx(0.987, abs=0.002),
                                                               pytest.approx(0.916, abs=0.002),
                                                               pytest.approx(1.107, abs=0.002))
    assert m["u_d"] * m["coincidence"] * m["voltage_loss"] == pytest.approx(m["loading"], abs=0.003)
    assert "LD-02:s8" in ld01["causes"] and "LD-03:s8" in ld01["causes"]
    assert "VT-01:f1" in ld01["related"]  # the current rise comes from the low voltage
    tr02 = f["TR-02:grid"]  # generated with the MV side at 1.0 p.u.: the busbar sits above the 0.96 p.u. reference
    assert tr02["severity"] == "info" and tr02["metrics"]["vm_busbar"] == pytest.approx(0.9724, abs=0.0005)
    assert tr02["metrics"]["shortfall_pp"] == pytest.approx(-1.24, abs=0.05) and tr02["contribution_pp"] == 0
    assert "tap_steps" not in tr02["metrics"] and "tap" not in budget  # no tap what-if: the tap stays neutral
    assert result["counts"]["critical"] >= 4


def test_what_if_scaling_reports_new_findings(sandbox):
    grid = sandbox["grids"]["3"]
    version = sandbox["versions"]["1"]
    base = D.diagnose(grid["inputs"], version["gp"], pf=None, context=version["context"])
    pf = dict(grid["pf"], load_scaling=1.0)
    same = D.diagnose(grid["inputs"], version["gp"], pf=pf, context=version["context"])
    diff = D.compare(same, base)
    assert "LD-01:s8" in diff["changed"]  # estimated warning -> critical in the power flow
    assert not diff["resolved"]



# --------------------------------------------------------------------------- station voltage
def _at_reference(sandbox: dict, gid: int, offset_pu: float = 0.0) -> tuple[dict, dict, dict]:
    """Inputs of a sandbox grid with the MV side as the validation power flow sets it (LV busbar at 0.96 p.u.)."""
    grid = sandbox["grids"][str(gid)]
    version = sandbox["versions"][grid["inputs"]["grid"]["version_id"]]
    model = D.GridModel(grid["inputs"], D.Params(version["gp"]))
    inputs = dict(grid["inputs"], vm_ext=0.96 + model.dU_T_est / 100 + offset_pu)
    return inputs, version["gp"], version["context"]


def test_station_at_the_reference_takes_the_mv_share_of_the_band(sandbox):
    inputs, gp, context = _at_reference(sandbox, 3)
    result = D.diagnose(inputs, gp, pf=None, context=context)
    f = by_id(result)
    tr02 = f["TR-02:grid"]
    assert tr02["severity"] == "info" and tr02["metrics"]["vm_busbar"] == pytest.approx(0.96, abs=1e-6)
    assert tr02["metrics"]["shortfall_pp"] == pytest.approx(0.0, abs=0.01)
    assert "at the 0.96 p.u. reference" in tr02["message"]
    station = next(bar for bar in result["budget"][0]["bars"] if bar["key"] == "trafo")
    assert station["pp"] == pytest.approx(4.0, abs=1e-3)
    de03 = f["DE-03:f1"]
    assert de03["metrics"]["budget_pct"] == pytest.approx(13.94, abs=0.02) and "about 5 %" in de03["remedy"]


def test_station_below_the_reference_is_a_cause(sandbox):
    inputs, gp, context = _at_reference(sandbox, 3, offset_pu=-0.03)  # as at a higher load than the stored ×1
    f = by_id(D.diagnose(inputs, gp, pf=None, context=context))
    tr02 = f["TR-02:grid"]
    assert tr02["severity"] == "warning" and tr02["metrics"]["shortfall_pp"] == pytest.approx(3.0, abs=0.01)
    assert tr02["contribution_pp"] == pytest.approx(3.0, abs=0.01) and "TR-02:grid" in f["VT-01:f1"]["causes"]


# --------------------------------------------------------------------------- grid 1 and 25
def test_grid1_dominant_load_at_the_feeder_end(sandbox):
    f = by_id(run(sandbox, 1, with_pf=True))
    assert f["VT-01:f1"]["severity"] == "warning" and f["VT-01:f1"]["metrics"]["vm_pu"] == pytest.approx(0.910, abs=0.001)
    load = f["VT-03:b139:f1"]
    assert load["metrics"]["share"] == pytest.approx(0.46, abs=0.02) and load["metrics"]["at_feeder_end"]
    assert load["severity"] == "warning" and "VT-01:f1" in load["explains"]
    data = next(x for x in f.values() if x["rule"] == "DA-01" and "Schieferweg 29" in x["title"])
    assert data["severity"] == "warning" and data["metrics"]["hall"] and data["metrics"]["estimated"]
    assert "VT-01:f1" in data["explains"]  # raised because the building drives the symptom
    assert f["VT-02:s13:f1"]["metrics"]["share"] == pytest.approx(0.37, abs=0.04)
    assert f["TR-02:grid"]["severity"] == "info"  # busbar above the reference (MV side at 1.0 p.u.)


def test_grid_without_problems_is_calm(sandbox):
    for with_pf in (False, True):
        result = run(sandbox, 25, with_pf=with_pf)
        assert result["counts"] == {"critical": 0, "warning": 0, "info": 0}
        assert all(check["status"] == "ok" for check in result["checks"])
        assert not result["budget"]


def test_overrides_change_heuristics_only(sandbox):
    f = by_id(run(sandbox, 3, long_feeder_m=1000.0, min_vm_pu=0.5))
    assert "TP-01:f1" not in f
    assert f["VT-01:f1"]["severity"] == "critical"  # pylovo limits are not overridable
    assert all(t["source"].startswith(("version parameter", "heuristic", "pylovo", "cable catalogue"))
               for t in f["VT-01:f1"]["thresholds"])


def test_table_overrides_merge_and_reach_the_formulas(sandbox):
    th = D.effective_thresholds({"grouping_factors": {2: 0.5}, "fault_conductor_factor": 1.3, "unknown": 1})
    assert th["grouping_factors"][2] == 0.5 and th["grouping_factors"][6] == 0.60 and "unknown" not in th
    assert D.DEFAULT_THRESHOLDS["grouping_factors"][2] == 0.85
    de05 = [x for x in run(sandbox, 3, fault_conductor_factor=1.3)["findings"] if x["id"].startswith("DE-05")]
    assert de05 and all("1.3 R" in x["formula"] and "1.24" not in x["formula"] for x in de05)


def test_findings_are_json_and_consistent(sandbox):
    result = run(sandbox, 3, with_pf=True)
    json.dumps(result)
    ids = {x["id"] for x in result["findings"]}
    ranks = [(D.SEV_RANK[x["severity"]]) for x in result["findings"]]
    assert ranks == sorted(ranks)
    for x in result["findings"]:
        assert set(x["explains"]) <= ids and set(x["causes"]) <= ids
        assert x["rule"] in D.RULES and x["category"] in D.CATEGORIES
        assert x["message"] and x["title"] and x["why"]
        assert all(isinstance(v, int) for v in x["targets"]["lines"] + x["targets"]["buses"])
    per_rule_symptom: dict = {}
    for x in result["findings"]:
        for sym in x["explains"]:
            per_rule_symptom.setdefault((x["rule"], sym), []).append(x["id"])
    assert max(len(v) for v in per_rule_symptom.values()) <= D.DEFAULT_THRESHOLDS["max_causes_per_rule"]


def test_robust_against_missing_data(sandbox):
    """Old versions without parameters, missing geometry or buildings and a failed power flow."""
    import copy

    grid = copy.deepcopy(sandbox["grids"]["3"]["inputs"])
    grid["buildings"], grid["splits"] = [], []
    for bus in grid["buses"]:
        bus["lon"] = bus["lat"] = None
    grid["grid"]["lon"] = grid["grid"]["lat"] = None
    for line in grid["lines"]:
        line["r_ohm_per_km"] = None  # falls back to the catalogue (here: none, so zero impedance)
    result = D.diagnose(grid, {}, pf={"converged": False, "error": "no solution", "load_scaling": 2.0})
    assert result["meta"]["generation_parameters_stored"] is False
    assert any(f["id"] == "DA-03:grid" and f["severity"] == "critical" for f in result["findings"])
    empty = D.diagnose({"grid": {"grid_result_id": 1}, "buses": [], "lines": [], "loads": []}, None)
    assert empty["findings"] == [] or all(f["severity"] != "critical" for f in empty["findings"])


def test_version_statistics_and_summary(sandbox):
    grid = sandbox["grids"]["3"]
    stats = D.version_statistics([grid["inputs"]], sandbox["versions"]["1"]["gp"])
    assert stats["sections"] and stats["reach_p90_m"]
    summary = D.grid_summary(run(sandbox, 3))
    assert summary["worst"] == "critical" and "VT-01" in summary["rules"]
    assert D.rule_frequency([summary])[0]["grids"] == 1


def test_threshold_overrides_from_config_analysis(client, project):
    """GRID_DIAGNOSTICS in the project's config_analysis.yaml overrides heuristics (no database needed)."""
    path = project / "config" / "config_analysis.yaml"
    path.write_text(path.read_text(encoding="utf-8") + "\nGRID_DIAGNOSTICS:\n  LONG_FEEDER_M: 650\n  NOT_A_KEY: 1\n",
                    encoding="utf-8")
    rules = client.get("/api/diagnostics/rules").json()
    assert rules["thresholds"]["long_feeder_m"] == 650 and rules["overrides"] == ["long_feeder_m"]
    assert set(rules["categories"]) == {"transformer", "loading", "voltage", "design", "topology", "data"}


# --------------------------------------------------------------------------- endpoints (sandbox database)
@requires_db
def test_diagnostics_endpoints(client):
    versions = client.get("/api/versions").json()
    if not versions:
        pytest.skip("no generated grids in the sandbox")
    summary = client.get(f"/api/results/{versions[0]['version_id']}/summary").json()
    gid = summary["grids"][0]["grid_result_id"]
    before = client.get(f"/api/grids/{gid}/diagnostics", params={"cached": False}).json()
    assert before["source"]["basis"] == "stored_check" and "comparison" not in before
    assert {c["rule"] for c in before["checks"]} >= {"VT-01", "TR-01", "DE-01", "LD-01"}
    pf = client.post(f"/api/grids/{gid}/powerflow").json()
    assert pf["converged"] and pf["diagnostics"]["source"]["basis"] == "power_flow"
    assert "comparison" in pf["diagnostics"]
    cached = client.get(f"/api/grids/{gid}/diagnostics").json()
    assert cached["source"]["basis"] == "power_flow"  # the power flow is reused
    other = client.get(f"/api/grids/{gid}/diagnostics", params={"load_scaling": 1.7}).json()
    assert other["source"]["basis"] == "stored_check"  # no power flow at x1.7 yet
    batch = client.get(f"/api/results/{versions[0]['version_id']}/diagnostics").json()
    assert len(batch["grids"]) == summary["kpis"]["grids"] and "rules" in batch
    assert client.get("/api/grids/987654321/diagnostics").status_code == 404
    assert client.get("/api/diagnostics/rules").json()["rules"]["VT-01"]["kind"] == "symptom"


@requires_db
def test_sandbox_grid3_matches_the_design(client):
    """Version 1, grid 1/3 of the sandbox: critical at 0.883 p.u. with the causes of the design."""
    rows = client.get("/api/results/1/summary").json()["grids"]
    grid = next((g for g in rows if (g["kcid"], g["bcid"]) == (1, 3)), None)
    if grid is None:
        pytest.skip("sandbox grid 1/3 of version 1 not found")
    result = client.get(f"/api/grids/{grid['grid_result_id']}/diagnostics").json()
    rules = {f["rule"] for f in result["findings"] if f["severity"] != "info"}
    assert {"VT-01", "VT-04", "LD-04", "LD-06", "TP-02", "TP-03", "TP-01", "DE-01", "DE-02", "DE-05"} <= rules
    tr02 = next(f for f in result["findings"] if f["rule"] == "TR-02")
    assert tr02["severity"] == "info"  # generated with the MV side at 1.0 p.u.: the busbar is above the reference
