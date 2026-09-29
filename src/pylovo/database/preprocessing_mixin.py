"""Queries that prepare the input of one PLZ: version snapshot, configuration tables,
buildings, transformers and ways in the working tables ``buildings_tem`` and ``ways_tem``."""

import json
import warnings

import numpy as np
import pandas as pd
from psycopg2 import sql
from psycopg2.extras import execute_batch

from pylovo.config_loader import (
    AGGREGATE_NEARBY_CONNECTION_POINTS,
    CONFIG_EQUIPMENT_DATA,
    CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS,
    CONNECTION_POINT_AGGREGATION_RADIUS_M,
    CONSUMER_CATEGORIES,
    DEFAULT_POWER_FACTOR,
    ELECTRICAL_BACKEND,
    EXCLUDE_BUILDINGS_WITHOUT_ADDRESS,
    FEEDER_SPLIT_MAX_CURRENT_KA,
    GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA,
    GREENFIELD_TRAFO_POSITION_TOLERANCE,
    K_MEANS_SEED,
    LV_REFERENCE_VOLTAGE_PU,
    MAX_BROWNFIELD_TRAFO_DISTANCE,
    MAX_BUILDINGS_PER_KCID,
    MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT,
    MAX_GREENFIELD_TRAFO_DISTANCE,
    MAX_GREENFIELD_TRAFO_DISTANCE_STD,
    MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT,
    MAX_TAP_STEPS,
    MERGE_GREENFIELD_CLUSTERS,
    MIN_SHARED_PREFIX_LENGTH_M,
    MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW,
    PEAK_LOAD_HOUSEHOLD,
    POWER_FLOW_MAX_VM_PU,
    POWER_FLOW_MIN_VM_PU,
    RESIDENTIAL_ONLY_GENERATION,
    RURAL_MAX_HOUSEHOLDS,
    RURAL_MIN_BUILDING_DISTANCE,
    SIM_FACTOR,
    TARGET_EPSG,
    TRANSFORMER_MAPPING,
    TRANSFORMER_PLANNING_UTILIZATION,
    URBAN_MAX_BUILDING_DISTANCE,
    URBAN_MIN_HOUSEHOLDS,
    USE_DSO_TRANSFORMER_POSITIONS,
    USE_INFDB,
    USE_MANUAL_TRANSFORMER_POSITIONS,
    USE_OPEN_TRANSFORMER_POSITIONS,
    VERSION_COMMENT,
    VERSION_ID,
    VN,
)
from pylovo.database.base_mixin import BaseMixin, plz_table_name
from pylovo.database.transformer_sources import IS_DSO_TRANSFORMER_SQL as _IS_DSO_TRANSFORMER_SQL
from pylovo.database.transformer_sources import SOURCE_ENABLED_SQL, source_params
from pylovo.version_snapshot import compare_snapshots

warnings.simplefilter(action='ignore', category=UserWarning)


