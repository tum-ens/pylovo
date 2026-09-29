"""Persistence of pandapower networks, analysis parameters and GeoDataFrame readers."""

import json
import re
import warnings
from typing import Any

import geopandas as gpd
import pandapower as pp
import pandas as pd
from psycopg2 import sql
from psycopg2.extras import execute_batch

from pylovo.config_loader import TARGET_EPSG, VERSION_ID
from pylovo.database.base_mixin import BaseMixin
from pylovo.database.migrations import pending_migrations
from pylovo.electrical_backend.pandapower.backend import empty_network

warnings.simplefilter(action="ignore", category=UserWarning)


class AnalysisMixin(BaseMixin):
    """Store generated networks and analysis results, and read result tables for plotting."""

    def ensure_grid_persistence_schema(self) -> None:
        """Require every schema migration before writing grid results."""
        pending = pending_migrations(self.cur)
        if pending is None:
            raise RuntimeError("PyLovo schema is not migrated; run pylovo-setup first (it keeps existing grids)")
        if pending:
            raise RuntimeError(
                f"PyLovo schema has pending migrations ({', '.join(pending)}); "
                "run pylovo-setup first (it keeps existing grids)"
            )

    def insert_plz_parameters(self, plz: int, trafo_string: str, load_count_string: str, bus_count_string: str):
        update_query = f"""INSERT INTO pylovo.plz_parameters (version_id, plz, trafo_num, load_count_per_trafo, bus_count_per_trafo)
                          VALUES (%s, %s, %s, %s,
                                  %s);"""  # TODO: check - should values be updated for same plz and version if analysis is started? And Add a column
        self.cur.execute(update_query, vars=(VERSION_ID, plz, trafo_string, load_count_string, bus_count_string), )
        self.logger.debug("basic parameter count finished")

    def insert_cable_length(self, plz: int, cable_length_string: str):
        """Store the cable length per cable type (JSON) of a PLZ in ``plz_parameters``."""
        update_query = """UPDATE pylovo.plz_parameters
                          SET cable_length = %(c)s
                          WHERE version_id = %(v)s
                            AND plz = %(p)s;"""
        self.cur.execute(update_query, {"v": VERSION_ID, "c": cable_length_string,
                                        "p": plz})  # TODO: change to cable_length_per_type, add cable_length_per_trafo
        self.logger.debug("cable count finished")

    def insert_trafo_parameters(self, plz: int, trafo_load_string: str, trafo_max_distance_string: str,
            trafo_avg_distance_string: str):
        """Store the per-transformer load and distance statistics (JSON) of a PLZ in ``plz_parameters``."""
        update_query = """UPDATE pylovo.plz_parameters
                          SET sim_peak_load_per_trafo = %(l)s,
                              max_distance_per_trafo  = %(m)s,
                              avg_distance_per_trafo  = %(a)s
                          WHERE version_id = %(v)s
                            AND plz = %(p)s;
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
        ampacity_max_feeder_voltage_drop_percent: float | None = None,
        selected_max_feeder_voltage_drop_percent: float | None = None,
        feeder_voltage_drop_limit_met: bool | None = None,
        ampacity_max_service_voltage_drop_percent: float | None = None,
        selected_max_service_voltage_drop_percent: float | None = None,
        service_voltage_drop_limit_met: bool | None = None,
        service_voltage_upgraded_count: int | None = None,
        long_service_connection_count: int | None = None,
        max_total_design_voltage_drop_percent: float | None = None,
        max_feeder_voltage_drop_pu: float | None = None,
        max_service_voltage_drop_pu: float | None = None,
        max_total_lv_voltage_drop_pu: float | None = None,
    ) -> None:
        """Store the pandapower JSON and the design results of one grid in ``grid_result``.

        The keyword arguments after ``power_flow_status`` are design results of the cable
        dimensioning and go to the ``grid_result`` columns of the same name (``None`` and NaN
        become NULL).

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            json_string: pandapower network as JSON (``pandapower.to_json``), or ``None``.
            transformer_description: Description of the installed transformer.
            power_flow_status: Outcome of the power-flow check.
        """
        insert_query = ("""UPDATE pylovo.grid_result
                           SET grid = %s,
                               transformer_description = %s,
                               power_flow_status = %s,
                               ampacity_max_feeder_voltage_drop_percent = %s,
                               selected_max_feeder_voltage_drop_percent = %s,
                               feeder_voltage_drop_limit_met = %s,
                               ampacity_max_service_voltage_drop_percent = %s,
                               selected_max_service_voltage_drop_percent = %s,
                               service_voltage_drop_limit_met = %s,
                               service_voltage_upgraded_count = %s,
                               long_service_connection_count = %s,
                               max_total_design_voltage_drop_percent = %s,
                               max_feeder_voltage_drop_pu = %s,
                               max_service_voltage_drop_pu = %s,
                               max_total_lv_voltage_drop_pu = %s
                           WHERE version_id = %s
                             AND plz = %s
                             AND kcid = %s
                             AND bcid = %s;""")
        self.cur.execute(
            insert_query,
            vars=(
                json_string,
                transformer_description,
                power_flow_status,
                self._normalize_sql_scalar(ampacity_max_feeder_voltage_drop_percent),
                self._normalize_sql_scalar(selected_max_feeder_voltage_drop_percent),
                self._normalize_sql_scalar(feeder_voltage_drop_limit_met),
                self._normalize_sql_scalar(ampacity_max_service_voltage_drop_percent),
                self._normalize_sql_scalar(selected_max_service_voltage_drop_percent),
                self._normalize_sql_scalar(service_voltage_drop_limit_met),
                self._normalize_sql_scalar(service_voltage_upgraded_count),
                self._normalize_sql_scalar(long_service_connection_count),
                self._normalize_sql_scalar(max_total_design_voltage_drop_percent),
                self._normalize_sql_scalar(max_feeder_voltage_drop_pu),
                self._normalize_sql_scalar(max_service_voltage_drop_pu),
                self._normalize_sql_scalar(max_total_lv_voltage_drop_pu),
                VERSION_ID,
                plz,
                kcid,
                bcid,
            ),
        )

    @staticmethod
    def _normalize_sql_scalar(value: Any) -> Any:
        """Convert numpy scalars to Python values and NaN/NA to ``None`` so psycopg2 can adapt them."""
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
        """Return ``row[column]`` normalised for SQL, or ``None`` if the column is missing."""
        if column not in row.index:
            return None
        return self._normalize_sql_scalar(row[column])

    def _normalize_geojson(self, value: Any) -> str | None:
        """Return a GeoJSON value (string, dict or list) as a JSON string, anything else as ``None``."""
        normalized = self._normalize_sql_scalar(value)
        if normalized is None:
            return None

        if isinstance(normalized, str):
            return normalized

        if isinstance(normalized, (dict, list)):
            return json.dumps(normalized)

        return None

    def _delete_pandapower_element_rows(self, grid_result_id: int) -> None:
        """Delete the ``pandapower_*`` element rows of one grid."""
        for table_name in ("pandapower_line", "pandapower_trafo", "pandapower_load", "pandapower_bus"):
            self.cur.execute(f"DELETE FROM pylovo.{table_name} WHERE grid_result_id = %(g)s", {"g": grid_result_id})

    def _insert_pandapower_bus_rows(self, grid_result_id: int, bus_df: pd.DataFrame | None) -> None:
        """Insert the rows of ``net.bus`` into ``pylovo.pandapower_bus``."""
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

        execute_batch(self.cur, insert_query, rows, page_size=500)

    def _insert_pandapower_line_rows(self, grid_result_id: int, line_df: pd.DataFrame | None) -> None:
        """Insert the rows of ``net.line`` into ``pylovo.pandapower_line``."""
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
                type,
                feeder_section_id,
                feeder_sizing_basis,
                ampacity_std_type,
                ampacity_parallel,
                service_sizing_basis,
                service_ampacity_voltage_drop_percent,
                service_selected_voltage_drop_percent,
                service_voltage_drop_limit_met,
                service_length_review,
                total_design_voltage_drop_percent
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
                %(type)s,
                %(feeder_section_id)s,
                %(feeder_sizing_basis)s,
                %(ampacity_std_type)s,
                %(ampacity_parallel)s,
                %(service_sizing_basis)s,
                %(service_ampacity_voltage_drop_percent)s,
                %(service_selected_voltage_drop_percent)s,
                %(service_voltage_drop_limit_met)s,
                %(service_length_review)s,
                %(total_design_voltage_drop_percent)s
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
                    "feeder_section_id": self._series_value(row, "feeder_section_id"),
                    "feeder_sizing_basis": self._series_value(row, "feeder_sizing_basis"),
                    "ampacity_std_type": self._series_value(row, "ampacity_std_type"),
                    "ampacity_parallel": self._series_value(row, "ampacity_parallel"),
                    "service_sizing_basis": self._series_value(row, "service_sizing_basis"),
                    "service_ampacity_voltage_drop_percent": self._series_value(
                        row, "service_ampacity_voltage_drop_percent"
                    ),
                    "service_selected_voltage_drop_percent": self._series_value(
                        row, "service_selected_voltage_drop_percent"
                    ),
                    "service_voltage_drop_limit_met": self._series_value(row, "service_voltage_drop_limit_met"),
                    "service_length_review": self._series_value(row, "service_length_review"),
                    "total_design_voltage_drop_percent": self._series_value(row, "total_design_voltage_drop_percent"),
                }
            )

        execute_batch(self.cur, insert_query, rows, page_size=500)

    def _insert_pandapower_trafo_rows(self, grid_result_id: int, trafo_df: pd.DataFrame | None) -> None:
        """Insert the rows of ``net.trafo`` into ``pylovo.pandapower_trafo``."""
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

        execute_batch(self.cur, insert_query, rows, page_size=500)

    def _insert_pandapower_load_rows(self, grid_result_id: int, load_df: pd.DataFrame | None) -> None:
        """Insert the rows of ``net.load`` into ``pylovo.pandapower_load``."""
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
                service_design_p_mw,
                operating_point_basis,
                category,
                load_units,
                consumer_vertex,
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
                %(service_design_p_mw)s,
                %(operating_point_basis)s,
                %(category)s,
                %(load_units)s,
                %(consumer_vertex)s,
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
                    "service_design_p_mw": self._series_value(row, "service_design_p_mw"),
                    "operating_point_basis": self._series_value(row, "operating_point_basis"),
                    "category": self._series_value(row, "category"),
                    "load_units": self._series_value(row, "load_units"),
                    "consumer_vertex": self._series_value(row, "consumer_vertex"),
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

        execute_batch(self.cur, insert_query, rows, page_size=500)

    def save_pandapower_net_with_sql(
        self,
        plz: int,
        kcid: int,
        bcid: int,
        net: pp.pandapowerNet,
        version_id: str | None = None,
    ) -> None:
        """Store the element tables of a pandapower network in the ``pandapower_*`` tables.

        Existing rows of the grid are replaced. Logs a warning and does nothing if ``net`` is
        ``None`` or the grid does not exist.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            net: pandapower network.
            version_id: Version of the grid; defaults to the configured ``VERSION_ID``.
        """
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
        """Return whether ``clustering_parameters`` already has a row for the grid in the active version."""
        query = """
            SELECT 1 
            FROM pylovo.clustering_parameters cp
            JOIN pylovo.grid_result gr ON cp.grid_result_id = gr.grid_result_id
            WHERE gr.plz = %s AND gr.kcid = %s AND gr.bcid = %s AND gr.version_id = %s
        """
        self.cur.execute(query, (plz, kcid, bcid, VERSION_ID))
        return bool(self.cur.fetchone())

    def read_per_trafo_dict(self, plz: int) -> tuple[list[dict], list[str], dict]:
        """Read the per-transformer statistics of a PLZ from ``plz_parameters`` for plotting.

        Returns:
            ``(data_list, data_labels, trafo_dict)``: the load count, bus count, simultaneous peak
            load, maximum and average transformer distance dicts (each keyed by transformer size,
            ascending), their axis labels, and the transformer count per size (descending).
        """
        result = [self._fetch_plz_parameters(
            plz,
            "load_count_per_trafo, bus_count_per_trafo, sim_peak_load_per_trafo, "
            "max_distance_per_trafo, avg_distance_per_trafo",
        )]

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
        """Read the pandapower network of a grid from ``grid_result.grid``.

        Args:
            plz: Postcode.
            kcid: K-means cluster ID.
            bcid: Building cluster ID.
            version_id: Version of the grid; defaults to the configured ``VERSION_ID``.

        Returns:
            The pandapower network.

        Raises:
            ValueError: If the grid does not exist.
        """
        effective_version_id = VERSION_ID if version_id is None else str(version_id)
        read_query = "SELECT grid FROM pylovo.grid_result WHERE version_id = %s AND plz = %s AND kcid = %s AND bcid = %s LIMIT 1"
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
        net = pp.from_json_string(grid_json_string, empty_dict_like_object=empty_network())

        return net

    def insert_clustering_parameters(self, params: dict) -> None:
        """Insert the calculated parameters of one grid into ``clustering_parameters`` and commit.

        Args:
            params: Column values plus ``version_id``, ``plz``, ``kcid`` and ``bcid`` of the grid.
        """
        insert_query = """INSERT INTO pylovo.clustering_parameters (
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

    @staticmethod
    def _geo_identifier(value: str) -> bool:
        """Accept a plain SQL identifier, optionally qualified once."""
        return bool(re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)?", value))

    def _geo_relation(self, value: str) -> tuple[str, str]:
        """Quote a PyLovo relation and its optional alias for plotting queries."""
        parts = value.split()
        if len(parts) not in (1, 2):
            raise ValueError("Expected a PyLovo table and optional alias")
        name = parts[0]
        if "." in name:
            schema, table = name.split(".", 1)
            if schema != "pylovo":
                raise ValueError("Plotting queries only accept PyLovo tables")
        else:
            table = name
        if not self._geo_identifier(table):
            raise ValueError("Invalid table name")
        qualified = sql.Identifier("pylovo", table).as_string(self.cur)
        if len(parts) == 1:
            return qualified, qualified
        alias = parts[1]
        if not self._geo_identifier(alias) or "." in alias:
            raise ValueError("Invalid table alias")
        quoted_alias = sql.Identifier(alias).as_string(self.cur)
        return f"{qualified} AS {quoted_alias}", quoted_alias

    def _equality_filters(self, filters: dict) -> tuple[str, dict]:
        """Return equality predicates with validated column names and bound values."""
        clauses = ""
        params = {}
        for index, (column, value) in enumerate(filters.items()):
            if not self._geo_identifier(column):
                raise ValueError(f"Invalid filter column: {column!r}")
            identifier = sql.Identifier(*column.split(".")).as_string(self.cur)
            clauses += f" AND {identifier} = %(f{index})s"
            params[f"f{index}"] = self._normalize_sql_scalar(value)
        return clauses, params

    def get_geo_df(self, table: str, **kwargs) -> gpd.GeoDataFrame:
        """Read a PyLovo geometry table with version and equality filters."""
        version = kwargs.pop("version_id", VERSION_ID)
        filters, params = self._equality_filters(kwargs)
        relation, _ = self._geo_relation(table)
        query = f"SELECT * FROM {relation} WHERE version_id = %(v)s " + filters
        params["v"] = version
        with self.sqla_engine.begin() as connection:
            return gpd.read_postgis(query, con=connection, params=params)

    def get_geo_df_join(
        self, select: list[str], from_table: str, join_table: str,
        on: tuple[str, str], **kwargs
    ) -> gpd.GeoDataFrame:
        """Read the supported plotting join with quoted relations and safe columns."""
        version = kwargs.pop("version_id", VERSION_ID)
        filters, params = self._equality_filters(kwargs)
        from_relation, _ = self._geo_relation(from_table)
        join_relation, join_prefix = self._geo_relation(join_table)
        if any(not self._geo_identifier(column) for column in on):
            raise ValueError("Invalid join column")
        on_sql = [sql.Identifier(*column.split(".")).as_string(self.cur) for column in on]
        geo_expression = (
            "ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(pl.geo::text), 4326), "
            f"{TARGET_EPSG}) AS geom"
        )
        for column in select:
            if not (
                column == "*"
                or re.fullmatch(r"[a-z][a-z0-9_]*(?:\.(?:[a-z][a-z0-9_]*|\*))?", column)
                or column == geo_expression
            ):
                raise ValueError(f"Unsupported plotting select expression: {column!r}")
        query = (
            f"SELECT {', '.join(select)} FROM {from_relation} "
            f"JOIN {join_relation} ON {on_sql[0]} = {on_sql[1]} "
            f"WHERE {join_prefix}.version_id = %(v)s " + filters
        )
        params["v"] = version
        with self.sqla_engine.begin() as connection:
            return gpd.read_postgis(query, con=connection, params=params)

    def read_trafo_dict(self, plz: int) -> dict:
        """Return the transformer count per transformer size of a PLZ from ``plz_parameters``."""
        return self._fetch_plz_parameters(plz, "trafo_num")[0]

    def read_cable_dict(self, plz: int) -> dict:
        """Return the cable length per cable type of a PLZ from ``plz_parameters``."""
        return self._fetch_plz_parameters(plz, "cable_length")[0]

    def _fetch_plz_parameters(self, plz: int, columns: str) -> tuple:
        """Return the given ``plz_parameters`` columns of a PLZ in the active version.

        Raises:
            LookupError: If the PLZ has not been analysed for ``VERSION_ID`` yet.
        """
        self.cur.execute(
            f"SELECT {columns} FROM pylovo.plz_parameters WHERE version_id = %(v)s AND plz = %(p)s;",
            {"v": VERSION_ID, "p": plz},
        )
        row = self.cur.fetchone()
        if row is None:
            raise LookupError(
                f"PLZ {plz} has no plz_parameters for version {VERSION_ID}; "
                f"run `pylovo-analyze --plz {plz}` first."
            )
        return row

    def is_grid_analyzed(self, plz: int):
        """Return whether the PLZ has a ``plz_parameters`` row in the active version."""
        query = """
            SELECT 1
            FROM pylovo.plz_parameters
            WHERE version_id = %(version_id)s AND plz = %(plz)s
            LIMIT 1;
        """

        self.cur.execute(query, {"version_id": VERSION_ID, "plz": plz})
        result = self.cur.fetchone()
        return result is not None
