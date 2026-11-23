"""Stage 3 of the LV generation: the supply area (``bcid``) and the station of every grid.

:class:`StationPlanner` works through the ``kcid`` of a postcode. Existing stations take the buildings around
them first (brownfield, negative ``bcid``); the remaining buildings are split into greenfield areas
(:class:`~pylovo.lv_grid.greenfield_areas.GreenfieldAreas`), each of which gets a station position and a rating.
"""

import numpy as np
import pandas as pd  # type: ignore

from pylovo import supply_areas, utils
from pylovo.config_loader import (
    GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS,
    GREENFIELD_TRAFO_POSITION_TOLERANCE,
    K_MEANS_SEED,
    MAX_BROWNFIELD_TRAFO_DISTANCE,
    MAX_GREENFIELD_TRAFO_DISTANCE,
    MERGE_GREENFIELD_CLUSTERS,
)
from pylovo.lv_grid.context import GenerationContext, Stage
from pylovo.lv_grid.greenfield_areas import GreenfieldAreas


class StationPlanner(Stage):
    """Plan the supply areas and stations of all ``kcid`` of the postcode (:meth:`run`)."""

    def __init__(self, context: GenerationContext):
        super().__init__(context)
        self.areas = GreenfieldAreas(context)

    def run(self):
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
                self.areas.split(self.plz, kcid)
                self.logger.debug(f"kcid{kcid} building clusters finished")

            # Case 2: Transformers present
            else:
                self.logger.debug(f"kcid{kcid} has {len(transformers)} transformers")
                # Create brownfield building clusters with existing transformers
                self.assign_to_existing_stations(self.plz, kcid, transformers)

                # Buildings no existing station takes get greenfield stations; a single
                # remaining connection point becomes its own grid instead of being dropped
                if self.dbc.count_kmean_cluster_consumers(kcid) > 0:
                    self.areas.split(self.plz, kcid)
                    if MERGE_GREENFIELD_CLUSTERS and GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS is not None:
                        self.merge_small_areas_into_stations(self.plz, kcid)
                else:
                    self.dbc.delete_isolated_building(self.plz, kcid)
                self.logger.debug("Remaining building clustering finished")

            # Process unfinished clusters
            for bcid in self.dbc.get_greenfield_bcids(self.plz, kcid):
                # Transformer positioning for greenfield clusters
                if bcid >= 0:
                    self.place_station(self.plz, kcid, bcid)
                    self.logger.debug(f"Transformer positioning for kcid{kcid}, bcid{bcid} finished")
                    self.dbc.update_transformer_rated_power(self.plz, kcid, bcid)
                    self.logger.debug("Transformer_rated_power in grid_result updated.")

    def assign_to_existing_stations(self, plz: int, kcid: int, transformer_list: list) -> None:
        """
        Assign buildings to the existing transformers and store them as bcid in buildings_tem.

        A consumer may only join its nearest transformer (routed, closer than
        ``MAX_BROWNFIELD_TRAFO_DISTANCE``; transformers at the same distance all count): when that
        one is full, the consumer is left for greenfield instead of joining a farther transformer
        behind or through the area of the full one. The pairs are visited from the shortest routed
        distance up; a consumer joins the transformer unless the coincident load would exceed the
        transformer's imported rating or, without one, reach the largest allowed catalogue rating
        (after ``TRANSFORMER_PLANNING_UTILIZATION``), and the first consumer that does not fit
        marks the transformer as full. A consumer on the street path between another consumer and
        its nearest transformer has the same nearest transformer (apart from ties) and is closer to
        it, so it is visited first: the area of each transformer is connected along the streets.
        Every used transformer becomes a bcid with a negative id (-1, -2, ...) and keeps its
        imported rating or gets the smallest sufficient catalogue rating; unused transformers are
        removed. Unassigned consumers stay for greenfield clustering.

        Args:
            plz: Postal code
            kcid: K-means cluster ID
            transformer_list: List of transformer IDs
        """
        self.logger.info(f"{len(transformer_list)} Transformers found for kcid {kcid}")
        buildings = self.dbc.get_buildings_from_kcid(kcid)
        consumer_cat_df = self.consumer_categories
        loads = utils.CoincidentLoads(buildings, consumer_cat_df)

        # Get cost dataframe between consumers and transformers
        cost_df = self.dbc.get_consumer_to_transformer_df(kcid, transformer_list)
        # Keep connections shorter than MAX_BROWNFIELD_TRAFO_DISTANCE, nearest first (the costs are whole
        # metres, so ties are common: then the lower consumer and station vertex, not the routing row order)
        cost_df = cost_df[cost_df["agg_cost"] < MAX_BROWNFIELD_TRAFO_DISTANCE].sort_values(
            by=["agg_cost", "start_vid", "end_vid"])
        if not cost_df.empty:
            # only each consumer's nearest station: when it is full, the consumer is left for greenfield
            # instead of joining a farther station that still has capacity
            cost_df = cost_df[cost_df["agg_cost"] <= cost_df.groupby("start_vid")["agg_cost"].transform("min")]

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
            # A catalogue rating must lie strictly above load / utilization (the rating pick below and the
            # greenfield split say the same), so a load exactly at the largest rating already fills the station.
            if (float(sim_load) > known) if known is not None else (
                    utils.planned_station_load(float(sim_load)) >= max(possible_transformers)):
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
                # the loop above filled the station only below its largest rating, so a rating always fits
                transformer_rated_power = utils.smallest_rating_above(possible_transformers,
                                                                      utils.planned_station_load(float(sim_load)))

            # Update database with new building cluster
            self.dbc.update_building_cluster(transformer_id, pre_result_dict[transformer_id], building_cluster_count, kcid,
                plz, transformer_rated_power)

        self.logger.info("Brownfield clusters completed")

    def merge_small_areas_into_stations(self, plz: int, kcid: int) -> int:
        """Let the small greenfield clusters beside existing stations join the nearest one.

        Part of ``MERGE_GREENFIELD_CLUSTERS``: after the greenfield merge, a greenfield cluster
        that still has at most ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS`` loaded buildings joins
        the existing station with the shortest routed distance from one of its connection
        points, if that distance is below ``MAX_BROWNFIELD_TRAFO_DISTANCE`` (the distance within
        which existing stations take buildings anyway) and the station is not overloaded then.
        The planning reserve of ``TRANSFORMER_PLANNING_UTILIZATION`` may be used: the brownfield
        step fills stations nearest first, so a station can be full before the last few
        buildings at the edge of its area come, and real stations run well below their rating.
        The coincident load must stay within the rating, though: an imported rating, or else
        the largest catalogue rating. A cluster that would overload its nearest station tries
        the next one. An imported rating is kept; a station without one gets the smallest
        catalogue rating above its new load (after ``TRANSFORMER_PLANNING_UTILIZATION``), or
        the largest. The station's area and the cluster must form one piece among the buildings of
        the other grids (:func:`pylovo.supply_areas.is_contiguous`), otherwise the next station is tried.

        Returns:
            The number of clusters that joined a station.
        """
        stations = self.dbc.get_brownfield_stations(plz, kcid)
        if not stations:
            return 0
        positions = self.dbc.get_consumer_positions(kcid)
        bcid_by_vertex = {vertex: bcid for bcid, vertex in stations.items()}
        known_capacities = self.dbc.get_brownfield_transformer_capacity_map(list(stations.values()))
        capacities, _ = self.dbc.get_transformer_data(self.dbc.get_settlement_type_from_plz(plz))
        consumer_cat_df = self.consumer_categories
        merged = 0
        # get_greenfield_bcids lists every unfinished grid, the brownfield ones (bcid < 0) too
        for bcid in [b for b in self.dbc.get_greenfield_bcids(plz, kcid) if b >= 0]:
            buildings = self.dbc.get_buildings_from_bcid(plz, kcid, bcid)
            if buildings.empty or len(buildings) > GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS:
                continue
            points = sorted(utils.planning_nodes(buildings).dropna().astype(int).unique().tolist())
            costs = self.dbc.get_connection_point_to_transformer_costs(points, list(stations.values()))
            costs = costs[costs["agg_cost"] < MAX_BROWNFIELD_TRAFO_DISTANCE]
            if costs.empty:
                continue
            # each station once, nearest first (from the nearest connection point of the cluster)
            candidates = costs.sort_values(by=["agg_cost", "end_vid"]).drop_duplicates("end_vid")["end_vid"]
            for vertex in map(int, candidates):
                target = bcid_by_vertex[vertex]
                station_buildings = self.dbc.get_buildings_from_bcid(plz, kcid, target)
                joined = pd.concat([station_buildings, buildings])
                load = utils.simultaneous_peak_load(
                    joined, consumer_cat_df, utils.planning_nodes(joined).dropna().astype(int).unique().tolist())
                if load > known_capacities.get(vertex, capacities.max()):
                    continue   # the station would be overloaded: try the next one
                joined_area = positions["bcid"].isin([bcid, target])
                if not supply_areas.is_contiguous(positions.loc[joined_area, ["x", "y"]],
                                                  positions.loc[~joined_area & positions["bcid"].notna(), ["x", "y"]]):
                    continue   # the station's area and the cluster would be in several pieces
                positions.loc[positions["bcid"] == bcid, "bcid"] = target
                self.dbc.move_building_cluster(plz, kcid, bcid, target)
                if vertex not in known_capacities:
                    rating = utils.smallest_rating_above(capacities, utils.planned_station_load(load))
                    self.dbc._set_transformer_rated_power(
                        plz, kcid, target, rating if rating is not None else int(capacities.max()))
                merged += 1
                break
        if merged:
            self.logger.info(f"Greenfield merge for PLZ {plz}, KCID {kcid}: {merged} small clusters joined their "
                             f"nearest existing station (max_buildings={GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS})")
        return merged

    def place_station(self, plz, kcid, bcid):
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
