"""The OpenDSS backend on pylovo's specs, compared with the pandapower backend (needs the opendss extra)."""
from __future__ import annotations

import dataclasses
import logging

import pytest

altdss = pytest.importorskip("altdss")

from pylovo.electrical_backend.core.specs import BusSpec, ExtGridSpec, LineSpec, LoadSpec, TransformerSpec  # noqa: E402
from pylovo.electrical_backend.opendss.backend import OpenDSSBackend, OpenDSSBackendError  # noqa: E402

LOGGER = logging.getLogger("test")
CABLES = [("NAYY_4_150", 0.208, 0.08, 0.27, 10.0), ("NAYY_4_50", 0.642, 0.083, 0.142, 5.0)]
LOADS_KW = {21: 20.0, 22: 25.0, 23: 30.0}


def feeder_specs(parallel: int = 1) -> list:
    """A 400 kVA station with pylovo's names: a feeder of ``parallel`` cables, a branch and three consumers."""
    specs = [
        BusSpec(name="MVbus 1", voltage_kv=20.0),
        BusSpec(name="LVbus 1", voltage_kv=0.4),
        ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1.0),
        TransformerSpec(name="Trafo 1", bus1="MVbus 1", bus2="LVbus 1", kva=400.0),
        BusSpec(name="Connection Nodebus 10"),
        BusSpec(name="Connection Nodebus 11"),
        *(BusSpec(name=f"Consumer Nodebus {vertex}") for vertex in LOADS_KW),
        LineSpec(name="Line to 10", bus1="LVbus 1", bus2="Connection Nodebus 10", cable_name="NAYY_4_150",
                 length_km=0.15, parallel=parallel),
        LineSpec(name="Line to 11", bus1="Connection Nodebus 10", bus2="Connection Nodebus 11",
                 cable_name="NAYY_4_150", length_km=0.2),
        LineSpec(name="Line to 21", bus1="Connection Nodebus 10", bus2="Consumer Nodebus 21",
                 cable_name="NAYY_4_50", length_km=0.02),
        LineSpec(name="Line to 22", bus1="Connection Nodebus 11", bus2="Consumer Nodebus 22",
                 cable_name="NAYY_4_50", length_km=0.03),
        LineSpec(name="Line to 23", bus1="Connection Nodebus 11", bus2="Consumer Nodebus 23",
                 cable_name="NAYY_4_50", length_km=0.25),
    ]
    specs += [LoadSpec(name=f"Load {vertex} residential", bus=f"Consumer Nodebus {vertex}", kw=kw, kvar=0.33 * kw)
              for vertex, kw in LOADS_KW.items()]
    return specs


def build(backend, specs: list):
    """``backend`` with the grid of ``specs``; the first spec is the MV bus."""
    backend.initialize_circuit(name="PLZ1_kcid1_bcid1", source_bus=specs[0].name, primary_kv=20.0)
    backend.register_cable_types(CABLES)
    for spec in specs:
        backend.create_component(spec)
    return backend


def opendss(specs: list | None = None) -> OpenDSSBackend:
    return build(OpenDSSBackend(logger=LOGGER), feeder_specs() if specs is None else specs)


def test_the_first_circuit_of_a_process_runs_at_50_hz():
    altdss.altdss("Clear")
    altdss.altdss("Set DefaultBaseFrequency=60")        # the OpenDSS default in a new process
    backend = OpenDSSBackend(logger=LOGGER)
    backend.initialize_circuit(name="PLZ1_kcid1_bcid1", source_bus="MVbus_1", primary_kv=20.0)
    assert [source.BaseFreq for source in backend.dss.Vsource] == [50.0]


def test_pylovo_names_give_the_power_flow_of_names_without_blanks():
    backend = opendss()
    assert backend.solve_power_flow()
    metrics = backend.get_circuit_metrics()
    assert metrics["total_power_kw"] < -70                  # the source supplies the loads (negative into the grid)

    no_blanks = [dataclasses.replace(spec, **{key: getattr(spec, key).replace(" ", "_")
                                              for key in ("name", "bus", "bus1", "bus2") if hasattr(spec, key)})
                 for spec in feeder_specs()]
    backend = opendss(no_blanks)
    assert backend.solve_power_flow()
    assert backend.get_circuit_metrics() == pytest.approx(metrics)


def test_a_duplicate_element_name_gets_a_suffix():
    specs = feeder_specs() + [
        BusSpec(name="Consumer Nodebus 24"),
        LineSpec(name="Line to 23", bus1="Connection Nodebus 11", bus2="Consumer Nodebus 24",
                 cable_name="NAYY_4_50", length_km=0.05),
    ]
    backend = opendss(specs)
    assert backend.solve_power_flow()
    assert backend.get_component_count("lines") == 6
    assert {"line_to_23", "line_to_23_2"} <= {line.Name for line in backend.dss.Line}


def test_two_buses_with_the_same_opendss_name_raise():
    backend = opendss()
    with pytest.raises(OpenDSSBackendError, match="same OpenDSS name"):
        backend.create_component(BusSpec(name="Consumer_Nodebus 21"))
