"""The run control of the MV stage without a database: run orchestration (variant lock, PLZ check, new/resume/grow,
failures, post steps) and the district workers, all with fakes."""
from __future__ import annotations

import logging
import sys
import types
from types import SimpleNamespace

import pandas as pd
import pytest
import shapely
from test_mv_district_input import WorkerDb

from pylovo.mv_grid import generator, post_steps, preparation, runtime
from pylovo.mv_grid.model import NODE_COLUMNS, GridOutcome
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError

LV_SNAPSHOT = {
    "load_calculation": {"sim_factor": {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6},
                         "consumer_categories": [{"definition": "Commercial", "peak_load_per_m2": 79.0},
                                                 {"definition": "Public", "peak_load_per_m2": 29.0},
                                                 {"definition": "Unknown", "peak_load_per_m2": 29.0}],
                         "default_power_factor": 0.95},
    "cable_dimensioning": {"mv_direct_connection_load_threshold_kw": 300},
    "power_flow_assessment": {"lv_reference_voltage_pu": 0.96},
}
LV = LvVersionParameters.from_generation_parameters("mv1", LV_SNAPSHOT)


def test_status_of_a_run():
    assert generator._status({"grids_total": 3, "grids_done": 3}) == "done"
    assert generator._status({"grids_total": 3, "grids_done": 2}) == "partial"
    assert generator._status({"grids_total": 3, "grids_done": 0}) == "failed"
    assert generator._status({"grids_total": 0, "grids_done": 0}) == "failed"


class FakeDb:
    """Records the calls of the stage; ``mode``, ``generated``, ``busy``, ``region_built`` script the answers."""

    def __init__(self, *, generated=(1, 2), busy=(), mode="new", stored_plz=None, region_built=False,
                 lock=True, totals=None, fingerprint=None):
        self.calls: list[tuple] = []
        self.stored_fingerprint = fingerprint
        self.generated, self.busy, self.mode = list(generated), list(busy), mode
        self.stored_plz, self.region_built, self.lock = stored_plz, region_built, lock
        self.totals = totals or {"grids_total": 2, "grids_done": 2, "grids_failed": 0, "grids_pending": 0,
                                 "n_stations": 30, "n_customers": 1, "circuit_km": 12.0,
                                 "cable_share": 0.7, "n_violation_grids": 0, "n_band_conflicts": 3}
        self.finished = None
        self.closed = False

    def __getattr__(self, name):          # every other method records its call and returns None
        def method(*args, **kwargs):
            self.calls.append((name, args))
        return method

    def _call(self, name, *args):
        self.calls.append((name, args))

    def names(self):
        return [c[0] for c in self.calls]

    def ensure_grid_persistence_schema(self):
        self._call("schema")

    def check_mv_sources(self):
        return []

    def source_reader(self):
        return SimpleNamespace(where="the pylovo database", close=lambda: self._call("close_source"))

    def try_lock_variant(self, version_id, variant_id):
        self._call("lock", version_id, variant_id)
        return self.lock

    def generated_plz(self, version_id, plz):
        self._call("generated_plz", version_id, plz)
        return [p for p in self.generated if plz is None or p in plz]

    def busy_plz(self, plz):
        self._call("busy_plz", list(plz))
        return [p for p in self.busy if p in plz]

    def delete_variants(self, version_id, ids):
        self._call("delete_variants", version_id, ids)
        return 1

    def variant_row(self, version_id, variant_id):
        self._call("variant_row", version_id, variant_id)
        return None if self.stored_plz is None else {"plz_list": list(self.stored_plz), "mv_parameters": {},
                                                     "lv_fingerprint": self.stored_fingerprint}

    def lv_snapshot(self, version_id):
        return LV_SNAPSHOT

    def planning_region(self, plz):
        self._call("planning_region", list(plz))
        return shapely.box(0, 0, 1, 1).wkb

    def ensure_graph(self, region, params):
        self._call("ensure_graph")
        return "g123"

    def lv_fingerprint(self, version_id, plz):
        return {"n_grids": len(plz)}

    def register_variant(self, version_id, params, plz, graph_id, lv, fingerprint):
        self._call("register_variant", list(plz), graph_id, fingerprint)
        return self.mode

    def variant_region_built(self, version_id, variant_id):
        return self.region_built

    def pending_grids(self, version_id, variant_id):
        self._call("pending_grids")
        return [(7, 30), (6, 10)]

    def variant_totals(self, version_id, variant_id):
        return dict(self.totals)

    def variant_node_table(self, version_id, variant_id):
        rows = {c: [0.0, 0.0, 0.0] for c in NODE_COLUMNS}
        rows.update(kind=["station", "station", "customer"], connection_hint="ring",
                    inst_res=[900.0, 600.0, 0.0], units_res=[60.0, 40.0, 0.0], p_fixed_kw=[150.0, 0.0, 400.0])
        return pd.DataFrame(rows, index=pd.Index([4, 5, 6], name="mv_node_id"))

    def plz_population(self, plz):
        return pd.DataFrame({"plz": list(plz), "pop": 1000.0, "area_km2": 10.0, "polygon_km2": 10.0})

    def finish_variant(self, version_id, variant_id, summary):
        self._call("finish_variant", summary.status)
        self.finished = summary

    def close(self):
        self.closed = True


