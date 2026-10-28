"""``DatabaseCommunication`` and the filter step against a fake cursor, plus static SQL-to-DDL consistency checks."""
import inspect
import re

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import Point

import pylovo.classification.database_communication.database_communication as dcm
from classification_helpers import FakeCursor, FakeDbc, ddl_columns, synthetic_clustering_parameters
from pylovo.config_loader import CLUSTERING_PARAMETERS, MUNICIPAL_REGISTER
from pylovo.database.config_table_structure import CREATE_QUERIES

PARAMS = ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"]


def make_dc() -> dcm.DatabaseCommunication:
    dc = object.__new__(dcm.DatabaseCommunication)  # skip __init__: no connection
    dc.dbc = FakeDbc()
    return dc


# ---------------------------------------------------------------------------------------- filter conditions
READERS = ["get_clustering_parameters_for_classification_version",
           "municipal_register_with_clustering_parameters_for_classification_version"]


def read_rows(stored=None, rows=None) -> pd.DataFrame:
    """What the readers' SQL returns: key figures of version ``rows`` and the version ``stored`` with the
    classification (``None``: the current version, and none stored)."""
    return synthetic_clustering_parameters().assign(
        key_figures_version=dcm.KEY_FIGURES_VERSION if rows is None else rows, classification_key_figures_version=stored)


def capture_queries(monkeypatch):
    seen = []

    def fake_read(query, con=None, params=None):
        seen.append((" ".join(query.split()), params))
        return read_rows()

    monkeypatch.setattr(dcm.pd, "read_sql_query", fake_read)
    return seen


@pytest.mark.parametrize("reader", READERS)
def test_readers_apply_the_default_filters_with_the_configured_thresholds(monkeypatch, reader):
    seen = capture_queries(monkeypatch)
    getattr(make_dc(), reader)(False)
    ((query, params),) = seen
    assert "cp.max_trafo_dis > %(max_trafo_dis)s" in query
    assert "br.households > %(households_per_building)s" in query
    assert "avg_trafo_dis <" not in query, "the lower bounds belong to additional filtering"
    assert params == {"v": dcm.VERSION_ID, "c": dcm.CLASSIFICATION_VERSION, **dcm.FILTER_PARAMETERS}
    assert params["max_trafo_dis"] == dcm.THRESHOLD_MAX_TRAFO_DIS
    assert params["households_per_building"] == dcm.THRESHOLD_HOUSEHOLDS_PER_BUILDING


@pytest.mark.parametrize("reader", READERS)
def test_readers_add_the_four_lower_bounds_with_additional_filtering(monkeypatch, reader):
    seen = capture_queries(monkeypatch)
    getattr(make_dc(), reader)(True)
    ((query, params),) = seen
    lower = {"avg_trafo_dis": dcm.THRESHOLD_AVG_TRAFO_DIS, "no_house_connections": dcm.THRESHOLD_NO_HOUSE_CONNECTIONS,
             "vsw_per_branch": dcm.THRESHOLD_VSW_PER_BRANCH, "no_households": dcm.THRESHOLD_NO_HOUSEHOLDS}
    assert dcm.LOWER_FILTER_THRESHOLDS == lower
    for parameter, threshold in lower.items():
        assert f"cp.{parameter} < %({parameter})s" in query and params[parameter] == threshold
    assert query.count("NOT COALESCE((") == 6, "every condition is NULL-safe"


@pytest.mark.parametrize("additional", [False, True])
def test_every_placeholder_of_the_filter_has_a_value(monkeypatch, additional):
    seen = capture_queries(monkeypatch)
    make_dc().get_clustering_parameters_for_classification_version(additional)
    ((query, params),) = seen
    assert set(re.findall(r"%\((\w+)\)s", query)) <= set(params)


def test_the_filter_is_scoped_to_the_classified_grids_and_stores_nothing(monkeypatch):
    """CLASSIF-04: no UPDATE of the grids of every version and classification any more, the flag is not read."""
    seen = capture_queries(monkeypatch)
    make_dc().get_clustering_parameters_for_classification_version(True)
    ((query, _),) = seen
    assert "FROM pylovo.sample_set WHERE classification_id= %(c)s" in query and "gr.version_id = %(v)s" in query
    assert "cp.filtered" not in query
    assert "UPDATE pylovo.clustering_parameters" not in inspect.getsource(dcm)


