"""Stage 3 of the LV generation, greenfield part: the supply areas of new stations.

:class:`GreenfieldAreas` splits the buildings of a ``kcid`` that no existing station supplies into supply areas
(``bcid``) that one transformer can supply, and merges neighbouring areas again where the result stays feasible.
"""

import math

import numpy as np
import pandas as pd  # type: ignore
from scipy.cluster.hierarchy import linkage
from scipy.spatial.distance import squareform

from pylovo import supply_areas, utils
from pylovo.config_loader import (
    GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS,
    GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA,
    MAX_GREENFIELD_TRAFO_DISTANCE,
    MERGE_GREENFIELD_CLUSTERS,
)
from pylovo.lv_grid.context import Stage


class GreenfieldAreas(Stage):
    """Split the unassigned buildings of a ``kcid`` into feasible greenfield supply areas (:meth:`split`)."""

    def split(self, plz: int, kcid: int, first_bcid: int = 1, merge: bool = True,
              replace: bool = True, first_cut: int = 2) -> list[int]:
        """Split the unassigned buildings of a kcid into greenfield transformer areas (bcids).

        Average-linkage hierarchical clustering on the routed distance matrix is cut
        into two clusters at a time; a cluster that exceeds the largest allowed
        transformer (after ``TRANSFORMER_PLANNING_UTILIZATION``), has no station
        position within the greenfield distance limit or lies in several pieces among the
        buildings that other grids supply already (:mod:`pylovo.supply_areas`) is split again,
        until all clusters are feasible. Optionally neighbouring clusters are merged again
        (``MERGE_GREENFIELD_CLUSTERS``, limited to small clusters with
        ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS``). The bcids are numbered from ``first_bcid`` by
        their smallest vertex id and written to ``grid_result`` with their rating.

        Args:
            plz: Postal code
            kcid: K-means cluster ID
            first_bcid: bcid of the first new area (the voltage repair adds areas behind the existing ones).
            merge: Merge neighbouring feasible areas as configured; the voltage repair does not, as the
                areas it splits were too large for their feeders.
            replace: Delete the greenfield ``grid_result`` rows of the kcid first (a kcid clustered from scratch);
                the voltage repair keeps them.
            first_cut: Number of clusters of the first cut of the tree: 2 for a kcid and for a greenfield area
                that the voltage repair splits, 1 for the buildings an existing station gave up (one area
                if it is feasible).

        Returns:
            The new bcids.
        """
        # Get data needed for clustering
        buildings = self.dbc.get_buildings_from_kcid(kcid)
        consumer_cat_df = self.consumer_categories
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
                total_sim_load = utils.planned_station_load(utils.simultaneous_peak_load(buildings, consumer_cat_df, vertices))
                transformer_size = utils.smallest_rating_above(transformer_capacities, total_sim_load)
                if transformer_size is None:
                    transformer_size = int(math.ceil(total_sim_load))
                if replace:
                    self.dbc.clear_grid_result_in_kmean_cluster(plz, kcid)
                self.dbc.upsert_bcid(plz, kcid, first_bcid, vertices=vertices, transformer_rated_power=transformer_size)
                self.logger.info(
                    f"BCID dimensioning fallback for PLZ {plz}, KCID {kcid}: "
                    f"single connection point assigned to BCID {first_bcid} with transformer {transformer_size} kVA"
                )
                return [first_bcid]
            self.logger.warning(
                f"Skipped BCID dimensioning for PLZ {plz}, KCID {kcid}: "
                f"empty distance vector for {len(vertices)} active connection point(s)"
            )
            # A kcid without a grid_result row is picked again by StationPlanner.run, and the
            # kcids after it would never be clustered: drop its unassigned rows (recorded as excluded).
            self.dbc.delete_isolated_building(plz, kcid)
            return []

        # an area in several pieces among the buildings of other grids is split again
        contiguity = supply_areas.AreaContiguity(self.dbc.get_consumer_positions(kcid))

        # Initialize hierarchical clustering
        Z = linkage(dist_vector, method="average")
        valid_cluster_dict = {}
        invalid_trans_cluster_dict = {}
        cluster_amount = first_cut
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
                contiguity=contiguity,
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
        if merge:
            valid_cluster_dict = self._merge_feasible(
                valid_cluster_dict,
                buildings,
                consumer_cat_df,
                transformer_capacities,
                dist_mat,
                vid2localid,
                plz,
                kcid,
                contiguity,
            )

        # Reorder bcids for consistency
        valid_cluster_dict = self._number(valid_cluster_dict, first_bcid)

        # Save results to database
        if replace:
            self.dbc.clear_grid_result_in_kmean_cluster(plz, kcid)
        for bcid, cluster_data in valid_cluster_dict.items():
            self.dbc.upsert_bcid(plz, kcid, bcid, vertices=cluster_data[0],
                                         transformer_rated_power=cluster_data[1])

        self.logger.debug(f"bcids for plz {plz} kcid {kcid} found...")
        return list(valid_cluster_dict)

    def _merge_feasible(
        self,
        cluster_dict: dict,
        buildings: pd.DataFrame,
        consumer_cat_df: pd.DataFrame,
        transformer_capacities: np.ndarray,
        dist_mat: np.ndarray,
        vid2localid: dict[int, int],
        plz: int,
        kcid: int,
        contiguity: supply_areas.AreaContiguity | None = None,
    ) -> dict:
        """Merge neighboring undersized greenfield clusters if still feasible.

        The load-constrained hierarchical split prevents oversized transformer
        areas. This conservative pass only recombines already valid neighboring
        clusters when the merged area still fits a configured single transformer
        and satisfies its own greenfield distance limit (the per-cluster draw of
        ``greenfield_distance_limit`` that the split and the station placement use). With
        ``GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS`` a pair is only merged if its
        smaller cluster has at most that many loaded buildings, so small
        fragments join a neighbour while larger clusters stay as split. With ``contiguity`` the merged
        area must also form one piece among the buildings of other grids.
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
        planning_points = utils.planning_nodes(buildings)

        while True:
            best_candidate = None
            cluster_items = list(merged_clusters.items())
            building_counts = {
                cluster_id: int(planning_points.isin(vertices).sum())
                for cluster_id, (vertices, _transformer_size) in cluster_items
            }

            for left_index in range(len(cluster_items)):
                left_id, (left_vertices, _left_transformer) = cluster_items[left_index]
                left_local_ids = [vid2localid[vid] for vid in left_vertices if vid in vid2localid]
                if not left_local_ids:
                    continue

                for right_id, (right_vertices, _right_transformer) in cluster_items[left_index + 1:]:
                    if frozenset((left_id, right_id)) not in neighboring_pairs:
                        continue
                    if (
                        GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS is not None
                        and min(building_counts[left_id], building_counts[right_id])
                        > GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS
                    ):
                        continue

                    right_local_ids = [vid2localid[vid] for vid in right_vertices if vid in vid2localid]
                    if not right_local_ids:
                        continue

                    combined_vertices = list(dict.fromkeys(left_vertices + right_vertices))
                    combined_load = utils.planned_station_load(
                        utils.simultaneous_peak_load(buildings, consumer_cat_df, combined_vertices))
                    merged_rating = utils.smallest_rating_above(merge_capacities, combined_load)
                    if merged_rating is None:
                        continue

                    if not self.dbc.cluster_has_feasible_transformer_position(
                        combined_vertices,
                        dist_mat,
                        vid2localid,
                        self.dbc.greenfield_distance_limit(combined_vertices, MAX_GREENFIELD_TRAFO_DISTANCE),
                    ):
                        continue
                    if contiguity is not None and not contiguity(combined_vertices):
                        continue

                    nearest_distance = float(
                        dist_mat[np.ix_(left_local_ids, right_local_ids)].min()
                    )
                    candidate = (
                        nearest_distance,
                        combined_load,
                        merged_rating,
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
                f"merged {merge_count} neighboring cluster pairs, final_clusters={len(merged_clusters)}, "
                f"max_buildings_of_smaller_cluster={GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS}"
            )

        return dict(enumerate(merged_clusters.values()))

    def _number(self, cluster_dict: dict, start: int = 1) -> dict:
        """Renumber clusters from ``start`` in the order of their smallest vertex id.

        Partitions that are equal up to renaming therefore get the same bcids.

        Args:
            cluster_dict: ``{cluster id: (vertices, transformer rating)}``.
            start: The first bcid.

        Returns:
            ``{bcid: (vertices, transformer rating)}`` with bcids start, start + 1, ...
        """
        ordered_vertices = sorted(cluster_dict.items(), key = lambda cluster: min(cluster[1][0]))
        return {new_bcid: vertices for new_bcid, (_, vertices) in enumerate(ordered_vertices, start=start)}
