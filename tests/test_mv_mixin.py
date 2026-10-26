"""MvResultMixin without a database: a scripted fake cursor records every statement (pattern of
``tests/test_bulk_persistence.py``)."""
from __future__ import annotations

import json
import logging
import math
import re
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import shapely

import pylovo.database.mv_result_mixin as mixin
from pylovo.mv_grid.options import MV_STAGE_VERSION
from pylovo.mv_grid.parameters import MvStageError
from pylovo.mv_grid.model import (
    NODE_COLUMNS,
    CaseResult,
    DistrictPlan,
    LinePlan,
    RingCheck,
    RingPlan,
    StationBudget,
    TopologyPlan,
    Violation,
)


def flat(sql: str) -> str:
    return " ".join(str(sql).split())


class FakeCursor:
    """Answers statements by the first rule whose pattern occurs in the SQL; ``nextval`` allocates ids."""

    def __init__(self, rules=(), first_id=1000):
        self.rules = list(rules)
        self.statements: list[tuple[str, object]] = []
        self.rowcount = 1
        self.description = None
        self._rows: list[tuple] = []
        self.next_id = first_id

    def execute(self, query, params=None):
        text = flat(query)
        self.statements.append((text, params))
        self.rowcount, self.description, self._rows = 1, None, []
        if "nextval(pg_get_serial_sequence" in text:
            n = params[0]
            self._rows = [(self.next_id + i,) for i in range(n)]
            self.next_id += n
            return
        for pattern, response in self.rules:
            if pattern in text:
                response = response(text, params) if callable(response) else response
                columns, rows, rowcount = response
                self.description = [(c,) for c in columns] if columns else None
                self._rows = list(rows)
                self.rowcount = rowcount
                return

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def sql(self, pattern: str) -> list[tuple[str, object]]:
        return [(s, p) for s, p in self.statements if pattern in s]


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class Client(mixin.MvResultMixin):
    pass


@pytest.fixture()
def recorded(monkeypatch):
    """execute_values calls of the mixin: (sql, values, template)."""
    calls = []

    def fake_execute_values(cur, query, values, template=None, page_size=100, fetch=False):
        cur.statements.append((flat(query), "execute_values"))
        calls.append((flat(query), list(values), template))

    monkeypatch.setattr(mixin, "execute_values", fake_execute_values)
    return calls


def make_client(rules=(), first_id=1000) -> Client:
    client = object.__new__(Client)
    client.cur = FakeCursor(rules, first_id)
    client.conn = FakeConnection()
    client.logger = logging.getLogger("mv-mixin-test")
    return client


# ---------------------------------------------------------------------------------------------- variants
class FakeParams:
    def __init__(self, variant_id="d0", comment="test", snapshot=None):
        self.variant_id = variant_id
        self.comment = comment
        self._snapshot = snapshot or {"schema": 1, "mv_stage_version": MV_STAGE_VERSION,
                                      "topology": {"max_stations_per_ring": 15}}

    def to_snapshot(self):
        return json.loads(json.dumps(self._snapshot))


LV = SimpleNamespace(as_json=lambda: {"version_id": "mv1", "sim_factor": {"Residential": 0.07}})
VARIANT_COLUMNS = ["version_id", "mv_variant_id", "mv_variant_comment", "mv_parameters", "lv_parameters", "plz_list",
                   "graph_id", "status", "lv_fingerprint", "summary", "created_at", "finished_at"]


def variant_rule(snapshot, plz_list):
    row = ("mv1", "d0", "test", snapshot, {}, plz_list, "g1", "done", {"n_grids": 3}, None, None, None)
    return ("FROM pylovo.mv_variant WHERE version_id = %s AND mv_variant_id = %s", (VARIANT_COLUMNS, [row], 1))


def test_hv_boundary_reads_the_etrago_bus_of_every_supplying_substation():
    columns = ["substation_id", "name", "operator", "source", "etrago_bus_id", "egon_distance_m", "flags", "n_grids",
               "vn_kv", "p_mw", "q_mvar", "peak_load_mw", "n_transformers", "transformer_mva",
               "loading_n0_percent", "loading_n1_percent", "lon", "lat"]
    rows = [(7, "A", None, "osm", 33316, 0.0, [], 1, 20.0, 14.5, -0.5, 13.3, 2, 25.0, 30.0, 60.0, 11.5, 48.5),
            (8, "B", None, "osm", None, None, ["no_egon_bus"], 1, 20.0, 6.0, 0.3, 5.5, 2, 25.0, 12.0, 24.0, 11.6, 48.6)]
    client = make_client([("FROM pylovo.mv_substation s", (columns, rows, 2))])
    table = client.hv_boundary("mv1", "d1")
    assert table["etrago_bus_id"].dtype == "Int64" and table["etrago_bus_id"].isna().tolist() == [False, True]
    assert int(table["etrago_bus_id"].iloc[0]) == 33316 and table["p_mw"].sum() == pytest.approx(20.5)
    sql, params = client.cur.statements[0]
    assert params == ("mv1", "d1") and "load_slack_q_mvar" in sql and "g.status = 'done'" in sql


def test_variant_lock_uses_its_own_key():
    client = make_client([("pg_try_advisory_lock", (["ok"], [(True,)], 1))])
    assert client.try_lock_variant("mv1", "d0") is True
    client.unlock_variant("mv1", "d0")
    (lock_sql, lock_params), (unlock_sql, unlock_params) = client.cur.statements
    assert "hashtext(%s)" in lock_sql and lock_params == (907362, "mv1/d0")
    assert "pg_advisory_unlock" in unlock_sql and unlock_params == (907362, "mv1/d0")


def test_register_new_variant_commits():
    client = make_client([("INSERT INTO pylovo.mv_variant", ([], [], 1))])
    assert client.register_variant("mv1", FakeParams(), [3, 1, 2, 2], "g1", LV, {"n_grids": 3}) == "new"
    sql, params = client.cur.sql("INSERT INTO pylovo.mv_variant")[0]
    assert "ON CONFLICT (version_id, mv_variant_id) DO NOTHING" in sql
    assert params[0:3] == ("mv1", "d0", "test") and params[5] == [1, 2, 3] and params[6] == "g1"
    assert client.conn.commits == 1


