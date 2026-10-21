"""Backend components created by ``CableInstaller`` (station, buses, feeder lines) and the feeder line orchestration of
``GridGenerator._install_feeder_lines``; recording fakes instead of a backend and a database."""

import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from pylovo import feeder_planning
from pylovo.cable_installer import CableInstaller
from pylovo.electrical_backend import BusSpec, ExtGridSpec, LineSpec, TransformerSpec
from pylovo.feeder_planning import FeederBranch
from pylovo.grid_generator import GridGenerator

LOGGER = logging.getLogger("installer-components-test")
LON, LAT = 11.5, 48.1
CABLES = [("NAYY_4_150", 0.208, 0.080, 0.270, 25.0), ("NAYY_4_185", 0.164, 0.080, 0.313, 38.0),
          ("NAYY_4_50", 0.642, 0.083, 0.142, 11.0)]
COORDINATES = {1: (LON, LAT), 2: (LON, LAT + 0.0009), 3: (LON, LAT + 0.0018), 4: (LON + 0.0008, LAT + 0.0009)}
PATHS = {2: (2, 1), 3: (3, 2, 1), 4: (4, 2, 1)}


@pytest.fixture(autouse=True)
def planning_rules(monkeypatch):
    monkeypatch.setattr(feeder_planning, "CABLE_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(feeder_planning, "FEEDER_STANDARD_CABLE", None)
    monkeypatch.setattr(feeder_planning, "MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT", 5.0)


class Recorder:
    """Collects the created components, transformer ratings and queued line rows."""

    def __init__(self):
        self.components, self.ratings = [], []

    def create_component(self, spec):
        self.components.append(spec)

    def get_bus_coordinates(self, name):
        return None

    def set_transformer_rating(self, name, rating_mva):
        self.ratings.append((name, rating_mva))


class Database:
    def __init__(self, rated_power=None):
        self.rated_power, self.equipment = rated_power, []

    def get_transformer_rated_power_from_bcid(self, plz, kcid, bcid):
        return self.rated_power

    def set_transformer_equipment_name(self, plz, kcid, bcid, rating):
        self.equipment.append((plz, kcid, bcid, rating))


def make_installer(rated_power=None, database=None):
    backend = Recorder()
    installer = CableInstaller(
        backend, database or Database(), LOGGER, CABLES, pd.DataFrame({"name": ["NAYY_4_150", "NAYY_4_185"]}),
        pd.DataFrame({"name": ["NAYY_4_50"]}), node_coordinates=COORDINATES, transformer_coordinates=COORDINATES[1],
        transformer_rated_power=rated_power, paths_to_transformer=PATHS)
    return installer, backend


@pytest.mark.parametrize("rated_power, name, kva, parallel", [
    (100, "single 100 kva transformer", 100, 1),
    (630, "single 630 kva transformer", 630, 1),
    (500, "double 250.0 transformer", 250.0, 2),
    (800, "double 400.0 transformer", 400.0, 2),
    (1260, "double 630.0 transformer", 630.0, 2),
    (1890, "3-fold 630 transformer", 630, 3),      # above the catalogue: units of 630 kVA
    (315, "1-fold 630 transformer", 630, 1),       # off the catalogue and below one unit
])
def test_the_station_rating_decides_between_a_single_a_double_and_a_multiple_of_630_kva_units(
        rated_power, name, kva, parallel):
    database = Database()
    installer, backend = make_installer(rated_power, database)
    installer.create_transformer(80803, 1, 2)

    (spec,) = backend.components
    assert (spec.name, spec.kva, spec.parallel, spec.bus1, spec.bus2) == (name, kva, parallel, "MVbus 1", "LVbus 1")
    # the unit rating is set so that the station always totals the requested rating
    assert backend.ratings == [(name, pytest.approx(rated_power * 1e-3 / parallel))]
    assert database.equipment == [(80803, 1, 2, rated_power / parallel)]


def test_without_a_given_rating_the_stored_one_of_the_grid_is_used():
    installer, backend = make_installer(None, Database(rated_power=250))
    installer.create_transformer(80803, 1, 2)
    assert backend.components[0].name == "single 250 kva transformer"


def test_the_station_busbar_the_mv_bus_and_the_external_grid_are_created_at_the_station():
    installer, backend = make_installer()
    installer.create_lvmv_bus(80803, 1, 2)

    lv, mv, grid = backend.components
    assert (lv.name, lv.voltage_kv, lv.coordinates) == ("LVbus 1", pytest.approx(0.4), COORDINATES[1])
    assert (mv.name, mv.voltage_kv, mv.coordinates) == ("MVbus 1", 20.0, (LON, LAT + 1.5e-4))
    assert isinstance(grid, ExtGridSpec) and (grid.bus, grid.vm_pu) == ("MVbus 1", 1)


def test_the_station_vertex_gets_no_connection_bus_of_its_own():
    installer, backend = make_installer()
    installer.create_connection_bus([1, 2, 3], station_vertex=1)

    assert [spec.name for spec in backend.components] == ["Connection Nodebus 2", "Connection Nodebus 3"]
    assert all(isinstance(spec, BusSpec) for spec in backend.components)


def test_a_feeder_start_line_leaves_the_station_busbar_along_the_routed_path():
    installer, backend = make_installer()
    installer.create_connection_bus([2, 3], station_vertex=1)
    backend.components.clear()
    length = installer.create_line_start_to_lv_bus(
        80803, 2, 1, 3, {3: 200.0}, "NAYY_4_150", 2, 1, feeder_section_id=4, feeder_sizing_basis="ampacity",
        ampacity_std_type="NAYY_4_150", ampacity_parallel=2)

    assert length == pytest.approx(2 * 0.2)     # material: both parallel cables
    (line,) = backend.components
    assert isinstance(line, LineSpec)
    assert (line.name, line.bus1, line.bus2, line.parallel) == ("Line to 3", "LVbus 1", "Connection Nodebus 3", 2)
    assert line.length_km == pytest.approx(0.2) and line.feeder_section_id == 4
    # geometry: station, then the routed path without the vertex of the station itself
    assert line.coordinates == [COORDINATES[1], COORDINATES[2], COORDINATES[3]]
    assert installer._line_records[0]["from_bus"] == 1 and installer._line_records[0]["to_bus"] == 3


def test_a_node_to_node_line_is_named_after_its_far_node_and_takes_its_length_from_the_routed_distances():
    installer, backend = make_installer()
    material = {"NAYY_4_150": 0.0}
    installer.create_line_node_to_node(80803, 1, 2, [3, 2], {2: 100.0, 3: 250.0}, material, "NAYY_4_150", 1, 2,
                                       feeder_section_id=5)

    (line,) = backend.components
    assert (line.name, line.bus1, line.bus2, line.parallel) == ("Line to 3", "Connection Nodebus 2",
                                                                "Connection Nodebus 3", 2)
    assert line.length_km == pytest.approx(0.150) and line.feeder_section_id == 5
    assert material == {"NAYY_4_150": pytest.approx(2 * 0.150)}
    assert [record["line_name"] for record in installer._line_records] == ["L3"]


def test_a_line_at_the_transformer_vertex_starts_on_the_busbar_at_the_station_coordinates():
    installer, backend = make_installer()
    installer._station_vertex = 1
    installer.create_line_node_to_node(80803, 1, 2, [2, 1], {1: 0.0, 2: 100.0}, {"NAYY_4_150": 0.0}, "NAYY_4_150", 1, 1)

    (line,) = backend.components
    assert (line.bus1, line.bus2) == ("LVbus 1", "Connection Nodebus 2")
    assert line.coordinates[0] == COORDINATES[1] and line.length_km == pytest.approx(0.1)


# --- GridGenerator._install_feeder_lines ------------------------------------------------------------------------------
def test_feeder_lines_follow_the_branches_one_by_one_and_start_at_the_busbar_or_a_split_node():
    installer, backend = make_installer()
    installer.create_connection_bus([2, 3, 4], station_vertex=1)
    backend.components.clear()
    buildings = pd.DataFrame({
        "vertice_id": [12, 13, 14], "connection_point": [2, 3, 4], "residential_peak_load_in_kw": 17.0,
        "households": 1.0, "nonresidential_peak_load_in_kw": 0.0, "nonresidential_use": None,
        "nonresidential_mv_direct": None})
    consumers = pd.DataFrame({"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]})
    distances = {2: 100.0, 3: 200.0, 4: 180.0}
    # main street 1 - 2 - 3 from the busbar, node 4 splits off at node 2
    branches = [FeederBranch(0, [3, 2], 1), FeederBranch(1, [4], 2)]
    design = feeder_planning.size_feeder_tree(installer, branches, 1, distances, buildings, consumers, LOGGER, 80803)

    generator = GridGenerator.__new__(GridGenerator)
    generator.plz, generator.logger = 80803, LOGGER
    material = {name: 0.0 for name, *_ in CABLES}
    material = generator._install_feeder_lines(installer, branches, design, buildings, consumers, distances, 1, material, 1, 2)

    lines = {spec.name: (spec.bus1, spec.bus2, spec.cable_name, spec.parallel) for spec in backend.components}
    assert lines == {
        "Line to 3": ("Connection Nodebus 2", "Connection Nodebus 3", "NAYY_4_150", 1),
        "Line to 2": ("LVbus 1", "Connection Nodebus 2", "NAYY_4_150", 1),
        "Line to 4": ("Connection Nodebus 2", "Connection Nodebus 4", "NAYY_4_150", 1),
    }
    assert [spec.name for spec in backend.components] == ["Line to 3", "Line to 2", "Line to 4"]   # branch by branch
    # every line carries the feeder section it was sized in, and the material is the routed length of each
    assert {spec.name: spec.feeder_section_id for spec in backend.components} == {
        "Line to 3": design.section_by_edge[(2, 3)], "Line to 2": design.section_by_edge[(1, 2)],
        "Line to 4": design.section_by_edge[(2, 4)]}
    assert material["NAYY_4_150"] == pytest.approx(0.100 + 0.100 + 0.080)


# --- the per-grid service summary ---------------------------------------------------------------------------------------
def service_row(ampacity_drop, selected_drop, selected="NAYY_4_50", ampacity="NAYY_4_50", met=True, review=False,
                total=None):
    return {"ampacity_drop_percent": ampacity_drop, "selected_drop_percent": selected_drop,
            "selected_cable": selected, "ampacity_cable": ampacity, "voltage_drop_limit_met": met,
            "length_review": review, "total_design_drop_percent": total}


def test_the_service_summary_of_a_grid_without_services_is_empty_but_complete():
    summary = GridGenerator._summarize_service_diagnostics([])

    assert summary == {
        "ampacity_max_service_voltage_drop_percent": 0.0, "selected_max_service_voltage_drop_percent": 0.0,
        "service_voltage_drop_limit_met": True, "service_voltage_upgraded_count": 0,
        "long_service_connection_count": 0, "max_total_design_voltage_drop_percent": None}


def test_the_service_summary_takes_the_worst_drop_and_counts_upgrades_and_long_services():
    summary = GridGenerator._summarize_service_diagnostics([
        service_row(0.4, 0.4, total=2.0),
        service_row(1.6, 0.9, selected="NAYY_4_70", met=True, review=True, total=None),
        service_row(2.5, 1.5, selected="NAYY_4_150", ampacity="NAYY_4_70", met=False, total=4.5),
    ])

    assert summary["ampacity_max_service_voltage_drop_percent"] == 2.5
    assert summary["selected_max_service_voltage_drop_percent"] == 1.5
    assert summary["service_voltage_drop_limit_met"] is False
    assert summary["service_voltage_upgraded_count"] == 2
    assert summary["long_service_connection_count"] == 1
    assert summary["max_total_design_voltage_drop_percent"] == 4.5
