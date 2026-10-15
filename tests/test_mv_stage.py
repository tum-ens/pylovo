"""The MV stage driver without a database: worker count, region helpers, run orchestration (variant lock, PLZ
check, new/resume/grow, failures) and the district worker, all with fakes."""
from __future__ import annotations

import sys
import types

import numpy as np
import pandas as pd
import pytest
import shapely

from pylovo.mv_grid import stage
from pylovo.mv_grid.model import NODE_COLUMNS, EdgeSet, GridOutcome
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError

LV_SNAPSHOT = {
    "load_calculation": {"sim_factor": {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6},
                         "default_power_factor": 0.95},
    "cable_dimensioning": {"mv_direct_connection_load_threshold_kw": 300},
    "power_flow_assessment": {"lv_reference_voltage_pu": 0.96},
}
LV = LvVersionParameters.from_generation_parameters("mv1", LV_SNAPSHOT)


# ---------------------------------------------------------------------------------------------- helpers
def test_worker_count_caps_by_jobs_pending_and_memory(monkeypatch):
    monkeypatch.setattr(stage, "mem_available_gb", lambda: 10.0)
    assert stage.worker_count(4, 10) == 4
    assert stage.worker_count(4, 3) == 3
    assert stage.worker_count(4, 0) == 0
    assert stage.worker_count(8, 20, per_worker_gb=4.0) == 2            # (10 - 2) / 4
    monkeypatch.setattr(stage, "mem_available_gb", lambda: 2.5)
    assert stage.worker_count(4, 10) == 1
    monkeypatch.setattr(stage, "mem_available_gb", lambda: None)
    assert stage.worker_count(None, 10) == min(4, stage._config().N_JOBS)
    assert stage.mem_available_gb.__name__ == "<lambda>"


def test_generator_split_follows_the_level_rules():
    locations = pd.DataFrame({"voltage_class": ["hv", "hvmv", "mv", "mvlv", "mvlv", "lv"],
                              "gen_solar_kw": [9000.0, 8000.0, 500.0, 150.0, 300.0, 30.0],
                              "gen_wind_kw": [0.0, 0.0, 0.0, 60.0, 0.0, 0.0]})
    node, lv_side, hv = stage._generator_split(locations, MvParameters())
    assert node.tolist() == [False, True, True, True, True, False]      # mvlv 210 kW > 200: own MV node
    assert lv_side.tolist() == [False, False, False, False, False, True]
    assert hv.tolist() == [True, False, False, False, False, False]
    locations.loc[3, "gen_wind_kw"] = 0.0                                # mvlv 150 kW: LV side
    node, lv_side, _ = stage._generator_split(locations, MvParameters())
    assert node[3] == False and lv_side[3] == True  # noqa: E712
    locations["treatment"] = ["excluded", "node", "node", "lv_side", "node", "lv_side"]   # inputs decides first
    node, lv_side, hv = stage._generator_split(locations, MvParameters())
    assert node.tolist() == [False, True, True, False, True, False] and hv.tolist()[0] is True


def test_attachment_moves_unmapped_customers_to_the_street():
    rows = pd.DataFrame({"mv_node_id": [1, 2, 3, 4], "kind": ["busbar", "station", "customer", "customer"],
                         "mapped_station": [None, None, False, True], "x": [0.0, 1.0, 2.0, 3.0],
                         "y": [0.0, 0.0, 50.0, 60.0]})
    attach = pd.DataFrame({"key": pd.array([4, 3, 2, 1], dtype="Int64"),        # as MvGraphMixin.attach_points
                           "edge_id": pd.array([7, 6, 5, 4], dtype="Int64"), "component": 1,
                           "in_settlement": [True, True, False, True],
                           "settlement_id": pd.array([9, 9, None, 2], dtype="Int64"),
                           "settlement_urban": [False, True, True, False], "fraction": [0.1, 0.2, 0.3, 0.0],
                           "offset_m": [61.0, 48.0, 0.0, 400.0], "ax": [3.0, 2.0, 1.0, 0.0],
                           "ay": [0.0, 2.0, 0.0, 1.0]})
    out = stage._attachment_updates(rows, attach, MvParameters(), stage.logging.getLogger("t"))
    assert out["mv_node_id"].tolist() == [1, 2, 3, 4]
    assert out["attach_edge_id"].tolist() == [4, 5, 6, 7]
    assert out["area_class"].tolist() == ["rural", "rural", "urban", "rural"]   # station 2: open field
    assert out["settlement_id"].isna().tolist() == [False, True, False, False]
    assert out.loc[2, ["x", "y", "attach_offset_m"]].tolist() == [2.0, 2.0, 0.0]   # transfer point
    assert out.loc[3, "attach_offset_m"] == 61.0 and np.isnan(out.loc[3, "x"])   # mapped customer stays
    assert out["flags"].tolist() == [[], [], ["transfer_point"], []]


