"""Coverage reconciliation: every source building gets exactly one status (no database)."""

import pandas as pd

from pylovo.analysis import coverage
from pylovo.analysis.coverage import classify, coverage_totals


def test_every_source_building_gets_exactly_one_status():
    eligible = pd.DataFrame({"objectid": ["a", "b", "c", "d", "e"], "home_plz": [1, 1, 1, 2, 3],
                             "households": [1, 2, 3, 4, 5]})
    generated = pd.DataFrame({"objectid": ["a", "x"], "grid_plz": [1, 1], "grid_result_id": [10, 11]})
    excluded = pd.DataFrame({"objectid": ["b"], "plz": [1], "reason": ["mv_direct"]})
    out = classify(eligible, generated, excluded, postcodes_with_results={1, 2}).set_index("objectid")["status"]
    assert out.to_dict() == {"a": "generated", "b": "excluded", "c": "missing", "d": "missing",
                             "e": "postcode_not_generated", "x": "not_in_source"}


def test_a_building_generated_in_another_plz_counts_where_it_is_generated():
    eligible = pd.DataFrame({"objectid": ["a"], "home_plz": [1], "households": [1]})
    generated = pd.DataFrame({"objectid": ["a"], "grid_plz": [2], "grid_result_id": [10]})
    buildings = classify(eligible, generated, pd.DataFrame(columns=["objectid", "plz", "reason"]), {1, 2})
    assert coverage_totals(buildings) == {"generated": 1}


def test_an_exclusion_row_without_a_source_building_is_reported_as_not_in_source():
    eligible = pd.DataFrame({"objectid": ["a"], "home_plz": [1], "households": [1]})
    generated = pd.DataFrame({"objectid": ["a"], "grid_plz": [1], "grid_result_id": [10]})
    excluded = pd.DataFrame({"objectid": ["z"], "plz": [2], "reason": ["mv_direct"]})
    buildings = classify(eligible, generated, excluded, postcodes_with_results={1, 2}).set_index("objectid")
    assert buildings["status"].to_dict() == {"a": "generated", "z": "not_in_source"}
    assert buildings.loc["z", "reason"] == "mv_direct" and buildings.loc["z", "excluded_plz"] == 2
    assert coverage_totals(buildings.reset_index()) == {"generated": 1, "not_in_source": 1}


def test_the_summary_counts_a_stored_exclusion_of_a_missing_source_building(monkeypatch):
    class Source:
        def fetch_buildings_from_infdb(self, plz):
            row = [None] * 15
            row[coverage._OBJECTID], row[coverage._HOUSEHOLDS] = "a", 3
            return [row]

    class Db:
        conn = object()
        cur = type("Cursor", (), {"execute": lambda self, *args: None, "fetchall": lambda self: [(1,)]})()

    tables = iter([pd.DataFrame({"objectid": ["a"], "grid_plz": [1], "grid_result_id": [10],
                                 "generated_households": [3]}),
                   pd.DataFrame({"objectid": ["z"], "plz": [1], "reason": ["mv_direct"]})])
    monkeypatch.setattr(coverage.pd, "read_sql_query", lambda *args, **kwargs: next(tables))
    buildings, summary = coverage.reconcile_coverage(Db(), Source(), [1], version_id="v")
    assert sorted(buildings["status"]) == ["generated", "not_in_source"]
    rows = summary.set_index("status")
    assert rows.loc["not_in_source", ["plz", "reason", "buildings"]].tolist() == [1, "mv_direct", 1]
