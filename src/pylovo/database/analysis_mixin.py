import json
import warnings
from typing import Any

import geopandas as gpd
import pandapower as pp
import pandas as pd
from abc import ABC

from pylovo.config_loader import *
from pylovo.database.base_mixin import BaseMixin

warnings.simplefilter(action="ignore", category=UserWarning)


class AnalysisMixin(BaseMixin, ABC):
    def __init__(self):
        super().__init__()

    def insert_plz_parameters(self, plz: int, trafo_string: str, load_count_string: str, bus_count_string: str):
        update_query = f"""INSERT INTO pylovo.plz_parameters (version_id, plz, trafo_num, load_count_per_trafo, bus_count_per_trafo)
                          VALUES (%s, %s, %s, %s,
                                  %s);"""  # TODO: check - should values be updated for same plz and version if analysis is started? And Add a column
        self.cur.execute(update_query, vars=(VERSION_ID, plz, trafo_string, load_count_string, bus_count_string), )
        self.logger.debug("basic parameter count finished")

    def insert_cable_length(self, plz: int, cable_length_string: str):
        update_query = f"""UPDATE pylovo.plz_parameters
                          SET cable_length = %(c)s
                          WHERE version_id = %(v)s
                            AND plz = %(p)s;"""
        self.cur.execute(update_query, {"v": VERSION_ID, "c": cable_length_string,
                                        "p": plz})  # TODO: change to cable_length_per_type, add cable_length_per_trafo
        self.logger.debug("cable count finished")

    def insert_trafo_parameters(self, plz: int, trafo_load_string: str, trafo_max_distance_string: str,
            trafo_avg_distance_string: str):
        update_query = f"""UPDATE pylovo.plz_parameters
                          SET sim_peak_load_per_trafo = %(l)s,
                              max_distance_per_trafo  = %(m)s,
                              avg_distance_per_trafo  = %(a)s
                          WHERE version_id = %(v)s
                            AND plz = %(p)s; \
                       """
        self.cur.execute(update_query,
                         {"v": VERSION_ID, "p": plz, "l": trafo_load_string, "m": trafo_max_distance_string,
                          "a": trafo_avg_distance_string, }, )
        self.logger.debug("per trafo analysis finished")

    def save_pp_net_with_json(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        json_string: str | None,
        transformer_description: str,
        power_flow_status: str,
    ) -> None:
        insert_query = (f"""UPDATE pylovo.grid_result
                           SET grid = %s,
                               transformer_description = %s,
                               power_flow_status = %s
                           WHERE version_id = %s
                             AND plz = %s
                             AND kcid = %s
                             AND bcid = %s;""")
        self.cur.execute(
            insert_query,
            vars=(json_string, transformer_description, power_flow_status, VERSION_ID, plz, kcid, bcid),
        )

    @staticmethod
    def _normalize_sql_scalar(value: Any) -> Any:
        if value is None:
            return None

        if hasattr(value, "item"):
            value = value.item()

        try:
            if pd.isna(value):
                return None
        except TypeError:
            pass

        return value

    def _series_value(self, row: pd.Series, column: str) -> Any:
        if column not in row.index:
            return None
        return self._normalize_sql_scalar(row[column])

    def _normalize_geojson(self, value: Any) -> str | None:
        normalized = self._normalize_sql_scalar(value)
        if normalized is None:
            return None

        if isinstance(normalized, str):
            return normalized

        if isinstance(normalized, (dict, list)):
            return json.dumps(normalized)

        return None

    def get_grid_result_id(self, plz: int, kcid: int, bcid: int, version_id: str | None = None) -> int | None:
        effective_version_id = VERSION_ID if version_id is None else str(version_id)
        query = """
            SELECT grid_result_id
            FROM pylovo.grid_result
            WHERE version_id = %s
              AND plz = %s
              AND kcid = %s
              AND bcid = %s
            LIMIT 1
        """
        self.cur.execute(query, vars=(effective_version_id, plz, kcid, bcid))
        result = self.cur.fetchone()
        if result is None:
            return None
        return int(result[0])

    def _delete_pandapower_element_rows(self, grid_result_id: int) -> None:
        for table_name in ("pandapower_bus", "pandapower_line", "pandapower_trafo", "pandapower_load"):
            self.cur.execute(f"DELETE FROM pylovo.{table_name} WHERE grid_result_id = %(g)s", {"g": grid_result_id})

    def _insert_pandapower_bus_rows(self, grid_result_id: int, bus_df: pd.DataFrame | None) -> None:
        if bus_df is None or bus_df.empty:
            return

        insert_query = """
            INSERT INTO pylovo.pandapower_bus (
                grid_result_id,
                pp_index,
                name,
                vn_kv,
                type,
                zone,
                geo,
                in_service,
                min_vm_pu,
                max_vm_pu
            ) VALUES (
                %(grid_result_id)s,
                %(pp_index)s,
                %(name)s,
                %(vn_kv)s,
                %(type)s,
                %(zone)s,
                %(geo)s,
                %(in_service)s,
                %(min_vm_pu)s,
                %(max_vm_pu)s
            )
        """

        rows = []
        for pp_index, row in bus_df.iterrows():
            rows.append(
                {
                    "grid_result_id": grid_result_id,
                    "pp_index": self._normalize_sql_scalar(pp_index),
                    "name": self._series_value(row, "name"),
                    "vn_kv": self._series_value(row, "vn_kv"),
                    "type": self._series_value(row, "type"),
                    "zone": self._series_value(row, "zone"),
                    "geo": self._normalize_geojson(self._series_value(row, "geo")),
                    "in_service": self._series_value(row, "in_service"),
                    "min_vm_pu": self._series_value(row, "min_vm_pu"),
                    "max_vm_pu": self._series_value(row, "max_vm_pu"),
                }
            )

        self.cur.executemany(insert_query, rows)

    def _insert_pandapower_line_rows(self, grid_result_id: int, line_df: pd.DataFrame | None) -> None:
        if line_df is None or line_df.empty:
            return

        insert_query = """
            INSERT INTO pylovo.pandapower_line (
                grid_result_id,
                pp_index,
                name,
                std_type,
                from_bus,
                to_bus,
                length_km,
                parallel,
                geo,
                in_service,
                r_ohm_per_km,
                x_ohm_per_km,
                c_nf_per_km,
                g_us_per_km,
                max_i_ka,
                df,
                type
            ) VALUES (
                %(grid_result_id)s,
                %(pp_index)s,
                %(name)s,
                %(std_type)s,
                %(from_bus)s,
                %(to_bus)s,
                %(length_km)s,
                %(parallel)s,
                %(geo)s,
                %(in_service)s,
                %(r_ohm_per_km)s,
                %(x_ohm_per_km)s,
                %(c_nf_per_km)s,
                %(g_us_per_km)s,
                %(max_i_ka)s,
                %(df)s,
                %(type)s
            )
        """

        rows = []
        for pp_index, row in line_df.iterrows():
            rows.append(
                {
                    "grid_result_id": grid_result_id,
                    "pp_index": self._normalize_sql_scalar(pp_index),
                    "name": self._series_value(row, "name"),
                    "std_type": self._series_value(row, "std_type"),
                    "from_bus": self._series_value(row, "from_bus"),
                    "to_bus": self._series_value(row, "to_bus"),
                    "length_km": self._series_value(row, "length_km"),
                    "parallel": self._series_value(row, "parallel"),
                    "geo": self._normalize_geojson(self._series_value(row, "geo")),
                    "in_service": self._series_value(row, "in_service"),
                    "r_ohm_per_km": self._series_value(row, "r_ohm_per_km"),
                    "x_ohm_per_km": self._series_value(row, "x_ohm_per_km"),
                    "c_nf_per_km": self._series_value(row, "c_nf_per_km"),
                    "g_us_per_km": self._series_value(row, "g_us_per_km"),
                    "max_i_ka": self._series_value(row, "max_i_ka"),
                    "df": self._series_value(row, "df"),
                    "type": self._series_value(row, "type"),
                }
            )

        self.cur.executemany(insert_query, rows)

    def _insert_pandapower_trafo_rows(self, grid_result_id: int, trafo_df: pd.DataFrame | None) -> None:
        if trafo_df is None or trafo_df.empty:
            return

        insert_query = """
            INSERT INTO pylovo.pandapower_trafo (
                grid_result_id,
                pp_index,
                name,
                std_type,
                hv_bus,
                lv_bus,
                sn_mva,
                vn_hv_kv,
                vn_lv_kv,
                vkr_percent,
                vk_percent,
                pfe_kw,
                i0_percent,
                shift_degree,
                tap_side,
                tap_neutral,
                tap_min,
                tap_max,
                tap_step_percent,
                tap_pos,
                tap_phase_shifter,
                parallel,
                in_service
            ) VALUES (
                %(grid_result_id)s,
                %(pp_index)s,
                %(name)s,
                %(std_type)s,
                %(hv_bus)s,
                %(lv_bus)s,
                %(sn_mva)s,
                %(vn_hv_kv)s,
                %(vn_lv_kv)s,
                %(vkr_percent)s,
                %(vk_percent)s,
                %(pfe_kw)s,
                %(i0_percent)s,
                %(shift_degree)s,
                %(tap_side)s,
                %(tap_neutral)s,
                %(tap_min)s,
                %(tap_max)s,
                %(tap_step_percent)s,
                %(tap_pos)s,
                %(tap_phase_shifter)s,
                %(parallel)s,
                %(in_service)s
            )
        """

        rows = []
        for pp_index, row in trafo_df.iterrows():
            rows.append(
                {
                    "grid_result_id": grid_result_id,
                    "pp_index": self._normalize_sql_scalar(pp_index),
                    "name": self._series_value(row, "name"),
                    "std_type": self._series_value(row, "std_type"),
                    "hv_bus": self._series_value(row, "hv_bus"),
                    "lv_bus": self._series_value(row, "lv_bus"),
                    "sn_mva": self._series_value(row, "sn_mva"),
                    "vn_hv_kv": self._series_value(row, "vn_hv_kv"),
                    "vn_lv_kv": self._series_value(row, "vn_lv_kv"),
                    "vkr_percent": self._series_value(row, "vkr_percent"),
                    "vk_percent": self._series_value(row, "vk_percent"),
                    "pfe_kw": self._series_value(row, "pfe_kw"),
                    "i0_percent": self._series_value(row, "i0_percent"),
                    "shift_degree": self._series_value(row, "shift_degree"),
                    "tap_side": self._series_value(row, "tap_side"),
                    "tap_neutral": self._series_value(row, "tap_neutral"),
                    "tap_min": self._series_value(row, "tap_min"),
                    "tap_max": self._series_value(row, "tap_max"),
                    "tap_step_percent": self._series_value(row, "tap_step_percent"),
                    "tap_pos": self._series_value(row, "tap_pos"),
                    "tap_phase_shifter": self._series_value(row, "tap_phase_shifter"),
                    "parallel": self._series_value(row, "parallel"),
                    "in_service": self._series_value(row, "in_service"),
                }
            )

        self.cur.executemany(insert_query, rows)

    def _insert_pandapower_load_rows(self, grid_result_id: int, load_df: pd.DataFrame | None) -> None:
        if load_df is None or load_df.empty:
            return

        insert_query = """
            INSERT INTO pylovo.pandapower_load (
                grid_result_id,
                pp_index,
                name,
                bus,
                p_mw,
                q_mvar,
                const_z_percent,
                const_i_percent,
                sn_mva,
                scaling,
                in_service,
                type,
                controllable,
                max_p_mw,
                min_p_mw,
                max_q_mvar,
                min_q_mvar
            ) VALUES (
                %(grid_result_id)s,
                %(pp_index)s,
                %(name)s,
                %(bus)s,
                %(p_mw)s,
                %(q_mvar)s,
                %(const_z_percent)s,
                %(const_i_percent)s,
                %(sn_mva)s,
                %(scaling)s,
                %(in_service)s,
                %(type)s,
                %(controllable)s,
                %(max_p_mw)s,
                %(min_p_mw)s,
                %(max_q_mvar)s,
                %(min_q_mvar)s
            )
        """

        rows = []
        for pp_index, row in load_df.iterrows():
            rows.append(
                {
                    "grid_result_id": grid_result_id,
                    "pp_index": self._normalize_sql_scalar(pp_index),
                    "name": self._series_value(row, "name"),
                    "bus": self._series_value(row, "bus"),
                    "p_mw": self._series_value(row, "p_mw"),
                    "q_mvar": self._series_value(row, "q_mvar"),
                    "const_z_percent": self._series_value(row, "const_z_percent"),
                    "const_i_percent": self._series_value(row, "const_i_percent"),
                    "sn_mva": self._series_value(row, "sn_mva"),
                    "scaling": self._series_value(row, "scaling"),
                    "in_service": self._series_value(row, "in_service"),
                    "type": self._series_value(row, "type"),
                    "controllable": self._series_value(row, "controllable"),
                    "max_p_mw": self._series_value(row, "max_p_mw"),
                    "min_p_mw": self._series_value(row, "min_p_mw"),
                    "max_q_mvar": self._series_value(row, "max_q_mvar"),
                    "min_q_mvar": self._series_value(row, "min_q_mvar"),
                }
            )

        self.cur.executemany(insert_query, rows)

    def save_pandapower_net_with_sql(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        net: pp.pandapowerNet,
        version_id: str | None = None,
    ) -> None:
        if net is None:
            self.logger.warning(
                "Skipping pandapower SQL persistence because no pandapower network instance was provided."
            )
            return

        grid_result_id = self.get_grid_result_id(plz=plz, kcid=kcid, bcid=bcid, version_id=version_id)
        if grid_result_id is None:
            self.logger.warning(
                f"Skipping pandapower SQL persistence because grid_result_id was not found for "
                f"plz={plz}, kcid={kcid}, bcid={bcid}."
            )
            return

        self._delete_pandapower_element_rows(grid_result_id)
        self._insert_pandapower_bus_rows(grid_result_id, getattr(net, "bus", None))
        self._insert_pandapower_line_rows(grid_result_id, getattr(net, "line", None))
        self._insert_pandapower_trafo_rows(grid_result_id, getattr(net, "trafo", None))
        self._insert_pandapower_load_rows(grid_result_id, getattr(net, "load", None))

    def has_clustering_parameters(self, plz: int, kcid: int, bcid: int) -> bool:
        """
        Check if parameters already exist for a specific grid.
        
        Args:
            plz: Postal code
            kcid: Grid cluster ID
            bcid: Building cluster ID
            
        Returns:
            bool: True if parameters exist, False otherwise
        """
        query = f"""
            SELECT 1 
            FROM pylovo.clustering_parameters cp
            JOIN pylovo.grid_result gr ON cp.grid_result_id = gr.grid_result_id
            WHERE gr.plz = %s AND gr.kcid = %s AND gr.bcid = %s AND gr.version_id = %s
        """
        self.cur.execute(query, (plz, kcid, bcid, VERSION_ID))
        return bool(self.cur.fetchone())

    def count_clustering_parameters(self, plz: int) -> int:
        """
        :param plz:
        :return:
        """
        query = f"""SELECT COUNT(cp.grid_result_id)
                                     FROM pylovo.clustering_parameters cp
                                                        JOIN pylovo.grid_result gr ON gr.grid_result_id = cp.grid_result_id
                   WHERE version_id = %(v)s
                     AND plz = %(p)s"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz})
        return int(self.cur.fetchone()[0])

    def read_per_trafo_dict(self, plz: int) -> tuple[list[dict], list[str], dict]:
        read_query = """SELECT load_count_per_trafo,
                               bus_count_per_trafo,
                               sim_peak_load_per_trafo,
                               max_distance_per_trafo,
                               avg_distance_per_trafo
                                                FROM pylovo.plz_parameters
                        WHERE version_id = %(v)s
                          AND plz = %(p)s;"""
        self.cur.execute(read_query, {"v": VERSION_ID, "p": plz})
        result = self.cur.fetchall()

        # Sort all parameters according to transformer size
        load_dict = dict(sorted(result[0][0].items(), key=lambda x: int(x[0])))
        bus_dict = dict(sorted(result[0][1].items(), key=lambda x: int(x[0])))
        peak_dict = dict(sorted(result[0][2].items(), key=lambda x: int(x[0])))
        max_dict = dict(sorted(result[0][3].items(), key=lambda x: int(x[0])))
        avg_dict = dict(sorted(result[0][4].items(), key=lambda x: int(x[0])))

        trafo_dict = dict(sorted(self.read_trafo_dict(plz).items(), key=lambda x: int(x[0]), reverse=True))
        # Create list with all parameter dicts
        data_list = [load_dict, bus_dict, peak_dict, max_dict, avg_dict]
        data_labels = ['Load Number [-]', 'Bus Number [-]', 'Simultaneous peak load [kW]', 'Max. Trafo-Distance [m]',
                       'Avg. Trafo-Distance [m]']

        return data_list, data_labels, trafo_dict

    def read_net_db(self, plz: int, kcid: int, bcid: int, version_id: str | None = None) -> pp.pandapowerNet:
        """
        Reads a pandapower network from the database for the specified grid.

        Args:
            plz: Postal code ID
            kcid: Kmeans cluster ID
            bcid: Building cluster ID

        Returns:
            A pandapower network object

        Raises:
            ValueError: If the requested grid does not exist in the database
        """
        effective_version_id = VERSION_ID if version_id is None else str(version_id)
        read_query = f"SELECT grid FROM pylovo.grid_result WHERE version_id = %s AND plz = %s AND kcid = %s AND bcid = %s LIMIT 1"
        self.cur.execute(read_query, vars=(effective_version_id, plz, kcid, bcid))

        result = self.cur.fetchall()
        if not result:
            self.logger.error(
                f"Grid not found for plz={plz}, kcid={kcid}, bcid={bcid}, version_id={effective_version_id}"
            )
            raise ValueError(f"Grid not found for plz={plz}, kcid={kcid}, bcid={bcid}")

        grid_tuple = result[0]
        grid_dict = grid_tuple[0]
        grid_json_string = json.dumps(grid_dict)
        net = pp.from_json_string(grid_json_string)

        return net

    def insert_clustering_parameters(self, params: dict) -> None:
        """Insert calculated grid parameters into clustering_parameters table."""

        insert_query = f"""INSERT INTO pylovo.clustering_parameters (
                   grid_result_id,
                   no_connection_buses,
                   no_branches,
                   no_house_connections,
                   no_house_connections_per_branch,
                   no_households,
                   no_household_equ,
                   no_households_per_branch,
                   max_no_of_households_of_a_branch,
                   house_distance_km,
                   transformer_mva,
                   osm_trafo,
                   max_trafo_dis,
                   avg_trafo_dis,
                   cable_length_km,
                   cable_len_per_house,
                   max_power_mw,
                   simultaneous_peak_load_mw,
                   resistance,
                   reactance,
                   ratio,
                   vsw_per_branch,
                   max_vsw_of_a_branch
                  )
                  VALUES (
                  (SELECT grid_result_id FROM pylovo.grid_result WHERE version_id = %(version_id)s AND plz = %(plz)s AND bcid = %(bcid)s AND kcid = %(kcid)s),
                  %(no_connection_buses)s,
                  %(no_branches)s,
                  %(no_house_connections)s,
                  %(no_house_connections_per_branch)s,
                  %(no_households)s,
                  %(no_household_equ)s,
                  %(no_households_per_branch)s,
                  %(max_no_of_households_of_a_branch)s,
                  %(house_distance_km)s,
                  %(transformer_mva)s,
                  %(osm_trafo)s,
                  %(max_trafo_dis)s,
                  %(avg_trafo_dis)s,
                  %(cable_length_km)s,
                  %(cable_len_per_house)s,
                  %(max_power_mw)s,
                  %(simultaneous_peak_load_mw)s,
                  %(resistance)s,
                  %(reactance)s,
                  %(ratio)s,
                  %(vsw_per_branch)s,
                  %(max_vsw_of_a_branch)s);"""

        self.cur.execute(insert_query, params)
        self.conn.commit()

    def get_geo_df(self, table: str, **kwargs, ) -> gpd.GeoDataFrame:
        """
        Args:
            **kwargs: equality filters matching with the table column names
        Returns: A geodataframe with all building information
        :param table: table name
        """
        if kwargs:
            filters = " AND " + " AND ".join(
                [f"{key} = {value}" for key, value in kwargs.items() if key != 'version_id'])
        else:
            filters = ""
        table_name = table if "." in table or table.startswith("(") else f"pylovo.{table}"
        query = (f"""SELECT * FROM {table_name}
                        WHERE version_id = %(v)s """ + filters)
        version = VERSION_ID
        if 'version_id' in kwargs:
            version = kwargs.get('version_id')

        params = {"v": version}
        with self.sqla_engine.begin() as connection:
            gdf = gpd.read_postgis(query, con=connection, params=params)

        return gdf

    def get_geo_df_join(self, select: list[str], from_table: str, join_table: str, on: tuple[str, str],
            **kwargs, ) -> gpd.GeoDataFrame:
        """
        Args:
            **kwargs: equality filters matching with the table column names
        Returns: A geodataframe with all building information
        :param select: list of column names
        :param from_table: table name
        :param join_table: table name
        :param on: join on on[0] = on[1]
        """
        if kwargs:
            filters = " AND " + " AND ".join(
                [f"{key} = {value}" for key, value in kwargs.items() if key != 'version_id'])
        else:
            filters = ""

        column_names = ", ".join(select)

        from_parts = from_table.split(" ", 1)
        from_name = from_parts[0]
        if "." not in from_name and not from_name.startswith("("):
            from_table = f"pylovo.{from_name}" + (f" {from_parts[1]}" if len(from_parts) == 2 else "")

        join_parts = join_table.split(" ", 1)
        join_name = join_parts[0]
        if "." not in join_name and not join_name.startswith("("):
            join_table = f"pylovo.{join_name}" + (f" {join_parts[1]}" if len(join_parts) == 2 else "")

        jt_prefix = join_table
        parts = join_table.split(" ")
        if len(parts) == 2:
            jt_prefix = parts[1]

        query = (f"""SELECT {column_names}
                        FROM {from_table}
                        JOIN {join_table}
                          ON {on[0]} = {on[1]}
                        WHERE {jt_prefix}.version_id = %(v)s """ + filters)
        version = VERSION_ID
        if 'version_id' in kwargs:
            version = kwargs.get('version_id')

        params = {"v": version}
        with self.sqla_engine.begin() as connection:
            gdf = gpd.read_postgis(query, con=connection, params=params)

        return gdf


    def read_trafo_dict(self, plz: int) -> dict:
        read_query = """SELECT trafo_num
                                                FROM pylovo.plz_parameters
                        WHERE version_id = %(v)s
                          AND plz = %(p)s;"""
        self.cur.execute(read_query, {"v": VERSION_ID, "p": plz})
        trafo_num_dict = self.cur.fetchall()[0][0]

        return trafo_num_dict

    def read_cable_dict(self, plz: int) -> dict:
        read_query = """SELECT cable_length
                                                FROM pylovo.plz_parameters
                        WHERE version_id = %(v)s
                          AND plz = %(p)s;"""
        self.cur.execute(read_query, {"v": VERSION_ID, "p": plz})
        cable_length = self.cur.fetchall()[0][0]

        return cable_length

    def is_grid_analyzed(self, plz: int):
        """
        Check if grid has been analyzed.

        Args:
            plz: Postal code to be checked

        Returns:
            bool: True if record exists, False otherwise
        """
        query = f"""
            SELECT 1
            FROM pylovo.plz_parameters
            WHERE version_id = %(version_id)s AND plz = %(plz)s
            LIMIT 1;
        """

        self.cur.execute(query, {"version_id": VERSION_ID, "plz": plz})
        result = self.cur.fetchone()
        return result is not None

    def get_grids_from_plz(self, plz : int) -> pd.DataFrame:
        grids_query = f"""SELECT * FROM pylovo.grid_result
                        WHERE plz = %(p)s"""
        params = {"p": plz}
        grids_df = pd.read_sql_query(grids_query, con=self.conn, params=params)
        self.logger.debug(f"{len(grids_df)} grid data fetched.")

        return grids_df
