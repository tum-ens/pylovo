"""Bus-name patterns of a DSO export: the feeder counting uses them for grids without the pylovo bus names."""
import logging
from types import SimpleNamespace

import pandapower as pp
import pandapower.topology as top

from pylovo.analysis.grid_analysis import compute_comparison_parameters
from pylovo.analysis.parameter_calculation import DSO_BUS_TYPE_CONFIG, ParameterCalculator, classify_bus_types


def dso_net():
    """Root bus 1 - branching node 2 - two feeders (3, 4) and the house connection 5 of a DSO export."""
    net = pp.create_empty_network()
    mv = pp.create_bus(net, 10.0, name="MV")
    for name in ("NS_Busbar", "Kn_1", "Kn_2", "Kn_3", "NS_HaAn_4"):
        pp.create_bus(net, 0.4, name=name)
    pp.create_transformer_from_parameters(net, mv, 1, 0.4, 10.0, 0.4, 1.0, 4.0, 1.0, 0.1)
    pp.create_ext_grid(net, mv)
    for from_bus, to_bus, length_km in ((1, 2, 0.1), (2, 3, 0.1), (2, 4, 0.2), (2, 5, 0.01)):
        pp.create_line_from_parameters(net, from_bus, to_bus, length_km, 0.2, 0.08, 10.0, 0.3)
    for bus in (3, 4, 5):
        pp.create_load(net, bus, 0.01)
    return net


def calculator():
    calc = object.__new__(ParameterCalculator)  # without the database client
    calc.lvbus_keyword, calc.consumer_bus_keyword, calc.connection_bus_keyword = (
        "LVbus", "Consumer Nodebus", "Connection Nodebus")
    calc.dbc = SimpleNamespace(logger=logging.getLogger("dso-bus-type-config-test"))
    return calc


def test_dso_config_names_service_nodes_kvs_and_backbone():
    net = pp.create_empty_network()
    for name in ("Kn_1", "NS_HaAn_7", "NS_Kn(n)_KbAn_2", "NS_KVS_3", "ErLast_4"):
        pp.create_bus(net, 0.4, name=name)
    assert classify_bus_types(net, DSO_BUS_TYPE_CONFIG) == {
        0: "backbone", 1: "house_connection", 2: "house_connection", 3: "kvs", 4: "house_connection"}


def test_feeder_count_without_pylovo_bus_names_uses_the_dso_config():
    net, calc = dso_net(), calculator()
    graph = top.create_nxgraph(net, include_trafos=False, respect_switches=True)
    assert not calc.uses_synthetic_bus_naming(net)
    # The house connection NS_HaAn_4 is no feeder for the DSO config, but one for the pylovo config.
    assert calc.count_feeders(net, graph, 1, uses_synthetic_naming=False) == 2
    assert calc.count_feeders(net, graph, 1, uses_synthetic_naming=True) == 3


def test_comparison_parameters_of_a_dso_grid():
    result = compute_comparison_parameters(calculator(), dso_net())
    assert result["metric_status"] == "ok"
    assert result["uses_synthetic_naming"] is False
    assert result["root_bus"] == 1
    assert result["consumer_bus_count"] == 3
    assert result["feeder_lines"] >= 1
    assert result["transformer_mva"] == 0.4