def test_register_subset_resumes_and_superset_grows():
    snapshot = FakeParams().to_snapshot()
    client = make_client([("INSERT INTO pylovo.mv_variant", ([], [], 0)), variant_rule(snapshot, [1, 2, 3])])
    assert client.register_variant("mv1", FakeParams(), [1, 2], "g1", LV, {}) == "resume"
    assert client.cur.sql("SET status = 'running', finished_at = NULL")
    assert not client.cur.sql("SET plz_list")

    client = make_client([("INSERT INTO pylovo.mv_variant", ([], [], 0)), variant_rule(snapshot, [1, 2, 3])])
    assert client.register_variant("mv1", FakeParams(), [1, 4], "g2", LV, {"n_grids": 5}) == "grow"
    sql, params = client.cur.sql("SET plz_list")[0]
    assert params[0] == [1, 2, 3, 4] and params[1] == "g2" and params[-2:] == ("mv1", "d0")
    assert client.conn.commits == 1

    # a subset with another routing graph rebuilds: the stored attachments use the edge ids of graph g1
    client = make_client([("INSERT INTO pylovo.mv_variant", ([], [], 0)), variant_rule(snapshot, [1, 2, 3])])
    assert client.register_variant("mv1", FakeParams(), [1, 2], "g2", LV, {}) == "grow"
    sql, params = client.cur.sql("SET plz_list")[0]
    assert params[0] == [1, 2, 3] and params[1] == "g2"


def test_register_rejects_other_parameters():
    stored = {"schema": 1, "mv_stage_version": MV_STAGE_VERSION, "topology": {"max_stations_per_ring": 20}}
    client = make_client([("INSERT INTO pylovo.mv_variant", ([], [], 0)), variant_rule(stored, [1])])
    with pytest.raises(ValueError, match=r"MV parameters differ from the stored snapshot of MV variant d0 of "
                                         r"version mv1 \(topology.max_stations_per_ring\)"):
        client.register_variant("mv1", FakeParams(), [1], "g1", LV, {})
    assert client.conn.rollbacks == 1 and client.conn.commits == 0
    client = make_client([variant_rule(stored, [1])])
    with pytest.raises(ValueError, match="pylovo-mv delete --version mv1 --variant d0"):
        client.check_variant_parameters("mv1", FakeParams())


def test_register_refuses_a_variant_of_another_stage_version():
    older = {"schema": 1, "mv_stage_version": MV_STAGE_VERSION - 1, "topology": {"max_stations_per_ring": 15}}
    message = (f"MV variant d0 of version mv1 was planned with MV stage version {MV_STAGE_VERSION - 1}; plan it "
               "again with --replace or under a new id")
    client = make_client([variant_rule(older, [1])])
    with pytest.raises(MvStageError, match=re.escape(message)):
        client.check_variant_parameters("mv1", FakeParams())
    client = make_client([("INSERT INTO pylovo.mv_variant", ([], [], 0)), variant_rule(older, [1])])
    with pytest.raises(MvStageError, match=re.escape(message)):
        client.register_variant("mv1", FakeParams(), [1], "g1", LV, {})
    assert client.conn.rollbacks == 1 and client.conn.commits == 0


def test_reset_region_and_delete_variants_are_version_scoped():
    client = make_client()
    client.reset_variant_region("mv1", "d0")
    assert all(p == ("mv1", "d0") for _, p in client.cur.statements)
    assert [s.split(" WHERE")[0] for s, _ in client.cur.statements][-1] == "DELETE FROM pylovo.mv_node"

    client = make_client([("DELETE FROM pylovo.mv_variant", ([], [], 2))])
    assert client.delete_variants("mv1", ["d0", "d1"]) == 2
    sql, params = client.cur.statements[1]
    assert "mv_variant_id = ANY(%s)" in sql and params == ("mv1", ["d0", "d1"])
    client = make_client([("DELETE FROM pylovo.mv_variant", ([], [], 5))])
    assert client.delete_variants("mv1", None) == 5
    assert client.cur.statements[1] == ("DELETE FROM pylovo.mv_variant WHERE version_id = %s", ("mv1",))
    assert client.conn.commits == 1


def test_delete_variants_refuses_a_variant_that_is_being_generated():
    client = make_client([("pg_try_advisory_xact_lock", (["version_id", "mv_variant_id"], [("mv1", "d1")], 1))])
    with pytest.raises(RuntimeError, match="MV variant\\(s\\) mv1/d1 are being generated or coupled"):
        client.delete_variants("mv1", ["d0", "d1"])
    lock_sql, params = client.cur.statements[0]
    assert "pg_try_advisory_xact_lock(907362, hashtext(version_id || '/' || mv_variant_id))" in lock_sql
    assert "mv_variant_id = ANY(%s)" in lock_sql and params == ("mv1", ["d0", "d1"])
    assert not client.cur.sql("DELETE FROM") and client.conn.rollbacks == 1 and client.conn.commits == 0


# ---------------------------------------------------------------------------------------------- region writes
def test_insert_nodes_preallocates_ids_and_fills_defaults(recorded):
    client = make_client(first_id=41)
    rows = pd.DataFrame({
        "kind": ["busbar", "station"], "connection_hint": ["busbar", "ring"], "x": [1.0, 2.0], "y": [3.0, 4.0],
        "substation_id": [7, np.nan], "grid_result_id": [np.nan, 99], "load_stats": [None, {"Residential": [5.0, 3]}],
        "gen_solar_kw": [np.nan, 12.0], "flags": [None, ["lv_not_converged"]], "unknown_column": [1, 2],
    })
    ids = client.insert_nodes("mv1", "d0", rows)
    assert ids.tolist() == [41, 42]
    sql, values, template = recorded[0]
    assert sql.startswith("INSERT INTO pylovo.mv_node (mv_node_id, version_id, mv_variant_id, kind,")
    assert "unknown_column" not in sql and template.endswith("ST_SetSRID(ST_MakePoint(%s, %s), 25832))")
    columns = sql.split("(", 1)[1].split(")")[0].split(", ")
    first, second = (dict(zip(columns, v)) for v in values)
    assert first["mv_node_id"] == 41 and first["version_id"] == "mv1" and first["mv_variant_id"] == "d0"
    assert first["substation_id"] == 7 and first["grid_result_id"] is None and first["load_stats"] is None
    assert first["gen_solar_kw"] == 0.0 and first["bridged"] is False and first["flags"] == []
    assert json.loads(second["load_stats"]) == {"Residential": [5.0, 3]} and second["flags"] == ["lv_not_converged"]
    assert values[1][-2:] == (2.0, 4.0)


