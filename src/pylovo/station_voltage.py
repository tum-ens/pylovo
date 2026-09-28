"""Station voltage of the validation power flow: LV busbar reference and off-load tap.

pylovo models one LV grid with its MV/LV transformer and the external grid on the MV side. The
voltage band of DIN EN 50160 (±10 %) is shared between the MV and the LV grid; following the
split of Niederle et al. (2026, 0.96 p.u. at the LV busbar of the station, 0.90 p.u. at the
last customer), the validation power flow

1. sets the MV-side voltage so that the LV busbar sits at ``LV_REFERENCE_VOLTAGE_PU`` at the
   operating point (neutral tap), and then
2. if a bus is still below the lower limit, lifts the LV side with the off-load tap of the
   station transformer, one step (2.5 % for the pandapower standard types) at a time, at most
   ``MAX_TAP_STEPS`` steps and never beyond the upper limit.

The resulting MV-side voltage and tap position stay in the stored net, so a later power flow of
the stored net (at ×1) reproduces the check. Without a reference and with ``MAX_TAP_STEPS: 0``
the power flow runs once as before (MV side 1.0 p.u., neutral tap).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

REFERENCE_TOLERANCE_PU = 1e-5
MAX_REFERENCE_ITERATIONS = 6


@dataclass
class StationVoltage:
    """Outcome of :func:`solve_validation_power_flow`."""

    converged: bool
    source_vm_pu: float | None = None      # MV side (external grid)
    lv_busbar_vm_pu: float | None = None   # after the tap
    tap_steps: int = 0                     # steps towards a higher LV voltage
    applied: bool = False                  # the convention was applied (backend supports it)

    def describe(self) -> str:
        if not self.applied:
            return "MV side as built, tap as built"
        lv = f"{self.lv_busbar_vm_pu:.3f}" if self.lv_busbar_vm_pu is not None else "?"
        mv = f"{self.source_vm_pu:.3f}" if self.source_vm_pu is not None else "?"
        return f"LV busbar {lv} p.u. (MV side {mv} p.u.), tap {self.tap_steps:+d} step(s)"


def _min_max(backend) -> tuple[float | None, float | None]:
    metrics = backend.get_circuit_metrics()
    return metrics.get("min_voltage_pu"), metrics.get("max_voltage_pu")


def solve_validation_power_flow(backend, reference_pu: float | None, max_tap_steps: int,
                                min_vm_pu: float, max_vm_pu: float, lv_bus: str = "LVbus 1",
                                logger: logging.Logger | None = None) -> StationVoltage:
    """Solve the validation power flow of ``backend`` with the station-voltage convention.

    Args:
        backend: Electrical backend holding the finished net (external grid on the MV side).
        reference_pu: LV busbar voltage at the operating point (``None``: keep the MV side as built).
        max_tap_steps: Maximum off-load tap steps towards a higher LV voltage (0: never).
        min_vm_pu: Lower limit of the voltage band (``POWER_FLOW_MIN_VM_PU``).
        max_vm_pu: Upper limit (``POWER_FLOW_MAX_VM_PU``); a tap step that exceeds it is undone.
        lv_bus: Name of the station's LV busbar.
        logger: For a warning when the backend does not support the convention.

    Returns:
        The :class:`StationVoltage`; the backend holds the solved net in its final state.
    """
    logger = logger or logging.getLogger(__name__)
    max_tap_steps = max(0, int(max_tap_steps or 0))
    if reference_pu is None and max_tap_steps == 0:
        return StationVoltage(converged=bool(backend.solve_power_flow()))

    try:
        backend.set_transformer_tap_steps(0)
        source = backend.get_source_voltage()
    except NotImplementedError:
        logger.warning("The electrical backend does not support the LV reference voltage and tap: "
                       "MV side and tap stay as built.")
        return StationVoltage(converged=bool(backend.solve_power_flow()))

    converged = bool(backend.solve_power_flow())
    if converged and reference_pu is not None:
        for _ in range(MAX_REFERENCE_ITERATIONS):
            diff = float(reference_pu) - float(backend.get_bus_voltage_pu(lv_bus))
            if abs(diff) < REFERENCE_TOLERANCE_PU:
                break
            source += diff
            backend.set_source_voltage(source)
            converged = bool(backend.solve_power_flow())
            if not converged:
                break

    steps = 0
    while converged and steps < max_tap_steps:
        low, _ = _min_max(backend)
        if low is None or low >= min_vm_pu:
            break
        applied = backend.set_transformer_tap_steps(steps + 1)
        if applied <= steps:          # the transformer's tap range is exhausted
            backend.set_transformer_tap_steps(steps)
            break
        converged = bool(backend.solve_power_flow())
        _, high = _min_max(backend) if converged else (None, None)
        if not converged or (high is not None and high > max_vm_pu):
            backend.set_transformer_tap_steps(steps)   # this step goes too far: back to the last one
            converged = bool(backend.solve_power_flow())
            break
        steps = applied

    lv = float(backend.get_bus_voltage_pu(lv_bus)) if converged else None
    return StationVoltage(converged=converged, source_vm_pu=float(source), lv_busbar_vm_pu=lv,
                          tap_steps=steps, applied=True)
