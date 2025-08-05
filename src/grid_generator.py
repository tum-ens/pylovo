import os
import traceback
import warnings
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import pandapower as pp
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform
from sklearn.cluster import KMeans

import src.database.database_client as dbc
from src.infdb.infdb_client import InfdbClient
from src.parameter_calculator import ParameterCalculator
from src import utils
from src.config_loader import *

class ResultExistsError(Exception):
    "Raised when the PLZ has already been created."
    pass


class GridGenerator:
    """
    Generates the grid for the given plz area
    """

    def __init__(self, plz=999999, **kwargs):
        self.plz = plz
        self.dbc = dbc.DatabaseClient()
        self.dbc.insert_version_if_not_exists()
        self.dbc.insert_parameter_tables(consumer_categories=CONSUMER_CATEGORIES)
        self.logger = utils.create_logger(
            name="GridGenerator", log_file=kwargs.get("log_file", "log.txt"), log_level=LOG_LEVEL
        )
        self.inf_dbc = None
        if USE_INFDB:
            self.inf_dbc = InfdbClient()

    def __del__(self):
        self.dbc.__del__()

    def generate_grid_for_single_plz(self, plz: int, analyze_grids: bool = False) -> None:
        """
        Generates the grid for a single PLZ.

        :param plz: Postal code for which the grid should be generated.
        :type plz: int
        :param analyze_grids: Option to analyze the results after grid generation, defaults to False.
        :type analyze_grids: bool
        """
        self.plz = plz
        print('-------------------- start', self.plz, '---------------------------')
        self.dbc.create_temp_tables()  # create temp tables for the grid generation

        try:
            self.generate_grid()
            self.dbc.save_tables(plz=self.plz)  # Save data from temporary tables to result tables
            self.dbc.commit_changes()
            if analyze_grids:
                pc = ParameterCalculator()
                pc.calc_parameters_per_plz(plz=self.plz)
                self.dbc.commit_changes()  # commit the changes to the database
        except ResultExistsError:
            self.dbc.logger.info(f"Grid for the postcode area {plz} has already been generated.")
        except Exception as e:
            self.logger.error(f"Error during grid generation for PLZ {self.plz}: {e}")
            self.logger.info(f"Skipped PLZ {self.plz} due to generation error.")
            self.dbc.conn.rollback()  # rollback the transaction
            self.dbc.delete_plz_from_sample_set_table(str(CLASSIFICATION_VERSION), self.plz)  # delete from sample set
            traceback.print_exc()

        self.dbc.drop_temp_tables()  # drop temp tables
        self.dbc.refresh_materialized_views() # update the materialized views to reflect changes in their base tables
        self.dbc.commit_changes()  # commit the changes to the database
        print('-------------------- end', self.plz, '-----------------------------')

    @staticmethod
    def _worker(plz: int, analyze_grids: bool) -> None:
        """Execute grid generation for a single PLZ inside a worker process.

        Each worker creates its own ``GridGenerator`` instance to ensure
        separate database connections and dedicated log files.
        """
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)  # ensure log directory exists
        log_file = log_dir / f"log_{plz}.txt"  # unique log file per PLZ
        gg = GridGenerator(log_file=str(log_file))  # new instance for this worker
        gg.generate_grid_for_single_plz(plz, analyze_grids)

    def generate_grid_for_multiple_plz(self, df_plz: pd.DataFrame, analyze_grids: bool = False, n_jobs: int | None = None) -> None:
        """Generate grids for all PLZs contained in the ``plz`` column of ``df_plz``.

        Args:
            df_plz: Table that contains PLZ for grid generation.
            analyze_grids: Option to analyse the results after grid generation.
            n_jobs: Optional number of parallel worker processes. If ``None``
                the value is derived from ``N_JOBS_PERCENTAGE`` in the
                configuration. ``n_jobs`` must be at least ``1``; ``1`` will
                execute sequentially.
        """
        if n_jobs is None:
            cores = os.cpu_count() or 1  # determine available CPU cores
            n_jobs = max(1, round(cores * N_JOBS_PERCENTAGE / 100))  # scale by configured percentage

        if n_jobs < 1:
            raise ValueError("n_jobs must be at least 1")

        if n_jobs == 1:
            # Sequential execution identical to previous behaviour
            for index, row in df_plz.iterrows():
                self.plz = int(row['plz'])
                print('-------------------- start', self.plz, '---------------------------')
                self.dbc.create_temp_tables()  # create temp tables for the grid generation
                try:
                    self.generate_grid()
                    self.dbc.save_tables(plz=self.plz)  # Save data from temporary tables to result tables
                    self.dbc.commit_changes()  # commit the changes to the database
                    if analyze_grids:
                        pc = ParameterCalculator()
                        pc.calc_parameters_per_plz(plz=self.plz)
                        self.dbc.commit_changes()  # commit the changes to the database
                except ResultExistsError:
                    self.dbc.logger.info(f"Grid for the postcode area {self.plz} has already been generated.")
                except Exception as e:
                    self.logger.error(f"Error during grid generation for PLZ {self.plz}: {e}")
                    self.logger.info(f"Skipped PLZ {self.plz} due to generation error.")
                    self.dbc.conn.rollback()  # rollback the transaction
                    self.dbc.delete_plz_from_sample_set_table(str(CLASSIFICATION_VERSION),
                                                              self.plz)  # delete from sample set
                    continue
                print('-------------------- end', self.plz, '-----------------------------')

            self.dbc.drop_temp_tables()  # drop temp tables
            self.dbc.refresh_materialized_views()  # update the materialized views to reflect changes in their base tables
            self.dbc.commit_changes()  # commit the changes to the database
        else:
            # Parallel execution using a process pool
            self.dbc.__del__()  # close unused connection in parent process
            plz_list = df_plz['plz'].astype(int).tolist()
            with ProcessPoolExecutor(max_workers=n_jobs) as executor:
                from itertools import repeat
                executor.map(GridGenerator._worker, plz_list, repeat(analyze_grids))

    def generate_grid(self):
        if self.dbc.is_grid_generated(self.plz):
            raise ResultExistsError(
                f"The grids for the postcode area {self.plz} is already generated "
                f"for the version {VERSION_ID}."
            )
        self.prepare_postcodes()
        self.prepare_buildings()
        self.prepare_transformers()
        self.prepare_ways()
        self.apply_kmeans_clustering()
        self.position_all_transformers()
        self.install_cables()

    def prepare_postcodes(self):
        """
        Caches postcode from raw data tables and stores in temporary tables.
        FROM: postcode
        INTO: postcode_result
        """
        self.dbc.copy_postcode_result_table(self.plz)
        self.logger.info(f"Working on plz {self.plz}")

    def prepare_buildings(self):
        """
        Caches buildings from raw data tables and stores in temporary tables.
        FROM: res, oth
        INTO: buildings_tem
        """
        if USE_INFDB:
            buildings_data = self.inf_dbc.get_relevant_buildings_in_plz(self.plz)
            self.dbc.set_buildings_table(buildings_data)
        else:
            self.dbc.set_residential_buildings_table(self.plz)
            self.dbc.set_other_buildings_table(self.plz)

        self.logger.info("Buildings_tem table prepared")
        self.dbc.remove_duplicate_buildings()
        self.logger.info("Duplicate buildings removed from buildings_tem")

        self.dbc.set_plz_settlement_type(self.plz)
        self.logger.info("House_distance and settlement_type in postcode_result")

        unloadcount = self.dbc.set_building_peak_load()
        self.logger.info(
            f"Building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large_consumers = self.dbc.update_too_large_consumers_to_zero()
        self.logger.info(f"{too_large_consumers} too large consumers removed from buildings_tem")

        self.dbc.assign_close_buildings()
        self.logger.info("All close buildings assigned and removed from buildings_tem")

    def prepare_transformers(self):
        """
        Cache transformers from raw data tables and stores in temporary tables.
        FROM: transformers
        INTO: buildings_tem
        """
        self.dbc.insert_transformers(self.plz)
        self.logger.info("Transformers inserted in to the buildings_tem table")
        self.dbc.count_indoor_transformers()
        self.dbc.drop_indoor_transformers()
        self.logger.info("Indoor transformers dropped from the buildings_tem table")

    def prepare_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        """
        if USE_INFDB:
            ways_rows = self.inf_dbc.fetch_ways_from_infdb(self.plz)
            ways_count = self.dbc.set_ways_tem_table_infdb(ways_rows)
        else:
            ways_count = self.dbc.set_ways_tem_table(self.plz)
        self.logger.info(f"The ways_tem table filled with {ways_count} ways")

        # Run preprocessing functions that segment roads and connect buildings
        self.dbc.preprocess_ways()
        print("Ways preprocessing completed in ways_tem.")

        # Build pgRouting topology on the processed network
        self.dbc.build_pgr_network_topology()
        print("pgRouting network topology created from ways_tem.")

        self.dbc.update_ways_cost()
        unconn = self.dbc.set_vertice_id()
        self.logger.debug(f"vertice id set, {unconn} buildings with no vertice id")

    def apply_kmeans_clustering(self):
        """
        Find connected components (subgraphs) of an undirected street-graph applying the Depth-First Search algorithm
        to edges and vertices from ways_tem and (if necessary due to their size) apply k-means clustering to these
        street network components.

        FROM: ways_tem, buildings_tem
        INTO: ways_tem, vertices_pgr, buildings_tem
        """

        # Get connected components from the street network
        component, vertices = self.dbc.get_connected_component()
        component_ids = np.unique(component)

        if len(component_ids) > 0:
            # Handle components based on number
            if len(component_ids) > 1:
                # Process multiple connected components
                for i, component_id in enumerate(component_ids):
                    related_vertices = vertices[np.argwhere(component == component_id)]
                    self._process_component_to_kcid(related_vertices, i)
            else:
                # Process single connected component
                self._process_component_to_kcid(vertices)
        else:
            # No components found - issue warning
            warnings.warn("No connected components found in ways_tem table")

        # Verify clustering was successful for all buildings
        no_kmean_count = self.dbc.count_no_kmean_buildings()
        if no_kmean_count not in [0, None]:
            warnings.warn(f"K-means clustering issue: {no_kmean_count} buildings not assigned to clusters")

    def _process_component_to_kcid(self, vertices, component_index=None):
        """Helper method to process components to kcid groups"""
        conn_building_count = self.dbc.count_connected_buildings(vertices)

        if conn_building_count <= 1 or conn_building_count is None:
            # Remove isolated or empty components
            self.dbc.delete_ways(vertices)
            self.dbc.delete_transformers_from_buildings_tem(vertices)
            self.logger.debug("Empty/isolated component removed. Ways and transformers deleted from temporary tables.")
        elif conn_building_count >= LARGE_COMPONENT_LOWER_BOUND:
            # K-means applied to large component to define subgroups with cluster ids
            cluster_count = int(conn_building_count / LARGE_COMPONENT_DIVIDER)
            k_means = KMeans(n_clusters=cluster_count, random_state=K_MEANS_SEED, n_init="auto")
            (selected_vertices, coordinates) = self.dbc.get_connected_component_geometries(vertices)
            kcids = k_means.fit_predict(coordinates) + self.dbc.get_kcid_length() + 1
            self.dbc.update_kmeans_cluster_multiple(selected_vertices, kcids)
            log_msg = f"Large component {component_index} clustered into {cluster_count} groups" if component_index is not None else f"Large component clustered into {cluster_count} groups"
            self.logger.debug(log_msg)
        else:
            # Allocate cluster id for connected component smaller than the building threshold
            self.dbc.update_kmeans_cluster(vertices)

    def position_all_transformers(self):
        """
        Positions all transformers for each bcid cluster (brownfield with existing transformers and greenfield)
        FROM: buildings_tem, grid_result
        INTO: buildings_tem, grid_result
        """
        kcid_length = self.dbc.get_kcid_length()

        for _ in range(kcid_length):
            kcid = self.dbc.get_next_unfinished_kcid(self.plz)
            self.logger.debug(f"working on kcid {kcid}")
            # Building clustering
            # 0. Check for existing transformers from OSM
            transformers = self.dbc.get_included_transformers(kcid)

            # Case 1: No transformers present
            if not transformers:
                self.logger.debug(f"kcid{kcid} has no included transformer")
                # Create greenfield building clusters
                self.create_bcid_for_kcid(self.plz, kcid)
                self.logger.debug(f"kcid{kcid} building clusters finished")

            # Case 2: Transformers present
            else:
                self.logger.debug(f"kcid{kcid} has {len(transformers)} transformers")
                # Create brownfield building clusters with existing transformers
                self.position_brownfield_transformers(self.plz, kcid, transformers)

                # Check buildings and manage clusters
                if self.dbc.count_kmean_cluster_consumers(kcid) > 1:
                    self.create_bcid_for_kcid(self.plz, kcid) #TODO: name should include transformer_size allocation
                else:
                    self.dbc.delete_isolated_building(self.plz, kcid) #TODO: check approach with isolated buildings
                self.logger.debug("rest building cluster finished")

            # Process unfinished clusters
            for bcid in self.dbc.get_greenfield_bcids(self.plz, kcid):
                # Transformer positioning for greenfield clusters
                if bcid >= 0:
                    self.position_greenfield_transformers(self.plz, kcid, bcid)
                    self.logger.debug(f"Transformer positioning for kcid{kcid}, bcid{bcid} finished")
                    self.dbc.update_transformer_rated_power(self.plz, kcid, bcid, 1)
                    self.logger.debug("transformer_rated_power in grid_result is updated.")

    def create_bcid_for_kcid(self, plz: int, kcid: int) -> None:
        """
        Create building clusters (bcids) with average linkage method for a given kcid.
        :param plz: Postal code
        :param kcid: K-means cluster ID
        :return: None
        """
        # Get data needed for clustering
        buildings = self.dbc.get_buildings_from_kcid(kcid)
        consumer_cat_df = self.dbc.get_consumer_categories()
        settlement_type = self.dbc.get_settlement_type_from_plz(plz)
        transformer_capacities, _ = self.dbc.get_transformer_data(settlement_type)
        double_trans = np.multiply(transformer_capacities[2:4], 2)

        # Get distance matrix and prepare for hierarchical clustering
        localid2vid, dist_mat, vid2localid = self.dbc.get_distance_matrix_from_kcid(kcid)
        dist_vector = squareform(dist_mat)

        if len(dist_vector) == 0:
            return

        # Initialize hierarchical clustering
        Z = linkage(dist_vector, method="average")
        valid_cluster_dict = {}
        invalid_trans_cluster_dict = {}
        cluster_amount = 2
        new_localid2vid = localid2vid

        # Iterative clustering process
        while True:
            # Try clustering with current parameters
            invalid_cluster_dict, cluster_dict, _ = self.dbc.try_clustering(Z, cluster_amount, new_localid2vid, buildings,
                                                                        consumer_cat_df, transformer_capacities,
                                                                        double_trans)

            # Process valid clusters
            if cluster_dict:
                current_valid_amount = len(valid_cluster_dict)
                valid_cluster_dict.update({x + current_valid_amount: y for x, y in cluster_dict.items()})
                valid_cluster_dict = dict(enumerate(valid_cluster_dict.values()))  # reindexing the dict with enumerate

            # Process invalid clusters
            if invalid_cluster_dict:
                current_invalid_amount = len(invalid_trans_cluster_dict)
                invalid_trans_cluster_dict.update(
                    {x + current_invalid_amount: y for x, y in invalid_cluster_dict.items()})
                invalid_trans_cluster_dict = dict(enumerate(invalid_trans_cluster_dict.values()))

            # Check if clustering is complete
            if not invalid_trans_cluster_dict:
                self.logger.info(
                    f"Found {len(valid_cluster_dict)} single transformer clusters for PLZ: {plz}, KCID: {kcid}")
                break
            else:
                # Process too large clusters by re-clustering them
                self.logger.info(
                    f"Found {len(invalid_trans_cluster_dict)} too_large clusters for PLZ: {plz}, KCID: {kcid}")

                # Get buildings from the first too-large cluster for re-clustering
                invalid_vertice_ids = list(invalid_trans_cluster_dict[0])
                invalid_local_ids = [vid2localid[v] for v in invalid_vertice_ids]

                # Create new mappings and distance matrix for the subclustering
                new_localid2vid = {k: v for k, v in localid2vid.items() if k in invalid_local_ids}
                new_localid2vid = dict(enumerate(new_localid2vid.values()))
                new_dist_mat = dist_mat[invalid_local_ids][:, invalid_local_ids]
                new_dist_vector = squareform(new_dist_mat)

                # Prepare for next iteration
                Z = linkage(new_dist_vector, method="average")
                cluster_amount = 2
                del invalid_trans_cluster_dict[0]
                invalid_trans_cluster_dict = dict(enumerate(invalid_trans_cluster_dict.values()))

        # At this point, we've successfully found a valid electrical clustering solution with the minimum
        # number of clusters. Each cluster:
        #   1. Contains buildings that can be served by a single transformer
        #   2. Has an appropriately sized transformer assigned
        # The valid_cluster_dict maps building cluster IDs to tuples of (building_vertices_list, optimal_transformer_size)
        # We could calculate the total transformer cost by summing the costs of all selected transformers:
        # total_transformer_cost = sum([transformer2cost[v[1]] for v in valid_cluster_dict.values()])

        # Reorder bcids for consistency
        valid_cluster_dict = self._order_clusters_by_min_vertice(valid_cluster_dict)

        # Save results to database
        self.dbc.clear_grid_result_in_kmean_cluster(plz, kcid)
        for bcid, cluster_data in valid_cluster_dict.items():
            self.dbc.upsert_bcid(plz, kcid, bcid, vertices=cluster_data[0],
                                         transformer_rated_power=cluster_data[1])
        self.logger.debug(f"bcids for plz {plz} kcid {kcid} found...")

    def _order_clusters_by_min_vertice(self, cluster_dict: dict) -> dict:
        """
        Helper function to reassign bcids of the given building clusters ordered by the smallest vertice IDs of the clusters.
        Returns the same result for cluster distributions that are equivalent up to renaming.
        :param cluster_dict: input clusters
        :return: reordered clusters
        """
        ordered_vertices = sorted(cluster_dict.items(), key = lambda cluster: min(cluster[1][0]))
        return {new_bcid: vertices for new_bcid, (_, vertices) in enumerate(ordered_vertices, start=1)}

    def position_brownfield_transformers(self, plz: int, kcid: int, transformer_list: list) -> None:
        """
        Assign buildings to the existing transformers and store them as bcid in buildings_tem.
        Args:
            plz: Postal code
            kcid: K-means cluster ID
            transformer_list: List of transformer IDs
        """
        self.logger.debug(f"{len(transformer_list)} transformers found")

        # Get cost dataframe between consumers and transformers
        cost_df = self.dbc.get_consumer_to_transformer_df(kcid, transformer_list)

        # Filter out connections with distance >= 300
        cost_df = cost_df[cost_df["agg_cost"] < 300].sort_values(by=["agg_cost"])

        # Initialize tracking variables
        pre_result_dict = {transformer_id: [] for transformer_id in transformer_list}
        full_transformer_list = []
        assigned_consumer_list = []

        # Assign consumers to closest transformer
        for _, row in cost_df.iterrows():
            start_consumer_id = row["start_vid"]
            end_transformer_id = row["end_vid"]

            # Skip if consumer already assigned or transformer full
            if start_consumer_id in assigned_consumer_list or end_transformer_id in full_transformer_list:
                continue

            # Try to assign consumer to transformer
            pre_result_dict[end_transformer_id].append(int(start_consumer_id))
            sim_load = self.dbc.calculate_sim_load(pre_result_dict[end_transformer_id])

            # Check if transformer capacity exceeded
            if float(sim_load) >= 630:
                # Remove consumer and mark transformer as full
                pre_result_dict[end_transformer_id].pop()
                full_transformer_list.append(end_transformer_id)

                # Exit if all transformers are full
                if len(full_transformer_list) == len(transformer_list):
                    self.logger.info("All transformers full")
                    break
            else:
                # Mark consumer as assigned
                assigned_consumer_list.append(start_consumer_id)

        self.logger.debug("Transformer selection finished")

        # Create building clusters for each transformer
        building_cluster_count = 0
        for transformer_id in transformer_list:
            # Skip empty transformers
            if not pre_result_dict[transformer_id]:
                self.logger.debug(f"Transformer {transformer_id} has no assigned consumer, deleted")
                self.dbc.delete_transformers_from_buildings_tem([transformer_id])
                continue

            # Create building cluster with sequential negative ID
            building_cluster_count -= 1

            # Calculate the simulated load for all loads assigned to this transformer
            sim_load = self.dbc.calculate_sim_load(pre_result_dict[transformer_id])

            # Define the available standard transformer sizes in kVA
            possible_transformers = np.array([100, 160, 250, 400, 630])  # TODO: check with settlement_type approach

            # Select the smallest transformer that is larger than the simulated load
            transformer_rated_power = possible_transformers[possible_transformers > float(sim_load)][0].item()

            # Update database with new building cluster
            self.dbc.update_building_cluster(transformer_id, pre_result_dict[transformer_id], building_cluster_count, kcid,
                plz, transformer_rated_power)

        self.logger.debug("Brownfield clusters completed")

    def position_greenfield_transformers(self, plz, kcid, bcid):
        """
        Positions a transformer at the optimal location for a greenfield building cluster.

        The optimal location minimizes the sum of distance*load from each vertex to others.

        Args:
            plz: Postcode
            kcid: Kmeans cluster ID
            bcid: Building cluster ID
        """
        # Get all connection points in the building cluster
        connection_points = self.dbc.get_building_connection_points_from_bc(kcid, bcid)

        # If there's only one connection point, use it
        if len(connection_points) == 1:
            self.dbc.upsert_transformer_selection(plz, kcid, bcid, connection_points[0])
            return

        # Get distance matrix between all connection points
        localid2vid, dist_mat, _ = self.dbc.get_distance_matrix_from_bcid(kcid, bcid)

        # Get load vector for each connection point
        loads = self.dbc.generate_load_vector(kcid, bcid)

        # Calculate weighted distance (distance * load) for each potential location
        total_load_per_vertice = dist_mat.dot(loads)

        # Select the point with minimum weighted distance as transformer location
        min_localid = np.argmin(total_load_per_vertice)
        ont_connection_id = int(localid2vid[min_localid])

        # Update the database with the selected transformer position
        self.dbc.upsert_transformer_selection(plz, kcid, bcid, ont_connection_id)

    def install_cables(self):
        """
        Installs electrical cables to connect buildings and transformers in power grid clusters.

        This method creates a pandapower network for each building cluster (kcid, bcid) in the
        postal code area and connects the buildings with appropriate electrical cables. It follows
        a branch-by-branch approach, starting from the furthest nodes and working inward toward
        the transformer.

        The algorithm works as follows:
        1. Retrieves all clusters (kcid, bcid) for the postal code area
        2. For each cluster:
           a. Prepares building and connection data
           b. Creates an electrical network with pandapower
           c. Adds buses, transformers, and loads to the network
           d. Installs cables using a greedy algorithm that:
              - Starts from the furthest nodes from the transformer
              - Creates branches with maximum possible load
              - Selects minimum size cables that can handle the current
              - Connects branches back to transformer
        3. Tracks progress and saves the network configurations

        The cable installation prioritizes cost efficiency while ensuring the electrical
        requirements are met for each branch of the distribution network.

        Returns:
            None
        """
        # Get all clusters for the postal code area
        cluster_list = self.dbc.get_list_from_plz(self.plz)
        ci_count = 0
        ci_process = 0
        main_street_available_cables = CABLE_COST_DICT.keys()

        for id in cluster_list:
            kcid, bcid = id
            self.logger.debug(f"working on kcid {kcid}, bcid {bcid}")

            # Get data for this cluster
            vertices_dict, ont_vertice, vertices_list, buildings_df, consumer_df, consumer_list, connection_nodes = (
                self.prepare_vertices_list(self.plz, kcid, bcid)
            )
            Pd, load_units, load_type = self.get_consumer_simultaneous_load_dict(consumer_list, buildings_df)
            local_length_dict = {c: 0 for c in CABLE_COST_DICT.keys()}

            # Create network and add components
            net = pp.create_empty_network()
            self.dbc.create_cable_std_type(net)
            self.create_lvmv_bus(self.plz, kcid, bcid, net)
            self.create_transformer(self.plz, kcid, bcid, net)
            self.create_connection_bus(connection_nodes, net)
            self.create_consumer_bus_and_load(consumer_list, load_units, net, load_type, buildings_df)

            # Install cables branch by branch
            branch_deviation = 0
            connection_node_list = connection_nodes

            while connection_node_list:
                # Handle single remaining node case
                if len(connection_node_list) == 1:
                    sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, connection_node_list)
                    Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))

                    # Install consumer cables
                    local_length_dict = self.install_consumer_cables(
                        self.plz, bcid, kcid, branch_deviation, connection_node_list,
                        ont_vertice, vertices_dict, Pd, net, CONNECTION_AVAILABLE_CABLES, local_length_dict,
                    )

                    # Connect to transformer
                    if connection_node_list[0] == ont_vertice:
                        cable, count = self.find_minimal_available_cable(Imax, net, main_street_available_cables)
                        self.create_line_ont_to_lv_bus(
                            self.plz, bcid, kcid, connection_node_list[0], branch_deviation, net, cable, count
                        )
                    else:
                        cable, count = self.find_minimal_available_cable(
                            Imax, net, main_street_available_cables, vertices_dict[connection_nodes[0]]
                        )
                        length = self.create_line_start_to_lv_bus(
                            self.plz, bcid, kcid, connection_node_list[0], branch_deviation,
                            net, vertices_dict, cable, count, ont_vertice
                        )
                        local_length_dict[cable] += length

                    self.deviate_bus_geodata(connection_node_list, branch_deviation, net)
                    self.logger.debug("main street cable installation finished")
                    break

                # Process multiple nodes as branches
                furthest_node_path_list = self.find_furthest_node_path_list(
                    connection_node_list, vertices_dict, ont_vertice
                )
                branch_node_list, Imax = self.determine_maximum_load_branch(
                    furthest_node_path_list, buildings_df, consumer_df
                )

                # Install cables for this branch
                local_length_dict = self.install_consumer_cables(
                    self.plz, bcid, kcid, branch_deviation, branch_node_list,
                    ont_vertice, vertices_dict, Pd, net, CONNECTION_AVAILABLE_CABLES, local_length_dict
                )

                # Select appropriate cable and connect nodes
                branch_distance = vertices_dict[branch_node_list[0]]
                cable, count = self.find_minimal_available_cable(
                    Imax, net, main_street_available_cables, branch_distance
                )

                if len(branch_node_list) >= 2:
                    local_length_dict = self.create_line_node_to_node(
                        self.plz, kcid, bcid, branch_node_list, branch_deviation,
                        vertices_dict, local_length_dict, cable, ont_vertice, count, net
                    )

                # Connect branch to transformer
                branch_start_node = branch_node_list[-1]
                if branch_start_node == ont_vertice:
                    self.create_line_ont_to_lv_bus(
                        self.plz, bcid, kcid, branch_start_node, branch_deviation, net, cable, count
                    )
                else:
                    length = self.create_line_start_to_lv_bus(
                        self.plz, bcid, kcid, branch_start_node, branch_deviation,
                        net, vertices_dict, cable, count, ont_vertice
                    )
                    local_length_dict[cable] += length

                # Update processed nodes and visualization
                for vertice in branch_node_list:
                    connection_node_list.remove(vertice)

                self.deviate_bus_geodata(branch_node_list, branch_deviation, net)
                branch_deviation += 1

            # Track and report progress
            ci_count += 1
            progress_increment = 10  # Report progress in 10% increments
            progress_threshold = max(1, len(cluster_list) / progress_increment)

            if ci_count >= progress_threshold:
                ci_process += progress_increment
                ci_count = 0
                self.logger.info(
                    f"Cable installation: {min(ci_process, 100)}% complete ({ci_process // progress_increment}/{progress_increment})")

            self.save_net(net, kcid, bcid)

    def prepare_vertices_list(self, plz: int, kcid: int, bcid: int) -> tuple[
        dict, int, list, pd.DataFrame, pd.DataFrame, list, list]:
        vertices_dict, ont_vertice = self.dbc.get_vertices_from_bcid(plz, kcid, bcid)
        vertices_list = list(vertices_dict.keys())

        buildings_df = self.dbc.get_buildings_from_bcid(plz, kcid, bcid)
        consumer_df = self.dbc.get_consumer_categories()
        consumer_list = buildings_df.vertice_id.to_list()
        consumer_list = list(dict.fromkeys(consumer_list))  # removing duplicates

        connection_nodes = [i for i in vertices_list if i not in consumer_list]

        return (vertices_dict, ont_vertice, vertices_list, buildings_df, consumer_df, consumer_list, connection_nodes,)

    def get_consumer_simultaneous_load_dict(self, consumer_list: list, buildings_df: pd.DataFrame) -> tuple[
        dict, dict, dict]:
        Pd = {consumer: 0 for consumer in consumer_list}  # dict of all vertices in bc, 0 as default
        load_units = {consumer: 0 for consumer in consumer_list}
        load_type = {consumer: "SFH" for consumer in consumer_list}

        for row in buildings_df.itertuples():
            load_units[row.vertice_id] = row.households_per_building
            load_type[row.vertice_id] = row.type
            gzf = CONSUMER_CATEGORIES.loc[CONSUMER_CATEGORIES.definition == row.type, "sim_factor"].item()

            # Determine simultaneous load of each building in MW
            Pd[row.vertice_id] = utils.oneSimultaneousLoad(row.peak_load_in_kw * 1e-3, row.households_per_building, gzf)

        return Pd, load_units, load_type

    def create_lvmv_bus(self, plz: int, kcid: int, bcid: int, net: pp.pandapowerNet) -> None:
        geodata = self.dbc.get_ont_geom_from_bcid(plz, kcid, bcid)

        pp.create_bus(net, name="LVbus 1", vn_kv=VN * 1e-3, geodata=geodata, max_vm_pu=V_BAND_HIGH,
            min_vm_pu=V_BAND_LOW, type="n", )

        # medium voltage external network and mvbus
        mv_data = (float(geodata[0]), float(geodata[1]) + 1.5 * 1e-4)
        mv_bus = pp.create_bus(net, name="MVbus 1", vn_kv=20, geodata=mv_data, max_vm_pu=V_BAND_HIGH,
            min_vm_pu=V_BAND_LOW, type="n", )
        pp.create_ext_grid(net, bus=mv_bus, vm_pu=1, name="External grid")

        return None

    def create_transformer(self, plz: int, kcid: int, bcid: int, net: pp.pandapowerNet) -> None:
        transformer_rated_power = self.dbc.get_transformer_rated_power_from_bcid(plz, kcid, bcid)
        if transformer_rated_power in (250, 400, 630):
            trafo_name = f"{str(transformer_rated_power)} transformer"
            trafo_std = f"{str(transformer_rated_power * 1e-3)} MVA 20/0.4 kV"
            parallel = 1
        elif transformer_rated_power in (100, 160):
            trafo_name = f"{str(transformer_rated_power)} transformer"
            trafo_std = "0.25 MVA 20/0.4 kV"
            parallel = 1
        elif transformer_rated_power in (500, 800):
            trafo_name = f"{str(transformer_rated_power * 0.5)} transformer"
            trafo_std = f"{str(transformer_rated_power * 1e-3 * 0.5)} MVA 20/0.4 kV"
            parallel = 2
        else:
            trafo_name = "630 transformer"
            trafo_std = "0.63 MVA 20/0.4 kV"
            parallel = transformer_rated_power / 630
        trafo_index = pp.create_transformer(net, pp.get_element_index(net, "bus", "MVbus 1"),
            pp.get_element_index(net, "bus", "LVbus 1"), name=trafo_name, std_type=trafo_std, tap_pos=0,
            parallel=parallel, )
        net.trafo.at[trafo_index, "sn_mva"] = transformer_rated_power * 1e-3
        return None

    def create_connection_bus(self, connection_nodes: list, net: pp.pandapowerNet):
        for i in range(len(connection_nodes)):
            node_geodata = self.dbc.get_node_geom(connection_nodes[i])
            pp.create_bus(net, name=f"Connection Nodebus {connection_nodes[i]}", vn_kv=VN * 1e-3, geodata=node_geodata,
                max_vm_pu=V_BAND_HIGH, min_vm_pu=V_BAND_LOW, type="n", )

    def create_consumer_bus_and_load(self, consumer_list: list, load_units: dict, net: pp.pandapowerNet,
            load_type: dict, building_df: pd.DataFrame) -> None:
        for i in range(len(consumer_list)):
            node_geodata = self.dbc.get_node_geom(consumer_list[i])

            ltype = load_type[consumer_list[i]]

            if ltype in ["SFH", "MFH", "AB", "TH"]:
                peak_load = CONSUMER_CATEGORIES.loc[CONSUMER_CATEGORIES["definition"] == ltype, "peak_load"].values[0]
            else:
                peak_load = building_df[building_df["vertice_id"] == consumer_list[i]]["peak_load_in_kw"].tolist()[0]

            pp.create_bus(net=net, name=f"Consumer Nodebus {consumer_list[i]}", vn_kv=VN * 1e-3, geodata=node_geodata,
                max_vm_pu=V_BAND_HIGH, min_vm_pu=V_BAND_LOW, type="n", zone=ltype, )
            for j in range(1, load_units[consumer_list[i]] + 1):
                pp.create_load(net=net, bus=pp.get_element_index(net, "bus", f"Consumer Nodebus {consumer_list[i]}"),
                    p_mw=0, name=f"Load {consumer_list[i]} household {j}", max_p_mw=peak_load * 1e-3, )

    def install_consumer_cables(self, plz: int, bcid: int, kcid: int, branch_deviation: float, branch_node_list: list,
            ont_vertice: int, vertices_dict: dict, Pd: dict, net: pp.pandapowerNet,
            connection_available_cables: list[str], local_length_dict: dict, ) -> dict:
        # lines
        # first draw house connections from consumer node to corresponding connection node
        consumer_list = self.dbc.get_vertices_from_connection_points(branch_node_list)
        branch_consumer_list = [n for n in consumer_list if n in vertices_dict.keys()]
        for vertice in branch_consumer_list:  # TODO: looping for duplicate vertices
            path_list = self.dbc.get_path_to_bus(vertice, ont_vertice)
            start_vid = path_list[1]
            end_vid = path_list[0]

            geodata = self.dbc.get_node_geom(start_vid)
            start_node_geodata = (float(geodata[0]) + 5 * 1e-6 * branch_deviation,
                                  float(geodata[1]) + 5 * 1e-6 * branch_deviation,)

            end_node_geodata = self.dbc.get_node_geom(end_vid)

            line_geodata = [start_node_geodata, end_node_geodata]

            cost_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3

            count = 1
            sim_load = Pd[end_vid]  # power in Watt
            Imax = sim_load * 1e-3 / (VN * V_BAND_LOW * np.sqrt(3))  # current in kA
            voltage_available_cables_df = None
            while True:
                line_df = pd.DataFrame.from_dict(net.std_types["line"], orient="index")
                current_available_cables_df = line_df[
                    (line_df["max_i_ka"] >= Imax / count) & (line_df.index.isin(connection_available_cables))]

                if len(current_available_cables_df) == 0:
                    count += 1
                    continue

                current_available_cables_df["cable_impedence"] = np.sqrt(
                    current_available_cables_df["r_ohm_per_km"] ** 2 + current_available_cables_df[
                        "x_ohm_per_km"] ** 2)  # impedence in ohm / km
                if sim_load <= 100:
                    voltage_available_cables_df = current_available_cables_df[
                        current_available_cables_df["cable_impedence"] <= 2 * 1e-3 / (Imax * cost_km / count)]
                else:
                    voltage_available_cables_df = current_available_cables_df[
                        current_available_cables_df["cable_impedence"] <= 4 * 1e-3 / (Imax * cost_km / count)]

                if len(voltage_available_cables_df) == 0:
                    count += 1
                    continue
                else:
                    break

            cable = voltage_available_cables_df.sort_values(by=["q_mm2"]).index.tolist()[0]
            local_length_dict[cable] += count * cost_km

            pp.create_line(net, from_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {start_vid}"),
                to_bus=pp.get_element_index(net, "bus", f"Consumer Nodebus {end_vid}"), length_km=cost_km,
                std_type=cable, name=f"Line to {end_vid}", geodata=line_geodata, parallel=count, )

            self.dbc.insert_lines(geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=f"Line to {end_vid}",
                              std_type=cable,
                              from_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {start_vid}"),
                              to_bus=pp.get_element_index(net, "bus", f"Consumer Nodebus {end_vid}"), length_km=cost_km)

        return local_length_dict

    def find_minimal_available_cable(self, Imax: float, net: pp.pandapowerNet, cables_list: list, distance: int = 0) -> \
    tuple[str, int]:
        count = 1
        cable = None
        while 1:
            line_df = pd.DataFrame.from_dict(net.std_types["line"], orient="index")
            current_available_cables = line_df[
                (line_df.index.isin(cables_list)) & (line_df["max_i_ka"] >= Imax / count)]
            if len(current_available_cables) == 0:
                count += 1
                continue

            if distance != 0:
                current_available_cables["cable_impedence"] = np.sqrt(
                    current_available_cables["r_ohm_per_km"] ** 2 + current_available_cables[
                        "x_ohm_per_km"] ** 2)  # impedence in ohm / km
                voltage_available_cables = current_available_cables[
                    current_available_cables["cable_impedence"] <= 400 * 0.045 / (Imax * distance / count)]
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

    def create_line_ont_to_lv_bus(self, plz: int, bcid: int, kcid: int, branch_start_node: int, branch_deviation: float,
            net: pp.pandapowerNet, cable: str, count: int):  # TODO: check if this line is required
        end_vid = branch_start_node
        node_geodata = self.dbc.get_node_geom(end_vid)
        node_geodata = (float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                        float(node_geodata[1]) + 5 * 1e-6 * branch_deviation,)
        lvbus_geodata = (
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "x"] + 5 * 1e-6 * branch_deviation,
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "y"],)
        line_geodata = [lvbus_geodata, node_geodata]

        cost_km = 0
        pp.create_line(net, from_bus=pp.get_element_index(net, "bus", "LVbus 1"),
            to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {end_vid}"), length_km=cost_km, std_type=cable,
            name=f"Line to {end_vid}", geodata=line_geodata, parallel=count, )

        self.dbc.insert_lines(geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=f"Line to {end_vid}",
            std_type=cable, from_bus=pp.get_element_index(net, "bus", "LVbus 1"),
            to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {end_vid}"), length_km=cost_km)

    def create_line_start_to_lv_bus(self, plz: int, bcid: int, kcid: int, branch_start_node: int,
            branch_deviation: float, net: pp.pandapowerNet, vertices_dict: dict, cable: str, count: int,
            ont_vertice: int, ) -> int:

        node_path_list = self.dbc.get_path_to_bus(branch_start_node, ont_vertice)

        line_geodata = []
        for p in node_path_list:
            node_geodata = self.dbc.get_node_geom(p)
            node_geodata = (float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                            float(node_geodata[1]) + 5 * 1e-6 * branch_deviation,)
            line_geodata.append(node_geodata)
        lvbus_geodata = (
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "x"] + 5 * 1e-6 * branch_deviation,
            net.bus_geodata.loc[pp.get_element_index(net, "bus", "LVbus 1"), "y"],)
        line_geodata.append(lvbus_geodata)
        line_geodata.reverse()

        cost_km = vertices_dict[branch_start_node] * 1e-3
        length = count * cost_km  # distance in m
        pp.create_line(net, from_bus=pp.get_element_index(net, "bus", "LVbus 1"),
            to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {branch_start_node}"), length_km=cost_km,
            std_type=cable, name=f"Line to {branch_start_node}", geodata=line_geodata, parallel=count, )

        self.dbc.insert_lines(geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=f"Line to {branch_start_node}",
                          std_type=cable, from_bus=pp.get_element_index(net, "bus", "LVbus 1"),
                          to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {branch_start_node}"),
                          length_km=cost_km)

        return length




    def deviate_bus_geodata(self, branch_node_list: list, branch_deviation: float, net: pp.pandapowerNet):
        for node in branch_node_list:
            net.bus_geodata.at[pp.get_element_index(net, "bus", f"Connection Nodebus {node}"), "x"] += (
                        5 * 1e-6 * branch_deviation)
            net.bus_geodata.at[pp.get_element_index(net, "bus", f"Connection Nodebus {node}"), "y"] += (
                        5 * 1e-6 * branch_deviation)

    def find_furthest_node_path_list(self, connection_node_list: list, vertices_dict: dict, ont_vertice: int) -> list:
        connection_node_dict = {n: vertices_dict[n] for n in connection_node_list}
        furthest_node = max(connection_node_dict, key=connection_node_dict.get)
        # all the connection nodes in the path from transformer to furthest node are considered as potential branch loads
        furthest_node_path_list = self.dbc.get_path_to_bus(furthest_node, ont_vertice)
        furthest_node_path = [p for p in furthest_node_path_list if p in connection_node_list]

        return furthest_node_path


    def determine_maximum_load_branch(self, furthest_node_path_list: list, buildings_df: pd.DataFrame,
            consumer_df: pd.DataFrame) -> tuple[list, float]:
        # TOD O explanation?
        branch_node_list = []
        for node in furthest_node_path_list:
            branch_node_list.append(node)
            sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, branch_node_list)  # sim_peak load in kW
            Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))  # current in kA
            if Imax >= 0.313 and len(
                    branch_node_list) > 1:  # 0.313 is the current limit of the largest allowed cable 4x185SE
                branch_node_list.remove(node)
                break
            elif Imax >= 0.313 and len(branch_node_list) == 1:
                break
        sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, branch_node_list)
        Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))

        return branch_node_list, Imax

    def create_line_node_to_node(self, plz: int, kcid: int, bcid: int, branch_node_list: list, branch_deviation: float,
            vertices_dict: dict, local_length_dict: dict, cable: str, ont_vertice: int, count: float,
            net: pp.pandapowerNet) -> dict:
        """creates the lines / cables from one Connection Nodebus to the next. Adds them to the pandapower network
        and lines result table"""
        for i in range(len(branch_node_list) - 1):
            # to get the line geodata, we now need to consider all the nodes in database, not only connection points
            node_path_list = self.dbc.get_path_to_bus(branch_node_list[i], ont_vertice)  # gets the path along ways_result
            # end at next connection point
            if branch_node_list[i + 1] not in node_path_list:  # if next node of branch node list not in node path list
                self.logger.debug(f"creating line to node i + 1: {i + 1} node: {branch_node_list[i + 1]}")
                node_path_list = self.dbc.get_path_to_bus(branch_node_list[i], branch_node_list[
                    i + 1])  # node_path_list = [branch_node_list[i], branch_node_list[i + 1]]  # intermediate nodes up to next connection nodebus are neglected  # the cable will directly connect to next connection nodebus

            node_path_list = node_path_list[: node_path_list.index(
                branch_node_list[i + 1]) + 1]  # the node path list goes up to the index (branch_node_list[i + 1]) +1
            node_path_list.reverse()  # to keep the correct direction

            start_vid = node_path_list[0]
            end_vid = node_path_list[-1]

            line_geodata = []
            for p in node_path_list:
                node_geodata = self.dbc.get_node_geom(p)
                node_geodata = (float(node_geodata[0]) + 5 * 1e-6 * branch_deviation,
                                float(node_geodata[1]) + 5 * 1e-6 * branch_deviation,)
                line_geodata.append(node_geodata)

            cost_km = (vertices_dict[end_vid] - vertices_dict[start_vid]) * 1e-3

            local_length_dict[cable] += count * cost_km
            pp.create_line(net, from_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {start_vid}"),
                to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {end_vid}"), length_km=cost_km,
                std_type=cable, name=f"Line to {end_vid}", geodata=line_geodata, parallel=count, )

            self.dbc.insert_lines(geom=line_geodata, plz=plz, bcid=bcid, kcid=kcid, line_name=f"Line to {end_vid}",
                std_type=cable, from_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {start_vid}"),
                to_bus=pp.get_element_index(net, "bus", f"Connection Nodebus {end_vid}"), length_km=cost_km)
        return local_length_dict

    def save_net(self, net, kcid, bcid):
        """
        Save one grid to file and to database
        """
        if SAVE_GRID_FOLDER:
            savepath_folder = Path(RESULT_DIR, "grids", f"version_{VERSION_ID}", str(self.plz))
            savepath_folder.mkdir(parents=True, exist_ok=True)
            filename = f"kcid{kcid}bcid{bcid}.json"
            savepath_file = Path(savepath_folder, filename)
            pp.to_json(net, filename=savepath_file)

        json_string = pp.to_json(net, filename=None)

        self.dbc.save_pp_net_with_json(self.plz, kcid, bcid, json_string)

        self.logger.info(f"Grid with kcid:{kcid} bcid:{bcid} is stored. ")