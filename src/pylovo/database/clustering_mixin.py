import heapq
import math
import warnings
import time
from abc import ABC
from typing import *

import numpy as np
from scipy.cluster.hierarchy import cut_tree

from pylovo import utils
from pylovo.config_loader import *
from pylovo.database.base_mixin import BaseMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class ClusteringMixin(BaseMixin, ABC):
    def __init__(self):
        super().__init__()

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
        """Build graph-Voronoi cluster adjacency in the components containing the clusters."""
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
        Reads from ways_tem
        :return:
        """
        component_query = """SELECT component, node
                             FROM pgr_connectedComponents(
                                     'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem');"""
        self.cur.execute(component_query)
        data = self.cur.fetchall()
        component = np.asarray([i[0] for i in data])
        node = np.asarray([i[1] for i in data])

        return component, node

    def count_no_kmean_buildings(self):
        """
        Counts relative buildings in buildings_tem, which could not be clustered via k-means
        :return: count
        """
        query = """SELECT COUNT(*)
                   FROM buildings_tem
                   WHERE peak_load_in_kw != 0
                     AND kcid ISNULL;"""
        self.cur.execute(query)
        count = self.cur.fetchone()[0]

        return count

    def count_connected_buildings(self, vertices: Union[list, tuple]) -> int:
        """
        Get count from buildings_tem where type is not transformer
        :param vertices: np.array
        :return: count of buildings with given vertice_id s from buildings_tem
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
        """
        Deletes selected ways from ways_tem and ways_tem_vertices_pgr
        :param vertices:
        :return:
        """
        query = """DELETE
                   FROM ways_tem
                   WHERE target IN %(v)s;
        DELETE
        FROM ways_tem_vertices_pgr
        WHERE id IN %(v)s;"""
        self.cur.execute(query, {"v": tuple(map(int, vertices))})

    def get_connected_component_geometries(self, vertices: Union[list, tuple]) -> tuple[np.ndarray, np.ndarray]:
        """
        Gets the vertice IDs and coordinates of all buildings within a connected component
        :param vertices: vertice IDs of the connected component
        :return: (selected_vertices, coordinates) - vertice IDs and coordinates of the buildings within the connected component as tuple of two np.arrays
        """
        query = """
                SELECT vertice_id, ST_AsText(centroid) as wkt 
                FROM buildings_tem
                WHERE vertice_id IN %(v)s
                  AND peak_load_in_kw != 0
                """
        self.cur.execute(query, {"v": tuple(map(int, vertices))})
        data = self.cur.fetchall()
        selected_vertices = np.array([x[0] for x in data])
        coordinates = np.float64(np.array([x[1].replace('POINT(', '').replace(')', '').split() for x in data]))

        return selected_vertices, coordinates

    def update_kmeans_cluster_multiple(self, vertices: np.ndarray, kcids: np.ndarray) -> None:
        """
        Assigns the given kcids to the buildings with the given vertice IDs.
        Both inputs should have the same length and corresponding order.
        :param vertices: np.array containing the vertice IDs of the buildings
        :param kcids: np.array containing the kcids
        :return:
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
        """
        Groups connected components into a k-means id withouth applying clustering
        :param vertices:
        :return:
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
        """
        Creates a distance matrix from the buildings in the kcid
        Args:
            kcid: k-means cluster id
        Returns: The distance matrix of the buildings in the k-means cluster as np.array and the mapping between vertice_id and local ID as dict
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

        costmatrix_query = """SELECT * \
                              FROM pgr_dijkstraCostMatrix( \
                                      'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem', \
                                      (SELECT array_agg(DISTINCT COALESCE(b.agg_connection_point, b.connection_point)) \
                                       FROM (SELECT * \
                                             FROM buildings_tem \
                                             WHERE kcid = %(k)s \
                                               AND bcid ISNULL \
                                               AND peak_load_in_kw != 0 \
                                             ORDER BY COALESCE(agg_connection_point, connection_point)) AS b), \
                                      false);"""
        params = {"k": kcid}
        localid2vid, dist_mat, _ = self.calculate_cost_arr_dist_matrix(costmatrix_query, params)

        return localid2vid, dist_mat, _

    def calculate_cost_arr_dist_matrix(self, costmatrix_query: str, params: dict) -> tuple[dict, np.ndarray, dict]:
        """
        Helper function for calculating cost array and distance matrix from given parameters
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
        for i in range(len(cost_df)):
            start_id = vid2localid[cost_arr[i, 0]]
            end_id = vid2localid[cost_arr[i, 1]]
            dist_matrix[start_id][end_id] = cost_arr[i, 2]
        et = time.time()
        self.logger.debug(f"Elapsed time for dist_matrix creation: {et - st}")
        return localid2vid, dist_matrix, vid2localid


    def generate_load_vector(self, kcid: int, bcid: int) -> np.ndarray:
        query = """SELECT SUM(peak_load_in_kw)::float
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND bcid = %(b)s
                     AND peak_load_in_kw != 0
                   GROUP BY COALESCE(agg_connection_point, connection_point)
                   ORDER BY COALESCE(agg_connection_point, connection_point);"""
        self.cur.execute(query, {"k": kcid, "b": bcid})
        load = np.asarray([i[0] for i in self.cur.fetchall()])

        return load

    def generate_load_vector_for_connection_points(
        self, kcid: int, bcid: int, connection_points: list[int]
    ) -> tuple[np.ndarray, list[int]]:
        """Return loads aligned to a given connection-point order."""
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
            consumer_cat_df: pd.DataFrame, transformer_capacities: np.ndarray, double_trans: np.ndarray,
            dist_mat: np.ndarray | None = None, vid2localid: dict[int, int] | None = None,
            max_transformer_distance: float | None = None, ) -> tuple[
        dict, dict, int]:
        """
        Attempts to cluster buildings based on hierarchical clustering linkage matrix Z and assigns transformers.

        This function cuts the hierarchical tree to form `cluster_amount` clusters. For each cluster, it calculates
        the simultaneous peak load. It then attempts to assign an optimal transformer (single or double) based on
        the load and available capacities. If a cluster's load exceeds the maximum single transformer capacity
        and has enough buildings, it is marked as invalid (too big).

        Args:
            Z (np.ndarray): The linkage matrix from hierarchical clustering (scipy.cluster.hierarchy.linkage).
            cluster_amount (int): The number of clusters to form.
            localid2vid (dict): Mapping from local clustering indices to building vertice IDs.
            buildings (pd.DataFrame): DataFrame containing building information (loads, types, etc.).
            consumer_cat_df (pd.DataFrame): DataFrame containing consumer category definitions (simultaneity factors).
            transformer_capacities (np.ndarray): Array of available single transformer capacities (sorted).
            double_trans (np.ndarray): Array of available double transformer capacities (sorted).
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
            total_sim_load = utils.simultaneousPeakLoad(buildings, consumer_cat_df, vid_list)
            distance_feasible = True
            if dist_mat is not None and vid2localid is not None:
                distance_feasible = self.cluster_has_feasible_transformer_position(
                    vid_list,
                    dist_mat,
                    vid2localid,
                    max_transformer_distance,
                )
            if (total_sim_load >= max(transformer_capacities) and len(vid_list) >= 5):  # the cluster is too big
                invalid_cluster_dict[cluster_id] = vid_list
            elif not distance_feasible:
                invalid_cluster_dict[cluster_id] = vid_list
            elif total_sim_load < max(transformer_capacities):
                # find the smallest transformer, that satisfies the load
                opt_transformer = transformer_capacities[transformer_capacities > total_sim_load][0]
                opt_double_transformer = double_trans[double_trans > total_sim_load * 1.15][0]
                if (opt_double_transformer - total_sim_load) > (opt_transformer - total_sim_load):
                    cluster_dict[cluster_id] = (vid_list, opt_transformer)
                else:
                    cluster_dict[cluster_id] = (vid_list, opt_double_transformer)
            else:
                opt_transformer = math.ceil(total_sim_load)
                cluster_dict[cluster_id] = (vid_list, opt_transformer)
        return invalid_cluster_dict, cluster_dict, cluster_count

    def get_kcid_length(self) -> int:
        query = """SELECT COUNT(DISTINCT kcid)
                   FROM buildings_tem
                   WHERE kcid IS NOT NULL; """
        self.cur.execute(query)
        kcid_length = self.cur.fetchone()[0]
        return kcid_length

    def get_next_unfinished_kcid(self, plz: int) -> int:
        """
        :return: one unmodeled k mean cluster ID - plz
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
        """
        Reads the vertice ids of transformers from a given kcid
        :param kcid:
        :return: list
        """
        query = """SELECT vertice_id
                   FROM buildings_tem
                   WHERE kcid = %(k)s
                     AND type = 'Transformer';"""
        self.cur.execute(query, {"k": kcid})
        transformers_list = ([t[0] for t in data] if (data := self.cur.fetchall()) else [])
        return transformers_list

    def clear_grid_result_in_kmean_cluster(self, plz: int, kcid: int):
        # Remove old clustering at same postcode cluster
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
        """
        Assign buildings in buildings_tem the bcid and stores the cluster in grid_result
        Args:
            plz: postcode cluster ID - plz
            kcid: kmeans cluster ID
            bcid: building cluster ID
            vertices: List of vertice_id of selected buildings
            transformer_rated_power: Apparent power of the selected transformer
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
        consumer_query = """SELECT DISTINCT COALESCE(agg_connection_point, connection_point) AS connection_point
                            FROM buildings_tem
                            WHERE kcid = %(k)s
                              AND type != 'Transformer'
                              AND peak_load_in_kw != 0;"""
        self.cur.execute(consumer_query, {"k": kcid})
        consumer_list = [t[0] for t in self.cur.fetchall()]

        cost_query = """SELECT *
                        FROM pgr_dijkstraCost(
                                'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem',
                                %(cl)s, %(tl)s,
                                false);"""
        cost_df = pd.read_sql_query(cost_query, con=self.conn, params={"cl": consumer_list, "tl": transformer_list},
                                    dtype={"start_vid": np.int16, "end_vid": np.int16, "agg_cost": np.int16}, )

        return cost_df

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
        """
        Args:
            kcid: kmeans_cluster ID
        Returns: A dataframe with all building information
        """
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
        # dropping duplicate indices
        # buildings_df = buildings_df[~buildings_df.index.duplicated(keep='first')]

        self.logger.debug(f"{len(buildings_df)} building data fetched.")

        return buildings_df

    def get_existing_transformer_capacity_trafo_ui(self, plz: int, kcid: int, bcid: int) -> Optional[int]:
        """
        Check if there's an existing transformer with a specific capacity for the given cluster.
        
        Args:
            plz (int): The postal code
            kcid (int): K-means cluster ID
            bcid (int): Building cluster ID
            
        Returns:
            Optional[int]: Transformer capacity if found, None otherwise
        """
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
        
        # Check if there's a transformer with a specific capacity in this area
        # Use a more robust approach to handle GEOS topology issues
        transformer_query = f"""
            SELECT transformer_rated_power
            FROM pylovo.transformers t
            WHERE t.transformer_rated_power IS NOT NULL
            AND ST_Intersects(t.geom, ST_MakeValid(ST_Buffer(ST_MakeValid(ST_GeomFromText(%(cluster_geom_wkt)s, {TARGET_EPSG})), 0)))
            LIMIT 1
        """
        
        try:
            self.cur.execute(transformer_query, {"cluster_geom_wkt": cluster_geom_wkt})
            result = self.cur.fetchone()
            
            if result:
                return int(result[0])
        except Exception as e:
            # If ST_Intersects fails due to topology issues, try with a small buffer
            try:
                fallback_query = f"""
                    SELECT transformer_rated_power
                    FROM pylovo.transformers t
                    WHERE t.transformer_rated_power IS NOT NULL
                    AND ST_DWithin(t.geom, ST_MakeValid(ST_Buffer(ST_MakeValid(ST_GeomFromText(%(cluster_geom_wkt)s, {TARGET_EPSG})), 0)), 1.0)
                    LIMIT 1
                """
                self.cur.execute(fallback_query, {"cluster_geom_wkt": cluster_geom_wkt})
                result = self.cur.fetchone()
                
                if result:
                    return int(result[0])
            except Exception as fallback_error:
                # Log the error but don't fail the entire process
                self.logger.warning(f"Could not check transformer intersection for plz={plz}, kcid={kcid}, bcid={bcid}: {fallback_error}")
        
        return None

    def update_transformer_rated_power(self, plz: int, kcid: int, bcid: int, note: int):
        """
        Update the field transformer_rated_power in grid_result for a given building cluster (bcid).

        Process:
        1) Determine settlement type from postcode (plz) and fetch the allowed standard transformer capacities
           (ascending array transformer_capacities).
        2) Read the currently stored transformer_rated_power for the (plz, kcid, bcid) tuple.

        Behaviour controlled by note:
        - note == 0 (single standard transformer mode):
          Upgrade to the smallest standard capacity strictly greater than the current value.
          (Precondition: such a larger capacity must exist; otherwise an IndexError would occur.)
        - note != 0 (multi / grouped mode):
          a) Build an extended list by appending doubled capacities of selected mid–range sizes (transformer_capacities[2:4] * 2).
          b) If the current capacity already matches any allowed (standard or doubled) value: no change.
          c) Else round up to the next multiple of 630 kVA (ceil(current / 630) * 630) to emulate a grouped / parallel transformer arrangement.

        Parameters:
        plz  : Postcode cluster ID.
        kcid : K‑means cluster ID.
        bcid : Building cluster ID within the k‑means cluster.
        note : Control flag for update strategy (0 = standard single transformer upgrade, !=0 = multi / grouping logic).

        Returns:
        None. Performs an in‑place database update.
        """
        # First check if there's an existing transformer with a specific capacity
        existing_capacity = self.get_existing_transformer_capacity_trafo_ui(plz, kcid, bcid)
        if existing_capacity is not None:
            # Use the existing transformer capacity
            existing_capacity = int(existing_capacity)
            update_query = """UPDATE pylovo.grid_result
                              SET transformer_rated_power = %(n)s
                              WHERE version_id = %(v)s
                                AND plz = %(p)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
            self.cur.execute(update_query,
                             {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid, "n": existing_capacity})
            self.logger.debug(f"Using existing transformer capacity {existing_capacity} kVA for plz={plz}, kcid={kcid}, bcid={bcid}")
            return
        
        sdl = self.get_settlement_type_from_plz(plz)
        transformer_capacities, _ = self.get_transformer_data(sdl)

        if note == 0:
            old_query = """SELECT transformer_rated_power
                           FROM pylovo.grid_result
                           WHERE version_id = %(v)s
                             AND plz = %(p)s
                             AND kcid = %(k)s
                             AND bcid = %(b)s;"""
            self.cur.execute(old_query, {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid})
            transformer_rated_power = self.cur.fetchone()[0]

            new_transformer_rated_power = int(
                transformer_capacities[transformer_capacities > transformer_rated_power][0].item()
            )
            update_query = """UPDATE pylovo.grid_result
                              SET transformer_rated_power = %(n)s
                              WHERE version_id = %(v)s
                                AND plz = %(p)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
            self.cur.execute(update_query,
                             {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid, "n": new_transformer_rated_power}, )
        else:
            double_trans = np.multiply(transformer_capacities[2:4], 2)
            combined = np.concatenate((transformer_capacities, double_trans), axis=None)
            np.sort(combined, axis=None)
            old_query = """SELECT transformer_rated_power
                           FROM pylovo.grid_result
                           WHERE version_id = %(v)s
                             AND plz = %(p)s
                             AND kcid = %(k)s
                             AND bcid = %(b)s;"""
            self.cur.execute(old_query, {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid})
            transformer_rated_power = self.cur.fetchone()[0]
            if transformer_rated_power in combined.tolist():
                return None
            new_transformer_rated_power = int(np.ceil(transformer_rated_power / 630) * 630)
            update_query = """UPDATE pylovo.grid_result
                              SET transformer_rated_power = %(n)s
                              WHERE version_id = %(v)s
                                AND plz = %(p)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
            self.cur.execute(update_query,
                             {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid, "n": new_transformer_rated_power}, )
            self.logger.info(
                f"Updated transformer_rated_power (multi/group mode): plz={plz}, kcid={kcid}, bcid={bcid}, "
                f"old={transformer_rated_power} kVA -> new={new_transformer_rated_power} kVA)"
            )

    def get_transformer_data(self, settlement_type: int = None) -> tuple[np.array, dict]:
        """
        Args:
            Settlement type: 1=Rural, 2=Semi-urban, 3=Urban
        Returns: Typical transformer capacities and costs depending on the settlement type
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
        """
        Update building cluster information by performing multiple operations:
          - Update the 'bcid' in 'buildings_tem' where 'vertice_id' matches the transformer_id.
          - Update the 'bcid' in 'buildings_tem' for rows where 'connection_point' is in the provided list and type is not 'Transformer'.
          - Insert a new record into 'grid_result'.
          - Insert a new record into 'transformer_positions' using subqueries for geometry and OGC ID.
        Args:
            transformer_id (int): The ID of the transformer.
            conn_id_list (Union[list, tuple]): A list or tuple of connection point IDs.
            count (int): The new building cluster identifier.
            kcid (int): The KCID value.
            plz (int): The postcode value.
            transformer_rated_power (int): The selected transformer size for the building cluster.
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
                CROSS JOIN transformer_row; \

                """
        params = {"v": VERSION_ID, "count": count, "c": tuple(conn_id_list), "t": transformer_id, "k": kcid, "pc": plz,
            "l": transformer_rated_power, }
        self.cur.execute(query, params)

    def get_building_connection_points_from_bc(self, kcid: int, bcid: int) -> list:
        """
        Args:
            kcid: kmeans_cluster ID
            bcid: building cluster ID
        Returns: A dataframe with all building information
        """
        count_query = """SELECT DISTINCT COALESCE(agg_connection_point, connection_point) AS connection_point
                         FROM buildings_tem
                         WHERE vertice_id IS NOT NULL
                           AND bcid = %(b)s
                           AND kcid = %(k)s
                           AND peak_load_in_kw != 0;"""
        params = {"b": bcid, "k": kcid}
        self.cur.execute(count_query, params)
        try:
            cp = [t[0] for t in self.cur.fetchall()]
        except:
            cp = []

        return cp

    def upsert_transformer_selection(self, plz: int, kcid: int, bcid: int, connection_id: int):
        """Writes the vertice_id of chosen building as ONT location in the grid_result table"""

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
        """
        Args:
            kcid: k mean cluster ID
            bcid: building cluster ID
        Returns: The distance matrix of the buildings in the building cluster as np.array and the mapping between vertice_id and local ID as dict
        """

        costmatrix_query = """SELECT *
                              FROM pgr_dijkstraCostMatrix(
                                      'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem',
                                      (SELECT array_agg(DISTINCT COALESCE(b.agg_connection_point, b.connection_point))
                                       FROM (SELECT *
                                             FROM buildings_tem
                                             WHERE kcid = %(k)s
                                               AND bcid = %(b)s
                                               AND peak_load_in_kw != 0
                                             ORDER BY COALESCE(agg_connection_point, connection_point)) AS b),
                                      false);"""
        params = {"b": bcid, "k": kcid}
        localid2vid, dist_mat, _ = self.calculate_cost_arr_dist_matrix(costmatrix_query, params)

        return localid2vid, dist_mat, _

    def get_settlement_type_from_plz(self, plz) -> int:
        """
        Args:
            plz:
        Returns: Settlement type: 1=Rural, 2=Semi-urban, 3=Urban
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
