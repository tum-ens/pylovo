"""Read-only map layers and key figures of the generated LV grids, for plots and notebooks.

:meth:`LvLayerMixin.read_lv_layers` returns the layers of one postcode area (or of one grid) as GeoDataFrames in
``TARGET_EPSG``, the input of :mod:`pylovo.plotting.lv_grid` (like ``read_mv_layers`` for the MV maps).
:meth:`LvLayerMixin.read_grid_summary` returns one row of key figures per grid for many postcode areas.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import pandas as pd

from pylovo.config_loader import TARGET_EPSG, VERSION_ID

if TYPE_CHECKING:
    import geopandas as gpd

#: The layers of :meth:`LvLayerMixin.read_lv_layers`, in this order.
LV_LAYERS = ("postcode", "grids", "buildings", "lines", "streets", "connections", "split_points", "mv_customers",
             "exclusions")

#: ``ways_result.clazz`` of the connection lines from the buildings to the streets.
CONNECTION_LINE_CLAZZ = 110

_GRID_COLUMNS = (
    "gr.grid_result_id, gr.version_id, gr.plz, gr.kcid, gr.bcid, gr.transformer_rated_power, "
    "gr.transformer_equipment_name, gr.model_status, gr.power_flow_status, "
    "gr.ampacity_max_feeder_voltage_drop_percent, gr.selected_max_feeder_voltage_drop_percent, "
    "gr.feeder_voltage_drop_limit_met, gr.ampacity_max_service_voltage_drop_percent, "
    "gr.selected_max_service_voltage_drop_percent, gr.service_voltage_drop_limit_met, "
    "gr.service_voltage_upgraded_count, gr.long_service_connection_count, gr.max_total_design_voltage_drop_percent, "
    "gr.max_feeder_voltage_drop_pu, gr.max_service_voltage_drop_pu, gr.max_total_lv_voltage_drop_pu"
)


class LvLayerMixin:
    """Map layers and key figures of the LV grids of a version (read-only queries)."""

    def _read_lv_geo(self, query: str, params: dict) -> "gpd.GeoDataFrame":
        import geopandas as gpd

        with self.sqla_engine.begin() as connection:
            return gpd.read_postgis(query, con=connection, params=params, geom_col="geom")

    def _read_lv_frame(self, query: str, params: dict) -> pd.DataFrame:
        self.cur.execute(query, params)
        return pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])

    def read_lv_layers(self, plz: int, version_id: str | None = None, kcid: int | None = None,
                       bcid: int | None = None, layers: Sequence[str] | None = None) -> dict[str, Any]:
        """The map layers of the grids of a postcode area, or of one grid with ``kcid`` and ``bcid``.

        Args:
            plz: Postcode area.
            version_id: Version of the grids; defaults to the configured ``VERSION_ID``.
            kcid: With ``bcid``: only this grid in ``grids``, ``buildings``, ``lines`` and ``split_points``
                (the other layers stay those of the postcode area).
            bcid: See ``kcid``.
            layers: Only these layers (default: all of :data:`LV_LAYERS`).

        Returns:
            ``{name: GeoDataFrame}`` in ``TARGET_EPSG``:

            * ``postcode``: the postcode polygon with its settlement type and the two metrics behind it,
            * ``grids``: one row per grid at the position of its MV/LV station: rating, power-flow status, design
              voltage drops, the station source (``osm``, ``lod2``), the LV bus of the transformer, and the number
              of buildings and households,
            * ``buildings``: the supplied buildings (footprints) with ``kcid``/``bcid``, use, households and the
              residential and non-residential peak loads,
            * ``lines``: the cables (``pandapower_line``) with type, parallel cables, feeder section, sizing basis
              and ``is_service`` (house connection),
            * ``streets``, ``connections``: the street graph of the postcode area and the connection lines from the
              buildings to it (``ways_result``),
            * ``split_points``: branch points of the feeders (cable distribution cabinets),
            * ``mv_customers``: buildings supplied directly from the MV grid,
            * ``exclusions``: a DataFrame of the buildings left out of the grids (``objectid``, ``reason``).

        Raises:
            ValueError: An unknown layer name, or only one of ``kcid`` and ``bcid``.
        """
        if (kcid is None) != (bcid is None):
            raise ValueError("pass both kcid and bcid, or neither")
        wanted = list(LV_LAYERS) if layers is None else list(layers)
        unknown = [name for name in wanted if name not in LV_LAYERS]
        if unknown:
            raise ValueError(f"unknown LV layer {', '.join(unknown)} (one of {', '.join(LV_LAYERS)})")
        params = {"v": VERSION_ID if version_id is None else str(version_id), "plz": int(plz),
                  "k": None if kcid is None else int(kcid), "b": None if bcid is None else int(bcid),
                  "epsg": int(TARGET_EPSG), "conn": CONNECTION_LINE_CLAZZ}
        grid = "" if kcid is None else " AND gr.kcid = %(k)s AND gr.bcid = %(b)s"
        queries = {
            "postcode": """SELECT version_id, postcode_result_plz AS plz, settlement_type, house_distance,
                                  avg_households_per_building, geom
                           FROM pylovo.postcode_result WHERE version_id = %(v)s AND postcode_result_plz = %(plz)s""",
            "grids": f"""SELECT {_GRID_COLUMNS}, tp.osm_id, tp.osm, tp.lod2, pt.lv_bus, pt.sn_mva, pt.parallel,
                                COALESCE(b.buildings, 0) AS buildings, COALESCE(b.households, 0) AS households,
                                b.peak_load_kw, tp.geom
                         FROM pylovo.grid_result gr
                         LEFT JOIN LATERAL (SELECT * FROM pylovo.transformer_positions t
                                            WHERE t.grid_result_id = gr.grid_result_id LIMIT 1) tp ON TRUE
                         LEFT JOIN LATERAL (SELECT lv_bus, sn_mva, parallel FROM pylovo.pandapower_trafo t
                                            WHERE t.grid_result_id = gr.grid_result_id
                                            ORDER BY t.pp_index LIMIT 1) pt ON TRUE
                         LEFT JOIN (SELECT grid_result_id, count(*) AS buildings,
                                           sum(COALESCE(households, 0)) AS households,
                                           sum(peak_load_in_kw) AS peak_load_kw
                                    FROM pylovo.buildings_result WHERE version_id = %(v)s
                                    GROUP BY grid_result_id) b ON b.grid_result_id = gr.grid_result_id
                         WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s{grid}
                         ORDER BY gr.kcid, gr.bcid""",
            "buildings": f"""SELECT br.objectid, gr.grid_result_id, gr.kcid, gr.bcid, br.building_use, br.building_type,
                                    br.nonresidential_use, br.households, br.floor_area, br.floor_number, br.height,
                                    br.residential_peak_load_in_kw, br.nonresidential_peak_load_in_kw,
                                    br.peak_load_in_kw, br.nonresidential_mv_direct, br.vertice_id,
                                    br.connection_point, br.geom
                             FROM pylovo.buildings_result br
                             JOIN pylovo.grid_result gr ON gr.grid_result_id = br.grid_result_id
                             WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s{grid}
                             ORDER BY gr.kcid, gr.bcid, br.objectid""",
            "lines": f"""SELECT pl.grid_result_id, gr.kcid, gr.bcid, pl.pp_index, pl.name, pl.std_type, pl.from_bus,
                                pl.to_bus, pl.length_km, pl.parallel, pl.in_service, pl.max_i_ka, pl.feeder_section_id,
                                pl.feeder_sizing_basis, pl.ampacity_std_type, pl.ampacity_parallel,
                                pl.service_sizing_basis, pl.total_design_voltage_drop_percent,
                                pl.service_sizing_basis IS NOT NULL AS is_service,
                                ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(pl.geo::text), 4326), %(epsg)s) AS geom
                         FROM pylovo.pandapower_line pl
                         JOIN pylovo.grid_result gr ON gr.grid_result_id = pl.grid_result_id
                         WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s AND pl.geo IS NOT NULL{grid}
                         ORDER BY gr.kcid, gr.bcid, pl.pp_index""",
            "streets": """SELECT way_id, clazz, geom FROM pylovo.ways_result
                          WHERE version_id = %(v)s AND plz = %(plz)s AND clazz <> %(conn)s ORDER BY way_id""",
            "connections": """SELECT way_id, source, target, geom FROM pylovo.ways_result
                              WHERE version_id = %(v)s AND plz = %(plz)s AND clazz = %(conn)s ORDER BY way_id""",
            "split_points": f"""SELECT sp.grid_result_id, gr.kcid, gr.bcid, sp.split_bus, sp.outgoing_count,
                                       sp.split_type, sp.geom
                                FROM pylovo.split_points sp
                                JOIN pylovo.grid_result gr ON gr.grid_result_id = sp.grid_result_id
                                WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s{grid}
                                ORDER BY gr.kcid, gr.bcid, sp.split_point_id""",
            "mv_customers": """SELECT objectid, building_use, nonresidential_use, households,
                                      residential_peak_load_in_kw, nonresidential_peak_load_in_kw, geom
                               FROM pylovo.mv_customers WHERE version_id = %(v)s AND plz = %(plz)s
                               ORDER BY objectid""",
        }
        result: dict[str, Any] = {}
        for name in wanted:
            if name == "exclusions":
                result[name] = self._read_lv_frame(
                    "SELECT objectid, reason FROM pylovo.building_exclusion "
                    "WHERE version_id = %(v)s AND plz = %(plz)s ORDER BY objectid", params)
            else:
                result[name] = self._read_lv_geo(queries[name], params)
        return result

    def read_grid_summary(self, plz: int | Iterable[int] | None = None,
                          version_id: str | None = None) -> pd.DataFrame:
        """One row of key figures per grid: rating, status, size and installed cable length.

        Args:
            plz: One postcode area, several, or None for all grids of the version.
            version_id: Version; defaults to the configured ``VERSION_ID``.

        Returns:
            Columns ``plz``, ``kcid``, ``bcid``, ``settlement_type`` of the postcode area,
            ``transformer_rated_power`` (kVA), ``power_flow_status``, the design voltage drops of
            ``grid_result``, ``buildings``, ``households``, ``peak_load_kw`` (sum of the building peaks),
            ``feeder_km`` and ``service_km`` (installed length, parallel cables counted).
        """
        params: dict[str, Any] = {"v": VERSION_ID if version_id is None else str(version_id)}
        where = ""
        if plz is not None:
            params["plz"] = [int(plz)] if isinstance(plz, (int, str)) else [int(p) for p in plz]
            where = " AND gr.plz = ANY(%(plz)s)"
        query = f"""
            SELECT gr.plz, gr.kcid, gr.bcid, pr.settlement_type, gr.transformer_rated_power, gr.power_flow_status,
                   gr.ampacity_max_feeder_voltage_drop_percent, gr.selected_max_feeder_voltage_drop_percent,
                   gr.feeder_voltage_drop_limit_met, gr.max_total_lv_voltage_drop_pu,
                   COALESCE(b.buildings, 0) AS buildings, COALESCE(b.households, 0) AS households,
                   b.peak_load_kw, COALESCE(l.feeder_km, 0) AS feeder_km, COALESCE(l.service_km, 0) AS service_km
            FROM pylovo.grid_result gr
            LEFT JOIN pylovo.postcode_result pr
                   ON pr.version_id = gr.version_id AND pr.postcode_result_plz = gr.plz
            LEFT JOIN (SELECT grid_result_id, count(*) AS buildings, sum(COALESCE(households, 0)) AS households,
                              sum(peak_load_in_kw) AS peak_load_kw
                       FROM pylovo.buildings_result WHERE version_id = %(v)s GROUP BY grid_result_id) b
                   ON b.grid_result_id = gr.grid_result_id
            LEFT JOIN (SELECT pl.grid_result_id,
                              sum(pl.length_km * COALESCE(pl.parallel, 1))
                                  FILTER (WHERE pl.service_sizing_basis IS NULL) AS feeder_km,
                              sum(pl.length_km * COALESCE(pl.parallel, 1))
                                  FILTER (WHERE pl.service_sizing_basis IS NOT NULL) AS service_km
                       FROM pylovo.pandapower_line pl
                       JOIN pylovo.grid_result g2 ON g2.grid_result_id = pl.grid_result_id
                       WHERE g2.version_id = %(v)s AND COALESCE(pl.in_service, TRUE)
                       GROUP BY pl.grid_result_id) l
                   ON l.grid_result_id = gr.grid_result_id
            WHERE gr.version_id = %(v)s{where}
            ORDER BY gr.plz, gr.kcid, gr.bcid"""
        frame = self._read_lv_frame(query, params)
        for column in ("peak_load_kw", "feeder_km", "service_km", "max_total_lv_voltage_drop_pu",
                       "ampacity_max_feeder_voltage_drop_percent", "selected_max_feeder_voltage_drop_percent"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    def read_postcode_metrics(self, version_id: str | None = None) -> pd.DataFrame:
        """Settlement type and its two density metrics of every postcode area of a version.

        Returns:
            Columns ``plz``, ``settlement_type``, ``house_distance`` (m) and ``avg_households_per_building``.
        """
        frame = self._read_lv_frame(
            "SELECT postcode_result_plz AS plz, settlement_type, house_distance, avg_households_per_building "
            "FROM pylovo.postcode_result WHERE version_id = %(v)s ORDER BY 1",
            {"v": VERSION_ID if version_id is None else str(version_id)})
        for column in ("house_distance", "avg_households_per_building"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    def read_voltage_chain(self, plz: int, kcid: int, bcid: int, variant_id: str | None = None,
                           version_id: str | None = None) -> dict | None:
        """How one LV grid is connected upwards: MV/LV station, MV ring, MV supply area, HV/MV substation, 110 kV bus.

        Args:
            plz: Postcode area of the grid.
            kcid: K-means cluster id of the grid.
            bcid: Building cluster id of the grid.
            variant_id: MV variant; default the only done variant of the version (MV stage, ``pylovo-mv``).
            version_id: LV version; defaults to the configured ``VERSION_ID``.

        Returns:
            One dict with the LV grid (``kcid``, ``bcid``, ``transformer_rated_power``), its MV node
            (``mv_node_id``, ``ring_id``, ``feeder_id``, ``ring_position``, ``vm_load_pu``, ``mv_lv_tap_pos``,
            ``lv_busbar_vm_pu``), the MV grid (``mv_grid_id``, ``vn_kv``, ``n_stations``, ``n_rings``, ``circuit_km``,
            ``peak_load_mw``, ``hv_mv_transformers``, ``hv_mv_transformer_mva``) and the HV/MV substation
            (``substation_id``, ``substation_name``, ``egon_bus_id``: the bus of the eTraGo HV grid), or None if
            the version has no MV results for the grid.

        Raises:
            ValueError: No ``variant_id`` and not exactly one done MV variant.
        """
        version = VERSION_ID if version_id is None else str(version_id)
        if variant_id is None:
            self.cur.execute("SELECT mv_variant_id FROM pylovo.mv_variant WHERE version_id = %s AND status = 'done' "
                             "ORDER BY mv_variant_id", (version,))
            done = [row[0] for row in self.cur.fetchall()]
            if len(done) != 1:
                raise ValueError(f"version {version} has {len(done)} done MV variants; pass variant_id")
            variant_id = done[0]
        frame = self._read_lv_frame(
            """SELECT gr.version_id, n.mv_variant_id, gr.plz, gr.kcid, gr.bcid, gr.transformer_rated_power,
                      n.mv_node_id, n.ring_id, n.feeder_id, n.ring_position, n.vm_load_pu, n.mv_lv_tap_pos,
                      n.lv_busbar_vm_pu, g.mv_grid_id, g.vn_kv, g.n_stations, g.n_customers, g.n_rings, g.circuit_km,
                      g.cable_km, g.overhead_km, g.area_km2, g.peak_load_mw, g.hv_mv_transformers,
                      g.hv_mv_transformer_mva, s.substation_id, s.name AS substation_name, s.egon_bus_id
               FROM pylovo.grid_result gr
               JOIN pylovo.mv_node n ON n.grid_result_id = gr.grid_result_id AND n.version_id = gr.version_id
                                    AND n.mv_variant_id = %(var)s AND n.kind = 'station'
               JOIN pylovo.mv_grid g ON g.version_id = n.version_id AND g.mv_variant_id = n.mv_variant_id
                                    AND g.mv_grid_id = n.mv_grid_id
               JOIN pylovo.mv_substation s ON s.version_id = g.version_id AND s.mv_variant_id = g.mv_variant_id
                                          AND s.substation_id = g.substation_id
               WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s AND gr.kcid = %(k)s AND gr.bcid = %(b)s""",
            {"v": version, "var": str(variant_id), "plz": int(plz), "k": int(kcid), "b": int(bcid)})
        if frame.empty:
            return None
        return {key: (None if pd.isna(value) else value.item() if hasattr(value, "item") else value)
                for key, value in frame.iloc[0].items()}

    def read_generation_parameters(self, version_id: str | None = None) -> dict:
        """The generation parameter snapshot of a version (``pylovo.version.generation_parameters``), ``{}`` if none.

        The values the grids of the version were made with, e.g. ``power_flow_assessment`` (voltage band) and
        ``cable_dimensioning`` (voltage-drop limits); today's configuration may differ.
        """
        self.cur.execute("SELECT generation_parameters FROM pylovo.version WHERE version_id = %s",
                         (VERSION_ID if version_id is None else str(version_id),))
        row = self.cur.fetchone()
        if row is None or row[0] is None:
            return {}
        value = row[0]
        return json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
