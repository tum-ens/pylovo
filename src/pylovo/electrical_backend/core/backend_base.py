"""Abstract interface for electrical simulation backends.

Grid generation (``GridGenerator`` and ``CableInstaller``) describes every network
element with a backend-agnostic spec from :mod:`.specs` (``BusSpec``, ``LineSpec``,
...). A backend translates these specs into calls of its simulation engine, so the
engine can be switched with ``ELECTRICAL_BACKEND`` in ``config_generation.yaml``
without touching the generation algorithm.

A backend must:

1. implement every ``@abstractmethod`` below,
2. translate each spec passed to :meth:`IElectricalBackend.create_component`,
3. follow pylovo's grid conventions (0.4 kV LV, 20 kV MV, bus names such as
   ``"LVbus 1"``, ``"MVbus 1"``, ``"Connection Nodebus <vertex>"``),
4. accept the cable catalogue passed to :meth:`IElectricalBackend.register_cable_types`,
5. report ``min_voltage_pu`` and ``max_voltage_pu`` in
   :meth:`IElectricalBackend.get_circuit_metrics` (used for the voltage-band check).
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from .specs import ComponentSpec


class IElectricalBackend(ABC):
    """Interface that every electrical simulation backend implements.

    An implementation keeps one engine-specific network object (for example the
    pandapower ``net``) per circuit. ``pylovo.electrical_backend.pandapower.backend``
    is the reference implementation; register new backends with
    :func:`pylovo.electrical_backend.factory.register_backend`.
    """

    @abstractmethod
    def initialize_circuit(self, name: str, source_bus: str,
                           primary_kv: float) -> None:
        """
        Initialize a new electrical circuit.

        Args:
            name: Circuit name
            source_bus: Name of the source bus
            primary_kv: Primary voltage level
        """

    @abstractmethod
    def create_component(self, spec: ComponentSpec) -> Any:
        """
        Create electrical component from specification.

        Args:
            spec: Component specification object

        Returns:
            Backend-specific component object
        """

    @abstractmethod
    def solve_power_flow(self) -> bool:
        """
        Solve power flow and return convergence status.

        Returns:
            True if power flow converged, False otherwise
        """

    @abstractmethod
    def export_to_format(self, filename: Optional[str] = None) -> str:
        """
        Export circuit to JSON format.

        Args:
            filename: If provided, save to this file path. If None, return JSON string only.

        Returns:
            JSON string representation of the circuit
        """

    @abstractmethod
    def cleanup(self) -> None:
        """Clean up resources and reset backend state."""

    @abstractmethod
    def get_circuit_metrics(self) -> Dict[str, Any]:
        """
        Get key circuit metrics after solving.

        Returns:
            Dictionary with circuit performance metrics
        """

    # =========================================================================
    # Query Methods - Read data from backend
    # =========================================================================

    @abstractmethod
    def register_cable_types(self, cables: list) -> None:
        """
        Register cable equipment types from database tuples.

        Args:
            cables: List of tuples (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur)
        """

    @abstractmethod
    def get_cable_types(self) -> list[str]:
        """
        Get list of all registered cable type names.

        Returns:
            List of cable type names available in the backend
        """

    @abstractmethod
    def get_component_count(self, component_type: str) -> int:
        """
        Get count of components by type.

        Args:
            component_type: One of 'buses', 'lines', 'loads', 'transformers'

        Returns:
            Number of components of the specified type
        """

    @abstractmethod
    def get_bus_coordinates(self, bus_name: str) -> tuple[float, float] | None:
        """
        Get bus geographic coordinates.

        Args:
            bus_name: Name of the bus

        Returns:
            Tuple of (x, y) coordinates, or None if not available
        """

    # =========================================================================
    # Update Methods - Modify existing components
    # =========================================================================

    @abstractmethod
    def set_bus_coordinates(self, bus_name: str, x: float, y: float) -> None:
        """
        Set bus geographic coordinates.

        Args:
            bus_name: Name of the bus
            x: X coordinate
            y: Y coordinate

        Note:
            No-op for backends without geodata support (e.g., OpenDSS)
        """

    @abstractmethod
    def set_bus_zone(self, bus_name: str, zone: str) -> None:
        """
        Set bus zone attribute.

        Args:
            bus_name: Name of the bus
            zone: Zone identifier string

        Note:
            No-op for backends without zone support (e.g., OpenDSS)
        """

    # =========================================================================
    # Station voltage (optional; see pylovo.station_voltage)
    # =========================================================================

    def get_source_voltage(self) -> float:
        """Voltage of the external grid (MV side of the station) in p.u."""
        raise NotImplementedError(f"{type(self).__name__} does not support the station voltage")

    def set_source_voltage(self, vm_pu: float) -> None:
        """Set the voltage of the external grid (MV side of the station) in p.u."""
        raise NotImplementedError(f"{type(self).__name__} does not support the station voltage")

    def get_bus_voltage_pu(self, bus_name: str) -> float:
        """Solved voltage magnitude of a bus in p.u."""
        raise NotImplementedError(f"{type(self).__name__} does not support the station voltage")

    def set_transformer_tap_steps(self, steps: int) -> int:
        """Set the station transformer's off-load tap ``steps`` steps towards a higher LV voltage.

        ``0`` is the neutral position. Returns the steps applied within the transformer's tap range.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support the station voltage")

    @abstractmethod
    def set_transformer_rating(self, trafo_name: str, rating_mva: float) -> None:
        """
        Set transformer rated power.

        Args:
            trafo_name: Name of the transformer
            rating_mva: Rated power in MVA

        Note:
            No-op for backends that set rating at creation (e.g., OpenDSS)
        """
