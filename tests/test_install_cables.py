"""``GridGenerator.install_cables`` and ``save_net`` on a small grid: fake database client, real offline pandapower backend.

Grid (the station is vertex 1, 250 kVA): a street 1 - 2 (100 m) - 3 (200 m); one household of 17 kW at vertex 12 (connected
at 2) and two at 13 and 14 (connected at 3). The planning rules asserted are the documented ones: the cheapest feeder
cable that carries the current over ``CABLE_PLANNING_UTILIZATION``, the cheapest house-connection cable within the
1 % service limit, and the validation power flow with the LV busbar at ``LV_REFERENCE_VOLTAGE_PU``.
"""

import logging
from types import SimpleNamespace

import pandapower as pp
import pandas as pd
import pytest

from pylovo import feeder_planning, grid_generator
from pylovo.electrical_backend import BusSpec, ExtGridSpec, LineSpec, LoadSpec, TransformerSpec, create_backend
from pylovo.grid_generator import GridGenerator

LOGGER = logging.getLogger("install-cables-test")
CONSUMERS = pd.DataFrame({"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]})
# (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur): feeder cables, then house-connection cables
CABLES = [
    ("NAYY_4_150", 0.208, 0.080, 0.270, 25.0), ("NAYY_4_185", 0.164, 0.080, 0.313, 38.0),
    ("NAYY_4_240", 0.127, 0.080, 0.357, 45.0), ("NAYY_4_300", 0.100, 0.080, 0.425, 58.0),
    ("NAYY_4_50", 0.642, 0.083, 0.142, 11.0), ("NAYY_4_70", 0.443, 0.082, 0.173, 16.0),
]
LON, LAT = 11.5, 48.1
COORDINATES = {1: (LON, LAT), 2: (LON, LAT + 0.0009), 3: (LON, LAT + 0.0018), 12: (LON + 0.0001, LAT + 0.0009),
               13: (LON + 0.0001, LAT + 0.0018), 14: (LON - 0.0001, LAT + 0.0018)}
DISTANCES = {2: 100.0, 3: 200.0, 12: 107.0, 13: 207.0, 14: 207.0}
PATHS = {2: (2, 1), 3: (3, 2, 1), 12: (12, 2, 1), 13: (13, 3, 2, 1), 14: (14, 3, 2, 1)}
BUILDINGS = pd.DataFrame({
    "vertice_id": [12, 13, 14], "connection_point": [2, 3, 3], "residential_peak_load_in_kw": 17.0, "households": 1.0,
    "nonresidential_peak_load_in_kw": 0.0, "nonresidential_use": None, "nonresidential_mv_direct": None,
})


@pytest.fixture(autouse=True)
def pinned_configuration(monkeypatch):
    for module in (feeder_planning, grid_generator):
        for name, value in {"CABLE_PLANNING_UTILIZATION": 0.8, "FEEDER_STANDARD_CABLE": None,
                            "MAX_PARALLEL_FEEDER_CABLES": 4, "MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT": 5.0,
                            "MIN_SHARED_PREFIX_LENGTH_M": 50, "FEEDER_SPLIT_MAX_CURRENT_KA": 0.4}.items():
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(grid_generator, "ELECTRICAL_BACKEND", "pandapower")
    monkeypatch.setattr(grid_generator, "HOUSE_CONNECTION", "joint")
    monkeypatch.setattr(grid_generator, "CABINET_SPACING_M", 100)
    monkeypatch.setattr(grid_generator, "LV_REFERENCE_VOLTAGE_PU", 0.96)
    monkeypatch.setattr(grid_generator, "POWER_FLOW_MIN_VM_PU", 0.90)
    monkeypatch.setattr(grid_generator, "POWER_FLOW_MAX_VM_PU", 1.10)
    monkeypatch.setattr(grid_generator, "SAVE_GRID_FOLDER", False)
    monkeypatch.setattr(grid_generator, "FEEDER_CABLES", pd.DataFrame({"name": [c[0] for c in CABLES[:4]]}))
    monkeypatch.setattr(grid_generator, "CONSUMER_CONNECTION_CABLES", pd.DataFrame({"name": [c[0] for c in CABLES[4:]]}))


class Saver:
    """What ``save_net`` writes."""

    def __init__(self):
        self.json_calls, self.sql_calls = [], []

    def save_pp_net_with_json(self, plz, kcid, bcid, json_string, description, status, **diagnostics):
        self.json_calls.append({"plz": plz, "kcid": kcid, "bcid": bcid, "json": json_string,
                                "description": description, "status": status, **diagnostics})

    def save_pandapower_net_with_sql(self, plz, kcid, bcid, net):
        self.sql_calls.append((plz, kcid, bcid, net))


class FakeDbc(Saver):
    """The lookups and writes of ``install_cables`` for the small grid, recording what is written."""

    def __init__(self, clusters=((1, 1),), fail_visualization=False):
        super().__init__()
        self.clusters, self.fail_visualization = list(clusters), fail_visualization
        self.lines, self.sql, self.helpers, self.equipment = [], [], [], []
        self.cur = SimpleNamespace(execute=self.sql.append)

    def get_list_from_plz(self, plz):
        return self.clusters

    def get_consumer_categories(self):
        return CONSUMERS

    def fetch_node_coordinates(self, plz):
        return dict(COORDINATES)

    def fetch_house_connection_points(self, plz):
        return {}

    def fetch_consumer_connection_mapping(self, plz):
        return {2: [12], 3: [13, 14]}

    def fetch_cables(self):
        return CABLES

    def get_vertices_from_bcid(self, plz, kcid, bcid):
        return dict(DISTANCES), 1, dict(PATHS)

    def get_buildings_from_bcid(self, plz, kcid, bcid):
        return BUILDINGS.copy()

    def get_ont_geom_from_bcid(self, plz, kcid, bcid):
        return COORDINATES[1]

    def get_transformer_rated_power_from_bcid(self, plz, kcid, bcid):
        return 250

    def set_transformer_equipment_name(self, plz, kcid, bcid, rating):
        self.equipment.append((plz, kcid, bcid, rating))

    def insert_lines_batch(self, records, plz, kcid, bcid):
        self.lines.append((list(records), plz, kcid, bcid))

    def rebuild_lines_result_helpers_for_split_topology(self, plz, kcid, bcid, edges):
        self.helpers.append(("helpers", edges))
        if self.fail_visualization:
            raise RuntimeError("helper SQL failed")

    def rebuild_split_points_for_split_topology(self, plz, kcid, bcid, nodes, cabinet_kinds):
        self.helpers.append(("split points", nodes, cabinet_kinds))

    def rebuild_lines_result_view_for_grid(self, plz, kcid, bcid):
        self.helpers.append(("view",))


def install(dbc, caplog=None):
    generator = GridGenerator.__new__(GridGenerator)
    generator.plz, generator.dbc, generator.logger = 80803, dbc, LOGGER
    generator.install_cables()
    return generator


def stored_net(dbc):
    (call,) = dbc.json_calls
    return pp.from_json_string(call["json"])


def test_one_grid_gets_the_cheapest_cables_and_is_stored_with_a_converged_power_flow():
    dbc = FakeDbc()
    install(dbc)

    (call,) = dbc.json_calls
    assert (call["plz"], call["kcid"], call["bcid"]) == (80803, 1, 1)
    assert call["status"] == "converged" and call["description"] == "single 250 kva transformer"
    net = pp.from_json_string(call["json"])
    lines = net.line.set_index("name")
    # feeder: 0.037 kA of coincident load over 0.8 needs no more than the cheapest feeder cable; services the cheapest
    assert lines.loc[["Line to 2", "Line to 3"], "std_type"].tolist() == ["NAYY_4_150", "NAYY_4_150"]
    assert lines.loc[["Line to 12", "Line to 13", "Line to 14"], "std_type"].tolist() == ["NAYY_4_50"] * 3
    assert lines.loc["Line to 3", "length_km"] == pytest.approx(0.100)
    assert set(net.bus["name"]) == {"LVbus 1", "MVbus 1", "Connection Nodebus 2", "Connection Nodebus 3",
                                    "Consumer Nodebus 12", "Consumer Nodebus 13", "Consumer Nodebus 14"}
    assert len(net.load) == 3 and net.trafo["sn_mva"].tolist() == [0.25]
    # the planning diagnostics and the voltage drops of the power flow are stored with the grid
    assert call["feeder_voltage_drop_limit_met"] is True and call["service_voltage_drop_limit_met"] is True
    assert call["service_voltage_upgraded_count"] == 0 and call["long_service_connection_count"] == 0
    assert 0 < call["max_total_lv_voltage_drop_pu"] < 0.01
    assert dbc.equipment == [(80803, 1, 1, 250.0)]
    assert [(plz, kcid, bcid) for plz, kcid, bcid, _ in dbc.sql_calls] == [(80803, 1, 1)]


def test_the_lines_are_queued_once_with_their_feeder_section_and_flushed_before_the_gis_helpers():
    dbc = FakeDbc()
    install(dbc)

    (records, plz, kcid, bcid), = dbc.lines
    assert (plz, kcid, bcid) == (80803, 1, 1)
    by_name = {record["line_name"]: record for record in records}
    assert set(by_name) == {"L2", "L3", "L12", "L13", "L14"}
    assert [(by_name[name]["from_bus"], by_name[name]["to_bus"]) for name in ("L2", "L3")] == [(1, 2), (2, 3)]
    assert by_name["L2"]["feeder_section_id"] == by_name["L3"]["feeder_section_id"] == 0   # one section, no split
    assert by_name["L12"]["feeder_section_id"] is None and by_name["L12"]["std_type"] == "NAYY_4_50"
    assert dbc.sql == ["SAVEPOINT split_visualization_80803_1_1", "RELEASE SAVEPOINT split_visualization_80803_1_1"]
    assert dbc.helpers == [("helpers", []), ("split points", [], None), ("view",)]


def test_a_failing_gis_helper_rolls_back_its_savepoint_and_the_grid_is_still_stored(caplog):
    dbc = FakeDbc(fail_visualization=True)
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        install(dbc)

    assert dbc.sql == ["SAVEPOINT split_visualization_80803_1_1", "ROLLBACK TO SAVEPOINT split_visualization_80803_1_1",
                       "RELEASE SAVEPOINT split_visualization_80803_1_1"]
    assert any("Skipped split visualization rebuild" in message for message in caplog.messages)
    assert [call["status"] for call in dbc.json_calls] == ["converged"]


def test_a_cabinet_network_plans_cabinets_and_connects_every_house_from_one(monkeypatch):
    monkeypatch.setattr(grid_generator, "HOUSE_CONNECTION", "cabinet")
    dbc = FakeDbc()
    install(dbc)

    net = stored_net(dbc)
    lines = net.line.set_index("name")
    # cabinets: an added one 100 m from the station (node 2) and the end cabinet (node 3)
    assert dbc.helpers[1] == ("split points", [], {2: "added_cabinet", 3: "end_cabinet"})
    buses = net.bus["name"]
    from_bus = lambda name: buses[lines.loc[name, "from_bus"]]
    assert from_bus("Line to 12") == "Connection Nodebus 2"
    assert from_bus("Line to 13") == from_bus("Line to 14") == "Connection Nodebus 3"


def test_the_power_flow_statuses_of_every_grid_are_counted(monkeypatch, caplog):
    statuses = iter(["converged", "voltage_violation", "not_converged"])
    monkeypatch.setattr(GridGenerator, "save_net", lambda self, backend, kcid, bcid, planning_diagnostics=None: next(statuses))
    dbc = FakeDbc(clusters=[(1, 1), (1, 2), (1, 3)])
    with caplog.at_level(logging.INFO, logger=LOGGER.name):
        install(dbc)

    summary = [message for message in caplog.messages if message.startswith("Cable installation finished")]
    assert len(summary) == 1
    assert ("processed_clusters=3, power_flow_converged=1/3, power_flow_not_converged=1, "
            "voltage_band_violations=1") in summary[0]


# --- save_net ------------------------------------------------------------------------------------------------------
def small_backend(load_kw, feeder_km):
    """Station, one feeder to a connection node, one service cable to a consumer with ``load_kw``."""
    backend = create_backend("pandapower", logger=LOGGER)
    backend.initialize_circuit("test", "MVbus 1", 20.0)
    backend.register_cable_types(CABLES)
    with backend.batch():
        backend.create_component(BusSpec(name="LVbus 1", voltage_kv=0.4, coordinates=(LON, LAT)))
        backend.create_component(BusSpec(name="MVbus 1", voltage_kv=20.0, coordinates=(LON, LAT + 1.5e-4)))
        backend.create_component(ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1))
    backend.create_component(TransformerSpec(name="single 250 kva transformer", bus1="MVbus 1", bus2="LVbus 1", kva=250,
                                             parallel=1))
    with backend.batch():
        backend.create_component(BusSpec(name="Connection Nodebus 2", voltage_kv=0.4, coordinates=(LON, LAT + 0.001)))
        backend.create_component(BusSpec(name="Consumer Nodebus 12", voltage_kv=0.4, coordinates=(LON, LAT + 0.0011),
                                         zone="Residential"))
        backend.create_component(LoadSpec(name="Load 12 Residential", bus="Consumer Nodebus 12", kw=load_kw,
                                          kvar=load_kw * 0.33, max_p_mw=load_kw * 1e-3,
                                          service_design_p_mw=load_kw * 1e-3, category="Residential", load_units=1.0,
                                          consumer_vertex=12))
        backend.create_component(LineSpec(name="Line to 2", bus1="LVbus 1", bus2="Connection Nodebus 2",
                                          cable_name="NAYY_4_150", length_km=feeder_km, parallel=1))
        backend.create_component(LineSpec(name="Line to 12", bus1="Connection Nodebus 2", bus2="Consumer Nodebus 12",
                                          cable_name="NAYY_4_50", length_km=0.01, parallel=1))
    return backend


def save(backend, saver=None, **kwargs):
    saver = saver or Saver()
    generator = GridGenerator.__new__(GridGenerator)
    generator.plz, generator.dbc, generator.logger = 80803, saver, LOGGER
    status = generator.save_net(backend, 1, 2, **kwargs)
    return status, saver


def test_a_converged_grid_is_stored_with_the_voltage_drops_of_its_service_lines():
    backend = small_backend(load_kw=10.0, feeder_km=0.2)
    status, saver = save(backend)

    assert status == "converged"
    (call,) = saver.json_calls
    assert call["status"] == "converged" and call["description"] == "single 250 kva transformer"
    net = backend.net
    bus = lambda name: net.res_bus.at[net.bus.index[net.bus.name == name][0], "vm_pu"]
    assert bus("LVbus 1") == pytest.approx(0.96, abs=1e-4)                  # the station voltage reference
    assert call["max_feeder_voltage_drop_pu"] == pytest.approx(bus("LVbus 1") - bus("Connection Nodebus 2"))
    assert call["max_service_voltage_drop_pu"] == pytest.approx(bus("Connection Nodebus 2") - bus("Consumer Nodebus 12"))
    assert call["max_total_lv_voltage_drop_pu"] == pytest.approx(bus("LVbus 1") - bus("Consumer Nodebus 12"))
    assert call["feeder_voltage_drop_limit_met"] is None                    # no planning diagnostics were given
    assert len(saver.sql_calls) == 1


def test_a_grid_below_the_voltage_band_is_stored_as_a_voltage_violation(caplog):
    # 120 kW at the end of 800 m: the consumer bus drops below POWER_FLOW_MIN_VM_PU
    backend = small_backend(load_kw=120.0, feeder_km=0.8)
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        status, saver = save(backend, planning_diagnostics={"feeder_voltage_drop_limit_met": False})

    assert status == "voltage_violation" and saver.json_calls[0]["status"] == "voltage_violation"
    assert backend.get_circuit_metrics()["min_voltage_pu"] < 0.90
    assert saver.json_calls[0]["feeder_voltage_drop_limit_met"] is False and len(saver.sql_calls) == 1
    assert any("will be stored with status=voltage_violation" in message for message in caplog.messages)


class BrokenBackend:
    """A backend whose power flow does not converge, or raises."""

    net = SimpleNamespace(trafo=SimpleNamespace(name=["single 250 kva transformer"]))

    def __init__(self, error=None):
        self.error = error

    def get_source_voltage(self):
        return 1.0

    def solve_power_flow(self):
        if self.error:
            raise self.error
        return False

    def export_to_format(self, filename=None):
        return '{"fake": "net"}'


@pytest.mark.parametrize("error", [None, RuntimeError("boom")])
def test_a_grid_without_a_power_flow_result_is_stored_as_not_converged(error):
    status, saver = save(BrokenBackend(error))

    assert status == "not_converged"
    (call,) = saver.json_calls
    assert call["status"] == "not_converged" and call["json"] == '{"fake": "net"}'
    assert call["max_total_lv_voltage_drop_pu"] is None


def test_a_failed_json_export_stores_the_status_without_a_net_and_skips_the_sql_tables(monkeypatch, caplog):
    backend = small_backend(load_kw=10.0, feeder_km=0.2)
    monkeypatch.setattr(backend, "export_to_format", lambda filename=None: (_ for _ in ()).throw(RuntimeError("disk")))
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        status, saver = save(backend)

    assert status == "converged"
    assert saver.json_calls[0]["json"] is None and saver.sql_calls == []
    assert any("JSON export failed" in message for message in caplog.messages)


def test_without_planning_diagnostics_the_stored_columns_are_null():
    status, saver = save(small_backend(load_kw=10.0, feeder_km=0.2))

    (call,) = saver.json_calls
    for key in ("ampacity_max_feeder_voltage_drop_percent", "selected_max_feeder_voltage_drop_percent",
                "feeder_voltage_drop_limit_met", "ampacity_max_service_voltage_drop_percent",
                "selected_max_service_voltage_drop_percent", "service_voltage_drop_limit_met",
                "service_voltage_upgraded_count", "long_service_connection_count",
                "max_total_design_voltage_drop_percent"):
        assert call[key] is None


def test_the_grid_file_is_written_when_save_grid_folder_is_set(monkeypatch, tmp_path):
    monkeypatch.setattr(grid_generator, "SAVE_GRID_FOLDER", True)
    monkeypatch.setattr(grid_generator, "RESULT_DIR", str(tmp_path))
    monkeypatch.setattr(grid_generator, "VERSION_ID", "7")
    save(small_backend(load_kw=10.0, feeder_km=0.2))

    assert pp.from_json(str(tmp_path / "grids" / "version_7" / "80803" / "kcid1bcid2.json")).bus.shape[0] == 4
