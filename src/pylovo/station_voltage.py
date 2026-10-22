"""Station voltage of the validation power flow: LV busbar reference.

pylovo models one LV grid with its MV/LV transformer and the external grid on the MV side. The
voltage band of DIN EN 50160 (±10 %) is shared between the MV and the LV grid; following the
split of Niederle et al. (2026, 0.96 p.u. at the LV busbar of the station, 0.90 p.u. at the
last customer), the validation power flow sets the MV-side voltage so that the LV busbar sits at
``LV_REFERENCE_VOLTAGE_PU`` at the operating point. The transformer keeps its neutral tap: the
reference already stands for a station whose tap is set for its place in the MV grid.

The resulting MV-side voltage stays in the stored net, so a later power flow of the stored net
(at ×1) reproduces the check. Without a reference the power flow runs once as before (MV side
1.0 p.u.).
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
    lv_busbar_vm_pu: float | None = None
    applied: bool = False                  # the reference was applied (backend supports it)

    def describe(self) -> str:
        if not self.applied:
            return "MV side as built"
        lv = f"{self.lv_busbar_vm_pu:.3f}" if self.lv_busbar_vm_pu is not None else "?"
        mv = f"{self.source_vm_pu:.3f}" if self.source_vm_pu is not None else "?"
        return f"LV busbar {lv} p.u. (MV side {mv} p.u.)"


def solve_validation_power_flow(backend, reference_pu: float | None, lv_bus: str = "LVbus 1",
                                logger: logging.Logger | None = None) -> StationVoltage:
    """Solve the validation power flow of ``backend`` with the LV busbar at ``reference_pu``.

    Args:
        backend: Electrical backend holding the finished net (external grid on the MV side).
        reference_pu: LV busbar voltage at the operating point (``None``: keep the MV side as built).
        lv_bus: Name of the station's LV busbar.
        logger: For a warning when the backend does not support the reference.

    Returns:
        The :class:`StationVoltage`; the backend holds the solved net in its final state.
    """
    logger = logger or logging.getLogger(__name__)
    if reference_pu is None:
        return StationVoltage(converged=bool(backend.solve_power_flow()))

    try:
        source = backend.get_source_voltage()
    except NotImplementedError:
        logger.warning("The electrical backend does not support the LV reference voltage: "
                       "the MV side stays as built.")
        return StationVoltage(converged=bool(backend.solve_power_flow()))

    converged = bool(backend.solve_power_flow())
    if converged:
        for _ in range(MAX_REFERENCE_ITERATIONS):
            diff = float(reference_pu) - float(backend.get_bus_voltage_pu(lv_bus))
            if abs(diff) < REFERENCE_TOLERANCE_PU:
                break
            source += diff
            backend.set_source_voltage(source)
            converged = bool(backend.solve_power_flow())
            if not converged:
                break

    lv = float(backend.get_bus_voltage_pu(lv_bus)) if converged else None
    return StationVoltage(converged=converged, source_vm_pu=float(source), lv_busbar_vm_pu=lv,
                          applied=True)
