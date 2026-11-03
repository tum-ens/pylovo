"""Voltage repair (``MAX_VOLTAGE_REPAIRS``): which consumers move, how the grids are changed and built again (no database).

``GridGenerator.install_cables`` validates every grid before anything of it is stored; a grid outside the voltage band
goes to ``repair_voltage_violation``, which releases the buildings that are below the band because of the feeders
(all of them for a greenfield grid), clusters them into new greenfield grids and returns the grids to build again. A
building below the band because of its own house cable becomes its own planning node: its connection line is a feeder
section then, and if that is not enough either, it gets a station of its own at the building.
"""

import logging

import pandas as pd
import pytest

from pylovo import grid_generator, utils
from pylovo.grid_generator import GridGenerator
from test_install_cables import BUILDINGS, FakeDbc, install, pinned_configuration  # noqa: F401 (autouse fixture)
from test_install_cables import stored_net

LOGGER = logging.getLogger("voltage-repair-test")
NO_DROPS = {"max_feeder_voltage_drop_pu": None, "max_service_voltage_drop_pu": None,
            "max_total_lv_voltage_drop_pu": None}


class Backend:
    """Solved bus voltages by bus name; a missing name raises like a backend without that bus."""

    def __init__(self, voltages):
        self.voltages = voltages

    def get_bus_voltage_pu(self, name):
        return self.voltages[name]


@pytest.fixture(autouse=True)
def band(monkeypatch):
    monkeypatch.setattr(grid_generator, "POWER_FLOW_MIN_VM_PU", 0.90)
    monkeypatch.setattr(grid_generator, "MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT", 1.0)
    monkeypatch.setattr(grid_generator, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)


def buildings(*rows):
    """Buildings ``(vertex, connection point)``."""
    return pd.DataFrame({"vertice_id": [v for v, _ in rows], "connection_point": [c for _, c in rows]})


def targets(backend, rows, ont_vertice=5):
    generator = GridGenerator.__new__(GridGenerator)
    return generator._repair_targets(backend, buildings(*rows), ont_vertice=ont_vertice)


# --- which consumers move ------------------------------------------------------------------------------------------
def test_the_far_end_of_a_feeder_moves_when_a_consumer_there_is_below_the_band():
    generator = GridGenerator.__new__(GridGenerator)
    backend = Backend({
        "Consumer Nodebus 11": 0.95, "Connection Nodebus 1": 0.93,      # in the band
        "Consumer Nodebus 21": 0.89, "Connection Nodebus 2": 0.905,     # node below 0.91: the feeders took too much
        "Consumer Nodebus 22": 0.905, "Connection Nodebus 6": 0.908,    # in the band, but beyond the feeder share
        "Consumer Nodebus 31": 0.89, "Connection Nodebus 3": 0.92,      # node fine: its service cable takes the rest
        "Consumer Nodebus 41": 0.85,                                    # no bus at its node (cabinet network)
        "Consumer Nodebus 51": 0.89, "LVbus 1": 0.96,                   # at the station vertex itself
    })
    far, connections = generator._repair_targets(
        backend, buildings((11, 1), (21, 2), (22, 6), (31, 3), (41, 4), (51, 5)), ont_vertice=5)
    assert far == [2, 4, 6]
    # below the band because of their own house cables: at a street node within the feeder share (31) and at the
    # station busbar (51); their connection lines become feeders
    assert connections == [31, 51]


def test_no_node_moves_when_only_house_connections_leave_the_band():
    backend = Backend({"Consumer Nodebus 31": 0.89, "Connection Nodebus 3": 0.92,    # its service cable
                       "Consumer Nodebus 22": 0.905, "Connection Nodebus 6": 0.908})  # beyond the feeder share only
    assert targets(backend, [(31, 3), (22, 6)]) == ([], [31])


def test_a_building_that_is_its_own_feeder_node_and_stays_below_the_band_is_a_far_end():
    # its connection line is a feeder already: the next step is a station of its own
    backend = Backend({"Consumer Nodebus 31": 0.894, "Connection Nodebus 31": 0.895, "Consumer Nodebus 11": 0.95,
                       "Connection Nodebus 1": 0.93})
    assert targets(backend, [(31, 31), (11, 1)]) == ([31], [])


# --- how the grids change ------------------------------------------------------------------------------------------
class ClusterDb:
    """The writes of a repair, and a kcid whose released buildings form the greenfield grids ``new_bcids``."""

    def __init__(self, rated=None, next_bcid=4):
        self.calls, self.rated, self.next_bcid = [], rated or {}, next_bcid

    def release_buildings(self, plz, kcid, bcid, connection_points=None):
        self.calls.append(("release", bcid, connection_points))
        return 3

    def delete_grid(self, plz, kcid, bcid):
        self.calls.append(("delete", bcid))

    def get_next_greenfield_bcid(self, plz, kcid):
        return self.next_bcid

    def get_brownfield_transformer_capacity_map(self, vertices):
        return {v: self.rated[v] for v in vertices if v in self.rated}

    def get_transformer_data(self, settlement_type):
        return pd.Series([250, 400, 630]).to_numpy(), {}

    def get_settlement_type_from_plz(self, plz):
        return 2

    def get_consumer_categories(self):
        return None

    def _set_transformer_rated_power(self, plz, kcid, bcid, rating):
        self.calls.append(("rating", bcid, rating))

    def update_transformer_rated_power(self, plz, kcid, bcid):
        self.calls.append(("revise rating", bcid))

    def route_connections_as_feeders(self, plz, kcid, bcid, vertices):
        self.calls.append(("route", bcid, list(vertices)))
        return len(vertices)


