"""Coverage accounting (``reconcile_coverage``), the diagnostics report of ``pylovo-analyze --coverage
--border-diagnostics`` (``run_diagnostics``), and the border diagnostics (``postcode_context``, ``grid_edges``,
``border_kinds``) with fake database clients and shapely geometries (no database).

Not asserted here: exit codes and messages of ``pylovo-analyze`` for runs without any building."""
import json
import sys

import pandas as pd
import pytest
from shapely import wkb
from shapely.geometry import MultiPoint, box

from pylovo.analysis import border_diagnostics, coverage
from pylovo.analysis.border_diagnostics import border_kinds, grid_edges, postcode_context, summarize
from pylovo.analysis.coverage import coverage_totals, eligible_buildings, reconcile_coverage
from pylovo.cli import analyze
from pylovo.config_loader import TARGET_EPSG


def source_row(objectid, households):
    """A row of ``InfdbClient.fetch_buildings_from_infdb``: the object id is column 2, the households column 14."""
    row = [None] * 15
    row[2], row[14] = objectid, households
    return tuple(row)


class Infdb:
    """The source buildings per PLZ, and the scope and street lookups of the border diagnostics."""

    def __init__(self, buildings=None, scope=None, streets=(1.5, 0.5)):
        self.buildings, self.scope, self.streets, self.calls = buildings or {}, scope, streets, []

    def fetch_buildings_from_infdb(self, plz):
        return self.buildings.get(plz, [])

    def fetch_scope_wkb(self, polygon_wkb, epsg, ring_m):
        self.calls.append(("scope", epsg, ring_m))
        return None if self.scope is None else wkb.dumps(self.scope)

    def fetch_street_length_km(self, plz, polygon_wkb, epsg, ring_m):
        self.calls.append(("streets", plz, epsg, ring_m))
        return self.streets


class Cursor:
    """``fetchall`` returns the prepared result sets one after the other."""

    def __init__(self, *results):
        self.results, self.executed = list(results), []

    def execute(self, query, params=None):
        self.executed.append((" ".join(query.split()), params))

    def fetchall(self):
        return self.results.pop(0)


class Database:
    def __init__(self, *results, polygons=None):
        self.cur, self.conn, self.polygons = Cursor(*results), object(), polygons or {}

    def get_postcode_polygon_wkb(self, plz):
        return wkb.dumps(self.polygons[plz])


# --- reconcile_coverage ------------------------------------------------------------------------------------------------
def test_every_source_building_gets_one_status_and_the_queries_are_scoped_to_the_version_and_the_plz(monkeypatch):
    infdb = Infdb({80331: [source_row("A", 2), source_row("B", 1), source_row("C", 3), source_row("D", 1)],
                   80333: [source_row("A", 2), source_row("E", 4)]})          # A is listed in both PLZ: counted once
    queries = []

    def read_sql_query(sql, con, params):
        queries.append((" ".join(sql.split()), params))
        if "buildings_result" in sql:
            return pd.DataFrame({"objectid": ["A", "X"], "grid_plz": [80331, 80331], "grid_result_id": [10, 11],
                                 "generated_households": [2, 1]})
        return pd.DataFrame({"objectid": ["B"], "plz": [80331], "reason": ["mv_direct"]})

    monkeypatch.setattr(pd, "read_sql_query", read_sql_query)
    dbc = Database([(80331,)])                                          # only PLZ 80331 has results in the version

    buildings, summary = reconcile_coverage(dbc, infdb, [80331, 80333], "9")

    status = buildings.set_index("objectid")["status"].to_dict()
    assert status == {"A": "generated", "B": "excluded", "C": "missing", "D": "missing", "E": "postcode_not_generated",
                      "X": "not_in_source"}
    assert coverage_totals(buildings) == {"generated": 1, "excluded": 1, "missing": 2, "postcode_not_generated": 1,
                                          "not_in_source": 1, "excluded:mv_direct": 1}
    assert buildings.set_index("objectid").loc["E", "plz"] == 80333 and buildings.set_index("objectid").loc["X", "plz"] == 80331
    counts = summary.set_index(["plz", "status", "reason"])
    assert counts.loc[(80331, "missing", ""), "buildings"] == 2 and counts.loc[(80331, "missing", ""), "households"] == 4
    assert counts.loc[(80333, "postcode_not_generated", ""), "households"] == 4
    ids = ["A", "B", "C", "D", "E"]
    assert [params for _, params in queries] == [{"v": "9", "p": [80331, 80333], "o": ids}] * 2
    assert dbc.cur.executed[0][1] == {"v": "9", "p": [80331, 80333]}


def test_an_exclusion_row_without_a_source_building_is_reported_as_not_in_source():
    eligible = pd.DataFrame({"objectid": ["A"], "home_plz": [1], "households": [1]})
    generated = pd.DataFrame({"objectid": ["A"], "grid_plz": [1], "grid_result_id": [10]})
    excluded = pd.DataFrame({"objectid": ["Y"], "plz": [1], "reason": ["no_load"]})

    status = coverage.classify(eligible, generated, excluded, {1}).set_index("objectid")["status"].to_dict()
    assert status == {"A": "generated", "Y": "not_in_source"}


