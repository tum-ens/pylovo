"""MV node inputs (pylovo.mv_grid.inputs): stations, customers, MaStR connection points, LV-side generation, rows.

DB-free; the SQL of MvInputMixin is checked read-only on a development database by the acceptance script.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pylovo import utils
from pylovo.mv_grid import inputs
from pylovo.mv_grid.model import CATEGORIES
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

SIM = {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6}
CONSUMER_CATEGORIES = pd.DataFrame({"definition": list(SIM), "sim_factor": list(SIM.values())})
PARAMS = MvParameters()


def _lv(**changes) -> LvVersionParameters:
    values = dict(version_id="t1", sim_factor=dict(SIM), power_factor=0.95, mv_direct_threshold_kw=300.0,
                  residential_only=False, lv_reference_voltage_pu=0.96, use_dso=False, use_open=False,
                  use_manual=False, electrical_backend="pandapower")
    values.update(changes)
    return LvVersionParameters(**values)


def _buildings(rng: np.random.Generator, count: int, n_grids: int) -> pd.DataFrame:
    residential = rng.uniform(5, 40, count) * rng.integers(1, 12, count)
    return pd.DataFrame({
        "objectid": [f"b{i}" for i in range(count)],
        "vertice_id": np.arange(count),
        "connection_point": np.arange(count),
        "grid_result_id": rng.integers(0, n_grids, count) + 100,
        "residential_peak_load_in_kw": np.where(rng.random(count) < 0.1, np.nan, residential),
        "households": np.where(rng.random(count) < 0.1, np.nan, rng.integers(0, 12, count).astype(float)),
        "nonresidential_peak_load_in_kw": np.where(rng.random(count) < 0.35, rng.uniform(1, 290, count), 0.0),
        "nonresidential_use": rng.choice(["Commercial", "Public", "Unknown"], count),
        "nonresidential_mv_direct": rng.choice(np.array([True, False, False, False], dtype=object), count),
    })


def _sql_stats(buildings: pd.DataFrame) -> pd.DataFrame:
    """The long statistics exactly as MvInputMixin.station_load_stats computes them in SQL."""
    rows = []
    for b in buildings.itertuples(index=False):
        res = b.residential_peak_load_in_kw
        hh = b.households
        if pd.notna(res) and res > 0 and pd.notna(hh) and hh > 0:
            rows.append((b.grid_result_id, b.objectid, "Residential", float(res), float(hh)))
        non = b.nonresidential_peak_load_in_kw
        if pd.notna(non) and non > 0 and not bool(b.nonresidential_mv_direct):
            rows.append((b.grid_result_id, b.objectid, b.nonresidential_use, float(non), 1.0))
    comp = pd.DataFrame(rows, columns=["grid_result_id", "objectid", "category", "installed_kw", "units"])
    agg = comp.groupby(["grid_result_id", "category"]).agg(installed_kw=("installed_kw", "sum"),
                                                           units=("units", "sum"),
                                                           n_buildings=("objectid", "nunique")).reset_index()
    per_grid = comp.groupby("grid_result_id")["objectid"].nunique().rename("grid_n_buildings")
    return agg.join(per_grid, on="grid_result_id")


def _stations(ids, **extra) -> pd.DataFrame:
    ids = list(ids)
    frame = pd.DataFrame({"grid_result_id": ids, "plz": 90000, "sn_kva": 400.0, "power_flow_status": "converged",
                          "lv_max_drop_pu": 0.05, "plz_settlement_type": 2, "station_source": "greenfield",
                          "x": np.arange(len(ids)) * 100.0, "y": 0.0})
    for key, value in extra.items():
        frame[key] = value
    return frame


# ---------------------------------------------------------------------------------------------- stations
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4])
def test_station_peak_equals_pooled_pylovo_law(seed):
    rng = np.random.default_rng(seed)
    buildings = _buildings(rng, 300, 6)
    ids = sorted(buildings["grid_result_id"].unique())
    table = inputs.station_table(_stations(ids), _sql_stats(buildings), _lv())
    assert table["grid_result_id"].tolist() == ids
    for row in table.itertuples(index=False):
        part = buildings[buildings["grid_result_id"] == row.grid_result_id]
        expected = utils.simultaneous_peak_load(part, CONSUMER_CATEGORIES, part["vertice_id"])
        assert row.p_own_kw == pytest.approx(expected, rel=1e-12, abs=1e-12)
        components = utils.build_load_components(part)
        assert row.installed_kw == pytest.approx(components["installed_kw"].sum(), rel=1e-12)
        residential = components[components["category"] == "Residential"]
        assert row.households == pytest.approx(residential["load_units"].sum())
        assert row.n_buildings == part.loc[part["objectid"].isin(
            buildings.loc[buildings["vertice_id"].isin(components["consumer_vertex"]), "objectid"]), "objectid"].nunique()
        stats = row.load_stats
        assert set(stats) == set(components["category"])
        for category, (installed, units) in stats.items():
            mask = components["category"] == category
            assert installed == pytest.approx(components.loc[mask, "installed_kw"].sum())
            assert units == pytest.approx(components.loc[mask, "load_units"].sum())


def test_station_table_columns_flags_and_missing_stats():
    stations = _stations([7, 5, 6], power_flow_status=["converged", "voltage_violation", None])
    stations.loc[2, "x"] = np.nan                      # grid 6 has no position: not an MV node
    stats = pd.DataFrame({"grid_result_id": [5, 5], "category": ["Residential", "Mixed"],
                          "installed_kw": [100.0, 50.0], "units": [10.0, 1.0], "grid_n_buildings": [4, 4]})
    table = inputs.station_table(stations, stats, _lv())
    assert tuple(table.columns) == inputs.STATION_COLUMNS
    assert table["grid_result_id"].tolist() == [5, 7]
    assert table["kind"].eq("station").all() and table["connection_hint"].eq("ring").all()
    five, seven = table.iloc[0], table.iloc[1]
    assert five["flags"] == ["lv_not_converged"] and seven["flags"] == []
    assert five["units_unk"] == 1.0 and five["inst_unk"] == 50.0      # an unknown use counts as Unknown
    expected = 100.0 * (0.07 + 0.93 * 10 ** -0.75) + 50.0 * (0.6 + 0.4)
    assert five["p_own_kw"] == pytest.approx(expected)
    assert five["n_buildings"] == 4
    assert seven["p_own_kw"] == 0.0 and seven["installed_kw"] == 0.0 and seven["load_stats"] == {}
    assert (table[list(inputs.GENERATION_COLUMNS)] == 0.0).all().all()
    assert table["p_fixed_kw"].eq(0.0).all()


# ---------------------------------------------------------------------------------------------- customers
def test_customer_table_loads_classes_and_sources():
    rows = pd.DataFrame({
        "objectid": ["c3", "c1", "c2", "c4", "c5"],
        "plz": [1, 1, 2, 2, 2],
        "nonresidential_use": ["Commercial", "Public", "Commercial", "Garage", "Unknown"],
        "installed_kw": [400.0, 12000.0, 45000.0, 500.0, 0.0],
        "x": [1.0, 2.0, 3.0, 4.0, 5.0], "y": 0.0,
        "mapped_station": [False, True, False, False, False],
        "source": ["mv_customers", "mv_customers", "mv_customers", "buildings_result", "mv_customers"],
    })
    table = inputs.customer_table(rows, _lv(), PARAMS)
    assert tuple(table.columns) == inputs.CUSTOMER_COLUMNS
    assert table["objectid"].tolist() == ["c1", "c2", "c3", "c4"]          # c5 has no load
    by_id = table.set_index("objectid")
    assert by_id.loc["c3", "p_own_kw"] == pytest.approx(0.5 * 400.0)
    assert by_id.loc["c3", "connection_hint"] == "ring"
    assert by_id.loc["c1", "p_own_kw"] == pytest.approx(0.6 * 12000.0)
    assert by_id.loc["c1", "connection_hint"] == "dedicated"              # 7.2 MW > 5.5 MW
    assert "mapped_station" in by_id.loc["c1", "flags"] and by_id.loc["c1", "mapped_station"]
    assert by_id.loc["c2", "connection_hint"] == "excluded"               # 22.5 MW > 20 MW
    assert "hv_customer" in by_id.loc["c2", "flags"]
    assert by_id.loc["c4", "category"] == "Unknown" and "use_unknown" in by_id.loc["c4", "flags"]
    assert by_id.loc["c4", "customer_objectid"] is None                   # not in pylovo.mv_customers
    assert "not_in_mv_customers" in by_id.loc["c4", "flags"]
    assert by_id.loc["c3", "customer_objectid"] == "c3"
    assert (table["p_fixed_kw"] == table["p_own_kw"]).all()
    assert table["load_stats"].isna().all()                               # fixed loads: never pooled again


# ---------------------------------------------------------------------------------------------- MaStR
@pytest.mark.parametrize("text, expected", [
    ("Höchstspannung", "hv"), ("Hochspannung", "hv"), ("Umspannebene Höchstspannung/Hochspannung", "hv"),
    ("Umspannebene Hochspannung/Mittelspannung", "hvmv"), ("Mittelspannung", "mv"),
    ("Umspannebene Mittelspannung/Niederspannung", "mvlv"),
    ("Niederspannung (= Hausanschluss/Haushaltsstrom)", "lv"), ("  mittelspannung ", "mv"),
])
def test_classify_level_texts(text, expected):
    assert inputs.classify_level(text, 50_000.0, PARAMS) == expected      # the text wins over the power


@pytest.mark.parametrize("kw, expected", [
    (10.0, "lv"), (100.0, "lv"), (150.0, "mvlv"), (200.0, "mvlv"), (201.0, "mv"), (5500.0, "mv"),
    (6000.0, "hvmv"), (20000.0, "hvmv"), (20001.0, "hv"), (float("nan"), "lv"),
])
def test_classify_level_power_fallback(kw, expected):
    assert inputs.classify_level(None, kw, PARAMS) == expected
    assert inputs.classify_level("something new", kw, PARAMS) == expected


def test_treatment_rules():
    assert inputs.treatment("hv", 1e6, PARAMS) == "excluded"
    assert inputs.treatment("hvmv", 1.0, PARAMS) == "node"
    assert inputs.treatment("mv", 1.0, PARAMS) == "node"
    assert inputs.treatment("mvlv", 250.0, PARAMS) == "node"
    assert inputs.treatment("mvlv", 200.0, PARAMS) == "lv_side"
    assert inputs.treatment("lv", 1e6, PARAMS) == "lv_side"


def _units() -> pd.DataFrame:
    return pd.DataFrame([
        # a wind park: one location, three units close together
        ("m01", "SEL1", "wind", 3000.0, "Mittelspannung", 0.0, 0.0),
        ("m02", "SEL1", "wind", 3000.0, "Mittelspannung", 300.0, 0.0),
        ("m03", "SEL1", "wind", 6000.0, "Mittelspannung", 600.0, 0.0),
        # PV + storage + biomass at one connection point; storage is no generation
        ("m04", "SEL2", "solar", 150.0, "Umspannebene Mittelspannung/Niederspannung", 1000.0, 1000.0),
        ("m05", "SEL2", "storage", 500.0, "Umspannebene Mittelspannung/Niederspannung", 1000.0, 1000.0),
        ("m06", "SEL2", "biomass", 100.0, "Umspannebene Mittelspannung/Niederspannung", 1000.0, 1010.0),
        # a location with units 20 km apart (two connection points)
        ("m07", "SEL3", "solar", 30.0, "Niederspannung (= Hausanschluss/Haushaltsstrom)", 0.0, 5000.0),
        ("m09", "SEL3", "solar", 20.0, "Niederspannung (= Hausanschluss/Haushaltsstrom)", 20000.0, 5000.0),
        ("m08", "SEL3", "solar", 10.0, "Niederspannung (= Hausanschluss/Haushaltsstrom)", 50.0, 5000.0),
        # no location id, no level: classified by power
        ("m10", None, "hydro", 7000.0, None, 3000.0, 3000.0),
        ("m11", "", "solar", 0.0, None, 3000.0, 3000.0),                  # no power: left out
        ("m12", "SEL4", "combustion", 900.0, "Hochspannung", 4000.0, 0.0),
        # known level and a NULL unit: the known level decides
        ("m13", "SEL5", "solar", 15000.0, "Mittelspannung", 9000.0, 0.0),
        ("m14", "SEL5", "solar", 15000.0, None, 9010.0, 0.0),
    ], columns=["mastr_id", "location_id", "technology", "net_power_kw", "voltage_level", "x", "y"])


def test_aggregate_locations():
    points = inputs.aggregate_locations(_units(), PARAMS)
    assert tuple(points.columns) == inputs.LOCATION_COLUMNS
    assert points["mastr_connection_id"].tolist() == ["SEL1", "SEL2", "SEL3/1", "SEL3/2", "SEL4", "SEL5",
                                                       "unit/m10"]
    by_id = points.set_index("mastr_connection_id")
    wind = by_id.loc["SEL1"]
    assert wind["gen_wind_kw"] == 12000.0 and wind["n_units"] == 3 and wind["technology"] == "wind"
    assert wind["x"] == pytest.approx((0 * 3000 + 300 * 3000 + 600 * 6000) / 12000)
    assert wind["voltage_class"] == "mv" and wind["treatment"] == "node"
    mixed = by_id.loc["SEL2"]
    assert mixed["gen_solar_kw"] == 150.0 and mixed["gen_other_kw"] == 100.0 and mixed["kw"] == 250.0
    assert mixed["technology"] == "solar+other"
    assert mixed["voltage_class"] == "mvlv" and mixed["treatment"] == "node"   # 250 kW > 200 kW
    assert by_id.loc["SEL3/1", "n_units"] == 2 and by_id.loc["SEL3/2", "n_units"] == 1
    assert by_id.loc["SEL3/1", "split"] and by_id.loc["SEL3/1", "flags"] == ["location_split"]
    assert by_id.loc["SEL3/1", "treatment"] == "lv_side"
    assert by_id.loc["SEL4", "voltage_class"] == "hv" and by_id.loc["SEL4", "treatment"] == "excluded"
    assert by_id.loc["SEL5", "voltage_class"] == "mv"                      # not hvmv by its 30 MW
    assert by_id.loc["unit/m10", "voltage_class"] == "hvmv"                # 7 MW without a level
    assert by_id.loc["unit/m10", "location_id"] is None
    assert points.attrs["n_split_locations"] == 1
    assert points.attrs["class_counts"]["mv"] == 2


def test_aggregate_locations_empty():
    empty = inputs.aggregate_locations(pd.DataFrame(columns=_units().columns), PARAMS)
    assert empty.empty and tuple(empty.columns) == inputs.LOCATION_COLUMNS


# ---------------------------------------------------------------------------------------------- LV-side generation
def test_distribute_lv_generation():
    stations = _stations([1, 2, 3], plz=[90001, 90001, 90002])         # x = 0, 100, 200
    targets = pd.DataFrame({
        "x": [0.0, 140.0, 5000.0, 90.0], "y": 0.0,
        "gen_solar_kw": [10.0, 20.0, 40.0, 5.0], "gen_wind_kw": 0.0, "gen_other_kw": [0.0, 0.0, 0.0, 1.0],
        "grid_result_id": [3, np.nan, np.nan, 99],                    # 99: not a station of the table
    })
    aggregates = pd.DataFrame({"postcode": ["90001", "90001", "90002", "90003"], "plz": [90001, 90001, 90002, 90003],
                               "technology": ["solar", "hydro", "solar", "solar"],
                               "net_power_kw": [300.0, 30.0, 50.0, 70.0], "area_ratio": [0.5, 0.5, 1.0, 1.0]})
    counts = pd.DataFrame({"grid_result_id": [1, 2, 2], "postcode": ["90001", "90001", "90005"],
                           "n_buildings": [1, 2, 7]})
    gen = inputs.distribute_lv_generation(stations, targets, aggregates, counts)
    assert tuple(gen.columns) == inputs.GENERATION_RESULT_COLUMNS
    by_id = gen.set_index("grid_result_id")
    # grid 3: the building target (10) + PLZ 90002 without counted buildings: equal split over its stations (50)
    assert by_id.loc[3, "gen_solar_kw"] == pytest.approx(10.0 + 50.0)
    # point at x=140 -> nearest station 2 (x=100); point of grid 99 (no station) at x=90 -> station 2
    # PLZ 90001: 300 x 0.5 = 150 solar, 15 other, split 1:2 by building counts
    assert by_id.loc[2, "gen_solar_kw"] == pytest.approx(20.0 + 5.0 + 100.0)
    assert by_id.loc[2, "gen_other_kw"] == pytest.approx(1.0 + 10.0)
    assert by_id.loc[1, "gen_solar_kw"] == pytest.approx(50.0) and by_id.loc[1, "gen_other_kw"] == pytest.approx(5.0)
    assert gen.attrs["points_building"] == 1 and gen.attrs["points_station"] == 2
    assert gen.attrs["points_dropped"] == 1 and gen.attrs["kw_dropped"] == pytest.approx(40.0)
    assert gen.attrs["aggregate_kw_dropped"] == pytest.approx(70.0)       # PLZ 90003 has no station
    assert gen.attrs["aggregate_kw"] == pytest.approx(150.0 + 15.0 + 50.0 + 70.0)


def test_distribute_lv_generation_without_inputs():
    gen = inputs.distribute_lv_generation(_stations([4, 2]), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert gen["grid_result_id"].tolist() == [2, 4]
    assert (gen[list(inputs.GENERATION_COLUMNS)] == 0.0).all().all()


# ---------------------------------------------------------------------------------------------- node rows
def test_node_rows_order_hints_and_columns():
    lv = _lv()
    stations = inputs.station_table(_stations([12, 11]), pd.DataFrame(columns=["grid_result_id", "category",
                                                                               "installed_kw", "units"]), lv)
    stations.loc[stations["grid_result_id"] == 12, "gen_solar_kw"] = 30.0
    customers = inputs.customer_table(pd.DataFrame({"objectid": ["z", "a"], "plz": 1,
                                                    "nonresidential_use": "Commercial",
                                                    "installed_kw": [400.0, 13000.0], "x": 1.0, "y": 2.0,
                                                    "mapped_station": False}), lv, PARAMS)
    points = inputs.aggregate_locations(_units(), PARAMS)
    sites = pd.DataFrame({"substation_id": [42, 41], "x": [0.0, 10.0], "y": [0.0, 0.0]})
    rows = inputs.node_rows(stations, customers, points, sites, PARAMS)
    assert rows.index.tolist() == list(range(len(rows)))
    kinds = rows["kind"].tolist()
    assert kinds == ["busbar"] * 2 + ["station"] * 2 + ["customer"] * 2 + ["generator"] * 4
    assert rows.loc[rows["kind"] == "busbar", "substation_id"].tolist() == [41, 42]
    assert rows.loc[rows["kind"] == "busbar", "connection_hint"].eq("busbar").all()
    assert rows.loc[rows["kind"] == "station", "grid_result_id"].tolist() == [11, 12]
    assert rows.loc[rows["kind"] == "customer", "customer_objectid"].tolist() == ["a", "z"]
    gens = rows[rows["kind"] == "generator"].set_index("mastr_connection_id")
    # node treatment only: SEL1 (mv), SEL2 (mvlv 250 kW), SEL5 (mv, 30 MW: dedicated), unit/m10 (hvmv)
    assert gens.index.tolist() == ["SEL1", "SEL2", "SEL5", "unit/m10"]
    assert gens.loc["SEL1", "connection_hint"] == "dedicated"             # 12 MW > 5.5 MW
    assert gens.loc["SEL2", "connection_hint"] == "tee"
    assert gens.loc["unit/m10", "connection_hint"] == "dedicated"         # HS/MS level
    assert gens.loc["SEL1", "voltage_class"] == "mv" and gens.loc["SEL1", "technology"] == "wind"
    for column in inputs.GENERATION_COLUMNS:
        assert rows[column].notna().all()
    assert rows.loc[rows["grid_result_id"] == 12, "gen_solar_kw"].item() == 30.0
    assert all(isinstance(f, list) for f in rows["flags"])
    assert rows["x"].notna().all() and rows["y"].notna().all()
    assert not rows.loc[rows["kind"] == "customer", "mapped_station"].any()
    summary = inputs.summary_counts(rows)
    assert summary["customer/dedicated"] == 1 and summary["generator/tee"] == 1


def test_node_rows_without_sites_or_nodes():
    rows = inputs.node_rows(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(columns=["x", "y"]), PARAMS)
    assert rows.empty and {"kind", "connection_hint", "x", "y"} <= set(rows.columns)


def test_load_stats_follow_the_categories():
    stats = inputs.load_stats_dicts(np.array([[1.0, 0.0, 0.0, 2.0]]), np.array([[3.0, 0.0, 0.0, 1.0]]))
    assert stats == [{"Residential": [1.0, 3.0], "Unknown": [2.0, 1.0]}]
    assert list(CATEGORIES) == ["Residential", "Commercial", "Public", "Unknown"]