def test_insert_substations_and_grids(recorded):
    client = make_client(first_id=5)
    sites = pd.DataFrame({"source": ["osm"], "source_ids": [["way/1", "node/2"]], "x": [10.0], "y": [20.0],
                          "egon_bus_id": pd.Series([pd.NA], dtype="Int64"), "in_region": [True], "flags": [[]],
                          "footprint_wkb": [shapely.box(0, 0, 1, 1).wkb], "osm_ratings_mva": [None]})
    assert client.insert_substations("mv1", "d0", sites).tolist() == [5]
    sql, values, template = recorded[0]
    assert values[0][:6] == (5, "mv1", "d0", "osm", ["way/1", "node/2"], None)
    assert values[0][8] is None and values[0][10] is True and isinstance(values[0][-1], bytes)
    assert "ST_CollectionExtract(ST_GeomFromWKB(%s, 25832), 3)" in template
    grids = client.create_grids("mv1", "d0", [5, 9], 20.0)
    assert grids == {5: 6, 9: 7}
    assert recorded[1][1] == [(6, "mv1", "d0", 5, "pending", 20.0), (7, "mv1", "d0", 9, "pending", 20.0)]
    sql, params = client.cur.sql("UPDATE pylovo.mv_node n SET mv_grid_id = g.mv_grid_id")[0]
    assert params == ("mv1", "d0", [6, 7]) and "n.kind = 'busbar'" in sql


def test_attachment_and_grid_updates_are_typed(recorded):
    client = make_client()
    client.update_node_attachments(pd.DataFrame({
        "mv_node_id": [1, 2], "attach_edge_id": [10, 11], "attach_fraction": [0.5, np.nan],
        "attach_offset_m": [0.0, 3.0], "settlement_id": [np.nan, 4], "area_class": ["rural", "urban"],
        "x": [np.nan, 5.0], "y": [np.nan, 6.0], "flags": [[], ["transfer_point"]]}))
    sql, values, template = recorded[0]
    assert "geom = CASE WHEN v.x IS NULL" in sql and "unnest(n.flags || v.flags)" in sql
    assert template.startswith("(%s::bigint, %s::bigint, %s::double precision")
    assert values[0] == (1, 10, 0.5, 0.0, None, "rural", None, None, [])
    assert values[1][-1] == ["transfer_point"]
    assert client.cur.sql("UPDATE pylovo.mv_substation s SET attach_edge_id = n.attach_edge_id")
    client.set_node_grids(pd.DataFrame({"mv_node_id": [1, 2], "mv_grid_id": [6, None], "bridged": [False, True]}))
    assert recorded[1][1] == [(1, 6, False), (2, None, True)]


def test_write_grid_areas_partitions_the_region():
    client = make_client()
    client.write_grid_areas("mv1", "d0", [80801, "80804"])
    texts = [s for s, _ in client.cur.statements]
    stage, stage_params = next((s, p) for s, p in client.cur.statements if "CREATE TEMP TABLE mv_area_point" in s)
    assert "kind IN ('station', 'customer')" in stage and stage_params == {"v": "mv1", "var": "d0"}
    assert any("ON mv_area_point USING gist (geom)" in s for s in texts)    # the cells find their points by index
    sql, params = client.cur.statements[-1]
    assert "ST_VoronoiPolygons(ST_Collect(p.geom), 0.0, (SELECT geom FROM region))" in sql
    assert "JOIN mv_area_point p ON ST_Intersects(v.geom, p.geom)" in sql
    assert "ST_CollectionExtract(ST_Intersection" in sql and "< 0.10" in sql
    assert params == {"v": "mv1", "var": "d0", "plz": [80801, 80804]}


# ---------------------------------------------------------------------------------------------- readers
NODE_ROW_COLUMNS = ["mv_node_id", "mv_grid_id", "substation_id", "kind", "connection_hint", "x", "y",
                    "attach_edge_id", "attach_fraction", "attach_offset_m", "bridged", "settlement_id", "area_class",
                    "plz", "plz_settlement_type", "load_stats", "p_own_kw", "gen_solar_kw", "gen_wind_kw",
                    "gen_other_kw", "sn_kva", "grid_result_id", "lv_design_source_vm_pu", "lv_max_drop_pu"]


def node_rows():
    def row(i, kind, hint, stats, p_own, solar=0.0, area="rural", gr=None):
        return (i, 6, 5 if kind == "busbar" else None, kind, hint, float(i), 0.0, 100 + i, 0.5, 1.0, False,
                None, area, 80801, 2, stats, p_own, solar, 0.0, 0.0, 400.0 if kind == "station" else None, gr,
                None, 0.02)
    return [
        row(12, "customer", "ring", {}, 300.0, area="urban"),
        row(11, "station", "ring", {"Residential": [100.0, 20], "Commercial": [50.0, 1]}, 80.0, solar=30.0, gr=7),
        row(10, "busbar", "busbar", None, None),
        row(13, "generator", "tee", {}, None, solar=500.0),
    ]


def test_load_district_nodes_maps_to_node_columns():
    grid_columns = ["mv_grid_id", "version_id", "mv_variant_id", "substation_id", "vn_kv", "status",
                    "osm_ratings_mva", "in_region", "substation_flags"]
    client = make_client([
        ("FROM pylovo.mv_grid g JOIN pylovo.mv_substation s", (grid_columns,
                                                               [(6, "mv1", "d0", 5, 20.0, "pending", None, True, [])],
                                                               1)),
        ("FROM pylovo.mv_node n WHERE n.mv_grid_id", (NODE_ROW_COLUMNS, node_rows(), 4)),
    ])
    nodes, grid = client.load_district_nodes(6)
    assert grid["substation_id"] == 5 and grid["vn_kv"] == 20.0
    assert list(nodes.columns) == list(NODE_COLUMNS) and nodes.index.name == "mv_node_id"
    assert nodes.index.tolist() == [10, 11, 12, 13]                 # busbar first, then ascending ids
    assert nodes.loc[11, ["inst_res", "units_res", "inst_com", "units_com", "inst_pub"]].tolist() == [
        100.0, 20.0, 50.0, 1.0, 0.0]
    assert nodes.loc[12, "p_fixed_kw"] == 300.0 and nodes.loc[11, "p_fixed_kw"] == 0.0
    assert nodes["urban"].tolist() == [False, False, True, False]
    assert nodes["gen_level"].tolist() == ["", "lv", "", "mv"]
    sql, params = client.cur.sql("FROM pylovo.mv_node n")[0]
    assert "n.kind <> 'joint'" in sql and params == {"g": 6}


