"""Queries of the LV results that the MV stage builds on.

Reads, for an explicit ``version_id``, the generated PLZ and the planning region, the LV version
snapshot, the stations of the LV grids with their load statistics, the MV customers of
``pylovo.mv_customers``, the LV-side generation targets, the stored LV voltage requirement, the
stored LV pandapower nets and the population of the PLZ. Read-only (no temporary tables, so the
methods also run in a read-only transaction); the methods never commit. The planning rules that turn
these rows into MV nodes live in :mod:`pylovo.mv_grid.inputs` (DESIGN §3.1, §3.4–§3.6).
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Iterator, Sequence

import numpy as np
import pandas as pd
from psycopg2 import sql

from pylovo.config_loader import INFDB_SOURCE_SCHEMA, TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin
from pylovo.database.transformer_sources import IS_DSO_TRANSFORMER_SQL, SOURCE_ENABLED_SQL, source_params

if TYPE_CHECKING:   # imported lazily at run time: an LV-only run never loads the MV stage or pandapower here
    import pandapower as pp

    from pylovo.mv_grid.parameters import LvVersionParameters

#: Advisory lock key of LV generation per PLZ (``UtilsMixin.acquire_plz_lock``).
PLZ_LOCK_KEY = 907361
#: DataFrames kept when an LV net is streamed for the coupling (DESIGN §5.7); the others are not deserialised.
LV_NET_TABLES: frozenset[str] = frozenset({"bus", "line", "trafo", "load", "ext_grid"})

#: Columns of :meth:`MvInputMixin.station_nodes`.
STATION_NODE_COLUMNS: tuple[str, ...] = (
    "grid_result_id", "plz", "kcid", "bcid", "sn_kva", "power_flow_status", "lv_max_drop_pu", "plz_settlement_type",
    "station_source", "x", "y",
)
#: Columns of :meth:`MvInputMixin.station_load_stats`.
STATION_STATS_COLUMNS: tuple[str, ...] = (
    "grid_result_id", "category", "installed_kw", "units", "n_buildings", "grid_n_buildings",
)
#: Columns of :meth:`MvInputMixin.mv_customer_rows`.
CUSTOMER_ROW_COLUMNS: tuple[str, ...] = (
    "objectid", "plz", "nonresidential_use", "installed_kw", "x", "y", "mapped_station", "source", "footprint_m2",
    "nonresidential_floor_area",
)
#: Columns of :meth:`MvInputMixin.plz_population`.
POPULATION_COLUMNS: tuple[str, ...] = ("plz", "pop", "area_km2", "polygon_km2")

_STATION_NODES_SQL = sql.SQL("""
SELECT gr.grid_result_id, gr.plz, gr.kcid, gr.bcid,
       gr.transformer_rated_power::double precision AS sn_kva,
       gr.power_flow_status, gr.max_total_lv_voltage_drop_pu AS lv_max_drop_pu,
       pr.settlement_type AS plz_settlement_type,
       CASE WHEN tp.grid_result_id IS NULL THEN 'lv_bus_fallback'
            WHEN tp.comment = 'on_way' THEN 'greenfield'
            WHEN tp.osm_id LIKE 'dso/%%' OR tp.osm_id LIKE 'dso_validation/%%' THEN 'dso'
            WHEN tp.osm_id LIKE 'manual/%%' THEN 'manual'
            WHEN tp.osm_id LIKE 'lod2/%%' THEN 'lod2'
            WHEN tp.osm_id ~ '^(node|way|relation)/' THEN 'osm'
            ELSE 'brownfield_unlinked' END AS station_source,
       ST_X(g.geom) AS x, ST_Y(g.geom) AS y
FROM pylovo.grid_result gr
JOIN pylovo.postcode_result pr ON pr.version_id = gr.version_id AND pr.postcode_result_plz = gr.plz
LEFT JOIN pylovo.transformer_positions tp ON tp.version_id = gr.version_id AND tp.grid_result_id = gr.grid_result_id
CROSS JOIN LATERAL (
    SELECT COALESCE(tp.geom,
           (SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(b.geo::text), 4326), {epsg})
            FROM pylovo.pandapower_bus b
            WHERE b.grid_result_id = gr.grid_result_id AND b.name = 'LVbus 1' LIMIT 1)) AS geom) g
