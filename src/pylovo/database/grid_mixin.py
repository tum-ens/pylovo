import warnings
from abc import ABC

import pandapower as pp
from shapely.geometry import LineString

from pylovo.config_loader import *
from pylovo.database.base_mixin import BaseMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class GridMixin(BaseMixin, ABC):
    def __init__(self):
        super().__init__()

    def fetch_cables(self) -> list:
        query = f"""SELECT name,
                       r_mohm_per_km / 1000.0 as r_ohm_per_km,
                       x_mohm_per_km / 1000.0 as x_ohm_per_km,
                       max_i_a / 1000.0       as max_i_ka
            FROM pylovo.equipment_data
                WHERE typ = 'Cable' \
                """
        self.cur.execute(query)
        return self.cur.fetchall()

    def get_vertices_from_bcid(self, plz: int, kcid: int, bcid: int) -> tuple[dict, int]:
        ont = self.get_ont_info_from_bc(plz, kcid, bcid)["ont_vertice_id"]

        consumer_query = """SELECT vertice_id
                            FROM buildings_tem
                            WHERE plz = %(p)s
                              AND kcid = %(k)s
                              AND bcid = %(b)s;"""
        self.cur.execute(consumer_query, {"p": plz, "k": kcid, "b": bcid})
        consumer = [t[0] for t in self.cur.fetchall()]

        connection_query = """SELECT DISTINCT connection_point
                              FROM buildings_tem
                              WHERE plz = %(p)s
                                AND kcid = %(k)s
                                AND bcid = %(b)s;"""
        self.cur.execute(connection_query, {"p": plz, "k": kcid, "b": bcid})
        connection = [t[0] for t in self.cur.fetchall()]

        vertices_query = """ SELECT DISTINCT node, agg_cost
                             FROM pgr_dijkstra(
                                     'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem'::text,
                                     %(o)s, %(c)s::integer[], false)
                             ORDER BY agg_cost;"""
        self.cur.execute(vertices_query, {"o": ont, "c": consumer})
        data = self.cur.fetchall()
        vertice_cost_dict = {t[0]: t[1] for t in data if t[0] in consumer or t[0] in connection}

        return vertice_cost_dict, ont

    def get_ont_info_from_bc(self, plz: int, kcid: int, bcid: int) -> dict | None:

        query = f"""SELECT ont_vertice_id, transformer_rated_power
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
        query = f"""SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326))
                                     FROM pylovo.transformer_positions tp
                                                        JOIN pylovo.grid_result gr
                                 ON tp.grid_result_id = gr.grid_result_id
                   WHERE gr.version_id = %(v)s
                     AND plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s;"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid})
        geo = self.cur.fetchone()

        return geo

    def get_transformer_rated_power_from_bcid(self, plz: int, kcid: int, bcid: int) -> int:
        query = f"""SELECT transformer_rated_power
                                     FROM pylovo.grid_result
                   WHERE version_id = %(v)s
                     AND plz = %(p)s
                     AND kcid = %(k)s
                     AND bcid = %(b)s;"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz, "k": kcid, "b": bcid})
        transformer_rated_power = self.cur.fetchone()[0]

        return transformer_rated_power

    def get_node_geom(self, vid: int):
        query = """SELECT ST_X(ST_Transform(geom, 4326)), ST_Y(ST_Transform(geom, 4326))
                   FROM ways_tem_vertices_pgr
                   WHERE id = %(id)s;"""
        self.cur.execute(query, {"id": vid})
        geo = self.cur.fetchone()

        return geo

    def get_vertices_from_connection_points(self, connection: list) -> list:
        query = """SELECT vertice_id
                   FROM buildings_tem
                   WHERE connection_point IN %(c)s
                     AND type != 'Transformer';"""
        self.cur.execute(query, {"c": tuple(connection)})
        data = self.cur.fetchall()
        return [t[0] for t in data]

    def get_path_to_bus(self, vertice: int, ont: int) -> list:
        """routing problem: find the shortest path from vertice to the ont (ortsnetztrafo)"""
        query = """SELECT node
                   FROM pgr_Dijkstra(
                           'SELECT way_id as id, source, target, cost, reverse_cost FROM ways_tem', %(v)s, %(o)s,
                           false);"""
        """query = WITH
                    dijkstra AS(
                        SELECT * FROM pgr_Dijkstra(
                                        'SELECT way_id, source, target, cost, reverse_cost FROM ways_tem', %(v)s, %(o)s, false)
                    ),
                        get_geom AS(
                            SELECT dijkstra. *,
                            -- adjusting directionality
                                CASE
                                    WHEN dijkstra.node = ways.source THEN geom
                                    ELSE ST_Reverse(geom)
                                END AS route_geom
                            FROM dijkstra JOIN ways ON(edge=way_id)
                            ORDER BY seq)
                        SELECT seq, cost,
                        degrees(ST_azimuth(ST_StartPoint(route_geom), ST_EndPoint(route_geom))) AS azimuth,
                        ST_AsText(route_geom),
                        route_geom
                    FROM get_geom
                    ORDER BY seq;"""
        self.cur.execute(query, {"o": ont, "v": vertice})
        data = self.cur.fetchall()
        way_list = [t[0] for t in data]

        return way_list

    def _ensure_lines_result_visualization_schema(self) -> None:
        if getattr(self, "_lines_result_visualization_schema_checked", False):
            return

        self.cur.execute("ALTER TABLE pylovo.lines_result ADD COLUMN IF NOT EXISTS parallel integer;")
        self.cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS pylovo.lines_result_helper (
                lines_result_helper_id SERIAL PRIMARY KEY,
                source_lines_result_id bigint NOT NULL,
                grid_result_id bigint NOT NULL,
                geom geometry(Geometry,{TARGET_EPSG}),
                line_name varchar(50),
                std_type varchar(50),
                from_bus integer,
                to_bus integer,
                parallel integer,
                length_km double precision,
                helper_type varchar(50),
                CONSTRAINT fk_lines_result_helper_source_line
                    FOREIGN KEY (source_lines_result_id)
                    REFERENCES pylovo.lines_result (lines_result_id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_lines_result_helper_grid_result
                    FOREIGN KEY (grid_result_id)
                    REFERENCES pylovo.grid_result (grid_result_id)
                    ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_lines_result_helper_grid_result_id
                ON pylovo.lines_result_helper (grid_result_id);
            CREATE INDEX IF NOT EXISTS idx_lines_result_helper_geom
                ON pylovo.lines_result_helper USING gist (geom);

            CREATE TABLE IF NOT EXISTS pylovo.split_points (
                split_point_id SERIAL PRIMARY KEY,
                grid_result_id bigint NOT NULL,
                split_bus integer NOT NULL,
                outgoing_count integer,
                split_type varchar(50),
                geom geometry(Point,{TARGET_EPSG}),
                CONSTRAINT fk_split_points_grid_result
                    FOREIGN KEY (grid_result_id)
                    REFERENCES pylovo.grid_result (grid_result_id)
                    ON DELETE CASCADE,
                CONSTRAINT uq_split_points_grid_bus
                    UNIQUE (grid_result_id, split_bus)
            );
            CREATE INDEX IF NOT EXISTS idx_split_points_grid_result_id
                ON pylovo.split_points (grid_result_id);
            CREATE INDEX IF NOT EXISTS idx_split_points_geom
                ON pylovo.split_points USING gist (geom);
            """
        )
        self.cur.execute("DROP MATERIALIZED VIEW IF EXISTS pylovo.lines_result_with_grid CASCADE;")
        self.cur.execute(
            """
            CREATE MATERIALIZED VIEW pylovo.lines_result_with_grid AS (
                SELECT
                    lr.lines_result_id as id,
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
                    gr.version_id, gr.kcid, gr.bcid, gr.plz
                FROM pylovo.lines_result lr
                JOIN pylovo.grid_result gr ON lr.grid_result_id = gr.grid_result_id
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM pylovo.lines_result_helper lrh
                    WHERE lrh.source_lines_result_id = lr.lines_result_id
                )
                UNION ALL
                SELECT
                    -lrh.lines_result_helper_id as id,
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
                    gr.version_id, gr.kcid, gr.bcid, gr.plz
                FROM pylovo.lines_result_helper lrh
                JOIN pylovo.grid_result gr ON lrh.grid_result_id = gr.grid_result_id
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lines_result_with_grid_uq_id
                ON pylovo.lines_result_with_grid (id);
            CREATE INDEX IF NOT EXISTS idx_lines_result_with_grid_geom
                ON pylovo.lines_result_with_grid USING gist (geom);
            """
        )
        self._lines_result_visualization_schema_checked = True

    def rebuild_lines_result_helpers_for_split_topology(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        split_edges: list[dict[str, int]],
        offset_m: float = 0.5,
    ) -> None:
        """Create shifted helper rows for real lines that leave split nodes."""
        self._ensure_lines_result_visualization_schema()

        selected_grid_query = """
            SELECT grid_result_id
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
        self.cur.execute(
            "DELETE FROM pylovo.lines_result_helper WHERE grid_result_id = %(grid_result_id)s;",
            {"grid_result_id": grid_result_id},
        )

        if split_edges:
            insert_query = """
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
                    CASE
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
                    END,
                    lr.line_name,
                    lr.std_type,
                    lr.from_bus,
                    lr.to_bus,
                    lr.parallel,
                    lr.length_km,
                    'split_topology_offset'
                FROM pylovo.lines_result lr
                CROSS JOIN LATERAL (
                    SELECT ST_LineMerge(ST_OffsetCurve(lr.geom, %(offset_m)s * %(offset_rank)s)) AS offset_line
                ) AS offset_geom
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
        """Create helper rows for remaining feeder lines that share route geometry."""
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

        insert_query = """
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
                    CASE
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
                    END AS helper_geom
                FROM source_line lr
                CROSS JOIN candidate_ranks
                CROSS JOIN LATERAL (
                    SELECT ST_LineMerge(
                        ST_OffsetCurve(lr.geom, %(offset_m)s * candidate_ranks.offset_rank)
                    ) AS offset_line
                ) AS offset_geom
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
        """Store feeder split nodes (excluding transformer node) for GIS inspection."""
        self._ensure_lines_result_visualization_schema()

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

    def insert_lines(self, geom: list, plz: int, bcid: int, kcid: int, line_name: str, std_type: str, from_bus: int,
            to_bus: int, length_km: float, parallel: int = 1) -> None:
        """writes lines / cables that belong to a network into the database"""
        self._ensure_lines_result_visualization_schema()

        line_insertion_query = f"""INSERT INTO pylovo.lines_result (grid_result_id,
                                                            geom,
                                                            line_name,
                                                            std_type,
                                                            from_bus,
                                                            to_bus,
                                                            parallel,
                                                            length_km)
                                  VALUES ((SELECT grid_result_id
                           FROM pylovo.grid_result
                                           WHERE version_id = %(v)s
                                             AND plz = %(plz)s
                                             AND kcid = %(kcid)s
                                             AND bcid = %(bcid)s),
                                          ST_Transform(ST_SetSRID(%(geom)s::geometry, 4326), {TARGET_EPSG}),
                                          %(line_name)s,
                                          %(std_type)s,
                                          %(from_bus)s,
                                          %(to_bus)s,
                                          %(parallel)s,
                                          %(length_km)s); """
        self.cur.execute(line_insertion_query,
                         {"v": VERSION_ID, "geom": LineString(geom).wkb_hex, "plz": int(plz), "bcid": int(bcid),
                          "kcid": int(kcid), "line_name": line_name, "std_type": std_type, "from_bus": int(from_bus),
                          "to_bus": int(to_bus), "parallel": int(parallel), "length_km": length_km})

    def is_grid_generated(self, plz: int):
        """
        Check if grid exists.

        Args:
            plz: Postal code to be checked

        Returns:
            bool: True if record exists, False otherwise
        """
        query = f"""
            SELECT 1
            FROM pylovo.postcode_result
            WHERE version_id = %(version_id)s AND postcode_result_plz = %(plz)s
            LIMIT 1;
        """

        self.cur.execute(query, {"version_id": VERSION_ID, "plz": plz})
        result = self.cur.fetchone()
        return result is not None