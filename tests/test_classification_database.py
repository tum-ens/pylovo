"""``DatabaseCommunication`` and the filter step against a fake cursor, plus static SQL-to-DDL consistency checks."""
import inspect
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

import pylovo.classification.clustering.filter_grids as filter_grids
import pylovo.classification.database_communication.database_communication as dcm
from classification_helpers import FakeDbc, ddl_columns, synthetic_clustering_parameters
from pylovo.config_loader import CLUSTERING_PARAMETERS, MUNICIPAL_REGISTER
from pylovo.database.config_table_structure import CREATE_QUERIES

PARAMS = ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"]


def make_dc() -> dcm.DatabaseCommunication:
    dc = object.__new__(dcm.DatabaseCommunication)  # skip __init__: no connection
    dc.dbc = FakeDbc()
    return dc


# ---------------------------------------------------------------------------------------- filter statements
def test_threshold_updates_use_the_configured_values():
    dc = make_dc()
    dc.apply_max_trafo_dis_threshold()
    dc.apply_households_per_building_threshold()
    dc.apply_list_of_clustering_parameters_thresholds()
    dc.set_remaining_filter_values_false()
    (q1, p1), (q2, p2), (q3, p3), (q4, p4) = dc.dbc.cur.executed
    assert "UPDATE pylovo.clustering_parameters SET filtered = true WHERE max_trafo_dis > %(t)s" in q1
    assert p1 == {"t": dcm.THRESHOLD_MAX_TRAFO_DIS}
    assert "FROM pylovo.buildings_result WHERE households > %(h)s" in q2
    assert p2 == {"h": dcm.THRESHOLD_HOUSEHOLDS_PER_BUILDING}
    assert p3 == {"avg_trafo_dis": dcm.THRESHOLD_AVG_TRAFO_DIS, "no_house_connections": dcm.THRESHOLD_NO_HOUSE_CONNECTIONS,
                  "vsw_per_branch": dcm.THRESHOLD_VSW_PER_BRANCH, "no_households": dcm.THRESHOLD_NO_HOUSEHOLDS}
    assert q3.count(" OR ") == 3 and q3.count("<") == 4
    assert "SET filtered = false WHERE filtered IS NULL" in q4 and p4 is None
    assert dc.dbc.conn.commits == 4


@pytest.mark.parametrize("additional", [False, True])
def test_filter_steps_run_in_order(monkeypatch, additional):
    calls = []

    class Recorder:
        def __getattr__(self, name):
            return lambda *a, **k: calls.append(name)

    monkeypatch.setattr(filter_grids, "DatabaseCommunication", Recorder)
    filter_grids.apply_filter_to_grids(additional_filtering=additional)
    expected = ["apply_max_trafo_dis_threshold", "apply_households_per_building_threshold"]
    expected += ["apply_list_of_clustering_parameters_thresholds"] if additional else []
    assert calls == expected + ["set_remaining_filter_values_false"]


@pytest.mark.xfail(strict=True, reason="CLASSIF-04: the filter UPDATEs are not scoped to the grids of the classification")
def test_filter_updates_are_scoped_to_the_classified_grids():
    dc = make_dc()
    dc.apply_max_trafo_dis_threshold()
    dc.apply_households_per_building_threshold()
    dc.apply_list_of_clustering_parameters_thresholds()
    for query, _ in dc.dbc.cur.executed:
        assert "version_id" in query or "sample_set" in query


# ---------------------------------------------------------------------------------------- reading parameters
def test_clustering_parameters_are_returned_in_the_configured_columns(monkeypatch):
    raw = synthetic_clustering_parameters().assign(grid_result_id=1, filtered=False)  # the SQL also returns these
    seen = {}

    def fake_read(query, con=None, params=None):
        seen.update(query=query, params=params)
        return raw

    monkeypatch.setattr(dcm.pd, "read_sql_query", fake_read)
    out = make_dc().get_clustering_parameters_for_classification_version()
    assert list(out.columns) == CLUSTERING_PARAMETERS
    assert seen["params"] == {"v": dcm.VERSION_ID, "c": dcm.CLASSIFICATION_VERSION}
    assert "cp.filtered = false" in " ".join(seen["query"].split())


