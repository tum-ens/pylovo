"""
Pandapower backend implementation for pylovo.

This module implements IElectricalBackend using pandapower as the simulation engine.
Pandapower uses pandas DataFrames to represent network components.

Key features:
    - DataFrame-based network representation
    - Buses, loads and lines created in batches (pp.create_buses/loads/lines), see
      PandapowerBackend.batch
    - Built-in power flow solvers (Newton-Raphson, etc.)
"""

import copy
import functools
import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, Optional

import numpy as np
import pandapower as pp
import pandas as pd

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
from pylovo.config_loader import POWER_FLOW_MAX_VM_PU, POWER_FLOW_MIN_VM_PU


class PandapowerBackendError(Exception):
    """Exception raised by Pandapower backend operations."""


# pylovo's own columns of net.line and net.load, in table order, with their dtype in the stored net:
# numbers float64 (NaN where not applicable), text and flags objects (None where not applicable).
# pandapower would infer the dtype of each batch from its values instead.
LINE_ATTRIBUTE_DTYPES = {
    "feeder_section_id": "float64",
    "feeder_sizing_basis": "object",
    "ampacity_std_type": "object",
    "ampacity_parallel": "float64",
    "service_sizing_basis": "object",
    "service_ampacity_voltage_drop_percent": "object",
    "service_selected_voltage_drop_percent": "object",
    "service_voltage_drop_limit_met": "object",
    "service_length_review": "object",
    "total_design_voltage_drop_percent": "object",
}
LOAD_ATTRIBUTE_DTYPES = {
    "service_design_p_mw": "float64",
    "operating_point_basis": "object",
    "category": "object",
    "load_units": "float64",
    "consumer_vertex": "float64",
    "max_p_mw": "float64",
}


@functools.cache
def _empty_network_template() -> pp.pandapowerNet:
    return pp.create_empty_network()


def empty_network(name: str = "") -> pp.pandapowerNet:
    """Return the content of ``pp.create_empty_network(name=name)``, copied from a cached template.

    ``create_empty_network`` takes about 50 ms, a copy about 4 ms; generation builds and the
    analysis reads one network per grid.
    """
    net = copy.deepcopy(_empty_network_template())
    net["name"] = name
    return net


