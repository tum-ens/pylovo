"""The net metrics, ``key_figures`` and ``ParameterCalculator`` on in-memory pandapower nets built like pylovo builds them
(real offline ``PandapowerBackend``, fake database client; no database).

Net: LVbus 1 -f0 (100 m)- Connection Nodebus 10, which feeds 11 (f1, 50 m) and 12 (f2, 60 m); each of those has two
17 kW households on service cables (10 to 13 m). Station 400 kVA. Options: f2 starts at the busbar, buildings connected
at the busbar (15 m service cables), no street at all.
"""
import json
import logging
import math

import numpy as np
import pandapower as pp
import pandapower.topology as top
import pytest

from pylovo.analysis import grid_analysis, impedance_metrics, net_metrics, parameter_calculation
from pylovo.analysis.net_metrics import station_mva
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo.database.analysis_mixin import AnalysisMixin
from pylovo.electrical_backend import BusSpec, ExtGridSpec, LineSpec, LoadSpec, TransformerSpec, create_backend

LOGGER = logging.getLogger("parameter-calculator-test")
CABLES = [("NAYY_4_150", 0.206, 0.080, 0.275, 20.0), ("NAYY_4_240", 0.125, 0.078, 0.357, 30.0)]
LON, LAT = 11.5, 48.1
R, X = 0.206, 0.080
SIM_FACTOR = net_metrics.SIM_FACTOR["Residential"]


def build(parallel_feeder=1, kva=400, parallel_trafo=1, load_kw=17.0, f2_at_busbar=False, busbar=(), street=True,
          f0_km=0.1, drop=()):
    """The test net; ``busbar`` are the vertices of buildings connected at the busbar, ``street=False`` leaves out the
    feeders and their four households, ``f0_km`` is the length of f0 and ``drop`` removes consumers with their cable."""
    backend = create_backend("pandapower", logger=LOGGER)
    backend.initialize_circuit("c", "MVbus 1", 20.0)
    backend.register_cable_types(CABLES)
    with backend.batch():
        backend.create_component(BusSpec(name="LVbus 1", voltage_kv=0.4, coordinates=(LON, LAT)))
        backend.create_component(BusSpec(name="MVbus 1", voltage_kv=20.0, coordinates=(LON, LAT + 1.5e-4)))
        backend.create_component(ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1))
    backend.create_component(TransformerSpec(name="t", bus1="MVbus 1", bus2="LVbus 1", kva=kva, parallel=parallel_trafo))
    with backend.batch():
        consumers = [(21, 0.0012, 0.0003), (22, 0.0013, 0.0001), (23, 0.0012, -0.0003), (24, 0.0011, -0.0001)] if street else []
        consumers += [(vertex, -0.0002 * (i + 1), 0.0001) for i, vertex in enumerate(busbar)]
        if street:
            for vertex, dx, dy in [(10, 0.0005, 0), (11, 0.001, 0.0002), (12, 0.001, -0.0002)]:
                backend.create_component(BusSpec(name=f"Connection Nodebus {vertex}", voltage_kv=0.4,
                                                 coordinates=(LON + dx, LAT + dy)))
        for vertex, dx, dy in consumers:
            backend.create_component(BusSpec(name=f"Consumer Nodebus {vertex}", voltage_kv=0.4,
                                             coordinates=(LON + dx, LAT + dy), zone="Residential"))
            backend.create_component(LoadSpec(name=f"Load {vertex} Residential", bus=f"Consumer Nodebus {vertex}", kw=3.0,
                                              kvar=1.0, max_p_mw=load_kw * 1e-3, service_design_p_mw=0.01,
                                              category="Residential", load_units=1.0, consumer_vertex=vertex))

        def line(name, a, b, length, parallel=1):
            backend.create_component(LineSpec(name=name, bus1=a, bus2=b, cable_name="NAYY_4_150", length_km=length,
                                              parallel=parallel))

        if street:
            line("f0", "LVbus 1", "Connection Nodebus 10", f0_km, parallel_feeder)
            line("f1", "Connection Nodebus 10", "Connection Nodebus 11", 0.05, parallel_feeder)
            line("f2", "LVbus 1" if f2_at_busbar else "Connection Nodebus 10", "Connection Nodebus 12", 0.06)
            line("s21", "Connection Nodebus 11", "Consumer Nodebus 21", 0.01)
            line("s22", "Connection Nodebus 11", "Consumer Nodebus 22", 0.012)
            line("s23", "Connection Nodebus 12", "Consumer Nodebus 23", 0.011)
            line("s24", "Connection Nodebus 12", "Consumer Nodebus 24", 0.013)
        for vertex in busbar:
            line(f"s{vertex}", "LVbus 1", f"Consumer Nodebus {vertex}", 0.015)
    net = backend.net
    pp.toolbox.drop_buses(net, [int(net.bus.index[net.bus["name"] == f"Consumer Nodebus {v}"][0]) for v in drop])
    return net