def test_station_busbar_customers_are_the_fixed_load_of_the_station():
    frame = pd.DataFrame(node_rows(), columns=NODE_ROW_COLUMNS)
    record = {"n": 2, "kw": 175.0, "installed_kw": 350.0, "categories": {"Commercial": {"n": 2, "kw": 175.0}}}
    stats = {"Residential": [100.0, 20], "busbar_customers": record}
    for value in (stats, json.dumps(stats)):                            # as written and as read from jsonb text
        frame["load_stats"] = [{}, value, None, {}]
        nodes = mixin.node_columns_frame(frame)
        assert nodes.loc[11, "p_fixed_kw"] == 175.0 and nodes.loc[11, ["inst_res", "units_res"]].tolist() == [
            100.0, 20.0]
        assert nodes.loc[12, "p_fixed_kw"] == 300.0                     # customers keep their own fixed load
        assert nodes.loc[[10, 13], "p_fixed_kw"].tolist() == [0.0, 0.0]


def test_plot_readers_never_read_the_net(monkeypatch):
    client = make_client()
    queries = []
    monkeypatch.setattr(Client, "_read_geo", lambda self, q, p, geom_col="geom": queries.append((flat(q), p)))
    layers = client.read_mv_layers("mv1", "d0", mv_grid_id=6)
    assert set(layers) == {"nodes", "lines", "substations", "grids", "settlements"}
    for sql, params in queries:
        assert not re.search(r"\bnet\b", sql), sql
        assert params["v"] == "mv1" and params["var"] == "d0" and params["g"] == 6
        assert "%(v)s" in sql and "%(var)s" in sql
    grids_sql = next(s for s, _ in queries if "FROM pylovo.mv_grid " in s)
    assert "metrics" in grids_sql and "SELECT *" not in grids_sql
    nodes_sql = next(s for s, _ in queries if "FROM pylovo.mv_node " in s and "mv_node_id" in s.split("FROM")[0])
    assert "load_stats" in nodes_sql                                    # the busbar customers for pylovo-mv couple
    queries.clear()
    some = client.read_mv_layers("mv1", "d0", layers=("grids", "nodes"))   # the coupling reads only these
    assert list(some) == ["grids", "nodes"] and len(queries) == 2
    assert "FROM pylovo.mv_grid " in queries[0][0] and "FROM pylovo.mv_node " in queries[1][0]
    with pytest.raises(ValueError, match="unknown MV layer net"):
        client.read_mv_layers("mv1", "d0", layers=("net",))


def test_list_variants_flags_stale_fingerprints():
    columns = ["version_id", "mv_variant_id", "mv_variant_comment", "status", "mv_parameters", "plz_list",
               "graph_id", "lv_fingerprint", "created_at", "finished_at", "n_grids", "n_done", "n_failed",
               "n_stations",
               "circuit_km", "cable_share", "n_electrical_pass", "n_electrical_violations",
               "n_electrical_not_checked"]
    current = {"mv_stage_version": MV_STAGE_VERSION}
    rows = [("mv1", "d0", "a", "done", current, [1, 2], "g", {"n_grids": 3}, None, None, 2, 2, 0, 10,
             4.0, 0.5, 1, 1, 0),
            ("mv1", "d1", "b", "done", '{"mv_stage_version": 12}', [1], "g", {"n_grids": 1}, None, None, 1, 1, 0, 3,
             1.0, 0.5, 0, 0, 1)]
    client = make_client([("LEFT JOIN pylovo.mv_variant_stats s", (columns, rows, 2))])
    client.lv_fingerprint = lambda version_id, plz: {"n_grids": 3}
    df = client.list_variants("mv1")
    assert df["stale"].tolist() == [False, True] and df["n_plz"].tolist() == [2, 1]
    assert df[["n_electrical_pass", "n_electrical_violations", "n_electrical_not_checked"]].values.tolist() == [
        [1, 1, 0], [0, 0, 1]]
    query, params = client.cur.sql("LEFT JOIN pylovo.mv_variant_stats s")[0]
    assert "power_flow_status = 'converged'" in query and "n_electrical_not_checked" in query
    assert "v.mv_parameters" in query and "mv_parameters" not in df.columns
    # the MV stage version of each snapshot (jsonb as dict or text): d1 is an older variant, listed as stored
    assert df["mv_stage_version"].tolist() == [MV_STAGE_VERSION, 12]
    assert params["violations"] == ["voltage_violation", "loading_violation", "not_converged"]
    assert client.cur.sql("SAVEPOINT mv_list_fingerprint")


# ---------------------------------------------------------------------------------------------- district results
def line(key, a, b, role="ring", ring=1, length=100.0, kind="cable", open_at=None, exit_=None):
    geom = shapely.LineString([(float(abs(a)), 0.0), (float(abs(b)), 1.0)])
    return LinePlan(key=key, from_key=a, to_key=b, role=role, kind=kind, feeder_id=1 if exit_ != "b" else 2,
                    ring_id=ring, length_m=length, cable_m=length if kind == "cable" else 0.0,
                    overhead_m=length if kind == "overhead" else 0.0, straight_m=length * 0.8, bridge_m=0.0,
                    geometry=geom, std_type="NA2XS2Y 1x240 RM/25 12/20 kV", normally_open=open_at is not None,
                    open_at_key=open_at, exit=exit_)