# ---------------------------------------------------------------------------------------- choices of a classification
def stored_dc(row) -> dcm.DatabaseCommunication:
    """A ``DatabaseCommunication`` whose classification row holds ``row`` (``None``: no row)."""
    dc = make_dc()
    dc.dbc.cur = FakeCursor(fetchone_values=[row])
    return dc


def test_the_stored_choices_of_the_classification_win_over_the_configuration(monkeypatch):
    """The configuration holds the choices of the last run: classification 9002 must not change those of 9001."""
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", True)
    dc = stored_dc((["no_branches", "avg_trafo_dis"], 5, 3, False))
    assert dc.get_classification_choices() == {"list_of_clustering_parameters": ["no_branches", "avg_trafo_dis"],
                                               "n_clusters_kmeans": 5, "n_clusters_gmm": 3, "additional_filtering": False}
    ((query, params),) = dc.dbc.cur.executed
    assert query == ("SELECT list_of_clustering_parameters, n_clusters_kmeans, n_clusters_gmm, additional_filtering "
                     "FROM pylovo.classification_version WHERE classification_id = %(c)s")
    assert params == {"c": dcm.CLASSIFICATION_VERSION}
    assert set(dcm.CLASSIFICATION_CHOICES) <= table_columns("classification_version")


@pytest.mark.parametrize("row", [(None, None, None, None), None])
def test_a_classification_without_stored_choices_uses_the_configuration(monkeypatch, row):
    """Clustered before migration 0014 (NULL) or not registered: the values of config_clustering.yaml apply."""
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", True)
    assert stored_dc(row).get_classification_choices() == {
        "list_of_clustering_parameters": dcm.LIST_OF_CLUSTERING_PARAMETERS, "n_clusters_kmeans": dcm.N_CLUSTERS_KMEANS,
        "n_clusters_gmm": dcm.N_CLUSTERS_GMM, "additional_filtering": True}


@pytest.mark.parametrize("reader", READERS + ["get_grid_status_of_sample_set"])
@pytest.mark.parametrize("stored, configured", [(False, True), (True, False), (None, True), (None, False)])
def test_the_readers_default_to_the_filter_choice_stored_with_the_classification(monkeypatch, reader, stored, configured):
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", configured)
    seen = capture_queries(monkeypatch)
    getattr(stored_dc((["no_branches"], 4, 4, stored)), reader)()
    ((query, _),) = seen
    lower = configured if stored is None else stored
    assert ("cp.avg_trafo_dis < %(avg_trafo_dis)s" in query) is lower


def test_an_explicit_filter_choice_needs_no_lookup(monkeypatch):
    capture_queries(monkeypatch)
    dc = stored_dc((["no_branches"], 4, 4, True))
    dc.get_clustering_parameters_for_classification_version(False)
    assert dc.dbc.cur.executed == []


# ---------------------------------------------------------------------------------------- reading parameters
def test_clustering_parameters_are_returned_in_the_configured_columns(monkeypatch):
    raw = read_rows().assign(grid_result_id=1)  # the SQL also returns these
    seen = {}

    def fake_read(query, con=None, params=None):
        seen.update(query=query, params=params)
        return raw

    monkeypatch.setattr(dcm.pd, "read_sql_query", fake_read)
    out = make_dc().get_clustering_parameters_for_classification_version()
    assert list(out.columns) == CLUSTERING_PARAMETERS
    assert seen["params"] == {"v": dcm.VERSION_ID, "c": dcm.CLASSIFICATION_VERSION, **dcm.FILTER_PARAMETERS}
    assert "cp.filtered" not in " ".join(seen["query"].split())


def test_both_clustering_queries_are_ordered_by_the_grid_key(monkeypatch):
    """The clusters depend on the row order, which the database leaves unspecified without ORDER BY."""
    queries = []
    monkeypatch.setattr(dcm.pd, "read_sql_query", lambda query, con=None, params=None: queries.append(query) or read_rows())
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
    dc.get_clustering_parameters_for_classification_version = lambda additional_filtering=None: df.copy()
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
    assert [entry[0] for entry in log] == ["begin", "execute", "to_sql", "execute", "commit"]
    _, statement, params = log[1]
    assert statement == "DELETE FROM pylovo.transformer_classified WHERE classification_id = :c"
    assert params == {"c": dcm.CLASSIFICATION_VERSION}
    assert captured["con"] is dc.dbc.sqla_engine.connection, "the insert uses the connection of the transaction"