class Database:
    """The lookups of the calculator: the per-PLZ lists of the per-transformer analysis (``data``)."""
    logger = LOGGER

    def __init__(self, data=None):
        self.data = data if data is not None else [{}, {}, {}, {}, {}]

    def read_per_trafo_dict(self, plz):
        return self.data, [], {}


def calculator(data=None, plz=1):
    pc = object.__new__(ParameterCalculator)
    pc.dbc, pc.version_id, pc.plz, pc.kcid, pc.bcid = Database(data), "1", plz, 1, 1
    return pc


def coincident(households, installed_kw):
    """Coincident load of residential households: installed x (g + (1 - g) n^(-3/4))."""
    return installed_kw * (SIM_FACTOR + (1 - SIM_FACTOR) * households ** -0.75)


def haversine_km(a, b):
    (lon1, lat1), (lon2, lat2) = np.radians(a), np.radians(b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def test_clustering_metrics_of_a_pylovo_grid():
    net = build()
    metrics = grid_analysis.key_figures(net)

    peak = grid_analysis.PEAK_LOAD_HOUSEHOLD
    assert (metrics["no_connection_buses"], metrics["no_house_connections"], metrics["no_households"]) == (3, 4, 4)
    assert metrics["no_branches"] == 1 and metrics["no_house_connections_per_branch"] == 4.0
    assert metrics["max_power_mw"] == pytest.approx(0.068)
    assert metrics["no_household_equ"] == pytest.approx(68.0 / peak)                 # installed kW over a household peak
    assert metrics["no_household_equ_per_branch"] == pytest.approx(68.0 / peak)
    assert metrics["cable_length_km"] == pytest.approx(0.256)
    assert metrics["cable_len_per_house"] == pytest.approx(0.256 / 4)
    assert metrics["transformer_mva"] == pytest.approx(0.4)
    # routed distances from the busbar to the four consumers, in km: 0.160, 0.162, 0.171, 0.173
    assert metrics["max_trafo_dis"] == pytest.approx(0.173) and metrics["avg_trafo_dis"] == pytest.approx(0.1665)
    assert metrics["simultaneous_peak_load_mw"] == pytest.approx(coincident(4, 68.0) / 1000)    # the grid's own loads
    assert metrics["ratio"] == pytest.approx(metrics["resistance"] / metrics["reactance"])
    assert metrics["vsw_per_branch"] == pytest.approx(metrics["resistance"])         # one branch


def key_figures(**options):
    """Key figures of ``build(**options)``."""
    return grid_analysis.key_figures(build(**options))


@pytest.mark.parametrize("options, branches, behind, ends", [
    ({}, 1, 4, 2),                                            # one feeder, two branch ends (nodes 11 and 12)
    ({"busbar": (31,)}, 1, 4, 2),                             # a building at the busbar belongs to no feeder
    ({"f2_at_busbar": True, "busbar": (31, 32)}, 2, 4, 2),    # two feeders of two houses each
    ({"f0_km": 0.001}, 2, 4, 2),                              # a 1 m stub belongs to the station: two feeders
    ({"f2_at_busbar": True, "drop": (24,)}, 1, 2, 1),         # a cable from the station to one house: no feeder
    ({"street": False, "busbar": (31, 32, 33)}, 0, 0, 0),     # a star grid: buildings at the busbar only
    ({"street": False, "busbar": (31,)}, 0, 0, 0),            # a single-building grid
])
def test_a_branch_is_a_feeder_of_the_structural_metrics(options, branches, behind, ends):
    """A feeder leaves the station towards at least two buildings; the per-feeder figures count the buildings
    behind the feeders and are 0 without a feeder."""
    metrics = key_figures(**options)
    assert (metrics["no_branches"], metrics["no_branch_ends"]) == (branches, ends)
    per_branch = behind / branches if branches else 0.0
    assert metrics["no_house_connections_per_branch"] == pytest.approx(per_branch)
    assert metrics["no_household_equ_per_branch"] == pytest.approx(per_branch * 17.0 / grid_analysis.PEAK_LOAD_HOUSEHOLD)
    assert metrics["no_house_connections_per_branch"] <= metrics["no_house_connections"]
    assert 0 <= metrics["vsw_per_branch"] <= metrics["resistance"]
    if not branches:
        assert metrics["vsw_per_branch"] == metrics["max_vsw_of_a_branch"] == metrics["max_household_equ_of_a_branch"] == 0


@pytest.mark.parametrize("parallel, metres", [(1, 100 + 60 + 13), (2, 50 + 60 + 13)])
def test_the_route_resistance_is_the_highest_series_resistance_to_a_house(parallel, metres):
    metrics = key_figures(parallel_feeder=parallel)   # f0 and f1 doubled: house 24 behind f2 is the worst
    assert metrics["max_route_resistance_ohm"] == pytest.approx(metres / 1000 * R)
    assert key_figures(street=False, busbar=(31,))["max_route_resistance_ohm"] == pytest.approx(0.015 * R)


def test_a_single_building_grid_has_no_house_distance():
    metrics = key_figures(street=False, busbar=(31,))
    assert metrics["house_distance_km"] is None                                       # stored as NULL, not 0 km
    assert key_figures(street=False, busbar=(31, 32))["house_distance_km"] > 0


def test_the_largest_branch_is_one_of_the_same_feeders():
    peak = grid_analysis.PEAK_LOAD_HOUSEHOLD
    metrics = key_figures(busbar=(31,))
    assert metrics["max_household_equ_of_a_branch"] == pytest.approx(4 * 17.0 / peak)    # not the busbar building
    metrics = key_figures(f2_at_busbar=True, busbar=(31, 32))
    assert metrics["max_household_equ_of_a_branch"] == pytest.approx(2 * 17.0 / peak)    # two houses per feeder
    assert metrics["vsw_per_branch"] < metrics["max_vsw_of_a_branch"] < metrics["resistance"]


def test_the_house_distance_of_geographic_coordinates_is_the_great_circle_distance():
    pair = pp.create_empty_network()
    for lon in (LON, LON + 0.001):                                                   # two consumers 74 m apart
        pp.create_load(pair, pp.create_bus(pair, 0.4, geodata=(lon, LAT)), 0.01)
    assert net_metrics.average_house_distance(pair) == pytest.approx(haversine_km((LON, LAT), (LON + 0.001, LAT)), rel=1e-4)

    # the documented statistic: median over the consumers of the mean distance to the nearest (up to four) consumers;
    # the station, MV and connection buses do not count
    net = build()
    consumers = net.bus[net.bus["name"].str.startswith("Consumer Nodebus")]
    points = [tuple(json.loads(geo)["coordinates"]) for geo in consumers["geo"]]
    means = [sum(sorted(haversine_km(point, other) for j, other in enumerate(points) if j != i)[:3]) / 3
             for i, point in enumerate(points)]
    assert net_metrics.average_house_distance(net) == pytest.approx(float(np.median(means)), rel=1e-4)


def test_the_house_distance_of_projected_coordinates_is_in_km_and_needs_two_consumers():
    net = pp.create_empty_network()
    assert math.isnan(net_metrics.average_house_distance(net))                      # no bus: undefined
    pp.create_load(net, pp.create_bus(net, 0.4, geodata=(500000.0, 5300000.0)), 0.01)
    pp.create_bus(net, 0.4, geodata=(500100.0, 5300000.0))                          # no consumer: not counted
    assert math.isnan(net_metrics.average_house_distance(net))                      # one consumer: undefined
    pp.create_load(net, pp.create_bus(net, 0.4, geodata=(500300.0, 5300000.0)), 0.01)
    assert net_metrics.average_house_distance(net) == pytest.approx(0.3)            # 300 m


def test_the_households_are_the_load_units_of_the_residential_loads():
    net = build()
    net.load.loc[net.load.index[0], "load_units"] = 6.0                              # a building with six flats
    pp.create_load(net, net.load.at[net.load.index[1], "bus"], 0.005, max_p_mw=0.02, category="Commercial",
                   load_units=1.0)                                                   # a shop in a house: no household
    assert net_metrics.count_households(net) == 6 + 3
    assert grid_analysis.key_figures(net)["no_households"] == 9
    net.load.drop(columns="load_units", inplace=True)                               # nets without the column
    assert net_metrics.count_households(net) == 4


@pytest.mark.parametrize("parallel", [1, 2])
def test_the_legacy_impedance_metrics_weight_each_path_with_its_cumulated_simultaneity(parallel):
    net = build(parallel_feeder=parallel)
    cables = {"f0": parallel, "f1": parallel}                                       # parallel cables divide R and X
    households = 68.0 / grid_analysis.PEAK_LOAD_HOUSEHOLD
    weight = 17.0 / grid_analysis.PEAK_LOAD_HOUSEHOLD                                # household equivalents per consumer
    behind = {"f0": 4, "f1": 2, "f2": 2, "s": 1}                                    # households behind each cable
    factor = {name: coincident(count, 1.0) for name, count in behind.items()}      # sim_factor_cumulated of the line
    length = {"f0": 0.1, "f1": 0.05, "f2": 0.06, "s21": 0.01, "s22": 0.012, "s23": 0.011, "s24": 0.013}
    paths = {"s21": ("f0", "f1"), "s22": ("f0", "f1"), "s23": ("f0", "f2"), "s24": ("f0", "f2")}
    resistance = sum(weight * (sum(length[c] * R / cables.get(c, 1) * factor[c] for c in feeders) + length[s] * R * factor["s"])
                     for s, feeders in paths.items())
    reactance = sum(weight * (sum(length[c] * X / cables.get(c, 1) * factor[c] for c in feeders) + length[s] * X * factor["s"])
                    for s, feeders in paths.items())

    max_branch, total_r, total_x, ratio, max_branch_r, branch_r = impedance_metrics.impedance_metrics(
        net, top.create_nxgraph(net, respect_switches=True))

    assert (total_r, total_x) == (pytest.approx(resistance), pytest.approx(reactance))
    assert ratio == pytest.approx(resistance / reactance) == pytest.approx(R / X)    # one cable type: its R/X
    assert max_branch == pytest.approx(households)       # every consumer hangs behind the one feeder f0
    assert max_branch_r == pytest.approx(resistance) == pytest.approx(branch_r)


def test_the_ratio_lies_within_the_r_x_of_the_lines():
    net = build()
    net.line.loc[net.line["name"] == "f2", ["r_ohm_per_km", "x_ohm_per_km"]] = (0.125, 0.078)    # NAYY_4_240
    ratio = impedance_metrics.impedance_metrics(net, top.create_nxgraph(net, respect_switches=True))[3]
    assert 0.125 / 0.078 < ratio < R / X


def test_the_cable_length_is_the_installed_length_with_parallel_cables():
    assert net_metrics.cable_length(build(parallel_feeder=2)) == pytest.approx(0.256 + 0.1 + 0.05)


def test_the_cable_length_can_be_restricted_to_lines_in_service():
    net = build()
    net.line.loc[net.line["name"] == "f2", "in_service"] = False
    assert net_metrics.cable_length(net) == pytest.approx(0.256)
    assert net_metrics.cable_length(net, only_in_service=True) == pytest.approx(0.256 - 0.06)


def test_the_station_rating_counts_the_parallel_units_and_a_net_without_transformer_is_an_error():
    assert station_mva(build(kva=400, parallel_trafo=2)) == pytest.approx(0.8)
    assert net_metrics.transformer_power(build(kva=630)) == pytest.approx(0.63)
    net = build()
    net.trafo.drop(net.trafo.index, inplace=True)
    with pytest.raises(ValueError, match="No transformer found"):
        net_metrics.transformer_power(net)


# --- the coincident peak load of a grid ---------------------------------------------------------------------------------
def test_the_coincident_peak_load_applies_the_simultaneity_per_category():
    net = build()
    assert net_metrics.simultaneous_peak_load(net) == pytest.approx(coincident(4, 68.0))       # kW
    pp.create_load(net, net.load.at[net.load.index[0], "bus"], 0.005, max_p_mw=0.02, category="Commercial",
                   load_units=1.0)                                                   # one shop: its full 20 kW
    assert net_metrics.simultaneous_peak_load(net) == pytest.approx(coincident(4, 68.0) + 20.0)


# --- the per-transformer analysis of a PLZ, on nets stored as JSON and read back -----------------------------------------
class Cursor:
    def __init__(self, store):
        self.store, self.key = store, None

    def execute(self, query, vars=None):
        self.key = vars

    def fetchall(self):
        _, _, kcid, bcid = self.key
        net = self.store.get((kcid, bcid))
        return [] if net is None else [(json.loads(pp.to_json(net)),)]


class Client(AnalysisMixin):
    """``read_net_db`` of the real mixin on stored nets; collects what the analysis stores."""

    def __init__(self, store):
        self.store, self.cur, self.logger, self.inserted = store, Cursor(store), LOGGER, {}

    def get_list_from_plz(self, plz):
        return list(self.store) + [(9, 9)]                                           # (9, 9) was never stored

    def insert_trafo_parameters(self, plz, loads, max_distances, avg_distances):
        self.inserted = {"loads": json.loads(loads), "max": json.loads(max_distances), "avg": json.loads(avg_distances)}


def analyse(store):
    client = Client(store)
    pc = calculator()
    pc.dbc = client
    pc.analyze_transformer_parameters_for_plz(1)
    return client.inserted


def test_the_per_transformer_analysis_groups_grids_by_station_rating_with_parallel_units_counted():
    inserted = analyse({(1, 1): build(kva=400), (1, 2): build(kva=400, parallel_trafo=2), (1, 3): build(kva=630)})

    # 4 x 17 kW households: coincident load in kW and the routed distances to the consumers in m
    assert set(inserted["loads"]) == {"400", "800", "630"}
    for key in ("400", "800", "630"):
        assert inserted["loads"][key] == [pytest.approx(coincident(4, 68.0))]
        assert inserted["max"][key] == [pytest.approx(173.0)] and inserted["avg"][key] == [pytest.approx(166.5)]


def test_a_grid_that_cannot_be_read_or_has_no_loads_is_left_out_of_the_lists():
    unloaded = build()
    unloaded.load.drop(unloaded.load.index, inplace=True)
    inserted = analyse({(1, 1): build(kva=400), (1, 2): unloaded})                   # (9, 9) cannot be read at all

    assert inserted["loads"] == {"400": [pytest.approx(coincident(4, 68.0))]}


def test_the_peak_load_of_a_grid_is_its_entry_in_the_per_transformer_lists():
    stored = {(1, i): build(kva=400, load_kw=17.0 + i) for i in (1, 2, 3)}
    for (_, i), net in stored.items():
        net.line["length_km"] = net.line["length_km"] * (1 + 0.37 * i)               # different distances
    inserted = analyse(stored)

    for position, (kcid, bcid) in enumerate(stored):
        reloaded = Client(stored).read_net_db(1, kcid, bcid)
        peak = grid_analysis.key_figures(reloaded)["simultaneous_peak_load_mw"]
        assert peak == pytest.approx(coincident(4, 4 * (17.0 + bcid)) / 1000)
        assert peak * 1000 == pytest.approx(inserted["loads"]["400"][position])


def test_two_grids_with_equal_rating_and_distance_but_different_loads_get_their_own_peak_load():
    small, large = build(), build(load_kw=51.0)                                      # the same geometry, 3 x the load
    # the key figures are computed from the net of a grid alone, so no other grid of the PLZ can be taken for it
    assert grid_analysis.key_figures(small)["simultaneous_peak_load_mw"] == pytest.approx(coincident(4, 68.0) / 1000)
    peak = grid_analysis.key_figures(large)["simultaneous_peak_load_mw"]
    assert peak == pytest.approx(coincident(4, 204.0) / 1000)


# --- the workflows around it ---------------------------------------------------------------------------------------------
class Workflow:
    """The database side of ``analyze_parameters_for_plz`` and ``analyze_grid_parameters_for_plz``."""
    logger = LOGGER

    def __init__(self, generated=True, analyzed=False, grids=((1, 1), (1, 2), (1, 3)), done=()):
        self.generated, self.analyzed, self.grids = generated, analyzed, list(grids)
        self.done = dict(done)           # (kcid, bcid) -> key-figure version of the stored row
        self.events, self.conn = [], type("Conn", (), {"commit": lambda s: self.events.append("commit")})()

    def is_grid_generated(self, plz):
        return self.generated

    def is_grid_analyzed(self, plz):
        return self.analyzed

    def get_list_from_plz(self, plz):
        return self.grids

    def has_clustering_parameters(self, plz, kcid, bcid, key_figures_version):
        return self.done.get((kcid, bcid)) == key_figures_version

    def delete_plz_from_sample_set_table(self, classification_id, plz):
        self.events.append(("sample set cleared", plz))

    def rollback_changes(self):
        self.events.append("rollback")


def workflow(dbc, monkeypatch, fail_on=None):
    pc = calculator(plz=None)
    pc.dbc = dbc
    for name in ("analyze_basic_parameters_for_plz", "analyze_cable_lengths_for_plz",
                 "analyze_transformer_parameters_for_plz"):
        def step(plz, name=name):
            dbc.events.append(name)
            if name == fail_on:
                raise RuntimeError("analysis failed")
        monkeypatch.setattr(pc, name, step)

    def single(bcid, kcid):
        dbc.events.append(("grid", kcid, bcid))
        if (kcid, bcid) == (1, 2):
            raise ValueError("No transformer found")
    monkeypatch.setattr(pc, "analyze_single_grid", single)
    return pc


def test_the_plz_analysis_runs_its_three_steps_and_commits_once(monkeypatch):
    dbc = Workflow()
    workflow(dbc, monkeypatch).analyze_parameters_for_plz(80803)
    assert dbc.events == ["analyze_basic_parameters_for_plz", "analyze_cable_lengths_for_plz",
                          "analyze_transformer_parameters_for_plz", "commit"]


@pytest.mark.parametrize("state", [{"generated": False}, {"analyzed": True}])
def test_a_plz_that_is_not_generated_or_already_analyzed_is_not_analyzed_again(monkeypatch, state):
    dbc = Workflow(**state)
    workflow(dbc, monkeypatch).analyze_parameters_for_plz(80803)
    assert dbc.events == []


def test_a_failing_step_raises_without_touching_the_sample_set(monkeypatch):
    dbc = Workflow()
    with pytest.raises(RuntimeError, match="analysis failed"):
        workflow(dbc, monkeypatch, fail_on="analyze_cable_lengths_for_plz").analyze_parameters_for_plz(80803)
    assert ("sample set cleared", 80803) not in dbc.events and "commit" not in dbc.events   # CLASSIF-03


def test_the_per_grid_analysis_skips_stored_grids_and_goes_on_after_a_failing_one(monkeypatch):
    dbc = Workflow(analyzed=True, done={(1, 1): grid_analysis.KEY_FIGURES_VERSION})
    workflow(dbc, monkeypatch).analyze_grid_parameters_for_plz(80803)
    # (1, 1) is stored, (1, 2) fails and its transaction is rolled back, (1, 3) is still done
    assert dbc.events == [("grid", 1, 2), "rollback", ("grid", 1, 3)]


def test_the_per_grid_analysis_computes_rows_of_older_definitions_again(monkeypatch):
    current = grid_analysis.KEY_FIGURES_VERSION
    dbc = Workflow(analyzed=True, grids=((1, 1), (1, 3)), done={(1, 1): current - 1, (1, 3): current})
    workflow(dbc, monkeypatch).analyze_grid_parameters_for_plz(80803)
    assert dbc.events == [("grid", 1, 1)]


def test_the_per_grid_analysis_needs_the_plz_analysis_first(monkeypatch):
    dbc = Workflow(analyzed=False)
    workflow(dbc, monkeypatch).analyze_grid_parameters_for_plz(80803)
    assert dbc.events == []


def test_a_single_grid_is_stored_with_its_context_and_the_origin_of_its_transformer():
    stored = []
    pc = calculator(plz=80803)
    seen_tables = []
    pc.dbc = type("Dbc", (), {"logger": LOGGER,
                              "read_net_db": lambda s, plz, kcid, bcid, tables=None: seen_tables.append(tables) or build(),
                              "insert_clustering_parameters": lambda s, params: stored.append(params)})()
    pc.analyze_single_grid(-3, 7)       # a negative bcid is a grid around an existing (OSM) transformer
    pc.analyze_single_grid(4, 7)

    assert [(p["plz"], p["kcid"], p["bcid"], p["osm_trafo"], p["version_id"]) for p in stored] == [
        (80803, 7, -3, True, "1"), (80803, 7, 4, False, "1")]
    assert all(p["key_figures_version"] == grid_analysis.KEY_FIGURES_VERSION for p in stored)
    assert stored[0]["no_house_connections"] == 4
    assert seen_tables == [parameter_calculation.CLUSTERING_ANALYSIS_TABLES] * 2     # only the tables the key figures read


@pytest.mark.parametrize("options", [{}, {"busbar": (31,)}, {"f2_at_busbar": True, "busbar": (31, 32)},
                                     {"parallel_feeder": 2, "parallel_trafo": 2}, {"street": False, "busbar": (31, 32, 33)},
                                     {"f2_at_busbar": True, "drop": (24,)}])
def test_the_key_figures_of_the_pruned_read_equal_those_of_the_full_read(options):
    client = Client({(1, 1): build(**options)})
    full = grid_analysis.key_figures(client.read_net_db(1, 1, 1))
    pruned = grid_analysis.key_figures(client.read_net_db(1, 1, 1, tables=parameter_calculation.CLUSTERING_ANALYSIS_TABLES))
    assert pruned == full