def test_the_eligible_buildings_are_the_source_rows_of_each_plz_without_duplicates():
    infdb = Infdb({1: [source_row("A", 1), source_row("B", 2)], 2: [source_row("B", 2), source_row("C", 3)]})
    frame = eligible_buildings(infdb, [1, 2])
    assert frame.to_dict("records") == [{"objectid": "A", "home_plz": 1, "households": 1},
                                        {"objectid": "B", "home_plz": 1, "households": 2},
                                        {"objectid": "C", "home_plz": 2, "households": 3}]


# --- run_diagnostics ----------------------------------------------------------------------------------------------------
@pytest.fixture
def clients(monkeypatch):
    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("pylovo.database.database_client.DatabaseClient", Client)
    monkeypatch.setattr("pylovo.infdb.infdb_client.InfdbClient",
                        lambda *args, **kwargs: type("Infdb", (), {"close": lambda self: None})())


def fake_coverage(missing):
    buildings = pd.DataFrame({"objectid": ["A"] * 3 + ["M"] * missing,
                              "status": ["generated", "excluded", "excluded"] + ["missing"] * missing,
                              "reason": [None, "mv_direct", "no_load"] + [None] * missing})
    return lambda dbc, inf_dbc, plz_list, version_id: (buildings, pd.DataFrame({"status": ["generated"], "buildings": [1]}))


@pytest.mark.parametrize("missing", [0, 2])
def test_the_coverage_report_is_written_and_the_number_of_missing_buildings_returned(monkeypatch, clients, tmp_path, capsys,
                                                                                    missing):
    monkeypatch.setattr(coverage, "reconcile_coverage", fake_coverage(missing))
    out = tmp_path / "reports"

    assert analyze.run_diagnostics([80331], coverage=True, border=False, version_id="9", output=out) == missing

    assert sorted(path.name for path in out.iterdir()) == ["coverage_buildings_9.csv", "coverage_summary_9.csv",
                                                           "diagnostics_9.json"]
    report = json.loads((out / "diagnostics_9.json").read_text())
    assert report["version_id"] == "9" and report["plz"] == [80331]
    assert report["coverage"]["generated"] == 1 and report["coverage"].get("missing", 0) == missing
    assert report["coverage"]["excluded:mv_direct"] == 1
    assert json.loads(capsys.readouterr().out)["coverage"] == report["coverage"]


def test_the_border_report_adds_the_postcode_context_and_the_grid_summary(monkeypatch, clients, tmp_path):
    context = pd.DataFrame({"plz": [80331], "area_km2": [1.23456789], "scope_share": [1.0]})
    grids = pd.DataFrame({"n_buildings": [3], "small": [True], "generated_border_m": [10.0], "open_border_m": [500.0],
                          "outer_border_m": [500.0], "scope_edge_m": [110.0]})
    monkeypatch.setattr(border_diagnostics, "postcode_context", lambda dbc, inf_dbc, plz_list: context)
    monkeypatch.setattr(border_diagnostics, "grid_edges", lambda dbc, inf_dbc, plz_list, version_id: grids)

    missing = analyze.run_diagnostics([80331], coverage=False, border=True, version_id="9", output=tmp_path)

    assert missing == 0 and not (tmp_path / "coverage_buildings_9.csv").exists()
    report = json.loads((tmp_path / "diagnostics_9.json").read_text())
    assert report["postcodes"] == [{"plz": 80331, "area_km2": 1.2346, "scope_share": 1.0}]
    assert report["grids"]["grids"] == 1 and report["grids"]["grids_near_generated_border"] == 1
    assert pd.read_csv(tmp_path / "grid_edges_9.csv")["n_buildings"].tolist() == [3]


def test_the_version_defaults_to_the_configured_one(monkeypatch, clients, tmp_path):
    monkeypatch.setattr(coverage, "reconcile_coverage", fake_coverage(0))
    monkeypatch.setattr("pylovo.config_loader.VERSION_ID", "42")
    analyze.run_diagnostics([80331], coverage=True, border=False, version_id=None, output=tmp_path)
    assert (tmp_path / "diagnostics_42.json").exists()


@pytest.mark.parametrize("missing, exit_code", [(2, 3), (0, None)])
def test_the_coverage_option_exits_with_three_only_when_source_buildings_are_missing(monkeypatch, tmp_path, missing,
                                                                                    exit_code):
    monkeypatch.setattr(analyze, "run_diagnostics", lambda *args, **kwargs: missing)
    monkeypatch.setattr(sys, "argv", ["pylovo-analyze", "--plz", "80331", "--coverage", "--output", str(tmp_path)])
    if exit_code is None:
        analyze.main()
    else:
        with pytest.raises(SystemExit) as stopped:
            analyze.main()
        assert stopped.value.code == exit_code == analyze.EXIT_INCOMPLETE