WHERE gr.version_id = %(v)s AND gr.plz = ANY(%(plz)s)
ORDER BY gr.grid_result_id
""")

# The rules of utils._building_load_components: a residential component where the residential peak and the
# households are positive, a non-residential one where its peak is positive and it is not MV-direct.
_STATION_STATS_SQL = """
WITH comp AS (
    SELECT br.grid_result_id, br.objectid, c.category, c.installed_kw, c.units
    FROM pylovo.buildings_result br
    JOIN pylovo.grid_result gr ON gr.version_id = br.version_id AND gr.grid_result_id = br.grid_result_id
    CROSS JOIN LATERAL (VALUES
        ('Residential',
         CASE WHEN br.residential_peak_load_in_kw > 0 AND br.households > 0 THEN br.residential_peak_load_in_kw END,
         CASE WHEN br.residential_peak_load_in_kw > 0 AND br.households > 0 THEN br.households::double precision END),
        (br.nonresidential_use,
         CASE WHEN br.nonresidential_peak_load_in_kw > 0 AND NOT COALESCE(br.nonresidential_mv_direct, false)
              THEN br.nonresidential_peak_load_in_kw END,
         CASE WHEN br.nonresidential_peak_load_in_kw > 0 AND NOT COALESCE(br.nonresidential_mv_direct, false)
              THEN 1.0::double precision END)
    ) AS c(category, installed_kw, units)
    WHERE br.version_id = %(v)s AND gr.plz = ANY(%(plz)s) AND c.installed_kw IS NOT NULL
)
SELECT s.grid_result_id, s.category, s.installed_kw, s.units, s.n_buildings, t.grid_n_buildings
FROM (SELECT grid_result_id, category, sum(installed_kw) AS installed_kw, sum(units) AS units,
             count(DISTINCT objectid) AS n_buildings
      FROM comp GROUP BY grid_result_id, category) s
JOIN (SELECT grid_result_id, count(DISTINCT objectid) AS grid_n_buildings FROM comp GROUP BY grid_result_id) t
     USING (grid_result_id)
ORDER BY s.grid_result_id, s.category
"""

_LOAD_CHECK_SQL = """
SELECT pl.grid_result_id, sum(pl.p_mw) * 1000.0 AS p_kw
FROM pylovo.pandapower_load pl
JOIN pylovo.grid_result gr ON gr.grid_result_id = pl.grid_result_id
WHERE gr.version_id = %(v)s AND gr.plz = ANY(%(plz)s)
GROUP BY pl.grid_result_id
ORDER BY pl.grid_result_id
"""

_CUSTOMERS_SQL = sql.SQL("""
SELECT mc.objectid, mc.plz, mc.nonresidential_use, mc.nonresidential_peak_load_in_kw AS installed_kw,
       ST_X(COALESCE(st.geom, mc.centroid)) AS x, ST_Y(COALESCE(st.geom, mc.centroid)) AS y,
       (st.geom IS NOT NULL) AS mapped_station, 'mv_customers' AS source, ST_Area(mc.geom) AS footprint_m2,
       mc.nonresidential_floor_area
FROM pylovo.mv_customers mc
LEFT JOIN LATERAL (
    SELECT ST_GeometryN(t.geom, 1) AS geom FROM pylovo.transformers t
    WHERE ST_Intersects(t.geom, mc.geom) AND {enabled}
    ORDER BY t.osm_id LIMIT 1) st ON true
WHERE mc.version_id = %(v)s AND mc.plz = ANY(%(plz)s) AND mc.nonresidential_peak_load_in_kw > 0
ORDER BY mc.objectid
""").format(enabled=sql.SQL(SOURCE_ENABLED_SQL))

# Versions generated before pylovo.mv_customers existed: only the mixed MV-direct buildings are left in
# buildings_result (the pure ones were removed from the LV model), DESIGN §2.6.
_CUSTOMERS_FALLBACK_SQL = """
SELECT br.objectid, br.postcode AS plz, br.nonresidential_use, br.nonresidential_peak_load_in_kw AS installed_kw,
       ST_X(COALESCE(br.centroid, ST_PointOnSurface(br.geom))) AS x,
       ST_Y(COALESCE(br.centroid, ST_PointOnSurface(br.geom))) AS y,
       false AS mapped_station, 'buildings_result' AS source, ST_Area(br.geom) AS footprint_m2,
       br.nonresidential_floor_area
