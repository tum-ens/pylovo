"""Cable installation module for electrical grid generation."""

import numpy as np
import pandas as pd
from pyproj import Transformer

from pylovo.electrical_backend import IElectricalBackend, BusSpec, TransformerSpec, LineSpec, LoadSpec, ExtGridSpec
from pylovo.config_loader import (
    VN, MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW, DEFAULT_POWER_FACTOR, TARGET_EPSG,
    MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT,
)
from pylovo.utils import oneSimultaneousLoad
from pylovo.electrical_backend import normalize_cable_name


SERVICE_LENGTH_REVIEW_THRESHOLD_M = 100.0


class CableInstaller:
    """Handles cable installation for electrical grids using backend abstraction."""

    _WGS84_TO_TARGET = Transformer.from_crs(4326, TARGET_EPSG, always_xy=True)

    def __init__(self, backend: IElectricalBackend, dbc, logger, cables: list,
                 feeder_cables: pd.DataFrame, consumer_connection_cables: pd.DataFrame):
        """Initialize cable installer.

        Args:
            backend: Electrical backend instance (e.g., PandapowerBackend)
            dbc: Database client for accessing grid data
            logger: Logger instance
            cables: List of cable tuples from database (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur)
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
            cables: List of tuples (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur)

        Returns:
            DataFrame indexed by normalized cable name with electrical properties
        """
        cable_data = {}
        for cable in cables:
            name, r_ohm, x_ohm, max_i_ka, cost_eur = cable
            normalized_name = normalize_cable_name(name)

            try:
                q_mm2 = int(name.split("_")[-1])
            except (ValueError, IndexError):
                q_mm2 = 0

            cable_data[normalized_name] = {
                'r_ohm_per_km': float(r_ohm),
                'x_ohm_per_km': float(x_ohm),
                'max_i_ka': float(max_i_ka),
                'cost_eur': float(cost_eur),
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

    def _service_line_length_km(self, line_geodata: list[tuple[float, float]]) -> float:
        """Return projected straight-line length for a consumer service connection."""
        if len(line_geodata) < 2:
            return 0.0
        start_x, start_y = self._WGS84_TO_TARGET.transform(*line_geodata[0])
        end_x, end_y = self._WGS84_TO_TARGET.transform(*line_geodata[-1])
        return float(np.hypot(end_x - start_x, end_y - start_y) * 1e-3)

    def _get_transformer_visual_coordinates(
        self,
        plz: int,
        kcid: int,
        bcid: int,
    ) -> tuple[float, float]:
        ont_geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid)
        return float(ont_geodata[0]), float(ont_geodata[1])

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

    def create_consumer_bus_and_load(
        self,
        consumer_list: list,
        powerflow_snapshot_components: dict,
    ) -> None:
        """Create one bus and one snapshot load per use component."""

        for consumer in consumer_list:
            node_geodata = self.dbc.get_node_geom(consumer)
            components = powerflow_snapshot_components.get(consumer, [])
            if not components:
                raise ValueError(f"Consumer vertex {consumer} has no LV load components.")
            categories = [component["category"] for component in components]
            load_type = categories[0] if len(categories) == 1 else "Mixed"

            # Create bus
            bus_spec = BusSpec(
                name=f"Consumer Nodebus {consumer}",
                voltage_kv=VN * 1e-3,
                coordinates=node_geodata,
                zone=load_type
            )
            self.backend.create_component(bus_spec)
            self.backend.set_bus_zone(bus_spec.name, load_type)

            for component in components:
                simultaneous_load_kw = float(component["simultaneous_kw"])
                phi = np.arccos(DEFAULT_POWER_FACTOR)
                kvar = simultaneous_load_kw * np.tan(phi)
                load_spec = LoadSpec(
                    name=f"Load {consumer} {component['category']}",
                    bus=f"Consumer Nodebus {consumer}",
                    kw=simultaneous_load_kw,
                    kvar=kvar,
                    max_p_mw=float(component["installed_kw"]) * 1e-3,
                    service_design_p_mw=float(component["service_design_kw"]) * 1e-3,
                    operating_point_basis="synthetic_transformer_coincident_proportional",
                    category=component["category"],
                    load_units=float(component["load_units"]),
                    consumer_vertex=int(consumer),
                )
                self.backend.create_component(load_spec)

    def _service_voltage_drop_percent(
        self,
        design_current_ka: float,
        length_km: float,
        cable: str,
        parallel: int,
    ) -> float:
        """Calculate approximate three-phase service drop at local design load."""
        row = self._cable_df.loc[cable]
        sin_phi = np.sqrt(1 - DEFAULT_POWER_FACTOR ** 2)
        effective_impedance = (
            float(row["r_ohm_per_km"]) * DEFAULT_POWER_FACTOR
            + float(row["x_ohm_per_km"]) * sin_phi
        ) / parallel
        nominal_voltage_kv = VN * 1e-3
        return float(
            np.sqrt(3)
            * design_current_ka
            * length_km
            * effective_impedance
            / nominal_voltage_kv
            * 100
        )

    def select_service_cable_design(
        self,
        design_current_ka: float,
        length_km: float,
        available_cables: list[str],
    ) -> dict:
        """Select a service cable by ampacity, then by local design voltage drop."""
        line_df = self._cable_df.loc[self._cable_df.index.isin(available_cables)]
        if line_df.empty:
            raise ValueError("No configured service cable is available for selection.")

        parallel = 1
        while True:
            ampacity_options = line_df.loc[
                line_df["max_i_ka"] >= design_current_ka / parallel
            ]
            if not ampacity_options.empty:
                break
            parallel += 1

        ampacity_cable = ampacity_options.sort_values(by=["cost_eur", "q_mm2"]).index[0]
        ampacity_drop_percent = self._service_voltage_drop_percent(
            design_current_ka, length_km, ampacity_cable, parallel
        )
        drops_by_cable = {
            cable: self._service_voltage_drop_percent(
                design_current_ka, length_km, cable, parallel
            )
            for cable in ampacity_options.index
        }
        voltage_options = ampacity_options.loc[
            [
                cable
                for cable, drop_percent in drops_by_cable.items()
                if drop_percent <= MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT + 1e-9
            ]
        ]
        if voltage_options.empty:
            selected_cable = min(
                ampacity_options.index,
                key=lambda cable: (
                    drops_by_cable[cable],
                    float(ampacity_options.at[cable, "cost_eur"]),
                    int(ampacity_options.at[cable, "q_mm2"]),
                ),
            )
        else:
            selected_cable = voltage_options.sort_values(by=["cost_eur", "q_mm2"]).index[0]

        selected_drop_percent = drops_by_cable[selected_cable]
        return {
            "cable": selected_cable,
            "parallel": parallel,
            "ampacity_cable": ampacity_cable,
            "ampacity_parallel": parallel,
            "ampacity_drop_percent": ampacity_drop_percent,
            "selected_drop_percent": selected_drop_percent,
            "voltage_drop_limit_met": bool(
                selected_drop_percent <= MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT + 1e-9
            ),
            "sizing_basis": "service_voltage_drop" if selected_cable != ampacity_cable else "ampacity",
        }

    def install_consumer_cables(self, plz: int, bcid: int, kcid: int,
                                branch_node_list: list,
                                ont_vertice: int, vertices_dict: dict,
                                service_design_load_per_consumer: dict,
                                material_length_by_cable_km: dict,
                                feeder_voltage_drop_percent_by_node: dict[int, float] | None = None,
                                ) -> tuple[dict, list[dict]]:
        """Install service cables using local ampacity and voltage-drop design loads."""
        consumer_connections = self.dbc.get_consumer_vertices_from_connection_points(branch_node_list)
        branch_consumer_connections = [
            (connection_point, vertice_id)
            for connection_point, vertice_id in consumer_connections
            if connection_point in vertices_dict and vertice_id in vertices_dict
        ]
        service_diagnostics = []

        for start_vid, end_vid in branch_consumer_connections:
            start_node_geodata = self._get_line_node_coordinates(start_vid, f"Connection Nodebus {start_vid}")
            end_node_geodata = self._get_line_node_coordinates(end_vid, f"Consumer Nodebus {end_vid}")
            line_geodata = [start_node_geodata, end_node_geodata]

            length_km = self._service_line_length_km(line_geodata)
            sim_load = service_design_load_per_consumer[end_vid]
            Imax = sim_load / (VN * DEFAULT_POWER_FACTOR * np.sqrt(3))

            connection_available_cables = self._consumer_connection_cables
            # Direct transformer connection point may use feeder cables as well.
            if start_vid == ont_vertice:
                connection_available_cables = list(dict.fromkeys(
                    self._consumer_connection_cables + self._feeder_available_cables
                ))

            design = self.select_service_cable_design(Imax, length_km, connection_available_cables)
            cable = design["cable"]
            count = design["parallel"]
            length_review = bool(length_km * 1000 > SERVICE_LENGTH_REVIEW_THRESHOLD_M)
            feeder_drop_percent = None
            if feeder_voltage_drop_percent_by_node is not None:
                feeder_drop_percent = feeder_voltage_drop_percent_by_node.get(int(start_vid))
            total_design_drop_percent = (
                None
                if feeder_drop_percent is None
                else float(feeder_drop_percent + design["selected_drop_percent"])
            )
            service_diagnostics.append(
                {
                    "consumer_vertex": int(end_vid),
                    "connection_vertex": int(start_vid),
                    "length_km": float(length_km),
                    "ampacity_cable": design["ampacity_cable"],
                    "selected_cable": cable,
                    "parallel": int(count),
                    "ampacity_drop_percent": float(design["ampacity_drop_percent"]),
                    "selected_drop_percent": float(design["selected_drop_percent"]),
                    "voltage_drop_limit_met": bool(design["voltage_drop_limit_met"]),
                    "length_review": length_review,
                    "total_design_drop_percent": total_design_drop_percent,
                }
            )

            if design["sizing_basis"] == "service_voltage_drop":
                self.logger.info(
                    f"Upsized service cable to consumer {end_vid} from {design['ampacity_cable']} "
                    f"to {cable}: local_design_drop={design['ampacity_drop_percent']:.3f}% -> "
                    f"{design['selected_drop_percent']:.3f}%."
                )
            if not design["voltage_drop_limit_met"]:
                self.logger.warning(
                    f"Service voltage-drop limit remains violated for consumer {end_vid}: "
                    f"selected_cable={cable}, selected_drop={design['selected_drop_percent']:.3f}%, "
                    f"limit={MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT:.3f}%."
                )
            if length_review:
                self.logger.warning(
                    f"Long service connection flagged for review at consumer {end_vid}: "
                    f"length={length_km * 1000:.1f} m, "
                    f"threshold={SERVICE_LENGTH_REVIEW_THRESHOLD_M:.1f} m."
                )

            material_length_by_cable_km[cable] += count * length_km

            line_spec = LineSpec(
                name=f"Line to {end_vid}",
                bus1=f"Connection Nodebus {start_vid}",
                bus2=f"Consumer Nodebus {end_vid}",
                cable_name=cable,
                length_km=length_km,
                parallel=count,
                coordinates=line_geodata,
                service_sizing_basis=design["sizing_basis"],
                ampacity_std_type=design["ampacity_cable"],
                ampacity_parallel=design["ampacity_parallel"],
                service_ampacity_voltage_drop_percent=design["ampacity_drop_percent"],
                service_selected_voltage_drop_percent=design["selected_drop_percent"],
                service_voltage_drop_limit_met=design["voltage_drop_limit_met"],
                service_length_review=length_review,
                total_design_voltage_drop_percent=total_design_drop_percent,
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

        return material_length_by_cable_km, service_diagnostics

    def get_feeder_cable_options(self, Imax: float, max_parallel: int) -> list[dict]:
        """Return thermally feasible feeder designs up to ``max_parallel`` cables."""
        feeder_df = self._cable_df.loc[
            self._cable_df.index.isin(self._feeder_available_cables)
        ]
        options = []
        for parallel in range(1, max_parallel + 1):
            feasible = feeder_df.loc[feeder_df["max_i_ka"] * parallel >= Imax]
            for cable, row in feasible.iterrows():
                options.append(
                    {
                        "cable": cable,
                        "parallel": parallel,
                        "r_ohm_per_km": float(row["r_ohm_per_km"]),
                        "x_ohm_per_km": float(row["x_ohm_per_km"]),
                        "cost_eur_per_m": float(row["cost_eur"]),
                        "q_mm2": int(row["q_mm2"]),
                    }
                )
        return options

    def find_minimal_available_cable(self, Imax: float) -> tuple[str, int]:
        """Find the smallest feeder design that meets the ampacity requirement."""
        count = 1
        line_df = self._cable_df

        while True:
            current_available_cables = line_df.loc[
                (line_df["max_i_ka"] >= Imax / count) &
                (line_df.index.isin(self._feeder_available_cables))
            ].copy()

            if len(current_available_cables) == 0:
                count += 1
                continue
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
            feeder_sizing_basis="ampacity",
            ampacity_std_type=cable,
            ampacity_parallel=count,
        )
        self.backend.create_component(line_spec)

    def create_line_start_to_lv_bus(self, plz: int, bcid: int, kcid: int,
                                     branch_start_node: int,
                                     vertices_dict: dict, cable: str, count: int,
                                     ont_vertice: int, feeder_section_id: int | None = None,
                                     feeder_sizing_basis: str | None = None,
                                     ampacity_std_type: str | None = None,
                                     ampacity_parallel: int | None = None) -> int:
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
            feeder_section_id=feeder_section_id,
            feeder_sizing_basis=feeder_sizing_basis,
            ampacity_std_type=ampacity_std_type,
            ampacity_parallel=ampacity_parallel,
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
            feeder_section_id=feeder_section_id,
        )

        return length

    def create_line_node_to_node(self, plz: int, kcid: int, bcid: int,
                                  branch_node_list: list,
                                  vertices_dict: dict, material_length_by_cable_km: dict,
                                  cable: str, ont_vertice: int, count: float,
                                  feeder_section_id: int | None = None,
                                  feeder_sizing_basis: str | None = None,
                                  ampacity_std_type: str | None = None,
                                  ampacity_parallel: int | None = None) -> dict:
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
            material_length_by_cable_km[cable] += count * length_km

            line_spec = LineSpec(
                name=f"Line to {end_vid}",
                bus1=f"Connection Nodebus {start_vid}",
                bus2=f"Connection Nodebus {end_vid}",
                cable_name=cable,
                length_km=length_km,
                parallel=count,
                coordinates=line_geodata,
                feeder_section_id=feeder_section_id,
                feeder_sizing_basis=feeder_sizing_basis,
                ampacity_std_type=ampacity_std_type,
                ampacity_parallel=ampacity_parallel,
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
                feeder_section_id=feeder_section_id,
            )

        return material_length_by_cable_km
