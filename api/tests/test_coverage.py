"""Region gate without a database: evaluation rules, scheduling, invalidation, cache, generate gate."""
from __future__ import annotations

import json
import time

import pytest
from fastapi import HTTPException
from pylovo_api import coverage as cv
from pylovo_api import gate
from pylovo_api.coverage import BC, FULL, InputCoverage, TableState

FLAGS = {"exclude_buildings_without_address": False, "residential_only": False}
ADDR = {"exclude_buildings_without_address": True, "residential_only": False}


def bc(**kw) -> list[int]:
    """Building counters; unspecified ones follow from the given ones (all addressed, none bad)."""
    total = kw.get("total", 10)
    cand = kw.get("cand", total)
    values = {"total": total, "cand": cand, "cand_addr": kw.get("cand_addr", cand), "res": kw.get("res", cand)}
    values["res_addr"] = kw.get("res_addr", min(values["res"], values["cand_addr"]))
    values.update(stations=kw.get("stations", 0), bad=kw.get("bad", 0), bad_addr=kw.get("bad_addr", 0),
                  bad_res=kw.get("bad_res", 0), bad_res_addr=kw.get("bad_res_addr", 0))
    return [values[k] for k in BC]


def engine(buildings=None, ways=None, lines=None, strategy="ags", register=None, polygons=(85653,),
           computed=True, key_col="gemeindeschluessel") -> InputCoverage:
    """An initialised engine with synthetic counters: ``{key: {plz: counters}}`` per table."""
    cov = InputCoverage()
    cov.initialized = True
    cov.register = register if register is not None else {85653: [9184137]}
    cov.ags_names = {9184137: "Aying", 9184129: "Hohenbrunn"}
    cov.local_bbox = {p: (11.7, 47.9, 11.8, 48.0) for p in polygons}
    cov.settings = {"infdb_source_schema": "basedata", "infdb_opendata_schema": "opendata"}
    default = {"buildings": {"09184137": {85653: bc(total=50)}}, "ways": {"09184137": {85653: [100]}},
               "lines": {"09184137": {85653: [50]}}}
    for name, data in (("buildings", buildings), ("ways", ways), ("lines", lines)):
        t = cov.tables[name]
        t.strategy = strategy
        t.key_col = key_col if name == "buildings" else "ags"
        data = default[name] if data is None else data
        t.keys = set(data)
        t.index_keys()
        if computed:          # a fresh engine has keys (from the key listing) but no counters yet
            t.merge(sorted(data), data, time.time())
            if strategy in ("full", "full_large"):
                t.computed[FULL] = time.time()
    return cov


def status(cov, plz=85653, flags=FLAGS):
    return cov.status_of(plz, flags)


# ------------------------------------------------------------------ evaluation
def test_ready_counts_and_brief():
    st = status(engine())
    assert st["status"] == "ready" and st["severity"] == "ok" and st["selectable"] and st["verified"] and st["exact"]
    assert st["counts"]["buildings_importable"] == 50 and st["counts"]["street_segments"] == 100
    assert cv.brief(st) == {"status": "ready", "severity": "ok", "selectable": True, "verified": True,
                            "short": "50 buildings"}


@pytest.mark.parametrize("table", ["buildings", "ways", "lines"])
def test_no_source_blocks_every_plz(table):
    cov = engine()
    cov.tables[table].strategy = "missing"
    st = status(cov)
    assert st["status"] == "no_source" and not st["selectable"] and st["actions"][0]["step"] == "database"


def test_no_geometry_mirrors_the_library_match():
    cov = engine(polygons=())
    assert status(cov)["status"] == "no_geometry"
    cov.infdb_polygons = {"85653"}                       # fetch_postcode_from_infdb: plz = '85653'
    st = status(cov)
    assert st["status"] == "ready" and any(w["code"] == "geometry_infdb" for w in st["warnings"])
    cov.infdb_polygons = {"01067"}                       # the library does not zero-pad (plz::varchar)
    assert status(cov, 1067)["status"] == "no_geometry"


