"""Bus-type configs of the feeder counting for grids without the pylovo bus names: neutral by default (the load buses
are the house connections), name patterns of an export only when the caller passes them."""
import logging
from types import SimpleNamespace

import pandapower as pp
import pandapower.topology as top

from pylovo.analysis.grid_analysis import compute_comparison_parameters
from pylovo.analysis.parameter_calculation import GENERIC_BUS_TYPE_CONFIG, ParameterCalculator, classify_bus_types

#: The name patterns of an invented export: service buses, cable distribution cabinets and service cables.
EXPORT_CONFIG = {"name_column": "name", "house_connection_pattern": r"^Service", "kvs_pattern": r"^Cabinet",
                 "service_line_pattern": r"^SL_"}


def export_net():
    """Root bus 1 - branching node 2 - two feeders (3, 4) and the service bus 5 of an export."""
    net = pp.create_empty_network()
    mv = pp.create_bus(net, 10.0, name="MV")
    for name in ("Busbar", "Node_1", "Node_2", "Node_3", "Service_4"):
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
    calc.dbc = SimpleNamespace(logger=logging.getLogger("bus-type-config-test"))
    return calc


def test_without_name_patterns_every_bus_is_backbone():
    net = export_net()
    assert set(classify_bus_types(net, GENERIC_BUS_TYPE_CONFIG).values()) == {"backbone"}


def test_the_patterns_of_an_export_name_service_buses_and_cabinets():
    net = pp.create_empty_network()
    for name in ("Node_1", "Service_7", "service_2", "Cabinet_3"):
        pp.create_bus(net, 0.4, name=name)
    assert classify_bus_types(net, EXPORT_CONFIG) == {
        0: "backbone", 1: "house_connection", 2: "house_connection", 3: "kvs"}


def test_feeder_count_without_pylovo_bus_names():
    net, calc = export_net(), calculator()
    graph = top.create_nxgraph(net, include_trafos=False, respect_switches=True)
    assert not calc.uses_synthetic_bus_naming(net)
    # by name the service bus 5 is a third feeder end, unless the export patterns or the consumer buses mark it
    assert calc.count_feeders(net, graph, 1, uses_synthetic_naming=False) == 3
    assert calc.count_feeders(net, graph, 1, uses_synthetic_naming=False, bus_type_config=EXPORT_CONFIG) == 2
    assert calc.count_feeders(net, graph, 1, uses_synthetic_naming=False, additional_house_connection_buses=[5]) == 2


def test_comparison_parameters_of_a_grid_without_pylovo_bus_names():
    result = compute_comparison_parameters(calculator(), export_net())
    assert result["metric_status"] == "ok"
    assert result["uses_synthetic_naming"] is False
    assert result["root_bus"] == 1
    assert result["consumer_bus_count"] == 3                                        # the load buses
    assert result["feeder_lines"] >= 1
    assert result["transformer_mva"] == 0.4