def test_both_clustering_queries_are_ordered_by_the_grid_key(monkeypatch):
    """The clusters depend on the row order, which the database leaves unspecified without ORDER BY."""
    queries = []
    monkeypatch.setattr(dcm.pd, "read_sql_query", lambda query, con=None, params=None: queries.append(query) or synthetic_clustering_parameters())
    dc = make_dc()
    dc.get_clustering_parameters_for_classification_version()
    dc.municipal_register_with_clustering_parameters_for_classification_version()
    assert len(queries) == 2
    for query in queries:
        assert " ".join(query.split()).endswith("ORDER BY c.plz, c.kcid, c.bcid;")


# ---------------------------------------------------------------------------------------- writing the result
def prepare_save(monkeypatch, fail=False):
    """Fake the database around ``save_transformers_with_classification_info`` and return ``(dc, captured, df)``."""
    df = synthetic_clustering_parameters()
    positions = gpd.GeoDataFrame({"version_id": df["version_id"], "plz": df["plz"], "kcid": df["kcid"], "bcid": df["bcid"],
                                  "geom": [Point(11 + i * 1e-3, 48) for i in range(len(df))]}, geometry="geom")
    grid_result = pd.DataFrame({"grid_result_id": np.arange(100, 100 + len(df)), "version_id": df["version_id"],
                                "plz": df["plz"], "kcid": df["kcid"], "bcid": df["bcid"]})
    captured = {}

    dc = make_dc()

    def fake_to_sql(self, name, con=None, **kw):
        captured.update(name=name, frame=self.copy(), kw=kw, con=con)
        dc.dbc.sqla_engine.log.append(("to_sql",))
        if fail:
            raise RuntimeError("write failed")

    monkeypatch.setattr(dcm.gpd, "read_postgis", lambda *a, **k: positions.copy())
    monkeypatch.setattr(dcm.pd, "read_sql_query", lambda *a, **k: grid_result.copy())
    monkeypatch.setattr(pd.DataFrame, "to_sql", fake_to_sql)
    dc.get_clustering_parameters_for_classification_version = lambda: df.copy()
    return dc, captured, df


@pytest.fixture()
def run_save(monkeypatch):
    dc, captured, df = prepare_save(monkeypatch)
    dc.save_transformers_with_classification_info(PARAMS, 4, 4)
    return dc, captured, df


def test_transformer_classified_frame_matches_the_table(run_save):
    dc, captured, df = run_save
    frame = captured["frame"]
    assert captured["name"] == "transformer_classified" and captured["kw"]["if_exists"] == "append"
    assert set(frame.columns) == set(ddl_columns(CREATE_QUERIES["transformer_classified"]))
    assert len(frame) == len(df) and frame["grid_result_id"].is_unique
    assert (frame["classification_id"] == dcm.CLASSIFICATION_VERSION).all()
    assert frame["kmedoid_clusters"].isna().all() and not frame["kmedoid_representative_grid"].any()


def test_a_repeated_run_replaces_the_rows_of_the_classification_in_one_transaction(run_save):
    """The duplicate-key error of a second run is gone: the old rows are deleted in the transaction of the insert."""
    dc, captured, _ = run_save
    log = dc.dbc.sqla_engine.log
    assert [entry[0] for entry in log] == ["begin", "execute", "to_sql", "commit"]
    _, statement, params = log[1]
    assert statement == "DELETE FROM pylovo.transformer_classified WHERE classification_id = :c"
    assert params == {"c": dcm.CLASSIFICATION_VERSION}
    assert captured["con"] is dc.dbc.sqla_engine.connection, "the insert uses the connection of the transaction"


def test_a_failing_write_rolls_the_delete_back_and_leaves_the_view_alone(monkeypatch):
    dc, _, _ = prepare_save(monkeypatch, fail=True)
    with pytest.raises(RuntimeError, match="write failed"):
        dc.save_transformers_with_classification_info(PARAMS, 4, 4)
    assert [entry[0] for entry in dc.dbc.sqla_engine.log] == ["begin", "execute", "to_sql", "rollback"]
    assert dc.dbc.refreshed == 0 and dc.dbc.conn.commits == 0


