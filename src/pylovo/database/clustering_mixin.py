"""Queries and helpers that split a PLZ into k-means clusters (kcid) and building clusters (bcid)
and choose their transformers."""

import hashlib
import heapq
import math
import time
import warnings
from typing import Optional, Union

import numpy as np
import pandas as pd
import psycopg2
import scipy.sparse
from scipy.cluster.hierarchy import cut_tree
from scipy.sparse.csgraph import dijkstra

from pylovo import utils
from pylovo.config_loader import (
    K_MEANS_SEED,
    MAX_GREENFIELD_TRAFO_DISTANCE_STD,
    TARGET_EPSG,
    TRANSFORMER_MAPPING,
    TRANSFORMER_PLANNING_UTILIZATION,
    USE_DSO_TRANSFORMER_POSITIONS,
    USE_MANUAL_TRANSFORMER_POSITIONS,
    USE_OPEN_TRANSFORMER_POSITIONS,
    VERSION_ID,
)
from pylovo.database.base_mixin import WAYS_TEM_EDGES_SQL, BaseMixin
from pylovo.database.transformer_sources import SOURCE_ENABLED_SQL, source_params

warnings.simplefilter(action='ignore', category=UserWarning)

# Street distances between the distinct connection points of the loaded buildings of one kcid;
# {bcid_filter} selects one building cluster (the kcid matrix itself: street_distance_matrix).
_CONNECTION_POINT_COST_MATRIX_SQL = f"""
    SELECT *
    FROM pgr_dijkstraCostMatrix(
            {WAYS_TEM_EDGES_SQL},
            (SELECT array_agg(DISTINCT COALESCE(b.agg_connection_point, b.connection_point))
             FROM (SELECT *
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND {{bcid_filter}}
                     AND peak_load_in_kw != 0
                   ORDER BY COALESCE(agg_connection_point, connection_point)) AS b),
            false);"""