# --- border diagnostics ---------------------------------------------------------------------------------------------------
def test_the_postcode_context_gives_the_scope_shares_and_the_street_lengths():
    polygon = box(0, 0, 1000, 1000)
    infdb = Infdb(scope=box(-5000, -5000, 500, 5000), streets=(1.5, 0.5))
    context = postcode_context(Database(polygons={80331: polygon}), infdb, [80331], ring_m=100.0)

    row = context.iloc[0]
    assert row["plz"] == 80331 and row["area_km2"] == pytest.approx(1.0)
    assert row["scope_share"] == pytest.approx(0.5)            # the scope covers the western half of the polygon
    assert row["context_share"] == pytest.approx(0.5, rel=1e-6)    # and, by symmetry, half of the ring around it
    assert (row["streets_inside_km"], row["streets_ring_km"]) == (1.5, 0.5)
    assert infdb.calls == [("scope", TARGET_EPSG, 100.0), ("streets", 80331, TARGET_EPSG, 100.0)]


def test_without_an_import_scope_the_shares_are_undefined():
    context = postcode_context(Database(polygons={80331: box(0, 0, 1000, 1000)}), Infdb(scope=None), [80331], ring_m=100.0)
    assert pd.isna(context.iloc[0]["scope_share"]) and pd.isna(context.iloc[0]["context_share"])


def test_borders_are_classified_by_the_neighbour_across_them():
    polygons = {1: box(0, 0, 1000, 1000), 2: box(1000, 0, 2000, 1000), 3: box(0, 1000, 1000, 2000)}
    kinds = border_kinds(polygons, [1], generated={2})

    assert kinds["generated"].length == pytest.approx(1000, abs=5)    # east: neighbour 2 has results
    assert kinds["open"].length == pytest.approx(1000, abs=5)         # north: neighbour 3 has none
    assert kinds["outer"].length == pytest.approx(2000, abs=5)        # west and south: no neighbour at all


def test_a_plz_without_a_polygon_is_reported_with_its_number():
    with pytest.raises(ValueError, match="99999"):
        border_kinds({1: box(0, 0, 100, 100)}, [99999], generated=set())


def test_each_grid_gets_its_distances_to_the_borders_of_every_kind_and_to_the_scope_edge(monkeypatch):
    polygons = [(1, wkb.dumps(box(0, 0, 1000, 1000))), (2, wkb.dumps(box(1000, 0, 2000, 1000))),
                (3, wkb.dumps(box(0, 1000, 1000, 2000)))]
    dbc = Database([(2,)], polygons, polygons={})                      # PLZ 2 has results, PLZ 3 has none
    near_east = MultiPoint([(940, 500), (960, 500), (990, 500)])
    inland = MultiPoint([(200 + 10 * i, 200) for i in range(6)])
    monkeypatch.setattr(pd, "read_sql_query", lambda sql, con, params: pd.DataFrame({
        "grid_result_id": [1, 2], "plz": [1, 1], "kcid": [1, 1], "bcid": [1, 2], "n_buildings": [3, 6],
        "points": [wkb.dumps(near_east), wkb.dumps(inland)]}))

    grids = grid_edges(dbc, Infdb(scope=box(-100, -100, 1100, 1100)), [1], "9")

    assert dbc.cur.executed[0][1] == ("9",)
    east, middle = grids.iloc[0], grids.iloc[1]
    # distances are exact to a few metres: the borders carry 2 m stubs of the matching tolerance at their corners
    assert east["generated_border_m"] == pytest.approx(10, abs=3) and east["open_border_m"] == pytest.approx(500, abs=3)
    assert east["outer_border_m"] == pytest.approx(500, abs=3) and east["scope_edge_m"] == pytest.approx(110, abs=3)
    assert middle["outer_border_m"] == pytest.approx(200, abs=3) and middle["scope_edge_m"] == pytest.approx(300, abs=3)
    assert grids["small"].tolist() == [True, False]                    # up to 4 buildings is a small grid
    assert summarize(grids) == {
        "grids": 2, "buildings": 9, "small_grids": 1, "small_grid_buildings": 3,
        "grids_near_generated_border": 1, "small_grids_near_generated_border": 1,
        "grids_near_open_border": 0, "small_grids_near_open_border": 0,
        "grids_near_outer_border": 1, "small_grids_near_outer_border": 0,
        "grids_near_scope_edge": 1, "small_grids_near_scope_edge": 1}


def test_the_summary_of_no_grid_is_all_zero():
    empty = pd.DataFrame({column: pd.Series([], dtype=float) for column in
                          ("n_buildings", "generated_border_m", "open_border_m", "outer_border_m", "scope_edge_m")})
    empty["small"] = empty["n_buildings"] <= 4
    assert set(summarize(empty).values()) == {0}