def test_the_values_used_are_stored_with_the_classification_in_the_same_transaction(run_save):
    dc, _, _ = run_save
    _, statement, params = dc.dbc.sqla_engine.log[3]
    assert statement == ("UPDATE pylovo.classification_version SET "
                         "list_of_clustering_parameters = :list_of_clustering_parameters, "
                         "n_clusters_kmeans = :n_clusters_kmeans, n_clusters_gmm = :n_clusters_gmm, "
                         "additional_filtering = :additional_filtering, key_figures_version = :key_figures_version "
                         "WHERE classification_id = :c")
    assert params == {"c": dcm.CLASSIFICATION_VERSION, "list_of_clustering_parameters": PARAMS, "n_clusters_kmeans": 4,
                      "n_clusters_gmm": 4, "additional_filtering": dcm.ADDITIONAL_FILTERING,
                      "key_figures_version": dcm.KEY_FIGURES_VERSION}


def test_a_repeated_step_7_without_values_clusters_with_the_stored_choices(monkeypatch):
    """Repeating step 7 for an older classification must not take the values a later run wrote to the configuration."""
    dc, captured, df = prepare_save(monkeypatch)
    dc.dbc.cur = FakeCursor(fetchone_values=[(["no_house_connections", "avg_trafo_dis"], 3, 2, True)])
    read = {}
    dc.get_clustering_parameters_for_classification_version = (
        lambda additional_filtering=None: read.update(additional_filtering=additional_filtering) or df.copy())
    monkeypatch.setattr(dcm, "ADDITIONAL_FILTERING", False)
    dc.save_transformers_with_classification_info()
    assert read == {"additional_filtering": True}
    assert captured["frame"]["kmeans_clusters"].nunique() == 3
    params = dc.dbc.sqla_engine.log[3][2]
    assert params["list_of_clustering_parameters"] == ["no_house_connections", "avg_trafo_dis"]
    assert (params["n_clusters_kmeans"], params["n_clusters_gmm"], params["additional_filtering"]) == (3, 2, True)


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
    raw = read_rows()
    monkeypatch.setattr(dcm.pd, "read_sql_query", lambda *a, **k: raw)
    assert make_dc().municipal_register_with_clustering_parameters_for_classification_version() is raw
    source = inspect.getsource(dcm.DatabaseCommunication.municipal_register_with_clustering_parameters_for_classification_version)
    assert "mr.regio7" in source and "mr.pop_den" in source


# ---------------------------------------------------------------------------------------- version of the key figures
@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("stored, rows", [(None, 0), (-1, -1)])
def test_the_readers_return_key_figures_of_the_definitions_of_the_classification(monkeypatch, reader, stored, rows):
    """A new classification reads the current definitions; one clustered before reads its own until they change."""
    current = dcm.KEY_FIGURES_VERSION
    stored = None if stored is None else current + stored
    monkeypatch.setattr(dcm.pd, "read_sql_query", lambda *a, **k: read_rows(stored, current + rows))
    out = getattr(make_dc(), reader)(False)
    assert out.attrs["key_figures_version"] == current + rows
    assert "classification_key_figures_version" not in out.columns


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("stored, rows, hint", [
    (-1, 0, "computed again with other definitions"),           # clustered before its grids were analysed again
    (None, -1, "compute them again for the sampled PLZ"),        # key figures older than the current definitions
])
def test_the_readers_refuse_key_figures_of_other_definitions(monkeypatch, reader, stored, rows, hint):
    current = dcm.KEY_FIGURES_VERSION
    needed = current if stored is None else current + stored
    monkeypatch.setattr(dcm.pd, "read_sql_query",
                        lambda *a, **k: read_rows(None if stored is None else needed, current + rows))
    with pytest.raises(ValueError, match=f"needs key figures of version {needed}, but grids of its sample hold version "
                                         f"{current + rows}: .*{hint}"):
        getattr(make_dc(), reader)(False)


