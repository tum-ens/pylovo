"""The batched element creation of PandapowerBackend gives the net single-element creation gave."""

import logging

import pandapower as pp
import pytest

from pylovo.config_loader import POWER_FLOW_MAX_VM_PU, POWER_FLOW_MIN_VM_PU
from pylovo.electrical_backend import BusSpec, ExtGridSpec, LineSpec, LoadSpec, TransformerSpec
from pylovo.electrical_backend.pandapower.backend import PandapowerBackend, PandapowerBackendError

CABLES = [("NAYY_4_150", 0.208, 0.08, 0.27, 25.0), ("NAYY_4_50", 0.642, 0.083, 0.142, 11.0)]


def _specs():
    """A small grid in generation order: station, feeder nodes, consumers, feeder lines, service lines."""
    buses = [BusSpec(name="LVbus 1", voltage_kv=0.4, coordinates=(11.0, 49.0)),
             BusSpec(name="MVbus 1", voltage_kv=20.0, coordinates=(11.0, 49.00015))]
    grid = [ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1),
            TransformerSpec(name="single 250 kva transformer", bus1="MVbus 1", bus2="LVbus 1", kva=250)]
    nodes = [BusSpec(name=f"Connection Nodebus {n}", voltage_kv=0.4, coordinates=(11.0 + n * 1e-4, 49.0))
             for n in range(1, 4)]
    consumers, loads = [], []
    for n in range(1, 4):
        consumers.append(BusSpec(name=f"Consumer Nodebus {100 + n}", voltage_kv=0.4,
                                 coordinates=(11.0 + n * 1e-4, 49.0003), zone="Residential"))
        loads.append(LoadSpec(name=f"Load {100 + n} Residential", bus=f"Consumer Nodebus {100 + n}", kw=4.2 * n,
                              kvar=1.38 * n, max_p_mw=0.017 * n, service_design_p_mw=0.012 * n,
                              operating_point_basis="synthetic_transformer_coincident_proportional",
                              category="Residential", load_units=float(n), consumer_vertex=100 + n))
    feeders = [LineSpec(name=f"Line to {n}", bus1="LVbus 1" if n == 1 else f"Connection Nodebus {n - 1}",
                        bus2=f"Connection Nodebus {n}", cable_name="NAYY_4_150", length_km=0.03 * n, parallel=1,
                        coordinates=[(11.0, 49.0), (11.0 + n * 1e-4, 49.0)], feeder_section_id=n // 2,
                        feeder_sizing_basis="ampacity", ampacity_std_type="NAYY_4_150", ampacity_parallel=1)
               for n in range(1, 4)]
    services = [LineSpec(name=f"Line to {100 + n}", bus1=f"Connection Nodebus {n}", bus2=f"Consumer Nodebus {100 + n}",
                         cable_name="NAYY_4_50", length_km=0.012, parallel=1,
                         coordinates=[(11.0 + n * 1e-4, 49.0), (11.0 + n * 1e-4, 49.0003)],
                         service_sizing_basis="ampacity", ampacity_std_type="NAYY_4_50", ampacity_parallel=1,
                         service_ampacity_voltage_drop_percent=0.4 * n, service_selected_voltage_drop_percent=0.4 * n,
                         service_voltage_drop_limit_met=True, service_length_review=n == 3,
                         total_design_voltage_drop_percent=2.5 + n)
                for n in range(1, 4)]
    return buses, grid, nodes, consumers, loads, feeders, services


def _backend_net(batch: bool):
    backend = PandapowerBackend(logger=logging.getLogger("batch-test"))
    backend.initialize_circuit(name="PLZ1_kcid1_bcid1", source_bus="MVbus 1", primary_kv=20.0)
    backend.register_cable_types(CABLES)
    buses, grid, nodes, consumers, loads, feeders, services = _specs()

    def create(specs):
        for spec in specs:
            backend.create_component(spec)

    if batch:
        with backend.batch():
            create(buses + grid + nodes)
            for consumer, load in zip(consumers, loads):
                create([consumer, load])
        with backend.batch():
            create(feeders + services)
    else:
        create(buses + grid + nodes)
        for consumer, load in zip(consumers, loads):
            create([consumer, load])
        create(feeders + services)
    return backend.net


