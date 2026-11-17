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


@dataclass
class Validation:
    """Outcome of :func:`validate_operating_point` (the ``grid_result`` columns written by ``save_net``)."""

    status: str
    min_voltage_pu: float | None = None
    max_voltage_pu: float | None = None
    max_feeder_voltage_drop_pu: float | None = None
    max_service_voltage_drop_pu: float | None = None
    max_total_lv_voltage_drop_pu: float | None = None
    error: str | None = None
    station: StationVoltage | None = None

    @property
    def drops(self) -> dict[str, float | None]:
        return {"max_feeder_voltage_drop_pu": self.max_feeder_voltage_drop_pu,
                "max_service_voltage_drop_pu": self.max_service_voltage_drop_pu,
                "max_total_lv_voltage_drop_pu": self.max_total_lv_voltage_drop_pu}


def validate_operating_point(backend, min_vm_pu: float, max_vm_pu: float,
                             lv_reference_voltage_pu: float | None = None,
                             logger: logging.Logger | None = None) -> Validation:
    """Run and classify the validation power flow of a finished grid.

    Generation (``GridGenerator.validate_net``) and the load editor (:mod:`pylovo.load_editing`, with the
    parameters of the grid's version) both use this function. Status ``converged``, ``voltage_violation`` (a bus
    outside ``[min_vm_pu, max_vm_pu]``) or ``not_converged`` (also when the solver raises; ``error`` then holds the
    exception). For a pandapower net the largest feeder, service and total LV voltage drops are measured over the
    service lines (to ``Consumer Nodebus`` buses) relative to ``LVbus 1``.

    Args:
        backend: Electrical backend holding the finished net; it keeps the solved net.
        min_vm_pu: Lower voltage limit (``POWER_FLOW_MIN_VM_PU``).
        max_vm_pu: Upper voltage limit.
        lv_reference_voltage_pu: LV busbar voltage of :func:`solve_validation_power_flow`.
        logger: Passed to :func:`solve_validation_power_flow`.
    """
    result = Validation("not_converged")
    try:
        result.station = solve_validation_power_flow(backend, lv_reference_voltage_pu, logger=logger)
        if result.station.converged:
            metrics = backend.get_circuit_metrics()
            min_voltage_pu = metrics.get("min_voltage_pu")
            max_voltage_pu = metrics.get("max_voltage_pu")
            result.min_voltage_pu, result.max_voltage_pu = min_voltage_pu, max_voltage_pu

            if getattr(backend, "net", None) is not None:
                net = backend.net
                lv_buses = net.bus.index[net.bus.name == "LVbus 1"]
                consumer_buses = set(
                    net.bus.index[
                        net.bus.name.fillna("").str.startswith("Consumer Nodebus ")
                    ]
                )
                service_lines = net.line.loc[net.line.to_bus.isin(consumer_buses)]
                if len(lv_buses) == 1 and not service_lines.empty:
                    lv_voltage_pu = float(net.res_bus.at[int(lv_buses[0]), "vm_pu"])
                    feeder_drops = []
                    service_drops = []
                    total_drops = []
                    for line in service_lines.itertuples():
                        connection_voltage_pu = float(net.res_bus.at[line.from_bus, "vm_pu"])
                        consumer_voltage_pu = float(net.res_bus.at[line.to_bus, "vm_pu"])
                        feeder_drops.append(lv_voltage_pu - connection_voltage_pu)
                        service_drops.append(connection_voltage_pu - consumer_voltage_pu)
                        total_drops.append(lv_voltage_pu - consumer_voltage_pu)
                    result.max_feeder_voltage_drop_pu = max(feeder_drops)
                    result.max_service_voltage_drop_pu = max(service_drops)
                    result.max_total_lv_voltage_drop_pu = max(total_drops)

            voltage_out_of_band = False
            if min_voltage_pu is not None and min_voltage_pu < min_vm_pu:
                voltage_out_of_band = True
            if max_voltage_pu is not None and max_voltage_pu > max_vm_pu:
                voltage_out_of_band = True
            result.status = "voltage_violation" if voltage_out_of_band else "converged"
    except Exception as exc:  # stored as not_converged
        result.error = f"{type(exc).__name__}: {exc}"
    return result
