"""Grid generation for one or more postcode areas (PLZ).

:class:`GridGenerator` runs the whole pipeline for a PLZ inside PLZ-specific
temporary tables and finally copies the results into the ``*_result`` tables:

1. ``prepare_*``: postcode, buildings with loads, candidate transformers and the
   routable street network,
2. :meth:`GridGenerator.apply_kmeans_clustering`: one ``kcid`` per connected street
   component, split with k-means when it has more than ``MAX_BUILDINGS_PER_KCID``
   buildings,
3. :meth:`GridGenerator.position_all_transformers`: building clusters (``bcid``)
   around existing (brownfield) transformers, the remaining buildings clustered
   into greenfield transformer areas, and the greenfield station positions,
4. :meth:`GridGenerator.install_cables`: feeders and service cables of every
   grid, a validation power flow, and storage of the network.
"""

import math
import traceback
import warnings
from pathlib import Path

import numpy as np
import pandas as pd  # type: ignore
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform
from sklearn.cluster import KMeans
from concurrent.futures import ProcessPoolExecutor, as_completed  # lightweight parallel execution

import pylovo.database.database_client as dbc
from pylovo.infdb.infdb_client import InfdbClient
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo import utils
from pylovo.config_loader import (
    AGGREGATE_NEARBY_CONNECTION_POINTS,
    CLASSIFICATION_VERSION,
    CONFIG_EQUIPMENT_DATA,
    CONFIG_GENERATION,
    CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS,
    CONNECTION_POINT_AGGREGATION_RADIUS_M,
    CONSUMER_CATEGORIES,
    CONSUMER_CONNECTION_CABLES,
    ELECTRICAL_BACKEND,
    FEEDER_CABLES,
    GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA,
    GREENFIELD_TRAFO_POSITION_TOLERANCE,
    K_MEANS_SEED,
    LOG_LEVEL,
    LV_REFERENCE_VOLTAGE_PU,
    MAX_BROWNFIELD_TRAFO_DISTANCE,
    MAX_BUILDINGS_PER_KCID,
    MAX_GREENFIELD_TRAFO_DISTANCE,
    MERGE_GREENFIELD_CLUSTERS,
    N_JOBS,
    POWER_FLOW_MAX_VM_PU,
    POWER_FLOW_MIN_VM_PU,
    RESIDENTIAL_ONLY_GENERATION,
    RESULT_DIR,
    RURAL_MAX_HOUSEHOLDS,
    RURAL_MIN_BUILDING_DISTANCE,
    SAVE_GRID_FOLDER,
    TRANSFORMER_PLANNING_UTILIZATION,
    URBAN_MAX_BUILDING_DISTANCE,
    URBAN_MIN_HOUSEHOLDS,
    USE_DSO_TRANSFORMER_POSITIONS,
    USE_INFDB,
    USE_MANUAL_TRANSFORMER_POSITIONS,
    USE_OPEN_TRANSFORMER_POSITIONS,
    VERSION_ID,
)

# Import electrical backend components
from pylovo.electrical_backend import IElectricalBackend, create_backend
from pylovo.cable_installer import CableInstaller
from pylovo import feeder_planning
from pylovo.station_voltage import solve_validation_power_flow

class ResultExistsError(Exception):
    """Raised when the grids of a PLZ already exist for the current ``VERSION_ID``."""