def _single_element_net():
    """The same grid built the way the backend built it before batching (one pp.create_* per element)."""
    net = pp.create_empty_network(name="PLZ1_kcid1_bcid1")
    for name, r, x, i, _ in CABLES:
        pp.create_std_type(net, {"r_ohm_per_km": r, "x_ohm_per_km": x, "max_i_ka": i, "c_nf_per_km": 0.0,
                                 "q_mm2": int(name.split("_")[-1])}, name=name, element="line")
    buses, grid, nodes, consumers, loads, feeders, services = _specs()
    index = {}

    def bus(spec):
        index[spec.name] = pp.create_bus(net, name=spec.name, vn_kv=spec.voltage_kv, geodata=spec.coordinates,
                                         max_vm_pu=POWER_FLOW_MAX_VM_PU, min_vm_pu=POWER_FLOW_MIN_VM_PU, type="n",
                                         zone=spec.zone if spec.zone is not None else "n")

    for spec in buses:
        bus(spec)
    pp.create_ext_grid(net, bus=index["MVbus 1"], vm_pu=1, name="External grid")
    pp.create_transformer(net, hv_bus=index["MVbus 1"], lv_bus=index["LVbus 1"], std_type="0.25 MVA 20/0.4 kV",
                          name="single 250 kva transformer", parallel=1)
    for spec in nodes:
        bus(spec)
    for consumer, load in zip(consumers, loads):
        bus(consumer)
        pp.create_load(net, bus=index[load.bus], p_mw=load.kw / 1000.0, q_mvar=load.kvar / 1000.0, name=load.name,
                       max_p_mw=load.max_p_mw, service_design_p_mw=load.service_design_p_mw,
                       operating_point_basis=load.operating_point_basis, category=load.category,
                       load_units=load.load_units, consumer_vertex=load.consumer_vertex)
    for line in feeders + services:
        pp.create_line(net, from_bus=index[line.bus1], to_bus=index[line.bus2], length_km=line.length_km,
                       std_type=line.cable_name, name=line.name, geodata=line.coordinates, parallel=line.parallel,
                       **{a: getattr(line, a) for a in (
                           "feeder_section_id", "feeder_sizing_basis", "ampacity_std_type", "ampacity_parallel",
                           "service_sizing_basis", "service_ampacity_voltage_drop_percent",
                           "service_selected_voltage_drop_percent", "service_voltage_drop_limit_met",
                           "service_length_review", "total_design_voltage_drop_percent")})
    return net


@pytest.mark.parametrize("batch", [True, False])
def test_batched_creation_gives_the_single_element_json(batch):
    assert pp.to_json(_backend_net(batch)) == pp.to_json(_single_element_net())


def test_queued_buses_answer_lookups_and_unknown_elements_fail_at_once():
    backend = PandapowerBackend(logger=logging.getLogger("batch-test"))
    backend.initialize_circuit(name="x", source_bus="MVbus 1", primary_kv=20.0)
    backend.register_cable_types(CABLES)
    with backend.batch():
        backend.create_component(BusSpec(name="a", voltage_kv=0.4, coordinates=(1.5, 2.5)))
        assert backend.get_bus_coordinates("a") == (1.5, 2.5)
        assert backend.get_bus_coordinates("street vertex") is None
        assert len(backend._net.bus) == 0  # still queued
        with pytest.raises(PandapowerBackendError, match="Bus not found"):
            backend.create_component(LoadSpec(name="l", bus="missing"))
        with pytest.raises(PandapowerBackendError, match="Unknown standard line type"):
            backend.create_component(LineSpec(name="x", bus1="a", bus2="a", cable_name="NAYY_4_999"))
        assert list(backend.net.bus.name) == ["a"]  # reading net creates the queued elements