def test_no_buildings_exact_and_preliminary():
    cov = engine(buildings={"09184137": {}}, register={85653: [9184137]})
    st = status(cov)
    assert st["status"] == "no_buildings" and st["exact"] and not st["preliminary"] and not st["selectable"]
    # municipality not processed at all while other keys are still counted: preliminary block
    cov = engine(buildings={"09175116": {85658: bc()}}, register={85653: [9184129]})
    cov.tables["buildings"].keys.add("09999999")
    cov.tables["buildings"].index_keys()
    st = status(cov)
    assert st["status"] == "no_buildings" and st["preliminary"] and st["ags"] == ["09184129"]
    assert st["actions"][0]["kind"] == "infdb" and st["actions"][0]["tool"] == "infdb-basedata-buildings"


def test_pending_when_register_keys_are_not_counted_yet():
    cov = engine(computed=False)
    st = status(cov)
    assert st["status"] == "pending" and st["selectable"] and not st["verified"]


def test_block_from_an_incomplete_view_is_pending_not_a_block():
    # one of two municipalities counted and without consumers: wait for the other one
    data = {"09184137": {85653: bc(total=3, cand=0)}, "09184129": {}}
    cov = engine(buildings=data, register={85653: [9184137, 9184129]})
    cov.tables["buildings"].computed.pop("09184129")
    assert status(cov)["status"] == "pending"
    cov.tables["buildings"].computed["09184129"] = time.time()
    assert status(cov)["status"] == "no_consumers"


def test_no_consumers_and_config_actions():
    cov = engine(buildings={"09184137": {85653: bc(total=5, cand=4, cand_addr=0)}})
    st = status(cov, flags=ADDR)
    assert st["status"] == "no_consumers"
    assert st["actions"] == [{"kind": "config", "key": "EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", "value": False,
                              "label": "Set EXCLUDE_BUILDINGS_WITHOUT_ADDRESS = False (4 buildings would pass)"}]
    assert status(cov)["status"] == "ready"                              # without the address filter
    only_other = engine(buildings={"09184137": {85653: bc(total=5, cand=0)}})
    st = status(only_other, flags=ADDR)
    assert st["status"] == "no_consumers" and st["actions"] == []          # no config switch helps


def test_too_few_buildings():
    cov = engine(buildings={"09184137": {85653: bc(total=3, cand=1)}})
    st = status(cov)
    assert st["status"] == "too_few_buildings" and st["short"] == "1 building" and not st["selectable"]


def test_no_residential_and_residential_only():
    cov = engine(buildings={"09184137": {85653: bc(total=6, cand=6, res=0)}})
    assert status(cov)["status"] == "no_residential"
    cov = engine(buildings={"09184137": {85653: bc(total=30, cand=30, res=1)}})
    ro = {"exclude_buildings_without_address": False, "residential_only": True}
    st = status(cov, flags=ro)
    assert st["status"] == "too_few_buildings"                              # cand_eff = res_eff = 1
    assert status(cov)["status"] == "ready"


def test_no_streets_and_no_connection_lines():
    assert status(engine(ways={"09184137": {85653: [0]}}))["status"] == "no_streets"   # only cycle/footpaths
    assert status(engine(ways={"09184137": {}}))["status"] == "no_streets"
    st = status(engine(lines={"09184137": {}}))
    assert st["status"] == "no_connection_lines" and st["actions"][0]["tool"] == "infdb-basedata-ways"


def test_unknown_without_data_is_selectable():
    cov = InputCoverage()
    cov.error = "Database not reachable"
    st = cov.status_of(85653, FLAGS)
    assert st["status"] == "unknown" and st["selectable"] and not st["verified"]
    assert st["actions"] == [{"kind": "retry", "label": "Check again"}]


def test_partial_uses_the_key_set_not_postcode_counts():
    reg = {85653: [9184137, 9184129]}
    cov = engine(register=reg)
    st = status(cov)
    assert st["status"] == "ready" and st["severity"] == "warn" and st["short"] == "partial"
    assert {w["code"] for w in st["warnings"]} == {"partial:buildings", "partial:ways", "partial:lines"}
    # processed municipality without rows of this postcode: a marginal register pair, not partial
    data = {"09184137": {85653: bc(total=50)}, "09184129": {85662: bc()}}
    ways = {"09184137": {85653: [100]}, "09184129": {85662: [5]}}
    lines = {"09184137": {85653: [50]}, "09184129": {85662: [5]}}
    assert status(engine(buildings=data, ways=ways, lines=lines, register=reg))["severity"] == "ok"