class PreprocessingMixin(BaseMixin):
    """Fill the PLZ working tables and the configuration tables of the active version.

    Two input paths exist. With ``USE_INFDB=True`` (default) buildings and ways come from InfDB
    through :class:`~pylovo.infdb.infdb_client.InfdbClient`; with ``USE_INFDB=False`` they are
    read from the local tables ``res``, ``oth`` and ``ways`` that ``pylovo-setup`` and the
    building import fill from files.
    """

    @staticmethod
    def _dataframe_records(df: pd.DataFrame) -> list[dict]:
        return json.loads(df.to_json(orient="records"))

    def _generation_parameters_snapshot(self) -> dict:
        return {
            "electrical_backend": ELECTRICAL_BACKEND,
            "residential_only_generation": RESIDENTIAL_ONLY_GENERATION,
            "exclude_buildings_without_address": EXCLUDE_BUILDINGS_WITHOUT_ADDRESS,
            "load_calculation": {
                "peak_load_household": PEAK_LOAD_HOUSEHOLD,
                "sim_factor": SIM_FACTOR,
                "default_power_factor": DEFAULT_POWER_FACTOR,
                "household_fallback": self._household_fallback_parameters(),
                "consumer_categories": self._dataframe_records(CONSUMER_CATEGORIES),
            },
            "equipment_data": self._dataframe_records(CONFIG_EQUIPMENT_DATA),
            "cable_dimensioning": {
                "vn": VN,
                "min_shared_prefix_length_m": MIN_SHARED_PREFIX_LENGTH_M,
                "feeder_split_max_current_ka": FEEDER_SPLIT_MAX_CURRENT_KA,
                "max_end_to_end_feeder_voltage_drop_percent": MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT,
                "max_service_design_voltage_drop_percent": MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT,
                "mv_direct_connection_load_threshold_kw": MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW,
            },
            "power_flow_assessment": {
                "min_vm_pu": POWER_FLOW_MIN_VM_PU,
                "max_vm_pu": POWER_FLOW_MAX_VM_PU,
                "lv_reference_voltage_pu": LV_REFERENCE_VOLTAGE_PU,
                "max_tap_steps": MAX_TAP_STEPS,
            },
            "connection_point_aggregation": {
                "enabled": AGGREGATE_NEARBY_CONNECTION_POINTS,
                "radius_m": CONNECTION_POINT_AGGREGATION_RADIUS_M,
                "max_buildings": CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS,
            },
            "transformer_placement": {
                "rural_max_households": RURAL_MAX_HOUSEHOLDS,
                "urban_min_households": URBAN_MIN_HOUSEHOLDS,
                "rural_min_building_distance": RURAL_MIN_BUILDING_DISTANCE,
                "urban_max_building_distance": URBAN_MAX_BUILDING_DISTANCE,
                "transformer_mapping": TRANSFORMER_MAPPING,
                "max_brownfield_trafo_distance": MAX_BROWNFIELD_TRAFO_DISTANCE,
                "use_dso_transformer_positions": USE_DSO_TRANSFORMER_POSITIONS,
                "use_open_transformer_positions": USE_OPEN_TRANSFORMER_POSITIONS,
                "use_manual_transformer_positions": USE_MANUAL_TRANSFORMER_POSITIONS,
                "merge_greenfield_clusters": MERGE_GREENFIELD_CLUSTERS,
                "greenfield_cluster_merge_transformer_kva": GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA,
                "max_greenfield_trafo_distance": MAX_GREENFIELD_TRAFO_DISTANCE,
                "max_greenfield_trafo_distance_std": MAX_GREENFIELD_TRAFO_DISTANCE_STD,
                "greenfield_trafo_position_tolerance": GREENFIELD_TRAFO_POSITION_TOLERANCE,
                "transformer_planning_utilization": TRANSFORMER_PLANNING_UTILIZATION,
                "max_buildings_per_kcid": MAX_BUILDINGS_PER_KCID,
                "k_means_seed": K_MEANS_SEED,
            },
        }

    def insert_version_if_not_exists(self):
        """Insert an immutable generation-parameter snapshot for the active version."""
        try:
            generation_parameters = json.dumps(
                self._generation_parameters_snapshot(),
                allow_nan=False,
                sort_keys=True,
            )

            self.cur.execute("ALTER TABLE pylovo.version ADD COLUMN IF NOT EXISTS generation_parameters jsonb;")
            insert_query = """
                INSERT INTO pylovo.version (
                    version_id,
                    version_comment,
                    generation_parameters
                )
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (version_id) DO UPDATE
                    SET generation_parameters = EXCLUDED.generation_parameters
                    WHERE pylovo.version.generation_parameters IS NULL;
            """
            self.cur.execute(
                insert_query,
                (
                    VERSION_ID,
                    VERSION_COMMENT,
                    generation_parameters,
                ),
            )
            version_created_or_backfilled = self.cur.rowcount > 0

            self.cur.execute(
                "SELECT generation_parameters FROM pylovo.version WHERE version_id = %s;",
                (VERSION_ID,),
            )
            stored_parameters = self.cur.fetchone()[0]
            if isinstance(stored_parameters, str):
                stored_parameters = json.loads(stored_parameters)
            expected_parameters = json.loads(generation_parameters)
            differences, not_recorded = compare_snapshots(stored_parameters, expected_parameters)
            if differences:
                raise ValueError(
                    f"Generation parameters differ from the stored snapshot for version {VERSION_ID} "
                    f"({', '.join(differences[:5])}{' …' if len(differences) > 5 else ''}). "
                    "Increment VERSION_ID before generating grids with the changed configuration."
                )
            if not_recorded:
                self.logger.warning(
                    f"The stored snapshot of version {VERSION_ID} predates the parameters "
                    f"{', '.join(not_recorded)}; their values used for its existing grids are unknown."
                )

            self.conn.commit()

            if version_created_or_backfilled:
                self.logger.info(f"Version: {VERSION_ID} (created or generation parameters backfilled)")
            else:
                self.logger.debug(f"Version: {VERSION_ID} (configuration matches stored snapshot)")

        except Exception as e:
            self.logger.error(f"Error inserting version {VERSION_ID}: {e}")
            self.conn.rollback()
            raise

    def insert_equipment_data_from_config(self, equipment_data: pd.DataFrame):
        """Upsert the configured equipment (transformers and cables) into ``equipment_data`` and commit.

        Missing columns are filled with NULL, numeric columns are cast to integers, and rows
        without ``version_id`` get the configured ``VERSION_ID``. Existing rows with the same
        ``(version_id, name)`` are updated.

        Args:
            equipment_data: Equipment table, normally ``CONFIG_EQUIPMENT_DATA``.

        Raises:
            psycopg2.Error: If the upsert fails; the transaction is rolled back first.
        """
        df = equipment_data.copy()
        expected_cols = ["version_id", "name", "s_max_kva", "max_i_a", "r_mohm_per_km", "x_mohm_per_km",
                         "z_mohm_per_km", "cost_eur", "typ", "grid_role"]
        if "version_id" not in df.columns:
            df["version_id"] = VERSION_ID

        # Add any missing columns
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None
        
        # Keep only relevant columns
        df = df[expected_cols]

        # Numeric conversion (Int / None)
        int_cols = ["s_max_kva", "max_i_a", "r_mohm_per_km", "x_mohm_per_km", "z_mohm_per_km", "cost_eur"]
        for c in int_cols:
            df[c] = pd.to_numeric(df[c], errors='coerce').astype('Int64')

        # Replace NaNs with None
        df = df.where(~df.isna(), None)

        insert_sql = ("""
                      INSERT INTO pylovo.equipment_data
                      (version_id, name, s_max_kva, max_i_a, r_mohm_per_km, x_mohm_per_km, z_mohm_per_km, cost_eur, typ, grid_role)
                      VALUES (%(version_id)s, %(name)s, %(s_max_kva)s, %(max_i_a)s, %(r_mohm_per_km)s,
                              %(x_mohm_per_km)s, %(z_mohm_per_km)s, %(cost_eur)s, %(typ)s, %(grid_role)s)
                      ON CONFLICT (version_id, name) DO UPDATE SET s_max_kva        = EXCLUDED.s_max_kva,
                                                                   max_i_a          = EXCLUDED.max_i_a,
                                                                   r_mohm_per_km    = EXCLUDED.r_mohm_per_km,
                                                                   x_mohm_per_km    = EXCLUDED.x_mohm_per_km,
                                                                   z_mohm_per_km    = EXCLUDED.z_mohm_per_km,
                                                                   cost_eur         = EXCLUDED.cost_eur,
                                                                   typ              = EXCLUDED.typ,
                                                                   grid_role        = EXCLUDED.grid_role;""")
        rows = df.to_dict(orient='records')
        try:
            self.cur.executemany(insert_sql, rows)
            self.conn.commit()  # Added commit to persist equipment data
            self.logger.info(f"Inserted/updated equipment_data rows: {len(rows)} (version {VERSION_ID})")
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Failed inserting/updating equipment_data for version {VERSION_ID}: {e}")
            raise

    def insert_consumer_categories_from_config(self, consumer_categories: pd.DataFrame):
        """Make ``consumer_categories`` equal to the configured load categories and commit.

        Categories that are no longer configured are deleted, the others are upserted. The
        placeholder ``'PEAK_LOAD_HOUSEHOLD'`` in the ``peak_load`` column is replaced by the
        configured value. Building classifications deliberately do not constrain this table.

        Args:
            consumer_categories: Category table, normally ``CONSUMER_CATEGORIES``.

        Raises:
            psycopg2.Error: If the synchronisation fails; the transaction is rolled back first.
        """
        df = consumer_categories.copy()

        if 'peak_load' in df.columns:
            s = df['peak_load']
            mask = s == 'PEAK_LOAD_HOUSEHOLD'
            if mask.any():
                s = s.where(~mask, PEAK_LOAD_HOUSEHOLD)
            df['peak_load'] = s

        # Expected target table columns
        expected_cols = ["consumer_category_id", "definition", "peak_load", "yearly_consumption", "peak_load_per_m2",
                         "yearly_consumption_per_m2", "sim_factor"]
        for col in expected_cols:
            if col not in df.columns:
                df[col] = None
        df = df[expected_cols]

        # Convert numeric columns
        numeric_cols = ['peak_load', 'yearly_consumption', 'peak_load_per_m2', 'yearly_consumption_per_m2',
                        'sim_factor']
        for col in numeric_cols:
            df[col] = pd.to_numeric(df[col], errors='coerce')

        df = df.where(pd.notna(df), None)

        rows = df.to_dict(orient='records')
        configured_ids = [int(row["consumer_category_id"]) for row in rows]
        upsert_sql = ("""
                      INSERT INTO pylovo.consumer_categories
                      (consumer_category_id, definition, peak_load, yearly_consumption, peak_load_per_m2,
                       yearly_consumption_per_m2, sim_factor)
                      VALUES (%(consumer_category_id)s, %(definition)s, %(peak_load)s, %(yearly_consumption)s,
                              %(peak_load_per_m2)s, %(yearly_consumption_per_m2)s, %(sim_factor)s)
                      ON CONFLICT (consumer_category_id) DO UPDATE SET definition                = EXCLUDED.definition,
                                                                       peak_load                 = EXCLUDED.peak_load,
                                                                       yearly_consumption        = EXCLUDED.yearly_consumption,
                                                                       peak_load_per_m2          = EXCLUDED.peak_load_per_m2,
                                                                       yearly_consumption_per_m2 = EXCLUDED.yearly_consumption_per_m2,
                                                                       sim_factor                = EXCLUDED.sim_factor;""")
        try:
            self.cur.execute(
                "DELETE FROM pylovo.consumer_categories "
                "WHERE NOT (consumer_category_id = ANY(%s));",
                (configured_ids,),
            )
            self.cur.executemany(upsert_sql, rows)
            self.conn.commit()
            self.logger.info(f"Synchronized consumer_categories rows: {len(rows)}")
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Failed synchronizing consumer_categories: {e}")
            raise

    def postcode_exists_locally(self, plz: int) -> bool:
        """Return whether the PLZ exists in the local ``postcode`` table."""
        self.cur.execute(
            "SELECT 1 FROM pylovo.postcode WHERE plz = %(p)s LIMIT 1;", {"p": plz}
        )
        return self.cur.fetchone() is not None

    def insert_postcode(self, postcode_row: tuple) -> None:
        """Insert one postcode into the local ``postcode`` table unless it exists.

        Args:
            postcode_row: ``(plz, note, qkm, population, geom)`` as returned by
                ``InfdbClient.fetch_postcode_from_infdb``; ``geom`` is transformed to ``TARGET_EPSG``.
        """
        query = f"""
            INSERT INTO pylovo.postcode (plz, note, qkm, population, geom)
            VALUES (%s, %s, %s, %s, ST_Transform(%s::geometry, {TARGET_EPSG}))
            ON CONFLICT (plz) DO NOTHING;"""
        self.cur.execute(query, postcode_row)

    def copy_postcode_result_table(self, plz: int) -> None:
        """Copy the PLZ polygon from ``postcode`` into ``postcode_result`` for the active version.

        Does nothing if the row already exists.

        Args:
            plz: Postcode to copy.
        """
        query = """INSERT INTO pylovo.postcode_result (version_id, postcode_result_plz, geom)
                   SELECT %(v)s as version_id, plz, geom
                   FROM pylovo.postcode
                   WHERE plz = %(p)s
                   LIMIT 1
                   ON CONFLICT (version_id,postcode_result_plz) DO NOTHING;"""

        self.cur.execute(query, {"v": VERSION_ID, "p": plz})

    def set_residential_buildings_table(self, plz: int):
        """Fill ``buildings_tem`` with the residential buildings of the PLZ (file-based input).

        Used when ``USE_INFDB=False``: reads the imported shapefile table ``res`` and keeps the
        buildings whose centroid lies inside the PLZ polygon of ``postcode_result``.

        Args:
            plz: Postcode to fill.
        """
        query = """INSERT INTO buildings_tem (objectid, floor_area, building_use, type, geom, centroid, floor_number)
                   SELECT osm_id, area, building_t, building_t, geom, ST_Centroid(geom), floors::int
                   FROM res
                   WHERE ST_Contains((SELECT post.geom
                                      FROM pylovo.postcode_result as post
                                      WHERE version_id = %(v)s
                                        AND postcode_result_plz = %(plz)s
                                      LIMIT 1), ST_Centroid(res.geom));
        UPDATE buildings_tem
        SET plz = %(plz)s
        WHERE plz ISNULL;"""
        self.cur.execute(query, {"v": VERSION_ID, "plz": plz})

    def set_buildings_table(self, buildings_data: list[tuple], plz: int = None) -> None:
        """Insert InfDB buildings into ``buildings_tem`` (``USE_INFDB=True``).

        Args:
            buildings_data: Rows in the column order of ``InfdbClient.fetch_buildings_from_infdb``
                (``id``, ``feature_id``, ``objectid``, ..., ``geom``, ``centroid``, ...,
                ``assigned_way_id``, ``type``); both geometries are transformed to ``TARGET_EPSG``.
            plz: Unused; ``plz`` is set later by ``set_buildings_tem_plz``.
        """
        insert_query = f"""
            INSERT INTO buildings_tem
            (id, feature_id, objectid, height, floor_area, floor_number, residential_floor_area,
             nonresidential_floor_area, nonresidential_use, mix_score, mix_rule, mix_confidence,
             building_use, building_use_id,
             building_type, occupants, households, construction_year, postcode, address_street_id, street,
             house_number, geom, centroid, gemeindeschluessel, changelog_id, assigned_way_id, type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    ST_Transform(%s::geometry, {TARGET_EPSG}), ST_Transform(%s::geometry, {TARGET_EPSG}),
                    %s, %s, %s, %s)
        """
        execute_batch(self.cur, insert_query, buildings_data, page_size=500)
        # self.conn.commit() only for debugging

    def set_other_buildings_table(self, plz: int):
        """Add the commercial and public buildings of the PLZ to ``buildings_tem`` (file-based input).

        Used when ``USE_INFDB=False``: reads the imported shapefile table ``oth``. Buildings
        without a floor count get one floor.

        Args:
            plz: Postcode to fill.
        """
        query = """INSERT INTO buildings_tem(objectid, floor_area, building_use, type, geom, centroid)
                   SELECT osm_id, area, use, use, geom, ST_Centroid(geom)
                   FROM oth AS o
                   WHERE o.use in ('Commercial', 'Public')
                     AND ST_Contains((SELECT post.geom
                                      FROM pylovo.postcode_result as post
                                      WHERE version_id = %(v)s
                                        AND postcode_result_plz = %(plz)s), ST_Centroid(o.geom));;
        UPDATE buildings_tem
        SET plz = %(plz)s
        WHERE plz ISNULL;
        UPDATE buildings_tem
        SET floor_number = 1
        WHERE floor_number ISNULL;"""
        self.cur.execute(query, {"v": VERSION_ID, "plz": plz})

    def remove_duplicate_buildings(self):
        """Delete buildings without geometry or objectid, and ``*copy*`` duplicates of identical geometries."""
        remove_query = """DELETE
                          FROM buildings_tem
                          WHERE geom ISNULL;"""
        self.cur.execute(remove_query)

        remove_noid_building = """DELETE
                                  FROM buildings_tem
                                  WHERE objectid ISNULL;"""
        self.cur.execute(remove_noid_building)

        query = """DELETE
                   FROM buildings_tem
                   WHERE geom IN
                         (SELECT geom FROM buildings_tem GROUP BY geom HAVING count(*) > 1)
                     AND objectid LIKE '%copy%';"""
        self.cur.execute(query)

    def calculate_house_distance_metric(self, plz: int, k_nearest: int = 4) -> float:
        """Store the mean distance between buildings in ``postcode_result.house_distance``.

        The metric is the mean centroid distance of every building in ``buildings_tem`` to its
        ``k_nearest`` nearest neighbours (fewer if the PLZ has fewer buildings).

        Args:
            plz: Postcode of the working tables.
            k_nearest: Number of neighbours per building.

        Returns:
            The mean distance in metres.

        Raises:
            ValueError: If ``buildings_tem`` holds fewer than two buildings.
        """
        from scipy.spatial import cKDTree
        self.cur.execute("SELECT ST_X(centroid), ST_Y(centroid) FROM buildings_tem WHERE centroid IS NOT NULL")
        points = np.asarray(self.cur.fetchall(), dtype=float)
        if len(points) < 2:
            raise ValueError("House distance calculation needs at least two buildings in buildings_tem.")
        k = min(k_nearest, len(points) - 1)
        # The first neighbour of every point is the point itself at distance zero.
        distances, _ = cKDTree(points).query(points, k=k + 1)
        avg_dis = float(distances[:, 1:].mean())
        update_query = """
            UPDATE pylovo.postcode_result
            SET house_distance = %(avg)s
            WHERE version_id = %(v)s
              AND postcode_result_plz = %(p)s;"""
        self.cur.execute(update_query, {"avg": avg_dis, "v": VERSION_ID, "p": plz})
        return avg_dis

    def calculate_avg_households_per_building(self, plz: int) -> float:
        """Store the mean household count of residential buildings in ``postcode_result``.

        Args:
            plz: Postcode of the working tables.

        Returns:
            The mean number of households per residential building.

        Raises:
            ValueError: If no residential building has a household count.
        """
        avg_query = """
            SELECT AVG(households)::DOUBLE PRECISION
            FROM buildings_tem
            WHERE households IS NOT NULL
              AND (
                  COALESCE(residential_floor_area, 0) > 0
                  OR type IN ('SFH','TH','MFH','AB')
              );"""
        self.cur.execute(avg_query)
        avg_val = self.cur.fetchone()[0]
        if avg_val is None:
            raise ValueError(f"No residential buildings with household data for ZIP {plz}.")
        update_query = """
            UPDATE pylovo.postcode_result
            SET avg_households_per_building = %(avg)s
            WHERE version_id = %(v)s
              AND postcode_result_plz = %(p)s;"""
        self.cur.execute(update_query, {"avg": avg_val, "v": VERSION_ID, "p": plz})
        return float(avg_val)

    def set_settlement_type_per_plz(
        self,
        plz: int,
        settlement_type_thresholds: dict | None = None,
    ) -> int:
        """Classify the PLZ as rural (1), semi-urban (2) or urban (3) and store it in ``postcode_result``.

        Both metrics of ``postcode_result`` are normalised to [0, 1], where 1 means urban:

        1. ``avg_households_per_building``: 0 at or below ``rural_max_households``, 1 at or above
           ``urban_min_households``, linear in between.
        2. ``house_distance`` (inverted): 0 at or above ``rural_min_distance``, 1 at or below
           ``urban_max_distance``, linear in between.

        The score ``0.5 * households + 0.5 * distance`` is binned: below 1/3 -> 1, below 2/3 -> 2,
        otherwise 3.

        Args:
            plz: Postcode to classify.
            settlement_type_thresholds: Dict with ``rural_max_households``, ``urban_min_households``,
                ``rural_min_distance`` and ``urban_max_distance``. Defaults to ``RURAL_MAX_HOUSEHOLDS``,
                ``URBAN_MIN_HOUSEHOLDS``, ``RURAL_MIN_BUILDING_DISTANCE`` and ``URBAN_MAX_BUILDING_DISTANCE``.

        Returns:
            The settlement type.

        Raises:
            ValueError: If one of the two metrics is not set yet.
        """
        if settlement_type_thresholds is None:
            settlement_type_thresholds = {
                "rural_max_households": RURAL_MAX_HOUSEHOLDS,
                "urban_min_households": URBAN_MIN_HOUSEHOLDS,
                "rural_min_distance": RURAL_MIN_BUILDING_DISTANCE,
                "urban_max_distance": URBAN_MAX_BUILDING_DISTANCE,
            }
        fetch_query = """
            SELECT avg_households_per_building, house_distance
            FROM pylovo.postcode_result
            WHERE version_id = %(v)s AND postcode_result_plz = %(p)s;"""
        self.cur.execute(fetch_query, {"v": VERSION_ID, "p": plz})
        row = self.cur.fetchone()
        if not row or row[0] is None or row[1] is None:
            raise ValueError("Both metrics must be set before classification.")
        avg_households, house_distance = float(row[0]), float(row[1])

        # Normalization households
        denom_hh = max(1e-9, (settlement_type_thresholds["urban_min_households"] - settlement_type_thresholds["rural_max_households"]))
        hh_norm = (avg_households - settlement_type_thresholds["rural_max_households"]) / denom_hh
        hh_norm = min(1.0, max(0.0, hh_norm))
        # Normalization distances (inverted)
        denom_dist = max(1e-9, (settlement_type_thresholds["rural_min_distance"] - settlement_type_thresholds["urban_max_distance"]))
        dist_norm_raw = (house_distance - settlement_type_thresholds["urban_max_distance"]) / denom_dist
        dist_norm = 1.0 - min(1.0, max(0.0, dist_norm_raw))

        score = 0.5 * hh_norm + 0.5 * dist_norm
        if score >= 2/3:
            settlement_type = 3
        elif score >= 1/3:
            settlement_type = 2
        else:
            settlement_type = 1

        update_query = """
            UPDATE pylovo.postcode_result
            SET settlement_type = %(stype)s
            WHERE version_id = %(v)s AND postcode_result_plz = %(p)s;"""
        self.cur.execute(update_query, {"stype": settlement_type, "v": VERSION_ID, "p": plz})
        return settlement_type

    @staticmethod
    def _household_fallback_parameters() -> dict:
        """Return assumptions used only for missing InfDB household counts."""
        return {
            "SFH": {"fixed_households": 1},
            "TH": {"fixed_households": 1},
            "MFH": {"minimum_households": 2, "residential_area_per_household_m2": 181},
            "untyped_residential": {"minimum_households": 1, "residential_area_per_household_m2": 181},
            "AB": {"minimum_households": 5, "residential_area_per_household_m2": 146},
        }

    def set_building_peak_load(self) -> int:
        """Calculate the peak load of every building in ``buildings_tem`` and drop unloaded ones.

        Validates the source floor-area split, fills missing household counts from the
        ``_household_fallback_parameters`` assumptions, derives residential and non-residential
        floor areas and peak loads from ``consumer_categories``, and sums them into
        ``peak_load_in_kw`` (residential only if ``RESIDENTIAL_ONLY_GENERATION``).

        Returns:
            Number of buildings deleted because their peak load is zero.

        Raises:
            ValueError: If the source areas or the calculated load components are inconsistent.
        """
        self.cur.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE residential_floor_area < 0 OR nonresidential_floor_area < 0
                ),
                COUNT(*) FILTER (
                    WHERE (residential_floor_area IS NULL)
                       <> (nonresidential_floor_area IS NULL)
                ),
                COUNT(*) FILTER (
                    WHERE residential_floor_area IS NOT NULL
                      AND nonresidential_floor_area IS NOT NULL
                      AND floor_area IS NOT NULL
                      AND floor_number IS NOT NULL
                      AND ABS(
                          residential_floor_area + nonresidential_floor_area
                          - floor_area * floor_number
                      ) > 0.01
                )
            FROM buildings_tem;
            """
        )
        negative_areas, incomplete_splits, inconsistent_splits = self.cur.fetchone()
        if any((negative_areas, incomplete_splits, inconsistent_splits)):
            raise ValueError(
                "Invalid source building area components: "
                f"negative={negative_areas}, incomplete={incomplete_splits}, "
                f"inconsistent with gross floor area={inconsistent_splits}."
            )

        household_fallback = self._household_fallback_parameters()
        query = """
                UPDATE buildings_tem
                SET floor_area = ST_Area(geom)
                WHERE floor_area IS NULL;

                UPDATE buildings_tem
                -- Preserve InfDB household counts and fill only missing values.
                SET households = (
                    CASE
                    WHEN type = 'SFH' THEN %(sfh_households)s
                    WHEN type = 'TH' THEN %(th_households)s
                    WHEN type = 'MFH' THEN GREATEST(
                        %(mfh_minimum_households)s,
                        ROUND(
                            COALESCE(residential_floor_area, floor_area * COALESCE(floor_number, 1))
                            / %(mfh_area_per_household_m2)s
                        )::integer
                    )
                    WHEN type IN ('Residential', 'Mixed')
                         AND COALESCE(residential_floor_area, 0) > 0 THEN GREATEST(
                        %(untyped_residential_minimum_households)s,
                        ROUND(
                            residential_floor_area
                            / %(untyped_residential_area_per_household_m2)s
                        )::integer
                    )
                    WHEN type = 'AB' THEN GREATEST(
                        %(ab_minimum_households)s,
                        ROUND(
                            COALESCE(residential_floor_area, floor_area * COALESCE(floor_number, 1))
                            / %(ab_area_per_household_m2)s
                        )::integer
                    )
                    WHEN type IN ('Commercial', 'Public') THEN 1
                    ELSE households
                    END
                )
                WHERE households IS NULL;

                UPDATE buildings_tem
                SET residential_floor_area = CASE
                        WHEN residential_floor_area IS NOT NULL THEN residential_floor_area
                        WHEN type IN ('SFH', 'TH', 'MFH', 'AB')
                            THEN floor_area * COALESCE(floor_number, 1)
                        ELSE 0
                    END,
                    nonresidential_floor_area = CASE
                        WHEN nonresidential_floor_area IS NOT NULL THEN nonresidential_floor_area
                        WHEN type IN ('Commercial', 'Public')
                            THEN floor_area * COALESCE(floor_number, 1)
                        ELSE 0
                    END;

                UPDATE buildings_tem
                SET nonresidential_use = CASE
                        WHEN COALESCE(nonresidential_floor_area, 0) <= 0 THEN NULL
                        WHEN nonresidential_use IN ('Commercial', 'Public') THEN nonresidential_use
                        WHEN type IN ('Commercial', 'Public') THEN type
                        WHEN building_use = 'Residential' THEN 'Commercial'
                        ELSE nonresidential_use
                    END;

                UPDATE buildings_tem b
                SET residential_peak_load_in_kw = CASE
                        WHEN COALESCE(b.residential_floor_area, 0) > 0 THEN b.households * (
                            SELECT peak_load
                            FROM pylovo.consumer_categories
                            WHERE definition = 'Residential'
                        )
                        ELSE 0
                    END,
                    nonresidential_peak_load_in_kw = CASE
                        WHEN COALESCE(b.nonresidential_floor_area, 0) > 0 THEN b.nonresidential_floor_area * (
                            SELECT peak_load_per_m2
                            FROM pylovo.consumer_categories
                            WHERE definition = b.nonresidential_use
                        ) / 1000
                        ELSE 0
                    END;

                UPDATE buildings_tem
                SET peak_load_in_kw = COALESCE(residential_peak_load_in_kw, 0)
                    + CASE WHEN %(include_nonresidential)s
                           THEN COALESCE(nonresidential_peak_load_in_kw, 0)
                           ELSE 0 END;"""
        self.cur.execute(
            query,
            {
                "include_nonresidential": not RESIDENTIAL_ONLY_GENERATION,
                "sfh_households": household_fallback["SFH"]["fixed_households"],
                "th_households": household_fallback["TH"]["fixed_households"],
                "mfh_minimum_households": household_fallback["MFH"]["minimum_households"],
                "mfh_area_per_household_m2": household_fallback["MFH"][
                    "residential_area_per_household_m2"
                ],
                "untyped_residential_minimum_households": household_fallback["untyped_residential"]["minimum_households"],
                "untyped_residential_area_per_household_m2": household_fallback["untyped_residential"]["residential_area_per_household_m2"],
                "ab_minimum_households": household_fallback["AB"]["minimum_households"],
                "ab_area_per_household_m2": household_fallback["AB"][
                    "residential_area_per_household_m2"
                ],
            },
        )

        self.cur.execute(
            """
            SELECT
                COUNT(*) FILTER (
                    WHERE residential_floor_area > 0
                      AND (households IS NULL OR households <= 0)
                ),
                COUNT(*) FILTER (
                    WHERE nonresidential_floor_area > 0
                      AND (
                          nonresidential_use IS NULL
                          OR nonresidential_use NOT IN ('Commercial', 'Public')
                      )
                ),
                COUNT(*) FILTER (
                    WHERE residential_peak_load_in_kw IS NULL
                       OR nonresidential_peak_load_in_kw IS NULL
                )
            FROM buildings_tem;
            """
        )
        invalid_households, invalid_nonresidential_use, missing_peak = self.cur.fetchone()
        if any((invalid_households, invalid_nonresidential_use, missing_peak)):
            raise ValueError(
                "Invalid calculated building load components: "
                f"invalid residential households={invalid_households}, "
                f"invalid non-residential uses={invalid_nonresidential_use}, "
                f"missing component peaks={missing_peak}."
            )

        count_query = """SELECT COUNT(*)
                          FROM buildings_tem
                          WHERE peak_load_in_kw = 0;"""
        self.cur.execute(count_query)
        count = self.cur.fetchone()[0]

        delete_query = """DELETE
                          FROM buildings_tem
                          WHERE peak_load_in_kw = 0;"""
        self.cur.execute(delete_query)

        return count

    def update_too_large_consumers_to_zero(self) -> int:
        """Exclude non-residential loads above ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` from the LV model.

        Such components are flagged ``nonresidential_mv_direct`` (assumed to be supplied from the
        MV grid) and removed from ``peak_load_in_kw``; the residential part of the building stays.

        Returns:
            Number of buildings with an MV-direct component.
        """
        query = """
                UPDATE buildings_tem
                SET nonresidential_mv_direct = (
                        %(include_nonresidential)s
                        AND nonresidential_peak_load_in_kw > %(threshold)s
                    ),
                    peak_load_in_kw = COALESCE(residential_peak_load_in_kw, 0)
                        + CASE
                            WHEN %(include_nonresidential)s
                             AND nonresidential_peak_load_in_kw <= %(threshold)s
                                THEN COALESCE(nonresidential_peak_load_in_kw, 0)
                            ELSE 0
                          END;
                SELECT COUNT(*)
                FROM buildings_tem
                WHERE nonresidential_mv_direct;"""
        self.cur.execute(
            query,
            {
                "threshold": MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW,
                "include_nonresidential": not RESIDENTIAL_ONLY_GENERATION,
            },
        )
        too_large = self.cur.fetchone()[0]

        return too_large

    def set_buildings_tem_plz(self, plz: int) -> None:
        """Set ``plz`` on all rows of ``buildings_tem`` that do not have one yet."""
        query = """UPDATE buildings_tem
                   SET plz = %(p)s
                   WHERE plz ISNULL;"""
        self.cur.execute(query, {"p": plz})


    def upsert_lod2_transformer_stations(self, transformer_buildings: list[tuple]) -> int:
        """Add LoD2 transformer-station buildings to the raw transformer candidates.

        A building within 3 m of an existing candidate (or intersecting it) marks the nearest
        candidate as LoD2-confirmed; otherwise its centroid is inserted as ``lod2/<objectid>``.

        Args:
            transformer_buildings: ``(objectid, geom, centroid)`` rows of
                ``InfdbClient.fetch_transformer_station_buildings_from_infdb``.

        Returns:
            Number of processed buildings.
        """
        if not transformer_buildings:
            return 0

        insert_query = f"""
            WITH candidate AS (
                SELECT
                    %(objectid)s::text AS objectid,
                    ST_Transform(%(geom)s::geometry, {TARGET_EPSG}) AS building_geom,
                    ST_Transform(%(centroid)s::geometry, {TARGET_EPSG}) AS centroid_geom
            ),
            matched AS (
                SELECT t.osm_id
                FROM pylovo.transformers t
                JOIN candidate c
                  ON ST_Intersects(t.geom, c.building_geom)
                  OR ST_DWithin(t.geom, c.centroid_geom, 3.0)
                ORDER BY t.geom <-> c.centroid_geom
                LIMIT 1
            ),
            updated AS (
                UPDATE pylovo.transformers t
                SET lod2 = true,
                    lod2_objectid = COALESCE(t.lod2_objectid, (SELECT objectid FROM candidate))
                FROM matched
                WHERE t.osm_id = matched.osm_id
                RETURNING t.osm_id
            )
            INSERT INTO pylovo.transformers (
                osm_id,
                area,
                type,
                transformer_rated_power,
                geom_type,
                within_shopping,
                osm,
                lod2,
                lod2_objectid,
                geom
            )
            SELECT
                'lod2/' || objectid,
                ST_Area(building_geom),
                'Transformer',
                NULL,
                'lod2_building_centroid',
                false,
                false,
                true,
                objectid,
                ST_Multi(centroid_geom)
            FROM candidate
            WHERE NOT EXISTS (SELECT 1 FROM updated)
            ON CONFLICT (osm_id) DO UPDATE SET
                lod2 = true,
                lod2_objectid = EXCLUDED.lod2_objectid;
        """
        rows = [
            {"objectid": row[0], "geom": row[1], "centroid": row[2]}
            for row in transformer_buildings
        ]
        self.cur.executemany(insert_query, rows)
        return len(rows)

    def remove_non_residential_buildings_overlapping_transformers(self, include_dso: bool = False) -> int:
        """Remove non-residential consumer buildings that overlap transformer candidates.

        Imported DSO stations count only when they are in use
        (`include_dso`): they stay in `pylovo.transformers` after the run that
        imported them, and must not remove buildings from a run that ignores them.

        Args:
            include_dso: Whether imported DSO stations are used in this run.

        Returns:
            Number of deleted buildings.
        """
        query = f"""
            DELETE FROM buildings_tem b
            WHERE (b.type IS NULL OR b.type NOT IN ('SFH', 'MFH', 'TH', 'AB'))
              AND COALESCE(b.residential_floor_area, 0) <= 0
              AND COALESCE(b.type, '') != 'Transformer'
              AND EXISTS (
                  SELECT 1
                  FROM pylovo.transformers t
                  WHERE (ST_Intersects(t.geom, b.geom) OR ST_Within(t.geom, b.geom))
                    AND (%(include_dso)s OR NOT {_IS_DSO_TRANSFORMER_SQL})
              );
        """
        self.cur.execute(query, {"include_dso": include_dso})
        return self.cur.rowcount

    def remove_non_residential_buildings_from_buildings_tem(self) -> int:
        """Delete buildings without residential use from ``buildings_tem`` (``RESIDENTIAL_ONLY_GENERATION``).

        Returns:
            Number of deleted buildings.
        """
        query = """
            DELETE FROM buildings_tem
            WHERE COALESCE(residential_floor_area, 0) <= 0
              AND (type IS NULL OR type NOT IN ('SFH', 'MFH', 'TH', 'AB'));
        """
        self.cur.execute(query)
        return self.cur.rowcount

    def insert_transformers(self, plz: int, include_dso: bool = False, include_open: bool = True,
                            include_manual: bool = False) -> None:
        """Add the existing transformers inside the PLZ to ``buildings_tem`` as ``Transformer`` rows.

        The new rows get ``peak_load_in_kw = -1``, which marks transformer rows throughout the
        generation. They are recognised by their still-empty columns (``plz``, ``centroid``,
        ``type``, ``peak_load_in_kw``), so this must run after the buildings are complete.

        Args:
            plz: Postcode.
            include_dso: Include imported DSO transformer positions.
            include_open: Include all other (OSM, LoD2 and manual) transformer positions.
            include_manual: Include the manual (UI) positions, also without ``include_open``.
        """
        insert_query = f"""
                       INSERT INTO buildings_tem (objectid, geom)
                       SELECT osm_id, geom
                       FROM pylovo.transformers as t
                       WHERE ST_Within(t.geom, (SELECT geom
                                                FROM pylovo.postcode_result
                                                WHERE postcode_result_plz = %(p)s
                                                  AND version_id = %(v)s))
                         AND {SOURCE_ENABLED_SQL};
                       UPDATE buildings_tem
                       SET plz = %(p)s
                       WHERE plz ISNULL;
                       UPDATE buildings_tem
                       SET centroid = ST_Centroid(geom)
                       WHERE centroid ISNULL;
                       UPDATE buildings_tem
                       SET building_use = 'Transformer', type = 'Transformer'
                       WHERE type ISNULL;
                       UPDATE buildings_tem
                       SET peak_load_in_kw = -1
                       WHERE peak_load_in_kw ISNULL;"""
        self.cur.execute(insert_query, {"p": plz, "v": VERSION_ID,
                                        **source_params(include_dso, include_open, include_manual)})

    def remove_transformer_evidence_buildings_from_buildings_tem(self, include_dso: bool = False, include_open: bool = True,
                                                                 include_manual: bool = False) -> int:
        """Remove load-building rows that are used as transformer evidence.

        LoD2 transformer-station buildings can otherwise remain as Public or
        Commercial consumers at the same vertex as the transformer.  That creates
        artificial service cables routed back into the transformer node.

        A building counts as evidence if it is a LoD2 station (``building_use_id`` 31001_2523,
        only with ``include_open``), if a used transformer carries its objectid, or if it is a
        non-residential building that intersects a used transformer.

        Args:
            include_dso: Whether imported DSO stations are used in this run.
            include_open: Whether all other transformer positions are used in this run.
            include_manual: Whether the manual (UI) positions are used in this run.

        Returns:
            Number of deleted buildings.
        """
        query = f"""
            WITH transformer_buildings AS (
                SELECT DISTINCT b.objectid, b.plz
                FROM buildings_tem b
                WHERE %(include_open)s
                  AND b.type != 'Transformer'
                  AND b.building_use_id = '31001_2523'

                UNION

                SELECT DISTINCT b.objectid, b.plz
                FROM buildings_tem b
                JOIN pylovo.transformers t
                  ON (
                      t.osm_id = b.objectid
                      OR t.osm_id = CONCAT('lod2/', b.objectid)
                      OR (
                          b.type NOT IN ('SFH', 'MFH', 'AB', 'TH', 'Transformer')
                          AND COALESCE(b.residential_floor_area, 0) <= 0
                          AND b.geom IS NOT NULL
                          AND ST_Intersects(b.geom, t.geom)
                      )
                  )
                WHERE b.type != 'Transformer'
                  AND {SOURCE_ENABLED_SQL}
                  AND (
                      t.osm_id = b.objectid
                      OR t.osm_id = CONCAT('lod2/', b.objectid)
                      OR (
                          b.type NOT IN ('SFH', 'MFH', 'AB', 'TH', 'Transformer')
                          AND COALESCE(b.residential_floor_area, 0) <= 0
                      )
                  )
            ), deleted AS (
                DELETE FROM buildings_tem b
                USING transformer_buildings tb
                WHERE b.objectid = tb.objectid
                  AND b.plz IS NOT DISTINCT FROM tb.plz
                RETURNING 1
            )
            SELECT COUNT(*) FROM deleted;
        """
        self.cur.execute(query, source_params(include_dso, include_open, include_manual))
        return int(self.cur.fetchone()[0])


    def count_indoor_transformers(self) -> None:
        """Log (debug level) how many transformer rows ``drop_indoor_transformers`` will delete."""
        query = """WITH union_table (ungeom) AS
                                (SELECT ST_Union(geom) FROM buildings_tem WHERE peak_load_in_kw = 0)
                   SELECT COUNT(*)
                   FROM buildings_tem
                   WHERE ST_Within(centroid, (SELECT ungeom FROM union_table))
                     AND type = 'Transformer';"""
        self.cur.execute(query)
        count = self.cur.fetchone()[0]
        self.logger.debug(f"{count} indoor transformers will be deleted")

    def drop_indoor_transformers(self) -> None:
        """Delete transformer rows whose centroid lies inside a zero-load building.

        Buildings without load were deleted by ``set_building_peak_load``; the zero-load rows
        left at this point are buildings whose whole load was moved to the MV grid by
        ``update_too_large_consumers_to_zero``.
        """
        query = """WITH union_table (ungeom) AS
                                (SELECT ST_Union(geom) FROM buildings_tem WHERE peak_load_in_kw = 0)
                   DELETE
                   FROM buildings_tem
                   WHERE ST_Within(centroid, (SELECT ungeom FROM union_table))
                     AND type = 'Transformer';"""
        self.cur.execute(query)   

    def set_ways_tem_table_infdb(self, ways_data: list[tuple], plz: int = None) -> int:
        """Insert the InfDB ways into ``ways_tem`` (``USE_INFDB=True``).

        Args:
            ways_data: ``(clazz, source, target, cost, reverse_cost, geom, way_id)`` rows of
                ``InfdbClient.fetch_ways_from_infdb``; ``geom`` is transformed to ``TARGET_EPSG``.
            plz: Unused.

        Returns:
            Number of rows in ``ways_tem`` afterwards.

        Raises:
            ValueError: If ``ways_data`` is empty.
        """
        if not ways_data:
            raise ValueError("No rows to insert into ways_tem")

        insert_query = f"""
            INSERT INTO ways_tem
            (clazz, source, target, cost, reverse_cost, geom, way_id)
            VALUES (%s, %s, %s, %s, %s, ST_Transform(%s::geometry, {TARGET_EPSG}), %s)
        """
        execute_batch(self.cur, insert_query, ways_data, page_size=500)
        self.cur.execute("SELECT COUNT(*) FROM ways_tem")
        return self.cur.fetchone()[0]

    def set_ways_tem_table(self, plz: int) -> int:
        """Copy the ways that intersect the PLZ polygon from ``ways`` into ``ways_tem`` (file-based input).

        Used when ``USE_INFDB=False``; ``pylovo-setup`` fills ``ways`` from the osm2po SQL file.

        Args:
            plz: Postcode.

        Returns:
            Number of rows in ``ways_tem``.

        Raises:
            ValueError: If no way intersects the PLZ.
        """
        query = """INSERT INTO ways_tem
                   SELECT *
                   FROM pylovo.ways AS w
                   WHERE ST_Intersects(w.geom, (SELECT geom
                                                FROM pylovo.postcode_result
                                                WHERE version_id = %(v)s
                                                  AND postcode_result_plz = %(p)s));
        SELECT COUNT(*)
        FROM ways_tem;"""
        self.cur.execute(query, {"v": VERSION_ID, "p": plz})
        count = self.cur.fetchone()[0]

        if count == 0:
            raise ValueError(f"Ways table is empty for the given plz: {plz}")

        return count

    def index_and_analyze_staging(self, plz: int) -> None:
        """Index loaded session-local roads before nearest-road searches."""
        roads = sql.Identifier("pg_temp", plz_table_name("ways_tem", plz))
        buildings = sql.Identifier("pg_temp", plz_table_name("buildings_tem", plz))
        self.cur.execute(sql.SQL("CREATE INDEX ON {} USING gist (geom)").format(roads))
        self.cur.execute(sql.SQL("CREATE INDEX ON {} (way_id)").format(roads))
        self.cur.execute(sql.SQL("ANALYZE {}").format(roads))
        self.cur.execute(sql.SQL("ANALYZE {}").format(buildings))

    def preprocess_ways(self) -> None:
        """Connect buildings and transformers to the street network in ``ways_tem``.

        Calls the SQL functions of ``ways_preprocessing_functions`` (loaded by ``pylovo-setup``).

        With ``USE_INFDB=True``, InfDB already delivers segmented ways and the building
        connection lines, so only ``generate_transformer_to_way_connections_infdb()`` runs: it
        connects the transformer rows of ``buildings_tem`` to their nearest way.

        With ``USE_INFDB=False`` two functions run in sequence:

        1. ``segment_intersecting_ways()`` splits crossing ways at their intersection point
           (via ``insert_way_segment()``).
        2. ``generate_building_to_way_connections()`` adds a connection line from every loaded
           building to its nearest way and splits that way at the connection point (via
           ``generate_building_way_connection_candidates()``, ``insert_way_segment()`` and
           ``split_way_at_connection_points()``).

        Running them in this order ensures that all intersecting ways are split before every
        building gets its own connection segment.
        """
        if USE_INFDB:
            self.cur.execute("SELECT generate_transformer_to_way_connections_infdb();")
        else:
            self.cur.execute("SELECT segment_intersecting_ways();")
            self.cur.execute("SELECT generate_building_to_way_connections();")

    def build_pgr_network_topology(self, plz: int) -> None:
        """Build the pgRouting topology of ``ways_tem_<plz>``.

        Uses the pgRouting 3.8+ workflow that replaces the deprecated ``pgr_createTopology()``:

        1. ``pgr_extractVertices()`` writes the distinct edge end points to
           ``ways_tem_<plz>_vertices_pgr``.
        2. ``source`` and ``target`` of every edge are set to the vertex at its start and end point.

        The vertices table is exposed to the session as the temporary view ``ways_tem_vertices_pgr``.

        Args:
            plz: Postcode of the working tables.
        """
        edge_name = plz_table_name("ways_tem", plz)
        vertices_name = f"{edge_name}_vertices_pgr"
        edges = sql.Identifier("pg_temp", edge_name)
        vertices = sql.Identifier("pg_temp", vertices_name)

        # Align endpoints before extracting vertices so pgRouting does not split components on
        # floating-point noise introduced by geometric preprocessing.
        self.cur.execute(sql.SQL("""
            UPDATE {edges}
            SET geom = ST_SnapToGrid(geom, 0.000001)
            WHERE geom IS NOT NULL;
        """).format(edges=edges))

        # Ensure source and target columns exist on the edge table
        # (required before pgr_extractVertices can work)
        self.cur.execute(sql.SQL("""
            ALTER TABLE {edges} ADD COLUMN IF NOT EXISTS source integer;
            ALTER TABLE {edges} ADD COLUMN IF NOT EXISTS target integer;
        """).format(edges=edges))

        self.cur.execute(sql.SQL("DROP TABLE IF EXISTS {};").format(vertices))

        # Step 1: pgr_extractVertices() takes the edge query as a text argument.
        edge_query = sql.SQL("SELECT way_id AS id, geom FROM {} ORDER BY way_id").format(edges)
        self.cur.execute(sql.SQL("""
            CREATE TABLE {vertices} AS
            SELECT id, geom
            FROM pgr_extractVertices({edge_query});
        """).format(vertices=vertices, edge_query=sql.Literal(edge_query.as_string(self.cur))))

        self.cur.execute(sql.SQL("ALTER TABLE {} ADD PRIMARY KEY (id);").format(vertices))
        self.cur.execute(sql.SQL("CREATE INDEX {} ON {} USING GIST (geom);").format(
            sql.Identifier(f"{vertices_name}_geom_idx"), vertices
        ))

        # Index source/target before filling them: an index built after these updates in the same
        # transaction (indcheckxmin) is unusable until commit, and generation runs in one transaction.
        self.cur.execute(sql.SQL("""
            CREATE INDEX IF NOT EXISTS {source_idx} ON {edges} (source);
            CREATE INDEX IF NOT EXISTS {target_idx} ON {edges} (target);
        """).format(
            source_idx=sql.Identifier(f"{edge_name}_source_idx"),
            target_idx=sql.Identifier(f"{edge_name}_target_idx"),
            edges=edges,
        ))

        # Step 2: link the start and end point of every edge to its vertex ID.
        self.cur.execute(sql.SQL("""
            UPDATE {edges} AS e
            SET source = v.id
            FROM {vertices} AS v
            WHERE ST_StartPoint(e.geom) = v.geom;
        """).format(edges=edges, vertices=vertices))
        self.cur.execute(sql.SQL("""
            UPDATE {edges} AS e
            SET target = v.id
            FROM {vertices} AS v
            WHERE ST_EndPoint(e.geom) = v.geom;
        """).format(edges=edges, vertices=vertices))

        # Temporary tables get no autovacuum; refresh the statistics taken before source/target existed.
        self.cur.execute(sql.SQL("ANALYZE {}; ANALYZE {};").format(edges, vertices))

        self.cur.execute(
            sql.SQL("CREATE TEMP VIEW ways_tem_vertices_pgr AS SELECT * FROM {}").format(vertices)
        )

    def update_ways_cost(self) -> None:
        """Set ``cost`` and ``reverse_cost`` of every way in ``ways_tem`` to its length in metres."""
        query = """UPDATE ways_tem
                   SET cost = ST_Length(geom);
        UPDATE ways_tem
        SET reverse_cost = cost;"""
        self.cur.execute(query)

    def set_vertice_id(self) -> int:
        """
        Updates buildings_tem with the vertice_id s from ways_tem_vertices_pgr
        :return:
        """
        query = """UPDATE buildings_tem b
                   SET vertice_id = (SELECT id
                                     FROM ways_tem_vertices_pgr AS v
                                     WHERE ST_DWithin(v.geom, b.centroid, 0.000001)
                                     ORDER BY ST_Distance(v.geom, b.centroid)
                                     LIMIT 1);"""
        self.cur.execute(query)

        query2 = """UPDATE buildings_tem b
                    SET connection_point = (SELECT target FROM ways_tem WHERE source = b.vertice_id LIMIT 1)
                    WHERE vertice_id IS NOT NULL
                      AND connection_point IS NULL;"""
        self.cur.execute(query2)

        agg_query = """UPDATE buildings_tem
                       SET agg_connection_point = connection_point
                       WHERE connection_point IS NOT NULL
                         AND agg_connection_point IS NULL;"""
        self.cur.execute(agg_query)

        count_query = """ SELECT COUNT(*)
                          FROM buildings_tem
                          WHERE connection_point IS NULL
                            AND peak_load_in_kw != 0;"""
        self.cur.execute(count_query)
        count = self.cur.fetchone()[0]

        delete_query = """DELETE
                          FROM buildings_tem
                          WHERE connection_point IS NULL
                            AND peak_load_in_kw != 0;"""
        self.cur.execute(delete_query)

        return count

    def aggregate_nearby_connection_points(
        self,
        radius_m: float,
        max_buildings: int,
    ) -> int:
        """Aggregate nearby street-side connection points into ``agg_connection_point``.

        This is a conservative open-data proxy for DSO house-connection
        aggregation. It writes the representative street-side node to
        ``buildings_tem.agg_connection_point``; the original connection point,
        building-side vertices, geometries, load values, and building rows remain
        unchanged. Clustering is partitioned by stable street identifiers when
        available. If no street information exists, nearby points are grouped
        geometrically without falling back to fragmented split-way IDs.

        A group is merged only if it has more than one connection point, at most
        ``max_buildings`` buildings and a diameter of at most ``radius_m``; its representative is
        the point closest to the group centroid.

        Args:
            radius_m: DBSCAN radius and maximum group diameter in metres.
            max_buildings: Maximum number of buildings of a merged group.

        Returns:
            Number of buildings whose ``agg_connection_point`` was changed.
        """
        query = """
            WITH building_points AS (
                SELECT
                    b.objectid,
                    b.connection_point,
                    COALESCE(
                        NULLIF(b.address_street_id::text, ''),
                        NULLIF(b.street, ''),
                        '__NO_STREET__'
                    ) AS street_key,
                    v.geom AS connection_geom
                FROM buildings_tem b
                JOIN ways_tem_vertices_pgr v ON v.id = b.connection_point
                WHERE b.peak_load_in_kw != 0
                  AND b.connection_point IS NOT NULL
            ), clustered AS (
                SELECT
                    objectid,
                    connection_point,
                    street_key,
                    connection_geom,
                    ST_ClusterDBSCAN(
                        connection_geom,
                        eps := %(radius_m)s,
                        minpoints := 1
                    ) OVER (PARTITION BY street_key) AS cluster_id
                FROM building_points
            ), cluster_stats AS (
                SELECT
                    street_key,
                    cluster_id,
                    COUNT(*) AS building_count,
                    COUNT(DISTINCT connection_point) AS connection_point_count,
                    ST_Centroid(ST_Collect(connection_geom)) AS cluster_centroid,
                    ST_MaxDistance(
                        ST_Collect(connection_geom),
                        ST_Collect(connection_geom)
                    ) AS cluster_diameter_m
                FROM clustered
                GROUP BY street_key, cluster_id
                HAVING COUNT(DISTINCT connection_point) > 1
                   AND COUNT(*) <= %(max_buildings)s
                   AND ST_MaxDistance(
                       ST_Collect(connection_geom),
                       ST_Collect(connection_geom)
                   ) <= %(radius_m)s
            ), representatives AS (
                SELECT street_key, cluster_id, connection_point AS representative_connection_point
                FROM (
                    SELECT
                        c.street_key,
                        c.cluster_id,
                        c.connection_point,
                        ROW_NUMBER() OVER (
                            PARTITION BY c.street_key, c.cluster_id
                            ORDER BY ST_Distance(c.connection_geom, cs.cluster_centroid), c.connection_point
                        ) AS rn
                    FROM clustered c
                    JOIN cluster_stats cs USING (street_key, cluster_id)
                ) ranked
                WHERE rn = 1
            ), mapping AS (
                SELECT DISTINCT
                    c.connection_point AS old_connection_point,
                    r.representative_connection_point
                FROM clustered c
                JOIN representatives r USING (street_key, cluster_id)
                WHERE c.connection_point != r.representative_connection_point
            ), updated AS (
                UPDATE buildings_tem b
                SET agg_connection_point = m.representative_connection_point
                FROM mapping m
                WHERE b.connection_point = m.old_connection_point
                  AND b.peak_load_in_kw != 0
                RETURNING 1
            )
            SELECT COUNT(*) FROM updated;
        """
        self.cur.execute(query, {"radius_m": radius_m, "max_buildings": max_buildings})
        return int(self.cur.fetchone()[0])

    def get_ags_log(self) -> pd.DataFrame:
        """Return the AGS log of the file-based building import.

        The log lists the official municipal keys (Amtlicher Gemeindeschlüssel) of the
        municipalities whose building shapefiles were already imported (``USE_INFDB=False``).

        Returns:
            DataFrame with the column ``ags``.
        """
        query = """SELECT *
                   FROM pylovo.ags_log;"""
        df_query = pd.read_sql_query(query, con=self.conn, )
        return df_query

    def write_ags_log(self, ags: int) -> None:
        """Record that the buildings of a municipality were imported, and commit.

        Args:
            ags: Official municipal key (Amtlicher Gemeindeschlüssel).
        """
        query = """INSERT INTO pylovo.ags_log (ags)
                   VALUES (%(a)s); """
        self.cur.execute(query, {"a": int(ags), })
        self.conn.commit()
