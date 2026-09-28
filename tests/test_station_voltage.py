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


def solve(backend, reference=0.96, steps=2, low=0.90, high=1.10):
    return solve_validation_power_flow(backend, reference, steps, low, high)


def lv(backend) -> float:
    return float(backend.net.res_bus.vm_pu[1])


def test_without_reference_and_tap_the_net_is_solved_as_built():
    b = backend_with_feeder(0.12)
    result = solve(b, reference=None, steps=0)
    assert result.converged and not result.applied
    assert b.net.ext_grid.vm_pu.iloc[0] == 1.0 and b.net.trafo.tap_pos.iloc[0] == 0


def test_the_lv_busbar_is_set_to_the_reference():
    b = backend_with_feeder(0.12)
    result = solve(b, steps=0)
    assert result.converged and result.applied and result.tap_steps == 0
    assert lv(b) == pytest.approx(0.96, abs=2e-5) and result.lv_busbar_vm_pu == pytest.approx(lv(b))
    assert 0.96 < b.net.ext_grid.vm_pu.iloc[0] < 1.0                   # the MV side covers the transformer drop
    assert result.source_vm_pu == pytest.approx(b.net.ext_grid.vm_pu.iloc[0])


def test_the_smallest_tap_that_restores_the_band_is_used():
    b = backend_with_feeder(0.15)
    assert solve(backend_with_feeder(0.15), steps=0).converged
    no_tap = backend_with_feeder(0.15)
    solve(no_tap, steps=0)
    assert no_tap.net.res_bus.vm_pu.min() < 0.90                       # at 0.96 the end of the cable is too low
    result = solve(b)
    assert result.tap_steps == 1 and b.net.res_bus.vm_pu.min() >= 0.90
    assert b.net.trafo.tap_pos.iloc[0] == b.net.trafo.tap_neutral.iloc[0] - 1   # HV-side tap: fewer HV turns
    assert lv(b) > 0.975                                                 # one step lifts the busbar by about 2.5 %


def test_the_tap_stops_at_the_maximum_and_at_the_upper_limit():
    heavy = backend_with_feeder(0.30)
    result = solve(heavy)
    assert result.tap_steps == 2 and heavy.net.res_bus.vm_pu.min() < 0.90   # still low: reported, not hidden
    capped = backend_with_feeder(0.15)
    assert solve(capped, steps=1, high=0.975).tap_steps == 0               # a step above the upper limit is undone
    assert capped.net.trafo.tap_pos.iloc[0] == capped.net.trafo.tap_neutral.iloc[0]


def test_a_second_run_on_the_stored_state_reproduces_the_result():
    b = backend_with_feeder(0.15)
    first = solve(b)
    vm_first = b.net.res_bus.vm_pu.copy()
    again = solve(b)
    assert (again.tap_steps, again.source_vm_pu) == (first.tap_steps, pytest.approx(first.source_vm_pu, abs=1e-9))
    assert (b.net.res_bus.vm_pu - vm_first).abs().max() < 1e-9


class _PlainBackend:
    """A backend without the station-voltage methods (like the OpenDSS backend)."""

    def __init__(self):
        self.solved = 0

    def solve_power_flow(self):
        self.solved += 1
        return True

    def set_transformer_tap_steps(self, steps):
        raise NotImplementedError

    def get_source_voltage(self):
        raise NotImplementedError


def test_backends_without_support_solve_once_and_warn(caplog):
    plain = _PlainBackend()
    with caplog.at_level(logging.WARNING):
        result = solve_validation_power_flow(plain, 0.96, 2, 0.9, 1.1)
    assert result.converged and not result.applied and plain.solved == 1
    assert "does not support" in caplog.text