FROM pylovo.buildings_result br
WHERE br.version_id = %(v)s AND br.postcode = ANY(%(plz)s) AND br.nonresidential_mv_direct
  AND br.nonresidential_peak_load_in_kw > 0
ORDER BY br.objectid
"""

# LV-side generation points: the grid of the nearest building of the version (KNN on buildings_result.geom,
# which has the GiST index; WP0 D16). unnest instead of a temporary table: no writes.
_LV_TARGETS_SQL = sql.SQL("""
SELECT p.key, b.grid_result_id, b.distance_m
FROM unnest(%(keys)s::bigint[], %(xs)s::double precision[], %(ys)s::double precision[]) AS p(key, x, y)
CROSS JOIN LATERAL (SELECT ST_SetSRID(ST_MakePoint(p.x, p.y), {epsg}) AS geom) q
LEFT JOIN LATERAL (
    SELECT br.grid_result_id, ST_Distance(br.geom, q.geom) AS distance_m
    FROM pylovo.buildings_result br
    WHERE br.version_id = %(v)s AND br.grid_result_id IS NOT NULL
    ORDER BY br.geom <-> q.geom
    LIMIT 1) b ON b.distance_m <= %(max_m)s
ORDER BY p.key
""")

_BUILDING_COUNTS_SQL = """
SELECT br.grid_result_id, lpad(br.postcode::text, 5, '0') AS postcode, count(*) AS n_buildings
FROM pylovo.buildings_result br
JOIN pylovo.grid_result gr ON gr.version_id = br.version_id AND gr.grid_result_id = br.grid_result_id
WHERE br.version_id = %(v)s AND gr.plz = ANY(%(plz)s) AND br.postcode IS NOT NULL
GROUP BY br.grid_result_id, br.postcode
ORDER BY br.grid_result_id, br.postcode
"""

# grid is json (not jsonb); the ext_grid DataFrame is a split-orient JSON string inside it
_LV_REQUIREMENT_SQL = """
SELECT gr.grid_result_id,
       ((gr.grid->'_object'->'ext_grid'->>'_object')::json->'data'->0->>(ce.ord - 1)::int)::float AS vm_pu
FROM pylovo.grid_result gr
CROSS JOIN LATERAL json_array_elements_text((gr.grid->'_object'->'ext_grid'->>'_object')::json->'columns')
     WITH ORDINALITY ce(name, ord)
WHERE gr.version_id = %(v)s AND gr.grid_result_id = ANY(%(ids)s) AND ce.name = 'vm_pu'
ORDER BY gr.grid_result_id
"""

# Population per PLZ (municipal_register has one row per PLZ and municipality, all with the PLZ population) and
# the PLZ area of the register next to the area of the PLZ polygon that the planning region is made of.
_POPULATION_SQL = """
SELECT q.plz, r.pop, r.area_km2, ST_Area(p.geom) / 1e6 AS polygon_km2
FROM unnest(%(plz)s::integer[]) AS q(plz)
LEFT JOIN (SELECT plz, max(pop) AS pop, max(area) AS area_km2 FROM pylovo.municipal_register
           WHERE plz = ANY(%(plz)s) GROUP BY plz) r ON r.plz = q.plz
LEFT JOIN pylovo.postcode p ON p.plz = q.plz
ORDER BY q.plz
"""

_LOST_OVERLAP_SQL = sql.SQL("""
SELECT b.objectid::text AS objectid, b.postcode, b.building_use,
       COALESCE(b.nonresidential_floor_area,
                COALESCE(b.floor_area, ST_Area(ST_Transform(b.geom, {epsg}))) * COALESCE(b.floor_number, 1)) AS area_m2