def district_plan(extra_line=None) -> DistrictPlan:
    # busbar 10; ring 10-11-(-1)-12-10 with the open point at 12; stub -1 -> 13
    lines = [line(0, 10, 11, exit_="a"), line(1, 11, -1), line(2, -1, 12, open_at=12, length=0.0),
             line(3, 12, 10, exit_="b", kind="overhead"), line(4, -1, 13, role="stub", length=50.0)]
    if extra_line is not None:
        lines.append(extra_line)
    check = RingCheck(True, 2, 2, 300.0, 200.0, 100.0, 0.01, 0.005, 0.02, 0.1, 0.2, 200.0, 150.0, 60.0, None)
    topo = TopologyPlan(
        rings=[RingPlan(1, "ring", [11, -1, 12], 12, check, feeder_a=1, feeder_b=2)], lines=lines,
        joints={-1: (5.0, 6.0)}, node_role={10: "busbar", 11: "ring", 12: "ring", 13: "stub", -1: "joint"},
        node_feeder={11: 1, 12: 2, 13: 1, -1: 1}, node_ring={11: 1, 12: 1, 13: 1, -1: 1},
        node_position={11: 1, 12: 2}, stub_parent={13: -1},
        diagnostics={"node_flags": {12: ["band_conflict", "tap_limit"], 13: ["not_n1_secure"]}})
    keys = [10, 11, 12, 13, -1]
    vm = {"load": [1.02, 1.0, 0.99, 0.985, 0.995], "n1a": [1.02, 0.97, 0.96, 0.95, 0.965],
          "n1b": [1.02, 0.98, 0.95, 0.96, 0.97]}
    cases = {name: CaseResult(name, True, pd.Series(v, index=keys),
                              pd.Series([30.0, 20.0, 0.0, 25.0, 5.0], index=[0, 1, 2, 3, 4]) * (2 if "n1" in name
                                                                                                 else 1),
                              2.0, 0.5) for name, v in vm.items()}
    budgets = {11: StationBudget(11, 1.0, 0.97, math.nan, -1, 0.97, 0.94, math.nan, False, False),
               12: StationBudget(12, 0.99, 0.95, math.nan, 1, 0.95, 0.93, math.nan, True, True)}
    return DistrictPlan(topology=topo, hv_mv_units=2, hv_mv_unit_mva=25.0, hv_mv_sizing="sized", oltc_tap=-1,
                        cases=cases, violations=[Violation("n1b", "voltage_low", "bus", 12, 0.95, 0.92, 1)],
                        repairs=[{"action": "move_nop", "round": 1}], budgets=budgets, net=None,
                        metrics={"n_feeder_bays": 2, "trench_km": 0.3, "bridged_nodes": [13],
                                 "hv_mv_loading_n0_percent": 10.0, "hv_mv_loading_n1_percent": 20.0},
                        power_flow_status="converged", peak_load_mw=1.2)


def district_client() -> Client:
    stored = [(10, "busbar", "rural", None, None, "busbar"), (11, "station", "urban", 400.0, 7, "ring"),
              (12, "station", "urban", 250.0, 8, "ring"), (13, "station", "rural", 100.0, 9, "ring")]
    return make_client([
        ("SELECT mv_node_id, kind, area_class, sn_kva, grid_result_id, connection_hint", (
            ["mv_node_id", "kind", "area_class", "sn_kva", "grid_result_id", "connection_hint"], stored, 4)),
        ("UPDATE pylovo.mv_grid SET status = 'done'", ([], [], 1)),
    ], first_id=500)


def test_save_district_writes_one_transaction(recorded):
    client = district_client()
    client.save_district("mv1", "d0", 6, district_plan(), 12.5, requirements={7: 0.98, 8: 0.975})
    assert client.conn.commits == 1 and client.conn.rollbacks == 0
    deletes = [s for s, _ in client.cur.statements if s.startswith("DELETE")]
    assert deletes[0].startswith("DELETE FROM pylovo.mv_line") and "role <> 'tie'" in deletes[0]
    assert any("kind = 'joint'" in s for s in deletes)
    by_table = {sql.split("(")[0].strip(): (sql, values) for sql, values, _ in recorded}
    joint_sql, joint_values = by_table["INSERT INTO pylovo.mv_node"]
    assert joint_values == [(500, "mv1", "d0", 6, "joint", "joint", "joint", 1, 1, 5.0, 6.0)]
    _, line_values = by_table["INSERT INTO pylovo.mv_line"]
    assert [v[0] for v in line_values] == [501, 502, 503, 504, 505]       # ids pre-allocated in key order
    assert [(v[4], v[5]) for v in line_values] == [(10, 11), (11, 500), (500, 12), (12, 10), (500, 13)]
    assert line_values[2][12] == 0.001 and line_values[2][17] is True and line_values[2][18] == 12
    assert line_values[0][19:22] == (30.0, 60.0, None)                     # load, worst n-1, no feed-in case
    _, ring_values = by_table["INSERT INTO pylovo.mv_ring"]
    ring = ring_values[0]
    assert ring[:9] == ("mv1", "d0", 6, 1, "ring", 2, 2, 0, 0) and ring[9] == 1   # 2 members, 1 stub node
    assert ring[10] == pytest.approx(0.3) and ring[13] == pytest.approx(0.05) and ring[18] == 12
    node_sql = next(s for s, v, _ in recorded if s.startswith("UPDATE pylovo.mv_node n SET connection"))
    node_values = {v[0]: v for s, v_list, _ in recorded if s == node_sql for v in v_list}
    assert node_values[13][1] == "stub" and node_values[13][5] == 500 and node_values[13][14] is True
    assert node_values[12][9:14] == (1, 0.95, 0.93, None, True) and node_values[11][15] == 0.98
    assert node_values[12][6:9] == (0.99, 0.95, None)
    assert node_values[12][16] == ["band_conflict", "tap_limit"] and node_values[13][16] == ["not_n1_secure"]
    assert node_values[11][16] == [] and "unnest(n.flags || v.flags)" in node_sql     # region flags are kept
    grid_sql, grid_params = client.cur.sql("UPDATE pylovo.mv_grid SET status = 'done'")[0]
    assert "metrics = %(metrics)s::jsonb" in grid_sql and grid_params["net"] is None
    assert grid_params["n_stations"] == 3 and grid_params["n_joints"] == 1 and grid_params["n_rings"] == 1
    assert grid_params["circuit_km"] == pytest.approx(0.351) and grid_params["overhead_km"] == pytest.approx(0.1)
    assert grid_params["max_voltage_drop_percent"] == pytest.approx(3.5)
    assert grid_params["max_voltage_drop_n1_percent"] == pytest.approx(7.0)
    assert grid_params["max_voltage_rise_percent"] is None
    assert grid_params["n_band_conflicts"] == 1 and grid_params["n_tap_limit"] == 1
    assert grid_params["sum_station_kva"] == 750.0 and grid_params["runtime_s"] == 12.5
    assert json.loads(grid_params["metrics"])["violations"][0]["key"] == 12
    assert json.loads(grid_params["metrics"])["route_km"] == pytest.approx(0.351)
    assert grid_params["g"] == 6 and grid_params["v"] == "mv1" and grid_params["var"] == "d0"
    sub_sql, sub_params = client.cur.sql("UPDATE pylovo.mv_substation s SET n_transformers")[0]
    assert sub_params["n"] == 2 and sub_params["mva"] == 25.0 and sub_params["n0"] == 10.0