def repaired(monkeypatch, bcid, building_rows, low_points, rated=None, new_bcids=(4,), connections=(), ont=900):
    generator = GridGenerator.__new__(GridGenerator)
    generator.plz, generator.dbc, generator.logger = 80803, ClusterDb(rated), LOGGER
    monkeypatch.setattr(GridGenerator, "_repair_targets",
                        lambda self, backend, df, ont_vertice: (list(low_points), list(connections)))
    dimensioned = []

    def dimension(self, plz, kcid, first_bcid=1, merge=True, replace=True, first_cut=2):
        dimensioned.append({"first_bcid": first_bcid, "merge": merge, "replace": replace, "first_cut": first_cut})
        return list(new_bcids)

    monkeypatch.setattr(GridGenerator, "dimension_bcid_for_kcid", dimension)
    monkeypatch.setattr(GridGenerator, "position_greenfield_transformers",
                        lambda self, plz, kcid, b: self.dbc.calls.append(("station", b)))
    # 100 kW per connection point
    monkeypatch.setattr(utils, "simultaneous_peak_load", lambda df, categories, nodes: 100.0 * len(nodes))
    rebuild = generator.repair_voltage_violation(2, bcid, None, buildings(*building_rows), ont_vertice=ont)
    return rebuild, generator.dbc.calls, dimensioned


def test_an_existing_station_keeps_its_other_buildings_and_is_sized_again(monkeypatch):
    rebuild, calls, dimensioned = repaired(monkeypatch, -3, [(11, 1), (21, 2), (31, 3), (32, 3)], [3])
    # the station keeps points 1 and 2: 200 kW / 0.8 = 250 -> 400 kVA; the released buildings form one area if they can
    assert rebuild == [(2, -3), (2, 4)]
    assert calls == [("release", -3, [3]), ("rating", -3, 400), ("station", 4), ("revise rating", 4)]
    assert dimensioned == [{"first_bcid": 4, "merge": False, "replace": False, "first_cut": 1}]


def test_an_imported_rating_is_kept(monkeypatch):
    rebuild, calls, _ = repaired(monkeypatch, -3, [(11, 1), (21, 2)], [2], rated={900: 630})
    assert ("rating", -3, 400) not in calls and [c for c in calls if c[0] == "rating"] == []


def test_a_station_without_buildings_left_loses_its_grid(monkeypatch):
    rebuild, calls, _ = repaired(monkeypatch, -1, [(11, 1)], [1])
    assert rebuild == [(2, 4)]
    assert calls[:2] == [("release", -1, [1]), ("delete", -1)]


def test_a_greenfield_grid_is_clustered_again_into_at_least_two(monkeypatch):
    rebuild, calls, dimensioned = repaired(monkeypatch, 2, [(11, 1), (21, 2), (31, 3)], [3], new_bcids=(4, 5))
    assert rebuild == [(2, 4), (2, 5)]
    assert calls[:2] == [("release", 2, None), ("delete", 2)]
    assert dimensioned[0]["first_cut"] == 2 and dimensioned[0]["merge"] is False


@pytest.mark.parametrize("bcid, rows, low, ont", [
    (2, [(11, 1), (12, 1)], [1], 1),     # a greenfield grid at the node of its own station cannot be helped
    (-1, [(11, 1), (21, 2)], [], 900),   # nobody below the band because of the feeders
])
def test_no_repair_applies(monkeypatch, bcid, rows, low, ont):
    rebuild, calls, dimensioned = repaired(monkeypatch, bcid, rows, low, ont=ont)
    assert rebuild == [] and calls == [] and dimensioned == []


def test_house_connections_become_feeders_and_the_grid_is_built_again(monkeypatch):
    rebuild, calls, dimensioned = repaired(monkeypatch, -3, [(11, 1), (21, 2), (31, 3)], [], connections=[31])
    assert rebuild == [(2, -3)]
    assert calls == [("route", -3, [31])] and dimensioned == []


def test_a_building_its_feeder_cannot_supply_gets_a_station_of_its_own(monkeypatch):
    # a greenfield grid of one building that is its own planning node (31), with the station still at the street
    # node it had before: the building is clustered again on its own, and its single node takes the station
    rebuild, calls, dimensioned = repaired(monkeypatch, 2, [(31, 31)], [31], new_bcids=(4,), ont=3)
    assert rebuild == [(2, 4)]
    assert calls == [("release", 2, None), ("delete", 2), ("station", 4), ("revise rating", 4)]