def test_register_gap_rows_are_found_and_reported():
    # the buildings of 85658 carry the key of another municipality (as in the demo2 extract)
    cov = engine(buildings={"09184137": {85658: bc(total=20)}}, ways={"09184137": {85658: [30]}},
                 lines={"09184137": {85658: [20]}}, register={85658: [9175116]}, polygons=(85658,))
    st = status(cov, 85658)
    assert st["status"] == "ready" and "partial:buildings" in {w["code"] for w in st["warnings"]}


def test_sparse_and_data_quality_warnings():
    cov = engine(buildings={"09184137": {85653: bc(total=5, bad=2)}})
    st = status(cov)
    codes = {w["code"] for w in st["warnings"]}
    assert st["status"] == "ready" and codes == {"sparse", "data_quality"} and st["short"] == "data warning"


def test_full_strategy_without_key_column():
    cov = engine(strategy="full", key_col=None, buildings={"*": {85653: bc()}}, ways={"*": {85653: [3]}},
                 lines={"*": {85653: [3]}})
    for t in cov.tables.values():
        t.key_col = None
    assert status(cov)["status"] == "ready"
    cov.tables["ways"].computed.clear()
    assert status(cov)["status"] == "pending"


def test_enforce_off_turns_blocks_into_warnings(monkeypatch):
    monkeypatch.setattr(cv, "ENFORCE", False)
    st = status(engine(buildings={"09184137": {}}))
    assert st["status"] == "no_buildings" and st["severity"] == "warn" and st["selectable"]


def test_selectable_set_and_summary_counts():
    cov = engine(register={85653: [9184137], 85665: [9175128]}, polygons=(85653, 85665))
    assert cov.selectable_set(FLAGS) == {85653}
    counts = cov._summary(FLAGS)["counts"]
    assert counts == {"postcodes": 2, "ready": 1, "partial": 0, "pending": 0, "blocked": 1}


# ------------------------------------------------------------------ helpers & strategy
def test_key_mapping_and_plan_guard():
    assert cv.key_to_ags("09184137") == 9184137 == cv.key_to_ags("9184137")
    assert cv.key_to_ags("091840000137") == 9184137 and cv.key_to_ags("*") is None
    plan = [{"Plan": {"Node Type": "CTE Scan", "Plans": [{"Node Type": "Seq Scan", "Relation Name": "buildings"}]}}]
    assert cv._plan_has_seq_scan(plan, "buildings") and not cv._plan_has_seq_scan(plan, "ways_per_connection")


def probe(rows=1_000_000, index=True, cols=("postcode", "gemeindeschluessel"), select=True):
    return {"rel": "basedata.buildings", "relname": "buildings", "est_rows": rows, "bytes": 10 ** 9,
            "relfilenode": 42, "can_select": select, "cols": {c: "text" for c in cols},
            "indexes": {"gemeindeschluessel": "idx_buildings_gemeindeschluessel"} if index else {}}


def test_strategy_choice(monkeypatch):
    cov = InputCoverage()
    monkeypatch.setattr(cov, "_index_plan_ok", lambda *a: True)
    t = TableState("buildings")
    cov._apply_probe(t, probe(), None)
    assert t.strategy == "ags" and t.key_col == "gemeindeschluessel"
    cov._apply_probe(t := TableState("buildings"), probe(rows=522), None)
    assert t.strategy == "full" and t.pending == {FULL: 1}
    cov._apply_probe(t := TableState("buildings"), probe(index=False), None)
    assert t.strategy == "full_large" and "no btree index" in t.reason
    cov._apply_probe(t := TableState("buildings"), None, None)
    assert t.strategy == "missing"
    cov._apply_probe(t := TableState("buildings"), probe(select=False), None)
    assert t.strategy == "missing" and t.reason == "no SELECT privilege"
    monkeypatch.setattr(cov, "_index_plan_ok", lambda *a: False)       # EXPLAIN shows a Seq Scan
    cov._apply_probe(t := TableState("buildings"), probe(), None)
    assert t.strategy == "full_large" and "planner" in t.reason
    info = cov.coverage()
    assert info["tables"]["buildings"]["strategy"] == "unknown"  # engine tables untouched