@pytest.fixture()
def run(monkeypatch, tmp_path):
    """Run the stage on a FakeDb with the region step, the district pool and the tie and link steps replaced by
    recorders (a test may patch the tie and link steps itself)."""
    monkeypatch.setattr(post_steps, "plan_ties", lambda *a, **k: None)
    monkeypatch.setattr(post_steps, "plan_links", lambda *a, **k: None)

    def runner(db: FakeDb, plz=(1, 2, 3), *, region_error=None, **kwargs):
        def region(*args, **kw):
            db.calls.append(("region_step", list(args[3])))
            if region_error:
                raise region_error
            return {}

        monkeypatch.setattr(runtime, "database_client", lambda log_file: db)
        monkeypatch.setattr(preparation, "region_step", region)
        monkeypatch.setattr(generator, "_run_districts",
                            lambda dbc, v, var, pending, *a, **k: db.calls.append(("districts", list(pending))))
        params = kwargs.pop("params", MvParameters(variant_id="d0"))
        return generator.run_mv_stage("mv1", None if plz is None else list(plz), params,
                                  log_file=str(tmp_path / "log.txt"), **kwargs)
    return runner


def test_a_new_variant_runs_every_step(run, capsys):
    db = FakeDb(generated=(1, 2))
    summary = run(db, plz=(1, 2, 3))
    names = db.names()
    assert names.index("lock") < names.index("generated_plz") < names.index("ensure_graph") \
        < names.index("register_variant") < names.index("reset_variant_region") < names.index("region_step") \
        < names.index("districts") < names.index("finish_variant")
    assert ("busy_plz", ([3],)) in db.calls                               # missing PLZ are only try-locked
    assert ("register_variant", ([1, 2], "g123", {"n_grids": 2})) in db.calls
    assert ("districts", [(7, 30), (6, 10)]) in db.calls
    assert summary.status == "done" and summary.plz_missing == [3] and summary.plz_used == [1, 2]
    assert summary.n_band_conflicts == 3 and db.finished is summary
    assert ("unlock_variant", ("mv1", "d0")) in db.calls and db.closed
    assert capsys.readouterr().out.startswith("✓ MV stage on ")


def test_the_osm_coverage_report_follows_its_switch(run, monkeypatch):
    reports = []
    monkeypatch.setattr(post_steps, "osm_coverage_report", lambda *a, **k: reports.append(a[1:3]) or {"n_classes": 4})
    assert run(FakeDb(generated=(1, 2)), plz=(1, 2), osm_coverage=False).osm_coverage == {} and reports == []
    assert run(FakeDb(generated=(1, 2)), plz=(1, 2), osm_coverage=True).osm_coverage == {"n_classes": 4}
    monkeypatch.setattr(runtime.config(), "MV_OSM_COVERAGE", False)          # the configuration is the default
    assert run(FakeDb(generated=(1, 2)), plz=(1, 2)).osm_coverage == {} and reports == [("mv1", "d0")]