class ClusteringMixin(BaseMixin):
    """Clustering queries on ``buildings_tem`` and ``ways_tem`` and transformer sizing.

    ``kcid`` identifies a k-means cluster (a connected street component, split further if it
    is large), ``bcid`` a building cluster inside it that gets one transformer. Brownfield
    clusters (existing transformers) have negative ``bcid``, greenfield clusters non-negative ones.
    """

    @staticmethod
    def greenfield_distance_limit(connection_points, mean_limit: float) -> float:
        """Greenfield distance limit of one cluster, identified by its connection points.

        With MAX_GREENFIELD_TRAFO_DISTANCE_STD > 0 the limit is drawn from a normal
        distribution around ``mean_limit``, clipped to +/- 2 standard deviations. The
        draw is seeded by the cluster's own members, so every check of the same cluster
        (splitting, then station placement) sees the same limit.
        """
        if MAX_GREENFIELD_TRAFO_DISTANCE_STD <= 0 or mean_limit is None:
            return mean_limit
        members = np.asarray(sorted(int(p) for p in connection_points), dtype=np.int64)
        key = int.from_bytes(hashlib.sha256(members.tobytes()).digest()[:8], 'little')
        draw = np.random.default_rng([K_MEANS_SEED, key]).normal(mean_limit, MAX_GREENFIELD_TRAFO_DISTANCE_STD)
        spread = 2 * MAX_GREENFIELD_TRAFO_DISTANCE_STD
        return float(np.clip(draw, mean_limit - spread, mean_limit + spread))

    @staticmethod
    def cluster_has_feasible_transformer_position(
        vid_list: list[int],
        dist_mat: np.ndarray,
        vid2localid: dict[int, int],
        max_distance: float | None,
    ) -> bool:
        """Return whether any cluster connection point satisfies the max-distance limit."""
        if max_distance is None or max_distance <= 0 or len(vid_list) <= 1:
            return True

        local_ids = [vid2localid[vid] for vid in vid_list if vid in vid2localid]
        if len(local_ids) <= 1:
            return True

        cluster_dist_mat = dist_mat[np.ix_(local_ids, local_ids)]
        best_max_distance = float(cluster_dist_mat.max(axis=1).min())
        return best_max_distance <= max_distance

    @staticmethod
    def _cluster_adjacency_from_street_edges(
        cluster_dict: dict,
        street_edges: list[tuple[int, int, float]],
    ) -> set[frozenset[int]]:
        """Return cluster pairs whose graph-Voronoi territories share an edge."""
        cluster_ids = list(cluster_dict)
        cluster_rank = {cluster_id: rank for rank, cluster_id in enumerate(cluster_ids)}
        graph: dict[int, list[tuple[int, float]]] = {}
        normalized_edges = []
        for source, target, cost in street_edges:
            source = int(source)
            target = int(target)
            weight = float(cost)
            graph.setdefault(source, []).append((target, weight))
            graph.setdefault(target, []).append((source, weight))
            normalized_edges.append((source, target))

        distances: dict[int, float] = {}
        owners: dict[int, int] = {}
        queue = []
        for cluster_id, (vertices, _transformer_size) in cluster_dict.items():
            rank = cluster_rank[cluster_id]
            for vertex in vertices:
                vertex = int(vertex)
                current_owner = owners.get(vertex)
                if current_owner is None or rank < cluster_rank[current_owner]:
                    distances[vertex] = 0.0
                    owners[vertex] = cluster_id
                    heapq.heappush(queue, (0.0, rank, vertex, cluster_id))

        while queue:
            distance, rank, node, cluster_id = heapq.heappop(queue)
            if distance != distances.get(node) or cluster_id != owners.get(node):
                continue
            for neighbor, weight in graph.get(node, []):
                candidate_distance = distance + weight
                known_distance = distances.get(neighbor)
                known_owner = owners.get(neighbor)
                if (
                    known_distance is None
                    or candidate_distance < known_distance
                    or (
                        candidate_distance == known_distance
                        and rank < cluster_rank[known_owner]
                    )
                ):
                    distances[neighbor] = candidate_distance
                    owners[neighbor] = cluster_id
                    heapq.heappush(queue, (candidate_distance, rank, neighbor, cluster_id))

        neighboring_clusters = set()
        for source, target in normalized_edges:
            source_owner = owners.get(source)
            target_owner = owners.get(target)
            if source_owner is not None and target_owner is not None and source_owner != target_owner:
                neighboring_clusters.add(frozenset((source_owner, target_owner)))
        return neighboring_clusters

    def get_cluster_adjacency_from_street_graph(self, cluster_dict: dict) -> set[frozenset[int]]:
        """Return the pairs of clusters that are neighbours on the street graph of ``ways_tem``.

        Every street node belongs to the cluster with the shortest street distance to it
        (graph Voronoi partition); two clusters are neighbours if an edge joins their territories.

        Args:
            cluster_dict: ``{cluster_id: (vertex_ids, transformer_size)}``.

        Returns:
            Set of neighbouring cluster ID pairs.
        """
        self.cur.execute(
            """SELECT source, target, cost
               FROM ways_tem
               WHERE source IS NOT NULL
                 AND target IS NOT NULL
                 AND cost IS NOT NULL;"""
        )
        return self._cluster_adjacency_from_street_edges(cluster_dict, self.cur.fetchall())

    def get_connected_component(self) -> tuple[np.ndarray, np.ndarray]:
        """
        Read connected components from ways_tem in canonical order.

        pgRouting does not promise an ordering for either its result rows or
        its component labels. Canonicalizing the node lists here prevents
        component processing (and the resulting KCID numbering) from
        depending on the physical order of the temporary edge table.
        """
        component_query = f"""SELECT component, node
                             FROM pgr_connectedComponents({WAYS_TEM_EDGES_SQL})
                             ORDER BY component, node;"""
        self.cur.execute(component_query)
        data = self.cur.fetchall()
        return self._canonical_connected_components(data)

    @staticmethod
    def _canonical_connected_components(data) -> tuple[np.ndarray, np.ndarray]:
        """Normalize pgRouting component labels and node ordering."""
        nodes_by_component: dict[int, list[int]] = {}
        for component_id, node_id in data:
            nodes_by_component.setdefault(int(component_id), []).append(int(node_id))

        ordered_components = sorted(
            (sorted(nodes) for nodes in nodes_by_component.values()),
            key=lambda nodes: (nodes[0], nodes),
        )
        if not ordered_components:
            empty = np.asarray([], dtype=np.int64)
            return empty, empty.copy()

        component = np.concatenate(
            [np.full(len(nodes), index + 1, dtype=np.int64) for index, nodes in enumerate(ordered_components)]
        )
        node = np.concatenate([np.asarray(nodes, dtype=np.int64) for nodes in ordered_components])
        return component, node

    def count_no_kmean_buildings(self):
        """Return the number of loaded rows of ``buildings_tem`` without a kcid."""
        query = """SELECT COUNT(*)
                   FROM buildings_tem
                   WHERE peak_load_in_kw != 0
                     AND kcid ISNULL;"""
        self.cur.execute(query)
        count = self.cur.fetchone()[0]

        return count

    def count_connected_buildings(self, vertices: Union[list, tuple]) -> int:
        """Return the number of loaded consumer buildings at the given vertices.

        Args:
            vertices: Vertex IDs, typically all nodes of one connected component.
        """
        query = """SELECT COUNT(*)
                   FROM buildings_tem
                   WHERE vertice_id IN %(v)s
                     AND type != 'Transformer'
                     AND peak_load_in_kw != 0;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})
        count = self.cur.fetchone()[0]

        return count

    def delete_ways(self, vertices: list) -> None:
        """Delete the ways ending at the given vertices and the vertices themselves.

        Used for connected components with at most one consumer building. The vertices are
        deleted through the session view ``ways_tem_vertices_pgr``.

        Args:
            vertices: Vertex IDs of one connected component.
        """
        query = """DELETE
                   FROM ways_tem
                   WHERE target IN %(v)s;
        DELETE
        FROM ways_tem_vertices_pgr
        WHERE id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def get_connected_component_geometries(self, vertices: Union[list, tuple]) -> tuple[np.ndarray, np.ndarray]:
        """Return the vertex IDs and centroid coordinates of the loaded buildings of a component.

        Args:
            vertices: Vertex IDs of the connected component.

        Returns:
            ``(vertex_ids, coordinates)``: vertex IDs sorted ascending and an ``(n, 2)`` array of
            centroid coordinates in ``TARGET_EPSG``.
        """
        query = """
                SELECT vertice_id, ST_AsText(centroid) as wkt 
                FROM buildings_tem
                WHERE vertice_id IN %(v)s
                  AND peak_load_in_kw != 0
                ORDER BY vertice_id
                """
        self.cur.execute(query, {"v": tuple(map(int, vertices))})
        data = self.cur.fetchall()
        selected_vertices = np.array([x[0] for x in data])
        coordinates = np.float64(np.array([x[1].replace('POINT(', '').replace(')', '').split() for x in data]))

        return selected_vertices, coordinates

    def update_kmeans_cluster_multiple(self, vertices: np.ndarray, kcids: np.ndarray) -> None:
        """Assign k-means cluster IDs to the loaded buildings at the given vertices.

        Args:
            vertices: Vertex IDs of the buildings.
            kcids: kcid of each vertex, in the same order as ``vertices``.
        """
        query = """
                UPDATE buildings_tem
                SET kcid = %(k)s
                WHERE vertice_id IN %(v)s
                  AND peak_load_in_kw != 0;
                """
        for kcid in np.unique(kcids):
            self.cur.execute(query, {"k": int(kcid), "v": tuple(map(int, vertices[kcids == kcid]))})

    def update_kmeans_cluster(self, vertices: list) -> None:
        """Give the loaded buildings of a small component one new kcid (highest kcid + 1), without k-means.

        Args:
            vertices: Vertex IDs of the connected component.
        """
        query = """
                WITH maxk AS (SELECT MAX(kcid) AS max_k FROM buildings_tem)
                UPDATE buildings_tem
                SET kcid = (CASE
                                WHEN m.max_k ISNULL THEN 1
                                ELSE m.max_k + 1
                    END)
                FROM maxk AS m
                WHERE vertice_id IN %(v)s
                  AND peak_load_in_kw != 0;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    @staticmethod
    def _format_bytes(byte_count: int) -> str:
        """Format a byte count with a binary unit, e.g. ``1.5 MiB``."""
        units = ["B", "KiB", "MiB", "GiB", "TiB"]
        size = float(byte_count)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.1f} {unit}"
            size /= 1024

    def get_kcid_distance_matrix_stats(self, kcid: int) -> dict[str, int]:
        """Return pre-flight sizing stats for the KCID distance matrix query."""
        query = """SELECT COUNT(*) AS building_count,
                          COUNT(DISTINCT COALESCE(agg_connection_point, connection_point)) AS connection_point_count
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND bcid ISNULL
                     AND COALESCE(agg_connection_point, connection_point) IS NOT NULL
                     AND type != 'Transformer';"""
        self.cur.execute(query, {"k": kcid})
        building_count, connection_point_count = self.cur.fetchone()
        point_count = int(connection_point_count or 0)

        return {
            "building_count": int(building_count or 0),
            "connection_point_count": point_count,
            "estimated_pair_count": point_count * point_count,
            "estimated_dense_matrix_bytes": point_count * point_count * 8,
        }

    def get_distance_matrix_from_kcid(self, kcid: int) -> tuple[dict, np.ndarray, dict]:
        """Return the street-distance matrix between the connection points of a kcid's unclustered buildings.

        Args:
            kcid: K-means cluster ID.

        Returns:
            ``(localid2vid, dist_mat, vid2localid)`` as returned by ``street_distance_matrix``.
        """
        stats = self.get_kcid_distance_matrix_stats(kcid)
        self.logger.debug(
            "KCID %s distance-matrix preflight: buildings=%s, connection_points=%s, "
            "estimated_pairs=%s, dense_matrix_estimate=%s",
            kcid,
            stats["building_count"],
            stats["connection_point_count"],
            stats["estimated_pair_count"],
            self._format_bytes(stats["estimated_dense_matrix_bytes"]),
        )

        self.cur.execute(
            """SELECT DISTINCT COALESCE(agg_connection_point, connection_point)
               FROM buildings_tem
               WHERE kcid = %(k)s
                 AND bcid ISNULL
                 AND peak_load_in_kw != 0
                 AND COALESCE(agg_connection_point, connection_point) IS NOT NULL;""",
            {"k": kcid},
        )
        return self.street_distance_matrix([row[0] for row in self.cur.fetchall()])

    def street_distance_matrix(self, points: list[int], chunk_size: int = 256) -> tuple[dict, np.ndarray, dict]:
        """Return the street-distance matrix between vertices of ``ways_tem``, computed in memory.

        Gives the result of ``calculate_cost_arr_dist_matrix`` for ``pgr_dijkstraCostMatrix`` over
        ``points`` (undirected): vertices ordered by id, without the points that reach no other
        point, costs truncated to whole metres, 0 for pairs without a path. Dijkstra's distances do
        not depend on the order of relaxation (non-negative weights, rounding is monotonic), so they
        are the same as pgRouting's; transferring the n x n rows of pgRouting took longer than the
        search itself for kcids of a thousand points.

        Args:
            points: Vertex IDs.
            chunk_size: Number of source vertices per Dijkstra call (memory: chunk x vertices floats).

        Returns:
            ``(localid2vid, dist_matrix, vid2localid)``.
        """
        self.cur.execute("""SELECT source, target, cost, reverse_cost
                            FROM ways_tem
                            WHERE source IS NOT NULL AND target IS NOT NULL;""")
        edges = np.asarray(self.cur.fetchall(), dtype=float).reshape(-1, 4)
        vertices = np.unique(edges[:, :2])
        source = np.searchsorted(vertices, edges[:, 0])
        target = np.searchsorted(vertices, edges[:, 1])
        # Undirected: an edge is usable with the smaller of its non-negative costs (as in pgRouting);
        # parallel edges keep their minimum instead of being summed by the sparse matrix.
        weight = np.fmin(np.where(edges[:, 2] >= 0, edges[:, 2], np.inf),
                         np.where(edges[:, 3] >= 0, edges[:, 3], np.inf))
        keep = (source != target) & np.isfinite(weight)
        pairs = pd.DataFrame({"a": np.minimum(source, target)[keep], "b": np.maximum(source, target)[keep],
                              "w": weight[keep]}).groupby(["a", "b"], sort=True)["w"].min()
        graph = scipy.sparse.csr_matrix(
            (pairs.to_numpy(), (pairs.index.get_level_values("a"), pairs.index.get_level_values("b"))),
            shape=(len(vertices), len(vertices)),
        )

        points = np.asarray(sorted(int(p) for p in points), dtype=np.int64)
        points = points[np.isin(points, vertices)]
        point_index = np.searchsorted(vertices, points)
        distances = np.vstack(
            [dijkstra(graph, directed=False, indices=point_index[i:i + chunk_size])[:, point_index]
             for i in range(0, len(point_index), chunk_size)]
        ) if len(point_index) else np.zeros((0, 0))
        np.fill_diagonal(distances, np.inf)
        connected = np.isfinite(distances).any(axis=1)
        distances = distances[np.ix_(connected, connected)]
        dist_matrix = np.where(np.isfinite(distances), distances, 0).astype(np.int32).astype(float)

        localid2vid = dict(enumerate(points[connected].astype(np.int32)))
        vid2localid = {y: x for x, y in localid2vid.items()}
        return localid2vid, dist_matrix, vid2localid

    def calculate_cost_arr_dist_matrix(self, costmatrix_query: str, params: dict) -> tuple[dict, np.ndarray, dict]:
        """Run a ``pgr_dijkstraCostMatrix`` query and return it as a dense matrix.

        Costs are truncated to whole metres (``int32``); pairs without a path stay 0.

        Args:
            costmatrix_query: Query returning ``start_vid``, ``end_vid`` and ``agg_cost``.
            params: Query parameters.

        Returns:
            ``(localid2vid, dist_matrix, vid2localid)``: the mapping from matrix index to vertex ID,
            the square distance matrix and the reverse mapping.
        """
        st = time.time()
        cost_df = pd.read_sql_query(costmatrix_query, con=self.conn, params=params,
                                    dtype={"start_vid": np.int32, "end_vid": np.int32, "agg_cost": np.int32}, )
        cost_arr = cost_df.to_numpy()
        et = time.time()
        self.logger.debug(f"Elapsed time for SQL to cost_arr: {et - st}")
        localid2vid = dict(enumerate(cost_df["start_vid"].unique()))
        vid2localid = {y: x for x, y in localid2vid.items()}

        # Square distance matrix
        dist_matrix = np.zeros([len(localid2vid), len(localid2vid)])
        st = time.time()
        local_ids = pd.Index(list(localid2vid.values()))
        start_ids = local_ids.get_indexer(cost_arr[:, 0])
        end_ids = local_ids.get_indexer(cost_arr[:, 1])
        if (end_ids < 0).any():  # every end vertex is also a start vertex (undirected costs)
            raise KeyError(cost_arr[np.flatnonzero(end_ids < 0)[0], 1])
        dist_matrix[start_ids, end_ids] = cost_arr[:, 2]
        et = time.time()
        self.logger.debug(f"Elapsed time for dist_matrix creation: {et - st}")
        return localid2vid, dist_matrix, vid2localid

    def generate_load_vector_for_connection_points(
        self, kcid: int, bcid: int, connection_points: list[int]
    ) -> tuple[np.ndarray, list[int]]:
        """Return the summed peak load of a building cluster per connection point.

        Args:
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            connection_points: Connection points in the order of the returned vector.

        Returns:
            ``(loads, missing_points)``: loads in kW aligned to ``connection_points`` (0 for points
            without load) and the loaded connection points that are not in ``connection_points``.
        """
        query = """SELECT COALESCE(agg_connection_point, connection_point)::int AS connection_point,
                          SUM(peak_load_in_kw)::float AS load_kw
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND bcid = %(b)s
                     AND peak_load_in_kw != 0
                   GROUP BY COALESCE(agg_connection_point, connection_point);"""
        self.cur.execute(query, {"k": kcid, "b": bcid})
        load_by_point = {int(point): float(load) for point, load in self.cur.fetchall()}
        ordered_points = [int(point) for point in connection_points]
        loads = np.asarray([load_by_point.get(point, 0.0) for point in ordered_points])
        missing_points = sorted(set(load_by_point).difference(ordered_points))

        return loads, missing_points

    def load_constrained_hierarchical_clustering(self, Z: np.ndarray, cluster_amount: int, localid2vid: dict, buildings: pd.DataFrame,
            consumer_cat_df: pd.DataFrame, transformer_capacities: np.ndarray, double_trans: np.ndarray | None = None,
            dist_mat: np.ndarray | None = None, vid2localid: dict[int, int] | None = None,
            max_transformer_distance: float | None = None, ) -> tuple[
        dict, dict, int]:
        """
        Attempts to cluster buildings based on hierarchical clustering linkage matrix Z and assigns transformers.

        This function cuts the hierarchical tree to form `cluster_amount` clusters. For each cluster, it calculates
        the simultaneous peak load. It then assigns the smallest standard transformer that covers it, based on
        the load and available capacities. If a cluster's load exceeds the maximum single transformer capacity
        and has enough buildings, it is marked as invalid (too big).

        Args:
            Z (np.ndarray): The linkage matrix from hierarchical clustering (scipy.cluster.hierarchy.linkage).
            cluster_amount (int): The number of clusters to form.
            localid2vid (dict): Mapping from local clustering indices to building vertice IDs.
            buildings (pd.DataFrame): DataFrame containing building information (loads, types, etc.).
            consumer_cat_df (pd.DataFrame): DataFrame containing consumer category definitions (simultaneity factors).
            transformer_capacities (np.ndarray): Array of available single transformer capacities (sorted).
            double_trans: Unused. Callers passed twice the two largest capacities; a double station could
                never win the comparison it fed (a single transformer always fits below the largest
                rating), so the comparison was removed. Kept so positional callers keep working.
            dist_mat (np.ndarray, optional): Pairwise street-distance matrix for the cluster candidates.
            vid2localid (dict[int, int], optional): Reverse mapping for ``dist_mat`` lookup.
            max_transformer_distance (float, optional): Maximum allowed distance from a greenfield
                transformer point to any connection point in the cluster.

        Returns:
            tuple[dict, dict, int]:
                - invalid_cluster_dict (dict): Clusters that are too big (load > max single capacity & >= 5 buildings).
                  Key: cluster_id, Value: list of vertice IDs.
                - cluster_dict (dict): Valid clusters with assigned transformers.
                  Key: cluster_id, Value: tuple(list of vertice IDs, assigned transformer capacity).
                - cluster_count (int): The actual number of clusters formed.
        """
        flat_groups = cut_tree(Z, n_clusters=cluster_amount)
        cluster_ids = np.unique(flat_groups)
        cluster_count = len(cluster_ids)
        # Check if simultaneous load can be satisfied with possible transformers
        cluster_dict = {}
        invalid_cluster_dict = {}
        for cluster_id in range(cluster_count):
            vid_list = [localid2vid[lid[0]] for lid in np.argwhere(flat_groups == cluster_id)]
            total_sim_load = utils.simultaneous_peak_load(buildings, consumer_cat_df, vid_list) / TRANSFORMER_PLANNING_UTILIZATION
            distance_feasible = True
            if dist_mat is not None and vid2localid is not None:
                distance_feasible = self.cluster_has_feasible_transformer_position(
                    vid_list,
                    dist_mat,
                    vid2localid,
                    self.greenfield_distance_limit(vid_list, max_transformer_distance),
                )
            if (total_sim_load >= max(transformer_capacities) and len(vid_list) >= 5):  # the cluster is too big
                invalid_cluster_dict[cluster_id] = vid_list
            elif not distance_feasible:
                invalid_cluster_dict[cluster_id] = vid_list
            elif total_sim_load < max(transformer_capacities):
                # the smallest standard transformer that satisfies the load
                opt_transformer = transformer_capacities[transformer_capacities > total_sim_load][0]
                cluster_dict[cluster_id] = (vid_list, opt_transformer)
            else:
                opt_transformer = math.ceil(total_sim_load)
                cluster_dict[cluster_id] = (vid_list, opt_transformer)
        return invalid_cluster_dict, cluster_dict, cluster_count

    def get_kcid_length(self) -> int:
        """Return the number of distinct kcids in ``buildings_tem``."""
        query = """SELECT COUNT(DISTINCT kcid)
                   FROM buildings_tem
                   WHERE kcid IS NOT NULL; """
        self.cur.execute(query)
        kcid_length = self.cur.fetchone()[0]
        return kcid_length

    def get_next_unfinished_kcid(self, plz: int) -> int:
        """Return the smallest kcid of ``buildings_tem`` that has no ``grid_result`` row yet.

        Raises:
            TypeError: If every kcid is finished (``fetchone()`` returns ``None``).
        """
        query = """SELECT kcid
                   FROM buildings_tem
                   WHERE kcid NOT IN (SELECT DISTINCT kcid
                                      FROM pylovo.grid_result
                                      WHERE version_id = %(v)s
                                        AND grid_result.plz = %(plz)s)
                     AND kcid IS NOT NULL
                   ORDER BY kcid
                   LIMIT 1;"""
        self.cur.execute(query, {"v": VERSION_ID, "plz": plz})
        kcid = self.cur.fetchone()[0]
        return kcid

    def get_included_transformers(self, kcid: int) -> list:
        """Return the vertex IDs of the existing transformers inside a kcid."""
        query = """SELECT vertice_id
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND type = 'Transformer';"""
        self.cur.execute(query, {"k": kcid})
        transformers_list = ([t[0] for t in data] if (data := self.cur.fetchall()) else [])
        return transformers_list

    def clear_grid_result_in_kmean_cluster(self, plz: int, kcid: int):
        """Delete the greenfield ``grid_result`` rows (``bcid >= 0``) of a kcid before it is clustered again."""
        clear_query = """DELETE
                         FROM pylovo.grid_result
                         WHERE version_id = %(v)s
                           AND plz = %(pc)s
                           AND kcid = %(kc)s
                           AND bcid >= 0; """

        params = {"v": VERSION_ID, "pc": plz, "kc": kcid}
        self.cur.execute(clear_query, params)
        self.logger.debug(f"Building clusters with plz = {plz}, k_mean cluster = {kcid} area cleared.")

    def upsert_bcid(self, plz: int, kcid: int, bcid: int, vertices: list, transformer_rated_power: int):
        """Assign a bcid to the unclustered buildings at the given connection points and add its ``grid_result`` row.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: New building cluster ID.
            vertices: Connection points (``COALESCE(agg_connection_point, connection_point)``) of the cluster.
            transformer_rated_power: Rated power of the selected transformer in kVA.
        """
        # Insert references to building elements in which cluster they are.
        building_query = """UPDATE buildings_tem
                            SET bcid = %(bc)s
                            WHERE plz = %(pc)s
                              AND kcid = %(kc)s
                              AND bcid ISNULL
                              AND COALESCE(agg_connection_point, connection_point) IN %(vid)s
                              AND type != 'Transformer'
                              AND peak_load_in_kw != 0; """

        params = {"v": VERSION_ID, "pc": plz, "bc": bcid, "kc": kcid, "vid": tuple(map(int, vertices)), }
        self.cur.execute(building_query, params)

        # Insert new clustering
        cluster_query = """INSERT INTO pylovo.grid_result (version_id, plz, kcid, bcid, transformer_rated_power)
                           VALUES (%(v)s, %(pc)s, %(kc)s, %(bc)s, %(s)s); """

        params = {"v": VERSION_ID, "pc": plz, "bc": bcid, "kc": kcid, "s": int(transformer_rated_power)}
        self.cur.execute(cluster_query, params)

    def get_consumer_to_transformer_df(self, kcid: int, transformer_list: list) -> pd.DataFrame:
        """Return the street distance from every consumer connection point of a kcid to every given transformer.

        Args:
            kcid: K-means cluster ID.
            transformer_list: Vertex IDs of the transformers.

        Returns:
            DataFrame with ``start_vid`` (connection point), ``end_vid`` (transformer) and
            ``agg_cost`` (distance truncated to whole metres).
        """
        consumer_query = """SELECT DISTINCT COALESCE(agg_connection_point, connection_point) AS connection_point
                            FROM buildings_tem
                            WHERE kcid = %(k)s
                              AND type != 'Transformer'
                              AND peak_load_in_kw != 0;"""
        self.cur.execute(consumer_query, {"k": kcid})
        consumer_list = [t[0] for t in self.cur.fetchall()]

        cost_query = f"""SELECT *
                        FROM pgr_dijkstraCost(
                                {WAYS_TEM_EDGES_SQL},
                                %(cl)s, %(tl)s,
                                false);"""
        # int16 would silently wrap vertex IDs and distances above 32767.
        cost_df = pd.read_sql_query(cost_query, con=self.conn, params={"cl": consumer_list, "tl": transformer_list},
                                    dtype={"start_vid": np.int64, "end_vid": np.int64, "agg_cost": np.int32}, )

        return cost_df

    def get_brownfield_transformer_capacity_map(self, transformer_list: list) -> dict[int, int]:
        """Known ratings (kVA) of existing transformers, keyed by their temporary vertex ID.

        Only transformers imported with a rating, such as imported DSO
        stations, appear; the others keep the catalogue choice.
        """
        if not transformer_list:
            return {}
        query = """
            SELECT b.vertice_id, t.transformer_rated_power
            FROM buildings_tem b
            JOIN pylovo.transformers t
              ON t.osm_id = b.objectid
            WHERE b.vertice_id IN %(transformers)s
              AND b.type = 'Transformer'
              AND t.transformer_rated_power IS NOT NULL;
        """
        self.cur.execute(query, {"transformers": tuple(map(int, transformer_list))})
        return {int(vertice_id): int(capacity) for vertice_id, capacity in self.cur.fetchall()}

    def count_kmean_cluster_consumers(self, kcid: int) -> int:
        query = """SELECT COUNT(DISTINCT vertice_id)
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND type != 'Transformer'
                     AND peak_load_in_kw != 0
                     AND bcid ISNULL;"""
        self.cur.execute(query, {"k": kcid})
        count = self.cur.fetchone()[0]

        return count

    def delete_isolated_building(self, plz: int, kcid):
        query = """DELETE
                   FROM buildings_tem
                   WHERE plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid ISNULL;"""
        self.cur.execute(query, {"p": plz, "k": kcid})

    def get_greenfield_bcids(self, plz: int, kcid: int) -> list:
        """
        Args:
            plz: loadarea cluster ID
            kcid: kmeans cluster ID
        Returns: A list of greenfield building clusters for a given plz
        """
        query = """SELECT DISTINCT bcid
                   FROM pylovo.grid_result
                   WHERE version_id = %(v)s
                     AND kcid = %(kc)s
                     AND plz = %(pc)s
                     AND model_status ISNULL
                   ORDER BY bcid; """
        params = {"v": VERSION_ID, "pc": plz, "kc": kcid}
        self.cur.execute(query, params)
        bcid_list = [t[0] for t in data] if (data := self.cur.fetchall()) else []
        return bcid_list

    def get_buildings_from_kcid(self, kcid: int, ) -> pd.DataFrame:
        """Return the unclustered loaded buildings of a kcid, indexed and sorted by ``vertice_id``."""
        buildings_query = """SELECT *
                             FROM buildings_tem
                             WHERE COALESCE(agg_connection_point, connection_point) IS NOT NULL
                               AND kcid = %(k)s
                               AND bcid ISNULL
                               AND peak_load_in_kw != 0;"""
        params = {"k": kcid}

        buildings_df = pd.read_sql_query(buildings_query, con=self.conn, params=params)
        buildings_df.set_index("vertice_id", drop=False, inplace=True)
        buildings_df.sort_index(inplace=True)

        self.logger.debug(f"Building data fetched. {len(buildings_df)} buildings from kc={kcid} ...")

        return buildings_df

    def get_buildings_from_bcid(self, plz: int, kcid: int, bcid: int) -> pd.DataFrame:
        """Return the loaded consumer buildings of a building cluster, indexed and sorted by ``vertice_id``."""
        buildings_query = """SELECT *
                             FROM buildings_tem
                             WHERE type != 'Transformer'
                               AND peak_load_in_kw != 0
                               AND plz = %(p)s
                               AND bcid = %(b)s
                               AND kcid = %(k)s;"""
        params = {"p": plz, "b": bcid, "k": kcid}

        buildings_df = pd.read_sql_query(buildings_query, con=self.conn, params=params)
        buildings_df.set_index("vertice_id", drop=False, inplace=True)
        buildings_df.sort_index(inplace=True)

        self.logger.debug(f"{len(buildings_df)} building data fetched.")

        return buildings_df

    def get_existing_transformer_capacity_trafo_ui(self, plz: int, kcid: int, bcid: int,
                                                   include_dso: bool | None = None, include_open: bool | None = None,
                                                   include_manual: bool | None = None) -> Optional[int]:
        """Return the rated power of an existing transformer that intersects the buildings of a cluster.

        Used by grid generation (``update_transformer_rated_power``), not only by the UI: a rating
        entered in the transformer map UI (or imported) wins over the catalogue choice. Only
        candidates of the sources this run uses count (``USE_DSO_TRANSFORMER_POSITIONS``,
        ``USE_OPEN_TRANSFORMER_POSITIONS``, ``USE_MANUAL_TRANSFORMER_POSITIONS``; see
        :mod:`pylovo.database.transformer_sources`), so a rating of a disabled source never sizes a
        greenfield station. Among the rated candidates that intersect the collected geometries of
        the cluster's ``buildings_tem`` rows, the cluster's own station (its ``Transformer`` row)
        comes first, then the lowest ``osm_id``.

        Args:
            plz: Postcode (only for log messages).
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            include_dso, include_open, include_manual: Enabled sources; ``None`` uses the configuration.

        Returns:
            The rated power in kVA, or ``None`` if no rated candidate of an enabled source intersects
            the cluster.
        """
        sources = source_params(
            USE_DSO_TRANSFORMER_POSITIONS if include_dso is None else include_dso,
            USE_OPEN_TRANSFORMER_POSITIONS if include_open is None else include_open,
            USE_MANUAL_TRANSFORMER_POSITIONS if include_manual is None else include_manual,
        )
        if not any(sources.values()):
            return None
        # Get the geometry of the cluster area as text format for proper psycopg2 serialization
        cluster_geom_query = """
            SELECT ST_AsText(ST_Collect(geom)) as cluster_geom_wkt
            FROM buildings_tem
            WHERE kcid = %(kcid)s AND bcid = %(bcid)s
        """
        self.cur.execute(cluster_geom_query, {"kcid": kcid, "bcid": bcid})
        result = self.cur.fetchone()
        
        if not result or not result[0]:
            return None
            
        cluster_geom_wkt = result[0]
        # transformer rows of buildings_tem carry the candidate's osm_id as objectid
        own_station_first = """(t.osm_id NOT IN (SELECT objectid FROM buildings_tem
                                                  WHERE kcid = %(kcid)s AND bcid = %(bcid)s
                                                    AND type = 'Transformer' AND objectid IS NOT NULL))"""
        
        # Check if there's a transformer with a specific capacity in this area
        # Use a more robust approach to handle GEOS topology issues
        transformer_query = f"""
            SELECT transformer_rated_power
            FROM pylovo.transformers t
            WHERE t.transformer_rated_power IS NOT NULL
            AND {SOURCE_ENABLED_SQL}
            AND ST_Intersects(t.geom, ST_MakeValid(ST_Buffer(ST_MakeValid(ST_GeomFromText(%(cluster_geom_wkt)s, {TARGET_EPSG})), 0)))
            ORDER BY {own_station_first}, t.osm_id
            LIMIT 1
        """
        
        fallback_query = f"""
            SELECT transformer_rated_power
            FROM pylovo.transformers t
            WHERE t.transformer_rated_power IS NOT NULL
            AND {SOURCE_ENABLED_SQL}
            AND ST_DWithin(t.geom, ST_MakeValid(ST_Buffer(ST_MakeValid(ST_GeomFromText(%(cluster_geom_wkt)s, {TARGET_EPSG})), 0)), 1.0)
            ORDER BY {own_station_first}, t.osm_id
            LIMIT 1
        """
        params = {"cluster_geom_wkt": cluster_geom_wkt, "kcid": kcid, "bcid": bcid, **sources}

        # A failed query aborts the whole generation transaction; the savepoint keeps it
        # usable for the fallback query and everything after it.
        self.cur.execute("SAVEPOINT existing_transformer_capacity")
        try:
            try:
                self.cur.execute(transformer_query, params)
            except psycopg2.Error:
                # If ST_Intersects fails due to topology issues, retry with a 1 m tolerance.
                self.cur.execute("ROLLBACK TO SAVEPOINT existing_transformer_capacity")
                self.cur.execute(fallback_query, params)
            result = self.cur.fetchone()
        except psycopg2.Error as fallback_error:
            self.cur.execute("ROLLBACK TO SAVEPOINT existing_transformer_capacity")
            self.logger.warning(f"Could not check transformer intersection for plz={plz}, kcid={kcid}, bcid={bcid}: {fallback_error}")
            result = None
        self.cur.execute("RELEASE SAVEPOINT existing_transformer_capacity")

        return int(result[0]) if result else None

    def _set_transformer_rated_power(self, plz: int, kcid: int, bcid: int, transformer_rated_power: int) -> None:
        """Write ``transformer_rated_power`` of one grid in ``grid_result``."""
        update_query = """UPDATE pylovo.grid_result
                          SET transformer_rated_power = %(n)s
                          WHERE version_id = %(v)s
                            AND plz = %(p)s
                            AND kcid = %(k)s
                            AND bcid = %(b)s;"""
        self.cur.execute(update_query,
                         {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid, "n": transformer_rated_power})

    def update_transformer_rated_power(self, plz: int, kcid: int, bcid: int, note: int):
        """Revise ``grid_result.transformer_rated_power`` of one building cluster.

        A rated raw transformer inside the cluster (``get_existing_transformer_capacity_trafo_ui``)
        always wins. Otherwise the stored value is revised against the standard capacities of
        the PLZ's settlement type:

        * ``note == 0``: upgrade to the next larger standard capacity (``IndexError`` if there is none).
        * ``note != 0``: keep the value if it is a standard capacity or twice the third or fourth
          standard capacity (two parallel units); otherwise round it up to a multiple of 630 kVA.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            note: Update strategy, see above.
        """
        existing_capacity = self.get_existing_transformer_capacity_trafo_ui(plz, kcid, bcid)
        if existing_capacity is not None:
            existing_capacity = int(existing_capacity)
            self._set_transformer_rated_power(plz, kcid, bcid, existing_capacity)
            self.logger.debug(f"Using existing transformer capacity {existing_capacity} kVA for plz={plz}, kcid={kcid}, bcid={bcid}")
            return

        sdl = self.get_settlement_type_from_plz(plz)
        transformer_capacities, _ = self.get_transformer_data(sdl)

        if note == 0:
            transformer_rated_power = self.get_transformer_rated_power_from_bcid(plz, kcid, bcid)
            new_transformer_rated_power = int(
                transformer_capacities[transformer_capacities > transformer_rated_power][0].item()
            )
            self._set_transformer_rated_power(plz, kcid, bcid, new_transformer_rated_power)
        else:
            double_trans = np.multiply(transformer_capacities[2:4], 2)
            combined = np.concatenate((transformer_capacities, double_trans), axis=None)
            transformer_rated_power = self.get_transformer_rated_power_from_bcid(plz, kcid, bcid)
            if transformer_rated_power in combined.tolist():
                return None
            new_transformer_rated_power = int(np.ceil(transformer_rated_power / 630) * 630)
            self._set_transformer_rated_power(plz, kcid, bcid, new_transformer_rated_power)
            self.logger.info(
                f"Updated transformer_rated_power (multi/group mode): plz={plz}, kcid={kcid}, bcid={bcid}, "
                f"old={transformer_rated_power} kVA -> new={new_transformer_rated_power} kVA)"
            )

    def get_transformer_data(self, settlement_type: int = None) -> tuple[np.array, dict]:
        """Return the standard transformer capacities of a settlement type and their costs.

        Args:
            settlement_type: 1 = rural, 2 = semi-urban, 3 = urban (keys of ``TRANSFORMER_MAPPING``).

        Returns:
            ``(capacities, cost_by_capacity)``: capacities in kVA ascending, as found in
            ``equipment_data``, and a dict capacity -> cost in EUR. ``None`` (after an info log)
            for an unknown settlement type.
        """
        if settlement_type not in TRANSFORMER_MAPPING:
            self.logger.info("Incorrect settlement type number specified.")
            return

        allowed_capacities = tuple(TRANSFORMER_MAPPING[settlement_type])

        query = """SELECT equipment_data.s_max_kva, cost_eur
                   FROM pylovo.equipment_data
                   WHERE typ = 'Transformer' \
                     AND s_max_kva IN %(capacities)s
                   ORDER BY s_max_kva;"""

        self.cur.execute(query, {"capacities": allowed_capacities})
        data = self.cur.fetchall()
        capacities = [i[0] for i in data]
        transformer2cost = {i[0]: i[1] for i in data}

        self.logger.debug("Transformer data fetched.")
        return np.array(capacities), transformer2cost

    def update_building_cluster(self, transformer_id: int, conn_id_list: Union[list, tuple], count: int, kcid: int,
            plz: int, transformer_rated_power: int) -> None:
        """Store a brownfield building cluster around an existing transformer.

        Sets the bcid on the transformer row and on the loaded buildings at the given connection
        points, inserts the ``grid_result`` row and a ``transformer_positions`` row at the
        transformer (linked to its ``pylovo.transformers`` row when there is one).

        Args:
            transformer_id: Vertex ID of the transformer.
            conn_id_list: Connection points assigned to the transformer.
            count: bcid of the new cluster (negative for brownfield clusters).
            kcid: K-means cluster ID.
            plz: Postcode.
            transformer_rated_power: Rated power of the transformer in kVA.
        """
        query = """
                UPDATE buildings_tem
                SET bcid = %(count)s
                WHERE vertice_id = %(t)s;

                UPDATE buildings_tem
                SET bcid = %(count)s
                WHERE COALESCE(agg_connection_point, connection_point) IN %(c)s
                  AND type != 'Transformer'
                  AND peak_load_in_kw != 0;

                WITH inserted_grid AS (
                    INSERT INTO pylovo.grid_result
                        (version_id, plz, kcid, bcid, ont_vertice_id, transformer_rated_power)
                    VALUES (%(v)s, %(pc)s, %(k)s, %(count)s, %(t)s, %(l)s)
                    RETURNING grid_result_id
                ), transformer_row AS (
                    SELECT
                        b.centroid,
                        b.objectid,
                        tr.osm_id,
                        COALESCE(tr.osm, true) AS osm,
                        COALESCE(tr.lod2, false) AS lod2,
                        tr.lod2_objectid
                    FROM buildings_tem b
                    LEFT JOIN pylovo.transformers tr
                        ON tr.osm_id = b.objectid
                    WHERE b.vertice_id = %(t)s
                    ORDER BY tr.osm_id IS NULL, b.objectid
                    LIMIT 1
                )
                INSERT INTO pylovo.transformer_positions (
                    version_id, grid_result_id, geom, osm_id, comment, osm, lod2, lod2_objectid
                )
                SELECT
                    %(v)s,
                    inserted_grid.grid_result_id,
                    transformer_row.centroid,
                    COALESCE(transformer_row.osm_id, transformer_row.objectid),
                    'Normal',
                    transformer_row.osm,
                    transformer_row.lod2,
                    transformer_row.lod2_objectid
                FROM inserted_grid
                CROSS JOIN transformer_row;
                """
        params = {"v": VERSION_ID, "count": count, "c": tuple(conn_id_list), "t": transformer_id, "k": kcid, "pc": plz,
            "l": transformer_rated_power, }
        self.cur.execute(query, params)

    def get_building_connection_points_from_bc(self, kcid: int, bcid: int) -> list:
        """Return the distinct connection points of the loaded buildings of a building cluster."""
        count_query = """SELECT DISTINCT COALESCE(agg_connection_point, connection_point) AS connection_point
                         FROM buildings_tem
                         WHERE vertice_id IS NOT NULL
                           AND bcid = %(b)s
                           AND kcid = %(k)s
                           AND peak_load_in_kw != 0;"""
        params = {"b": bcid, "k": kcid}
        self.cur.execute(count_query, params)
        return [t[0] for t in self.cur.fetchall()]

    def upsert_transformer_selection(self, plz: int, kcid: int, bcid: int, connection_id: int):
        """Store the chosen greenfield transformer position of a building cluster.

        Writes the vertex as ``ont_vertice_id`` (ONT: Ortsnetztransformator), marks the grid as
        modelled (``model_status = 1``) and inserts a ``transformer_positions`` row at the vertex
        with comment ``'on_way'``.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            connection_id: Vertex ID of the transformer position.
        """

        query = """UPDATE pylovo.grid_result
                   SET ont_vertice_id = %(c)s
                   WHERE version_id = %(v)s
                     AND plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s;

        UPDATE pylovo.grid_result
        SET model_status = 1
        WHERE version_id = %(v)s
          AND plz = %(p)s
          AND kcid = %(k)s
          AND bcid = %(b)s;

        INSERT INTO pylovo.transformer_positions (version_id, grid_result_id, geom, comment, osm, lod2)
        VALUES(
                %(v)s,
                (SELECT grid_result_id
                 FROM pylovo.grid_result
                 WHERE version_id = %(v)s \
                   AND plz = %(p)s \
                   AND kcid = %(k)s \
                   AND bcid = %(b)s),
                                (SELECT geom FROM ways_tem_vertices_pgr WHERE id = %(c)s),
                                'on_way',
                                false,
                                false);"""
        params = {"v": VERSION_ID, "c": connection_id, "b": bcid, "k": kcid, "p": plz}

        self.cur.execute(query, params)

    def get_distance_matrix_from_bcid(self, kcid: int, bcid: int) -> tuple[dict, np.ndarray, dict]:
        """Return the street-distance matrix between the connection points of a building cluster.

        Args:
            kcid: K-means cluster ID.
            bcid: Building cluster ID.

        Returns:
            ``(localid2vid, dist_mat, vid2localid)`` as returned by ``calculate_cost_arr_dist_matrix``.
        """
        costmatrix_query = _CONNECTION_POINT_COST_MATRIX_SQL.format(bcid_filter="bcid = %(b)s")
        return self.calculate_cost_arr_dist_matrix(costmatrix_query, {"b": bcid, "k": kcid})

    def get_settlement_type_from_plz(self, plz) -> int:
        """Return the settlement type of the PLZ in the active version (1 rural, 2 semi-urban, 3 urban).

        Raises:
            ValueError: If the PLZ has not been classified.
        """
        settlement_query = """SELECT settlement_type
                              FROM pylovo.postcode_result
                              WHERE version_id = %(v)s
                                AND postcode_result_plz = %(p)s
                              LIMIT 1; """
        self.cur.execute(settlement_query, {"v": VERSION_ID, "p": plz})
        row = self.cur.fetchone()
        if row is None or row[0] is None:
            raise ValueError(
                f"No settlement_type found in postcode_result for PLZ {plz} "
                f"(version {VERSION_ID}). Ensure settlement type classification succeeded."
            )
        return row[0]
