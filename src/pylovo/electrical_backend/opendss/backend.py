"""
OpenDSS backend implementation for pylovo.

This module implements IElectricalBackend using OpenDSS as the simulation engine.
OpenDSS uses a stateful circuit model with explicit component ordering.

Key features:
    - Stateful circuit model (components must be created in order)
    - Line code caching for efficient cable type reuse
    - German distribution standards (20kV/0.4kV, 3-phase, 50Hz)

Note:
    Unlike pandapower's DataFrame-based model, OpenDSS requires line codes
    to be created before lines. This is handled internally via _create_line_code().
"""

import json
import logging
import os
import re
import shutil
from datetime import datetime
from typing import Any, Dict, Optional

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
from ..core.equipment import CableEquipment, TransformerEquipment

# Import Altdss with fallback
try:
    import altdss
except ImportError:
    altdss = None


class OpenDSSBackendError(Exception):
    """Exception raised by OpenDSS backend operations."""


class OpenDSSBackend(IElectricalBackend):
    """
    OpenDSS implementation of IElectricalBackend.

    Manages OpenDSS circuit lifecycle and component creation.
    Uses internal line code caching for efficient cable type management.
    """

    def __init__(self, logger: Optional[object] = None):
        """Initialize OpenDSS backend."""
        self.logger = logger or logging.getLogger(__name__)
        self.dss = None
        self._circuit_name = None
        self.cable_registry: Dict[str, CableEquipment] = {}
        # Line code cache (merged from component factory)
        self._line_codes: Dict[str, Any] = {}
        # Component tracking
        self._components_created: Dict[str, list] = {
            "buses": [],
            "transformers": [],
            "lines": [],
            "loads": [],
            "sources": [],
            "linecodes": [],
        }

        if altdss is None:
            raise OpenDSSBackendError(
                "OpenDSS not available. Install altdss: pip install altdss"
            )

    def initialize_circuit(
        self, name: str, source_bus: str, primary_kv: float
    ) -> None:
        """Initialize OpenDSS circuit with German distribution standards."""
        try:
            altdss.altdss("Clear")
            altdss.altdss(
                f"New Circuit.{name} basekv={primary_kv} pu=1.0 phases=3 bus1={source_bus}"
            )

            # Set German distribution voltage bases
            voltage_bases = [primary_kv, 20.0, 0.4]
            bases_str = ",".join(str(v) for v in voltage_bases)
            altdss.altdss(f"Set VoltageBases=[{bases_str}]")
            altdss.altdss("CalcVoltageBases")
            altdss.altdss("Set DefaultBaseFrequency=50")

            self.dss = altdss.altdss
            self._circuit_name = name

            # Reset component tracking
            self._line_codes = {}
            self._components_created = {
                "buses": [],
                "transformers": [],
                "lines": [],
                "loads": [],
                "sources": [],
                "linecodes": [],
            }

            # Configure voltage source
            self.dss(
                f"Edit Vsource.source basekv={primary_kv} pu=1.0 phases=3 "
                f"bus1={source_bus} MVASC3=1000 MVASC1=900"
            )
            self.logger.info(f"Initialized OpenDSS circuit: {name}")

        except Exception as e:
            self.logger.error(f"Failed to initialize OpenDSS circuit: {e}")
            raise OpenDSSBackendError(f"OpenDSS initialization failed: {e}") from e

    def create_component(self, spec: ComponentSpec) -> Any:
        """Create OpenDSS component from specification."""
        if self.dss is None:
            raise OpenDSSBackendError(
                "Backend not initialized. Call initialize_circuit() first."
            )

        try:
            if isinstance(spec, TransformerSpec):
                return self._create_transformer(spec)
            elif isinstance(spec, LineSpec):
                return self._create_line(spec)
            elif isinstance(spec, LoadSpec):
                return self._create_load(spec)
            elif isinstance(spec, BusSpec):
                # OpenDSS creates buses implicitly
                return spec.name
            elif isinstance(spec, ExtGridSpec):
                # External grid is created in initialize_circuit
                return "source"
            else:
                raise OpenDSSBackendError(
                    f"Unknown component spec type: {type(spec).__name__}"
                )
        except Exception as e:
            self.logger.error(f"Failed to create component {spec.name}: {e}")
            raise OpenDSSBackendError(f"Component creation failed: {e}") from e

    # =========================================================================
    # Private Component Creation Methods (merged from OpenDSSComponentFactory)
    # =========================================================================

    def _create_line_code(self, cable: CableEquipment) -> Any:
        """Create OpenDSS line code from cable equipment (with caching)."""
        code_name = f"LC_{cable.name}"

        if code_name in self._line_codes:
            return self._line_codes[code_name]

        line_code = self.dss.LineCode.new(
            code_name,
            NPhases=cable.n_phases,
            R1=cable.r_ohm_per_km,
            X1=cable.x_ohm_per_km,
            R0=cable.r_ohm_per_km * 3,  # Zero sequence approximation
            X0=cable.x_ohm_per_km * 3,
            C1=0.0,
            C0=0.0,
            Units="km",
            NormAmps=cable.max_i_a,
            EmergAmps=cable.max_i_a * 1.25,
        )

        self._line_codes[code_name] = line_code
        self._components_created["linecodes"].append(line_code)
        self.logger.debug(f"Created line code: {code_name}")
        return line_code

    def _create_transformer(self, spec: TransformerSpec) -> Any:
        """Create transformer from specification."""
        equipment = TransformerEquipment(
            name=spec.name,
            s_max_kva=spec.kva,
            primary_voltage_kv=20.0,  # German MV
            secondary_voltage_kv=0.4,  # German LV
        )

        xhl = equipment.reactance_pu * 100 if equipment.reactance_pu else 7.0

        transformer = self.dss.Transformer.new(
            spec.name,
            Phases=equipment.n_phases,
            Windings=2,
            Buses=[spec.bus1, spec.bus2],
            Conns=["delta", "wye"],
            kVs=[equipment.primary_voltage_kv, equipment.secondary_voltage_kv],
            kVAs=[equipment.s_max_kva, equipment.s_max_kva],
            pctRs=[0.5, 0.5],
            XHL=xhl,
        )

        self._components_created["transformers"].append(transformer)
        self.logger.debug(f"Created transformer: {spec.name} (kva={spec.kva})")
        return transformer

    def _create_line(self, spec: LineSpec) -> Any:
        """Create line/cable from specification."""
        cable_equipment = self.cable_registry.get(spec.cable_name)
        if not cable_equipment:
            cable_equipment = self._find_fallback_cable(spec.cable_name)
            if cable_equipment:
                self.logger.warning(
                    f"Cable '{spec.cable_name}' not found, using fallback "
                    f"'{cable_equipment.name}'"
                )
            else:
                available = list(self.cable_registry.keys())[:10]
                raise OpenDSSBackendError(
                    f"Cable type '{spec.cable_name}' not registered. "
                    f"Available: {available}... Call register_cable_types first."
                )

        # Ensure line code exists
        line_code = self._create_line_code(cable_equipment)

        # Enforce minimum length
        length_km = max(spec.length_km, 0.001)

        line = self.dss.Line.new(
            spec.name,
            Bus1=spec.bus1,
            Bus2=spec.bus2,
            LineCode=line_code,
            Length=length_km,
            Units="km",
            Phases=cable_equipment.n_phases,
        )

        self._components_created["lines"].append(line)
        self.logger.debug(
            f"Created line: {spec.name} (length={length_km:.3f}km, "
            f"type={cable_equipment.name})"
        )
        return line

    def _create_load(self, spec: LoadSpec) -> Any:
        """Create load from specification."""
        load = self.dss.Load.new(
            spec.name,
            Bus1=spec.bus,
            Phases=3,
            kV=0.4,  # German LV
            kW=spec.kw,
            kvar=spec.kvar,
            Conn="wye",
            Model=1,
        )

        self._components_created["loads"].append(load)
        self.logger.debug(f"Created load: {spec.name} (kw={spec.kw:.1f})")
        return load

    def _find_fallback_cable(self, cable_name: str) -> CableEquipment | None:
        """Find a fallback cable when requested cable is not available."""
        cable_type = None
        for prefix in ["NAYY", "NYY"]:
            if prefix in cable_name.upper():
                cable_type = prefix
                break

        if not cable_type:
            return None

        same_type_cables = [
            (name, cable)
            for name, cable in self.cable_registry.items()
            if cable_type in name.upper() and "kV" not in name
        ]

        if not same_type_cables:
            return None

        same_type_cables.sort(key=lambda x: x[1].max_i_a, reverse=True)
        return same_type_cables[0][1]

    def _get_bus_index(self, bus_name: str) -> str:
        """Return bus identifier (OpenDSS uses string names, not indices)."""
        return bus_name

    # =========================================================================
    # Cable Registration
    # =========================================================================

    def register_cable_types(self, cables: list) -> None:
        """Register cable types from database tuples."""
        for cable_tuple in cables:
            name, r_ohm, x_ohm, max_i, _cost_eur = cable_tuple
            cable_obj = CableEquipment(
                name=name,
                r_ohm_per_km=r_ohm,
                x_ohm_per_km=x_ohm,
                max_i_ka=max_i,
            )

            canonical = normalize_cable_name(name)
            self.cable_registry[canonical] = cable_obj
            if name != canonical:
                self.cable_registry[name] = cable_obj

            # Create line code immediately if circuit is initialized
            if self.dss is not None:
                self._create_line_code(cable_obj)

        self.logger.info(f"Registered {len(cables)} cable types")

    # =========================================================================
    # Power Flow & Analysis
    # =========================================================================

    def solve_power_flow(self) -> bool:
        """Solve power flow and return convergence status."""
        if self.dss is None:
            raise OpenDSSBackendError("No OpenDSS instance available for analysis")

        try:
            self.logger.info("Calculating voltage bases...")
            self.dss("CalcVoltageBases")
            self.logger.debug("Solving power flow...")
            self.dss.Solution.Solve()

            converged = self.dss.Solution.Converged
            if converged:
                self.logger.info("Power flow converged")
            else:
                self.logger.error("Power flow did not converge")

            try:
                report_filename = (
                    f"{self._circuit_name}_electrical_statistics.txt"
                    if self._circuit_name
                    else "electrical_statistics.txt"
                )
                self._generate_statistics_report(report_filename)
            except Exception as diag_err:
                self.logger.warning(f"Statistics report failed: {diag_err}")

            return converged

        except Exception as e:
            self.logger.error(f"Power flow solution failed: {e}")
            return False

    def get_circuit_metrics(self) -> Dict[str, Any]:
        """Get key circuit metrics after solving."""
        if self.dss is None:
            return {}

        try:
            total_power = self.dss.TotalPower()
            total_losses = self.dss.Losses()

            metrics = {
                "converged": self.dss.Solution.Converged,
                "total_power_kw": total_power.real if total_power else 0,
                "total_losses_kw": total_losses.real / 1000 if total_losses else 0,
                "num_buses": self.dss.NumBuses,
                "num_elements": self.dss.NumCircuitElements,
            }

            bus_voltages = self.dss.BusVMagPU()
            if bus_voltages is not None and len(bus_voltages) > 0:
                metrics["min_voltage_pu"] = min(bus_voltages)
                metrics["max_voltage_pu"] = max(bus_voltages)
                metrics["avg_voltage_pu"] = sum(bus_voltages) / len(bus_voltages)

            return metrics

        except Exception as e:
            self.logger.warning(f"Error getting circuit metrics: {e}")
            return {}

    # =========================================================================
    # Export & Cleanup
    # =========================================================================

    def export_to_format(self, filename: Optional[str] = None) -> str:
        """Export circuit to JSON format."""
        if self.dss is None:
            raise OpenDSSBackendError("No OpenDSS instance available for export")

        try:
            json_str = self.dss.to_json()
            if filename:
                with open(filename, "w") as f:
                    f.write(json_str)
                self.logger.info(f"Exported circuit to JSON file: {filename}")
            else:
                self.logger.info("Exported circuit to JSON format")
            return json_str

        except Exception as e:
            self.logger.error(f"JSON export failed: {e}")
            raise OpenDSSBackendError(f"JSON export failed: {e}") from e

    def cleanup(self) -> None:
        """Clean up OpenDSS resources and reset state."""
        if self.dss:
            try:
                self.dss("Clear")
                self.logger.debug("Cleared OpenDSS circuit")
            except Exception as e:
                self.logger.warning(f"Error clearing OpenDSS circuit: {e}")
            finally:
                self.dss = None

        self._line_codes = {}
        self._components_created = {
            "buses": [],
            "transformers": [],
            "lines": [],
            "loads": [],
            "sources": [],
            "linecodes": [],
        }
        self._circuit_name = None
        self.logger.debug("OpenDSS cleanup completed")

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_cable_types(self) -> list[str]:
        """Get all registered cable type names."""
        return list(self.cable_registry.keys())

    def get_component_count(self, component_type: str) -> int:
        """Get component count by type."""
        return len(self._components_created.get(component_type, []))

    def get_bus_coordinates(self, bus_name: str) -> tuple[float, float] | None:
        """Get bus coordinates (OpenDSS doesn't store geodata)."""
        return None

    # =========================================================================
    # Update Methods (no-ops for OpenDSS)
    # =========================================================================

    def set_bus_coordinates(self, bus_name: str, x: float, y: float) -> None:
        """Set bus coordinates (no-op - OpenDSS doesn't store geodata)."""
        pass

    def set_bus_zone(self, bus_name: str, zone: str) -> None:
        """Set bus zone (no-op - zone concept not used in OpenDSS)."""
        pass

    def set_transformer_rating(self, trafo_name: str, rating_mva: float) -> None:
        """Set transformer rating (no-op - sized at creation)."""
        pass

    # =========================================================================
    # Statistics Report (internal)
    # =========================================================================

    def _generate_statistics_report(
        self, output_filename: str = "electrical_statistics.txt"
    ) -> None:
        """Generate electrical statistics report after power flow solve."""
        if self.dss is None:
            return

        circuit_name = self._circuit_name or "unknown"
        m = re.search(r"K\d+_S\d+", circuit_name or "")
        subfolder = m.group(0) if m else circuit_name
        stats_dir = os.path.abspath(os.path.join(os.getcwd(), "statistics", subfolder))
        os.makedirs(stats_dir, exist_ok=True)
        out_path = os.path.join(stats_dir, output_filename)

        converged = bool(self.dss.Solution.Converged)
        total_power = self.dss.TotalPower()
        total_losses = self.dss.Losses()

        try:
            raw_bus_names = self.dss.BusNames()
            bus_names = list(raw_bus_names) if raw_bus_names else []
        except Exception:
            bus_names = []

        try:
            raw_bus_vmags = self.dss.BusVMagPU()
            bus_vmags = list(raw_bus_vmags) if raw_bus_vmags else []
        except Exception:
            bus_vmags = []

        min_v = min(bus_vmags) if bus_vmags else None
        max_v = max(bus_vmags) if bus_vmags else None
        avg_v = sum(bus_vmags) / len(bus_vmags) if bus_vmags else None

        # Export CSVs
        export_types = ["Voltages", "Currents", "Powers", "Losses", "Loads"]
        exported_files = []

        for export_type in export_types:
            try:
                self.dss(f"Export {export_type}")
                base_name = f"{export_type}.csv"
                root_base = os.path.join(os.getcwd(), base_name)
                tagged_name = f"{circuit_name}_EXP_{export_type.upper()}.csv"
                stats_tagged = os.path.join(stats_dir, tagged_name)

                if os.path.exists(root_base):
                    shutil.move(root_base, stats_tagged)
                    exported_files.append(stats_tagged)
            except Exception:
                continue

        # Write report
        def _to_kw(value: Any) -> float | None:
            try:
                if hasattr(value, "real"):
                    return float(value.real)
                if isinstance(value, (list, tuple)) and len(value) > 0:
                    return float(value[0])
                return float(value)
            except Exception:
                return None

        with open(out_path, "w") as f:
            f.write("=" * 80 + "\n")
            f.write("Electrical Statistics Report\n")
            f.write("=" * 80 + "\n\n")
            f.write(f"Timestamp: {datetime.utcnow().isoformat()}Z\n")
            f.write(f"Circuit: {self._circuit_name or 'unknown'}\n")
            f.write(f"Converged: {converged}\n")

            tp_kw = _to_kw(total_power)
            tl_w = _to_kw(total_losses)
            tl_kw = (tl_w / 1000.0) if tl_w is not None else None

            if tp_kw is not None:
                f.write(f"Total Power kW: {tp_kw:.3f}\n")
            if tl_kw is not None:
                f.write(f"Total Losses kW: {tl_kw:.3f}\n")

            f.write("\nVoltage stats (pu):\n")
            f.write(f"  min: {min_v if min_v is not None else 'n/a'}\n")
            f.write(f"  avg: {avg_v if avg_v is not None else 'n/a'}\n")
            f.write(f"  max: {max_v if max_v is not None else 'n/a'}\n")

            f.write("\nExported files:\n")
            for p in exported_files:
                f.write(f"  - {p}\n")
