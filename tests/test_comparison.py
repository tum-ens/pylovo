"""Checks of the grid comparison: the metrics of a hand-made grid, the scores, the totals and the cable tables."""
import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from pylovo.analysis import comparison

RESISTANCE = {"NAYY_4_150": .208, "NAYY_4_50": .642}

#: A grid with its station at (0, 0): a 1 m station stub drawn without length, one feeder (two NAYY 150 in parallel)
#: that branches to five houses, and a cable from the station to a single house, which is no feeder.
#: (from, to, std_type, parallel, length_km or None for the drawn length)
CABLES = [
    ((0, 0), (0, 0), "NAYY_4_150", 1, .001),
    ((0, 0), (100, 0), "NAYY_4_150", 2, None),
    ((100, 0), (200, 0), "NAYY_4_150", 1, None),
    ((100, 0), (100, -60), "NAYY_4_150", 1, None),
    ((100, 0), (100, 50), "NAYY_4_50", 1, None),        # house 1
    ((200, 0), (200, 30), "NAYY_4_50", 1, None),        # house 2
    ((200, 0), (230, 0), "NAYY_4_50", 1, None),         # house 3
    ((100, -60), (90, -60), "NAYY_4_50", 1, None),      # house 5
    ((100, -60), (110, -60), "NAYY_4_50", 1, None),     # house 6
    ((0, 0), (0, -40), "NAYY_4_150", 1, None),
    ((0, -40), (0, -60), "NAYY_4_50", 1, None),         # house 4
]


def _grid(kcid=1, bcid=1, offset=(0., 0.), capacity=400.):
    rows = []
    for start, end, std_type, parallel, length in CABLES:
        a, b = np.add(start, offset), np.add(end, offset)
        geometry = LineString([a, b])
        rows.append({"kcid": kcid, "bcid": bcid, "std_type": std_type, "parallel": parallel,
                     "length_km": geometry.length / 1000 if length is None else length, "geometry": geometry})
    lines = gpd.GeoDataFrame(rows, geometry="geometry")
    station = gpd.GeoDataFrame([{"kcid": kcid, "bcid": bcid, "transformer_rated_power": capacity,
                                 "geometry": Point(offset)}], geometry="geometry")
    return lines, station


def test_metrics_of_a_hand_made_grid():
    lines, stations = _grid()
    row = comparison.grid_metrics(lines, stations, RESISTANCE).iloc[0]
    assert row["building_count"] == 6
    assert row["feeder_branching_endpoints"] == 2                  # (200, 0) and (100, -60)
    assert row["feeders"] == 1                                     # the cable to a single house is no feeder
    assert row["feeder_capacity_a"] == pytest.approx(2 * 270)      # two NAYY 150 in parallel
    assert row["buildings_behind_feeders"] == 5
    assert row["max_supply_path_m"] == pytest.approx(230)
    # to house 2: half of 100 m NAYY 150, 100 m NAYY 150, 30 m NAYY 50
    assert row["max_series_resistance_ohm"] == pytest.approx(.208 * .1 / 2 + .208 * .1 + .642 * .03)
    assert row["cable_km"] == pytest.approx(.551)                  # the stub and both parallel circuits counted
    assert row["cable_length_per_building_m"] == pytest.approx(551 / 6)
    assert row["route_km"] == pytest.approx(.45)                   # parallel cables once, without the stub
    assert row["transformer_bank_capacity_kva"] == 400
    assert row["transformer_capacity_per_building_kva"] == pytest.approx(400 / 6)


def test_one_row_per_grid_and_unknown_cable_types():
    first, first_station = _grid()
    second, second_station = _grid(bcid=2, offset=(1000., 0.), capacity=630.)
    table = comparison.grid_metrics(pd.concat([first, second]), pd.concat([first_station, second_station]),
                                    RESISTANCE)
    assert list(zip(table["kcid"], table["bcid"])) == [(1, 1), (1, 2)]
    assert list(table["transformer_bank_capacity_kva"]) == [400, 630]
    unknown = comparison.grid_metrics(first, first_station, {"NAYY_4_150": .208}).iloc[0]
    assert pd.isna(unknown["max_series_resistance_ohm"])
    assert unknown["feeder_capacity_a"] == pytest.approx(540)      # the feeder head is NAYY 150, which is known


