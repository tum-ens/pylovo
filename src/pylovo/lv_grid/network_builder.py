"""Stage 4 of the LV generation: the electrical network of every supply area.

:class:`NetworkBuilder` builds, validates and stores one network per grid (``kcid``, ``bcid``) of the postcode:
buses, the station transformer and the loads, the feeders (:mod:`pylovo.lv_grid.feeder_planning`) and the service cables
(:class:`~pylovo.lv_grid.cable_installer.CableInstaller`). A grid outside the voltage band goes to the
:class:`~pylovo.lv_grid.voltage_repair.VoltageRepair`, and the grids it changes are built again.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

import pandas as pd  # type: ignore

from pylovo import utils
from pylovo.lv_grid import feeder_planning
from pylovo.lv_grid.cable_installer import CableInstaller
from pylovo.config_loader import (
    CABINET_SPACING_M,
    CONSUMER_CONNECTION_CABLES,
    ELECTRICAL_BACKEND,
    FEEDER_CABLES,
    HOUSE_CONNECTION,
    MAX_VOLTAGE_REPAIRS,
)
from pylovo.electrical_backend import IElectricalBackend, create_backend
from pylovo.lv_grid.context import GenerationContext, Stage
from pylovo.lv_grid.grid_storage import GridStorage
from pylovo.lv_grid.voltage_repair import VoltageRepair


@dataclass
class PostcodeInputs:
    """What every grid of the postcode is built with, read once after the routing topology exists
    (:meth:`NetworkBuilder.load_postcode`).

    Attributes:
        node_coordinates: Coordinates per vertex; a house cable ends at the house connection box on the facade,
            not at the building centroid.
        consumer_connection_mapping: The connection point of every consumer vertex. Read again after a voltage
            repair, which may make buildings their own planning nodes.
        cables: The cable catalogue of the version.
    """

    node_coordinates: dict
    consumer_connection_mapping: Any
    cables: list


@dataclass
class GridInput:
    """The routing and building data of one grid (:meth:`NetworkBuilder.load`).

    Attributes:
        kcid: K-means cluster of the grid.
        bcid: Building cluster of the grid (negative: the area of an existing station).
        distance_by_vertex: Routed distance from the station per vertex of the grid.
        station_vertex: The vertex of the station.
        buildings: The buildings of the grid.
        consumers: The consumer vertices, in the order of the buildings.
        connection_nodes: The street-side vertices that are not consumers, and the buildings that are their own
            planning node.
        paths_to_station: The routed path of each vertex to the station.
    """

    kcid: int
    bcid: int
    distance_by_vertex: dict[int, float]
    station_vertex: int
    buildings: pd.DataFrame
    consumers: list[int]
    connection_nodes: list[int]
    paths_to_station: dict[int, tuple[int, ...]]


@dataclass
class FeederPlan:
    """The split topology of one grid (:meth:`NetworkBuilder._plan_feeders`).

    Attributes:
        branches: The feeder branches of the street tree.
        cabinet_plan: The cabinets of a cabinet network (``HOUSE_CONNECTION: cabinet``), else None.
        feeder_branches: The branches the feeders are sized and laid along: ``branches``, or the branches from
            cabinet to cabinet.
        feeder_buildings: The buildings as the feeders see them: at their planning node, or at their cabinet.
        feeder_nodes: The nodes that get a connection bus: the connection nodes, or the cabinets.
        buildings_by_node: Number of buildings per planning node (cabinet networks only).
    """

    branches: list[feeder_planning.FeederBranch]
    cabinet_plan: Any
    feeder_branches: list[feeder_planning.FeederBranch]
    feeder_buildings: pd.DataFrame
    feeder_nodes: list[int]
    buildings_by_node: dict[int, int]


@dataclass
class BuiltGrid:
    """One grid on its electrical backend, before its validation and storage (:meth:`NetworkBuilder.build`).

    Attributes:
        backend: The electrical backend that holds the network.
        installer: The installer of the grid; its line rows are still queued.
        plan: The split topology.
        feeder_design: The sizing of the feeder tree.
        service_diagnostics: One row per service cable.
        material_length_by_cable_km: Installed cable length per cable type (parallel cables count per cable).
    """

    backend: IElectricalBackend
    installer: CableInstaller
    plan: FeederPlan
    feeder_design: feeder_planning.FeederDesign
    service_diagnostics: list[dict]
    material_length_by_cable_km: dict


class NetworkBuilder(Stage):
    """Build the networks of all grids of the postcode (:meth:`run`)."""

    def __init__(self, context: GenerationContext):
        super().__init__(context)
        self.repair = VoltageRepair(context)
        self.storage = GridStorage(context)

    def run(self) -> None:
        """Build, validate and store the electrical network of every grid of the PLZ.

        For each grid (``kcid``, ``bcid``) of ``grid_result``:

        1. load its buildings and routed distances (:meth:`load`),
        2. build its network on the configured electrical backend (:meth:`build`): buses, the station
           transformer and the snapshot loads, the planned and sized feeders
           (:mod:`pylovo.lv_grid.feeder_planning`) and the service cables,
        3. run the validation power flow (``GridStorage.validate_net``); a grid outside the voltage band is
           planned again by ``VoltageRepair.repair`` (at most ``MAX_VOLTAGE_REPAIRS`` times
           in a row), and the grids it changed or added are built in turn,
        4. write the lines to ``lines_result`` and rebuild the GIS helper rows (:meth:`_store_lines`),
        5. store the network (``GridStorage.save_net``).
        """
        # Get all clusters for the postal code area
        cluster_list = self.dbc.get_list_from_plz(self.plz)
        total_clusters = len(cluster_list)
        ci_count = 0
        next_progress_checkpoint = 10
        converged_count = 0
        not_converged_count = 0
        voltage_violation_count = 0

        postcode = self.load_postcode()

        # (kcid, bcid, repairs so far): a repaired grid and the grids a repair adds are built again
        work = deque((int(kcid), int(bcid), 0) for kcid, bcid in cluster_list)
        repaired_count = 0
        while work:
            kcid, bcid, repairs = work.popleft()
            self.logger.debug(f"Start cable installation for PLZ {self.plz} kcid {kcid} bcid {bcid}")
            grid = self.load(kcid, bcid)
            built = self.build(grid, postcode)

            # Validation power flow before anything of the grid is stored: a grid outside the voltage band
            # may be planned again (its lines are still only queued and on the backend).
            validation = self.storage.validate_net(built.backend, kcid, bcid)
            if validation[0] == "voltage_violation" and repairs < MAX_VOLTAGE_REPAIRS:
                rebuild = self.repair.repair(kcid, bcid, built.backend, grid.buildings, grid.station_vertex)
                if rebuild:
                    repaired_count += 1
                    total_clusters += len(rebuild) - 1
                    work.extend((rebuild_kcid, rebuild_bcid, repairs + 1) for rebuild_kcid, rebuild_bcid in rebuild)
                    # a repair may have made buildings their own planning nodes
                    postcode.consumer_connection_mapping = self.dbc.fetch_consumer_connection_mapping(self.plz)
                    continue

            self._store_lines(grid, built)
            service_planning_diagnostics = self._summarize_service_diagnostics(built.service_diagnostics)
            self._log_grid(grid, built, service_planning_diagnostics)
            planning_diagnostics = {**built.feeder_design.diagnostics, **service_planning_diagnostics}

            # Track and report progress using real cluster counts.
            ci_count += 1
            current_percent = int((ci_count / total_clusters) * 100)

            while current_percent >= next_progress_checkpoint and next_progress_checkpoint <= 100:
                self.logger.info(
                    f"Cable installation progress: {ci_count}/{total_clusters} clusters ({current_percent}%)"
                )
                next_progress_checkpoint += 10

            powerflow_status = self.storage.save_net(
                built.backend,
                kcid,
                bcid,
                planning_diagnostics=planning_diagnostics,
                validation=validation,
            )
            if powerflow_status == "converged":
                converged_count += 1
            elif powerflow_status == "voltage_violation":
                voltage_violation_count += 1
            else:
                not_converged_count += 1

        self.logger.info(
            f"Cable installation finished for PLZ {self.plz}: processed_clusters={total_clusters}, "
            f"power_flow_converged={converged_count}/{total_clusters}, "
            f"power_flow_not_converged={not_converged_count}, "
            f"voltage_band_violations={voltage_violation_count}, "
            f"voltage_repairs={repaired_count}"
        )

    # ------------------------------------------------------------------------------------------------ inputs
    def load_postcode(self) -> PostcodeInputs:
        """Read what is the same for every grid of the PLZ, once after the pgRouting topology exists."""
        node_coordinates = self.dbc.fetch_node_coordinates(self.plz)
        # House cables end at the house connection box on the facade, not at the building centroid.
        node_coordinates.update(self.dbc.fetch_house_connection_points(self.plz))
        consumer_connection_mapping = self.dbc.fetch_consumer_connection_mapping(
            self.plz,
        )
        cables = self.dbc.fetch_cables()
        return PostcodeInputs(node_coordinates, consumer_connection_mapping, cables)

    def load(self, kcid: int, bcid: int) -> GridInput:
        """Load the routing and building data of one grid for cable installation.

        A building that is its own planning node (voltage repair of a house connection,
        :meth:`VoltageRepair.repair <pylovo.lv_grid.voltage_repair.VoltageRepair.repair>`) is a feeder node at the
        building. A station at such a building has no route to itself: its vertex is added at distance 0.
        """
        vertices_dict, ont_vertice, paths_to_transformer = (
            self.dbc.get_vertices_from_bcid(self.plz, kcid, bcid)
        )
        buildings_df = self.dbc.get_buildings_from_bcid(self.plz, kcid, bcid)
        planning = set(utils.planning_nodes(buildings_df).dropna().astype(int).tolist())
        if int(ont_vertice) in planning and int(ont_vertice) not in vertices_dict:
            vertices_dict = {int(ont_vertice): 0.0, **vertices_dict}
            paths_to_transformer = {int(ont_vertice): (int(ont_vertice),), **paths_to_transformer}
        vertices_list = list(vertices_dict.keys())

        consumer_list = buildings_df.vertice_id.to_list()
        consumer_list = list(dict.fromkeys(consumer_list))  # removing duplicates

        connection_nodes = [i for i in vertices_list if i not in consumer_list or i in planning]

        return GridInput(
            kcid=kcid,
            bcid=bcid,
            distance_by_vertex=vertices_dict,
            station_vertex=ont_vertice,
            buildings=buildings_df,
            consumers=consumer_list,
            connection_nodes=connection_nodes,
            paths_to_station=paths_to_transformer,
        )

    # ------------------------------------------------------------------------------------------------- build
    def build(self, grid: GridInput, postcode: PostcodeInputs) -> BuiltGrid:
        """Build the network of one grid on a fresh electrical backend; nothing of it is stored yet.

        The steps: the consumer loads, the backend with the cable catalogue and the installer, the split
        topology (:meth:`_plan_feeders`), the buses with the station transformer and the loads, the sizing of
        the feeder tree, the feeder lines (:meth:`_install_feeder_lines`) and the service cables
        (:meth:`_install_service_cables`).
        """
        kcid, bcid = grid.kcid, grid.bcid
        consumer_df = self.consumer_categories
        service_design_load_per_consumer, powerflow_snapshot_components = (
            utils.allocate_consumer_simultaneous_loads(
                grid.consumers,
                grid.buildings,
                consumer_df,
            )
        )

        transformer_coordinates = self.dbc.get_ont_geom_from_bcid(self.plz, kcid, bcid)
        transformer_rated_power = self.dbc.get_transformer_rated_power_from_bcid(
            self.plz, kcid, bcid
        )

        # Initialize backend and register the already-fetched cable catalog.
        backend = create_backend(ELECTRICAL_BACKEND, logger=self.logger)
        circuit_name = f"PLZ{self.plz}_kcid{kcid}_bcid{bcid}"
        backend.initialize_circuit(
            name=circuit_name, source_bus="MVbus 1", primary_kv=20.0
        )
        backend.register_cable_types(postcode.cables)

        # Get available cable
        all_available_cables = backend.get_cable_types()
        if not all_available_cables:
            all_available_cables = [cable[0] for cable in postcode.cables]

        # Tracks installed cable material length, so parallel cables count multiple times.
        material_length_by_cable_km = {c: 0 for c in all_available_cables}

        # Create cable installer
        installer = CableInstaller(
            backend,
            self.dbc,
            self.logger,
            postcode.cables,
            FEEDER_CABLES,
            CONSUMER_CONNECTION_CABLES,
            node_coordinates=postcode.node_coordinates,
            transformer_coordinates=transformer_coordinates,
            transformer_rated_power=transformer_rated_power,
            consumer_connection_mapping=postcode.consumer_connection_mapping,
            paths_to_transformer=grid.paths_to_station,
            context=(self.plz, kcid, bcid),
        )

        plan = self._plan_feeders(grid)

        # Create network components (the backend creates them in batches)
        with backend.batch():
            installer.create_lvmv_bus(self.plz, kcid, bcid)
            installer.create_transformer(self.plz, kcid, bcid)
            installer.create_connection_bus(plan.feeder_nodes, station_vertex=grid.station_vertex)
            installer.create_consumer_bus_and_load(
                grid.consumers, powerflow_snapshot_components
            )

        self.logger.debug(
            f"Backend network initialized (buses={backend.get_component_count('buses')}, "
            f"loads={backend.get_component_count('loads')}, "
            f"transformer_rated_power={transformer_rated_power} kVA)"
        )

        # Size every feeder segment on the finalized tree so shared prefixes carry
        # the full downstream load; in a cabinet network the cable may change at every cabinet.
        feeder_design = feeder_planning.size_feeder_tree(
            installer,
            plan.feeder_branches,
            grid.station_vertex,
            grid.distance_by_vertex,
            plan.feeder_buildings,
            consumer_df,
            self.logger,
            self.plz,
            hard_nodes=list(plan.cabinet_plan.kind_by_cabinet) if plan.cabinet_plan else (),
        )
        with backend.batch():
            material_length_by_cable_km = self._install_feeder_lines(
                installer, plan.feeder_branches, feeder_design, plan.feeder_buildings, grid, material_length_by_cable_km
            )
            material_length_by_cable_km, service_diagnostics = self._install_service_cables(
                installer, grid, plan, feeder_design, service_design_load_per_consumer, material_length_by_cable_km
            )
        return BuiltGrid(backend, installer, plan, feeder_design, service_diagnostics, material_length_by_cable_km)

    def _path_to_transformer_lookup(self, grid: GridInput) -> feeder_planning.PathLookup:
        """Return a path lookup that prefers the cached routes and falls back to pgRouting."""

        def path_to_transformer(node: int):
            return grid.paths_to_station.get(node) or self.dbc.get_path_to_bus(node, grid.station_vertex)

        return path_to_transformer

    def _plan_feeders(self, grid: GridInput) -> FeederPlan:
        """Finalize the split topology of the grid.

        In a cabinet network the feeders then run from cabinet to cabinet and every building gets its own
        cable from its cabinet.
        """
        branches = feeder_planning.plan_feeder_branches(
            grid.connection_nodes,
            grid.distance_by_vertex,
            grid.station_vertex,
            self._path_to_transformer_lookup(grid),
            grid.buildings,
            self.consumer_categories,
            self.logger,
        )
        if HOUSE_CONNECTION != "cabinet":
            return FeederPlan(branches, None, branches, grid.buildings, grid.connection_nodes, {})
        buildings_by_node = utils.planning_nodes(grid.buildings).dropna().astype(int).value_counts().to_dict()
        cabinet_plan = feeder_planning.plan_cabinets(
            branches, grid.station_vertex, grid.distance_by_vertex, CABINET_SPACING_M, buildings_by_node
        )
        return FeederPlan(
            branches=branches,
            cabinet_plan=cabinet_plan,
            feeder_branches=cabinet_plan.branches,
            feeder_buildings=feeder_planning.cabinet_buildings(grid.buildings, cabinet_plan.cabinet_by_node),
            feeder_nodes=sorted(cabinet_plan.kind_by_cabinet),
            buildings_by_node=buildings_by_node,
        )

    def _install_feeder_lines(
        self,
        installer: CableInstaller,
        branches: list[feeder_planning.FeederBranch],
        design: feeder_planning.FeederDesign,
        buildings_df: pd.DataFrame,
        grid: GridInput,
        material_length_by_cable_km: dict,
    ) -> dict:
        """Create the planned feeder lines on the backend, branch by branch.

        Per branch: the lines between its nodes, then the line that connects its
        start node to the attachment node (a split point) or to ``LVbus 1``. The
        transformer vertex itself is ``LVbus 1`` (the station busbar), so a branch
        that starts there needs no further line.

        Args:
            branches: The branches the feeders are laid along (:attr:`FeederPlan.feeder_branches`).
            buildings_df: The buildings as the feeders see them (:attr:`FeederPlan.feeder_buildings`).

        Returns:
            ``material_length_by_cable_km``, updated.
        """
        vertices_dict, ont_vertice, kcid, bcid = grid.distance_by_vertex, grid.station_vertex, grid.kcid, grid.bcid
        loads = utils.CoincidentLoads(buildings_df, self.consumer_categories)
        for branch in branches:
            branch_nodes = list(branch.nodes)
            branch_index = int(branch.index)
            attachment_node = int(branch.attachment_node)

            for index in range(len(branch_nodes) - 1):
                edge = (int(branch_nodes[index + 1]), int(branch_nodes[index]))
                material_length_by_cable_km = self._install_feeder_edge(
                    installer, design, edge, grid, material_length_by_cable_km
                )

            branch_start_node = int(branch_nodes[-1])
            sim_load = loads.simultaneous_peak_load(design.downstream_nodes_by_node[branch_start_node])
            if branch_start_node == ont_vertice:
                # its first lines already leave the station busbar (the transformer vertex is LVbus 1)
                self.logger.debug(
                    f"Branch {branch_index} starts at the station busbar (load_kw={sim_load:.2f})."
                )
            elif attachment_node != ont_vertice:
                edge = (attachment_node, branch_start_node)
                material_length_by_cable_km = self._install_feeder_edge(
                    installer, design, edge, grid, material_length_by_cable_km
                )
                cable, count = design.cable_by_edge[edge]
                self.logger.debug(
                    f"Branch {branch_index} attached to finalized split node {attachment_node} after two-pass sizing "
                    f"(cable={cable}, parallels={count}, load_kw={sim_load:.2f})."
                )
            else:
                edge = (ont_vertice, branch_start_node)
                cable, count = design.cable_by_edge[edge]
                sizing = design.sizing_by_edge[edge]
                length = installer.create_line_start_to_lv_bus(
                    self.plz,
                    bcid,
                    kcid,
                    branch_start_node,
                    vertices_dict,
                    cable,
                    count,
                    ont_vertice,
                    design.section_by_edge[edge],
                    feeder_sizing_basis=sizing["feeder_sizing_basis"],
                    ampacity_std_type=sizing["ampacity_std_type"],
                    ampacity_parallel=sizing["ampacity_parallel"],
                )
                material_length_by_cable_km[cable] += length
                self.logger.debug(
                    f"Branch {branch_index} connected to LV bus after two-pass sizing "
                    f"(cable={cable}, parallels={count}, length_km={length:.4f}, load_kw={sim_load:.2f})."
                )

        return material_length_by_cable_km

    def _install_feeder_edge(
        self,
        installer: CableInstaller,
        design: feeder_planning.FeederDesign,
        edge: tuple[int, int],
        grid: GridInput,
        material_length_by_cable_km: dict,
    ) -> dict:
        """Create the feeder line of one ``(parent, child)`` edge between two connection nodes."""
        parent, child = edge
        cable, count = design.cable_by_edge[edge]
        sizing = design.sizing_by_edge[edge]
        return installer.create_line_node_to_node(
            self.plz,
            grid.kcid,
            grid.bcid,
            [child, parent],
            grid.distance_by_vertex,
            material_length_by_cable_km,
            cable,
            grid.station_vertex,
            count,
            design.section_by_edge[edge],
            feeder_sizing_basis=sizing["feeder_sizing_basis"],
            ampacity_std_type=sizing["ampacity_std_type"],
            ampacity_parallel=sizing["ampacity_parallel"],
        )

    def _install_service_cables(
        self,
        installer: CableInstaller,
        grid: GridInput,
        plan: FeederPlan,
        feeder_design: feeder_planning.FeederDesign,
        service_design_load_per_consumer: dict,
        material_length_by_cable_km: dict,
    ) -> tuple[dict, list[dict]]:
        """Size and create the service cables: from the cabinets in a cabinet network, else branch by branch.

        Returns:
            ``(material_length_by_cable_km, service diagnostics)``.
        """
        vertices_dict, ont_vertice = grid.distance_by_vertex, grid.station_vertex
        service_diagnostics = []
        cabinet_plan = plan.cabinet_plan
        if cabinet_plan is not None:
            # Buildings planned at one node share the house-connection cable from their cabinet.
            shared_design_load_by_node = {
                node: utils.simultaneous_peak_load(grid.buildings, self.consumer_categories, [node])
                for node, count in plan.buildings_by_node.items()
                if count > 1 and cabinet_plan.cabinet_by_node.get(node, node) != node
            }
            material_length_by_cable_km, service_diagnostics = installer.install_cabinet_service_cables(
                self.plz,
                grid.bcid,
                grid.kcid,
                cabinet_plan.cabinet_by_node,
                ont_vertice,
                vertices_dict,
                service_design_load_per_consumer,
                material_length_by_cable_km,
                feeder_design.drop_percent_by_node,
                shared_design_load_by_node,
            )
        for branch in plan.branches if cabinet_plan is None else []:
            material_length_by_cable_km, branch_service_diagnostics = (
                installer.install_consumer_cables(
                    list(branch.nodes),
                    ont_vertice,
                    vertices_dict,
                    service_design_load_per_consumer,
                    material_length_by_cable_km,
                    feeder_design.drop_percent_by_node,
                )
            )
            service_diagnostics.extend(branch_service_diagnostics)
        return material_length_by_cable_km, service_diagnostics

    @staticmethod
    def _summarize_service_diagnostics(service_diagnostics: list[dict]) -> dict:
        """Aggregate the per-service diagnostics of one grid for ``grid_result``."""
        total_design_drops = [
            row["total_design_drop_percent"]
            for row in service_diagnostics
            if row["total_design_drop_percent"] is not None
        ]
        return {
            "ampacity_max_service_voltage_drop_percent": max(
                (row["ampacity_drop_percent"] for row in service_diagnostics), default=0.0
            ),
            "selected_max_service_voltage_drop_percent": max(
                (row["selected_drop_percent"] for row in service_diagnostics), default=0.0
            ),
            "service_voltage_drop_limit_met": all(
                row["voltage_drop_limit_met"] for row in service_diagnostics
            ),
            "service_voltage_upgraded_count": sum(
                row["selected_cable"] != row["ampacity_cable"] for row in service_diagnostics
            ),
            "long_service_connection_count": sum(
                row["length_review"] for row in service_diagnostics
            ),
            "max_total_design_voltage_drop_percent": max(total_design_drops, default=None),
        }

    # ------------------------------------------------------------------------------------------------- store
    def _store_lines(self, grid: GridInput, built: BuiltGrid) -> None:
        """Write the lines of a validated grid and rebuild its GIS helper rows.

        The helper rows (split visualization, split points, line cache) are rebuilt inside a savepoint: if
        that fails, the grid is stored without them.
        """
        kcid, bcid = grid.kcid, grid.bcid
        # GIS helper SQL must see every persisted feeder/service line in the same
        # transaction, so flush exactly once before constructing visualization rows.
        built.installer.flush_line_records(self.plz, kcid, bcid)

        cabinet_plan = built.plan.cabinet_plan
        split_visualization_edges = feeder_planning.split_visualization_edges(
            built.plan.feeder_branches, grid.station_vertex
        )
        bcid_token = f"neg_{abs(int(bcid))}" if int(bcid) < 0 else str(int(bcid))
        savepoint_name = f"split_visualization_{self.plz}_{kcid}_{bcid_token}"
        try:
            self.dbc.cur.execute(f"SAVEPOINT {savepoint_name}")
            self.dbc.rebuild_lines_result_helpers_for_split_topology(
                self.plz,
                kcid,
                bcid,
                split_visualization_edges,
            )
            split_visualization_nodes = sorted(
                {int(edge["from_bus"]) for edge in split_visualization_edges}
            )
            self.dbc.rebuild_split_points_for_split_topology(
                self.plz,
                kcid,
                bcid,
                split_visualization_nodes,
                cabinet_plan.kind_by_cabinet if cabinet_plan is not None else None,
            )
            self.dbc.rebuild_lines_result_view_for_grid(self.plz, kcid, bcid)
            self.dbc.cur.execute(f"RELEASE SAVEPOINT {savepoint_name}")
        except Exception as visualization_error:
            try:
                self.dbc.cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name}")
                self.dbc.cur.execute(f"RELEASE SAVEPOINT {savepoint_name}")
            except Exception as rollback_error:
                self.logger.warning(
                    f"Failed to roll back split visualization savepoint for PLZ {self.plz}, "
                    f"kcid={kcid}, bcid={bcid}: {rollback_error}"
                )
                raise
            self.logger.warning(
                f"Skipped split visualization rebuild for PLZ {self.plz}, kcid={kcid}, bcid={bcid}: "
                f"{visualization_error}"
            )

    def _log_grid(self, grid: GridInput, built: BuiltGrid, service_planning_diagnostics: dict) -> None:
        """Log the summary of one built grid: branches, lines, service diagnostics and cable lengths."""
        material_length_by_cable_km = built.material_length_by_cable_km
        material_length = sum(material_length_by_cable_km.values())
        used_material_lengths = {k: v for k, v in material_length_by_cable_km.items() if v > 0}
        if used_material_lengths:
            cable_summary = ", ".join([f"{k}:{v:.3f} km" for k, v in sorted(used_material_lengths.items(), key=lambda x: -x[1])])
        else:
            cable_summary = "no cables installed"

        lines_count = built.backend.get_component_count('lines')
        self.logger.info(
            f"Finished cluster kcid={grid.kcid}, bcid={grid.bcid}: branches={len(built.plan.branches)}, lines={lines_count}, "
            f"service_voltage_upgrades={service_planning_diagnostics['service_voltage_upgraded_count']}, "
            f"unresolved_service_drops={sum(not row['voltage_drop_limit_met'] for row in built.service_diagnostics)}, "
            f"long_services_for_review={service_planning_diagnostics['long_service_connection_count']}, "
            f"material_length={material_length:.3f} km ({cable_summary})"
        )
