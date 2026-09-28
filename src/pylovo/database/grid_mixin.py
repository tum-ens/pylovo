"""Routing queries for cable installation and the line tables of generated grids."""

import warnings

from psycopg2 import sql
from psycopg2.extras import execute_values
from shapely.geometry import LineString

from pylovo.config_loader import FEEDER_CABLES, TARGET_EPSG, VERSION_ID
from pylovo.database.base_mixin import WAYS_TEM_EDGES_SQL, BaseMixin, plz_table_name

warnings.simplefilter(action='ignore', category=UserWarning)

# Visualisation helper geometry of a line ``lr``: its offset curve (subquery ``offset_geom`` of
# _offset_line_lateral_sql), oriented like the line and extended to the line's own end points so
# that the helper still meets both buses. Falls back to the unshifted line.
_OFFSET_HELPER_GEOM_SQL = """CASE
                        WHEN offset_geom.offset_line IS NULL OR ST_IsEmpty(offset_geom.offset_line) THEN lr.geom
                        WHEN ST_Distance(ST_StartPoint(offset_geom.offset_line), ST_StartPoint(lr.geom))
                             <= ST_Distance(ST_EndPoint(offset_geom.offset_line), ST_StartPoint(lr.geom))
                        THEN ST_AddPoint(
                            ST_AddPoint(
                                offset_geom.offset_line,
                                ST_StartPoint(lr.geom),
                                0
                            ),
                            ST_EndPoint(lr.geom)
                        )
                        ELSE ST_AddPoint(
                            ST_AddPoint(
                                ST_Reverse(offset_geom.offset_line),
                                ST_StartPoint(lr.geom),
                                0
                            ),
                            ST_EndPoint(lr.geom)
                        )
                    END"""


def _offset_line_lateral_sql(offset_expression: str) -> str:
    """Return a ``LEFT JOIN LATERAL`` subquery ``offset_geom`` for the line ``lr``.

    ``offset_geom.offset_line`` is the longest linestring of ``ST_OffsetCurve(lr.geom, offset)``;
    ``offset_expression`` is the SQL expression of the offset in metres (sign = side).
    """
    return f"""LEFT JOIN LATERAL (
                    SELECT dumped.geom AS offset_line
                    FROM ST_Dump(
                        ST_LineMerge(
                            ST_CollectionExtract(
                                ST_OffsetCurve(lr.geom, {offset_expression}),
                                2
                            )
                        )
                    ) AS dumped
                    WHERE GeometryType(dumped.geom) = 'LINESTRING'
                    ORDER BY ST_Length(dumped.geom) DESC
                    LIMIT 1
                ) AS offset_geom ON TRUE"""


