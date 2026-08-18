"""
Pandapower backend implementation for pylovo.

This module implements IElectricalBackend using pandapower as the simulation engine.
Pandapower uses pandas DataFrames to represent network components.

Key features:
    - DataFrame-based network representation
    - Simple component creation via pp.create_*() functions
    - Built-in power flow solvers (Newton-Raphson, etc.)
"""

import json
import logging
from typing import Any, Dict, Optional

import pandapower as pp

from ..core.backend_base import IElectricalBackend
from ..core.specs import (
    BusSpec,
    ComponentSpec,
    LineSpec,
    LoadSpec,
    TransformerSpec,
    ExtGridSpec,
    normalize_cable_name,
)
from pylovo.config_loader import V_BAND_HIGH, V_BAND_LOW


class PandapowerBackendError(Exception):
    """Exception raised by Pandapower backend operations."""


class PandapowerBackend(IElectricalBackend):
    """
    Pandapower implementation of IElectricalBackend.

    Manages pandapower network lifecycle and component creation.
    Designed to be a drop-in replacement for direct pp.create_*() calls.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize pandapower backend."""
        self.logger = logger or logging.getLogger(__name__)
        self.net = None
        self._circuit_name = None
        self._bus_cache: Dict[str, int] = {}

    def initialize_circuit(
        self, name: str, source_bus: str, primary_kv: float,
    ) -> None:
        """Initialize pandapower network."""
        try:
            self.net = pp.create_empty_network(name=name)
            self._circuit_name = name
            self._bus_cache = {}
        except Exception as e:
            self.logger.error(f"Failed to initialize circuit: {e}")
            raise PandapowerBackendError(f"Circuit initialization failed: {e}") from e

    def create_component(self, spec: ComponentSpec) -> Any:
        """Create pandapower component from specification."""
        if self.net is None:
            raise PandapowerBackendError(
                "Backend not initialized. Call initialize_circuit() first."
            )

        try:
            if isinstance(spec, BusSpec):
                return self._create_bus(spec)
            elif isinstance(spec, TransformerSpec):
                return self._create_transformer(spec)
            elif isinstance(spec, LineSpec):
                return self._create_line(spec)
            elif isinstance(spec, LoadSpec):
                return self._create_load(spec)
            elif isinstance(spec, ExtGridSpec):
                return self._create_ext_grid(spec)
            else:
                raise PandapowerBackendError(
                    f"Unknown component spec type: {type(spec).__name__}"
                )
        except Exception as e:
            self.logger.error(f"Failed to create component {spec.name}: {e}")
            raise PandapowerBackendError(f"Component creation failed: {e}") from e

    # =========================================================================
    # Private Component Creation Methods
    # =========================================================================

    def _create_bus(self, spec: BusSpec) -> int:
        """Create bus from specification."""
        zone = spec.zone if spec.zone is not None else "n"
        bus_idx = pp.create_bus(
            self.net,
            name=spec.name,
            vn_kv=spec.voltage_kv,
            geodata=spec.coordinates,
            max_vm_pu=V_BAND_HIGH,
            min_vm_pu=V_BAND_LOW,
            type="n",
            zone=zone
        )
        self._bus_cache[spec.name] = bus_idx
        self.logger.debug(f"Created bus: {spec.name} (vn={spec.voltage_kv}kV)")
        return bus_idx

    def _create_transformer(self, spec: TransformerSpec) -> int:
        """Create transformer from specification."""
        mv_bus = self._get_bus_index(spec.bus1)
        lv_bus = self._get_bus_index(spec.bus2)

        sn_mva = spec.kva / 1000.0
        std_type = f"{sn_mva} MVA 20/0.4 kV"

        trafo_idx = pp.create_transformer(
            self.net,
            hv_bus=mv_bus,
            lv_bus=lv_bus,
            std_type=std_type,
            name=spec.name,
            parallel=spec.parallel
        )
        self.logger.debug(f"Created transformer: {spec.name} (kva={spec.kva})")
        return trafo_idx

    def _create_line(self, spec: LineSpec) -> int:
        """Create line/cable from specification."""
        from_bus = self._get_bus_index(spec.bus1)
        to_bus = self._get_bus_index(spec.bus2)

        std_type = spec.cable_name if spec.cable_name else "NAYY_4_150"

        line_idx = pp.create_line(
            self.net,
            from_bus=from_bus,
            to_bus=to_bus,
            length_km=spec.length_km,
            std_type=std_type,
            name=spec.name,
            geodata=spec.coordinates,
            parallel=spec.parallel
        )
        self.logger.debug(
            f"Created line: {spec.name} (length={spec.length_km:.3f}km, type={std_type})"
        )
        return line_idx

    def _create_load(self, spec: LoadSpec) -> int:
        """Create load from specification."""
        bus = self._get_bus_index(spec.bus)
        p_mw = spec.kw / 1000.0

        load_idx = pp.create_load(
            self.net,
            bus=bus,
            p_mw=p_mw,
            name=spec.name,
            max_p_mw=spec.max_p_mw
        )
        self.logger.debug(
            f"Created load: {spec.name} (kw={spec.kw:.1f}, kvar={spec.kvar:.1f})"
        )
        return load_idx

    def _create_ext_grid(self, spec: ExtGridSpec) -> int:
        """Create external grid from specification."""
        bus = self._get_bus_index(spec.bus)
        ext_grid_idx = pp.create_ext_grid(
            self.net, bus=bus, vm_pu=spec.vm_pu, name=spec.name
        )
        return ext_grid_idx

    def _get_bus_index(self, bus_name: str) -> int:
        """Get bus index from name using cache."""
        if bus_name in self._bus_cache:
            return self._bus_cache[bus_name]

        buses = self.net.bus[self.net.bus.name == bus_name]
        if buses.empty:
            raise ValueError(f"Bus not found: {bus_name}")

        bus_idx = buses.index[0]
        self._bus_cache[bus_name] = bus_idx
        return bus_idx

    # =========================================================================
    # Cable Registration
    # =========================================================================

    def register_cable_types(self, cables: list) -> None:
        """Register cable standard types from equipment data."""
        for cable in cables:
            name, r_ohm_per_km, x_ohm_per_km, max_i_ka, _cost_eur = cable
            normalized = normalize_cable_name(name)
            q_mm2 = int(name.split("_")[-1])

            pp.create_std_type(
                self.net,
                {
                    "r_ohm_per_km": float(r_ohm_per_km),
                    "x_ohm_per_km": float(x_ohm_per_km),
                    "max_i_ka": float(max_i_ka),
                    "c_nf_per_km": float(0),
                    "q_mm2": q_mm2
                },
                name=normalized,
                element="line",
            )
        self.logger.debug(f"Created {len(cables)} standard cable types")

    # =========================================================================
    # Power Flow & Analysis
    # =========================================================================

    def solve_power_flow(self) -> bool:
        """Solve power flow using Newton-Raphson."""
        if self.net is None:
            raise PandapowerBackendError("No network available for power flow analysis")

        try:
            self.logger.debug("Solving power flow...")
            pp.runpp(self.net, algorithm='nr', init='auto')

            converged = self.net.converged
            if converged:
                self.logger.debug("Power flow converged")
            else:
                self.logger.warning("Power flow did not converge")
            return converged

        except Exception as e:
            self.logger.error(f"Power flow failed: {e}")
            return False

    def get_circuit_metrics(self) -> Dict[str, Any]:
        """Get circuit metrics after power flow solution."""
        if self.net is None:
            return {}

        metrics = {
            "name": self._circuit_name,
            "num_buses": len(self.net.bus),
            "num_lines": len(self.net.line),
            "num_transformers": len(self.net.trafo),
            "num_loads": len(self.net.load),
        }

        if hasattr(self.net, 'converged'):
            metrics["converged"] = self.net.converged

        if hasattr(self.net, 'res_bus') and not self.net.res_bus.empty:
            vm_pu = self.net.res_bus.vm_pu
            metrics["min_voltage_pu"] = float(vm_pu.min())
            metrics["max_voltage_pu"] = float(vm_pu.max())
            metrics["avg_voltage_pu"] = float(vm_pu.mean())

        if hasattr(self.net, 'res_line') and not self.net.res_line.empty:
            metrics["total_losses_mw"] = float(self.net.res_line.pl_mw.sum())

        return metrics

    # =========================================================================
    # Export & Cleanup
    # =========================================================================

    def export_to_format(self, filename: Optional[str] = None) -> str:
        """Export circuit to JSON format."""
        if self.net is None:
            raise PandapowerBackendError("No network available for export")

        try:
            if filename:
                pp.to_json(self.net, filename=filename)
                with open(filename, 'r') as f:
                    json_str = f.read()
                self.logger.debug(f"Exported to JSON file: {filename}")
            else:
                json_str = pp.to_json(self.net)
                self.logger.debug("Exported to JSON")
            return json_str

        except Exception as e:
            self.logger.error(f"JSON export failed: {e}")
            raise PandapowerBackendError(f"JSON export failed: {e}") from e

    def cleanup(self) -> None:
        """Clean up network resources."""
        if self.net:
            self.net = None
            self._bus_cache = {}
            self.logger.debug("Cleaned up network")
        self._circuit_name = None

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_cable_types(self) -> list[str]:
        """Get all registered cable type names."""
        if self.net is None:
            return []
        return list(self.net.std_types.get("line", {}).keys())

    def get_component_count(self, component_type: str) -> int:
        """Get component count by type."""
        if self.net is None:
            return 0
        type_map = {
            "buses": "bus",
            "lines": "line",
            "loads": "load",
            "transformers": "trafo",
        }
        df_name = type_map.get(component_type, component_type)
        df = getattr(self.net, df_name, None)
        return len(df) if df is not None else 0

    def get_bus_coordinates(self, bus_name: str) -> tuple[float, float] | None:
        """Get bus geographic coordinates from GeoJSON format."""
        if self.net is None or self.net.bus.empty:
            return None
        try:
            bus_idx = self._get_bus_index(bus_name)
        except ValueError:
            return None
        geo_str = self.net.bus.at[bus_idx, "geo"]
        if geo_str:
            geo_data = json.loads(geo_str)
            coords = geo_data["coordinates"]
            return (float(coords[0]), float(coords[1]))
        return None

    # =========================================================================
    # Update Methods
    # =========================================================================

    def set_bus_coordinates(self, bus_name: str, x: float, y: float) -> None:
        """Set bus geographic coordinates in GeoJSON format."""
        if self.net is None:
            return
        try:
            bus_idx = self._get_bus_index(bus_name)
            geo_json = json.dumps({"coordinates": [x, y], "type": "Point"})
            self.net.bus.at[bus_idx, "geo"] = geo_json
        except ValueError:
            pass

    def set_bus_zone(self, bus_name: str, zone: str) -> None:
        """Set bus zone attribute."""
        if self.net is None:
            return
        try:
            bus_idx = self._get_bus_index(bus_name)
            self.net.bus.at[bus_idx, "zone"] = zone
        except ValueError:
            pass

    def set_transformer_rating(self, trafo_name: str, rating_mva: float) -> None:
        """Set transformer rated power."""
        if self.net is None:
            return
        trafo_df = self.net.trafo[self.net.trafo["name"] == trafo_name]
        if not trafo_df.empty:
            trafo_idx = trafo_df.index[0]
            self.net.trafo.at[trafo_idx, "sn_mva"] = rating_mva
