"""Stage 4 of the LV generation: the electrical network of every supply area.

:class:`NetworkBuilder` builds, validates and stores one network per grid (``kcid``, ``bcid``) of the postcode:
buses, the station transformer and the loads, the feeders (:mod:`pylovo.feeder_planning`) and the service cables
(:class:`~pylovo.cable_installer.CableInstaller`). A grid outside the voltage band goes to the
:class:`~pylovo.lv_grid.voltage_repair.VoltageRepair`, and the grids it changes are built again.
"""

from collections import deque

import pandas as pd  # type: ignore

from pylovo import utils
from pylovo.config_loader import (
    CABINET_SPACING_M,
    CONSUMER_CONNECTION_CABLES,
    ELECTRICAL_BACKEND,
    FEEDER_CABLES,
    HOUSE_CONNECTION,
    MAX_VOLTAGE_REPAIRS,
)

from pylovo.electrical_backend import create_backend
from pylovo.cable_installer import CableInstaller
from pylovo import feeder_planning
from pylovo.lv_grid.context import GenerationContext

from pylovo.lv_grid.context import Stage
from pylovo.lv_grid.grid_storage import GridStorage
from pylovo.lv_grid.voltage_repair import VoltageRepair


class NetworkBuilder(Stage):
    """Build the networks of all grids of the postcode (:meth:`run`)."""

    def __init__(self, context: GenerationContext):
        super().__init__(context)
        self.repair = VoltageRepair(context)
        self.storage = GridStorage(context)

    def prepare_vertices_list(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        consumer_df: pd.DataFrame | None = None,
    ) -> tuple:
        """Load the routing and building data of one grid for cable installation.

        Returns:
            ``(vertices_dict, ont_vertice, vertices_list, buildings_df, consumer_df,
            consumer_list, connection_nodes, paths_to_transformer)``: routed distance
            from the transformer per vertex, the transformer vertex, the vertices,
            the buildings, the consumer categories (the configured ones if not given), the
            consumer vertices, the connection nodes (the street-side vertices that are
            not consumers, and the buildings that are their own planning node) and the
            routed path of each vertex to the transformer.

        A building that is its own planning node (voltage repair of a house connection,
        :meth:`VoltageRepair.repair <pylovo.lv_grid.voltage_repair.VoltageRepair.repair>`) is a feeder node at the building. A station at
        such a building has no route to itself: its vertex is added at distance 0.
        """
        vertices_dict, ont_vertice, paths_to_transformer = (
            self.dbc.get_vertices_from_bcid(plz, kcid, bcid)
        )
        buildings_df = self.dbc.get_buildings_from_bcid(plz, kcid, bcid)
        planning = set(utils.planning_nodes(buildings_df).dropna().astype(int).tolist())
        if int(ont_vertice) in planning and int(ont_vertice) not in vertices_dict:
            vertices_dict = {int(ont_vertice): 0.0, **vertices_dict}
            paths_to_transformer = {int(ont_vertice): (int(ont_vertice),), **paths_to_transformer}
        vertices_list = list(vertices_dict.keys())

        if consumer_df is None:
            consumer_df = self.consumer_categories
        consumer_list = buildings_df.vertice_id.to_list()
        consumer_list = list(dict.fromkeys(consumer_list))  # removing duplicates

        connection_nodes = [i for i in vertices_list if i not in consumer_list or i in planning]

        return (
            vertices_dict,
            ont_vertice,
            vertices_list,
            buildings_df,
            consumer_df,
            consumer_list,
            connection_nodes,
            paths_to_transformer,
        )

    def _path_to_transformer_lookup(
        self, paths_to_transformer: dict[int, tuple[int, ...]], ont_vertice: int
    ) -> feeder_planning.PathLookup:
        """Return a path lookup that prefers the cached routes and falls back to pgRouting."""

        def path_to_transformer(node: int):
            return paths_to_transformer.get(node) or self.dbc.get_path_to_bus(node, ont_vertice)

        return path_to_transformer

    def _install_feeder_lines(
        self,
        installer: CableInstaller,
        branches: list[feeder_planning.FeederBranch],
        design: feeder_planning.FeederDesign,
        buildings_df: pd.DataFrame,
        consumer_df: pd.DataFrame,
        vertices_dict: dict[int, float],
        ont_vertice: int,
        material_length_by_cable_km: dict,
        kcid: int,
        bcid: int,
    ) -> dict:
        """Create the planned feeder lines on the backend, branch by branch.

        Per branch: the lines between its nodes, then the line that connects its
        start node to the attachment node (a split point) or to ``LVbus 1``. The
        transformer vertex itself is ``LVbus 1`` (the station busbar), so a branch
        that starts there needs no further line.

        Returns:
            ``material_length_by_cable_km``, updated.
        """
        loads = utils.CoincidentLoads(buildings_df, consumer_df)
        for branch in branches:
            branch_nodes = list(branch.nodes)
            branch_index = int(branch.index)
            attachment_node = int(branch.attachment_node)

            for index in range(len(branch_nodes) - 1):
                edge = (int(branch_nodes[index + 1]), int(branch_nodes[index]))
                material_length_by_cable_km = self._install_feeder_edge(
                    installer, design, edge, vertices_dict, material_length_by_cable_km, ont_vertice, kcid, bcid
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
                    installer, design, edge, vertices_dict, material_length_by_cable_km, ont_vertice, kcid, bcid
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
        vertices_dict: dict[int, float],
        material_length_by_cable_km: dict,
        ont_vertice: int,
        kcid: int,
        bcid: int,
    ) -> dict:
        """Create the feeder line of one ``(parent, child)`` edge between two connection nodes."""
        parent, child = edge
        cable, count = design.cable_by_edge[edge]
        sizing = design.sizing_by_edge[edge]
        return installer.create_line_node_to_node(
            self.plz,
            kcid,
            bcid,
            [child, parent],
            vertices_dict,
            material_length_by_cable_km,
            cable,
            ont_vertice,
            count,
            design.section_by_edge[edge],
            feeder_sizing_basis=sizing["feeder_sizing_basis"],
            ampacity_std_type=sizing["ampacity_std_type"],
            ampacity_parallel=sizing["ampacity_parallel"],
        )

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

    def run(self):
        """Build, validate and store the electrical network of every grid of the PLZ.

        For each grid (``kcid``, ``bcid``) of ``grid_result``:

        1. load its buildings, routed distances and consumer loads,
        2. create buses, the station transformer and the snapshot loads on the
           configured electrical backend (:class:`CableInstaller`),
        3. plan the feeder branches and size the feeder cables
           (:mod:`pylovo.feeder_planning`), then create the feeder lines,
        4. size and create the service cables branch by branch,
        5. run the validation power flow (``GridStorage.validate_net``); a grid outside the voltage band is
           planned again by ``VoltageRepair.repair`` (at most ``MAX_VOLTAGE_REPAIRS`` times
           in a row), and the grids it changed or added are built in turn,
        6. write the lines to ``lines_result`` and rebuild the GIS helper rows,
        7. store the network (``GridStorage.save_net``).
        """
        # Get all clusters for the postal code area
        cluster_list = self.dbc.get_list_from_plz(self.plz)
        total_clusters = len(cluster_list)
        ci_count = 0
        next_progress_checkpoint = 10
        converged_count = 0
        not_converged_count = 0
        voltage_violation_count = 0

        # These inputs are invariant for every grid in a PLZ. Fetch them once after
        # pgRouting topology creation and pass immutable snapshots into each installer.
        consumer_df = self.consumer_categories
        node_coordinates = self.dbc.fetch_node_coordinates(self.plz)
        # House cables end at the house connection box on the facade, not at the building centroid.
        node_coordinates.update(self.dbc.fetch_house_connection_points(self.plz))
        consumer_connection_mapping = self.dbc.fetch_consumer_connection_mapping(
            self.plz,
        )
        cables = self.dbc.fetch_cables()

        # (kcid, bcid, repairs so far): a repaired grid and the grids a repair adds are built again
        work = deque((int(kcid), int(bcid), 0) for kcid, bcid in cluster_list)
        repaired_count = 0
        while work:
            kcid, bcid, repairs = work.popleft()
            self.logger.debug(f"Start cable installation for PLZ {self.plz} kcid {kcid} bcid {bcid}")

            # Get data for this cluster
            (
                vertices_dict,
                ont_vertice,
                _vertices_list,
                buildings_df,
                consumer_df,
                consumer_list,
                connection_nodes,
                paths_to_transformer,
            ) = self.prepare_vertices_list(self.plz, kcid, bcid, consumer_df)
            service_design_load_per_consumer, powerflow_snapshot_components = (
                utils.allocate_consumer_simultaneous_loads(
                    consumer_list,
                    buildings_df,
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
            backend.register_cable_types(cables)

            # Get available cable
            all_available_cables = backend.get_cable_types()
            if not all_available_cables:
                all_available_cables = [cable[0] for cable in cables]

            # Tracks installed cable material length, so parallel cables count multiple times.
            material_length_by_cable_km = {c: 0 for c in all_available_cables}

            # Create cable installer
            installer = CableInstaller(
                backend,
                self.dbc,
                self.logger,
                cables,
                FEEDER_CABLES,
                CONSUMER_CONNECTION_CABLES,
                node_coordinates=node_coordinates,
                transformer_coordinates=transformer_coordinates,
                transformer_rated_power=transformer_rated_power,
                consumer_connection_mapping=consumer_connection_mapping,
                paths_to_transformer=paths_to_transformer,
                context=(self.plz, kcid, bcid),
            )

            # First finalize the split topology. In a cabinet network the feeders then run
            # from cabinet to cabinet and every building gets its own cable from its cabinet.
            branches = feeder_planning.plan_feeder_branches(
                connection_nodes,
                vertices_dict,
                ont_vertice,
                self._path_to_transformer_lookup(paths_to_transformer, ont_vertice),
                buildings_df,
                consumer_df,
                self.logger,
            )
            cabinet_plan = None
            feeder_branches, feeder_buildings, feeder_nodes = branches, buildings_df, connection_nodes
            if HOUSE_CONNECTION == "cabinet":
                buildings_by_node = utils.planning_nodes(buildings_df).dropna().astype(int).value_counts().to_dict()
                cabinet_plan = feeder_planning.plan_cabinets(
                    branches, ont_vertice, vertices_dict, CABINET_SPACING_M, buildings_by_node
                )
                feeder_branches = cabinet_plan.branches
                feeder_buildings = feeder_planning.cabinet_buildings(buildings_df, cabinet_plan.cabinet_by_node)
                feeder_nodes = sorted(cabinet_plan.kind_by_cabinet)

            # Create network components (the backend creates them in batches)
            with backend.batch():
                installer.create_lvmv_bus(self.plz, kcid, bcid)
                installer.create_transformer(self.plz, kcid, bcid)
                installer.create_connection_bus(feeder_nodes, station_vertex=ont_vertice)
                installer.create_consumer_bus_and_load(
                    consumer_list, powerflow_snapshot_components
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
                feeder_branches,
                ont_vertice,
                vertices_dict,
                feeder_buildings,
                consumer_df,
                self.logger,
                self.plz,
                hard_nodes=list(cabinet_plan.kind_by_cabinet) if cabinet_plan else (),
            )
            with backend.batch():
                material_length_by_cable_km = self._install_feeder_lines(
                    installer,
                    feeder_branches,
                    feeder_design,
                    feeder_buildings,
                    consumer_df,
                    vertices_dict,
                    ont_vertice,
                    material_length_by_cable_km,
                    kcid,
                    bcid,
                )

                service_diagnostics = []
                if cabinet_plan is not None:
                    # Buildings planned at one node share the house-connection cable from their cabinet.
                    shared_design_load_by_node = {
                        node: utils.simultaneous_peak_load(buildings_df, consumer_df, [node])
                        for node, count in buildings_by_node.items()
                        if count > 1 and cabinet_plan.cabinet_by_node.get(node, node) != node
                    }
                    material_length_by_cable_km, service_diagnostics = installer.install_cabinet_service_cables(
                        self.plz,
                        bcid,
                        kcid,
                        cabinet_plan.cabinet_by_node,
                        ont_vertice,
                        vertices_dict,
                        service_design_load_per_consumer,
                        material_length_by_cable_km,
                        feeder_design.drop_percent_by_node,
                        shared_design_load_by_node,
                    )
                for branch in branches if cabinet_plan is None else []:
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
            service_planning_diagnostics = self._summarize_service_diagnostics(service_diagnostics)

            # Validation power flow before anything of the grid is stored: a grid outside the voltage band
            # may be planned again (its lines are still only queued and on the backend).
            validation = self.storage.validate_net(backend, kcid, bcid)
            if validation[0] == "voltage_violation" and repairs < MAX_VOLTAGE_REPAIRS:
                rebuild = self.repair.repair(kcid, bcid, backend, buildings_df, ont_vertice)
                if rebuild:
                    repaired_count += 1
                    total_clusters += len(rebuild) - 1
                    work.extend((rebuild_kcid, rebuild_bcid, repairs + 1) for rebuild_kcid, rebuild_bcid in rebuild)
                    # a repair may have made buildings their own planning nodes
                    consumer_connection_mapping = self.dbc.fetch_consumer_connection_mapping(self.plz)
                    continue

            # GIS helper SQL must see every persisted feeder/service line in the same
            # transaction, so flush exactly once before constructing visualization rows.
            installer.flush_line_records(self.plz, kcid, bcid)

            split_visualization_edges = feeder_planning.split_visualization_edges(
                feeder_branches, ont_vertice
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

            # Cluster summary
            material_length = sum(material_length_by_cable_km.values())
            used_material_lengths = {k: v for k, v in material_length_by_cable_km.items() if v > 0}
            if used_material_lengths:
                cable_summary = ", ".join([f"{k}:{v:.3f} km" for k, v in sorted(used_material_lengths.items(), key=lambda x: -x[1])])
            else:
                cable_summary = "no cables installed"

            lines_count = backend.get_component_count('lines')
            self.logger.info(
                f"Finished cluster kcid={kcid}, bcid={bcid}: branches={len(branches)}, lines={lines_count}, "
                f"service_voltage_upgrades={service_planning_diagnostics['service_voltage_upgraded_count']}, "
                f"unresolved_service_drops={sum(not row['voltage_drop_limit_met'] for row in service_diagnostics)}, "
                f"long_services_for_review={service_planning_diagnostics['long_service_connection_count']}, "
                f"material_length={material_length:.3f} km ({cable_summary})"
            )

            planning_diagnostics = {**feeder_design.diagnostics, **service_planning_diagnostics}

            # Track and report progress using real cluster counts.
            ci_count += 1
            current_percent = int((ci_count / total_clusters) * 100)

            while current_percent >= next_progress_checkpoint and next_progress_checkpoint <= 100:
                self.logger.info(
                    f"Cable installation progress: {ci_count}/{total_clusters} clusters ({current_percent}%)"
                )
                next_progress_checkpoint += 10

            powerflow_status = self.storage.save_net(
                backend,
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