def test_house_connections_and_far_ends_are_repaired_together(monkeypatch):
    rebuild, calls, _ = repaired(monkeypatch, -3, [(11, 1), (21, 2), (31, 3)], [3], connections=[11])
    assert rebuild == [(2, -3), (2, 4)]
    assert calls[:2] == [("route", -3, [11]), ("release", -3, [3])]


# --- the grids are built again -------------------------------------------------------------------------------------
def test_a_grid_outside_the_band_is_planned_again_before_anything_of_it_is_stored(monkeypatch):
    statuses = iter(["voltage_violation", "converged", "converged"])
    monkeypatch.setattr(GridGenerator, "validate_net", lambda self, backend, kcid, bcid: (next(statuses), NO_DROPS))
    repairs = []

    def repair(self, kcid, bcid, backend, buildings_df, ont_vertice):
        repairs.append((kcid, bcid, ont_vertice, len(buildings_df)))
        return [(kcid, bcid), (kcid, 7)]

    monkeypatch.setattr(GridGenerator, "repair_voltage_violation", repair)
    monkeypatch.setattr(grid_generator, "MAX_VOLTAGE_REPAIRS", 5)
    dbc = FakeDbc()
    install(dbc)

    assert repairs == [(1, 1, 1, 3)]
    assert [(c["kcid"], c["bcid"], c["status"]) for c in dbc.json_calls] == [(1, 1, "converged"), (1, 7, "converged")]
    assert [line[1:] for line in dbc.lines] == [(80803, 1, 1), (80803, 1, 7)]   # nothing of the first try is flushed


@pytest.mark.parametrize("limit", [0, 2])
def test_repairs_stop_after_max_voltage_repairs(monkeypatch, limit):
    monkeypatch.setattr(GridGenerator, "validate_net", lambda self, backend, kcid, bcid: ("voltage_violation", NO_DROPS))
    repairs = []
    monkeypatch.setattr(GridGenerator, "repair_voltage_violation",
                        lambda self, kcid, bcid, *args: repairs.append(bcid) or [(kcid, bcid)])
    monkeypatch.setattr(grid_generator, "MAX_VOLTAGE_REPAIRS", limit)
    dbc = FakeDbc()
    install(dbc)

    assert len(repairs) == limit
    assert [c["status"] for c in dbc.json_calls] == ["voltage_violation"]


# --- a building as its own feeder node, a station at the building ------------------------------------------------
class OwnNodeDbc(FakeDbc):
    """The small grid of test_install_cables with house 13 as its own planning node (its connection line 3-13 is a
    feeder section); with ``station_at_house`` a grid of house 12 alone with its station at the house."""

    def __init__(self, station_at_house=False):
        super().__init__()
        self.station_at_house = station_at_house

    def get_vertices_from_bcid(self, plz, kcid, bcid):
        if self.station_at_house:
            return {}, 12, {}     # pgRouting has no route from the station to itself
        return super().get_vertices_from_bcid(plz, kcid, bcid)

    def get_buildings_from_bcid(self, plz, kcid, bcid):
        if self.station_at_house:
            return BUILDINGS.iloc[[0]].assign(agg_connection_point=[12])
        return BUILDINGS.assign(agg_connection_point=[2, 13, 3])

    def fetch_consumer_connection_mapping(self, plz):
        return {12: [12]} if self.station_at_house else {2: [12], 3: [14], 13: [13]}

    def get_ont_geom_from_bcid(self, plz, kcid, bcid):
        return (11.5001, 48.1009) if self.station_at_house else super().get_ont_geom_from_bcid(plz, kcid, bcid)


def lines_of(net):
    """``(from bus, to bus, length km, parallel)`` of every line."""
    buses = net.bus["name"]
    return sorted((buses[row.from_bus], buses[row.to_bus], round(row.length_km, 4), int(row.parallel))
                  for row in net.line.itertuples())


def test_a_building_that_is_its_own_planning_node_is_supplied_over_its_connection_line_as_a_feeder():
    dbc = OwnNodeDbc()
    install(dbc)

    lines = lines_of(stored_net(dbc))
    to_13 = [line for line in lines if line[1].endswith(" 13")]
    # the connection line 3-13 is a feeder section; the house cable stays at the building: at least 1 m, so that
    # it has an impedance
    assert to_13 == [("Connection Nodebus 13", "Consumer Nodebus 13", 0.001, 1),
                     ("Connection Nodebus 3", "Connection Nodebus 13", 0.007, 1)]
    assert dbc.json_calls[0]["status"] == "converged"


def test_a_station_at_the_building_supplies_it_from_its_busbar():
    dbc = OwnNodeDbc(station_at_house=True)
    install(dbc)

    net = stored_net(dbc)
    assert set(net.bus["name"]) == {"LVbus 1", "MVbus 1", "Consumer Nodebus 12"}
    assert lines_of(net) == [("LVbus 1", "Consumer Nodebus 12", 0.001, 1)]
    assert dbc.json_calls[0]["status"] == "converged"