FROM {buildings} b
WHERE b.postcode = ANY(%(plz)s) AND COALESCE(b.residential_floor_area, 0) <= 0
  AND b.building_use IN ('Commercial', 'Public', 'Unknown', 'Mixed')
  AND EXISTS (SELECT 1 FROM pylovo.transformers t
              WHERE ST_Intersects(t.geom, ST_Transform(b.geom, {epsg})) AND NOT {is_dso})
  AND NOT EXISTS (SELECT 1 FROM pylovo.mv_customers m WHERE m.version_id = %(v)s AND m.objectid = b.objectid::text)
  AND NOT EXISTS (SELECT 1 FROM pylovo.buildings_result br
                  WHERE br.version_id = %(v)s AND br.objectid = b.objectid::text)
ORDER BY b.objectid
""")


def _ints(values: Sequence[Any] | None) -> list[int]:
    return sorted({int(v) for v in values}) if values is not None else []


def _frame(rows: list[tuple], columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=list(columns))


def _stage_error(message: str) -> Exception:
    """An :class:`~pylovo.mv_grid.parameters.MvStageError` (imported on use: LV runs never load the MV stage)."""
    from pylovo.mv_grid.parameters import MvStageError

    return MvStageError(message)


class MvInputMixin(BaseMixin):
    """Read the LV results of a version as inputs of the MV stage."""

    # ------------------------------------------------------------------------------------------ PLZ, region, LV
    def generated_plz(self, version_id: str, plz_list: Sequence[int] | None) -> list[int]:
        """The PLZ of the request that have LV results (a ``postcode_result`` row) in the version.

        The database, not the logs, says what was generated; requested PLZ without results are simply missing
        from the answer (border slivers without buildings never get grids).

        Args:
            version_id: LV version.
            plz_list: Requested PLZ; None means every generated PLZ of the version (``--all``).

        Returns:
            Sorted PLZ.
        """
        if plz_list is None:
            self.cur.execute("SELECT postcode_result_plz FROM pylovo.postcode_result WHERE version_id = %(v)s "
                             "ORDER BY 1", {"v": version_id})
        else:
            requested = _ints(plz_list)
            if not requested:
                return []
            self.cur.execute("SELECT postcode_result_plz FROM pylovo.postcode_result "
                             "WHERE version_id = %(v)s AND postcode_result_plz = ANY(%(plz)s) ORDER BY 1",
                             {"v": version_id, "plz": requested})
        return [int(row[0]) for row in self.cur.fetchall()]

    def busy_plz(self, plz_list: Sequence[int]) -> list[int]:
        """The PLZ whose LV generation runs right now in another session (DESIGN §1.4).

        Each PLZ lock (advisory lock 907361, taken by LV generation per PLZ) is tried and released at once, so
        nothing stays locked. Call it only for requested PLZ without results.

        Returns:
            Sorted PLZ whose lock is held by another session.
        """
        busy = []
        for plz in _ints(plz_list):
            self.cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (PLZ_LOCK_KEY, plz))
            if self.cur.fetchone()[0]:
                self.cur.execute("SELECT pg_advisory_unlock(%s, %s)", (PLZ_LOCK_KEY, plz))
            else:
                busy.append(plz)
        return busy

    def lv_snapshot(self, version_id: str) -> dict | None:
        """The generation parameter snapshot of an LV version (``pylovo.version.generation_parameters``).

        Returns:
            The snapshot as a dict, or None if the version has none (``LvVersionParameters`` then raises).

        Raises:
            MvStageError: If the version does not exist.
        """
        self.cur.execute("SELECT generation_parameters FROM pylovo.version WHERE version_id = %s", (version_id,))
        row = self.cur.fetchone()
        if row is None:
            raise _stage_error(f"LV version {version_id} does not exist")
        value = row[0]
        if isinstance(value, (str, bytes)):
            value = json.loads(value)
        return value

    def planning_region(self, plz_list: Sequence[int]) -> bytes:
        """The planning region: the union of the PLZ polygons (``pylovo.postcode``, version independent).

        Returns:
            MultiPolygon WKB in ``TARGET_EPSG``.

        Raises:
            MvStageError: If none of the PLZ has a polygon.
        """
        plz = _ints(plz_list)
        self.cur.execute("SELECT ST_AsBinary(ST_Multi(ST_CollectionExtract(ST_UnaryUnion(ST_Collect(p.geom)), 3))) "
                         "FROM pylovo.postcode p WHERE p.plz = ANY(%(plz)s)", {"plz": plz})
        row = self.cur.fetchone()
        if row is None or row[0] is None:
            raise _stage_error(f"no postcode polygons in pylovo.postcode for the PLZ {plz[:10]}")
        return bytes(row[0])

    # ------------------------------------------------------------------------------------------ stations
    def station_nodes(self, version_id: str, plz_list: Sequence[int]) -> pd.DataFrame:
        """One row per LV grid of the version's PLZ: station position, rating and LV status (DESIGN §3.4).

        The position is ``transformer_positions.geom`` (greenfield and brownfield alike), else the stored
        ``LVbus 1``. ``station_source`` is greenfield, osm, lod2, dso, manual, brownfield_unlinked or
        lv_bus_fallback. Grids with neither position are dropped with a warning; their ids are in
        ``attrs["dropped_grid_result_ids"]``.

        Returns:
            :data:`STATION_NODE_COLUMNS`, sorted by ``grid_result_id``.
        """
        plz = _ints(plz_list)
        if not plz:
            frame = _frame([], STATION_NODE_COLUMNS)
            frame.attrs["dropped_grid_result_ids"] = []
            return frame
        self.cur.execute(_STATION_NODES_SQL.format(epsg=sql.Literal(int(TARGET_EPSG))), {"v": version_id, "plz": plz})
        frame = _frame(self.cur.fetchall(), STATION_NODE_COLUMNS)
        missing = frame["x"].isna() | frame["y"].isna()
        dropped = [int(g) for g in frame.loc[missing, "grid_result_id"]]
        if dropped:
            self.logger.warning(f"{len(dropped)} LV grids of version {version_id} have neither a transformer position "
                                f"nor an LVbus 1 and are no MV nodes: {dropped[:20]}")
        frame = frame[~missing].reset_index(drop=True)
        for column in ("sn_kva", "lv_max_drop_pu", "x", "y"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
        frame.attrs["dropped_grid_result_ids"] = dropped
        return frame

    def station_load_stats(self, version_id: str, plz_list: Sequence[int]) -> pd.DataFrame:
        """Per LV grid and consumer category: installed load, load units and buildings (DESIGN §3.4).

        The load components follow ``utils._building_load_components``: residential where the residential peak
        and the households are positive (units = households), non-residential where the peak is positive and the
        building is not MV-direct (one unit). ``grid_n_buildings`` counts the distinct buildings with a component
        per grid (a mixed building counts once).

        Returns:
            :data:`STATION_STATS_COLUMNS` (long form), sorted by ``grid_result_id`` and ``category``.
        """
        plz = _ints(plz_list)
        if not plz:
            return _frame([], STATION_STATS_COLUMNS)
        self.cur.execute(_STATION_STATS_SQL, {"v": version_id, "plz": plz})
        frame = _frame(self.cur.fetchall(), STATION_STATS_COLUMNS)
        for column in ("installed_kw", "units"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
        return frame

    def pandapower_load_check(self, version_id: str, plz_list: Sequence[int]) -> pd.DataFrame:
        """The summed stored LV loads per grid of the version (cross-check of the station peaks, DESIGN §3.4).

        pylovo allocates each grid's coincident peak to its loads, so the sum equals the station's ``p_own_kw``
        unless loads were edited or rows are duplicated.

        Returns:
            Columns ``grid_result_id``, ``p_kw``.
        """
        plz = _ints(plz_list)
        if not plz:
            return _frame([], ("grid_result_id", "p_kw"))
        self.cur.execute(_LOAD_CHECK_SQL, {"v": version_id, "plz": plz})
        frame = _frame(self.cur.fetchall(), ("grid_result_id", "p_kw"))
        frame["p_kw"] = pd.to_numeric(frame["p_kw"], errors="coerce").astype(float)
        return frame

    def station_building_counts(self, version_id: str, plz_list: Sequence[int]) -> pd.DataFrame:
        """Buildings per LV grid and building postcode (to split the MaStR postcode aggregates, DESIGN §3.6).

        Returns:
            Columns ``grid_result_id``, ``postcode`` (text, 5 digits), ``n_buildings``.
        """
        plz = _ints(plz_list)
        if not plz:
            return _frame([], ("grid_result_id", "postcode", "n_buildings"))
        self.cur.execute(_BUILDING_COUNTS_SQL, {"v": version_id, "plz": plz})
        return _frame(self.cur.fetchall(), ("grid_result_id", "postcode", "n_buildings"))

    def lv_design_source_vm(self, version_id: str, grid_result_ids: Sequence[int]) -> dict[int, float]:
        """The stored LV requirement per grid: ``ext_grid.vm_pu`` of its net (DESIGN §3.4, §5.6).

        It is the MV-side voltage that puts the LV busbar at ``LV_REFERENCE_VOLTAGE_PU`` at the grid's own load.
        Read per district (the JSON operators parse the stored net text).

        Returns:
            ``{grid_result_id: vm_pu}``; grids without a stored pandapower ``ext_grid`` are missing.
        """
        ids = _ints(grid_result_ids)
        if not ids:
            return {}
        self.cur.execute(_LV_REQUIREMENT_SQL, {"v": version_id, "ids": ids})
        return {int(g): float(v) for g, v in self.cur.fetchall() if v is not None}

    def iter_lv_nets(self, version_id: str, grid_result_ids: Sequence[int], chunk: int = 50
                     ) -> Iterator[tuple[int, "pp.pandapowerNet"]]:
        """Stream the stored LV pandapower nets, pruned to ``bus``, ``line``, ``trafo``, ``load``, ``ext_grid``.

        The nets are fetched as text (psycopg2 does not parse them) in chunks of ``chunk`` grids; each chunk is
        fetched completely before its nets are yielded, so the shared cursor may be used between yields. Grids
        without a pandapower net (no net, another backend's JSON) are skipped (logged).

        Yields:
            ``(grid_result_id, net)`` in ascending ``grid_result_id``.
        """
        import pandapower as pp

        from pylovo.electrical_backend.pandapower.backend import empty_network

        ids = _ints(grid_result_ids)
        step = max(int(chunk), 1)
        for start in range(0, len(ids), step):
            part = ids[start:start + step]
            self.cur.execute("SELECT grid_result_id, grid::text FROM pylovo.grid_result "
                             "WHERE version_id = %s AND grid_result_id = ANY(%s) ORDER BY grid_result_id",
                             (version_id, part))
            rows = self.cur.fetchall()
            for grid_result_id, text in rows:
                if text is None:
                    self.logger.info(f"LV grid {grid_result_id} has no stored net")
                    continue
                data = json.loads(text)
                if not isinstance(data, dict) or data.get("_class") != "pandapowerNet" \
                        or not isinstance(data.get("_object"), dict):
                    self.logger.info(f"LV grid {grid_result_id} has no pandapower net")
                    continue
                data["_object"] = {key: value for key, value in data["_object"].items()
                                   if key in LV_NET_TABLES
                                   or not (isinstance(value, dict) and value.get("_class") == "DataFrame")}
                net = pp.from_json_string(json.dumps(data), empty_dict_like_object=empty_network())
                yield int(grid_result_id), net

    # ------------------------------------------------------------------------------------------ customers
    def mv_customer_rows(self, version_id: str, plz_list: Sequence[int], lv: "LvVersionParameters") -> pd.DataFrame:
        """The MV customers of the version's PLZ (``pylovo.mv_customers``, DESIGN §3.5).

        Position: the station candidate inside the building if the LV version used that source
        (``USE_*_TRANSFORMER_POSITIONS`` of its snapshot, so DSO points only for DSO versions; ``mapped_station``),
        else the building centroid. ``footprint_m2`` (area of the building polygon) and
        ``nonresidential_floor_area`` feed the hall rule of :func:`~pylovo.mv_grid.inputs.customer_table`.
        Versions without ``pylovo.mv_customers`` rows fall back to the mixed MV-direct buildings of
        ``buildings_result`` (warning; ``source = 'buildings_result'``).

        Returns:
            :data:`CUSTOMER_ROW_COLUMNS`, sorted by ``objectid``.
        """
        plz = _ints(plz_list)
        if not plz:
            return _frame([], CUSTOMER_ROW_COLUMNS)
        self.cur.execute("SELECT EXISTS (SELECT 1 FROM pylovo.mv_customers WHERE version_id = %s)", (version_id,))
        if self.cur.fetchone()[0]:
            params = {"v": version_id, "plz": plz, **source_params(lv.use_dso, lv.use_open, lv.use_manual)}
            self.cur.execute(_CUSTOMERS_SQL, params)
        else:
            self.logger.warning(f"LV version {version_id} has no rows in pylovo.mv_customers (generated before "
                                "they were stored): only the mixed MV-direct buildings of buildings_result are "
                                "MV customers")
            self.cur.execute(_CUSTOMERS_FALLBACK_SQL, {"v": version_id, "plz": plz})
        frame = _frame(self.cur.fetchall(), CUSTOMER_ROW_COLUMNS)
        for column in ("installed_kw", "x", "y", "footprint_m2", "nonresidential_floor_area"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
        return frame

    def plz_population(self, plz_list: Sequence[int]) -> pd.DataFrame:
        """Population and area of the PLZ for the demand anchor (:func:`~pylovo.mv_grid.inputs.demand_anchor`).

        ``pop`` is ``pylovo.municipal_register.pop`` (the population of the PLZ, repeated on each of its
        municipality rows), ``area_km2`` the PLZ area of the register and ``polygon_km2`` the area of the
        ``pylovo.postcode`` polygon; a polygon smaller than the register area is cut by the data extent. NaN where a
        PLZ has no register row or no polygon.

        Returns:
            :data:`POPULATION_COLUMNS`, one row per requested PLZ, sorted by ``plz``.
        """
        plz = _ints(plz_list)
        if not plz:
            return _frame([], POPULATION_COLUMNS)
        self.cur.execute(_POPULATION_SQL, {"plz": plz})
        frame = _frame(self.cur.fetchall(), POPULATION_COLUMNS)
        for column in ("pop", "area_km2", "polygon_km2"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
        return frame

    def lost_station_overlap_customers(self, version_id: str, plz_list: Sequence[int],
                                       lv: "LvVersionParameters") -> pd.DataFrame:
        """Large non-residential buildings the LV stage removed because a station candidate lies inside them.

        Diagnostic of the station-overlap loss (DESIGN §2.6, §3.5; notebook 3): pure non-residential source
        buildings of the PLZ with a non-DSO candidate inside, neither in ``buildings_result`` nor in
        ``mv_customers`` of the version, whose installed load by the LV formulas (non-residential floor area, else
        footprint x floors, times the W/m² of the LV snapshot's consumer categories; ``Mixed`` counted as
        Commercial) exceeds the version's MV-direct threshold.

        Returns:
            Columns ``objectid``, ``postcode``, ``building_use``, ``category``, ``area_m2``, ``installed_kw``,
            ``p_kw`` (``g_c x installed``), sorted by ``installed_kw`` descending.
        """
        columns = ["objectid", "postcode", "building_use", "category", "area_m2", "installed_kw", "p_kw"]
        plz = _ints(plz_list)
        if not plz or not INFDB_SOURCE_SCHEMA:
            return pd.DataFrame(columns=columns)
        snapshot = self.lv_snapshot(version_id) or {}
        per_m2 = {str(rec.get("definition")): rec.get("peak_load_per_m2")
                  for rec in (snapshot.get("load_calculation") or {}).get("consumer_categories") or []
                  if isinstance(rec, dict)}
        query = _LOST_OVERLAP_SQL.format(epsg=sql.Literal(int(TARGET_EPSG)),
                                         buildings=sql.Identifier(INFDB_SOURCE_SCHEMA, "buildings"),
                                         is_dso=sql.SQL(IS_DSO_TRANSFORMER_SQL))
        self.cur.execute(query, {"v": version_id, "plz": plz})
        frame = pd.DataFrame(self.cur.fetchall(), columns=["objectid", "postcode", "building_use", "area_m2"])
        if not len(frame):
            return pd.DataFrame(columns=columns)
        frame["category"] = frame["building_use"].map(lambda u: "Commercial" if u == "Mixed" else u)
        w_per_m2 = frame["category"].map(lambda c: float(per_m2.get(c) or 0.0))
        frame["area_m2"] = pd.to_numeric(frame["area_m2"], errors="coerce").fillna(0.0)
        frame["installed_kw"] = frame["area_m2"] * w_per_m2 / 1000.0
        frame["p_kw"] = frame["installed_kw"] * frame["category"].map(lambda c: float(lv.sim_factor.get(c, 0.0)))
        frame = frame[frame["installed_kw"] > lv.mv_direct_threshold_kw]
        return frame.sort_values(["installed_kw", "objectid"], ascending=[False, True])[columns] \
            .reset_index(drop=True)

    # ------------------------------------------------------------------------------------------ generation
    def lv_side_targets(self, version_id: str, points: pd.DataFrame, max_m: float = 100.0) -> pd.DataFrame:
        """The LV grid of the nearest building of the version within ``max_m`` of each point (DESIGN §3.6).

        Used for the generation connected on the LV side: rooftop PV lies on (or next to) the building whose LV
        grid connects it.

        Args:
            version_id: LV version.
            points: ``key`` (unique integers; default: the row number), ``x``, ``y`` in ``TARGET_EPSG`` and any
                other columns (kept).
            max_m: Largest distance to the building polygon (m).

        Returns:
            ``points`` (index reset) with ``grid_result_id`` (NaN where no building is within ``max_m``) and
            ``target_distance_m``.
        """
        frame = pd.DataFrame(points).copy().reset_index(drop=True)
        if "key" not in frame.columns:
            frame["key"] = np.arange(len(frame), dtype=np.int64)
        frame["grid_result_id"] = np.nan
        frame["target_distance_m"] = np.nan
        xs = pd.to_numeric(frame.get("x"), errors="coerce") if len(frame) else pd.Series(dtype=float)
        ys = pd.to_numeric(frame.get("y"), errors="coerce") if len(frame) else pd.Series(dtype=float)
        ok = (xs.notna() & ys.notna()).to_numpy() if len(frame) else np.zeros(0, dtype=bool)
        if not ok.any():
            return frame
        keys = frame.loc[ok, "key"].astype("int64").tolist()
        if len(set(keys)) != len(keys):
            raise ValueError("lv_side_targets needs unique point keys")
        self.cur.execute(_LV_TARGETS_SQL.format(epsg=sql.Literal(int(TARGET_EPSG))),
                         {"keys": keys, "xs": xs[ok].astype(float).tolist(), "ys": ys[ok].astype(float).tolist(),
                          "v": version_id, "max_m": float(max_m)})
        found = pd.DataFrame(self.cur.fetchall(), columns=["key", "grid_result_id", "distance_m"]).set_index("key")
        position = pd.Series(np.flatnonzero(ok), index=keys)
        hit = found["grid_result_id"].notna()
        rows = position.loc[found.index[hit]].to_numpy()
        frame.loc[rows, "grid_result_id"] = found.loc[hit, "grid_result_id"].astype(float).to_numpy()
        frame.loc[rows, "target_distance_m"] = found.loc[hit, "distance_m"].astype(float).to_numpy()
        return frame

    # ------------------------------------------------------------------------------------------ staleness
    def lv_fingerprint(self, version_id: str, plz_list: Sequence[int]) -> dict:
        """A fingerprint of the LV results of the PLZ that changes with regenerated grids, load edits and MV
        customers (staleness check of an MV variant, DESIGN §2.5).

        Returns:
            ``{"n_grids", "sum_grid_result_id", "max_load_edit_id", "n_mv_customers"}`` (JSON-native;
            ``max_load_edit_id`` None without load edits).
        """
        plz = _ints(plz_list)
        self.cur.execute("SELECT count(*), COALESCE(sum(grid_result_id), 0) FROM pylovo.grid_result "
                         "WHERE version_id = %(v)s AND plz = ANY(%(plz)s)", {"v": version_id, "plz": plz})
        n_grids, sum_ids = self.cur.fetchone()
        self.cur.execute("SELECT max(load_edit_id) FROM pylovo.load_edit WHERE version_id = %s", (version_id,))
        max_edit = self.cur.fetchone()[0]
        self.cur.execute("SELECT count(*) FROM pylovo.mv_customers WHERE version_id = %(v)s AND plz = ANY(%(plz)s)",
                         {"v": version_id, "plz": plz})
        n_customers = self.cur.fetchone()[0]
        return {"n_grids": int(n_grids), "sum_grid_result_id": int(sum_ids),
                "max_load_edit_id": None if max_edit is None else int(max_edit), "n_mv_customers": int(n_customers)}