def test_relfilenode_change_drops_the_counters(monkeypatch):
    cov = engine()
    monkeypatch.setattr(cov, "_index_plan_ok", lambda *a: True)
    t = cov.tables["buildings"]
    t.relfilenode = 41
    cov._apply_probe(t, probe(), None)                                  # 41 -> 42: table recreated
    assert t.computed == {} and t.stale and t.pending == {}
    st = status(cov)                    # stale-while-revalidate: old positives stay usable, flagged
    assert st["status"] == "ready" and st["stale"] and not st["exact"]
    t.plz_rows[85653]["09184137"] = bc(total=0)                          # an old negative ...
    assert status(cov)["status"] == "pending"                           # ... is not trusted (no block)


# ------------------------------------------------------------------ scheduling & invalidation
def test_ensure_priority_runs_before_the_backlog():
    cov = engine(computed=False)
    t = cov.tables["buildings"]
    t.keys |= {"09000001", "09000002"}
    t.index_keys()
    for key in t.keys:
        t.pending[key] = 3
    t.pending["09184137"] = 0
    table, keys = cov._next_batch()
    assert table.name == "buildings" and keys == ["09184137"]


def test_backoff_and_adaptive_chunks():
    cov = engine(computed=False)
    t = cov.tables["ways"]
    t.pending = {"09184137": 3}
    cov._batch_failed(t, ["09184137"], "canceling statement due to statement timeout")
    assert t.backoff["09184137"][0] == 1 and t.backoff["09184137"][1] > time.time() + 50
    assert cov._next_batch() is None                                    # the backed-off key waits
    cov.chunk = 40
    cov._batch_failed(t, ["a", "b", "c", "d"], "timeout")
    assert cov.chunk == 2


def test_full_large_waits_while_generate_runs():
    class Jobs:
        def running_writer(self):
            return type("J", (), {"kind": "generate"})()

    cov = engine(strategy="full_large", computed=False)
    for t in cov.tables.values():
        t.pending = {FULL: 3}
    cov.jobs = Jobs()
    assert cov._next_batch() is None
    cov.jobs = None
    assert cov._next_batch()[1] == [FULL]


def test_changelog_requeues_only_the_changed_municipality(monkeypatch):
    cov = engine(buildings={"09184137": {85653: bc()}, "09184129": {85662: bc()}},
                 ways={"09184137": {85653: [1]}, "09184129": {85662: [1]}},
                 lines={"09184137": {85653: [1]}, "09184129": {85662: [1]}})
    for t in cov.tables.values():
        t.markers = {"09184137": 10, "09184129": 11}
    rows = [("infdb-basedata-ways", "09184129", 12, None), ("infdb-basedata-buildings", "09184137", 10, None)]
    monkeypatch.setattr(cov, "_query", lambda sql, params=None: [(True,)] if "to_regclass" in sql else rows)
    cov._read_changelog()
    assert cov.tables["ways"].pending == {"09184129": 2} and cov.tables["lines"].pending == {"09184129": 2}
    assert cov.tables["buildings"].pending == {}


def test_rolling_ttl_requeues_old_keys():
    cov = engine()
    t = cov.tables["buildings"]
    t.computed["09184137"] = time.time() - cv.TTL_S - 5
    cov._apply_ttl()
    assert t.pending == {"09184137": 3}


def test_job_hooks_never_requeue_basedata():
    cov = engine()
    cov.on_job_finished(type("J", (), {"kind": "generate"})())
    assert cov._check_requested and not any(t.pending for t in cov.tables.values())


def test_no_checks_without_a_browser():
    cov = engine()
    cov._last_check = 0
    cov._last_seen = time.time() - cv.CLIENT_IDLE_S - 1
    assert not cov._check_due()
    cov.touch()
    assert cov._check_due()