def test_save_district_parallel_inventory_preserves_line_lengths(recorded):
    plan = district_plan()
    plan.topology.lines[0].parallel = 2
    plan.topology.lines[2].parallel = 3  # normally-open minimum-length switch section
    client = district_client()
    client.save_district("mv1", "d0", 6, plan, 12.5)
    _, values, _ = next(call for call in recorded if call[0].startswith("INSERT INTO pylovo.mv_line"))
    assert values[0][11:14] == (2, 0.1, 0.1)
    assert values[2][11:14] == (3, 0.001, 0.0)
    _, grid = client.cur.sql("UPDATE pylovo.mv_grid SET status = 'done'")[0]
    assert grid["circuit_km"] == pytest.approx(0.453)
    assert grid["cable_km"] == pytest.approx(0.35)
    assert grid["overhead_km"] == pytest.approx(0.1)
    assert json.loads(grid["metrics"])["route_km"] == pytest.approx(0.351)


def test_district_voltages_are_set_and_kept_by_the_district_result(recorded):
    client = make_client([("SELECT mv_grid_id, area_km2 FROM pylovo.mv_grid", (["g", "a"], [(6, 0.25), (7, None)], 2))])
    areas = client.grid_areas("mv1", "d0")
    assert areas[6] == 0.25 and np.isnan(areas[7])
    voltages = pd.DataFrame({"vn_kv": [10.0, 20.0]}, index=pd.Index([6, 7], name="mv_grid_id"))
    client.set_grid_voltages("mv1", "d0", voltages, {6: {"rule": "auto", "density_mva_km2": 8.12}})
    sql, values, template = recorded[-1]
    assert "SET vn_kv = v.vn_kv" in sql and "jsonb_build_object('voltage_level', v.record)" in sql
    assert values == [(6, 10.0, '{"density_mva_km2": 8.12, "rule": "auto"}', "mv1", "d0"),
                      (7, 20.0, "{}", "mv1", "d0")]
    assert "g.version_id = v.version_id" in sql and template.count("%s") == 5
    district = district_client()
    district.save_district("mv1", "d0", 6, district_plan(), 1.0)
    grid_sql, _ = district.cur.sql("UPDATE pylovo.mv_grid SET status = 'done'")[0]
    assert "metrics ? 'voltage_level'" in grid_sql and "metrics -> 'voltage_level'" in grid_sql


def test_save_district_rolls_back_on_an_unknown_node_key(recorded):
    client = district_client()
    plan = district_plan(extra_line=line(5, 10, 99))
    with pytest.raises(ValueError, match="node key 99"):
        client.save_district("mv1", "d0", 6, plan, 1.0)
    assert client.conn.rollbacks == 1 and client.conn.commits == 0


def test_stored_net_gets_the_database_ids():
    import pandapower as pp

    net = pp.create_empty_network()
    buses = [pp.create_bus(net, 20.0) for _ in range(3)]
    net.bus["node_key"] = [10, -1, np.nan]
    pp.create_line_from_parameters(net, buses[0], buses[1], 1.0, 0.1, 0.1, 0.0, 0.4)
    net.line["line_key"] = [3]
    text = mixin._net_json(net, {10: 10, -1: 500}, {3: 503})
    back = pp.from_json_string(text)
    assert back.bus["mv_node_id"].tolist()[:2] == [10.0, 500.0] and np.isnan(back.bus["mv_node_id"].iloc[2])
    assert back.line["mv_line_id"].tolist() == [503.0]


def test_failed_grid_and_finished_variant_commit():
    client = make_client()
    client.mark_grid_failed(6, "ValueError: line one\n   line two")
    assert client.cur.statements[0][1] == ("ValueError: line one line two", 6) and client.conn.commits == 1
    summary = SimpleNamespace(status="partial", to_dict=lambda: {"status": "partial", "km": float("nan")})
    client.finish_variant("mv1", "d0", summary)
    sql, params = client.cur.statements[1]
    assert "finished_at = now()" in sql and params[0] == "partial" and json.loads(params[1])["km"] is None


def test_variant_totals_and_pending_grids():
    totals = (3, 2, 1, 0, 40, 2, 50.0, 30.0, 1, 4)
    client = make_client([("FROM pylovo.mv_grid WHERE version_id", (["c"] * 10, [totals], 1)),
                          ("GROUP BY g.vn_kv", (["vn_kv", "n"], [(20.0, 2), (10.0, 1)], 2)),
                          ("GROUP BY g.mv_grid_id", (["g", "n"], [(7, 30), (6, 10)], 2))])
    out = client.variant_totals("mv1", "d0")
    assert out["grids_total"] == 3 and out["cable_share"] == pytest.approx(0.6) and out["n_band_conflicts"] == 4
    assert "sum(circuit_km ORDER BY mv_grid_id)" in client.cur.statements[0][0]     # independent of the row order
    assert out["grids_by_kv"] == {"20": 2, "10": 1}
    assert client.pending_grids("mv1", "d0") == [(7, 30), (6, 10)]
    assert "status IN ('pending', 'failed')" in client.cur.statements[-1][0]


