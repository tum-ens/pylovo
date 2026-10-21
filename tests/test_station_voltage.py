"""Station voltage of the validation power flow (pylovo.station_voltage), without a database."""
from __future__ import annotations

import logging

import pandapower as pp
import pytest

from pylovo.electrical_backend.pandapower.backend import PandapowerBackend
from pylovo.station_voltage import solve_validation_power_flow


def backend_with_feeder(load_mw: float, length_km: float = 0.3) -> PandapowerBackend:
    """MV slack at 1.0 p.u., a 400 kVA station, one NAYY 4x150 cable and one load at its end."""
    net = pp.create_empty_network()
    mv = pp.create_bus(net, 20.0, name="MVbus 1")
    lv = pp.create_bus(net, 0.4, name="LVbus 1")
    end = pp.create_bus(net, 0.4, name="Consumer Nodebus 1")
    pp.create_ext_grid(net, mv, vm_pu=1.0)
    pp.create_transformer(net, mv, lv, std_type="0.4 MVA 20/0.4 kV", name="station")
    pp.create_line(net, lv, end, length_km=length_km, std_type="NAYY 4x150 SE")
    pp.create_load(net, end, p_mw=load_mw, q_mvar=load_mw * 0.33)
    backend = PandapowerBackend(logger=logging.getLogger("test"))
    backend.net = net
    return backend


def lv(backend) -> float:
    return float(backend.net.res_bus.vm_pu[1])


def test_without_reference_the_net_is_solved_as_built():
    b = backend_with_feeder(0.12)
    result = solve_validation_power_flow(b, None)
    assert result.converged and not result.applied
    assert b.net.ext_grid.vm_pu.iloc[0] == 1.0


def test_the_lv_busbar_is_set_to_the_reference():
    b = backend_with_feeder(0.12)
    result = solve_validation_power_flow(b, 0.96)
    assert result.converged and result.applied
    assert lv(b) == pytest.approx(0.96, abs=2e-5) and result.lv_busbar_vm_pu == pytest.approx(lv(b))
    assert 0.96 < b.net.ext_grid.vm_pu.iloc[0] < 1.0                   # the MV side covers the transformer drop
    assert result.source_vm_pu == pytest.approx(b.net.ext_grid.vm_pu.iloc[0])


def test_the_tap_stays_neutral_below_the_band():
    b = backend_with_feeder(0.15)
    assert solve_validation_power_flow(b, 0.96).converged
    assert b.net.res_bus.vm_pu.min() < 0.90                            # reported, not lifted by the tap
    assert b.net.trafo.tap_pos.iloc[0] == b.net.trafo.tap_neutral.iloc[0]
    assert lv(b) == pytest.approx(0.96, abs=2e-5)


def test_a_second_run_on_the_stored_state_reproduces_the_result():
    b = backend_with_feeder(0.15)
    first = solve_validation_power_flow(b, 0.96)
    vm_first = b.net.res_bus.vm_pu.copy()
    again = solve_validation_power_flow(b, 0.96)
    assert again.source_vm_pu == pytest.approx(first.source_vm_pu, abs=1e-9)
    assert (b.net.res_bus.vm_pu - vm_first).abs().max() < 1e-9


class _PlainBackend:
    """A backend without the station-voltage methods (like the OpenDSS backend)."""

    def __init__(self):
        self.solved = 0

    def solve_power_flow(self):
        self.solved += 1
        return True

    def get_source_voltage(self):
        raise NotImplementedError


def test_backends_without_support_solve_once_and_warn(caplog):
    plain = _PlainBackend()
    with caplog.at_level(logging.WARNING):
        result = solve_validation_power_flow(plain, 0.96)
    assert result.converged and not result.applied and plain.solved == 1
    assert "does not support" in caplog.text