def test_a_failed_tie_or_link_step_keeps_the_done_districts(run, monkeypatch):
    def out_of_memory(*args):
        raise MemoryError("ties")

    links = []
    monkeypatch.setattr(post_steps, "plan_ties", out_of_memory)
    monkeypatch.setattr(post_steps, "plan_links", lambda *a: links.append(a[1:3]) or {"n_links": 3})
    db = FakeDb(generated=(1, 2))
    summary = run(db, plz=(1, 2))
    assert summary.status == "done" and db.finished is summary                  # the districts decide the status
    assert summary.ties == {} and summary.links == {"n_links": 3} and links == [("mv1", "d0")]
    assert any(w.startswith("Ties failed: MemoryError: ties; the districts are kept") for w in summary.warnings)
    assert "rollback_changes" in db.names()


def test_the_summary_carries_the_demand_anchor(run, capsys):
    summary = run(FakeDb(generated=(1, 2)), plz=(1, 2))
    demand = summary.demand
    expected_kw = 1500.0 * (0.07 + 0.93 * 100 ** -0.75) + 150.0 + 400.0     # category law + fixed loads
    assert demand["kw_per_inhabitant"] == pytest.approx(expected_kw / 2000.0)
    assert (demand["customer_mw"], demand["busbar_customer_mw"]) == pytest.approx((0.4, 0.15))
    assert demand["population"] == 2000 and demand["approximate"] is False
    assert summary.to_dict()["demand"] == demand
    assert "✓ Demand anchor: " in summary.describe()


def test_a_busy_plz_stops_before_any_write(run):
    db = FakeDb(generated=(1, 2), busy=(3,))
    with pytest.raises(MvStageError, match="being generated right now"):
        run(db, plz=(1, 2, 3))
    assert "register_variant" not in db.names() and "ensure_graph" not in db.names()
    assert ("unlock_variant", ("mv1", "d0")) in db.calls and db.closed


def test_a_locked_variant_is_not_touched(run):
    db = FakeDb(lock=False)
    with pytest.raises(MvStageError, match="another MV run of variant d0 of version mv1 is active"):
        run(db)
    assert db.names() == ["schema", "lock"] and db.closed


def test_resume_skips_the_region_and_grow_rebuilds_it(run):
    db = FakeDb(generated=(1, 2), mode="resume", stored_plz=(1, 2, 5), region_built=True)
    run(db, plz=(1,))
    assert "region_step" not in db.names() and "reset_variant_region" not in db.names()
    assert ("planning_region", ([1, 2, 5],)) in db.calls                 # the stored set stays the region
    assert "check_variant_parameters" in db.names()

    db = FakeDb(generated=(1, 2), mode="resume", stored_plz=(1, 2), region_built=False)
    run(db, plz=(1,))
    assert "region_step" in db.names()                                    # a crashed region step is redone

    db = FakeDb(generated=(1, 2, 7), mode="grow", stored_plz=(1, 2))
    run(db, plz=(7,))
    assert ("register_variant", ([1, 2, 7], "g123", {"n_grids": 3})) in db.calls
    assert ("region_step", [1, 2, 7]) in db.calls and "reset_variant_region" in db.names()


def test_a_resumed_variant_warns_when_its_lv_results_changed(run):
    """The fingerprint is stored at creation; a resume after a load edit mixes old and new LV results."""
    db = FakeDb(generated=(1, 2), mode="resume", stored_plz=(1, 2), region_built=True, fingerprint={"n_grids": 3})
    warnings = run(db, plz=(1,)).warnings
    assert any("changed since it was built" in w and "--replace or under a new id" in w for w in warnings)
    for same in ({"n_grids": 2}, None):                                     # unchanged, or not recorded
        db = FakeDb(generated=(1, 2), mode="resume", stored_plz=(1, 2), region_built=True, fingerprint=same)
        assert not any("changed since" in w for w in run(db, plz=(1,)).warnings)


def test_replace_deletes_the_variant_first(run):
    db = FakeDb()
    run(db, plz=None, replace=True)
    assert db.names().index("delete_variants") < db.names().index("register_variant")
    assert ("generated_plz", ("mv1", None)) in db.calls


def test_a_failed_region_step_marks_the_variant_failed(run):
    db = FakeDb()
    with pytest.raises(MvStageError, match="no substations"):
        run(db, region_error=MvStageError("no substations"))
    assert ("finish_variant", ("failed",)) in db.calls
    assert db.finished.warnings == ["MvStageError: no substations"]
    assert "districts" not in db.names() and "rollback_changes" in db.names()


