"""Coverage reconciliation: every source building gets exactly one status (no database)."""

import pandas as pd

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
