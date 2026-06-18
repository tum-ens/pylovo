import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd  # type: ignore
import pandapower as pp
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform
from sklearn.cluster import KMeans
from concurrent.futures import ProcessPoolExecutor, as_completed  # lightweight parallel execution

import pylovo.database.database_client as dbc
from pylovo.infdb.infdb_client import InfdbClient
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo import utils
from pylovo.config_loader import *

# Import electrical backend components
from pylovo.electrical_backend import IElectricalBackend, create_backend
from pylovo.cable_installer import CableInstaller

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
        self.logger = utils.create_logger(
            name="GridGenerator", log_file=kwargs.get("log_file", "log.txt"), log_level=LOG_LEVEL
        )
        self.inf_dbc = None
        if USE_INFDB:
            self.inf_dbc = InfdbClient()

    def __del__(self):
        self.dbc.__del__()

    def generate_grid_for_single_plz(
        self, plz: int, analyze_grids: bool = False, refresh_mv: bool = True
    ) -> None:
        """Generates the grid for a single PLZ.

        :param plz: Postal code for which the grid should be generated.
        :type plz: int
        :param analyze_grids: Option to analyze the results after grid generation, defaults to False.
        :type analyze_grids: bool
        :param refresh_mv: Refresh materialized views after processing, defaults to True.
        :type refresh_mv: bool
        """
        self.plz = plz
        print('-------------------- start', self.plz, '---------------------------')
        self.dbc.create_temp_tables(plz)  # create PLZ-suffixed temp tables
        # self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work

        interrupted = False
        try:
            self.generate_grid()
            self.dbc.save_tables(plz=self.plz)  # Save data from temporary tables to result tables
            self.dbc.commit_changes()
            if analyze_grids:
                pc = ParameterCalculator()
                pc.analyze_parameters_for_plz(plz)
                self.dbc.commit_changes()  # commit the changes to the database
        except ResultExistsError:
            self.dbc.logger.info(f"Grid for the postcode area {plz} has already been generated.")
        except KeyboardInterrupt:
            interrupted = True
            self.logger.warning(f"Grid generation interrupted by user for PLZ {self.plz}.")
            self.dbc.rollback_changes()
        except Exception as e:
            self.logger.error(f"Error during grid generation for PLZ {self.plz}: {e}")
            self.logger.info(f"Skipped PLZ {self.plz} due to generation error.")
            self.dbc.rollback_changes()
            try:
                self.dbc.delete_plz_from_sample_set_table(str(CLASSIFICATION_VERSION), self.plz)
            except Exception as cleanup_error:
                self.logger.warning(
                    f"Failed to remove PLZ {self.plz} from the sample set after generation error: {cleanup_error}"
                )
            traceback.print_exc()
        finally:
            # Always clean up temporary tables, even if there was an error.
            # Roll back first so cleanup can run after SQL errors/interrupts.
            self.dbc.rollback_changes()

            try:
                self.dbc.drop_temp_tables(plz)  # drop PLZ-suffixed temp tables
                # Commit cleanup so dropped tables don't reappear after interruption.
                self.dbc.commit_changes()
            except Exception as cleanup_error:
                self.logger.error(
                    f"Failed to clean up PLZ-specific temporary tables for PLZ {plz}: {cleanup_error}"
                )

        if interrupted:
            raise KeyboardInterrupt("Grid generation interrupted by user")

        if refresh_mv:
            # update the materialized views to reflect changes in their base tables
            self.dbc.ensure_connection()
            self.dbc.refresh_materialized_views()
            self.dbc.commit_changes()
        else:
            self.dbc.commit_changes()  # commit the changes to the database
        print('-------------------- end', self.plz, '-----------------------------')

    def generate_grid_for_multiple_plz(
        self, df_plz: pd.DataFrame, analyze_grids: bool = False, parallel: bool = True
    ) -> None:
        """Generate grids for all PLZ entries. Materialized views are refreshed once all grids have been processed.
        :param df_plz: table that contains PLZ for grid generation
        :param analyze_grids: option to analyse the results after grid generation, defaults to False
        :param parallel: optionally use parallel workers, defaults to True
        """
        # One-time cleanup of leftover PLZ-specific temp tables from previously interrupted runs.
        self.dbc.drop_orphaned_plz_temp_tables()
        self.dbc.commit_changes()

        plz_list = [int(row["plz"]) for _, row in df_plz.iterrows()]
        
        # Use parallel processing if:
        # 1. parallel=True AND
        # 2. We have multiple PLZ to process AND  
        # 3. We have more than 1 CPU core available (can't parallelize with 1 core)
        should_use_parallel = parallel and len(plz_list) > 1 and N_JOBS > 1
        
        print(f"🔍 Parallel processing check:")
        print(f"   - parallel parameter: {parallel}")
        print(f"   - Number of PLZ to process: {len(plz_list)}")
        print(f"   - Available CPU cores: {N_JOBS}")
        print(f"   - Will use parallel processing: {should_use_parallel}")
        failed_plz = []
        
        if should_use_parallel:
            # Use parallel processing for multiple PLZ
            # Use up to N_JOBS workers, but not more than the number of PLZ
            max_workers = min(N_JOBS, len(plz_list))
            print(f"   - Using {max_workers} workers for {len(plz_list)} PLZ")
            
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                # Create a dictionary that maps futures to their corresponding PLZ.
                futures = {
                    executor.submit(GridGenerator._worker, plz, analyze_grids): plz
                    for plz in plz_list
                }
                
                completed_count = 0
                total_count = len(plz_list)
                
                try:
                    worker_timeout_minutes = CONFIG_GENERATION.get("WORKER_TIMEOUT_MINUTES", 30)
                    worker_timeout = worker_timeout_minutes * 60  # Convert to seconds
                    for future in as_completed(futures, timeout=worker_timeout):
                        plz = futures[future]
                        completed_count += 1
                        
                        try:
                            # Calling future.result() will raise an exception if the worker process failed.
                            future.result()
                            print(f"✓ Completed PLZ {plz} ({completed_count}/{total_count})")
                        except Exception as exc:
                            # Log the exception to record the failed PLZ without stopping the execution
                            # for other, potentially successful, PLZs.
                            self.logger.error(f"PLZ {plz} generated an exception: {exc}")
                            failed_plz.append(plz)
                            traceback.print_exc()
                            print(f"✗ Failed PLZ {plz} ({completed_count}/{total_count})")
                            
                            # Clean up the failed future to prevent memory leaks
                            try:
                                future.cancel()
                            except Exception:
                                pass
                            
                except KeyboardInterrupt:
                    print(f"\n⚠️  KeyboardInterrupt received. Shutting down gracefully...")
                    print(f"   Completed: {completed_count}/{total_count} PLZ")
                    
                    # Cancel all pending futures
                    for future in futures:
                        future.cancel()
                    
                    # Wait a bit for ongoing processes to finish gracefully
                    print("   Waiting for ongoing processes to finish...")
                    try:
                        # Give processes time to finish gracefully based on config
                        shutdown_timeout = CONFIG_GENERATION.get("GRACEFUL_SHUTDOWN_TIMEOUT", 5)
                        for future in as_completed(futures, timeout=shutdown_timeout):
                            if not future.cancelled():
                                plz = futures[future]
                                try:
                                    future.result()
                                    print(f"✓ Gracefully completed PLZ {plz}")
                                except Exception as exc:
                                    print(f"✗ PLZ {plz} failed during graceful shutdown: {exc}")
                    except Exception:
                        # Timeout or other exception during graceful shutdown
                        pass
                    
                    print("   Shutdown complete.")
                    raise KeyboardInterrupt("Grid generation interrupted by user")
                    
                except Exception as e:
                    print(f"\n❌ Error during parallel processing: {e}")
                    print(f"   Completed: {completed_count}/{total_count} PLZ")
                    
                    # Cancel all pending futures on any error
                    for future in futures:
                        future.cancel()
                    
                    raise
        else:
            for plz in plz_list:
                # defer materialized view refresh until all PLZ are processed
                self.generate_grid_for_single_plz(
                    plz=plz, analyze_grids=analyze_grids, refresh_mv=False
                )

        # refresh materialized views once after all grids have been generated
        try:
            self.dbc.ensure_connection()
            self.dbc.refresh_materialized_views()
            self.dbc.commit_changes()
        except Exception as e:
            self.logger.error(f"Error refreshing materialized views: {e}")
            # Don't re-raise here as individual PLZ processing might have succeeded

        if should_use_parallel:
            if failed_plz:
                failed_plz = sorted(set(failed_plz))
                failed_plz_str = ", ".join(str(plz) for plz in failed_plz)
                summary = f"Parallel grid generation finished with {len(failed_plz)} failed PLZ: {failed_plz_str}"
                print(summary)
                self.logger.warning(summary)
            else:
                summary = "Parallel grid generation finished with no failed PLZ."
                print(summary)
                self.logger.info(summary)

    @staticmethod
    def _worker(plz: int, analyze_grids: bool) -> None:
        """Worker process to generate a grid for a single PLZ."""
        log_file = Path("log") / f"log_{plz}.txt"
        if log_file.exists():
            log_file.unlink()  # Overwrite log file if it exists

        # Create a dedicated GridGenerator instance for this worker
        # This ensures each worker has its own database connection and logger
        gg = None
        try:
            print(f"Worker starting for PLZ {plz}...")
            gg = GridGenerator(log_file=log_file)  # dedicated logger per PLZ
            print(f"Worker initialized for PLZ {plz}")

            # Generate grid with proper error handling
            gg.generate_grid_for_single_plz(
                plz=plz, analyze_grids=analyze_grids, refresh_mv=False
            )

            print(f"Worker completed for PLZ {plz}")

        except Exception as e:
            print(f"Worker failed for PLZ {plz}: {e}")
            import traceback
            traceback.print_exc()

            # Ensure proper cleanup even on failure
            if gg and hasattr(gg, 'dbc') and gg.dbc:
                try:
                    gg.dbc.rollback_changes()
                except Exception as rollback_error:
                    print(f"Rollback error for PLZ {plz}: {rollback_error}")
            raise
        finally:
            # Ensure proper cleanup of database connections
            if gg and hasattr(gg, 'dbc') and gg.dbc:
                try:
                    # Use the close method which handles all connection types
                    gg.dbc.close()
                except Exception as cleanup_error:
                    print(f"Cleanup error for PLZ {plz}: {cleanup_error}")

            # Also ensure GridGenerator cleanup
            if gg:
                try:
                    gg.__del__()
                except Exception as del_error:
                    print(f"GridGenerator cleanup error for PLZ {plz}: {del_error}")

            print(f"Worker cleanup completed for PLZ {plz}")

    def generate_grid(self):
        if self.dbc.is_grid_generated(self.plz):
            raise ResultExistsError(
                f"The grids for the postcode area {self.plz} is already generated "
                f"for the version {VERSION_ID}."
            )
        self.prepare_data_from_config()
        self.prepare_postcodes()
        self.prepare_buildings()
        self.prepare_transformers()
        self.prepare_ways()
        self.apply_kmeans_clustering()
        self.position_all_transformers()
        self.install_cables()

    def prepare_data_from_config(self):
        """
        Load data from config.
        """
        self.dbc.insert_equipment_data_from_config(equipment_data=CONFIG_EQUIPMENT_DATA)
        self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work
        self.dbc.insert_consumer_categories_from_config(consumer_categories=CONSUMER_CATEGORIES)

    def prepare_postcodes(self):
        """
        Caches postcode from raw data tables and stores in temporary tables.
        FROM: postcode (local) or InfDB opendata.postcodes_germany
        INTO: postcode_result

        In USE_INFDB mode, the local postcode table may be empty or stale for new PLZ
        regions added after the initial pylovo-setup run.  To avoid requiring a full
        re-run of setup, the postcode geometry is fetched on-demand from InfDB and
        inserted into the local postcode table before copying to postcode_result.
        """
        if USE_INFDB and not self.dbc.postcode_exists_locally(self.plz):
            postcode_row = self.inf_dbc.fetch_postcode_from_infdb(self.plz)
            if postcode_row is None:
                raise ValueError(
                    f"PLZ {self.plz} not found in InfDB opendata.postcodes_germany. "
                    "Cannot proceed without postcode geometry."
                )
            self.dbc.insert_postcode(postcode_row)
            self.logger.info(f"Missing postcode for plz {self.plz} fetched from InfDB and inserted into local database.")
        self.dbc.copy_postcode_result_table(self.plz)
        self.logger.info(f"Starting grid generation for plz {self.plz}")

    def prepare_buildings(self):
        """
        Caches buildings from raw data tables and stores in temporary tables.
        FROM: res, oth
        INTO: buildings_tem
        """
        if USE_INFDB:
            buildings_data = self.inf_dbc.fetch_buildings_from_infdb(self.plz)
            self.dbc.set_buildings_table(buildings_data, self.plz)
        else:
            self.dbc.set_residential_buildings_table(self.plz)
            self.dbc.set_other_buildings_table(self.plz)
        # self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work
        self.logger.info("Buildings_tem table prepared")
        self.dbc.remove_duplicate_buildings()
        self.logger.info("Duplicate buildings removed from buildings_tem")

        try:
            avg_hh = self.dbc.calculate_avg_households_per_building(self.plz)
            house_dist = self.dbc.calculate_house_distance_metric(self.plz)
            settlement_type = self.dbc.set_settlement_type_per_plz(self.plz, settlement_type_thresholds=
            {"rural_max_households": RURAL_MAX_HOUSEHOLDS,
             "urban_min_households": URBAN_MIN_HOUSEHOLDS,
             "rural_min_distance": RURAL_MIN_BUILDING_DISTANCE,
             "urban_max_distance": URBAN_MAX_BUILDING_DISTANCE})
            self.logger.info(
                f"Settlement type determined (avg_households_per_building={avg_hh:.2f}, house_distance={house_dist:.1f} m, settlement_type={settlement_type})"
            )
        except Exception as e:
            self.logger.warning(f"Settlement type classification failed: {e}")

        unloadcount = self.dbc.set_building_peak_load()
        self.logger.info(
            f"Building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )
        too_large_consumers = self.dbc.update_too_large_consumers_to_zero()
        self.logger.debug(
            f"{too_large_consumers} Commercial/Public consumers assumed MV-direct and excluded from LV modeling"
        )


    def prepare_transformers(self):
        """
        Cache transformers from raw data tables and stores in temporary tables.
        FROM: transformers
        INTO: buildings_tem
        """
        self.dbc.set_buildings_tem_plz(self.plz)
        use_existing_transformers = USE_DSO_TRANSFORMER_POSITIONS or USE_OPEN_TRANSFORMER_POSITIONS
        if use_existing_transformers:
            self.dbc.insert_transformers(
                self.plz,
                include_dso=USE_DSO_TRANSFORMER_POSITIONS,
                include_open=USE_OPEN_TRANSFORMER_POSITIONS,
            )
            self.logger.info(
                "Transformers inserted into buildings_tem table "
                f"(dso={USE_DSO_TRANSFORMER_POSITIONS}, open={USE_OPEN_TRANSFORMER_POSITIONS})"
            )
        else:
            self.logger.info("Existing transformer positions disabled by configuration")
        removed_transformer_buildings = self.dbc.remove_transformer_evidence_buildings_from_buildings_tem(
            include_dso=USE_DSO_TRANSFORMER_POSITIONS,
            include_open=USE_OPEN_TRANSFORMER_POSITIONS,
        )
        self.logger.info(
            f"Removed {removed_transformer_buildings} transformer-evidence buildings from buildings_tem consumer input"
        )
        self.dbc.count_indoor_transformers()
        self.dbc.drop_indoor_transformers()
        self.logger.info("Indoor transformers removed from buildings_tem table")

    def prepare_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ways, buildings_tem
        INTO: ways_tem, buildings_tem, ways_tem_vertices_pgr, ways_tem_
        """
        if USE_INFDB:
            ways_rows = self.inf_dbc.fetch_ways_from_infdb(self.plz)
            ways_count = self.dbc.set_ways_tem_table_infdb(ways_rows, self.plz)
        else:
            ways_count = self.dbc.set_ways_tem_table(self.plz)
        self.logger.info(f"The ways_tem table filled with {ways_count} ways")

        # Run preprocessing functions that segment roads and connect buildings
        self.dbc.preprocess_ways()
        self.logger.info(f"Ways preprocessing completed in ways_tem.")

        # Build pgRouting topology on the processed network
        self.dbc.build_pgr_network_topology(self.plz)
        self.logger.info(f"pgRouting network topology created from ways_tem.")

        self.dbc.update_ways_cost()
        unconn = self.dbc.set_vertice_id()
        self.logger.debug(f"vertice id set, {unconn} buildings with no vertice id")

    def apply_kmeans_clustering(self):
        """
        Find connected components (subgraphs) of an undirected street graph using Depth-First Search algorithm over
        edges and vertices from ways_tem and, if necessary due to their size, apply k-means clustering to these
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
                self.dimension_bcid_for_kcid(self.plz, kcid)
                self.logger.debug(f"kcid{kcid} building clusters finished")

            # Case 2: Transformers present
            else:
                self.logger.debug(f"kcid{kcid} has {len(transformers)} transformers")
                # Create brownfield building clusters with existing transformers
                self.position_brownfield_transformers(self.plz, kcid, transformers)

                # Check buildings and manage clusters
                if self.dbc.count_kmean_cluster_consumers(kcid) > 1:
                    self.dimension_bcid_for_kcid(self.plz, kcid)
                else:
                    self.dbc.delete_isolated_building(self.plz, kcid) #TODO: check approach with isolated buildings
                self.logger.debug("Remaining building clustering finished")

            # Process unfinished clusters
            for bcid in self.dbc.get_greenfield_bcids(self.plz, kcid):
                # Transformer positioning for greenfield clusters
                if bcid >= 0:
                    self.position_greenfield_transformers(self.plz, kcid, bcid)
                    self.logger.debug(f"Transformer positioning for kcid{kcid}, bcid{bcid} finished")
                    self.dbc.update_transformer_rated_power(self.plz, kcid, bcid, 1)
                    self.logger.debug("Transformer_rated_power in grid_result updated.")

    def dimension_bcid_for_kcid(self, plz: int, kcid: int) -> None:
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
        # Use the two largest available transformers
        double_trans = np.multiply(transformer_capacities[-2:], 2)
        self.logger.info(f"Start BCID dimensioning for PLZ {plz}, KCID {kcid}")

        # Get distance matrix and prepare for hierarchical clustering
        localid2vid, dist_mat, vid2localid = self.dbc.get_distance_matrix_from_kcid(kcid)
        dist_vector = squareform(dist_mat)

        if len(dist_vector) == 0:
            self.logger.warning(
                f"Skipped BCID dimensioning for PLZ {plz}, KCID {kcid}: empty distance vector"
            )
            return

        # Initialize hierarchical clustering
        Z = linkage(dist_vector, method="average")
        valid_cluster_dict = {}
        invalid_trans_cluster_dict = {}
        cluster_amount = 2
        new_localid2vid = localid2vid
        reclustering_iterations = 0

        # Iterative clustering process
        while True:
            reclustering_iterations += 1
            # Try clustering with current parameters
            invalid_cluster_dict, cluster_dict, _ = self.dbc.load_constrained_hierarchical_clustering(
                Z,
                cluster_amount,
                new_localid2vid,
                buildings,
                consumer_cat_df,
                transformer_capacities,
                double_trans,
                dist_mat=new_dist_mat if reclustering_iterations > 1 else dist_mat,
                vid2localid={value: key for key, value in new_localid2vid.items()},
                max_transformer_distance=MAX_GREENFIELD_TRAFO_DISTANCE,
            )

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
                    f"BCID dimensioning complete for PLZ {plz}, KCID {kcid}: "
                    f"{len(valid_cluster_dict)} single-transformer clusters, "
                    f"cluster_split_iterations={reclustering_iterations}"
                )
                break
            else:
                # Process too-large clusters by re-clustering them.
                # This value can go up and down while invalid clusters are split iteratively.
                pending_oversized = len(invalid_trans_cluster_dict)
                self.logger.debug(
                    f"BCID dimensioning progress for PLZ {plz}, KCID {kcid}: "
                    f"iteration={reclustering_iterations}, pending_oversized={pending_oversized}, "
                    f"accepted_clusters={len(valid_cluster_dict)}"
                )

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

        # At this point, a valid clustering solution (minimum number of transformers) was found.
        # The valid_cluster_dict maps building cluster IDs to tuples of (building_vertices_list, optimal_transformer_size)
        # Each cluster 1) Contains buildings that can be supplied by a single transformer and 2) has an appropriately sized 
        # transformer assigned. The hierarchical split procedure guarantees feasibility, but not minimality of the resulting 
        # feasible partition as the splitting is iterative and path-dependent. 
        # Therefore we add a conservative local merge step to test whether neighboring feasible clusters can be 
        # recombined without violating the same load and distance constraints.
        valid_cluster_dict = self._merge_feasible_greenfield_clusters(
            valid_cluster_dict,
            buildings,
            consumer_cat_df,
            transformer_capacities,
            dist_mat,
            vid2localid,
            plz,
            kcid,
        )

        # Reorder bcids for consistency
        valid_cluster_dict = self._order_clusters_by_min_vertice(valid_cluster_dict)

        # Save results to database
        self.dbc.clear_grid_result_in_kmean_cluster(plz, kcid)
        for bcid, cluster_data in valid_cluster_dict.items():
            self.dbc.upsert_bcid(plz, kcid, bcid, vertices=cluster_data[0],
                                         transformer_rated_power=cluster_data[1])

        self.logger.debug(f"bcids for plz {plz} kcid {kcid} found...")

    def _merge_feasible_greenfield_clusters(
        self,
        cluster_dict: dict,
        buildings: pd.DataFrame,
        consumer_cat_df: pd.DataFrame,
        transformer_capacities: np.ndarray,
        dist_mat: np.ndarray,
        vid2localid: dict[int, int],
        plz: int,
        kcid: int,
    ) -> dict:
        """Merge neighboring undersized greenfield clusters if still feasible.

        The load-constrained hierarchical split prevents oversized transformer
        areas. This conservative pass only recombines already valid neighboring
        clusters when the merged area still fits a configured single transformer
        and satisfies the existing greenfield distance limit.
        """
        if not MERGE_GREENFIELD_CLUSTERS or len(cluster_dict) <= 1:
            return cluster_dict

        configured_merge_capacities = np.asarray(GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA, dtype=float)
        merge_capacities = transformer_capacities[
            np.isin(transformer_capacities, configured_merge_capacities)
        ]
        if len(merge_capacities) == 0:
            self.logger.warning(
                "Greenfield cluster merging skipped: none of the configured merge capacities "
                f"{GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA} kVA are available for this settlement type."
            )
            return cluster_dict

        merged_clusters = {
            cluster_id: (list(vertices), transformer_size)
            for cluster_id, (vertices, transformer_size) in cluster_dict.items()
        }
        merge_count = 0

        while True:
            best_candidate = None
            cluster_items = list(merged_clusters.items())

            for left_index in range(len(cluster_items)):
                left_id, (left_vertices, _left_transformer) = cluster_items[left_index]
                left_local_ids = [vid2localid[vid] for vid in left_vertices if vid in vid2localid]
                if not left_local_ids:
                    continue

                for right_id, (right_vertices, _right_transformer) in cluster_items[left_index + 1:]:
                    right_local_ids = [vid2localid[vid] for vid in right_vertices if vid in vid2localid]
                    if not right_local_ids:
                        continue

                    combined_vertices = list(dict.fromkeys(left_vertices + right_vertices))
                    combined_load = utils.simultaneousPeakLoad(buildings, consumer_cat_df, combined_vertices)
                    feasible_capacities = merge_capacities[merge_capacities > combined_load]
                    if len(feasible_capacities) == 0:
                        continue

                    if not self.dbc.cluster_has_feasible_transformer_position(
                        combined_vertices,
                        dist_mat,
                        vid2localid,
                        MAX_GREENFIELD_TRAFO_DISTANCE,
                    ):
                        continue

                    nearest_distance = float(
                        dist_mat[np.ix_(left_local_ids, right_local_ids)].min()
                    )
                    candidate = (
                        nearest_distance,
                        combined_load,
                        int(feasible_capacities[0]),
                        left_id,
                        right_id,
                        combined_vertices,
                    )
                    if best_candidate is None or candidate[:2] < best_candidate[:2]:
                        best_candidate = candidate

            if best_candidate is None:
                break

            _nearest_distance, _combined_load, transformer_size, left_id, right_id, combined_vertices = best_candidate
            merged_clusters[left_id] = (combined_vertices, transformer_size)
            del merged_clusters[right_id]
            merge_count += 1

        if merge_count:
            self.logger.info(
                f"Greenfield cluster merge complete for PLZ {plz}, KCID {kcid}: "
                f"merged {merge_count} neighboring cluster pairs, final_clusters={len(merged_clusters)}"
            )

        return dict(enumerate(merged_clusters.values()))

    def _order_clusters_by_min_vertice(self, cluster_dict: dict) -> dict:
        """
        Helper to reassign bcids based on smallest vertex ID of each cluster
        for consistent ordering across equivalent partitions.
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
        self.logger.info(f"{len(transformer_list)} Transformers found for kcid {kcid}")

        # Get cost dataframe between consumers and transformers
        cost_df = self.dbc.get_consumer_to_transformer_df(kcid, transformer_list)

        # Filter out connections with distance >= 800
        cost_df = cost_df[cost_df["agg_cost"] < MAX_BROWNFIELD_TRAFO_DISTANCE].sort_values(by=["agg_cost"])

        # Get available transformer capacities from database
        settlement_type = self.dbc.get_settlement_type_from_plz(plz)
        possible_transformers, _ = self.dbc.get_transformer_data(settlement_type)

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

            if float(sim_load) > max(possible_transformers):
                # Remove consumer and mark transformer as full
                pre_result_dict[end_transformer_id].pop()
                full_transformer_list.append(end_transformer_id)

                # Exit if all transformers are full
                if len(full_transformer_list) == len(transformer_list):
                    self.logger.debug("All transformers full")
                    break
            else:
                # Mark consumer as assigned
                assigned_consumer_list.append(start_consumer_id)

        self.logger.info("Transformer selection finished")

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

            # Select the smallest transformer that is larger than the simulated load
            transformer_rated_power = possible_transformers[possible_transformers > float(sim_load)][0].item()

            # Update database with new building cluster
            self.dbc.update_building_cluster(transformer_id, pre_result_dict[transformer_id], building_cluster_count, kcid,
                plz, transformer_rated_power)

        self.logger.info("Brownfield clusters completed")


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

        if len(connection_points) == 0:
            raise ValueError(
                f"Greenfield cluster for PLZ {plz}, KCID {kcid}, BCID {bcid} has no active connection points. "
                "This indicates an inconsistent clustering state after preprocessing."
            )

        # If there's only one connection point, use it
        if len(connection_points) == 1:
            self.dbc.upsert_transformer_selection(plz, kcid, bcid, connection_points[0])
            self.logger.debug(
                f"Greenfield transformer positioned for PLZ {plz}, KCID {kcid}, BCID {bcid}: "
                f"single connection point {connection_points[0]}"
            )
            return

        # Get distance matrix between all connection points
        localid2vid, dist_mat, _ = self.dbc.get_distance_matrix_from_bcid(kcid, bcid)
        if dist_mat.size == 0:
            raise ValueError(
                f"Greenfield cluster for PLZ {plz}, KCID {kcid}, BCID {bcid} has {len(connection_points)} "
                "active connection points but no route distance matrix. This indicates an inconsistent routing state."
            )

        # Get load vector for each connection point
        loads = self.dbc.generate_load_vector(kcid, bcid)

        # Calculate weighted distance (distance * load) for each potential location
        total_load_per_vertice = dist_mat.dot(loads)

        # Prefer candidates that also satisfy the max-distance limit.
        feasible_candidate_ids = np.flatnonzero(dist_mat.max(axis=1) <= MAX_GREENFIELD_TRAFO_DISTANCE)
        if len(feasible_candidate_ids) > 0:
            min_localid = feasible_candidate_ids[np.argmin(total_load_per_vertice[feasible_candidate_ids])]
        else:
            min_localid = int(np.argmin(total_load_per_vertice))
            self.logger.warning(
                f"Greenfield transformer placement for PLZ {plz}, KCID {kcid}, BCID {bcid} has no candidate within "
                f"MAX_GREENFIELD_TRAFO_DISTANCE={MAX_GREENFIELD_TRAFO_DISTANCE} m; falling back to weighted minimum."
            )

        # Select the point with minimum weighted distance as transformer location
        ont_connection_id = int(localid2vid[min_localid])

        # Update the database with the selected transformer position
        self.dbc.upsert_transformer_selection(plz, kcid, bcid, ont_connection_id)

        self.logger.debug(
            f"Greenfield transformer positioned for PLZ {plz}, KCID {kcid}, BCID {bcid}: "
            f"selected connection point {ont_connection_id} from {len(connection_points)} candidates"
        )
        return

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

    def get_consumer_allocated_loads(self, consumer_list: list, buildings_df: pd.DataFrame) -> tuple[
        dict, dict, dict]:
        return utils.allocate_consumer_simultaneous_loads(
            consumer_list,
            buildings_df,
            CONSUMER_CATEGORIES,
        )


    def find_furthest_node_path_list(self, connection_node_list: list, vertices_dict: dict, ont_vertice: int) -> list:
        connection_node_dict = {n: vertices_dict[n] for n in connection_node_list}
        furthest_node = max(connection_node_dict, key=connection_node_dict.get)
        # all the connection nodes in the path from transformer to furthest node are considered as potential branch loads
        furthest_node_path_list = self.dbc.get_path_to_bus(furthest_node, ont_vertice)
        furthest_node_path = [p for p in furthest_node_path_list if p in connection_node_list]

        return furthest_node_path


    def determine_maximum_load_branch(self, furthest_node_path_list: list, buildings_df: pd.DataFrame,
            consumer_df: pd.DataFrame) -> tuple[list, float]:
        """
        Determine the longest feasible branch (in order from transformer to furthest node)
        limited by maximum allowable current.
        
        This method implements the primary constraint for cable dimensioning: current capacity.
        It builds branches by adding nodes one by one until the current limit is reached.
        
        Args:
            furthest_node_path_list: List of nodes from transformer to furthest node
            buildings_df: DataFrame with building load information
            consumer_df: DataFrame with consumer category information
            
        Returns:
            tuple: (branch_node_list, Imax) - List of nodes in the branch and maximum current
        """
        branch_node_list = []
        for node in furthest_node_path_list:
            branch_node_list.append(node)
            # Calculate simultaneous peak load for all nodes in current branch
            sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, branch_node_list)  # sim_peak load in kW

            # Calculate maximum current using worst-case voltage (VN * V_BAND_LOW)
            # This ensures cables are sized for the lowest expected voltage (95% of nominal)
            Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))  # current in kA

            # Check if current exceeds the configured topology grouping cap.
            # Cable sizing can still choose larger/parallel cables later.
            if Imax >= FEEDER_SPLIT_MAX_CURRENT_KA and len(branch_node_list) > 1:
                # Remove the last node if it would exceed current capacity
                branch_node_list.remove(node)
                break
            elif Imax >= FEEDER_SPLIT_MAX_CURRENT_KA and len(branch_node_list) == 1:
                # Even a single node exceeds capacity - keep it but break the loop
                break

        # Calculate final current for the selected branch
        sim_load = utils.simultaneousPeakLoad(buildings_df, consumer_df, branch_node_list)
        Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))

        return branch_node_list, Imax

    def find_branch_attachment_node(
        self,
        branch_start_node: int,
        ont_vertice: int,
        vertices_dict: dict[int, float],
        installed_connection_nodes: set[int],
    ) -> int:
        """Return the deepest already-installed upstream node for a new branch.

        The branch paths returned by ``get_path_to_bus`` are ordered from the
        branch node towards the transformer. Reusing the first installed ancestor
        on that path bundles later splits at existing street-corner nodes instead
        of reconnecting every branch at the transformer. To avoid collapsing too
        many feeders close to the transformer, only reuse split points whose
        routed distance from the transformer exceeds ``MIN_SHARED_PREFIX_LENGTH_M``.
        """
        node_path_list = self.dbc.get_path_to_bus(branch_start_node, ont_vertice)
        for node in node_path_list[1:]:
            if node in installed_connection_nodes:
                if vertices_dict.get(node, 0.0) < MIN_SHARED_PREFIX_LENGTH_M:
                    return ont_vertice
                return node
        return ont_vertice

    def _plan_backbone_branches(
        self,
        connection_nodes: list[int],
        vertices_dict: dict[int, float],
        ont_vertice: int,
        buildings_df: pd.DataFrame,
        consumer_df: pd.DataFrame,
        installer: CableInstaller,
        kcid: int,
        bcid: int,
    ) -> list[dict[str, int | float | list[int]]]:
        """Freeze the finalized branch topology before any feeder cable is sized.

        The previous implementation sized and installed branch backbones inside the
        greedy branch-selection loop. Once later branches were attached to an
        already-installed upstream node, those shared segments kept the original
        cable choice instead of being resized for the combined downstream load.

        This planning pass preserves the existing branch-selection logic and the
        chosen attachment nodes, but delays backbone line creation until the full
        branch tree is known.
        """
        branch_plans: list[dict[str, int | list[int]]] = []
        branch_index = 0
        connection_node_list = list(connection_nodes)
        installed_connection_nodes = set()

        while connection_node_list:
            if len(connection_node_list) == 1:
                remaining = connection_node_list[0]
                self.logger.debug(
                    f"Final remaining connection node {remaining} (kcid={kcid}, bcid={bcid}); preserving direct branch."
                )
                branch_node_list = [remaining]
                attachment_node = ont_vertice
            else:
                furthest_node_path_list = self.find_furthest_node_path_list(
                    connection_node_list, vertices_dict, ont_vertice
                )
                branch_node_list, Imax = self.determine_maximum_load_branch(
                    furthest_node_path_list, buildings_df, consumer_df
                )
                self.logger.debug(
                    f"Selected branch {branch_index} (nodes={len(branch_node_list)}, first={branch_node_list[0]}, "
                    f"last={branch_node_list[-1]}, Imax={Imax:.3f} kA)"
                )
                attachment_node = self.find_branch_attachment_node(
                    branch_node_list[-1],
                    ont_vertice,
                    vertices_dict,
                    installed_connection_nodes,
                )

            branch_plans.append(
                {
                    "branch_index": branch_index,
                    "attachment_node": attachment_node,
                    "branch_nodes": list(branch_node_list),
                }
            )

            for vertice in branch_node_list:
                connection_node_list.remove(vertice)
            installed_connection_nodes.update(branch_node_list)

            branch_index += 1

        return branch_plans

    def _get_split_visualization_edges(
        self,
        branch_plans: list[dict[str, int | list[int]]],
        ont_vertice: int,
    ) -> list[dict[str, int]]:
        """Return real line edges that should get shifted split-topology helpers."""
        children_by_parent: dict[int, set[int]] = {}
        for plan in branch_plans:
            branch_nodes = [int(node) for node in plan["branch_nodes"]]
            attachment_node = int(plan["attachment_node"])

            for index in range(len(branch_nodes) - 1):
                parent = int(branch_nodes[index + 1])
                child = int(branch_nodes[index])
                children_by_parent.setdefault(parent, set()).add(child)

            branch_start_node = int(branch_nodes[-1])
            if branch_start_node != ont_vertice:
                children_by_parent.setdefault(attachment_node, set()).add(branch_start_node)

        split_edges = []
        for parent, children in children_by_parent.items():
            ordered_children = sorted(children)
            if len(ordered_children) <= 1:
                continue

            for child_index, child in enumerate(ordered_children[1:], start=1):
                sign = 1 if child_index % 2 else -1
                magnitude = (child_index + 1) // 2
                split_edges.append(
                    {
                        "from_bus": int(parent),
                        "to_bus": int(child),
                        "offset_rank": int(sign * magnitude),
                    }
                )

        return split_edges

    def _build_feeder_edges_from_branch_plans(
        self,
        branch_plans: list[dict[str, int | list[int]]],
        ont_vertice: int,
    ) -> list[tuple[int, int]]:
        """Return directed feeder tree edges as ``(parent, child)`` pairs."""
        feeder_edges: list[tuple[int, int]] = []

        for plan in branch_plans:
            branch_nodes = [int(node) for node in plan["branch_nodes"]]
            attachment_node = int(plan["attachment_node"])

            for index in range(len(branch_nodes) - 1):
                parent = int(branch_nodes[index + 1])
                child = int(branch_nodes[index])
                feeder_edges.append((parent, child))

            branch_start_node = int(branch_nodes[-1])
            if branch_start_node != ont_vertice:
                feeder_edges.append((attachment_node, branch_start_node))

        return feeder_edges

    def _group_feeder_edges_by_hard_node_section(
        self,
        children_by_node: dict[int, list[int]],
        ont_vertice: int,
    ) -> dict[int, list[tuple[int, int]]]:
        """Group directed feeder edges into uniform sections between hard nodes."""
        hard_nodes = {ont_vertice}
        hard_nodes.update(
            parent for parent, children in children_by_node.items() if len(children) > 1
        )
        sections_by_key: dict[int, list[tuple[int, int]]] = {}
        section_index = 0

        for hard_node in sorted(hard_nodes):
            for first_child in children_by_node.get(hard_node, []):
                section_edges = []
                parent = hard_node
                child = int(first_child)

                while True:
                    section_edges.append((parent, child))
                    child_children = children_by_node.get(child, [])
                    if child in hard_nodes or len(child_children) != 1:
                        break

                    parent = child
                    child = int(child_children[0])

                sections_by_key[section_index] = section_edges
                section_index += 1

        return sections_by_key

    def _select_cables_for_feeder_sections(
        self,
        installer: CableInstaller,
        sections_by_key: dict[int, list[tuple[int, int]]],
        downstream_nodes_by_node: dict[int, list[int]],
        buildings_df: pd.DataFrame,
        consumer_df: pd.DataFrame,
        vertices_dict: dict[int, float],
        ont_vertice: int,
    ) -> dict[tuple[int, int], tuple[str, int]]:
        """Choose one feeder cable/count for every edge in each hard-node section."""
        cable_by_edge: dict[tuple[int, int], tuple[str, int]] = {}

        def _distance_from_transformer(node: int) -> float:
            if node == ont_vertice:
                return 0.0
            try:
                return float(vertices_dict[node])
            except KeyError as exc:
                raise KeyError(
                    f"Missing routed distance for feeder node {node} while sizing feeder sections."
                ) from exc

        for section_edges in sections_by_key.values():
            section_Imax = 0.0
            section_distance = 0.0

            for parent, child in section_edges:
                sim_load = utils.simultaneousPeakLoad(
                    buildings_df, consumer_df, downstream_nodes_by_node[child]
                )
                edge_Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3))
                edge_distance = _distance_from_transformer(child) - _distance_from_transformer(parent)
                section_Imax = max(section_Imax, edge_Imax)
                section_distance += edge_distance

            cable, count = installer.find_minimal_available_cable(
                section_Imax,
                section_distance,
            )

            for edge in section_edges:
                cable_by_edge[edge] = (cable, count)

        return cable_by_edge

    def _install_backbone_lines_two_pass(
        self,
        installer: CableInstaller,
        branch_plans: list[dict[str, int | list[int]]],
        buildings_df: pd.DataFrame,
        consumer_df: pd.DataFrame,
        vertices_dict: dict[int, float],
        ont_vertice: int,
        material_length_by_cable_km: dict,
        kcid: int,
        bcid: int,
    ) -> dict:
        """Install planned backbone lines on the finalized split tree."""
        children_by_node: dict[int, list[int]] = {}
        downstream_nodes_by_node: dict[int, list[int]] = {}

        feeder_edges = self._build_feeder_edges_from_branch_plans(branch_plans, ont_vertice)
        for parent, child in feeder_edges:
            children_by_node.setdefault(parent, []).append(child)

        def _collect_downstream_nodes(node: int) -> list[int]:
            cached_nodes = downstream_nodes_by_node.get(node)
            if cached_nodes is not None:
                return cached_nodes

            downstream_nodes = [] if node == ont_vertice else [node]
            for child in children_by_node.get(node, []):
                downstream_nodes.extend(_collect_downstream_nodes(child))

            downstream_nodes_by_node[node] = downstream_nodes
            return downstream_nodes

        _collect_downstream_nodes(ont_vertice)
        sections_by_key = self._group_feeder_edges_by_hard_node_section(
            children_by_node,
            ont_vertice,
        )
        cable_by_edge = self._select_cables_for_feeder_sections(
            installer,
            sections_by_key,
            downstream_nodes_by_node,
            buildings_df,
            consumer_df,
            vertices_dict,
            ont_vertice,
        )
        section_by_edge = {
            edge: int(section_id)
            for section_id, section_edges in sections_by_key.items()
            for edge in section_edges
        }

        for plan in branch_plans:
            branch_nodes = list(plan["branch_nodes"])
            branch_index = int(plan["branch_index"])
            attachment_node = int(plan["attachment_node"])

            for index in range(len(branch_nodes) - 1):
                parent = int(branch_nodes[index + 1])
                child = int(branch_nodes[index])
                cable, count = cable_by_edge[(parent, child)]
                material_length_by_cable_km = installer.create_line_node_to_node(
                    self.plz,
                    kcid,
                    bcid,
                    [child, parent],
                    vertices_dict,
                    material_length_by_cable_km,
                    cable,
                    ont_vertice,
                    count,
                    section_by_edge[(parent, child)],
                )

            branch_start_node = int(branch_nodes[-1])
            if branch_start_node == ont_vertice:
                sim_load = utils.simultaneousPeakLoad(
                    buildings_df, consumer_df, downstream_nodes_by_node[branch_start_node]
                )
                Imax = sim_load / (VN * V_BAND_LOW * np.sqrt(3)) if sim_load > 0 else 0.0
                cable, count = installer.find_minimal_available_cable(Imax)
                installer.create_line_ont_to_lv_bus(
                    self.plz, bcid, kcid, branch_start_node, cable, count, ont_vertice
                )
                self.logger.debug(
                    f"Branch {branch_index} connected directly to transformer after two-pass sizing "
                    f"(cable={cable}, parallels={count}, load_kw={sim_load:.2f})."
                )
            elif attachment_node != ont_vertice:
                sim_load = utils.simultaneousPeakLoad(
                    buildings_df, consumer_df, downstream_nodes_by_node[branch_start_node]
                )
                cable, count = cable_by_edge[(attachment_node, branch_start_node)]
                material_length_by_cable_km = installer.create_line_node_to_node(
                    self.plz,
                    kcid,
                    bcid,
                    [branch_start_node, attachment_node],
                    vertices_dict,
                    material_length_by_cable_km,
                    cable,
                    ont_vertice,
                    count,
                    section_by_edge[(attachment_node, branch_start_node)],
                )
                self.logger.debug(
                    f"Branch {branch_index} attached to finalized split node {attachment_node} after two-pass sizing "
                    f"(cable={cable}, parallels={count}, load_kw={sim_load:.2f})."
                )
            else:
                sim_load = utils.simultaneousPeakLoad(
                    buildings_df, consumer_df, downstream_nodes_by_node[branch_start_node]
                )
                cable, count = cable_by_edge[(ont_vertice, branch_start_node)]
                length = installer.create_line_start_to_lv_bus(
                    self.plz,
                    bcid,
                    kcid,
                    branch_start_node,
                    vertices_dict,
                    cable,
                    count,
                    ont_vertice,
                    section_by_edge[(ont_vertice, branch_start_node)],
                )
                material_length_by_cable_km[cable] += length
                self.logger.debug(
                    f"Branch {branch_index} connected to LV bus after two-pass sizing "
                    f"(cable={cable}, parallels={count}, length_km={length:.4f}, load_kw={sim_load:.2f})."
                )

        return material_length_by_cable_km

    def install_cables(self):
        """
        Installs electrical cables using the electrical backend pattern.

        The algorithm works as follows:
        1. Retrieves all clusters (kcid, bcid) for the postal code area
        2. For each cluster:
           a. Prepares building and connection data
           b. Creates an electrical network via backend
           c. Adds buses, transformers, and loads using ComponentSpecs
           d. Installs cables using the same branch-by-branch greedy algorithm
        3. Tracks progress and saves the network configurations

        Returns:
            None
        """
        # Get all clusters for the postal code area
        cluster_list = self.dbc.get_list_from_plz(self.plz)
        total_clusters = len(cluster_list)
        ci_count = 0
        next_progress_checkpoint = 10
        converged_count = 0
        not_converged_count = 0
        voltage_violation_count = 0

        for id in cluster_list:
            kcid, bcid = id
            self.logger.debug(f"Start cable installation for PLZ {self.plz} kcid {kcid} bcid {bcid}")

            # Get data for this cluster
            vertices_dict, ont_vertice, vertices_list, buildings_df, consumer_df, consumer_list, connection_nodes = (
                self.prepare_vertices_list(self.plz, kcid, bcid)
            )
            sim_load_per_building, load_units, load_type = self.get_consumer_allocated_loads(consumer_list, buildings_df)

            # Initialize backend using configuration
            backend = create_backend(ELECTRICAL_BACKEND, logger=self.logger)
            circuit_name = f"PLZ{self.plz}_kcid{kcid}_bcid{bcid}"
            backend.initialize_circuit(name=circuit_name, source_bus="MVbus 1", primary_kv=20.0)
            # Fetch cables once from database (single source of truth)
            cables = self.dbc.fetch_cables()

            # Register cable types from equipment data
            backend.register_cable_types(cables)

            # Get available cable
            all_available_cables = backend.get_cable_types()
            if not all_available_cables:
                all_available_cables = [cable[0] for cable in cables]

            # Tracks installed cable material length, so parallel cables count multiple times.
            material_length_by_cable_km = {c: 0 for c in all_available_cables}

            # Create cable installer
            installer = CableInstaller(
                backend, self.dbc, self.logger, cables,
                FEEDER_CABLES, CONSUMER_CONNECTION_CABLES
            )
            
            # Create network components
            installer.create_lvmv_bus(self.plz, kcid, bcid)
            installer.create_transformer(self.plz, kcid, bcid)
            installer.create_connection_bus(connection_nodes)
            installer.create_consumer_bus_and_load(consumer_list, sim_load_per_building, buildings_df, load_type)

            trafo_power = self.dbc.get_transformer_rated_power_from_bcid(self.plz, kcid, bcid)
            self.logger.debug(
                f"Backend network initialized (buses={backend.get_component_count('buses')}, "
                f"loads={backend.get_component_count('loads')}, transformer_rated_power={trafo_power} kVA)"
            )

            # First finalize the split topology, then size every backbone segment on
            # the resulting tree so shared prefixes carry the full downstream load.
            branch_plans = self._plan_backbone_branches(
                connection_nodes,
                vertices_dict,
                ont_vertice,
                buildings_df,
                consumer_df,
                installer,
                kcid,
                bcid,
            )

            for plan in branch_plans:
                material_length_by_cable_km = installer.install_consumer_cables(
                    self.plz,
                    bcid,
                    kcid,
                    list(plan["branch_nodes"]),
                    ont_vertice,
                    vertices_dict,
                    sim_load_per_building,
                    material_length_by_cable_km,
                )

            material_length_by_cable_km = self._install_backbone_lines_two_pass(
                installer,
                branch_plans,
                buildings_df,
                consumer_df,
                vertices_dict,
                ont_vertice,
                material_length_by_cable_km,
                kcid,
                bcid,
            )
            split_visualization_edges = self._get_split_visualization_edges(branch_plans, ont_vertice)
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
            )
            self.dbc.rebuild_lines_result_view_for_grid(self.plz, kcid, bcid)

            branch_index = len(branch_plans)

            # Cluster summary
            material_length = sum(material_length_by_cable_km.values())
            used_material_lengths = {k: v for k, v in material_length_by_cable_km.items() if v > 0}
            if used_material_lengths:
                cable_summary = ", ".join([f"{k}:{v:.3f} km" for k, v in sorted(used_material_lengths.items(), key=lambda x: -x[1])])
            else:
                cable_summary = "no cables installed"

            lines_count = backend.get_component_count('lines')
            self.logger.info(
                f"Finished cluster kcid={kcid}, bcid={bcid}: branches={branch_index}, lines={lines_count}, "
                f"material_length={material_length:.3f} km ({cable_summary})"
            )

            # Track and report progress using real cluster counts.
            ci_count += 1
            current_percent = int((ci_count / total_clusters) * 100)

            while current_percent >= next_progress_checkpoint and next_progress_checkpoint <= 100:
                self.logger.info(
                    f"Cable installation progress: {ci_count}/{total_clusters} clusters ({current_percent}%)"
                )
                next_progress_checkpoint += 10

            powerflow_status = self.save_net(backend, kcid, bcid)
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
            f"voltage_band_violations={voltage_violation_count}"
        )

    def save_net(self, backend: IElectricalBackend, kcid, bcid) -> str:
        """
        Validate and save grid to file and database using backend pattern.
        """
        # Validate grid with power flow before saving
        powerflow_status = "not_converged"
        try:
            converged = backend.solve_power_flow()
            if converged:
                metrics = backend.get_circuit_metrics()
                min_voltage_pu = metrics.get("min_voltage_pu")
                max_voltage_pu = metrics.get("max_voltage_pu")

                voltage_out_of_band = False
                if min_voltage_pu is not None and min_voltage_pu < V_BAND_LOW:
                    voltage_out_of_band = True
                if max_voltage_pu is not None and max_voltage_pu > V_BAND_HIGH:
                    voltage_out_of_band = True

                if voltage_out_of_band:
                    powerflow_status = "voltage_violation"
                    self.logger.warning(
                        f"Power flow converged but voltage band was violated for kcid={kcid}, bcid={bcid} "
                        f"(min_vm_pu={min_voltage_pu}, max_vm_pu={max_voltage_pu}, "
                        f"allowed=[{V_BAND_LOW}, {V_BAND_HIGH}])."
                    )
                else:
                    self.logger.info(f"Power flow converged for kcid={kcid}, bcid={bcid}")
                    powerflow_status = "converged"
            else:
                self.logger.warning(f"Power flow did NOT converge for kcid={kcid}, bcid={bcid}")
                powerflow_status = "not_converged"
        except Exception as e:
            self.logger.warning(f"Power flow failed for kcid={kcid}, bcid={bcid}: {e}")

        if powerflow_status != "converged":
            self.logger.warning(
                f"Grid with kcid:{kcid} bcid:{bcid} will be stored with status={powerflow_status}."
            )

        if SAVE_GRID_FOLDER:
            savepath_folder = Path(RESULT_DIR, "grids", f"version_{VERSION_ID}", str(self.plz))
            savepath_folder.mkdir(parents=True, exist_ok=True)
            filename = f"kcid{kcid}bcid{bcid}.json"
            savepath_file = Path(savepath_folder, filename)
            try:
                backend.export_to_format(filename=savepath_file)
            except Exception as e:
                self.logger.warning(
                    f"Failed to export grid file for kcid={kcid}, bcid={bcid}, status={powerflow_status}: {e}"
                )

        json_string = None
        try:
            json_string = backend.export_to_format(filename=None)
        except Exception as e:
            self.logger.warning(
                f"Failed to export grid JSON for kcid={kcid}, bcid={bcid}, status={powerflow_status}: {e}"
            )

        if ELECTRICAL_BACKEND == "pandapower":
            transformer_description = backend.net.trafo.name[0]
        else:
            transformer_description = "N/A"

        self.dbc.save_pp_net_with_json(
            self.plz,
            kcid,
            bcid,
            json_string,
            transformer_description,
            powerflow_status,
        )

        if ELECTRICAL_BACKEND == "pandapower":
            if json_string is None:
                self.logger.warning(
                    f"Skipping SQL net persistence for kcid={kcid}, bcid={bcid} because JSON export failed."
                )
            elif getattr(backend, "net", None) is None:
                self.logger.warning(
                    f"Skipping SQL net persistence for kcid={kcid}, bcid={bcid} because no backend network is present."
                )
            else:
                try:
                    self.dbc.save_pandapower_net_with_sql(
                        plz=self.plz,
                        kcid=kcid,
                        bcid=bcid,
                        net=backend.net,
                    )
                except Exception as e:
                    self.logger.warning(
                        f"Failed to store SQL net tables for kcid={kcid}, bcid={bcid}: {e}"
                    )

        self.logger.debug(
            f"Grid with kcid:{kcid} bcid:{bcid} is stored with status={powerflow_status}."
        )
        return powerflow_status