def test_status_of_a_run():
    assert stage._status({"grids_total": 3, "grids_done": 3}) == "done"
    assert stage._status({"grids_total": 3, "grids_done": 2}) == "partial"
    assert stage._status({"grids_total": 3, "grids_done": 0}) == "failed"
    assert stage._status({"grids_total": 0, "grids_done": 0}) == "failed"


# ---------------------------------------------------------------------------------------------- orchestration
class FakeDb:
    """Records the calls of the stage; ``mode``, ``generated``, ``busy``, ``region_built`` script the answers."""

    def __init__(self, *, generated=(1, 2), busy=(), mode="new", stored_plz=None, region_built=False,
                 lock=True, totals=None):
        self.calls: list[tuple] = []
        self.generated, self.busy, self.mode = list(generated), list(busy), mode
        self.stored_plz, self.region_built, self.lock = stored_plz, region_built, lock
        self.totals = totals or {"grids_total": 2, "grids_done": 2, "grids_failed": 0, "grids_pending": 0,
                                 "n_stations": 30, "n_customers": 1, "n_generators": 2, "circuit_km": 12.0,
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
        return None if self.stored_plz is None else {"plz_list": list(self.stored_plz), "mv_parameters": {}}

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

    def finish_variant(self, version_id, variant_id, summary):
        self._call("finish_variant", summary.status)
        self.finished = summary

    def close(self):
        self.closed = True


@pytest.fixture()
def run(monkeypatch, tmp_path):
    """Run the stage on a FakeDb with the region step and the district pool replaced by recorders."""
    def runner(db: FakeDb, plz=(1, 2, 3), *, region_error=None, **kwargs):
        def region(*args, **kw):
            db.calls.append(("region_step", list(args[3])))
            if region_error:
                raise region_error
            return {}

        monkeypatch.setattr(stage, "_database_client", lambda log_file: db)
        monkeypatch.setattr(stage, "_region_step", region)
        monkeypatch.setattr(stage, "_run_districts",
                            lambda dbc, v, var, pending, *a, **k: db.calls.append(("districts", list(pending))))
        return stage.run_mv_stage("mv1", None if plz is None else list(plz), MvParameters(variant_id="d0"),
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


def test_ties_are_skipped_without_the_module(run, monkeypatch):
    monkeypatch.setitem(sys.modules, "pylovo.mv_grid.interconnection", None)
    summary = run(FakeDb(), plz=(1,), )
    assert summary.warnings == []
    db = FakeDb()
    monkeypatch.setattr(stage, "_database_client", lambda log_file: db)
    params = MvParameters(variant_id="d0", interconnection="ties")
    monkeypatch.setattr(stage, "_region_step", lambda *a, **k: {})
    monkeypatch.setattr(stage, "_run_districts", lambda *a, **k: None)
    summary = stage.run_mv_stage("mv1", [1], params, log_file="log/log.txt")
    assert summary.warnings == ["ties not implemented in this build: MV_INTERCONNECTION ties was ignored"]


# ---------------------------------------------------------------------------------------------- workers
def district_nodes() -> pd.DataFrame:
    rows = []
    for i, (kind, gr, offset) in enumerate([("busbar", np.nan, 150.0), ("station", 7, 0.0), ("station", 8, 2500.0),
                                            ("customer", np.nan, 30.0)]):
        row = {c: 0.0 for c in NODE_COLUMNS}
        row.update(kind=kind, connection_hint="busbar" if kind == "busbar" else "ring", x=100.0 * i, y=0.0,
                   attach_edge_id=10 + i, attach_fraction=0.25, attach_offset_m=offset, grid_result_id=gr,
                   lv_design_source_vm_pu=np.nan)
        rows.append(row)
    return pd.DataFrame(rows, index=pd.Index([1, 2, 3, 4], name="mv_node_id"))[list(NODE_COLUMNS)]


class WorkerDb:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls: list[tuple] = []
        self.closed = False

    def load_district_nodes(self, mv_grid_id):
        self.calls.append(("load", mv_grid_id))
        if self.fail_on == "load":
            raise LookupError(f"MV grid {mv_grid_id} does not exist")
        return district_nodes(), {"version_id": "mv1", "mv_variant_id": "d0", "substation_id": 5, "vn_kv": 20.0,
                                  "osm_ratings_mva": [40.0, 40.0]}

    def lv_design_source_vm(self, version_id, ids):
        self.calls.append(("requirement", list(ids)))
        return {7: 0.98, 8: 0.975}

    def subgraph_edges(self, graph_id, hull_wkb, buffer_m, must):
        self.calls.append(("subgraph", graph_id, buffer_m, list(must)))
        return EdgeSet(*(np.zeros(0) for _ in range(7)), np.zeros((0, 2)))

    def edge_geometries(self, graph_id, ids):
        self.calls.append(("geometries", list(ids)))
        return {i: shapely.LineString([(0, 0), (i, 1)]) for i in ids}

    def rollback_changes(self):
        self.calls.append(("rollback",))

    def save_district(self, version_id, variant_id, mv_grid_id, plan, runtime_s, *, requirements=None):
        self.calls.append(("save", mv_grid_id, plan, requirements))

    def mark_grid_failed(self, mv_grid_id, error):
        self.calls.append(("failed", mv_grid_id, error))

    def close(self):
        self.closed = True


def test_district_input_reads_the_district(monkeypatch):
    db = WorkerDb()
    params = MvParameters(subgraph_buffer_m=2000.0)
    inp, requirements = stage.district_input(db, "mv1", "d0", 6, "g123", params, LV)
    assert requirements == {7: 0.98, 8: 0.975}
    assert inp.nodes["lv_design_source_vm_pu"].tolist()[1:3] == [0.98, 0.975]
    assert np.isnan(inp.nodes["lv_design_source_vm_pu"].iloc[0])
    (_, graph_id, buffer_m, must), = [c for c in db.calls if c[0] == "subgraph"]
    assert graph_id == "g123" and buffer_m == 2600.0 and must == [10, 11, 12, 13]
    assert inp.known_units_mva == (40.0, 40.0) and inp.substation_id == 5 and inp.vn_kv == 20.0
    assert set(inp.edge_geometry([3, 4])) == {3, 4} and set(inp.edge_geometry([4, 5])) == {4, 5}
    assert set(inp.edge_geometry([10, 13])) == {10, 13}
    assert [c[1] for c in db.calls if c[0] == "geometries"] == [[10, 11, 12, 13], [3, 4], [5]]   # once per edge
    # the street point of each node on its attach edge (LineString (0, 0)-(edge, 1) at attach_fraction 0.25)
    assert inp.nodes["ax"].tolist() == [2.5, 2.75, 3.0, 3.25] and inp.nodes["ay"].tolist() == [0.25] * 4
    with pytest.raises(ValueError, match="belongs to variant d0 of version mv1"):
        stage.district_input(db, "mv1", "d9", 6, "g123", params, LV)


def test_grid_worker_stores_a_plan_or_marks_the_grid_failed(monkeypatch, tmp_path):
    fake_planner = types.ModuleType("pylovo.mv_grid.planner")
    fake_planner.plan_district = lambda inp: types.SimpleNamespace(
        power_flow_status="converged", topology=types.SimpleNamespace(rings=[1, 2]), repairs=[])
    monkeypatch.setitem(sys.modules, "pylovo.mv_grid.planner", fake_planner)
    monkeypatch.setattr(MvParameters, "from_snapshot", classmethod(lambda cls, v, c, s: cls(variant_id=v)))
    db = WorkerDb()
    monkeypatch.setattr(stage, "_database_client", lambda log_file: db)
    log_file = str(tmp_path / "log.txt")
    outcome = stage._grid_worker("mv1", "d0", 6, {}, "c", LV.as_json(), "g123", log_file)
    assert outcome.status == "done" and outcome.error is None and db.closed
    (_, grid_id, plan, requirements), = [c for c in db.calls if c[0] == "save"]
    assert grid_id == 6 and requirements == {7: 0.98, 8: 0.975}
    assert (tmp_path / "log_mv_mv1_d0_6.txt").exists()

    db = WorkerDb(fail_on="load")
    monkeypatch.setattr(stage, "_database_client", lambda log_file: db)
    outcome = stage._grid_worker("mv1", "d0", 6, {}, "c", LV.as_json(), "g123", log_file)
    assert outcome.status == "failed" and outcome.error == "LookupError: MV grid 6 does not exist"
    assert ("failed", 6, "LookupError: MV grid 6 does not exist") in db.calls and db.closed


def test_districts_run_in_process_largest_first(monkeypatch):
    order = []
    monkeypatch.setattr(stage, "_grid_worker", lambda v, var, gid, *a: order.append(gid) or GridOutcome(
        gid, "done" if gid != 6 else "failed", None if gid != 6 else "boom", 1.0))
    warnings: list[str] = []
    outcomes = stage._run_districts(None, "mv1", "d0", [(7, 30), (6, 10), (5, 3)], MvParameters(), LV, "g",
                                    parallel=False, jobs=4, log_file="log/log.txt",
                                    logger=stage.logging.getLogger("t"), warnings=warnings)
    assert order == [7, 6, 5] and [o.status for o in outcomes] == ["done", "failed", "done"]
    assert stage._run_districts(None, "mv1", "d0", [], MvParameters(), LV, "g", parallel=True, jobs=4,
                                log_file="x", logger=stage.logging.getLogger("t"), warnings=warnings) == []


# ---------------------------------------------------------------------------------------------- region step
class RegionDb:
    """Fake client for the region step: ids from 1, three sites, two stations, a customer, a generator."""

    def __init__(self):
        self.calls: dict[str, list] = {}
        self.cur = types.SimpleNamespace(execute=lambda sql, *a: self.calls.setdefault("sql", []).append(sql))
        self.inserted_nodes = None

    def _record(self, name, *args):
        self.calls.setdefault(name, []).append(args)

    def egon_available(self):
        return True, "opendata"

    def egon_substations(self, region, buffer_m):
        return pd.DataFrame({"bus_id": [1], "x": [0.0], "y": [0.0]})

    def osm_substation_candidates(self, region, buffer_m):
        return pd.DataFrame({"source_id": ["way/1", "way/2", "way/3"]})

    def insert_substations(self, version_id, variant_id, sites):
        self._record("insert_substations", sites.copy())
        return np.array([11, 12, 13])

    def station_nodes(self, version_id, plz):
        return pd.DataFrame({"grid_result_id": [7, 8]})

    def station_load_stats(self, version_id, plz):
        return pd.DataFrame()

    def pandapower_load_check(self, version_id, plz):
        return pd.DataFrame({"grid_result_id": [7, 8], "p_kw": [10.0, 20.0]})

    def mastr_units_in_region(self, region):
        return pd.DataFrame()

    def lv_side_targets(self, version_id, points):
        self._record("lv_side_targets", points.copy())
        return points.assign(grid_result_id=7)

    def mastr_postcode_aggregates(self, plz):
        return pd.DataFrame()

    def station_building_counts(self, version_id, plz):
        return pd.DataFrame()

    def mv_customer_rows(self, version_id, plz, lv):
        return pd.DataFrame()

    def insert_nodes(self, version_id, variant_id, rows):
        self.inserted_nodes = rows.copy()
        return np.arange(101, 101 + len(rows))

    def attach_points(self, graph_id, points):
        offsets = {101: 50.0, 102: 2500.0, 103: 10.0, 104: 0.0, 105: 0.0, 106: 40.0, 107: 30.0}
        keys = points["key"].to_numpy()
        return pd.DataFrame({"key": pd.array(keys, dtype="Int64"), "edge_id": pd.array(keys + 1000, dtype="Int64"),
                             "component": 1, "in_settlement": True,
                             "settlement_id": pd.array([5] * len(keys), dtype="Int64"),
                             "settlement_urban": False, "fraction": 0.5,
                             "offset_m": [offsets[int(k)] for k in keys], "ax": points["x"] + 1.0, "ay": 0.0})

    def update_node_attachments(self, rows):
        self._record("update_node_attachments", rows.copy())

    def flag_substations(self, ids, flag):
        self._record("flag_substations", list(ids), flag)

    def variant_node_table(self, version_id, variant_id):
        rows = self.inserted_nodes.assign(mv_node_id=np.arange(101, 101 + len(self.inserted_nodes)))
        return rows.set_index("mv_node_id")[["kind", "connection_hint", "x", "y"]].assign(
            attach_edge_id=lambda f: f.index + 1000)

    def region_graph_arrays(self, graph_id):
        ids = np.arange(1100, 1108)
        return ids, ids * 10, ids * 10 + 1, np.full(len(ids), 100.0)

    def create_grids(self, version_id, variant_id, substation_ids, vn_kv):
        self._record("create_grids", list(substation_ids), vn_kv)
        return {int(s): 900 + i for i, s in enumerate(substation_ids)}

    def set_node_grids(self, assignment):
        self._record("set_node_grids", assignment.copy())

    def write_grid_areas(self, version_id, variant_id, plz):
        self._record("write_grid_areas", list(plz))


@pytest.fixture()
def region_modules(monkeypatch):
    """Fake substations, inputs, districts and graph modules for the region step."""
    recorded: dict[str, object] = {}
    subs = types.ModuleType("pylovo.mv_grid.substations")
    sites = [types.SimpleNamespace(key=f"way/{i}") for i in (1, 2, 3)]
    subs.select_osm_sites = lambda candidates, params: types.SimpleNamespace(sites=sites, describe=lambda: "3 sites")
    subs.match_egon = lambda s, egon, max_m: [(1, 0.0), (None, None), (None, None)]
    subs.sites_frame = lambda s, region_wkb=None, matches=None: pd.DataFrame({
        "source": "osm", "source_ids": [[x.key] for x in s], "egon_bus_id": pd.array([1, None, None], dtype="Int64"),
        "in_region": [True, False, True], "osm_ratings_mva": None, "flags": [[], [], []],
        "x": [0.0, 5000.0, 900.0], "y": [0.0, 0.0, 0.0], "footprint_wkb": None})
    inputs = types.ModuleType("pylovo.mv_grid.inputs")
    inputs.station_table = lambda st, stats, lv: pd.DataFrame({
        "kind": "station", "connection_hint": "ring", "grid_result_id": [7, 8], "x": [10.0, 20.0], "y": 0.0,
        "p_own_kw": [10.0, 20.0], "gen_solar_kw": 0.0})
    inputs.customer_table = lambda rows, lv, params: pd.DataFrame({
        "kind": ["customer"], "connection_hint": ["ring"], "x": [30.0], "y": [40.0], "mapped_station": [False]})
    inputs.aggregate_locations = lambda units, params: pd.DataFrame({
        "mastr_connection_id": ["SEL1", "SEL2", "SEL3"], "x": [50.0, 60.0, 70.0], "y": 0.0,
        "gen_solar_kw": [800.0, 30.0, 50000.0], "treatment": ["node", "lv_side", "excluded"]})

    def distribute(stations, targets, aggregates, counts):
        recorded["targets"] = targets
        return pd.DataFrame({"grid_result_id": [7, 8], "gen_solar_kw": [30.0, 0.0], "gen_wind_kw": 0.0,
                             "gen_other_kw": 0.0})

    inputs.distribute_lv_generation = distribute

    def node_rows(stations, customers, generators, sites_frame, params):
        recorded["stations"], recorded["generators"] = stations, generators
        busbars = pd.DataFrame({"kind": "busbar", "connection_hint": "busbar",
                                "substation_id": sites_frame["substation_id"], "x": sites_frame["x"], "y": 0.0})
        gens = generators.assign(kind="generator", connection_hint="tee")
        return pd.concat([busbars, stations, customers, gens], ignore_index=True)

    inputs.node_rows = node_rows
    districts = types.ModuleType("pylovo.mv_grid.districts")
    districts.site_capacity_mva = lambda sites, params: (recorded.__setitem__("sites", sites) or np.full(len(sites),
                                                                                                         63.0))

    def routed(csr, vertex_index, edge_len, sites, nodes, g, pf, capacity, params):
        recorded["nodes"] = nodes
        return types.SimpleNamespace(site_of_node=np.zeros(len(nodes), dtype=np.int64), iterations=1,
                                     overloaded=[0], bridged=np.array([False, True, False, False]))

    districts.routed_assignment = routed
    graph = types.ModuleType("pylovo.mv_grid.graph")
    graph.region_csr = lambda source, target, length: ("csr", {})
    for name, module in (("substations", subs), ("inputs", inputs), ("districts", districts), ("graph", graph)):
        monkeypatch.setitem(sys.modules, f"pylovo.mv_grid.{name}", module)
    return recorded


def test_region_step_writes_substations_nodes_and_districts(region_modules):
    db = RegionDb()
    warnings: list[str] = []
    info = stage._region_step(db, "mv1", "d0", [1, 2], shapely.box(0, 0, 1, 1).wkb, "g1", MvParameters(), LV,
                              None, stage.logging.getLogger("t"), warnings)
    # LV-side points go to lv_side_targets; only treatment 'node' becomes a generator node
    assert db.calls["lv_side_targets"][0][0]["mastr_connection_id"].tolist() == ["SEL2"]
    assert region_modules["generators"]["mastr_connection_id"].tolist() == ["SEL1"]
    assert region_modules["stations"]["gen_solar_kw"].tolist() == [30.0, 0.0]
    nodes = db.inserted_nodes
    assert nodes["kind"].tolist() == ["busbar"] * 3 + ["station", "station", "customer", "generator"]
    # the customer moves to its street transfer point
    update = db.calls["update_node_attachments"][0][0].set_index("mv_node_id")
    assert update.loc[106, ["x", "y", "attach_offset_m"]].tolist() == [31.0, 0.0, 0.0]
    assert update.loc[106, "flags"] == ["transfer_point"] and update.loc[104, "flags"] == []
    assert update["attach_edge_id"].dtype == np.float64 and update.loc[101, "area_class"] == "rural"
    # substation 12 is 2.5 km from the streets: no supply area; 13 gets no node
    flags = {flag: ids for ids, flag in db.calls["flag_substations"]}
    assert flags["long_attach"] == [12] and flags["no_nodes"] == [13] and flags["over_capacity"] == [11]
    assert region_modules["sites"]["substation_id"].tolist() == [11, 13]
    assert {"edge_source", "edge_target", "edge_length_m", "attach_edge_id"} <= set(region_modules["sites"].columns)
    assert region_modules["sites"]["edge_source"].tolist() == [11010, 11030]    # edges 1101, 1103
    assert region_modules["nodes"]["kind"].tolist() == ["station", "station", "customer", "generator"]
    assert db.calls["create_grids"] == [([11], 20.0)]
    grids = db.calls["set_node_grids"][0][0]
    assert grids["mv_node_id"].tolist() == [104, 105, 106, 107] and grids["mv_grid_id"].tolist() == [900] * 4
    assert grids["bridged"].tolist() == [False, True, False, False]
    assert db.calls["write_grid_areas"] == [([1, 2],)]
    assert info == {"n_sites": 3, "n_grids": 1, "kinds": {"station": 2, "customer": 1, "generator": 1}}
    assert warnings == [] and any("SAVEPOINT" in s for s in db.calls["sql"])
