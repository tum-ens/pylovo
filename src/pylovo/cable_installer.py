"""Cable installation module for electrical grid generation."""

import numpy as np
import pandas as pd

from pylovo.electrical_backend import IElectricalBackend, BusSpec, TransformerSpec, LineSpec, LoadSpec, ExtGridSpec
from pylovo.config_loader import VN, V_BAND_LOW, VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM, VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM, SMALL_LOAD_THRESHOLD_KW, DEFAULT_POWER_FACTOR
from pylovo.utils import oneSimultaneousLoad
from pylovo.electrical_backend import normalize_cable_name


class CableInstaller:
    """Handles cable installation for electrical grids using backend abstraction."""

    _NOMINAL_VOLTAGE_KV = VN * 1e-3
    _THREE_PHASE_FACTOR = np.sqrt(3)

    def __init__(self, backend: IElectricalBackend, dbc, logger, cables: list,
                 feeder_cables: pd.DataFrame, consumer_connection_cables: pd.DataFrame):
        """Initialize cable installer.

        Args:
            backend: Electrical backend instance (e.g., PandapowerBackend)
            dbc: Database client for accessing grid data
            logger: Logger instance
            cables: List of cable tuples from database (name, r_ohm_per_km, x_ohm_per_km, max_i_ka)
            feeder_cables: Configured feeder cable definitions
            consumer_connection_cables: Configured consumer connection cable definitions
        """
        self.backend = backend
        self.dbc = dbc
        self.logger = logger
        self._feeder_available_cables = self._extract_cable_names(feeder_cables)
        self._consumer_connection_cables = self._extract_cable_names(consumer_connection_cables)

        # Cache cable data from database as DataFrame (single source of truth)
        self._cable_df = self._build_cable_dataframe(cables)

    @staticmethod
    def _extract_cable_names(cable_df: pd.DataFrame) -> list[str]:
        """Extract normalized cable names from a config cable DataFrame."""
        if cable_df.empty or "name" not in cable_df.columns:
            return []
        return [normalize_cable_name(name) for name in cable_df["name"].tolist()]

    @staticmethod
    def _build_cable_dataframe(cables: list) -> pd.DataFrame:
        """Build cable DataFrame from database tuples.

        Normalizes cable names to underscore format (e.g., "NAYY_4_120")
        which is compatible with both pandapower and OpenDSS backends.

        Args:
            cables: List of tuples (name, r_ohm_per_km, x_ohm_per_km, max_i_ka)

        Returns:
            DataFrame indexed by normalized cable name with electrical properties
        """
        cable_data = {}
        for name, r_ohm, x_ohm, max_i_ka in cables:
            normalized_name = normalize_cable_name(name)

            try:
                q_mm2 = int(name.split("_")[-1])
            except (ValueError, IndexError):
                q_mm2 = 0

            cable_data[normalized_name] = {
                'r_ohm_per_km': float(r_ohm),
                'x_ohm_per_km': float(x_ohm),
                'max_i_ka': float(max_i_ka),
                'q_mm2': q_mm2
            }

        return pd.DataFrame.from_dict(cable_data, orient="index")

    def _get_bus_coordinates(self, bus_name: str, fallback: tuple[float, float] | None = None) -> tuple[float, float] | None:
        coords = self.backend.get_bus_coordinates(bus_name)
        if coords:
            return float(coords[0]), float(coords[1])
        if fallback is None:
            return None
        return float(fallback[0]), float(fallback[1])

    def _get_line_node_coordinates(self, node_id: int, bus_name: str | None = None) -> tuple[float, float]:
        fallback = self.dbc.get_node_geom(node_id)
        if bus_name is None:
            return float(fallback[0]), float(fallback[1])
        coords = self._get_bus_coordinates(bus_name, fallback=fallback)
        return float(coords[0]), float(coords[1])

    def _get_transformer_visual_coordinates(
        self,
        plz: int,
        kcid: int,
        bcid: int,
    ) -> tuple[float, float]:
        ont_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid)
        return float(ont_geodata[0]), float(ont_geodata[1])

    def _max_allowable_impedance_per_km(
        self,
        voltage_drop_percent: float,
        line_current_ka: float,
        distance_km: float,
        parallel_count: int,
    ) -> float:
        """Return the maximum allowable cable impedance magnitude in ohm/km.

        The planning budget uses three-phase line voltage, so the voltage-drop
        criterion is based on

            delta_u = sqrt(3) * I * Z * L

        with ``I`` in kA, ``Z`` in ohm/km, and ``L`` in km.
        """
        voltage_denominator = self._THREE_PHASE_FACTOR * line_current_ka * distance_km / parallel_count
        if voltage_denominator <= 0 or not np.isfinite(voltage_denominator):
            return np.inf
        return self._NOMINAL_VOLTAGE_KV * voltage_drop_percent / 100 / voltage_denominator

    def create_lvmv_bus(self, plz: int, kcid: int, bcid: int) -> None:
        """Create LV and MV buses."""
        lv_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid)
        lv_bus_spec = BusSpec(
            name="LVbus 1",
            voltage_kv=VN * 1e-3,
            coordinates=lv_geodata
        )
        mv_geodata = (float(lv_geodata[0]), float(lv_geodata[1]) + 1.5 * 1e-4)
        mv_bus_spec = BusSpec(
            name="MVbus 1",
            voltage_kv=20.0,
            coordinates=mv_geodata
        )
        self.backend.create_component(lv_bus_spec)
        self.backend.create_component(mv_bus_spec)

        self.backend.create_component(ExtGridSpec(name="External grid", bus="MVbus 1", vm_pu=1))

    def create_transformer(self, plz: int, kcid: int, bcid: int) -> None:
        """
        Create a transformer based on the required rated power.

        Maps the required capacity to either a single standard transformer
        or a parallel configuration (2x) for specific larger loads.
        """
        transformer_rated_power = self.dbc.get_transformer_rated_power_from_bcid(plz, kcid, bcid)

        if transformer_rated_power in (100, 160, 250, 400, 630):
            trafo_name = f"single {str(transformer_rated_power)} kva transformer"
            kva = transformer_rated_power
            parallel = 1
        elif transformer_rated_power in (500, 800, 1260):
            trafo_name = f"double {str(transformer_rated_power * 0.5)} transformer"
            kva = transformer_rated_power * 0.5
            parallel = 2
        else:
            # Fallback: use 630 kVA transformers in parallel for large parallel transormers
            kva = 630
            parallel = max(1, int(transformer_rated_power / 630))
            trafo_name = f"{str(parallel)}-fold 630 transformer"

        trafo_spec = TransformerSpec(
            name=trafo_name,
            bus1="MVbus 1",
            bus2="LVbus 1",
            kva=kva,
            parallel=parallel
        )

        self.backend.create_component(trafo_spec)
        self.backend.set_transformer_rating(trafo_spec.name, transformer_rated_power * 1e-3)

    def create_connection_bus(self, connection_nodes: list):
        """Create connection buses."""
        for node in connection_nodes:
            node_geodata = self.dbc.get_node_geom(node)
            bus_spec = BusSpec(
                name=f"Connection Nodebus {node}",
                voltage_kv=VN * 1e-3,
                coordinates=node_geodata,
            )
            self.backend.create_component(bus_spec)

    def create_consumer_bus_and_load(self, consumer_list: list, sim_load_per_building: dict, buildings_df: pd.DataFrame,
                                     load_type: dict) -> None:
        """Create consumer buses and loads with simultaneity-adjusted power.

        Applies Kerber formula per building to calculate simultaneous load.
        """

        for consumer in consumer_list:
            node_geodata = self.dbc.get_node_geom(consumer)
            ltype = load_type[consumer]
            total_installed_kw = buildings_df[buildings_df["vertice_id"] == consumer]["peak_load_in_kw"].sum()
            simultaneous_load_kw = sim_load_per_building[consumer]
            # Calculate reactive power from power factor
            phi = np.arccos(DEFAULT_POWER_FACTOR)
            kvar = simultaneous_load_kw * np.tan(phi)

            # Create bus
            bus_spec = BusSpec(
                name=f"Consumer Nodebus {consumer}",
                voltage_kv=VN * 1e-3,
                coordinates=node_geodata,
                zone=ltype
            )
            self.backend.create_component(bus_spec)
            self.backend.set_bus_zone(bus_spec.name, ltype)

            # Create one aggregated load per building with simultaneous load
            load_spec = LoadSpec(
                name=f"Load {consumer}",
                bus=f"Consumer Nodebus {consumer}",
                kw=simultaneous_load_kw,
                kvar=kvar,
                max_p_mw=total_installed_kw * 1e-3,
            )
            self.backend.create_component(load_spec)

    def install_consumer_cables(self, plz: int, bcid: int, kcid: int,
                                branch_node_list: list,
                                ont_vertice: int, vertices_dict: dict, Pd: dict,
                                local_length_dict: dict) -> dict:
        """Install consumer connection cables."""
        consumer_list = self.dbc.get_vertices_from_connection_points(branch_node_list)
        branch_consumer_list = [n for n in consumer_list if n in vertices_dict.keys()]

        for vertice in branch_consumer_list:
            path_list = self.dbc.get_path_to_bus(vertice, ont_vertice)
            start_vid = path_list[1]
            end_vid = path_list[0]

            start_node_geodata = self._get_line_node_coordinates(start_vid, f"Connection Nodebus {start_vid}")
            end_node_geodata = self._get_line_node_coordinates(end_vid, f"Consumer Nodebus {end_vid}")
            line_geodata = [start_node_geodata, end_node_geodata]

            length_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3
            count = 1
            sim_load = Pd[end_vid]
            Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))

            connection_available_cables = self._consumer_connection_cables
            # Direct high-load supplies from a dedicated transformer connection point may use feeder cables as well.
            if start_vid == ont_vertice and sim_load > SMALL_LOAD_THRESHOLD_KW:
                connection_available_cables = list(dict.fromkeys(
                    self._consumer_connection_cables + self._feeder_available_cables
                ))

            voltage_available_cables_df = None
            voltage_drop_percent = (
                VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM
                if sim_load <= SMALL_LOAD_THRESHOLD_KW
                else VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM
            )
            line_df = self._cable_df
            while True:
                current_available_cables_df = line_df.loc[
                    (line_df["max_i_ka"] >= Imax / count) & (line_df.index.isin(connection_available_cables))
                ].copy()

                if len(current_available_cables_df) == 0:
                    count += 1
                    continue

                current_available_cables_df["cable_impedence"] = np.sqrt(
                    current_available_cables_df["r_ohm_per_km"] ** 2 +
                    current_available_cables_df["x_ohm_per_km"] ** 2
                )

                if Imax * length_km == 0:
                    voltage_available_cables_df = current_available_cables_df
                else:
                    max_impedance = self._max_allowable_impedance_per_km(
                        voltage_drop_percent,
                        Imax,
                        length_km,
                        count,
                    )
                    voltage_available_cables_df = current_available_cables_df[
                        current_available_cables_df["cable_impedence"] <= max_impedance
                    ]

                if len(voltage_available_cables_df) == 0:
                    count += 1
                    continue
                else:
                    break

            cable = voltage_available_cables_df.sort_values(by=["q_mm2"]).index.tolist()[0]
            local_length_dict[cable] += count * length_km

            line_spec = LineSpec(
                name=f"Line to {end_vid}",
                bus1=f"Connection Nodebus {start_vid}",
                bus2=f"Consumer Nodebus {end_vid}",
                cable_name=cable,
                length_km=length_km,
                parallel=count,
                coordinates=line_geodata,
            )
            self.backend.create_component(line_spec)

            line_name = f"L{end_vid}"[:15]
            self.dbc.insert_lines(
                geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
                std_type=cable,
                from_bus=start_vid,
                to_bus=end_vid,
                length_km=length_km,
                parallel=count,
            )

        return local_length_dict

    def find_minimal_available_cable(self, Imax: float, distance: int = 0) -> tuple[str, int]:
        """Find the smallest feeder cable that meets current and voltage-drop limits.

        ``distance`` is the routed feeder length in meters, matching the
        ``agg_cost`` values returned by the pgRouting queries used throughout
        grid generation. The voltage-drop check therefore converts it to km
        before combining it with ``r_ohm_per_km`` / ``x_ohm_per_km`` cable data.
        """
        count = 1
        cable = None
        line_df = self._cable_df
        distance_km = distance * 1e-3

        while True:
            current_available_cables = line_df.loc[
                (line_df["max_i_ka"] >= Imax / count) &
                (line_df.index.isin(self._feeder_available_cables))
            ].copy()

            if len(current_available_cables) == 0:
                count += 1
                continue

            if distance_km != 0:
                current_available_cables["cable_impedence"] = np.sqrt(
                    current_available_cables["r_ohm_per_km"] ** 2 +
                    current_available_cables["x_ohm_per_km"] ** 2
                )

                feeder_voltage_drop_percent = (1 - V_BAND_LOW) * 100
                max_impedance = self._max_allowable_impedance_per_km(
                    feeder_voltage_drop_percent,
                    Imax,
                    distance_km,
                    count,
                )
                if not np.isfinite(max_impedance):
                    voltage_available_cables = current_available_cables
                else:
                    voltage_available_cables = current_available_cables[
                        current_available_cables["cable_impedence"] <= max_impedance
                    ]

                if len(voltage_available_cables) == 0:
                    count += 1
                    continue
                else:
                    cable = voltage_available_cables.sort_values(by=["q_mm2"]).index.tolist()[0]
                    break
            else:
                cable = current_available_cables.sort_values(by=["q_mm2"]).index.tolist()[0]
                break

        return cable, count

    def create_line_ont_to_lv_bus(self, plz: int, bcid: int, kcid: int,
                                   branch_start_node: int,
                                   cable: str, count: int, ont_vertice: int):
        """Create line from transformer to connection node."""
        end_vid = branch_start_node
        node_geodata = self._get_line_node_coordinates(end_vid, f"Connection Nodebus {end_vid}")

        transformer_geodata = self._get_transformer_visual_coordinates(plz, kcid, bcid)
        line_geodata = [transformer_geodata, node_geodata]
        # When branch starts at transformer, use 1 meter minimum to avoid zero-impedance
        length_km = 0.001

        line_spec = LineSpec(
            name=f"Line to {end_vid}",
            bus1="LVbus 1",
            bus2=f"Connection Nodebus {end_vid}",
            cable_name=cable,
            length_km=length_km,
            parallel=count,
            coordinates=line_geodata,
        )
        self.backend.create_component(line_spec)

    def create_line_start_to_lv_bus(self, plz: int, bcid: int, kcid: int,
                                     branch_start_node: int,
                                     vertices_dict: dict, cable: str, count: int,
                                     ont_vertice: int) -> int:
        """Create line from branch start to LV bus."""
        node_path_list = self.dbc.get_path_to_bus(branch_start_node, ont_vertice)

        line_geodata = []
        for p in node_path_list:
            node_geodata = self._get_line_node_coordinates(p, f"Connection Nodebus {p}")
            line_geodata.append(node_geodata)

        transformer_geodata = self._get_transformer_visual_coordinates(plz, kcid, bcid)
        line_geodata.append(transformer_geodata)
        line_geodata.reverse()
        if len(line_geodata) > 2:
            del line_geodata[1]

        length_km = vertices_dict[branch_start_node] * 1e-3

        length = count * length_km

        line_spec = LineSpec(
            name=f"Line to {branch_start_node}",
            bus1="LVbus 1",
            bus2=f"Connection Nodebus {branch_start_node}",
            cable_name=cable,
            length_km=length_km,
            parallel=count,
            coordinates=line_geodata,
        )
        self.backend.create_component(line_spec)

        line_name = f"L{branch_start_node}"[:15]
        self.dbc.insert_lines(
            geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
            std_type=cable,
            from_bus=ont_vertice,  # Use vertex ID directly (backend-agnostic)
            to_bus=branch_start_node,
            length_km=length_km,
            parallel=count,
        )

        return length

    def create_line_node_to_node(self, plz: int, kcid: int, bcid: int,
                                  branch_node_list: list,
                                  vertices_dict: dict, local_length_dict: dict,
                                  cable: str, ont_vertice: int, count: float) -> dict:
        """Create lines between connection nodes."""
        for i in range(len(branch_node_list) - 1):
            node_path_list = self.dbc.get_path_to_bus(branch_node_list[i], ont_vertice)

            if branch_node_list[i + 1] not in node_path_list:
                node_path_list = self.dbc.get_path_to_bus(branch_node_list[i], branch_node_list[i + 1])

            node_path_list = node_path_list[: node_path_list.index(branch_node_list[i + 1]) + 1]
            node_path_list.reverse()

            start_vid = node_path_list[0]
            end_vid = node_path_list[-1]

            line_geodata = []
            for p in node_path_list:
                node_geodata = self._get_line_node_coordinates(p, f"Connection Nodebus {p}")
                line_geodata.append(node_geodata)

            if start_vid == ont_vertice and line_geodata:
                line_geodata[0] = self._get_transformer_visual_coordinates(plz, kcid, bcid)

            length_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3
            local_length_dict[cable] += count * length_km

            line_spec = LineSpec(
                name=f"Line to {end_vid}",
                bus1=f"Connection Nodebus {start_vid}",
                bus2=f"Connection Nodebus {end_vid}",
                cable_name=cable,
                length_km=length_km,
                parallel=count,
                coordinates=line_geodata,
            )
            self.backend.create_component(line_spec)

            line_name = f"L{end_vid}"[:15]
            self.dbc.insert_lines(
                geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=line_name,
                std_type=cable,
                from_bus=start_vid,  # Use vertex ID directly (backend-agnostic)
                to_bus=end_vid,
                length_km=length_km,
                parallel=count,
            )

        return local_length_dict