# ------------------------------------------------------------------ persistence
def test_cache_round_trip_and_signature(tmp_path, monkeypatch):
    path = tmp_path / "cache" / "input-coverage.json"
    cov = engine()
    cov.cache_file = path
    cov.settings.update(host="h", port="1", dbname="d")
    for t in cov.tables.values():
        t.relfilenode = 42
    cov._save(force=True)
    assert path.exists() and not path.with_suffix(".tmp").exists()
    fresh = InputCoverage(cache_file=path)
    fresh.settings = dict(cov.settings)
    cached = fresh._load()
    monkeypatch.setattr(fresh, "_index_plan_ok", lambda *a: True)
    p = probe()
    fresh._apply_probe(fresh.tables["buildings"], p, cached["buildings"])
    assert fresh.tables["buildings"].plz_rows[85653]["09184137"] == bc(total=50)
    assert "09184137" in fresh.tables["buildings"].computed
    other = InputCoverage(cache_file=path)
    other.settings = dict(cov.settings, dbname="another")
    assert other._load() is None                                       # signature mismatch
    path.write_text("{truncated")
    assert fresh._load() is None


# ------------------------------------------------------------------ flags
def test_flags_follow_the_file_and_fall_back_when_broken(tmp_path):
    path = tmp_path / "config_generation.yaml"
    path.write_text("EXCLUDE_BUILDINGS_WITHOUT_ADDRESS: True\nRESIDENTIAL_ONLY_GENERATION: False\n")
    assert cv.read_flags(path) == {"exclude_buildings_without_address": True, "residential_only": False}
    path.write_text("EXCLUDE_BUILDINGS_WITHOUT_ADDRESS: [broken\n")
    flags = cv.read_flags(path)
    assert set(flags) == {"exclude_buildings_without_address", "residential_only"}   # config_loader values


# ------------------------------------------------------------------ generate gate
class FakeCoverage:
    def __init__(self, statuses):
        self._statuses = statuses

    def ensure(self, plz_list, wait_s=3.0, fresh=False):
        pass

    def statuses(self, plz_list, flags=None, detail=True):
        out = {}
        for p in plz_list:
            out[p] = engine_status(self._statuses.get(p, "ready"), p)
        return out

    def brief_status(self):
        return {"progress": {"eta_s": 12}, "mode": "infdb", "state": "building", "version": 1, "flags_key": "10",
                "building": True, "stale": False, "error": None}


def engine_status(kind: str, plz: int) -> dict:
    if kind == "ready":
        return status(engine(register={plz: [9184137]}, polygons=(plz,),
                             buildings={"09184137": {plz: bc(total=20)}}, ways={"09184137": {plz: [5]}},
                             lines={"09184137": {plz: [5]}}), plz)
    if kind == "pending":
        return status(engine(computed=False, register={plz: [9184137]}, polygons=(plz,)), plz)
    return status(engine(buildings={"09184137": {}}, register={plz: [9184137]}, polygons=(plz,)), plz)


@pytest.fixture()
def gated(monkeypatch):
    monkeypatch.setattr(gate, "generated_plz", lambda version, plz: set())

    def run(statuses, plz, ags=None, **kw):
        options = {"skip_blocked": False, "include_blocked": False, "allow_unverified": False} | kw
        return gate.gate_generate(plz, ags, FakeCoverage(statuses), "1", **options)
    return run


def test_gate_refuses_blocked_plz(gated):
    with pytest.raises(HTTPException) as err:
        gated({85665: "blocked"}, [85653, 85665])
    assert err.value.status_code == 409 and err.value.detail["blocked"][0]["plz"] == 85665


def test_gate_skip_include_and_conflicts(gated):
    out = gated({85665: "blocked"}, [85653, 85665], skip_blocked=True)
    assert out["plz"] == [85653] and out["skipped"] == [85665] and "skipped" in out["notes"][0]
    out = gated({85665: "blocked"}, [85653, 85665], include_blocked=True)
    assert out["plz"] == [85653, 85665] and out["included_blocked"] == [85665]
    with pytest.raises(HTTPException) as err:
        gated({}, [85653], skip_blocked=True, include_blocked=True)
    assert err.value.status_code == 400
    with pytest.raises(HTTPException) as err:
        gated({85665: "blocked"}, [85665], skip_blocked=True)
    assert err.value.status_code == 422


