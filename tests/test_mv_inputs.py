"""MV node inputs (pylovo.mv_grid.inputs): stations, customers, MaStR connection points, LV-side generation, rows.

DB-free; the SQL of MvInputMixin is checked read-only on a development database by the acceptance script.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from pylovo import utils
from pylovo.mv_grid import inputs
from pylovo.mv_grid.model import CATEGORIES
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

SIM = {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6, "Unknown": 0.6}
PER_M2 = {"Commercial": 79.0, "Public": 29.0, "Unknown": 29.0}           # W/m², pylovo's consumer categories
CONSUMER_CATEGORIES = pd.DataFrame({"definition": list(SIM), "sim_factor": list(SIM.values())})
PARAMS = MvParameters()


def _lv(**changes) -> LvVersionParameters:
    values = dict(version_id="t1", sim_factor=dict(SIM), power_factor=0.95, mv_direct_threshold_kw=300.0,
                  residential_only=False, lv_reference_voltage_pu=0.96, use_dso=False, use_open=False,
                  use_manual=False, electrical_backend="pandapower", peak_load_per_m2=dict(PER_M2))
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
    stations = _stations([7, 5, 6], power_flow_status=["converged", "not_converged", None])
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
    # a converged LV grid with violations is flagged by its status, not as not converged
    other = inputs.station_table(_stations([5], power_flow_status=["voltage_violation"]), stats, _lv())
    assert other.iloc[0]["flags"] == ["lv_voltage_violation"]
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
    assert table["installed_kw"].to_numpy() == pytest.approx(table["lv_installed_kw"].to_numpy())  # no hall data


def _customer_rows(**columns) -> pd.DataFrame:
    n = len(next(iter(columns.values())))
    rows = {"objectid": [f"c{i}" for i in range(n)], "plz": 1, "nonresidential_use": "Commercial", "x": 0.0,
            "y": 0.0, "mapped_station": False}
    rows.update(columns)
    if "installed_kw" not in columns:                 # the LV load: floor area x W/m² of the category
        uses = pd.Series(rows["nonresidential_use"], index=range(n))
        rows["installed_kw"] = (pd.Series(columns["nonresidential_floor_area"]) * uses.map(PER_M2) / 1000.0).tolist()
    return pd.DataFrame(rows)


def test_customer_halls_count_one_storey():
    rows = _customer_rows(
        nonresidential_use=["Commercial", "Commercial", "Commercial", "Public", "Commercial"],
        footprint_m2=[2500.0, 3000.0, 1500.0, 4000.0, 2100.0],
        nonresidential_floor_area=[7500.0, 3000.0, 6000.0, 20000.0, np.nan])
    rows.loc[4, "installed_kw"] = 400.0                                   # no stored floor area
    table = inputs.customer_table(rows, _lv(), PARAMS).set_index("objectid")
    # c0: a 2,500 m² hall with 3 derived storeys counts one storey
    assert table.loc["c0", "installed_kw"] == pytest.approx(2500.0 * 79.0 / 1000.0)
    assert table.loc["c0", "lv_installed_kw"] == pytest.approx(7500.0 * 79.0 / 1000.0)
    assert table.loc["c0", "p_fixed_kw"] == pytest.approx(0.5 * 197.5) and "hall_one_storey" in table.loc["c0", "flags"]
    # c1: a hall of one storey and c2: a small multi-storey building keep their floor area
    assert table.loc["c1", "installed_kw"] == pytest.approx(237.0) and table.loc["c1", "flags"] == []
    assert table.loc["c2", "installed_kw"] == pytest.approx(6000.0 * 0.079) and table.loc["c2", "flags"] == []
    # c3: a public hall with the W/m² of its category; c4: the floor area of its LV load (400 kW / 79 W/m²)
    assert table.loc["c3", "installed_kw"] == pytest.approx(4000.0 * 0.029)
    assert table.loc["c3", "p_fixed_kw"] == pytest.approx(0.6 * 116.0)
    assert table.loc["c4", "installed_kw"] == pytest.approx(2100.0 * 0.079)
    assert "hall_one_storey" in table.loc["c4", "flags"]
    # the threshold is a variant constant
    smaller = inputs.customer_table(rows, _lv(), PARAMS.replace(customer_hall_min_footprint_m2=1000.0))
    assert smaller.set_index("objectid").loc["c2", "installed_kw"] == pytest.approx(1500.0 * 0.079)


def test_customer_classes_follow_the_corrected_load():
    rows = _customer_rows(footprint_m2=[100000.0, 1500.0], nonresidential_floor_area=[400000.0, 200000.0])
    table = inputs.customer_table(rows, _lv(), PARAMS).set_index("objectid")
    # 31.6 MW installed in the LV version (15.8 MW coincident, a dedicated feeder); one storey: 3.95 MW on a ring
    assert table.loc["c0", "lv_installed_kw"] == pytest.approx(31600.0)
    assert table.loc["c0", "p_fixed_kw"] == pytest.approx(3950.0) and table.loc["c0", "connection_hint"] == "ring"
    # no hall: 15.8 MW installed, 7.9 MW coincident: dedicated
    assert table.loc["c1", "connection_hint"] == "dedicated"


def test_customer_table_needs_the_peak_load_per_m2():
    rows = _customer_rows(footprint_m2=[2500.0], nonresidential_floor_area=[7500.0])
    with pytest.raises(ValueError, match="no peak load per m² for Commercial"):
        inputs.customer_table(rows, _lv(peak_load_per_m2={"Public": 29.0, "Unknown": 29.0}), PARAMS)


def _split_case():
    lv = _lv()
    stats = pd.DataFrame({"grid_result_id": [1], "category": ["Residential"], "installed_kw": [100.0],
                          "units": [10.0]})
    stations = inputs.station_table(_stations([1, 2, 3]).assign(x=[0.0, 100.0, 1000.0]), stats, lv)
    # own peaks (kW, after the hall rule): 199 (just below 200), 200 (own node), 116 (public), 50 and 120
    rows = _customer_rows(nonresidential_use=["Commercial", "Commercial", "Public", "Commercial", "Commercial"],
                          installed_kw=[199.0, 200.0, 116.0, 50.0, 120.0], x=[90.0, 500.0, 980.0, 10.0, 160.0],
                          y=[0.0, 0.0, 30.0, 0.0, 0.0])
    customers = inputs.customer_table(rows, lv, PARAMS)
    return stations, customers


def test_small_customers_hang_on_the_nearest_station():
    stations, customers = _split_case()
    st, kept = inputs.busbar_customers(stations, customers, PARAMS)
    assert kept["objectid"].tolist() == ["c1"]                            # 200 kW: own MV node
    by_gid = st.set_index("grid_result_id")
    two = inputs.busbar_record(by_gid.loc[2, "load_stats"])              # c0 (10 m) and c4 (60 m) at station 2
    assert two["objectids"] == ["c0", "c4"] and two["n"] == 2
    assert two["installed_kw"] == pytest.approx(319.0) and two["kw"] == pytest.approx(0.5 * 319.0)
    assert two["categories"] == {"Commercial": {"n": 2, "installed_kw": pytest.approx(319.0),
                                                "kw": pytest.approx(159.5)}}
    assert two["max_distance_m"] == pytest.approx(60.0)
    three = inputs.busbar_record(by_gid.loc[3, "load_stats"])            # the public customer, 36 m away
    assert three["categories"] == {"Public": {"n": 1, "installed_kw": pytest.approx(116.0),
                                              "kw": pytest.approx(69.6)}}
    assert three["max_distance_m"] == pytest.approx(np.hypot(20.0, 30.0))
    one = by_gid.loc[1]
    assert inputs.busbar_record(one["load_stats"])["objectids"] == ["c3"]
    assert one["load_stats"]["Residential"] == [100.0, 10.0]             # the LV statistics stay
    assert by_gid["p_fixed_kw"].tolist() == pytest.approx([25.0, 159.5, 69.6])
    assert all(f == ["busbar_customers"] for f in by_gid["flags"])
    assert kept.attrs["n_busbar"] == 4 and kept.attrs["n_busbar_stations"] == 3
    assert kept.attrs["busbar_kw"] == pytest.approx(25.0 + 159.5 + 69.6)
    assert kept.attrs["max_busbar_distance_m"] == pytest.approx(60.0)
    assert stations["p_fixed_kw"].eq(0.0).all() and customers["objectid"].size == 5     # inputs unchanged
    assert "4 below 200 kW at 3 station busbars" in inputs.describe_customers(kept.attrs, PARAMS)
    # the threshold is a variant constant; a customer exactly at it keeps its node
    _, at = inputs.busbar_customers(stations, customers, PARAMS.replace(customer_min_mv_kw=50.0))
    assert at["objectid"].tolist() == ["c0", "c1", "c2", "c3", "c4"]
    _, above = inputs.busbar_customers(stations, customers, PARAMS.replace(customer_min_mv_kw=60.0))
    assert above["objectid"].tolist() == ["c0", "c1", "c2", "c4"]


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_busbar_customers_keep_the_totals(seed):
    rng = np.random.default_rng(seed)
    n_st, n_cu = 40, 120
    lv = _lv()
    stations = inputs.station_table(_stations(range(1, n_st + 1)).assign(x=rng.uniform(0, 5000, n_st),
                                                                          y=rng.uniform(0, 5000, n_st)),
                                    pd.DataFrame(columns=["grid_result_id", "category", "installed_kw", "units"]), lv)
    area = rng.uniform(500.0, 20000.0, n_cu)
    rows = _customer_rows(nonresidential_use=rng.choice(["Commercial", "Public", "Unknown"], n_cu).tolist(),
                          nonresidential_floor_area=area, footprint_m2=area / rng.integers(1, 5, n_cu),
                          x=rng.uniform(0, 5000, n_cu), y=rng.uniform(0, 5000, n_cu))
    customers = inputs.customer_table(rows, lv, PARAMS)
    unlimited = PARAMS.replace(busbar_customer_max_m=None)               # every small customer at a station busbar
    st, kept = inputs.busbar_customers(stations, customers, unlimited)
    small = customers["installed_kw"] < PARAMS.customer_min_mv_kw
    assert 0 < small.sum() < n_cu and len(kept) == (~small).sum()
    records = [inputs.busbar_record(s) for s in st["load_stats"]]
    # corrected customer kW = MV-node kW + station-busbar kW (fixed and installed)
    assert customers["p_fixed_kw"].sum() == pytest.approx(kept["p_fixed_kw"].sum() + st["p_fixed_kw"].sum(),
                                                          rel=1e-12)
    assert st["p_fixed_kw"].sum() == pytest.approx(sum(r.get("kw", 0.0) for r in records), rel=1e-12)
    assert customers.loc[small, "installed_kw"].sum() == pytest.approx(
        sum(r.get("installed_kw", 0.0) for r in records), rel=1e-12)
    assert sum(r.get("n", 0) for r in records) == small.sum()
    # every busbar customer sits at its nearest station (brute force)
    station_of = {o: gid for gid, r in zip(st["grid_result_id"], records) for o in r.get("objectids", [])}
    xy = st.set_index("grid_result_id")[["x", "y"]]
    for c in customers[small].itertuples():
        nearest = int(np.argmin(np.hypot(xy["x"] - c.x, xy["y"] - c.y)))
        assert station_of[c.objectid] == xy.index[nearest]


def test_small_customers_beyond_the_distance_limit_keep_their_node():
    lv = _lv()
    stations = inputs.station_table(_stations([1, 2]).assign(x=[0.0, 100.0]),
                                    pd.DataFrame(columns=["grid_result_id", "category", "installed_kw", "units"]), lv)
    rows = _customer_rows(nonresidential_use=["Commercial"] * 3, installed_kw=[100.0, 120.0, 150.0],
                          x=[1000.0, 1100.0, 1101.0], y=[0.0, 0.0, 0.0])   # 900 m, 1,000 m and 1,001 m from station 2
    customers = inputs.customer_table(rows, lv, PARAMS)
    assert PARAMS.busbar_customer_max_m == 1000.0
    st, kept = inputs.busbar_customers(stations, customers, PARAMS)
    assert inputs.busbar_record(st.set_index("grid_result_id").loc[2, "load_stats"])["objectids"] == ["c0", "c1"]
    assert kept["objectid"].tolist() == ["c2"] and kept.loc[0, "flags"] == ["remote_small_customer"]
    assert (kept.attrs["n_busbar"], kept.attrs["n_remote"]) == (2, 1)
    assert kept.attrs["remote_kw"] == pytest.approx(0.5 * 150.0)
    assert "1 below 200 kW farther than 1,000 m from every station keep an own MV node" in inputs.describe_customers(
        kept.attrs, PARAMS)
    _, all_remote = inputs.busbar_customers(stations, customers, PARAMS.replace(busbar_customer_max_m=500.0))
    assert len(all_remote) == 3 and all_remote.attrs["n_busbar"] == 0 and all_remote.attrs["n_remote"] == 3
    _, unlimited = inputs.busbar_customers(stations, customers, PARAMS.replace(busbar_customer_max_m=None))
    assert unlimited.empty and unlimited.attrs["n_busbar"] == 3


def test_overloaded_busbar_stations_get_the_next_catalogue_rating():
    """LV peak + busbar customers above rating x TRANSFORMER_PLANNING_UTILIZATION: the next catalogue rating of the
    PLZ settlement type (pylovo's choice for load / utilization), above the catalogue n x 630 kVA."""
    lv = _lv(transformer_planning_utilization=0.8,
             transformer_mapping={"1": [100.0, 160.0, 250.0, 400.0], "2": [250.0, 400.0, 630.0]})
    record = {"n": 1, "kw": 50.0, "installed_kw": 100.0, "categories": {}, "objectids": ["c0"],
              "max_distance_m": 10.0}
    stations = pd.DataFrame({
        "grid_result_id": [1, 2, 3, 4, 5, 6], "sn_kva": [400.0, 400.0, 400.0, 400.0, 400.0, np.nan],
        "p_own_kw": [270.0, 300.0, 300.0, 600.0, 340.0, 300.0], "plz_settlement_type": [2, 2, 1, 1, 2, 2],
        "load_stats": [{"busbar_customers": record}] * 4 + [{"Residential": [1.0, 1.0]}, {"busbar_customers": record}],
        "flags": [["busbar_customers"]] * 4 + [[], ["busbar_customers"]]})
    out = inputs.upsize_busbar_stations(stations, lv)
    assert out["sn_kva"].tolist()[:5] == [400.0, 630.0, 630.0, 1260.0, 400.0] and np.isnan(out["sn_kva"].iloc[5])
    # 1: 320 kW = 400 x 0.8 fits; 2: 350 kW needs 437.5 kVA -> 630 (type 2); 3: rural catalogue ends at 400 ->
    # 1 x 630; 4: 650 kW needs 812.5 kVA -> 2 x 630; 5: no busbar customers; 6: no rating
    assert out.attrs["n_upsized"] == 3
    two = inputs.busbar_record(out["load_stats"].iloc[1])
    assert (two["lv_sn_kva"], two["upsized_sn_kva"], two["load_kw"]) == (400.0, 630.0, 350.0)
    assert out["flags"].iloc[1] == ["busbar_customers", "station_busbar_overload"]
    assert out["flags"].iloc[0] == ["busbar_customers"] and "lv_sn_kva" not in inputs.busbar_record(
        out["load_stats"].iloc[0])
    assert stations["sn_kva"].tolist()[:5] == [400.0] * 5                 # the input stays
    text = json.dumps(dict(out["load_stats"].iloc[3]))                     # a JSON record (as stored) works too
    again = inputs.upsize_busbar_stations(stations.assign(load_stats=[text] * 6), lv)
    assert again["sn_kva"].iloc[3] == 1260.0
    assert "3 stations upsized" in inputs.describe_customers({"n_upsized_stations": 3}, PARAMS)


def test_upsized_station_rating_survives_the_database_round_trip():
    from pylovo.database.mv_result_mixin import node_columns_frame

    lv = _lv(transformer_planning_utilization=0.8, transformer_mapping={"2": [250.0, 400.0, 630.0]})
    stations, customers = _split_case()
    stations = stations.assign(sn_kva=[50.0, 400.0, 400.0])
    st, kept = inputs.busbar_customers(stations, customers, PARAMS)
    st = inputs.upsize_busbar_stations(st, lv)
    # station 1: 23.5 kW LV peak + 25 kW busbar customer > 0.8 x 50 kVA -> the smallest catalogue rating, 250 kVA
    assert st["sn_kva"].tolist() == [250.0, 400.0, 400.0]
    sites = pd.DataFrame({"substation_id": [41], "x": [0.0], "y": [0.0]})
    rows = inputs.node_rows(st, kept, sites)
    stored = rows.assign(mv_node_id=np.arange(1, len(rows) + 1),
                         load_stats=[None if not isinstance(s, dict) else json.dumps(s) for s in rows["load_stats"]])
    nodes = node_columns_frame(stored)
    stations_out = nodes[nodes["kind"] == "station"]
    assert stations_out["sn_kva"].tolist() == [250.0, 400.0, 400.0]
    assert stations_out["lv_sn_kva"].tolist() == [50.0, 400.0, 400.0]


def test_busbar_customers_without_stations_keep_their_nodes(caplog):
    _, customers = _split_case()
    with caplog.at_level("WARNING"):
        st, kept = inputs.busbar_customers(pd.DataFrame(columns=["x", "y"]), customers, PARAMS)
    assert len(kept) == 5 and kept.attrs["n_busbar"] == 0 and "no station node" in caplog.text
    empty_st, empty = inputs.busbar_customers(st, customers.iloc[0:0], PARAMS)
    assert empty.empty and empty.attrs["n_busbar"] == 0


def test_busbar_record_reads_dicts_and_text():
    record = {"n": 1, "kw": 50.0}
    assert inputs.busbar_record({"Residential": [1.0, 1.0], "busbar_customers": record}) == record
    assert inputs.busbar_record('{"busbar_customers": {"n": 1, "kw": 50.0}}') == record
    assert inputs.busbar_record({"Residential": [1.0, 1.0]}) == {} and inputs.busbar_record(None) == {}


def test_station_busbar_load_survives_the_database_round_trip():
    """node_rows -> mv_node (load_stats as JSON) -> NODE_COLUMNS: the station carries the busbar load as p_fixed_kw,
    so the district peak is the law on the stations plus every corrected customer load."""
    from pylovo.database.mv_result_mixin import node_columns_frame
    from pylovo.mv_grid import coincidence

    stations, customers = _split_case()
    st, kept = inputs.busbar_customers(stations, customers, PARAMS)
    sites = pd.DataFrame({"substation_id": [41], "x": [0.0], "y": [0.0]})
    rows = inputs.node_rows(st, kept, sites)
    stored = rows.assign(mv_node_id=np.arange(1, len(rows) + 1),
                         load_stats=[None if not isinstance(s, dict) else json.dumps(s) for s in rows["load_stats"]])
    nodes = node_columns_frame(stored)
    by_kind = nodes.groupby("kind")["p_fixed_kw"].sum()
    assert by_kind["customer"] == pytest.approx(kept["p_fixed_kw"].sum())
    assert by_kind["station"] == pytest.approx(kept.attrs["busbar_kw"])
    installed, units, fixed = coincidence.node_stats(nodes[nodes["kind"].isin(["station", "customer"])])
    law = coincidence.coincident_kw(installed.sum(axis=0), units.sum(axis=0), _lv().g())
    assert coincidence.pooled_kw(installed, units, fixed, _lv().g()) == pytest.approx(
        float(np.sum(law)) + customers["p_fixed_kw"].sum())


def test_demand_anchor_per_inhabitant():
    nodes = pd.DataFrame({"kind": ["busbar", "station", "station", "customer", "customer", "generator"],
                          "connection_hint": ["busbar", "ring", "ring", "ring", "excluded", "tee"]})
    for column in [*inputs.INSTALLED_COLUMNS, *inputs.UNITS_COLUMNS]:
        nodes[column] = 0.0
    nodes.loc[[1, 2], "inst_res"] = [600.0, 400.0]
    nodes.loc[[1, 2], "units_res"] = [40.0, 30.0]
    nodes["p_fixed_kw"] = [0.0, 150.0, 0.0, 800.0, 25000.0, 0.0]
    population = pd.DataFrame({"plz": [1, 2, 3], "pop": [1000.0, 3000.0, np.nan],
                               "area_km2": [10.0, 20.0, np.nan], "polygon_km2": [10.0, 5.0, 2.0]})
    anchor = inputs.demand_anchor(nodes, population, _lv())
    law = 1000.0 * (0.07 + 0.93 * 70 ** -0.75)
    assert anchor["coincident_mw"] == pytest.approx((law + 950.0) / 1000.0)
    assert anchor["population"] == 1000 + 750                            # PLZ 2: a quarter of its area
    assert anchor["kw_per_inhabitant"] == pytest.approx((law + 950.0) / 1750.0)
    assert (anchor["customer_mw"], anchor["n_customer_nodes"], anchor["busbar_customer_mw"]) == pytest.approx(
        (0.8, 1, 0.15))
    assert anchor["excluded_mw"] == pytest.approx(25.0)
    assert anchor["approximate"] is True and anchor["partial_plz"] == {"2": 0.25}
    assert anchor["plz_without_population"] == [3]
    assert json.loads(json.dumps(anchor)) == anchor
    exact = inputs.demand_anchor(nodes, population.iloc[:1], _lv())
    assert exact["approximate"] is False and exact["population"] == 1000
    assert inputs.demand_anchor(nodes.iloc[0:0], population.iloc[0:0], _lv())["kw_per_inhabitant"] is None


# ---------------------------------------------------------------------------------------------- node rows
def test_node_rows_order_hints_and_columns():
    lv = _lv()
    stations = inputs.station_table(_stations([12, 11]), pd.DataFrame(columns=["grid_result_id", "category",
                                                                               "installed_kw", "units"]), lv)
    customers = inputs.customer_table(pd.DataFrame({"objectid": ["z", "a"], "plz": 1,
                                                    "nonresidential_use": "Commercial",
                                                    "installed_kw": [400.0, 13000.0], "x": 1.0, "y": 2.0,
                                                    "mapped_station": False}), lv, PARAMS)
    sites = pd.DataFrame({"substation_id": [42, 41], "x": [0.0, 10.0], "y": [0.0, 0.0]})
    rows = inputs.node_rows(stations, customers, sites)
    assert rows.index.tolist() == list(range(len(rows)))
    kinds = rows["kind"].tolist()
    assert kinds == ["busbar"] * 2 + ["station"] * 2 + ["customer"] * 2
    assert rows.loc[rows["kind"] == "busbar", "substation_id"].tolist() == [41, 42]
    assert rows.loc[rows["kind"] == "busbar", "connection_hint"].eq("busbar").all()
    assert rows.loc[rows["kind"] == "station", "grid_result_id"].tolist() == [11, 12]
    assert rows.loc[rows["kind"] == "customer", "customer_objectid"].tolist() == ["a", "z"]
    for column in inputs.GENERATION_COLUMNS:                              # kept for stored variants, always 0
        assert rows[column].eq(0.0).all()
    assert all(isinstance(f, list) for f in rows["flags"])
    assert rows["x"].notna().all() and rows["y"].notna().all()
    assert not rows.loc[rows["kind"] == "customer", "mapped_station"].any()


def test_node_rows_without_sites_or_nodes():
    rows = inputs.node_rows(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(columns=["x", "y"]))
    assert rows.empty and {"kind", "connection_hint", "x", "y"} <= set(rows.columns)


def test_load_stats_follow_the_categories():
    stats = inputs.load_stats_dicts(np.array([[1.0, 0.0, 0.0, 2.0]]), np.array([[3.0, 0.0, 0.0, 1.0]]))
    assert stats == [{"Residential": [1.0, 3.0], "Unknown": [2.0, 1.0]}]
    assert list(CATEGORIES) == ["Residential", "Commercial", "Public", "Unknown"]