class GridMixin(BaseMixin):
    """Routing queries on ``ways_tem`` and the line tables of generated grids.

    ``lines_result`` holds the installed cables. ``lines_result_helper``, ``split_points`` and
    ``lines_result_cache`` holds visualisation rows derived from it (shifted duplicates of
    overlapping lines, feeder split nodes, merged feeder sections) for GIS inspection.
    """

    def fetch_cables(self) -> list:
        """Return all cables of ``equipment_data`` as ``(name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur)``.

        Rows of every version are returned.
        """
        query = """SELECT name,
                       r_mohm_per_km / 1000.0 as r_ohm_per_km,
                       x_mohm_per_km / 1000.0 as x_ohm_per_km,
                       max_i_a / 1000.0       as max_i_ka,
                       cost_eur
            FROM pylovo.equipment_data
                WHERE typ = 'Cable'
                """
        self.cur.execute(query)
        return self.cur.fetchall()

    def fetch_node_coordinates(self, plz: int) -> dict[int, tuple[float, float]]:
        """Return the WGS84 ``(lon, lat)`` of every pgRouting vertex of a PLZ, keyed by vertex ID."""
        vertices = sql.Identifier("pg_temp", plz_table_name("ways_tem", plz) + "_vertices_pgr")
        query = sql.SQL("""
            SELECT id,
                   ST_X(ST_Transform(geom, 4326)),
                   ST_Y(ST_Transform(geom, 4326))
            FROM {vertices}
            WHERE geom IS NOT NULL
            ORDER BY id
        """).format(vertices=vertices)
        self.cur.execute(query)
        return {int(node_id): (float(lon), float(lat)) for node_id, lon, lat in self.cur.fetchall()}

    def fetch_consumer_connection_mapping(self, plz: int) -> dict[int, list[int]]:
        """Return the building vertices of every consumer connection point of a PLZ.

        Returns:
            ``{connection_point: [vertice_id, ...]}`` over all loaded consumer buildings.
        """
        buildings = sql.Identifier("pg_temp", plz_table_name("buildings_tem", plz))
        query = sql.SQL("""
            SELECT COALESCE(agg_connection_point, connection_point) AS connection_point,
                   vertice_id
            FROM {buildings}
            WHERE type != 'Transformer'
              AND peak_load_in_kw != 0
              AND COALESCE(agg_connection_point, connection_point) IS NOT NULL
              AND vertice_id IS NOT NULL
            ORDER BY connection_point, vertice_id
        """).format(buildings=buildings)
        self.cur.execute(query)
        mapping: dict[int, list[int]] = {}
        for connection_point, vertice_id in self.cur.fetchall():
            mapping.setdefault(int(connection_point), []).append(int(vertice_id))
        return mapping

    def get_vertices_from_bcid(
        self, plz: int, kcid: int, bcid: int
    ) -> tuple[dict[int, float], int, dict[int, tuple[int, ...]]]:
        """Return the street distances and paths from the consumers of a building cluster to its transformer.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.

        Returns:
            ``(vertice_cost_dict, ont, paths_to_transformer)``: distance in metres from the
            transformer vertex to every building vertex and connection point of the cluster
            (ascending), the transformer vertex, and the node path from each of these vertices
            to the transformer.
        """
        ont = self.get_ont_info_from_bc(plz, kcid, bcid)["ont_vertice_id"]

        consumer_query = """SELECT vertice_id
                            FROM buildings_tem
                            WHERE plz = %(p)s
                              AND kcid = %(k)s
                              AND bcid = %(b)s
                              AND peak_load_in_kw != 0;"""
        self.cur.execute(consumer_query, {"p": plz, "k": kcid, "b": bcid})
        consumer = [t[0] for t in self.cur.fetchall()]

        connection_query = """SELECT DISTINCT COALESCE(agg_connection_point, connection_point) AS connection_point
                              FROM buildings_tem
                              WHERE plz = %(p)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s
                                AND peak_load_in_kw != 0;"""
        self.cur.execute(connection_query, {"p": plz, "k": kcid, "b": bcid})
        connection = [t[0] for t in self.cur.fetchall()]

        target_vertices = list(dict.fromkeys(consumer + connection))
        if not target_vertices:
            return {}, int(ont), {}

        vertices_query = f"""SELECT DISTINCT node, agg_cost
                             FROM pgr_dijkstra(
                                 {WAYS_TEM_EDGES_SQL}::text,
                                 %(o)s, %(c)s::integer[], false)
                             ORDER BY agg_cost;"""
        self.cur.execute(vertices_query, {"o": ont, "c": target_vertices})
        data = self.cur.fetchall()
        vertice_cost_dict = {
            row[0]: row[1]
            for row in data
            if row[0] in consumer or row[0] in connection
        }

        path_query = f"""SELECT start_vid, path_seq, node, agg_cost
                          FROM pgr_dijkstra(
                              {WAYS_TEM_EDGES_SQL}::text,
                              %(c)s::bigint[], %(o)s::bigint, false)
                          ORDER BY start_vid, path_seq;"""
        self.cur.execute(path_query, {"o": ont, "c": target_vertices})
        path_data = self.cur.fetchall()
        _, paths_to_transformer = self._routing_results_from_rows(path_data)

        return vertice_cost_dict, int(ont), paths_to_transformer

    @staticmethod
    def _routing_results_from_rows(data) -> tuple[dict[int, float], dict[int, tuple[int, ...]]]:
        """Turn many-to-one ``pgr_dijkstra`` rows into total costs and node paths per start vertex."""
        costs_by_vertex: dict[int, float] = {}
        path_nodes: dict[int, list[int]] = {}
        for start_vid, _path_seq, node, agg_cost in sorted(data, key=lambda row: (row[0], row[1])):
            start_vid = int(start_vid)
            path_nodes.setdefault(start_vid, []).append(int(node))
            costs_by_vertex[start_vid] = float(agg_cost)

        ordered_costs = dict(
            sorted(costs_by_vertex.items(), key=lambda item: (item[1], item[0]))
        )
        paths_to_transformer = {
            start_vid: tuple(nodes) for start_vid, nodes in path_nodes.items()
        }
        return ordered_costs, paths_to_transformer

    def get_ont_info_from_bc(self, plz: int, kcid: int, bcid: int) -> dict | None:
        """Return ``ont_vertice_id`` and ``transformer_rated_power`` of a grid, or ``None`` if it does not exist."""
        query = """SELECT ont_vertice_id, transformer_rated_power
                                     FROM pylovo.grid_result
                   WHERE version_id = %(v)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s
                     AND plz = %(p)s; """
        params = {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid}
        self.cur.execute(query, params)
        info = self.cur.fetchall()
        if not info:
            self.logger.debug(f"found no ont information for kcid {kcid}, bcid {bcid}")
            return None

        return {"ont_vertice_id": info[0][0], "transformer_rated_power": info[0][1]}

    def get_ont_geom_from_bcid(self, plz: int, kcid: int, bcid: int):
        """Return the WGS84 ``(lon, lat)`` of a grid's transformer position, or ``None``."""
        query = """SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326))
                                     FROM pylovo.transformer_positions tp
                                                        JOIN pylovo.grid_result gr
                                 ON tp.grid_result_id = gr.grid_result_id
                   WHERE gr.version_id = %(v)s
                     AND plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s;"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid})
        return self.cur.fetchone()

    def get_transformer_rated_power_from_bcid(self, plz: int, kcid: int, bcid: int) -> int:
        """Return ``grid_result.transformer_rated_power`` (kVA) of a grid."""
        query = """SELECT transformer_rated_power
                                     FROM pylovo.grid_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s;"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid})
        return self.cur.fetchone()[0]

    def get_node_geom(self, vid: int):
        """Return the WGS84 ``(lon, lat)`` of a vertex of ``ways_tem_vertices_pgr``, or ``None``."""
        query = """SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326))
                   FROM ways_tem_vertices_pgr
                   WHERE id = %(id)s;"""
        self.cur.execute(query, {"id": vid})
        return self.cur.fetchone()

    def get_consumer_vertices_from_connection_points(self, connection_points: list[int]) -> list[tuple[int, int]]:
        """Return ``(connection_point, vertice_id)`` of the loaded consumer buildings at the given connection points."""
        query = """SELECT COALESCE(agg_connection_point, connection_point) AS connection_point, vertice_id
                   FROM buildings_tem
                   WHERE COALESCE(agg_connection_point, connection_point) IN %(c)s
                     AND type != 'Transformer'
                     AND peak_load_in_kw != 0;"""
        self.cur.execute(query, {'c': tuple(connection_points)})
        return [
            (int(connection_point), int(vertice_id))
            for connection_point, vertice_id in self.cur.fetchall()
        ]

    def get_path_to_bus(self, vertice: int, ont: int) -> list:
        """Return the nodes of the shortest street path from ``vertice`` to the transformer vertex ``ont``."""
        query = f"""SELECT node
                   FROM pgr_Dijkstra(
                           {WAYS_TEM_EDGES_SQL}, %(v)s, %(o)s,
                           false);"""
        self.cur.execute(query, {"o": ont, "v": vertice})
        return [row[0] for row in self.cur.fetchall()]

    def rebuild_lines_result_helpers_for_split_topology(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        split_edges: list[dict[str, int]],
        offset_m: float = 0.5,
    ) -> None:
        """Rebuild the ``lines_result_helper`` rows of one grid.

        Lines leaving a feeder split node run on top of each other; each ``split_edges`` entry
        gets a copy shifted sideways by ``offset_m * offset_rank``. Afterwards the remaining
        overlapping feeder lines are shifted as well (``_insert_lines_result_overlap_helpers``).

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            split_edges: Dicts with ``from_bus``, ``to_bus`` and ``offset_rank``.
            offset_m: Offset per rank in metres.
        """
        grid_result_id = self.get_grid_result_id(int(plz), int(kcid), int(bcid))
        if grid_result_id is None:
            return

        self.cur.execute(
            "DELETE FROM pylovo.lines_result_helper WHERE grid_result_id = %(grid_result_id)s;",
            {"grid_result_id": grid_result_id},
        )

        if split_edges:
            insert_query = f"""
                INSERT INTO pylovo.lines_result_helper (
                    source_lines_result_id,
                    grid_result_id,
                    geom,
                    line_name,
                    std_type,
                    from_bus,
                    to_bus,
                    parallel,
                    length_km,
                    helper_type
                )
                SELECT
                    lr.lines_result_id,
                    lr.grid_result_id,
                    {_OFFSET_HELPER_GEOM_SQL},
                    lr.line_name,
                    lr.std_type,
                    lr.from_bus,
                    lr.to_bus,
                    lr.parallel,
                    lr.length_km,
                    'split_topology_offset'
                FROM pylovo.lines_result lr
                {_offset_line_lateral_sql("%(offset_m)s * %(offset_rank)s")}
                WHERE lr.grid_result_id = %(grid_result_id)s
                  AND lr.from_bus = %(from_bus)s
                  AND lr.to_bus = %(to_bus)s;
            """
            seen_edges = set()
            for edge in split_edges:
                from_bus = int(edge["from_bus"])
                to_bus = int(edge["to_bus"])
                offset_rank = int(edge["offset_rank"])
                edge_key = (from_bus, to_bus, offset_rank)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                self.cur.execute(
                    insert_query,
                    {
                        "grid_result_id": grid_result_id,
                        "from_bus": from_bus,
                        "to_bus": to_bus,
                        "offset_rank": offset_rank,
                        "offset_m": float(offset_m),
                    },
                )

        self._insert_lines_result_overlap_helpers(
            grid_result_id=grid_result_id,
            offset_m=offset_m,
        )

    def _insert_lines_result_overlap_helpers(
        self,
        grid_result_id: int,
        offset_m: float,
        min_overlap_m: float = 10.0,
        min_collision_overlap_m: float = 0.01,
        max_offset_rank: int = 10,
    ) -> None:
        """Add shifted helper rows for feeder lines that still share their route with another feeder line.

        Of every pair of feeder lines overlapping by at least ``min_overlap_m``, the shorter line
        gets a helper at the smallest offset rank (up to ``max_offset_rank``, both sides) where it
        no longer overlaps any visible line by ``min_collision_overlap_m`` or more.
        """
        feeder_cable_names = [str(name) for name in FEEDER_CABLES["name"].dropna().tolist()]
        if not feeder_cable_names:
            return

        source_query = """
            WITH candidate_pairs AS (
                SELECT
                    a.lines_result_id AS a_id,
                    b.lines_result_id AS b_id,
                    ST_Length(a.geom) AS a_length_m,
                    ST_Length(b.geom) AS b_length_m,
                    ST_Length(
                        ST_CollectionExtract(ST_Intersection(a.geom, b.geom), 2)
                    ) AS overlap_m
                FROM pylovo.lines_result a
                JOIN pylovo.lines_result b
                  ON a.grid_result_id = b.grid_result_id
                 AND a.lines_result_id < b.lines_result_id
                 AND a.geom && b.geom
                WHERE a.grid_result_id = %(grid_result_id)s
                  AND a.std_type = ANY(%(feeder_cables)s)
                  AND b.std_type = ANY(%(feeder_cables)s)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pylovo.lines_result_helper helper_a
                      WHERE helper_a.source_lines_result_id = a.lines_result_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pylovo.lines_result_helper helper_b
                      WHERE helper_b.source_lines_result_id = b.lines_result_id
                  )
            ),
            overlap_sources AS (
                SELECT DISTINCT ON (source_lines_result_id)
                    source_lines_result_id,
                    overlap_m
                FROM (
                    SELECT
                        CASE
                            WHEN a_length_m <= b_length_m THEN a_id
                            ELSE b_id
                        END AS source_lines_result_id,
                        overlap_m
                    FROM candidate_pairs
                    WHERE overlap_m >= %(min_overlap_m)s
                ) sources
                ORDER BY source_lines_result_id, overlap_m DESC
            )
            SELECT source_lines_result_id
            FROM overlap_sources
            ORDER BY overlap_m DESC, source_lines_result_id;
        """
        self.cur.execute(
            source_query,
            {
                "grid_result_id": int(grid_result_id),
                "feeder_cables": feeder_cable_names,
                "min_overlap_m": float(min_overlap_m),
            },
        )
        source_rows = self.cur.fetchall()

        insert_query = f"""
            WITH source_line AS (
                SELECT *
                FROM pylovo.lines_result
                WHERE lines_result_id = %(source_lines_result_id)s
            ),
            candidate_ranks AS (
                SELECT rank_abs * sign AS offset_rank
                FROM generate_series(1, %(max_offset_rank)s) AS rank_abs
                CROSS JOIN (VALUES (1), (-1)) AS signs(sign)
            ),
            candidate_geoms AS (
                SELECT
                    lr.*,
                    candidate_ranks.offset_rank,
                    {_OFFSET_HELPER_GEOM_SQL} AS helper_geom
                FROM source_line lr
                CROSS JOIN candidate_ranks
                {_offset_line_lateral_sql("%(offset_m)s * candidate_ranks.offset_rank")}
            ),
            visible_geoms AS (
                SELECT lr.lines_result_id::bigint AS source_id, lr.geom
                FROM pylovo.lines_result lr
                WHERE lr.grid_result_id = %(grid_result_id)s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pylovo.lines_result_helper helper
                      WHERE helper.source_lines_result_id = lr.lines_result_id
                  )
                UNION ALL
                SELECT lrh.source_lines_result_id, lrh.geom
                FROM pylovo.lines_result_helper lrh
                WHERE lrh.grid_result_id = %(grid_result_id)s
            ),
            free_candidate AS (
                SELECT candidate_geoms.*
                FROM candidate_geoms
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM visible_geoms visible
                    WHERE visible.geom && candidate_geoms.helper_geom
                      AND ST_Length(
                          ST_CollectionExtract(
                              ST_Intersection(candidate_geoms.helper_geom, visible.geom),
                              2
                          )
                      ) >= %(min_collision_overlap_m)s
                )
                ORDER BY ABS(offset_rank), offset_rank DESC
                LIMIT 1
            )
            INSERT INTO pylovo.lines_result_helper (
                source_lines_result_id,
                grid_result_id,
                geom,
                line_name,
                std_type,
                from_bus,
                to_bus,
                parallel,
                length_km,
                helper_type
            )
            SELECT
                lr.lines_result_id,
                lr.grid_result_id,
                lr.helper_geom,
                lr.line_name,
                lr.std_type,
                lr.from_bus,
                lr.to_bus,
                lr.parallel,
                lr.length_km,
                'shared_route_overlap_offset'
            FROM free_candidate lr;
        """
        for (source_lines_result_id,) in source_rows:
            self.cur.execute(
                insert_query,
                {
                    "grid_result_id": int(grid_result_id),
                    "source_lines_result_id": int(source_lines_result_id),
                    "offset_m": float(offset_m),
                    "min_collision_overlap_m": float(min_collision_overlap_m),
                    "max_offset_rank": int(max_offset_rank),
                },
            )

    def rebuild_split_points_for_split_topology(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        split_nodes: list[int],
    ) -> None:
        """Rebuild the ``split_points`` rows of one grid: its feeder split nodes except the transformer node.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            split_nodes: Bus IDs where a feeder splits.
        """
        selected_grid_query = """
            SELECT grid_result_id, ont_vertice_id
            FROM pylovo.grid_result
            WHERE version_id = %(v)s
              AND plz = %(plz)s
              AND kcid = %(kcid)s
              AND bcid = %(bcid)s
            LIMIT 1
        """
        params = {"v": VERSION_ID, "plz": int(plz), "kcid": int(kcid), "bcid": int(bcid)}
        self.cur.execute(selected_grid_query, params)
        selected_grid = self.cur.fetchone()
        if selected_grid is None:
            return

        grid_result_id = int(selected_grid[0])
        ont_vertice_id = None if selected_grid[1] is None else int(selected_grid[1])
        self.cur.execute(
            "DELETE FROM pylovo.split_points WHERE grid_result_id = %(grid_result_id)s;",
            {"grid_result_id": grid_result_id},
        )

        unique_nodes = sorted(
            {
                int(node)
                for node in split_nodes
                if node is not None and (ont_vertice_id is None or int(node) != ont_vertice_id)
            }
        )
        if not unique_nodes:
            return

        feeder_cable_names = [str(name) for name in FEEDER_CABLES["name"].dropna().tolist()]
        if not feeder_cable_names:
            return

        insert_query = """
            INSERT INTO pylovo.split_points (
                grid_result_id,
                split_bus,
                outgoing_count,
                split_type,
                geom
            )
            VALUES (
                %(grid_result_id)s,
                %(split_bus)s,
                (
                    SELECT COUNT(*)
                    FROM pylovo.lines_result lr_count
                    WHERE lr_count.grid_result_id = %(grid_result_id)s
                      AND lr_count.from_bus = %(split_bus)s
                                            AND lr_count.std_type = ANY(%(feeder_cables)s)
                ),
                                'feeder_split_topology',
                COALESCE(
                    (
                        SELECT ST_StartPoint(lr_start.geom)
                        FROM pylovo.lines_result lr_start
                        WHERE lr_start.grid_result_id = %(grid_result_id)s
                          AND lr_start.from_bus = %(split_bus)s
                                                    AND lr_start.std_type = ANY(%(feeder_cables)s)
                        ORDER BY lr_start.lines_result_id
                        LIMIT 1
                    ),
                    (
                        SELECT ST_EndPoint(lr_end.geom)
                        FROM pylovo.lines_result lr_end
                        WHERE lr_end.grid_result_id = %(grid_result_id)s
                          AND lr_end.to_bus = %(split_bus)s
                                                    AND lr_end.std_type = ANY(%(feeder_cables)s)
                        ORDER BY lr_end.lines_result_id
                        LIMIT 1
                    )
                )
            );
        """
        for split_bus in unique_nodes:
            self.cur.execute(
                insert_query,
                {
                    "grid_result_id": grid_result_id,
                    "split_bus": split_bus,
                    "feeder_cables": feeder_cable_names,
                },
            )

    def rebuild_lines_result_view_for_grid(self, plz: int, kcid: int, bcid: int) -> None:
        """Rebuild the ``lines_result_cache`` rows of one grid for GIS inspection.

        Feeder lines of the same feeder section are merged into one row
        (``helper_type = 'merged_feeder_section'``); all other lines, and helpers in place of
        their source lines, are copied as they are.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
        """
        grid_result_id = self.get_grid_result_id(int(plz), int(kcid), int(bcid))
        if grid_result_id is None:
            return

        self.cur.execute(
            "DELETE FROM pylovo.lines_result_cache WHERE grid_result_id = %(grid_result_id)s;",
            {"grid_result_id": grid_result_id},
        )
        insert_query = """
            INSERT INTO pylovo.lines_result_cache (
                is_helper,
                source_lines_result_id,
                helper_type,
                grid_result_id,
                geom,
                line_name,
                std_type,
                from_bus,
                to_bus,
                parallel,
                length_km,
                feeder_section_id,
                version_id,
                kcid,
                bcid,
                plz
            )
            WITH selected_grid AS (
                SELECT grid_result_id, version_id, kcid, bcid, plz
                FROM pylovo.grid_result
                WHERE grid_result_id = %(grid_result_id)s
            ),
            feeder_equipment AS (
                SELECT ed.name
                FROM pylovo.equipment_data ed
                JOIN selected_grid sg ON sg.version_id = ed.version_id
                WHERE ed.grid_role = 'feeder'
            ),
            visible_lines AS (
                SELECT
                    lr.lines_result_id::bigint AS source_id,
                    false AS is_helper,
                    NULL::bigint AS source_lines_result_id,
                    NULL::varchar(50) AS helper_type,
                    lr.grid_result_id,
                    lr.geom,
                    lr.line_name,
                    lr.std_type,
                    lr.from_bus,
                    lr.to_bus,
                    lr.parallel,
                    lr.length_km,
                    lr.feeder_section_id
                FROM pylovo.lines_result lr
                JOIN selected_grid sg ON sg.grid_result_id = lr.grid_result_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pylovo.lines_result_helper lrh
                    WHERE lrh.source_lines_result_id = lr.lines_result_id
                )
                UNION ALL
                SELECT
                    -lrh.lines_result_helper_id::bigint AS source_id,
                    true AS is_helper,
                    lrh.source_lines_result_id,
                    lrh.helper_type,
                    lrh.grid_result_id,
                    lrh.geom,
                    lrh.line_name,
                    lrh.std_type,
                    lrh.from_bus,
                    lrh.to_bus,
                    lrh.parallel,
                    lrh.length_km,
                    source_lr.feeder_section_id
                FROM pylovo.lines_result_helper lrh
                JOIN selected_grid sg ON sg.grid_result_id = lrh.grid_result_id
                JOIN pylovo.lines_result source_lr
                  ON source_lr.lines_result_id = lrh.source_lines_result_id
            ),
            feeder_section_lines AS (
                SELECT *
                FROM visible_lines
                WHERE feeder_section_id IS NOT NULL
                  AND std_type IN (SELECT name FROM feeder_equipment)
            ),
            passthrough_lines AS (
                SELECT *
                FROM visible_lines
                WHERE feeder_section_id IS NULL
                   OR std_type NOT IN (SELECT name FROM feeder_equipment)
                   OR std_type IS NULL
            ),
            section_start AS (
                SELECT fsl.grid_result_id, fsl.feeder_section_id, fsl.from_bus
                FROM feeder_section_lines fsl
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM feeder_section_lines incoming
                    WHERE incoming.grid_result_id = fsl.grid_result_id
                      AND incoming.feeder_section_id = fsl.feeder_section_id
                      AND incoming.to_bus = fsl.from_bus
                )
                GROUP BY fsl.grid_result_id, fsl.feeder_section_id, fsl.from_bus
            ),
            section_end AS (
                SELECT fsl.grid_result_id, fsl.feeder_section_id, fsl.to_bus
                FROM feeder_section_lines fsl
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM feeder_section_lines outgoing
                    WHERE outgoing.grid_result_id = fsl.grid_result_id
                      AND outgoing.feeder_section_id = fsl.feeder_section_id
                      AND outgoing.from_bus = fsl.to_bus
                )
                GROUP BY fsl.grid_result_id, fsl.feeder_section_id, fsl.to_bus
            ),
            merged_feeder_lines AS (
                SELECT
                    true AS is_helper,
                    NULL::bigint AS source_lines_result_id,
                    'merged_feeder_section'::varchar(50) AS helper_type,
                    fsl.grid_result_id,
                    ST_LineMerge(ST_Collect(fsl.geom)) AS geom,
                    CONCAT('M', fsl.grid_result_id, '_', fsl.feeder_section_id) AS line_name,
                    STRING_AGG(DISTINCT fsl.std_type, '+') AS std_type,
                    MIN(ss.from_bus) AS from_bus,
                    MIN(se.to_bus) AS to_bus,
                    MAX(fsl.parallel) AS parallel,
                    SUM(fsl.length_km) AS length_km,
                    fsl.feeder_section_id
                FROM feeder_section_lines fsl
                LEFT JOIN section_start ss
                  ON ss.grid_result_id = fsl.grid_result_id
                 AND ss.feeder_section_id = fsl.feeder_section_id
                LEFT JOIN section_end se
                  ON se.grid_result_id = fsl.grid_result_id
                 AND se.feeder_section_id = fsl.feeder_section_id
                GROUP BY fsl.grid_result_id, fsl.feeder_section_id
            )
            SELECT
                mfl.is_helper,
                mfl.source_lines_result_id,
                mfl.helper_type,
                mfl.grid_result_id,
                mfl.geom,
                mfl.line_name,
                mfl.std_type,
                mfl.from_bus,
                mfl.to_bus,
                mfl.parallel,
                mfl.length_km,
                mfl.feeder_section_id,
                sg.version_id,
                sg.kcid,
                sg.bcid,
                sg.plz
            FROM merged_feeder_lines mfl
            JOIN selected_grid sg ON sg.grid_result_id = mfl.grid_result_id
            UNION ALL
            SELECT
                pl.is_helper,
                pl.source_lines_result_id,
                pl.helper_type,
                pl.grid_result_id,
                pl.geom,
                pl.line_name,
                pl.std_type,
                pl.from_bus,
                pl.to_bus,
                pl.parallel,
                pl.length_km,
                pl.feeder_section_id,
                sg.version_id,
                sg.kcid,
                sg.bcid,
                sg.plz
            FROM passthrough_lines pl
            JOIN selected_grid sg ON sg.grid_result_id = pl.grid_result_id;
        """
        self.cur.execute(insert_query, {"grid_result_id": grid_result_id})

    @staticmethod
    def _line_wkb_hex(geom: list, *, plz: int, kcid: int, bcid: int, from_bus: int, to_bus: int) -> str:
        """Return the WKB hex of a line from ``geom``; falls back to a straight line between its end points.

        Raises:
            ValueError: If ``geom`` does not contain two usable points.
        """
        try:
            line_geom = LineString(geom)
            if line_geom.is_empty or len(line_geom.coords) < 2:
                raise ValueError("line geometry has fewer than two coordinates")
        except Exception as geom_error:
            fallback_points = [
                point for point in geom
                if isinstance(point, (list, tuple)) and len(point) >= 2
            ]
            if len(fallback_points) < 2:
                raise ValueError(
                    f"Cannot build fallback line geometry for plz={plz}, kcid={kcid}, bcid={bcid}, "
                    f"from_bus={from_bus}, to_bus={to_bus}: {geom_error}"
                ) from geom_error
            line_geom = LineString([fallback_points[0], fallback_points[-1]])
        return line_geom.wkb_hex

    def insert_lines_batch(
        self,
        records: list[dict],
        plz: int,
        kcid: int,
        bcid: int,
        page_size: int = 1000,
    ) -> None:
        """Insert the cables of one grid into ``lines_result``.

        Args:
            records: Dicts with ``geom`` (WGS84 coordinates), ``line_name``, ``std_type``,
                ``from_bus``, ``to_bus``, ``length_km`` and optionally ``parallel`` and
                ``feeder_section_id``.
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            page_size: Rows per ``INSERT`` statement.

        Raises:
            ValueError: If the grid does not exist or ``page_size`` is not positive.
        """
        if not records:
            return
        if page_size <= 0:
            raise ValueError("page_size must be positive")

        grid_result_id = self.get_grid_result_id(plz=plz, kcid=kcid, bcid=bcid)
        if grid_result_id is None:
            raise ValueError(
                f"Cannot persist lines: grid_result_id not found for plz={plz}, kcid={kcid}, bcid={bcid}"
            )

        values = []
        for record in records:
            from_bus = int(record["from_bus"])
            to_bus = int(record["to_bus"])
            values.append(
                (
                    grid_result_id,
                    self._line_wkb_hex(
                        record["geom"],
                        plz=int(plz),
                        kcid=int(kcid),
                        bcid=int(bcid),
                        from_bus=from_bus,
                        to_bus=to_bus,
                    ),
                    record["line_name"],
                    record["std_type"],
                    from_bus,
                    to_bus,
                    int(record.get("parallel", 1)),
                    record["length_km"],
                    None if record.get("feeder_section_id") is None else int(record["feeder_section_id"]),
                )
            )

        insert_query = """
            INSERT INTO pylovo.lines_result (
                grid_result_id, geom, line_name, std_type, from_bus, to_bus,
                parallel, length_km, feeder_section_id
            ) VALUES %s
        """
        template = (
            "(%s, ST_Transform(ST_SetSRID(%s::geometry, 4326), "
            f"{TARGET_EPSG}), %s, %s, %s, %s, %s, %s, %s)"
        )
        execute_values(
            self.cur,
            insert_query,
            values,
            template=template,
            page_size=page_size,
        )

    def is_grid_generated(self, plz: int):
        """Return whether the PLZ already has results (a ``postcode_result`` row) in the active version."""
        query = """
            SELECT 1
            FROM pylovo.postcode_result
            WHERE version_id = %(version_id)s AND postcode_result_plz = %(plz)s
            LIMIT 1;
        """

        self.cur.execute(query, {"version_id": VERSION_ID, "plz": plz})
        result = self.cur.fetchone()
        return result is not None