class GridGenerator:
    """
    Generates the synthetic LV grids of postcode areas (PLZ).

    One instance holds a :class:`~pylovo.database.database_client.DatabaseClient`
    (and an ``InfdbClient`` if ``USE_INFDB``) and logs to ``log_file``
    (keyword argument, default ``log/log.txt``). Generate grids with
    :meth:`generate_grid_for_single_plz` or :meth:`generate_grid_for_multiple_plz`.
    """

    def __init__(self, plz=999999, **kwargs):
        self.plz = plz
        self.log_file = kwargs.get("log_file", "log/log.txt")
        self.dbc = dbc.DatabaseClient(log_file=self.log_file)
        self.dbc.insert_version_if_not_exists()
        self.logger = utils.create_logger(
            name="GridGenerator", log_file=self.log_file, log_level=LOG_LEVEL
        )
        self.inf_dbc = None
        if USE_INFDB:
            self.inf_dbc = InfdbClient(log_file=self.log_file)

    def __del__(self):
        # Close the database connection. A destructor must not raise: dbc is missing if __init__
        # failed early, and test doubles may not implement close().
        close = getattr(getattr(self, "dbc", None), "close", None)
        if callable(close):
            close()

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
        self.dbc.ensure_grid_persistence_schema()
        self.dbc.commit_changes()
        print('-------------------- start', self.plz, '---------------------------')
        self.dbc.acquire_plz_lock(plz)
        interrupted = False
        try:
            self.dbc.create_temp_tables(plz)
            self.generate_grid()
            if not self.dbc.get_list_from_plz(plz):
                self.logger.warning(
                    f"No grid_result rows were generated for PLZ {plz}; skipping result-table persistence."
                )
                self.dbc.rollback_changes()
                return
            self.dbc.save_tables(plz=self.plz)
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
                self.dbc.drop_temp_tables(plz)
                # Commit cleanup so dropped tables don't reappear after interruption.
                self.dbc.commit_changes()
            except Exception as cleanup_error:
                self.logger.error(
                    f"Failed to clean up PLZ-specific temporary tables for PLZ {plz}: {cleanup_error}"
                )
                self.dbc.rollback_changes()
            finally:
                self.dbc.release_plz_lock(plz)
                self.dbc.commit_changes()

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
        """Generate the grids of several PLZ, in parallel worker processes if possible.

        Workers are used when ``parallel`` is set, there is more than one PLZ and
        ``N_JOBS`` (``N_JOBS_PERCENT`` of the CPU cores) is above one. Each worker
        logs to ``log/log_<plz>.txt``; a failing PLZ is logged and skipped. The
        materialized views are refreshed once at the end.

        Args:
            df_plz: Table with a ``plz`` column.
            analyze_grids: Run the parameter analysis after each PLZ.
            parallel: Allow parallel workers.

        Raises:
            KeyboardInterrupt: If the run is interrupted; pending PLZ are cancelled.
        """
        self.dbc.ensure_grid_persistence_schema()
        self.dbc.commit_changes()
        plz_list = [int(plz) for plz in df_plz["plz"]]

        # Parallel workers only help with several PLZ and more than one allowed core.
        should_use_parallel = parallel and len(plz_list) > 1 and N_JOBS > 1
        self.logger.info(
            f"Generating {len(plz_list)} PLZ: parallel={should_use_parallel} "
            f"(requested={parallel}, N_JOBS={N_JOBS})"
        )
        failed_plz = []

        if should_use_parallel:
            max_workers = min(N_JOBS, len(plz_list))
            self.logger.info(f"Using {max_workers} worker processes for {len(plz_list)} PLZ")

            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(GridGenerator._worker, plz, analyze_grids): plz
                    for plz in plz_list
                }
                completed_count = 0
                total_count = len(plz_list)

                try:
                    for future in as_completed(futures):
                        plz = futures[future]
                        completed_count += 1
                        try:
                            # Raises the worker's exception, if any.
                            future.result()
                            self.logger.info(f"Completed PLZ {plz} ({completed_count}/{total_count})")
                        except Exception as exc:
                            # Record the failure and continue with the other PLZ.
                            self.logger.exception(
                                f"PLZ {plz} generated an exception ({completed_count}/{total_count}): {exc}"
                            )
                            failed_plz.append(plz)

                except KeyboardInterrupt:
                    self.logger.warning(
                        f"KeyboardInterrupt received after {completed_count}/{total_count} PLZ; "
                        "cancelling pending PLZ and waiting for running workers."
                    )
                    for future in futures:
                        future.cancel()

                    # Report workers that still finish within the optional grace period.
                    shutdown_timeout = CONFIG_GENERATION.get("GRACEFUL_SHUTDOWN_TIMEOUT", 5)
                    try:
                        for future in as_completed(futures, timeout=shutdown_timeout):
                            if not future.cancelled():
                                plz = futures[future]
                                try:
                                    future.result()
                                    self.logger.info(f"Gracefully completed PLZ {plz}")
                                except Exception as exc:
                                    self.logger.warning(f"PLZ {plz} failed during graceful shutdown: {exc}")
                    except TimeoutError:
                        pass

                    raise KeyboardInterrupt("Grid generation interrupted by user") from None

                except Exception as e:
                    self.logger.error(
                        f"Error during parallel processing after {completed_count}/{total_count} PLZ: {e}"
                    )
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
                self.logger.warning(
                    f"Parallel grid generation finished with {len(failed_plz)} failed PLZ: {failed_plz_str}"
                )
            else:
                self.logger.info("Parallel grid generation finished with no failed PLZ.")

    @staticmethod
    def _worker(plz: int, analyze_grids: bool) -> None:
        """Generate the grid of one PLZ in a worker process.

        Each worker builds its own :class:`GridGenerator` (own database connection)
        that logs to ``log/log_<plz>.txt``. The connection is closed afterwards;
        exceptions are passed on to the parent process.
        """
        log_file = Path("log") / f"log_{plz}.txt"
        if log_file.exists():
            log_file.unlink()  # Overwrite log file if it exists

        gg = None
        try:
            gg = GridGenerator(log_file=log_file)
            gg.generate_grid_for_single_plz(
                plz=plz, analyze_grids=analyze_grids, refresh_mv=False
            )
        except Exception:
            if gg is not None:
                gg.logger.exception(f"Worker failed for PLZ {plz}")
                gg.dbc.rollback_changes()
            raise
        finally:
            if gg is not None:
                gg.dbc.close()

    def generate_grid(self):
        """Run all generation steps for ``self.plz`` inside the PLZ temporary tables.

        Raises:
            ResultExistsError: If the PLZ already has grids for ``VERSION_ID``.
        """
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
            transformer_station_buildings = self.inf_dbc.fetch_transformer_station_buildings_from_infdb(self.plz)
            transformer_candidate_count = self.dbc.upsert_lod2_transformer_stations(transformer_station_buildings)
            if transformer_candidate_count:
                self.logger.info(
                    f"LoD2 transformer-station buildings added to transformer candidates: {transformer_candidate_count}"
                )
            buildings_data = self.inf_dbc.fetch_buildings_from_infdb(self.plz)
            self.dbc.set_buildings_table(buildings_data, self.plz)
        else:
            self.dbc.set_residential_buildings_table(self.plz)
            if not RESIDENTIAL_ONLY_GENERATION:
                self.dbc.set_other_buildings_table(self.plz)
        if RESIDENTIAL_ONLY_GENERATION:
            removed_non_residential = self.dbc.remove_non_residential_buildings_from_buildings_tem()
            self.logger.info(
                f"Residential-only generation enabled: removed {removed_non_residential} non-residential buildings"
            )
        # self.dbc.commit_changes() # only activate for debugging - otherwise multiprocessing does not work
        self.logger.info("Buildings_tem table prepared")
        removed_transformer_buildings = self.dbc.remove_non_residential_buildings_overlapping_transformers(
            include_dso=USE_DSO_TRANSFORMER_POSITIONS)
        if removed_transformer_buildings:
            self.logger.info(
                f"Removed {removed_transformer_buildings} non-residential buildings overlapping transformer candidates"
            )
        self.dbc.remove_duplicate_buildings()
        self.logger.info("Duplicate buildings removed from buildings_tem")

        # Fill missing household counts before they contribute to the postcode's
        # settlement classification, then calculate residential demand.
        unloadcount = self.dbc.set_building_peak_load()
        self.logger.info(
            f"Building peakload calculated in buildings_tem, {unloadcount} unloaded buildings are removed from "
            f"buildings_tem"
        )

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

        too_large_consumers = self.dbc.update_too_large_consumers_to_zero()
        self.logger.debug(
            f"{too_large_consumers} non-residential components assumed MV-direct and excluded from LV modeling"
        )


    def prepare_transformers(self):
        """
        Cache transformers from raw data tables and stores in temporary tables.
        FROM: transformers
        INTO: buildings_tem
        """
        self.dbc.set_buildings_tem_plz(self.plz)
        sources = {"include_dso": USE_DSO_TRANSFORMER_POSITIONS, "include_open": USE_OPEN_TRANSFORMER_POSITIONS,
                   "include_manual": USE_MANUAL_TRANSFORMER_POSITIONS}
        if any(sources.values()):
            self.dbc.insert_transformers(self.plz, **sources)
            self.logger.info(
                "Transformers inserted into buildings_tem table "
                f"(dso={USE_DSO_TRANSFORMER_POSITIONS}, open={USE_OPEN_TRANSFORMER_POSITIONS}, "
                f"manual={USE_MANUAL_TRANSFORMER_POSITIONS})"
            )
        else:
            self.logger.info("Existing transformer positions disabled by configuration")
        removed_transformer_buildings = self.dbc.remove_transformer_evidence_buildings_from_buildings_tem(**sources)
        self.logger.info(
            f"Removed {removed_transformer_buildings} transformer-evidence buildings from buildings_tem consumer input"
        )
        self.dbc.count_indoor_transformers()
        self.dbc.drop_indoor_transformers()
        self.logger.info("Indoor transformers removed from buildings_tem table")

    def prepare_ways(self):
        """
        Cache ways, create network, connect buildings to the ways network
        FROM: ``ways`` (or the InfDB street tables), ``buildings_tem``
        INTO: ``ways_tem``, ``buildings_tem``, ``ways_tem_vertices_pgr``
        """
        if USE_INFDB:
            ways_rows = self.inf_dbc.fetch_ways_from_infdb(self.plz)
            ways_count = self.dbc.set_ways_tem_table_infdb(ways_rows, self.plz)
        else:
            ways_count = self.dbc.set_ways_tem_table(self.plz)
        self.logger.info(f"The ways_tem table filled with {ways_count} ways")

        # Index loaded roads before nearest-road searches.
        self.dbc.index_and_analyze_staging(self.plz)

        # Run preprocessing functions that segment roads and connect buildings
        self.dbc.preprocess_ways()
        self.logger.info("Ways preprocessing completed in ways_tem.")

        # Build pgRouting topology on the processed network
        self.dbc.build_pgr_network_topology(self.plz)
        self.logger.info("pgRouting network topology created from ways_tem.")

        self.dbc.update_ways_cost()
        unconn = self.dbc.set_vertice_id()
        self.logger.debug(f"vertice id set, {unconn} buildings with no vertice id")
        if AGGREGATE_NEARBY_CONNECTION_POINTS:
            aggregated_rows = self.dbc.aggregate_nearby_connection_points(
                CONNECTION_POINT_AGGREGATION_RADIUS_M,
                CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS,
            )
            self.logger.info(
                "Aggregated nearby building connection points "
                f"for {aggregated_rows} building rows "
                f"(radius={CONNECTION_POINT_AGGREGATION_RADIUS_M} m, "
                f"max_buildings={CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS})"
            )

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
                    # 1-D selection: int() of the 1-element rows of a 2-D selection is deprecated in NumPy.
                    related_vertices = vertices[component == component_id]
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
        elif conn_building_count > MAX_BUILDINGS_PER_KCID:
            # K-means applied to large components before the expensive KCID distance matrix is built.
            cluster_count = math.ceil(conn_building_count / MAX_BUILDINGS_PER_KCID)
            k_means = KMeans(n_clusters=cluster_count, random_state=K_MEANS_SEED, n_init="auto")
            (selected_vertices, coordinates) = self.dbc.get_connected_component_geometries(vertices)
            kcids = k_means.fit_predict(coordinates) + self.dbc.get_kcid_length() + 1
            self.dbc.update_kmeans_cluster_multiple(selected_vertices, kcids)
            log_msg = (
                f"Large component {component_index} clustered into {cluster_count} groups "
                f"(buildings={conn_building_count}, max_buildings_per_kcid={MAX_BUILDINGS_PER_KCID})"
                if component_index is not None
                else f"Large component clustered into {cluster_count} groups "
                     f"(buildings={conn_building_count}, max_buildings_per_kcid={MAX_BUILDINGS_PER_KCID})"
            )
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
        """Split the unassigned buildings of a kcid into greenfield transformer areas (bcids).

        Average-linkage hierarchical clustering on the routed distance matrix is cut
        into two clusters at a time; a cluster that exceeds the largest allowed
        transformer (after ``TRANSFORMER_PLANNING_UTILIZATION``) or has no station
        position within the greenfield distance limit is split again, until all
        clusters are feasible. Optionally neighbouring clusters are merged again
        (``MERGE_GREENFIELD_CLUSTERS``). The bcids are numbered from 1 by their
        smallest vertex id and written to ``grid_result`` with their rating.

        Args:
            plz: Postal code
            kcid: K-means cluster ID
        """
        # Get data needed for clustering
        buildings = self.dbc.get_buildings_from_kcid(kcid)
        consumer_cat_df = self.dbc.get_consumer_categories()
        settlement_type = self.dbc.get_settlement_type_from_plz(plz)
        transformer_capacities, _ = self.dbc.get_transformer_data(settlement_type)
        self.logger.info(f"Start BCID dimensioning for PLZ {plz}, KCID {kcid}")

        # Get distance matrix and prepare for hierarchical clustering
        localid2vid, dist_mat, vid2localid = self.dbc.get_distance_matrix_from_kcid(kcid)
        dist_vector = squareform(dist_mat)

        if len(dist_vector) == 0:
            # No pair of connection points: a single point becomes one bcid directly.
            planning_points = utils.planning_nodes(buildings)
            vertices = sorted(planning_points.dropna().astype(int).unique().tolist())
            if len(vertices) == 1:
                total_sim_load = utils.simultaneous_peak_load(buildings, consumer_cat_df, vertices) / TRANSFORMER_PLANNING_UTILIZATION
                feasible_transformers = transformer_capacities[transformer_capacities > total_sim_load]
                transformer_size = int(feasible_transformers[0]) if len(feasible_transformers) else int(math.ceil(total_sim_load))
                self.dbc.clear_grid_result_in_kmean_cluster(plz, kcid)
                self.dbc.upsert_bcid(plz, kcid, 1, vertices=vertices, transformer_rated_power=transformer_size)
                self.logger.info(
                    f"BCID dimensioning fallback for PLZ {plz}, KCID {kcid}: "
                    f"single connection point assigned to BCID 1 with transformer {transformer_size} kVA"
                )
                return
            self.logger.warning(
                f"Skipped BCID dimensioning for PLZ {plz}, KCID {kcid}: "
                f"empty distance vector for {len(vertices)} active connection point(s)"
            )
            return

        # Initialize hierarchical clustering
        Z = linkage(dist_vector, method="average")
        valid_cluster_dict = {}
        invalid_trans_cluster_dict = {}
        cluster_amount = 2
        new_localid2vid = localid2vid
        new_dist_mat = dist_mat  # distance matrix of the (sub)problem being clustered
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
                dist_mat=new_dist_mat,
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

        neighboring_pairs = self.dbc.get_cluster_adjacency_from_street_graph(cluster_dict)
        if not neighboring_pairs:
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
                    if frozenset((left_id, right_id)) not in neighboring_pairs:
                        continue

                    right_local_ids = [vid2localid[vid] for vid in right_vertices if vid in vid2localid]
                    if not right_local_ids:
                        continue

                    combined_vertices = list(dict.fromkeys(left_vertices + right_vertices))
                    combined_load = utils.simultaneous_peak_load(buildings, consumer_cat_df, combined_vertices) / TRANSFORMER_PLANNING_UTILIZATION
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
            neighboring_pairs = {
                frozenset(left_id if cluster_id == right_id else cluster_id for cluster_id in pair)
                for pair in neighboring_pairs
            }
            neighboring_pairs = {pair for pair in neighboring_pairs if len(pair) == 2}
            merge_count += 1

        if merge_count:
            self.logger.info(
                f"Greenfield cluster merge complete for PLZ {plz}, KCID {kcid}: "
                f"merged {merge_count} neighboring cluster pairs, final_clusters={len(merged_clusters)}"
            )

        return dict(enumerate(merged_clusters.values()))

    def _order_clusters_by_min_vertice(self, cluster_dict: dict) -> dict:
        """Renumber clusters from 1 in the order of their smallest vertex id.

        Partitions that are equal up to renaming therefore get the same bcids.

        Args:
            cluster_dict: ``{cluster id: (vertices, transformer rating)}``.

        Returns:
            ``{bcid: (vertices, transformer rating)}`` with bcids 1, 2, ...
        """
        ordered_vertices = sorted(cluster_dict.items(), key = lambda cluster: min(cluster[1][0]))
        return {new_bcid: vertices for new_bcid, (_, vertices) in enumerate(ordered_vertices, start=1)}

    def position_brownfield_transformers(self, plz: int, kcid: int, transformer_list: list) -> None:
        """
        Assign buildings to the existing transformers and store them as bcid in buildings_tem.

        Consumer-transformer pairs closer than ``MAX_BROWNFIELD_TRAFO_DISTANCE`` are
        visited from the shortest routed distance up; a consumer joins the
        transformer unless the coincident load would exceed the transformer's
        imported rating or, without one, the largest allowed catalogue rating
        (after ``TRANSFORMER_PLANNING_UTILIZATION``). Every used transformer becomes a
        bcid with a negative id (-1, -2, ...) and keeps its imported rating or gets
        the smallest sufficient catalogue rating; unused transformers are removed.
        Unassigned consumers stay for greenfield clustering.

        Args:
            plz: Postal code
            kcid: K-means cluster ID
            transformer_list: List of transformer IDs
        """
        self.logger.info(f"{len(transformer_list)} Transformers found for kcid {kcid}")
        buildings = self.dbc.get_buildings_from_kcid(kcid)
        consumer_cat_df = self.dbc.get_consumer_categories()
        loads = utils.CoincidentLoads(buildings, consumer_cat_df)

        # Get cost dataframe between consumers and transformers
        cost_df = self.dbc.get_consumer_to_transformer_df(kcid, transformer_list)

        # Keep connections shorter than MAX_BROWNFIELD_TRAFO_DISTANCE, nearest first
        cost_df = cost_df[cost_df["agg_cost"] < MAX_BROWNFIELD_TRAFO_DISTANCE].sort_values(by=["agg_cost"])

        # Get available transformer capacities from database
        settlement_type = self.dbc.get_settlement_type_from_plz(plz)
        possible_transformers, _ = self.dbc.get_transformer_data(settlement_type)
        # A transformer imported with its real rating is filled only up to that
        # rating and keeps it; any other keeps the catalogue limit and choice.
        known_capacities = self.dbc.get_brownfield_transformer_capacity_map(transformer_list)

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
            sim_load = loads.simultaneous_peak_load(pre_result_dict[end_transformer_id])

            known = known_capacities.get(int(end_transformer_id))
            if (float(sim_load) > known) if known is not None else (
                    float(sim_load) / TRANSFORMER_PLANNING_UTILIZATION > max(possible_transformers)):
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
            sim_load = loads.simultaneous_peak_load(pre_result_dict[transformer_id])

            if int(transformer_id) in known_capacities:
                transformer_rated_power = known_capacities[int(transformer_id)]
            else:
                # Select the smallest transformer that is larger than the simulated load
                transformer_rated_power = possible_transformers[
                    possible_transformers > float(sim_load) / TRANSFORMER_PLANNING_UTILIZATION][0].item()

            # Update database with new building cluster
            self.dbc.update_building_cluster(transformer_id, pre_result_dict[transformer_id], building_cluster_count, kcid,
                plz, transformer_rated_power)

        self.logger.info("Brownfield clusters completed")


    def position_greenfield_transformers(self, plz, kcid, bcid):
        """
        Positions a transformer at the optimal location for a greenfield building cluster.

        The optimal location minimizes the sum of distance*load from each vertex to others,
        among the connection points from which every point of the cluster is within the
        cluster's greenfield distance limit. With ``GREENFIELD_TRAFO_POSITION_TOLERANCE``
        > 0 the station is drawn (seeded per cluster) among the feasible points whose
        cost is at most ``1 + tolerance`` times the optimum.

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

        # Get load vector aligned to the matrix vertex order. Some pgRouting
        # matrix calls omit isolated vertices; keep the generated grid going
        # while logging the dropped load points explicitly.
        matrix_connection_points = [localid2vid[index] for index in range(len(localid2vid))]
        loads, missing_load_points = self.dbc.generate_load_vector_for_connection_points(
            kcid, bcid, matrix_connection_points
        )
        if missing_load_points:
            preview = ", ".join(str(point) for point in missing_load_points[:10])
            if len(missing_load_points) > 10:
                preview += ", ..."
            self.logger.warning(
                f"Greenfield transformer placement for PLZ {plz}, KCID {kcid}, BCID {bcid} "
                f"ignored {len(missing_load_points)} unroutable load connection point(s): {preview}"
            )
        if len(loads) != dist_mat.shape[1]:
            raise ValueError(
                f"Greenfield transformer placement for PLZ {plz}, KCID {kcid}, BCID {bcid} has "
                f"distance matrix shape {dist_mat.shape} but aligned load vector length {len(loads)}."
            )

        # Calculate weighted distance (distance * load) for each potential location
        total_load_per_vertice = dist_mat.dot(loads)

        # Prefer candidates that also satisfy the max-distance limit (the same per-cluster
        # limit the building clustering used for this cluster).
        distance_limit = self.dbc.greenfield_distance_limit(matrix_connection_points, MAX_GREENFIELD_TRAFO_DISTANCE)
        feasible_candidate_ids = np.flatnonzero(dist_mat.max(axis=1) <= distance_limit)
        if len(feasible_candidate_ids) > 0:
            min_localid = feasible_candidate_ids[np.argmin(total_load_per_vertice[feasible_candidate_ids])]
            if GREENFIELD_TRAFO_POSITION_TOLERANCE > 0:
                costs = total_load_per_vertice[feasible_candidate_ids]
                eligible = sorted(feasible_candidate_ids[costs <= (1 + GREENFIELD_TRAFO_POSITION_TOLERANCE) * costs.min()],
                                  key=lambda localid: localid2vid[localid])
                # Seeded per cluster, so a rerun of the same configuration places the same stations.
                rng = np.random.default_rng([K_MEANS_SEED, int(plz), int(kcid), int(bcid)])
                min_localid = eligible[int(rng.integers(len(eligible)))]
        else:
            min_localid = int(np.argmin(total_load_per_vertice))
            self.logger.warning(
                f"Greenfield transformer placement for PLZ {plz}, KCID {kcid}, BCID {bcid} has no candidate within "
                f"the greenfield distance limit of {distance_limit:.0f} m; falling back to weighted minimum."
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
            the buildings, the consumer categories (fetched if not given), the
            consumer vertices, the street-side connection nodes (vertices that are
            not consumers) and the routed path of each vertex to the transformer.
        """
        vertices_dict, ont_vertice, paths_to_transformer = (
            self.dbc.get_vertices_from_bcid(plz, kcid, bcid)
        )
        vertices_list = list(vertices_dict.keys())

        buildings_df = self.dbc.get_buildings_from_bcid(plz, kcid, bcid)
        if consumer_df is None:
            consumer_df = self.dbc.get_consumer_categories()
        consumer_list = buildings_df.vertice_id.to_list()
        consumer_list = list(dict.fromkeys(consumer_list))  # removing duplicates

        connection_nodes = [i for i in vertices_list if i not in consumer_list]

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

    def install_cables(self):
        """Build, validate and store the electrical network of every grid of the PLZ.

        For each grid (``kcid``, ``bcid``) of ``grid_result``:

        1. load its buildings, routed distances and consumer loads,
        2. create buses, the station transformer and the snapshot loads on the
           configured electrical backend (:class:`CableInstaller`),
        3. plan the feeder branches and size the feeder cables
           (:mod:`pylovo.feeder_planning`), then create the feeder lines,
        4. size and create the service cables branch by branch,
        5. write the lines to ``lines_result`` and rebuild the GIS helper rows,
        6. run the validation power flow and store the network (:meth:`save_net`).
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
        consumer_df = self.dbc.get_consumer_categories()
        node_coordinates = self.dbc.fetch_node_coordinates(self.plz)
        consumer_connection_mapping = self.dbc.fetch_consumer_connection_mapping(
            self.plz,
        )
        cables = self.dbc.fetch_cables()

        for kcid, bcid in cluster_list:
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

            # Create network components (the backend creates them in batches)
            with backend.batch():
                installer.create_lvmv_bus(self.plz, kcid, bcid)
                installer.create_transformer(self.plz, kcid, bcid)
                installer.create_connection_bus(connection_nodes, station_vertex=ont_vertice)
                installer.create_consumer_bus_and_load(
                    consumer_list, powerflow_snapshot_components
                )

            self.logger.debug(
                f"Backend network initialized (buses={backend.get_component_count('buses')}, "
                f"loads={backend.get_component_count('loads')}, "
                f"transformer_rated_power={transformer_rated_power} kVA)"
            )

            # First finalize the split topology, then size every feeder segment on
            # the resulting tree so shared prefixes carry the full downstream load.
            branches = feeder_planning.plan_feeder_branches(
                connection_nodes,
                vertices_dict,
                ont_vertice,
                self._path_to_transformer_lookup(paths_to_transformer, ont_vertice),
                buildings_df,
                consumer_df,
                self.logger,
            )
            feeder_design = feeder_planning.size_feeder_tree(
                installer,
                branches,
                ont_vertice,
                vertices_dict,
                buildings_df,
                consumer_df,
                self.logger,
                self.plz,
            )
            with backend.batch():
                material_length_by_cable_km = self._install_feeder_lines(
                    installer,
                    branches,
                    feeder_design,
                    buildings_df,
                    consumer_df,
                    vertices_dict,
                    ont_vertice,
                    material_length_by_cable_km,
                    kcid,
                    bcid,
                )

                service_diagnostics = []
                for branch in branches:
                    material_length_by_cable_km, branch_service_diagnostics = (
                        installer.install_consumer_cables(
                            self.plz,
                            bcid,
                            kcid,
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

            # GIS helper SQL must see every persisted feeder/service line in the same
            # transaction, so flush exactly once before constructing visualization rows.
            installer.flush_line_records(self.plz, kcid, bcid)

            split_visualization_edges = feeder_planning.split_visualization_edges(
                branches, ont_vertice
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

            powerflow_status = self.save_net(
                backend,
                kcid,
                bcid,
                planning_diagnostics=planning_diagnostics,
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
            f"voltage_band_violations={voltage_violation_count}"
        )

    def save_net(
        self,
        backend: IElectricalBackend,
        kcid,
        bcid,
        planning_diagnostics: dict[str, float | bool | int] | None = None,
    ) -> str:
        """
        Validate the synthetic transformer-coincident operating point and save the grid.

        Runs the power flow of the snapshot loads with the station voltage of
        :mod:`pylovo.station_voltage` (LV busbar at ``LV_REFERENCE_VOLTAGE_PU``), classifies it as
        ``converged``, ``voltage_violation`` (outside ``POWER_FLOW_VOLTAGE_LIMITS``) or
        ``not_converged``, and stores the network JSON with the planning and
        voltage-drop diagnostics in ``grid_result`` (plus the SQL network tables for
        pandapower, and a JSON file if ``SAVE_GRID_FOLDER``). A grid is stored even
        if the power flow fails.

        Args:
            backend: Backend holding the finished network.
            kcid: K-means cluster ID
            bcid: Building cluster ID
            planning_diagnostics: Feeder and service planning diagnostics.

        Returns:
            The power-flow status.
        """
        # Validate grid with power flow before saving
        powerflow_status = "not_converged"
        voltage_drop_diagnostics = {
            "max_feeder_voltage_drop_pu": None,
            "max_service_voltage_drop_pu": None,
            "max_total_lv_voltage_drop_pu": None,
        }
        if planning_diagnostics is None:
            planning_diagnostics = {
                "ampacity_max_feeder_voltage_drop_percent": None,
                "selected_max_feeder_voltage_drop_percent": None,
                "feeder_voltage_drop_limit_met": None,
                "ampacity_max_service_voltage_drop_percent": None,
                "selected_max_service_voltage_drop_percent": None,
                "service_voltage_drop_limit_met": None,
                "service_voltage_upgraded_count": None,
                "long_service_connection_count": None,
                "max_total_design_voltage_drop_percent": None,
            }
        try:
            self.logger.debug(
                "Running synthetic transformer-coincident validation operating point "
                f"for kcid={kcid}, bcid={bcid}."
            )
            station = solve_validation_power_flow(backend, LV_REFERENCE_VOLTAGE_PU, logger=self.logger)
            converged = station.converged
            if station.applied:
                self.logger.debug(f"Station voltage for kcid={kcid}, bcid={bcid}: {station.describe()}")
            if converged:
                metrics = backend.get_circuit_metrics()
                min_voltage_pu = metrics.get("min_voltage_pu")
                max_voltage_pu = metrics.get("max_voltage_pu")

                if ELECTRICAL_BACKEND == "pandapower" and getattr(backend, "net", None) is not None:
                    net = backend.net
                    lv_buses = net.bus.index[net.bus.name == "LVbus 1"]
                    consumer_buses = set(
                        net.bus.index[
                            net.bus.name.fillna("").str.startswith("Consumer Nodebus ")
                        ]
                    )
                    service_lines = net.line.loc[net.line.to_bus.isin(consumer_buses)]
                    if len(lv_buses) == 1 and not service_lines.empty:
                        lv_voltage_pu = float(net.res_bus.at[int(lv_buses[0]), "vm_pu"])
                        feeder_drops = []
                        service_drops = []
                        total_drops = []
                        for line in service_lines.itertuples():
                            connection_voltage_pu = float(net.res_bus.at[line.from_bus, "vm_pu"])
                            consumer_voltage_pu = float(net.res_bus.at[line.to_bus, "vm_pu"])
                            feeder_drops.append(lv_voltage_pu - connection_voltage_pu)
                            service_drops.append(connection_voltage_pu - consumer_voltage_pu)
                            total_drops.append(lv_voltage_pu - consumer_voltage_pu)
                        voltage_drop_diagnostics = {
                            "max_feeder_voltage_drop_pu": max(feeder_drops),
                            "max_service_voltage_drop_pu": max(service_drops),
                            "max_total_lv_voltage_drop_pu": max(total_drops),
                        }

                voltage_out_of_band = False
                if min_voltage_pu is not None and min_voltage_pu < POWER_FLOW_MIN_VM_PU:
                    voltage_out_of_band = True
                if max_voltage_pu is not None and max_voltage_pu > POWER_FLOW_MAX_VM_PU:
                    voltage_out_of_band = True

                if voltage_out_of_band:
                    powerflow_status = "voltage_violation"
                    self.logger.warning(
                        "Synthetic transformer-coincident validation power flow converged "
                        f"but violated the voltage band for kcid={kcid}, bcid={bcid} "
                        f"(min_vm_pu={min_voltage_pu}, max_vm_pu={max_voltage_pu}, "
                        f"allowed=[{POWER_FLOW_MIN_VM_PU}, {POWER_FLOW_MAX_VM_PU}])."
                    )
                else:
                    self.logger.info(
                        "Synthetic transformer-coincident validation power flow converged "
                        f"for kcid={kcid}, bcid={bcid}"
                    )
                    powerflow_status = "converged"
            else:
                self.logger.warning(
                    "Synthetic transformer-coincident validation power flow did NOT converge "
                    f"for kcid={kcid}, bcid={bcid}"
                )
                powerflow_status = "not_converged"
        except Exception as e:
            self.logger.warning(
                "Synthetic transformer-coincident validation power flow failed "
                f"for kcid={kcid}, bcid={bcid}: {e}"
            )

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
            **planning_diagnostics,
            **voltage_drop_diagnostics,
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