def test_the_readers_ask_for_the_version_stored_with_the_classification(monkeypatch):
    seen = capture_queries(monkeypatch)
    make_dc().get_clustering_parameters_for_classification_version(False)
    ((query, params),) = seen
    assert ("(SELECT key_figures_version FROM pylovo.classification_version WHERE classification_id = %(c)s) "
            "AS classification_key_figures_version") in query and params["c"] == dcm.CLASSIFICATION_VERSION


def test_step_7_stores_the_version_of_the_key_figures_it_clustered(monkeypatch):
    dc, _, df = prepare_save(monkeypatch)
    old = df.copy()
    old.attrs["key_figures_version"] = 1
    dc.get_clustering_parameters_for_classification_version = lambda additional_filtering=None: old.copy()
    dc.save_transformers_with_classification_info(PARAMS, 4, 4)
    assert dc.dbc.sqla_engine.log[3][2]["key_figures_version"] == 1


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
    assert "grid_result_id" in cols
    assert {"version_id", "plz", "kcid", "bcid"} <= table_columns("grid_result")
    assert {"households", "grid_result_id"} <= table_columns("buildings_result")
    assert {"grid_result_id", "geom"} <= table_columns("transformer_positions")


def test_insert_of_clustering_parameters_uses_ddl_columns():
    from pylovo.database.analysis_mixin import AnalysisMixin

    from pylovo.database.analysis_mixin import CLUSTERING_PARAMETER_COLUMNS

    source = inspect.getsource(AnalysisMixin.insert_clustering_parameters)
    assert "INSERT INTO pylovo.clustering_parameters (grid_result_id, " in source
    assert set(CLUSTERING_PARAMETER_COLUMNS) == table_columns("clustering_parameters") - {"grid_result_id"}


def test_municipal_register_config_matches_the_table_order():
    """``SELECT *`` plus ``columns=MUNICIPAL_REGISTER`` relies on the column order of the DDL."""
    assert ddl_columns(CREATE_QUERIES["municipal_register"]) == MUNICIPAL_REGISTER


def test_sample_set_references_the_register_and_the_version():
    ddl = CREATE_QUERIES["sample_set"]
    assert "REFERENCES pylovo.classification_version (classification_id)" in ddl
    assert "REFERENCES pylovo.municipal_register (plz, ags)" in ddl
    assert "ags bigint NOT NULL" in ddl


# ---------------------------------------------------------------------------------------- another classification
@pytest.mark.parametrize("reader", READERS + ["get_grid_status_of_sample_set"])
def test_the_readers_read_the_classification_and_version_given_to_the_constructor(monkeypatch, reader):
    """The notebooks pass the classification of their parameter cell; None keeps the configuration."""
    monkeypatch.setattr(dcm.dbc, "DatabaseClient", FakeDbc)
    seen = capture_queries(monkeypatch)
    dc = dcm.DatabaseCommunication(classification_id=9002, version_id=7)
    getattr(dc, reader)(False)
    ((_, params),) = seen
    assert (params["c"], params["v"]) == (9002, "7")
    assert (dc.classification_id, dc.version_id) == (9002, "7")


def test_without_arguments_the_configured_classification_is_read_at_call_time(monkeypatch):
    monkeypatch.setattr(dcm.dbc, "DatabaseClient", FakeDbc)
    dc = dcm.DatabaseCommunication()
    monkeypatch.setattr(dcm, "CLASSIFICATION_VERSION", 9003)
    monkeypatch.setattr(dcm, "VERSION_ID", "v9")
    assert (dc.classification_id, dc.version_id) == (9003, "v9")
    assert (make_dc().classification_id, make_dc().version_id) == (9003, "v9"), "also without __init__"


def test_the_choices_and_the_result_belong_to_the_given_classification(monkeypatch):
    dc = stored_dc((["no_branches"], 4, 4, False))
    dc._classification_id = 9004
    dc.get_classification_choices()
    assert dc.dbc.cur.executed[0][1] == {"c": 9004}
    save, captured, _ = prepare_save(monkeypatch)
    save._classification_id = 9004
    save.save_transformers_with_classification_info(PARAMS, 4, 4)
    assert set(captured["frame"]["classification_id"]) == {9004}
    statements = [entry for entry in save.dbc.sqla_engine.log if entry[0] == "execute"]
    assert all(entry[2]["c"] == 9004 for entry in statements)