def test_partial_and_failed_runs(run):
    totals = {"grids_total": 3, "grids_done": 2, "grids_failed": 1, "grids_pending": 0}
    assert run(FakeDb(totals=totals)).status == "partial"
    assert run(FakeDb(totals={"grids_total": 0, "grids_done": 0})).status == "failed"


def test_a_failed_tie_step_keeps_the_grid_counts_and_the_status_of_the_districts(run, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("tie step failed")

    monkeypatch.setattr(post_steps, "plan_ties", broken)
    db = FakeDb()
    summary = run(db, plz=(1, 2))
    assert db.finished is summary and summary.status == "done"
    assert summary.warnings[-1].startswith("Ties failed: RuntimeError: tie step failed; the districts are kept")
    assert (summary.grids_total, summary.grids_done, summary.circuit_km) == (2, 2, 12.0)


def test_an_interrupted_run_does_not_stay_running(run, monkeypatch):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(post_steps, "plan_links", interrupted)
    db = FakeDb()
    with pytest.raises(KeyboardInterrupt):
        run(db, plz=(1, 2))
    assert db.finished.status == "failed" and db.finished.warnings[-1].startswith("KeyboardInterrupt")
    assert db.finished.grids_done == 2 and "unlock_variant" in db.names()


def test_ties_are_planned_by_default(run, monkeypatch):
    seen = []
    monkeypatch.setattr(post_steps, "plan_ties", lambda dbc, v, var, graph_id, params, logger: seen.append(
        (v, var, graph_id)))
    monkeypatch.setattr(post_steps, "plan_links", lambda dbc, v, var, graph_id, params, logger: seen.append(
        ("links", var)))
    run(FakeDb(), plz=(1,), params=MvParameters(variant_id="d0"))
    assert seen == [("mv1", "d0", "g123"), ("links", "d0")]               # the links follow the ties


def test_grid_worker_stores_a_plan_or_marks_the_grid_failed(monkeypatch, tmp_path):
    fake_planner = types.ModuleType("pylovo.mv_grid.planner")
    fake_planner.plan_district = lambda inp: types.SimpleNamespace(
        power_flow_status="converged", topology=types.SimpleNamespace(rings=[1, 2]), repairs=[])
    monkeypatch.setitem(sys.modules, "pylovo.mv_grid.planner", fake_planner)
    monkeypatch.setattr(MvParameters, "from_snapshot", classmethod(lambda cls, v, c, s: cls(variant_id=v)))
    db = WorkerDb()
    monkeypatch.setattr(runtime, "database_client", lambda log_file: db)
    log_file = str(tmp_path / "log.txt")
    outcome = generator._grid_worker("mv1", "d0", 6, {}, "c", LV.as_json(), "g123", log_file)
    assert outcome.status == "done" and outcome.error is None and db.closed
    (_, grid_id, plan, requirements), = [c for c in db.calls if c[0] == "save"]
    assert grid_id == 6 and requirements == {7: 0.98, 8: 0.975}
    assert (tmp_path / "log_mv_mv1_d0_6.txt").exists()

    db = WorkerDb(fail_on="load")
    monkeypatch.setattr(runtime, "database_client", lambda log_file: db)
    outcome = generator._grid_worker("mv1", "d0", 6, {}, "c", LV.as_json(), "g123", log_file)
    assert outcome.status == "failed" and outcome.error == "LookupError: MV grid 6 does not exist"
    assert ("failed", 6, "LookupError: MV grid 6 does not exist") in db.calls and db.closed


def test_districts_run_in_process_largest_first(monkeypatch):
    order = []
    monkeypatch.setattr(generator, "_grid_worker", lambda v, var, gid, *a: order.append(gid) or GridOutcome(
        gid, "done" if gid != 6 else "failed", None if gid != 6 else "boom", 1.0))
    warnings: list[str] = []
    outcomes = generator._run_districts(None, "mv1", "d0", [(7, 30), (6, 10), (5, 3)], MvParameters(), LV, "g",
                                    parallel=False, jobs=4, log_file="log/log.txt",
                                    logger=logging.getLogger("t"), warnings=warnings)
    assert order == [7, 6, 5] and [o.status for o in outcomes] == ["done", "failed", "done"]
    assert generator._run_districts(None, "mv1", "d0", [], MvParameters(), LV, "g", parallel=True, jobs=4,
                                log_file="x", logger=logging.getLogger("t"), warnings=warnings) == []
