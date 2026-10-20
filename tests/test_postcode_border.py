"""Border kinds, the border summary, the width of the neighboring streets and their snapshot (no database)."""

import pandas as pd
from shapely.geometry import box

from pylovo import postcode_border
from pylovo.analysis.border_diagnostics import border_kinds, summarize
from pylovo.version_snapshot import compare_snapshots


def test_borders_are_split_into_generated_open_and_outer():
    polygons = {1: box(0, 0, 100, 100), 2: box(100, 0, 200, 100), 3: box(0, 100, 100, 200)}
    kinds = border_kinds(polygons, [1], generated={1, 2})
    # lengths up to the matching tolerance at the corners (2 m per end)
    assert abs(kinds["generated"].length - 100) <= 4    # shared with 2 (generated)
    assert abs(kinds["open"].length - 100) <= 4         # shared with 3 (no results)
    assert abs(kinds["outer"].length - 200) <= 4        # west and south: no neighbour
    assert kinds["generated"].distance(box(150, 40, 160, 50)) > 49 and kinds["open"].distance(box(40, 150, 50, 160)) > 49


def test_summary_counts_small_grids_near_each_border():
    grids = pd.DataFrame({"n_buildings": [3, 40, 2], "small": [True, False, True],
                          "generated_border_m": [10.0, 20.0, 900.0], "open_border_m": [900.0, 900.0, 100.0],
                          "outer_border_m": [float("nan")] * 3, "scope_edge_m": [5000.0] * 3})
    summary = summarize(grids)
    assert summary["small_grids"] == 2 and summary["small_grid_buildings"] == 5
    assert summary["small_grids_near_generated_border"] == 1 and summary["small_grids_near_open_border"] == 1


def test_the_neighboring_streets_hold_every_brownfield_route():
    assert postcode_border.neighboring_street_width_m() == postcode_border.MAX_BROWNFIELD_TRAFO_DISTANCE / 2 + 100
    assert postcode_border.snapshot(False) == {"route_across_postcode_borders": False,
                                               "neighboring_street_width_m": None,
                                               "neighboring_street_margin_m": None, "rule_revision": None}
    assert postcode_border.snapshot(True)["neighboring_street_width_m"] == postcode_border.neighboring_street_width_m()


def test_versions_stored_before_the_switch_stay_valid_only_without_routing_across_borders():
    stored = {"transformer_placement": {"max_brownfield_trafo_distance": 1000}}
    expected_off = {**stored, "postcode_border": postcode_border.snapshot(False)}
    expected_on = {**stored, "postcode_border": postcode_border.snapshot(True)}
    assert compare_snapshots(stored, expected_off) == ([], [])
    differences, _ = compare_snapshots(stored, expected_on)
    assert "postcode_border.route_across_postcode_borders" in differences
