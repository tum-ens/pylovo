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

Optional dependency:
    The engine bindings come from the ``altdss`` package of the ``opendss`` extra
    (``uv sync --extra opendss``). This module always imports; without ``altdss`` the
    name ``altdss`` is ``None`` and constructing :class:`OpenDSSBackend`
    (``ELECTRICAL_BACKEND: opendss``) raises :class:`OpenDSSBackendError`.
    The backend is not covered by the regression runs; parts of ``GridGenerator``
    (voltage-drop diagnostics, SQL network tables) only work with pandapower.
"""

import logging
import re
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

try:
    import altdss
except ImportError:  # optional dependency, see the module docstring
    altdss = None


def _empty_component_registry() -> Dict[str, list]:
    """Return the per-type lists that track the components of one circuit."""
    return {
        "buses": [],
        "transformers": [],
        "lines": [],
        "loads": [],
        "sources": [],
        "linecodes": [],
    }


def _dss_name(name: str) -> str:
    """Name of a pylovo bus or element in OpenDSS.

    The DSS parser splits names at blanks and reads ``.`` as a node separator, and OpenDSS
    lowercases every name: ``"Consumer Nodebus 21"`` becomes ``"consumer_nodebus_21"``.
    """
    return re.sub(r"[\s.]", "_", str(name)).lower()


#: Voltage range (p.u.) in which a load keeps constant power. OpenDSS switches a load to constant
#: impedance outside it (default 0.95 to 1.05, inside the check band), pandapower never does.
LOAD_CONSTANT_POWER_RANGE_PU = (0.7, 1.3)

#: Known defects of this backend; see the "OpenDSS (experimental)" section of the electrical backend docs.
EXPERIMENTAL_WARNING = (
    "The OpenDSS backend is experimental: bus names with blanks give a wrong power flow, parallel cables are "
    "ignored and its results cannot be stored for the analysis, the API, the MV stage or the viewer. "
    "Use ELECTRICAL_BACKEND: pandapower for results."
)


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
        # Line codes by name, so each cable type is defined only once per circuit
        self._line_codes: Dict[str, Any] = {}
        self._components_created: Dict[str, list] = _empty_component_registry()
        # pylovo bus name by OpenDSS bus name, and the OpenDSS names in use per element class
        self._bus_names: Dict[str, str] = {}
        self._element_names: Dict[str, set] = {}

        if altdss is None:
            raise OpenDSSBackendError(
                "OpenDSS not available. Install the opendss extra: uv sync --extra opendss"
            )
        self.logger.warning(EXPERIMENTAL_WARNING)

    def initialize_circuit(
        self, name: str, source_bus: str, primary_kv: float
    ) -> None:
        """Initialize OpenDSS circuit with German distribution standards."""
        try:
            self._bus_names = {}
            self._element_names = {}
            source_bus = self._bus(source_bus)
            altdss.altdss("Clear")
            # before the circuit: its source takes the default base frequency (60 Hz in a new process)
            altdss.altdss("Set DefaultBaseFrequency=50")
            altdss.altdss(
                f"New Circuit.{_dss_name(name)} basekv={primary_kv} pu=1.0 phases=3 bus1={source_bus}"
            )

            # Set German distribution voltage bases
            voltage_bases = [primary_kv, 20.0, 0.4]
            bases_str = ",".join(str(v) for v in voltage_bases)
            altdss.altdss(f"Set VoltageBases=[{bases_str}]")
            altdss.altdss("CalcVoltageBases")

            self.dss = altdss.altdss
            self._circuit_name = name

            # Reset component tracking
            self._line_codes = {}
            self._components_created = _empty_component_registry()

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
                # OpenDSS creates buses implicitly; only track them for get_component_count()
                self._bus(spec.name)
                self._components_created["buses"].append(spec.name)
                return spec.name
            elif isinstance(spec, ExtGridSpec):
                # The circuit's source (created in initialize_circuit) is the external grid
                self.set_source_voltage(spec.vm_pu)
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

    def _bus(self, name: str) -> str:
        """OpenDSS name of a pylovo bus; two pylovo buses must not get the same one."""
        dss_name = _dss_name(name)
        known = self._bus_names.setdefault(dss_name, name)
        if known != name:
            raise OpenDSSBackendError(
                f"Buses {known!r} and {name!r} have the same OpenDSS name {dss_name!r}"
            )
        return dss_name

    def _element_name(self, element_class: str, name: str) -> str:
        """Unique OpenDSS name of a new element (OpenDSS refuses duplicates, pandapower allows them)."""
        used = self._element_names.setdefault(element_class, set())
        base = dss_name = _dss_name(name)
        count = 1
        while dss_name in used:
            count += 1
            dss_name = f"{base}_{count}"
        used.add(dss_name)
        return dss_name

    def _create_line_code(self, cable: CableEquipment, parallel: int = 1) -> Any:
        """Create the OpenDSS line code of ``parallel`` cables of one type (with caching).

        Parallel cables are one line code with the impedances divided and the ampacity
        multiplied by ``parallel``.
        """
        code_name = _dss_name(f"LC_{cable.name}" + (f"_x{parallel}" if parallel > 1 else ""))

        if code_name in self._line_codes:
            return self._line_codes[code_name]

        line_code = self.dss.LineCode.new(
            code_name,
            NPhases=cable.n_phases,
            R1=cable.r_ohm_per_km / parallel,
            X1=cable.x_ohm_per_km / parallel,
            R0=cable.r_ohm_per_km * 3 / parallel,  # Zero sequence approximation
            X0=cable.x_ohm_per_km * 3 / parallel,
            C1=0.0,
            C0=0.0,
            Units="km",
            NormAmps=cable.max_i_a * parallel,
            EmergAmps=cable.max_i_a * 1.25 * parallel,
        )

        self._line_codes[code_name] = line_code
        self._components_created["linecodes"].append(line_code)
        self.logger.debug(f"Created line code: {code_name}")
        return line_code

    def _create_transformer(self, spec: TransformerSpec) -> Any:
        """Create the station transformer from its specification.

        ``spec.kva`` is the rating of one unit. A station of ``spec.parallel``
        identical units is modelled as one equivalent transformer with the summed
        rating (same per-unit impedance), because ``set_transformer_rating`` is a
        no-op for this backend.
        """
        equipment = TransformerEquipment(
            name=spec.name,
            s_max_kva=spec.kva * spec.parallel,
            primary_voltage_kv=20.0,  # German MV
            secondary_voltage_kv=0.4,  # German LV
        )

        xhl = equipment.reactance_pu * 100 if equipment.reactance_pu else 7.0

        transformer = self.dss.Transformer.new(
            self._element_name("transformer", spec.name),
            Phases=equipment.n_phases,
            Windings=2,
            Buses=[self._bus(spec.bus1), self._bus(spec.bus2)],
            Conns=["delta", "wye"],
            kVs=[equipment.primary_voltage_kv, equipment.secondary_voltage_kv],
            kVAs=[equipment.s_max_kva, equipment.s_max_kva],
            pctRs=[0.5, 0.5],
            XHL=xhl,
        )

        self._components_created["transformers"].append(transformer)
        self.logger.debug(
            f"Created transformer: {spec.name} (kva={spec.kva}, parallel={spec.parallel})"
        )
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

        parallel = int(spec.parallel)
        if parallel < 1:
            raise OpenDSSBackendError(f"Line {spec.name!r} has {spec.parallel} parallel cables")
        line_code = self._create_line_code(cable_equipment, parallel)

        # Enforce minimum length
        length_km = max(spec.length_km, 0.001)

        line = self.dss.Line.new(
            self._element_name("line", spec.name),
            Bus1=self._bus(spec.bus1),
            Bus2=self._bus(spec.bus2),
            LineCode=line_code,
            Length=length_km,
            Units="km",
            Phases=cable_equipment.n_phases,
        )

        self._components_created["lines"].append(line)
        self.logger.debug(
            f"Created line: {spec.name} (length={length_km:.3f}km, "
            f"type={cable_equipment.name}, parallel={parallel})"
        )
        return line

    def _create_load(self, spec: LoadSpec) -> Any:
        """Create load from specification."""
        load = self.dss.Load.new(
            self._element_name("load", spec.name),
            Bus1=self._bus(spec.bus),
            Phases=3,
            kV=0.4,  # German LV
            kW=spec.kw,
            kvar=spec.kvar,
            Conn="wye",
            Model=1,
            VMinpu=LOAD_CONSTANT_POWER_RANGE_PU[0],
            VMaxpu=LOAD_CONSTANT_POWER_RANGE_PU[1],
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
            self.dss("CalcVoltageBases")
            self.logger.debug("Solving power flow...")
            self.dss.Solution.Solve()

            converged = self.dss.Solution.Converged
            if converged:
                self.logger.debug("Power flow converged")
            else:
                self.logger.warning("Power flow did not converge")
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
        self._components_created = _empty_component_registry()
        self._bus_names = {}
        self._element_names = {}
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
    # Station voltage (see pylovo.station_voltage)
    # =========================================================================

    def get_source_voltage(self) -> float:
        """Voltage of the external grid (MV side of the station) in p.u."""
        return float(self.dss.Vsource["source"].pu)

    def set_source_voltage(self, vm_pu: float) -> None:
        """Set the voltage of the external grid (MV side of the station) in p.u."""
        self.dss(f"Edit Vsource.source pu={float(vm_pu)!r}")

    def get_bus_voltage_pu(self, bus_name: str) -> float:
        """Solved voltage magnitude of a bus in p.u. (mean of its three phases)."""
        magnitudes = self.dss.Bus[_dss_name(bus_name)].puVMagAngle[0::2]
        return float(sum(magnitudes) / len(magnitudes))

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