# ---------------------------------------------------------------------------------------- display geometry
def test_display_input_keys_paths_by_grid_ring_and_role():
    from shapely import LineString

    line = LineString([(0, 0), (10, 0)]).wkb
    client = make_client([
        ("FROM pylovo.mv_line", (["id"], [(1, 11, 12, 5, 2, "ring", line), (2, 12, 13, 5, None, "tie", line)], 2)),
        ("kind = 'busbar'", (["id"], [(11, 0.0, 0.0)], 1)),
    ])
    lines, busbars = client.display_input("mv1", "d1")
    assert [(ln.id, ln.from_node, ln.to_node, ln.path_key) for ln in lines] == [(1, 11, 12, (5, 2, "ring")),
                                                                                (2, 12, 13, None)]
    assert busbars == {11: (0.0, 0.0)}


def test_save_display_geometry_clears_the_variant_then_writes_lines_and_busbar_circles(recorded):
    from shapely import LineString, Point

    client = make_client()
    client.save_display_geometry("mv1", "d1", {7: LineString([(0, 0), (1, 1)])}, {11: Point(0, 0).buffer(6)})
    clears = client.cur.sql("SET display_geom = NULL")
    assert [p for _, p in clears] == [("mv1", "d1"), ("mv1", "d1")]
    assert "pylovo.mv_line" in clears[0][0] and "pylovo.mv_substation" in clears[1][0]
    (line_sql, line_values, _), (bus_sql, bus_values, _) = recorded
    assert "UPDATE pylovo.mv_line l SET display_geom" in line_sql and line_values[0][0] == 7
    assert "JOIN pylovo.mv_node n ON n.mv_node_id = v.mv_node_id" in bus_sql and bus_values[0][0] == 11
    assert client.conn.commits == 1


def test_a_failed_coupling_clears_the_coupled_values_of_an_earlier_run():
    client = make_client()
    client.save_coupled_results(5, pd.DataFrame({"mv_node_id": []}), "failed", {"error": "LV net 7 is broken"})
    texts = [text for text, _ in client.cur.statements]
    clear = next(i for i, text in enumerate(texts) if text.startswith("UPDATE pylovo.mv_node SET"))
    assert all(f"{c} = NULL" in texts[clear] for c in mixin.COUPLED_COLUMNS) and texts[clear].endswith(
        "WHERE mv_grid_id = %s")
    assert client.cur.statements[clear][1] == (5,)
    assert clear < next(i for i, text in enumerate(texts) if "UPDATE pylovo.mv_grid" in text)
    assert client.conn.commits == 1


def test_lv_fingerprint_counts_only_the_active_load_edits_of_its_plz():
    """A load edit in another PLZ of the version does not mark the variant stale; an undone edit does."""
    from pylovo.database.mv_input_mixin import MvInputMixin

    client = object.__new__(type("InputClient", (MvInputMixin,), {}))
    client.cur = FakeCursor([("FROM pylovo.grid_result", (["n", "s"], [(2, 21)], 1)),
                             ("FROM pylovo.load_edit", (["m"], [("a1b2",)], 1)),
                             ("FROM pylovo.mv_customers", (["n"], [(3,)], 1))])
    assert client.lv_fingerprint("v1", [80803]) == {"n_grids": 2, "sum_grid_result_id": 21,
                                                     "active_load_edits": "a1b2", "n_mv_customers": 3}
    [(query, params)] = client.cur.sql("FROM pylovo.load_edit")
    assert "plz = ANY" in query and params["v"] == "v1" and list(params["plz"]) == [80803]
    assert "undone_at IS NULL" in query and "string_agg(load_edit_id::text, ',' ORDER BY load_edit_id)" in query


def test_lv_fingerprint_changes_when_a_load_edit_is_undone():
    """Edit, variant, undo: the undo only stamps ``undone_at``, so the fingerprint must leave out undone edits."""
    import hashlib

    from pylovo.database.mv_input_mixin import MvInputMixin

    edits = [{"id": 9, "plz": 80803, "undone": False}, {"id": 10, "plz": 80803, "undone": False},
             {"id": 11, "plz": 80802, "undone": False}]

    class EditCursor:
        def execute(self, query, params=None):
            text = " ".join(query.split())
            if "FROM pylovo.load_edit" in text:
                ids = [e["id"] for e in edits if e["plz"] in params["plz"]
                       and not (e["undone"] and "undone_at IS NULL" in text)]
                self.row = (hashlib.md5(",".join(map(str, sorted(ids))).encode()).hexdigest(),) \
                    if "md5(" in text else (max(ids, default=None),)
            else:
                self.row = (2, 21) if "grid_result" in text else (0,)

        def fetchone(self):
            return self.row

    client = object.__new__(type("InputClient", (MvInputMixin,), {}))
    client.cur = EditCursor()
    built = client.lv_fingerprint("v1", [80803])
    edits[0]["undone"] = True                                   # undo the older edit: the newest stays active
    assert client.lv_fingerprint("v1", [80803]) != built
    edits[0]["undone"] = False                                  # the same active edits, the same fingerprint
    assert client.lv_fingerprint("v1", [80803]) == built
    edits[2]["undone"] = True                                   # an undo in another PLZ changes nothing
    assert client.lv_fingerprint("v1", [80803]) == built


def test_lv_requirement_and_population_readers():
    from pylovo.database.mv_input_mixin import POPULATION_COLUMNS, MvInputMixin

    client = object.__new__(type("InputClient", (MvInputMixin,), {}))
    client.cur = FakeCursor([("FROM pylovo.grid_result gr", (["g", "vm"], [(5, 1.01), (6, None)], 2)),
                             ("FROM unnest(%(plz)s::integer[])", (POPULATION_COLUMNS, [(80803, 9000, 2.0, None)], 1))])
    assert client.lv_design_source_vm("v1", [6, 5, 5]) == {5: 1.01}            # a grid without ext_grid is missing
    [(query, params)] = client.cur.sql("FROM pylovo.grid_result gr")
    assert "ce.name = 'vm_pu'" in query and params == {"v": "v1", "ids": [5, 6]}
    population = client.plz_population([80803])
    assert population.columns.tolist() == list(POPULATION_COLUMNS) and math.isnan(population["polygon_km2"].iloc[0])