def test_every_cluster_has_exactly_one_representative(run_save):
    _, captured, _ = run_save
    frame = captured["frame"]
    for algorithm in ("kmeans", "gmm"):
        assert sorted(frame[f"{algorithm}_clusters"].unique()) == [0, 1, 2, 3]
        reps = frame[frame[f"{algorithm}_representative_grid"]]
        assert sorted(reps[f"{algorithm}_clusters"]) == [0, 1, 2, 3]


def test_geometry_is_written_as_wkt_with_the_target_srid_and_the_view_is_refreshed(run_save):
    dc, captured, _ = run_save
    geom = captured["frame"]["geom"].iloc[0]
    assert type(geom).__name__ == "WKTElement" and geom.srid == dcm.TARGET_EPSG
    assert "Geometry" in repr(captured["kw"]["dtype"]["geom"]) and dc.dbc.refreshed == 1 and dc.dbc.conn.commits == 1


def test_municipal_register_variant_has_the_register_columns(monkeypatch):
    raw = synthetic_clustering_parameters()
    monkeypatch.setattr(dcm.pd, "read_sql_query", lambda *a, **k: raw)
    assert make_dc().municipal_register_with_clustering_parameters_for_classification_version() is raw
    source = inspect.getsource(dcm.DatabaseCommunication.municipal_register_with_clustering_parameters_for_classification_version)
    assert "mr.regio7" in source and "mr.pop_den" in source


# ---------------------------------------------------------------------------------------- SQL against the DDL
def table_columns(name: str) -> set[str]:
    return set(ddl_columns(CREATE_QUERIES[name]))


def test_every_table_the_classification_sql_names_exists_in_the_ddl():
    import pylovo.classification.sampling.sample as sample
    from pylovo.classification.utils import get_average_values_clustering_parameters as avg
    from pylovo.database.analysis_mixin import AnalysisMixin
    from pylovo.database.results_mixin import ResultsMixin

    text = "".join(inspect.getsource(m) for m in (dcm, sample, avg, AnalysisMixin.insert_clustering_parameters,
                                                  AnalysisMixin.has_clustering_parameters,
                                                  ResultsMixin.delete_classification_version_from_related_tables,
                                                  ResultsMixin.delete_plz_from_sample_set_table))
    tables = set(re.findall(r"(?:FROM|JOIN|UPDATE|INTO)\s+pylovo\.([a-z_]+)", text))
    missing = {t for t in tables if t not in CREATE_QUERIES}
    assert not missing, missing


def test_clustering_parameter_columns_exist_in_the_ddl():
    join_columns = {"version_id", "plz", "bcid", "kcid"}  # come from grid_result in the SELECT
    cols = table_columns("clustering_parameters")
    assert set(CLUSTERING_PARAMETERS) - join_columns <= cols
    assert {"grid_result_id", "filtered"} <= cols
    assert {"version_id", "plz", "kcid", "bcid"} <= table_columns("grid_result")
    assert {"households", "grid_result_id"} <= table_columns("buildings_result")
    assert {"grid_result_id", "geom"} <= table_columns("transformer_positions")


def test_insert_of_clustering_parameters_uses_ddl_columns():
    from pylovo.database.analysis_mixin import AnalysisMixin

    source = inspect.getsource(AnalysisMixin.insert_clustering_parameters)
    columns = [c.strip() for c in re.search(r"INSERT INTO pylovo.clustering_parameters \((.*?)\)\s+VALUES", source, re.S).group(1).split(",")]
    assert set(columns) <= table_columns("clustering_parameters") and "filtered" not in columns


def test_municipal_register_config_matches_the_table_order():
    """``SELECT *`` plus ``columns=MUNICIPAL_REGISTER`` relies on the column order of the DDL."""
    assert ddl_columns(CREATE_QUERIES["municipal_register"]) == MUNICIPAL_REGISTER


def test_sample_set_references_the_register_and_the_version():
    ddl = CREATE_QUERIES["sample_set"]
    assert "REFERENCES pylovo.classification_version (classification_id)" in ddl
    assert "REFERENCES pylovo.municipal_register (plz, ags)" in ddl
    assert "ags bigint NOT NULL" in ddl