def test_gate_unverified_needs_consent(gated):
    with pytest.raises(HTTPException) as err:
        gated({85658: "pending"}, [85653, 85658])
    assert err.value.status_code == 409 and err.value.detail["unverified"] == [85658]
    assert err.value.detail["eta_s"] == 12
    out = gated({85658: "pending"}, [85653, 85658], allow_unverified=True)
    assert out["plz"] == [85653, 85658] and out["unverified"] == [85658] and not out["use_ags"]


def test_gate_keeps_ags_only_when_every_plz_passes(gated):
    assert gated({}, [85653, 85658], ags=[9184137])["use_ags"]
    out = gated({85658: "blocked"}, [85653, 85658], ags=[9184137], skip_blocked=True)
    assert not out["use_ags"] and out["plz"] == [85653]


def test_existing_results_are_not_gated(monkeypatch):
    monkeypatch.setattr(gate, "generated_plz", lambda version, plz: {85665})
    out = gate.gate_generate([85665], None, FakeCoverage({85665: "blocked"}), "1", skip_blocked=False,
                             include_blocked=False, allow_unverified=False)
    assert out["plz"] == [85665] and not out["skipped"]      # pylovo-generate skips it (exists)


def test_status_texts_cover_every_status():
    texts = cv.status_texts()
    assert {"ready", "pending", "no_buildings", "no_streets"} <= set(texts)
    assert json.dumps(texts)


# ------------------------------------------------------------------ file mode (USE_INFDB=False)
def files_engine(tmp_path, names=("Res_09184137.shp",), logged=(), ways=True, register=None):
    buildings = tmp_path / "data" / "buildings"
    buildings.mkdir(parents=True, exist_ok=True)
    for name in names:
        (buildings / name).write_text("")
    cov = InputCoverage(root=tmp_path)
    cov.mode = "files"
    cov.register = register if register is not None else {85653: [9184137]}
    cov.ags_names = {9184137: "Aying", 9184129: "Hohenbrunn"}
    cov.local_bbox = {85653: (11.7, 47.9, 11.8, 48.0), 85654: (11.7, 47.9, 11.8, 48.0)}

    def query(sql, params=None):
        if "to_regclass" in sql:
            return [(True,)]
        if "FROM pylovo.ags_log" in sql:
            return [(a,) for a in logged]
        if "EXISTS (SELECT 1 FROM pylovo.ways)" in sql:
            return [(ways,)]
        return []
    cov._query = query
    cov.files = cov._read_files(cov.register)
    cov.initialized = True
    return cov


def test_file_mode_shapefiles_and_register(tmp_path, monkeypatch):
    monkeypatch.delenv("PYLOVO_DATA_DIR", raising=False)
    monkeypatch.delenv("PYLOVO_ROOT", raising=False)
    cov = files_engine(tmp_path, names=("Res_09184137.shp", "Oth_09184137.shp"))
    assert status(cov)["status"] == "ready" and status(cov)["exact"]
    st = status(files_engine(tmp_path / "a", names=("Res_09184137.shp",), register={85653: [9184137, 9184129]}))
    assert st["status"] == "ready" and {w["code"] for w in st["warnings"]} == {"partial:buildings", "no_other_buildings"}
    st = status(files_engine(tmp_path / "b", names=()))
    assert st["status"] == "no_buildings" and st["actions"][0]["kind"] == "files"
    assert status(files_engine(tmp_path / "c", names=(), logged=(9184137,)))["status"] == "ready"   # ags_log counts
    assert status(files_engine(tmp_path / "d"), 85654)["status"] == "not_in_register"
    assert status(files_engine(tmp_path / "e", ways=False))["status"] == "no_streets"
    ro = {"exclude_buildings_without_address": False, "residential_only": True}
    assert status(files_engine(tmp_path / "f"), flags=ro)["warnings"] == []                          # no Oth needed