# ---------------------------------------------------------------------------------------------- register + region
class TransactionalTables:
    """The committed and the working state of ``mv_variant`` and ``mv_substation`` (rollback restores the last
    commit), for the real registration, region and finish methods with the real ``stage._run_locked``."""

    COLUMNS = ["version_id", "mv_variant_id", "mv_variant_comment", "mv_parameters", "lv_parameters", "plz_list",
               "graph_id", "status", "lv_fingerprint", "summary", "created_at", "finished_at"]

    def __init__(self):
        self.state = {"variants": {}, "subs": []}
        self.committed = json.loads(json.dumps(self.state))

    def commit(self):
        self.committed = json.loads(json.dumps(self.state))

    def rollback(self):
        self.state = json.loads(json.dumps(self.committed))

    def execute(self, query, params=None):
        text, st = flat(query), self.state
        self.description, self._rows = None, []
        self.rowcount = 0
        key = "/".join(map(str, params[-2:])) if params else ""
        if text.startswith("INSERT INTO pylovo.mv_variant"):
            key = f"{params[0]}/{params[1]}"
            if key not in st["variants"]:
                st["variants"][key] = dict(zip(self.COLUMNS, [params[0], params[1], params[2], json.loads(params[3]),
                                                              {}, list(params[5]), params[6], "running",
                                                              json.loads(params[7]), None, None, None]))
                self.rowcount = 1
        elif text.startswith("SELECT version_id, mv_variant_id") and "FROM pylovo.mv_variant WHERE" in text:
            row = st["variants"].get(f"{params[0]}/{params[1]}")
            self.description = [(c,) for c in self.COLUMNS]
            self._rows = [tuple(row[c] for c in self.COLUMNS)] if row else []
        elif text.startswith("UPDATE pylovo.mv_variant SET status = 'running'"):
            st["variants"][key]["status"] = "running"
        elif text.startswith("UPDATE pylovo.mv_variant SET plz_list"):
            st["variants"][key].update(plz_list=list(params[0]), graph_id=params[1], status="running")
        elif text.startswith("UPDATE pylovo.mv_variant SET status = %s"):
            st["variants"][key]["status"] = params[0]
        elif text.startswith("SELECT EXISTS (SELECT 1 FROM pylovo.mv_substation"):
            self._rows = [(any(s[0] == key for s in st["subs"]),)]
        elif text.startswith("DELETE FROM pylovo.mv_substation"):
            st["subs"] = [s for s in st["subs"] if s[0] != key]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class RegionClient(mixin.MvResultMixin):
    """The real ``register_variant``, ``variant_region_built``, ``reset_variant_region`` and ``finish_variant`` on
    :class:`TransactionalTables`; the LV reads are stubbed."""

    def __init__(self):
        self.cur = self.tables = TransactionalTables()
        self.conn = SimpleNamespace(commit=self.tables.commit, rollback=self.tables.rollback)
        self.logger = logging.getLogger("mv-region-test")
        self.graph_id, self.fail_region, self.modes = "g1", False, []

    def register_variant(self, *args, **kwargs):
        self.modes.append(super().register_variant(*args, **kwargs))
        return self.modes[-1]

    def generated_plz(self, version_id, plz_list):
        return list(plz_list)

    def busy_plz(self, plz_list):
        return []

    def lv_snapshot(self, version_id):
        return {}

    def planning_region(self, plz_list):
        return b""

    def ensure_graph(self, region_wkb, params):
        return self.graph_id

    def lv_fingerprint(self, version_id, plz_list):
        return {"n_grids": len(plz_list)}

    def commit_changes(self):
        self.tables.commit()

    def rollback_changes(self):
        self.tables.rollback()

    def pending_grids(self, version_id, variant_id):
        return []

    def variant_totals(self, version_id, variant_id):
        return {"grids_total": 1, "grids_done": 1}


@pytest.fixture()
def region_steps(monkeypatch):
    """``stage._run_locked`` with a region step that stores one substation named after its PLZ and graph."""
    from pylovo.mv_grid import stage

    built = []

    def region_step(dbc, version_id, variant_id, plz, region_wkb, graph_id, *args):
        built.append((list(plz), graph_id))
        dbc.tables.state["subs"].append((f"{version_id}/{variant_id}", f"PLZ {list(plz)} on {graph_id}"))
        if dbc.fail_region:
            raise RuntimeError("no HV/MV substation is within reach")
        return {}

    for name, value in (("_region_step", region_step), ("_run_districts", lambda *a, **k: []),
                        ("_plan_ties", lambda *a, **k: None), ("_plan_links", lambda *a, **k: None),
                        ("display_geometry", lambda *a, **k: None), ("_demand_anchor", lambda *a, **k: {}),
                        ("_osm_coverage", lambda *a, **k: {})):
        monkeypatch.setattr(stage, name, value)
    monkeypatch.setattr(stage.LvVersionParameters, "from_generation_parameters",
                        classmethod(lambda cls, v, s: SimpleNamespace(as_json=lambda: {"version_id": v})))

    def run(dbc, plz):
        from pylovo.mv_grid.parameters import MvParameters

        return stage._run_locked(dbc, "mv1", MvParameters().replace(variant_id="d0"), plz, parallel=False, jobs=1,
                                 replace=False, log_file="log/x.txt", logger=logging.getLogger("t"), start=0.0)
    return run, built


def test_a_failed_variant_extension_is_rebuilt_by_the_next_run(region_steps):
    run, built = region_steps
    dbc = RegionClient()
    assert run(dbc, [1]).status == "done"                                     # new variant: PLZ 1 on graph g1
    dbc.graph_id, dbc.fail_region = "g2", True
    with pytest.raises(RuntimeError):                                         # grow to PLZ 1+2: the region fails
        run(dbc, [1, 2])
    assert dbc.tables.state["subs"] == []                                     # the old region went with the grow
    dbc.fail_region = False
    built.clear()
    summary = run(dbc, [1, 2])                                                # the same command again
    assert dbc.modes == ["new", "grow", "resume"] and built == [([1, 2], "g2")] and summary.status == "done"
    assert [s[1] for s in dbc.tables.state["subs"]] == ["PLZ [1, 2] on g2"]