class PandapowerBackend(IElectricalBackend):
    """Pandapower implementation of :class:`IElectricalBackend`.

    Holds one pandapower network (``self.net``) per circuit. Besides the standard
    pandapower columns, lines and loads get pylovo's planning attributes from the
    specs (for example ``feeder_section_id``, ``feeder_sizing_basis``,
    ``service_design_p_mw``) as extra table columns, so they are exported with the
    network JSON and the SQL network tables.

    Bus limits ``min_vm_pu``/``max_vm_pu`` are set from ``POWER_FLOW_VOLTAGE_LIMITS``
    in ``config_analysis.yaml``.

    Buses, loads and lines are created with pandapower's batch functions, one call per table
    (a single ``pp.create_line`` costs about 1 ms, mostly per-column bookkeeping). Inside
    :meth:`batch` they are queued until the block ends or ``net`` is read; otherwise every
    element is created at once. Either way their indices follow the creation order.
    """

    def __init__(self, logger: Optional[logging.Logger] = None):
        """Initialize pandapower backend."""
        self.logger = logger or logging.getLogger(__name__)
        self.net = None
        self._circuit_name = None
        self._batch_depth = 0

    @property
    def net(self):
        """The pandapower network, with every queued bus, load and line created."""
        self.flush()
        return self._net

    @net.setter
    def net(self, net) -> None:
        self._net = net
        self._bus_cache: Dict[str, int] = {}
        self._bus_coordinates: Dict[str, tuple[float, float] | None] = {}
        self._queue: Dict[str, list] = {"bus": [], "load": [], "line": []}

    @contextmanager
    def batch(self):
        """Queue the buses, loads and lines created in the block and create them when it ends."""
        self._batch_depth += 1
        try:
            yield self
        finally:
            self._batch_depth -= 1
        if self._batch_depth == 0:
            self.flush()

    def initialize_circuit(
        self, name: str, source_bus: str, primary_kv: float,
    ) -> None:
        """Initialize pandapower network."""
        try:
            self.net = empty_network(name=name)
            self._circuit_name = name
        except Exception as e:
            self.logger.error(f"Failed to initialize circuit: {e}")
            raise PandapowerBackendError(f"Circuit initialization failed: {e}") from e

    def create_component(self, spec: ComponentSpec) -> Any:
        """Create pandapower component from specification."""
        if self._net is None:
            raise PandapowerBackendError(
                "Backend not initialized. Call initialize_circuit() first."
            )

        try:
            if isinstance(spec, BusSpec):
                index = self._queue_bus(spec)
            elif isinstance(spec, TransformerSpec):
                return self._create_transformer(spec)
            elif isinstance(spec, LineSpec):
                index = self._queue_line(spec)
            elif isinstance(spec, LoadSpec):
                index = self._queue_load(spec)
            elif isinstance(spec, ExtGridSpec):
                return self._create_ext_grid(spec)
            else:
                raise PandapowerBackendError(
                    f"Unknown component spec type: {type(spec).__name__}"
                )
            if self._batch_depth == 0:
                self.flush()
            return index
        except Exception as e:
            self.logger.error(f"Failed to create component {spec.name}: {e}")
            raise PandapowerBackendError(f"Component creation failed: {e}") from e

    def flush(self) -> None:
        """Create the queued buses, loads and lines, one pandapower call per table."""
        if self._net is None or not any(self._queue.values()):
            return
        queue, self._queue = self._queue, {"bus": [], "load": [], "line": []}
        try:
            if queue["bus"]:
                self._create_buses(queue["bus"])
            if queue["load"]:
                self._create_loads(queue["load"])
            if queue["line"]:
                self._create_lines(queue["line"])
        except Exception as e:
            self.logger.error(f"Failed to create queued components: {e}")
            raise PandapowerBackendError(f"Component creation failed: {e}") from e

    # =========================================================================
    # Private Component Creation Methods
    # =========================================================================

    def _next_index(self, table: str) -> int:
        """Index the next element of ``table`` gets (pandapower's free id, after the queued ones)."""
        existing = self._net[table]
        free_id = int(existing.index.max()) + 1 if len(existing) else 0
        return free_id + len(self._queue[table])

    def _queue_bus(self, spec: BusSpec) -> int:
        """Queue a bus; its index and coordinates are known at once."""
        index = self._next_index("bus")
        coordinates = None if spec.coordinates is None else (float(spec.coordinates[0]), float(spec.coordinates[1]))
        self._queue["bus"].append((index, spec, coordinates))
        self._bus_cache[spec.name] = index
        self._bus_coordinates[spec.name] = coordinates
        return index

    def _create_buses(self, queued: list) -> None:
        index = [item[0] for item in queued]
        specs = [item[1] for item in queued]
        # the GeoJSON text of pp.create_bus, so stored grids keep one format
        geo = [None if xy is None else f'{{"coordinates":[{xy[0]},{xy[1]}], "type":"Point"}}' for *_, xy in queued]
        pp.create_buses(
            self._net,
            len(specs),
            vn_kv=[spec.voltage_kv for spec in specs],
            index=index,
            name=[spec.name for spec in specs],
            type="n",
            zone=[spec.zone if spec.zone is not None else "n" for spec in specs],
            geo=geo,
        )
        # the voltage limits in pp.create_bus's column order (create_buses sorts new columns)
        df = self._net.bus
        for column, value in (("min_vm_pu", POWER_FLOW_MIN_VM_PU), ("max_vm_pu", POWER_FLOW_MAX_VM_PU)):
            if column not in df.columns:
                df[column] = np.nan
            df.loc[index, column] = float(value)
        self.logger.debug(f"Created {len(specs)} buses")

    def _create_transformer(self, spec: TransformerSpec) -> int:
        """Create transformer from specification (queued components are created first)."""
        self.flush()
        mv_bus = self._get_bus_index(spec.bus1)
        lv_bus = self._get_bus_index(spec.bus2)

        sn_mva = spec.kva / 1000.0
        std_type = f"{sn_mva} MVA 20/0.4 kV"
        if not pp.std_type_exists(self.net, std_type, element="trafo"):
            self._register_small_transformer(sn_mva, std_type)

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

    def _queue_line(self, spec: LineSpec) -> int:
        """Queue a line; unknown buses and cable types raise at once."""
        from_bus = self._get_bus_index(spec.bus1)
        to_bus = self._get_bus_index(spec.bus2)
        std_type = spec.cable_name if spec.cable_name else "NAYY_4_150"
        if std_type not in self._net.std_types["line"]:
            raise UserWarning(f"Unknown standard line type {std_type}")
        index = self._next_index("line")
        self._queue["line"].append((index, spec, from_bus, to_bus, std_type))
        return index

    def _create_lines(self, queued: list) -> None:
        # pandapower takes the geometry of all lines of a call or of none
        with_geometry = [item for item in queued if item[1].coordinates]
        without_geometry = [item for item in queued if not item[1].coordinates]
        for group, geometry in ((with_geometry, True), (without_geometry, False)):
            if not group:
                continue
            specs = [item[1] for item in group]
            pp.create_lines(
                self._net,
                from_buses=[item[2] for item in group],
                to_buses=[item[3] for item in group],
                length_km=[spec.length_km for spec in specs],
                std_type=[item[4] for item in group],
                name=[spec.name for spec in specs],
                index=[item[0] for item in group],
                geodata=[list(spec.coordinates) for spec in specs] if geometry else None,
                parallel=[spec.parallel for spec in specs],
            )
        if with_geometry and without_geometry:
            self._net["line"] = self._net.line.sort_index()
        index = [item[0] for item in queued]
        # create_line takes "type" from the standard type if it has one; create_lines writes ""
        # for a whole batch without types
        line_types = self._net.std_types["line"]
        self._net.line.loc[index, "type"] = pd.Series(
            [line_types[item[4]].get("type") for item in queued], index=index, dtype=object
        )
        self._set_attributes("line", index, [item[1] for item in queued], LINE_ATTRIBUTE_DTYPES)
        self.logger.debug(f"Created {len(queued)} lines")

    def _queue_load(self, spec: LoadSpec) -> int:
        """Queue a load; an unknown bus raises at once."""
        bus = self._get_bus_index(spec.bus)
        index = self._next_index("load")
        self._queue["load"].append((index, spec, bus))
        return index

    def _create_loads(self, queued: list) -> None:
        index = [item[0] for item in queued]
        specs = [item[1] for item in queued]
        pp.create_loads(
            self._net,
            buses=[item[2] for item in queued],
            p_mw=[spec.kw / 1000.0 for spec in specs],
            q_mvar=[spec.kvar / 1000.0 for spec in specs],
            name=[spec.name for spec in specs],
            index=index,
        )
        self._set_attributes("load", index, specs, LOAD_ATTRIBUTE_DTYPES)
        self.logger.debug(f"Created {len(specs)} loads")

    def _set_attributes(self, table: str, index: list, specs: list, dtypes: dict[str, str]) -> None:
        """Write pylovo's attribute columns of new ``table`` rows with their fixed dtype."""
        df = self._net[table]
        for column, dtype in dtypes.items():
            values = pd.Series([getattr(spec, column) for spec in specs], index=index, dtype=dtype)
            if column not in df.columns:
                df[column] = pd.Series(np.nan if dtype == "float64" else None, index=df.index, dtype=dtype)
            elif df[column].dtype != dtype:
                df[column] = df[column].astype(dtype)
            df.loc[index, column] = values

    def _create_ext_grid(self, spec: ExtGridSpec) -> int:
        """Create external grid from specification (queued components are created first)."""
        self.flush()
        bus = self._get_bus_index(spec.bus)
        ext_grid_idx = pp.create_ext_grid(
            self.net, bus=bus, vm_pu=spec.vm_pu, name=spec.name
        )
        return ext_grid_idx

    def _get_bus_index(self, bus_name: str) -> int:
        """Return the pandapower index of the bus called ``bus_name``.

        Raises:
            ValueError: If no bus of that name exists.
        """
        if bus_name in self._bus_cache:
            return self._bus_cache[bus_name]
        if len(self._bus_cache) == len(self._net.bus) + len(self._queue["bus"]):
            # Every bus of the net is cached, so none has this name; skip the table scan.
            raise ValueError(f"Bus not found: {bus_name}")

        buses = self._net.bus[self._net.bus.name == bus_name]
        if buses.empty:
            raise ValueError(f"Bus not found: {bus_name}")

        bus_idx = buses.index[0]
        self._bus_cache[bus_name] = bus_idx
        return bus_idx

    #: pandapower ships 20/0.4 kV types from 0.25 MVA up only.  The smaller
    #: catalogue sizes (100 and 160 kVA) take its 0.25 MVA type with the rating
    #: changed: same short-circuit voltage, winding losses, no-load current and
    #: vector group, no-load losses scaled with the rating.  An approximation
    #: of a small distribution transformer, not a manufacturer's data sheet.
    SMALL_TRANSFORMER_BASE = "0.25 MVA 20/0.4 kV"

    def _register_small_transformer(self, sn_mva: float, std_type: str) -> None:
        base = pp.load_std_type(self.net, self.SMALL_TRANSFORMER_BASE, element="trafo")
        if sn_mva >= base["sn_mva"]:
            raise PandapowerBackendError(f"Unknown standard trafo type {std_type}")
        data = dict(base, sn_mva=sn_mva, pfe_kw=base["pfe_kw"] * sn_mva / base["sn_mva"])
        pp.create_std_type(self.net, data, name=std_type, element="trafo")
        self.logger.debug(f"Registered transformer type {std_type} from {self.SMALL_TRANSFORMER_BASE}")

    # =========================================================================
    # Cable Registration
    # =========================================================================

    def register_cable_types(self, cables: list) -> None:
        """Register cable standard types from equipment data.

        Args:
            cables: Tuples ``(name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur)``
                as returned by ``DatabaseClient.fetch_cables``. The cross-section
                ``q_mm2`` is parsed from the last ``_``-separated part of the name
                (for example ``NAYY_4_150``).
        """
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
        if self._net:
            self.net = None
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
        if bus_name in self._bus_coordinates:  # buses created by this backend, queued or not
            return self._bus_coordinates[bus_name]
        if self._net is None or (self._net.bus.empty and not self._queue["bus"]):
            return None
        try:
            bus_idx = self._get_bus_index(bus_name)
        except ValueError:
            return None
        # a bus the net had before (a queued bus has cached coordinates), so no flush is needed
        geo_str = self._net.bus.at[bus_idx, "geo"]
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
            self._bus_coordinates.pop(bus_name, None)
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

    def get_source_voltage(self) -> float:
        """Voltage of the (first) external grid in p.u."""
        return float(self.net.ext_grid.vm_pu.iloc[0])

    def set_source_voltage(self, vm_pu: float) -> None:
        """Set the voltage of every external grid (pylovo nets have one, on the MV side)."""
        self.net.ext_grid.loc[:, "vm_pu"] = float(vm_pu)

    def get_bus_voltage_pu(self, bus_name: str) -> float:
        """Solved voltage magnitude of the bus called ``bus_name``."""
        return float(self.net.res_bus.at[self._get_bus_index(bus_name), "vm_pu"])

    def set_transformer_tap_steps(self, steps: int) -> int:
        """Move the tap of the station transformer ``steps`` steps towards a higher LV voltage.

        The pandapower standard types have the tap on the HV side (lower position = fewer HV
        turns = higher LV voltage), ±2 steps of 2.5 %. The position is clipped to the type's
        range; returns the steps applied.
        """
        if self.net is None or self.net.trafo.empty:
            return 0
        idx = self.net.trafo.index[0]
        row = self.net.trafo.loc[idx]
        neutral = int(row.tap_neutral) if row.tap_neutral == row.tap_neutral else 0
        lo = int(row.tap_min) if row.tap_min == row.tap_min else neutral
        hi = int(row.tap_max) if row.tap_max == row.tap_max else neutral
        direction = 1 if str(row.tap_side).lower() == "lv" else -1
        position = min(hi, max(lo, neutral + direction * int(steps)))
        self.net.trafo.at[idx, "tap_pos"] = position
        return abs(position - neutral)

    def set_transformer_rating(self, trafo_name: str, rating_mva: float) -> None:
        """Set transformer rated power."""
        if self.net is None:
            return
        trafo_df = self.net.trafo[self.net.trafo["name"] == trafo_name]
        if not trafo_df.empty:
            trafo_idx = trafo_df.index[0]
            self.net.trafo.at[trafo_idx, "sn_mva"] = rating_mva
