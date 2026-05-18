import warnings
from abc import ABC

import pandapower as pp
from shapely.geometry import LineString

from pylovo.config_loader import *
from pylovo.database.base_mixin import BaseMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class GridMixin(BaseMixin, ABC):
    _VISUAL_PARALLEL_OFFSET_STEP = 1e-6
    _VISUAL_PARALLEL_OFFSET_RATIO_Y = 0.5

    def __init__(self):
        super().__init__()

    @staticmethod
    def _centered_lane_deviations(parallel: int) -> list[float]:
        center_offset = (parallel - 1) / 2
        return [float(index) - center_offset for index in range(parallel)]

    def _translate_visual_line_geometry(
        self,
        geom: list,
        deviation: float,
        anchor_start_point: bool = False,
        anchor_end_point: bool = False,
    ) -> LineString:
        offset_x = self._VISUAL_PARALLEL_OFFSET_STEP * deviation
        offset_y = self._VISUAL_PARALLEL_OFFSET_STEP * self._VISUAL_PARALLEL_OFFSET_RATIO_Y * deviation
        translated_coords = []
        for index, (x, y) in enumerate(geom):
            if anchor_start_point and index == 0:
                translated_coords.append((x, y))
                continue
            if anchor_end_point and index == len(geom) - 1:
                translated_coords.append((x, y))
                continue
            translated_coords.append((x + offset_x, y + offset_y))
        return LineString(translated_coords)

    def _build_visual_line_geometries(
        self,
        geom: list,
        parallel: int,
        anchor_start_point: bool = False,
        anchor_end_point: bool = False,
        lane_deviations: list[float] | None = None,
    ) -> list[LineString]:
        base_line = LineString(geom)
        if parallel <= 1 or len(geom) < 2 or base_line.length == 0:
            return [base_line]

        visual_lines = []
        effective_lane_deviations = lane_deviations or self._centered_lane_deviations(parallel)
        for deviation in effective_lane_deviations:
            if abs(deviation) < 1e-12:
                visual_lines.append(base_line)
                continue

            visual_lines.append(
                self._translate_visual_line_geometry(
                    geom,
                    deviation,
                    anchor_start_point=anchor_start_point,
                    anchor_end_point=anchor_end_point,
                )
            )

        return visual_lines

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

    def insert_lines(self, geom: list, plz: int, bcid: int, kcid: int, line_name: str, std_type: str, from_bus: int,
            to_bus: int, length_km: float, parallel: int = 1, anchor_start_point: bool = False,
            anchor_end_point: bool = False,
            lane_deviations: list[float] | None = None) -> None:
        """writes lines / cables that belong to a network into the database"""
        if not getattr(self, "_lines_result_parallel_schema_checked", False):
            self.cur.execute("ALTER TABLE pylovo.lines_result ADD COLUMN IF NOT EXISTS parallel integer;")
            self.cur.execute("DROP MATERIALIZED VIEW IF EXISTS pylovo.lines_result_with_grid CASCADE;")
            self.cur.execute(
                f"""
                CREATE MATERIALIZED VIEW pylovo.lines_result_with_grid AS (
                    SELECT
                        lr.lines_result_id as id,
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
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_lines_result_with_grid_uq_id
                ON pylovo.lines_result_with_grid (id);
                CREATE INDEX IF NOT EXISTS idx_lines_result_with_grid_geom
                ON pylovo.lines_result_with_grid USING gist (geom);
                """
            )
            self._lines_result_parallel_schema_checked = True

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
        for visual_geometry in self._build_visual_line_geometries(
            geom,
            max(1, int(parallel)),
            anchor_start_point=anchor_start_point,
            anchor_end_point=anchor_end_point,
            lane_deviations=lane_deviations,
        ):
            self.cur.execute(line_insertion_query,
                             {"v": VERSION_ID, "geom": visual_geometry.wkb_hex, "plz": int(plz), "bcid": int(bcid),
                                 "kcid": int(kcid), "line_name": line_name, "std_type": std_type,
                                 "from_bus": int(from_bus), "to_bus": int(to_bus), "parallel": int(parallel),
                                 "length_km": length_km})

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