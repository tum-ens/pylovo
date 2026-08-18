"""
Abstract base class for electrical simulation backends.

This module defines the interface that all electrical simulation backends
must implement. It serves as a contract between pylovo's grid generation algorithms
and the underlying electrical simulation software.

Purpose
-------
Enables pylovo to support multiple electrical simulation engines
(pandapower, OpenDSS, etc.) without modifying the core grid generation logic.
Grid construction algorithms work with high-level component specifications
(BusSpec, LineSpec, etc.) rather than backend-specific API calls.

Architecture
------------
- Grid generation algorithms create ComponentSpec objects (see specs.py)
- Backend implementations translate these specs to their native API calls
- This decoupling allows easy switching between simulation engines via config

Contract Requirements
---------------------
Any backend implementation MUST:
1. Implement all @abstractmethod functions defined below
2. Accept ComponentSpec objects and translate to native API calls
3. Handle pylovo grid conventions (400V LV, 20kV MV for German grids)
4. Support cable types registered from the configured feeder and consumer cable pools
5. Return consistent circuit metrics for analysis
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class IElectricalBackend(ABC):
    """
    Abstract backend interface for electrical simulation engines.

    This class defines the required interface that all electrical backends must implement
    to be compatible with pylovo's grid generation system.

    Implementation Guide
    --------------------
    1. Inherit from this class
    2. Implement all @abstractmethod functions
    3. Store backend-specific network object (e.g., pandapower.net, dss.circuit)
    4. Translate ComponentSpec objects to native API calls in create_component()
    5. Configure backend in config_generation.yaml under ELECTRICAL_BACKEND

    Example
    -------
    See src/electrical_backend/pandapower/backend.py for reference implementation.
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
    def create_component(self, spec: "ComponentSpec") -> Any:
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