def test_cable_ratings_follow_the_aluminium_reference():
    assert comparison.reference_rating_a(.208) == pytest.approx(270)
    assert 270 < comparison.reference_rating_a(.19) < 313
    assert comparison.reference_rating_a(None) is None
    assert comparison.equivalent_cross_section(.208) == 150
    assert comparison.equivalent_cross_section(.193) == 150     # copper 95 mm² resists like aluminium 150 mm²
    parameters = {"equipment_data": [{"typ": "Cable", "name": "NAYY_4_150", "r_mohm_per_km": 208.0},
                                     {"typ": "Transformer", "name": "Tr_400", "r_mohm_per_km": None}]}
    assert comparison.cable_resistances(parameters) == {"NAYY_4_150": .208}


def test_score_tells_noise_from_a_shift():
    reference = np.arange(30.)
    same = comparison.score(reference, reference.copy())
    assert same["wasserstein"] == 0 and same["shift"] == 0 and same["verdict"] == "same"
    shifted = comparison.score(reference, reference + 30)
    assert shifted["verdict"] == "higher" and shifted["p"] < comparison.ALPHA
    assert shifted["shift"] == pytest.approx(30)
    assert shifted["normalized"] == pytest.approx(30 / 14.5)      # W1 over the interquartile range of the reference
    assert shifted["floor_p90"] < shifted["normalized"]


def _metric_table(values):
    return pd.DataFrame({m.key: values for m in comparison.METRICS})


def test_compare_scorecard_and_ranking():
    rng = np.random.default_rng(1)
    reference = _metric_table(rng.uniform(10, 20, 40))
    close = _metric_table(rng.uniform(10, 20, 40))
    far = _metric_table(rng.uniform(30, 40, 40))
    scores = comparison.compare(reference, close)
    pd.testing.assert_frame_equal(scores, comparison.compare(reference, close))     # seeded per metric
    card = comparison.scorecard(comparison.compare(reference, far), names=("ref", "cand"))
    assert card.loc["Grids", "Δ"] == "+0%"
    assert card.loc["Buildings per grid", "Δ"].startswith("+")
    assert comparison.scorecard(scores).loc["Buildings per grid", "Δ"] == "≈"
    ranked = comparison.ranking({"far": comparison.compare(reference, far), "close": scores})
    assert list(ranked.index) == ["close", "far"]
    assert ranked.loc["close", "metrics within sampling noise"] == len(comparison.METRICS)


def test_area_totals_divide_sums():
    small = pd.DataFrame({"building_count": [2], "feeder_branching_endpoints": [1], "cable_km": [.2],
                          "route_km": [.2], "transformer_bank_capacity_kva": [100.], "feeder_head_capacity_a": [270.],
                          "buildings_behind_feeders": [2]})
    large = small.assign(building_count=8, cable_km=.4, route_km=.3, transformer_bank_capacity_kva=400.,
                         feeder_head_capacity_a=np.nan)
    totals = comparison.area_totals({"a": small, "b": pd.concat([small, large])})
    assert totals.loc["buildings", "b"] == 10
    assert totals.loc["cable length per building (m)", "b"] == pytest.approx(60)      # 600 m / 10, not a mean of ratios
    assert totals.loc["feeder capacity per building behind it (A)", "b"] == pytest.approx(135)   # known heads only
    assert totals.loc["transformer capacity per building (kVA)", "ratio"] == pytest.approx(1)


def test_cable_tables():
    lines, _ = _grid()
    lines["is_service"] = lines["std_type"] == "NAYY_4_50"
    table = comparison.cable_table(lines, RESISTANCE)
    assert set(table.loc[table["kind"] == "house services", "cross_section"]) == {50}
    assert table.loc[table["kind"] == "house services", "km"].sum() == pytest.approx(.15)
    summary = comparison.cable_summary({"grid": table})["grid"]
    assert summary["feeder cable in parallel (%)"] == pytest.approx(100 * .2 / .401, abs=.05)
