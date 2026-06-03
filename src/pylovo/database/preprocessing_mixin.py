import json
import warnings
from abc import ABC
import pandas as pd
import time

from pylovo.config_loader import *
from pylovo.database.base_mixin import BaseMixin

warnings.simplefilter(action='ignore', category=UserWarning)


class PreprocessingMixin(BaseMixin, ABC):
    def __init__(self):
        super().__init__()

    @staticmethod
    def _dataframe_records(df: pd.DataFrame) -> list[dict]:
        return json.loads(df.to_json(orient="records"))

    def _generation_parameters_snapshot(self) -> dict:
        return {
            "electrical_backend": ELECTRICAL_BACKEND,
            "load_calculation": {
                "peak_load_household": PEAK_LOAD_HOUSEHOLD,
                "sim_factor": SIM_FACTOR,
                "default_power_factor": DEFAULT_POWER_FACTOR,
                "consumer_categories": self._dataframe_records(CONSUMER_CATEGORIES),
            },
            "equipment_data": self._dataframe_records(CONFIG_EQUIPMENT_DATA),
            "cable_dimensioning": {
                "vn": VN,
                "v_band_low": V_BAND_LOW,
                "v_band_high": V_BAND_HIGH,
                "min_shared_prefix_length_m": MIN_SHARED_PREFIX_LENGTH_M,
                "small_load_threshold_kw": SMALL_LOAD_THRESHOLD_KW,
                "voltage_drop_small_load_percent_per_km": VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM,
                "voltage_drop_large_load_percent_per_km": VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM,
                "voltage_drop_distribution_percent": VOLTAGE_DROP_DISTRIBUTION_PERCENT,
            },
            "transformer_placement": {
                "rural_max_households": RURAL_MAX_HOUSEHOLDS,
                "urban_min_households": URBAN_MIN_HOUSEHOLDS,
                "rural_min_building_distance": RURAL_MIN_BUILDING_DISTANCE,
                "urban_max_building_distance": URBAN_MAX_BUILDING_DISTANCE,
                "transformer_mapping": TRANSFORMER_MAPPING,
                "max_brownfield_trafo_distance": MAX_BROWNFIELD_TRAFO_DISTANCE,
                "max_greenfield_trafo_distance": MAX_GREENFIELD_TRAFO_DISTANCE,
                "large_component_lower_bound": LARGE_COMPONENT_LOWER_BOUND,
                "large_component_divider": LARGE_COMPONENT_DIVIDER,
                "k_means_seed": K_MEANS_SEED,
            },
        }

    def insert_version_if_not_exists(self):
        """Insert version if it doesn't exist, with proper handling for concurrent access."""
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
            self.conn.commit()

            if self.cur.rowcount > 0:
                self.logger.info(f"Version: {VERSION_ID} (created or generation parameters backfilled)")
            else:
                self.logger.debug(f"Version: {VERSION_ID} (already exists)")

        except Exception as e:
            self.logger.error(f"Error inserting version {VERSION_ID}: {e}")
            self.conn.rollback()
            raise

    def insert_equipment_data_from_config(self, equipment_data: pd.DataFrame):
        """Populate equipment_data table from the combined config equipment DataFrame.
        Replaces former pandas.to_sql variant (replace) with conflict-safe inserts.
        Reasons:
        Strategy:
        - Map columns to expected structure
        - Fill missing columns with None
        - Cast values (numeric fields to Int, missing -> None)
        - ON CONFLICT (version_id, name) DO UPDATE for idempotency / update
        """
        df = equipment_data.copy()
        expected_cols = ["version_id", "name", "s_max_kva", "max_i_a", "r_mohm_per_km", "x_mohm_per_km",
                         "z_mohm_per_km", "cost_eur", "typ"]
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
                      (version_id, name, s_max_kva, max_i_a, r_mohm_per_km, x_mohm_per_km, z_mohm_per_km, cost_eur, typ)
                      VALUES (%(version_id)s, %(name)s, %(s_max_kva)s, %(max_i_a)s, %(r_mohm_per_km)s,
                              %(x_mohm_per_km)s, %(z_mohm_per_km)s, %(cost_eur)s, %(typ)s)
                      ON CONFLICT (version_id, name) DO UPDATE SET s_max_kva        = EXCLUDED.s_max_kva,
                                                                   max_i_a          = EXCLUDED.max_i_a,
                                                                   r_mohm_per_km    = EXCLUDED.r_mohm_per_km,
                                                                   x_mohm_per_km    = EXCLUDED.x_mohm_per_km,
                                                                   z_mohm_per_km    = EXCLUDED.z_mohm_per_km,
                                                                   cost_eur         = EXCLUDED.cost_eur,
                                                                   typ              = EXCLUDED.typ;""")
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
        """Insert consumer_categories from config.
        Replaces pandas.to_sql(replace) with ON CONFLICT upserts for stable parallel execution.
        Table is global (no version_id). Existing IDs / definitions are updated.
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
        rows = df.to_dict(orient='records')
        try:
            self.cur.executemany(upsert_sql, rows)
            self.conn.commit()  # Added commit to persist consumer categories
            self.logger.info(f"Inserted/updated consumer_categories rows: {len(rows)}")
        except Exception as e:
            self.conn.rollback()
            self.logger.error(f"Failed inserting/updating consumer_categories: {e}")
            raise

    def postcode_exists_locally(self, plz: int) -> bool:
        """Returns True if the given PLZ already exists in the local postcode table."""
        self.cur.execute(
            "SELECT 1 FROM pylovo.postcode WHERE plz = %(p)s LIMIT 1;", {"p": plz}
        )
        return self.cur.fetchone() is not None

    def insert_postcode(self, postcode_row: tuple) -> None:
        """Insert a single postcode row (plz, note, qkm, population, geom) into the postcode table."""
        query = f"""
            INSERT INTO pylovo.postcode (plz, note, qkm, population, geom)
            VALUES (%s, %s, %s, %s, ST_Transform(%s::geometry, {TARGET_EPSG}))
            ON CONFLICT (plz) DO NOTHING;"""
        self.cur.execute(query, postcode_row)

    def copy_postcode_result_table(self, plz: int) -> None:
        """
        Copies the given plz entry from postcode to the postcode_result table
        :param plz:
        :return:
        """
        query = """INSERT INTO pylovo.postcode_result (version_id, postcode_result_plz, geom)
                   SELECT %(v)s as version_id, plz, geom
                   FROM pylovo.postcode
                   WHERE plz = %(p)s
                   LIMIT 1
                   ON CONFLICT (version_id,postcode_result_plz) DO NOTHING;"""

        self.cur.execute(query, {"v": VERSION_ID, "p": plz})

    def set_residential_buildings_table(self, plz: int):
        """
        * Fills buildings_tem with residential buildings that lie inside the postal code geometry
        :param plz:
        :return:
        """

        # Fill table
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
        """
        Insert buildings data associated with a specific postal code into the database.

        This function takes building data and inserts it into a temporary buildings table, associating each
        building with the given postal code. The temporary tables are then used when generating grids.

        Args:
            buildings_data (list[tuple[int, float, str, str, str, int, int]]): List of building tuples
                containing (id, floor_area, building_type, geom, center_geom, floor_number, households, address_street_id, construction_year).

        Returns:
            None
        """
        insert_query = f"""
            INSERT INTO buildings_tem
            (id, feature_id, objectid, height, floor_area, floor_number, building_use, building_use_id,
             building_type, occupants, households, construction_year, postcode, address_street_id, street,
             house_number, geom, centroid, gemeindeschluessel, changelog_id, assigned_way_id, type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    ST_Transform(%s::geometry, {TARGET_EPSG}), ST_Transform(%s::geometry, {TARGET_EPSG}),
                    %s, %s, %s, %s)
        """
        self.cur.executemany(insert_query, buildings_data)
        # self.conn.commit() only for debugging

    def set_buildings_table_with_geometry_filter(self, buildings_data: list[tuple], allocated_plz: int) -> None:
        """
        Insert buildings data with geometry filtering for testing mode.
        Only buildings that intersect with the testing geometry are inserted.

        The temporary table approach is necessary here because:
        1. We need to bulk insert all buildings first (for performance)
        2. Then filter them against postcode geometry (complex spatial operation)
        3. PostgreSQL can't efficiently do both in a single operation without either
           a temp table or individual queries per building
        """
        if not buildings_data:
            return

        # Create temporary table - automatically dropped at end of session
        self.cur.execute("""
            CREATE TEMP TABLE IF NOT EXISTS testing_buildings (
                id integer,
                feature_id integer,
                objectid text,
                height double precision,
                floor_area double precision,
                floor_number integer,
                building_use text,
                building_use_id text,
                building_type text,
                occupants integer,
                households integer,
                construction_year text,
                postcode integer,
                address_street_id bigint,
                street text,
                house_number text,
                geom geometry,
                centroid geometry,
                gemeindeschluessel text,
                changelog_id bigint,
                assigned_way_id text,
                type varchar
            ) ON COMMIT DROP
        """)

        # Bulk insert all buildings with geometry transformation
        insert_query = f"""
            INSERT INTO testing_buildings
            (id, feature_id, objectid, height, floor_area, floor_number, building_use, building_use_id,
             building_type, occupants, households, construction_year, postcode, address_street_id, street,
             house_number, geom, centroid, gemeindeschluessel, changelog_id, assigned_way_id, type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    ST_Transform(%s::geometry, {TARGET_EPSG}), ST_Transform(%s::geometry, {TARGET_EPSG}),
                    %s, %s, %s, %s)
        """
        self.cur.executemany(insert_query, buildings_data)

        # Filter and insert only buildings that intersect with the postcode geometry
        self.cur.execute("""
            INSERT INTO buildings_tem
            (id, feature_id, objectid, height, floor_area, floor_number, building_use, building_use_id,
             building_type, occupants, households, construction_year, postcode, address_street_id, street,
             house_number, geom, centroid, gemeindeschluessel, changelog_id, assigned_way_id, type)
            SELECT tb.id, tb.feature_id, tb.objectid, tb.height, tb.floor_area, tb.floor_number,
                   tb.building_use, tb.building_use_id, tb.building_type, tb.occupants, tb.households,
                   tb.construction_year, tb.postcode, tb.address_street_id, tb.street, tb.house_number,
                   tb.geom, tb.centroid, tb.gemeindeschluessel, tb.changelog_id, tb.assigned_way_id,
                   tb.type
            FROM testing_buildings tb
            CROSS JOIN pylovo.postcode p
            WHERE p.plz = %(plz)s
            AND p.allocated_plz IS NOT NULL
            AND ST_Intersects(tb.geom, p.geom)
        """, {"plz": allocated_plz})

        # Explicitly drop temp table (though ON COMMIT DROP would handle it)
        self.cur.execute("DROP TABLE IF EXISTS testing_buildings")

    def set_other_buildings_table(self, plz: int):
        """
        * Fills buildings_tem with other (non-residential) buildings inside the plz area
        * Sets all floors to 1 if missing
        :param plz:
        :return:
        """

        # Fill table
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
        """
        * Remove buildings without geometry or objectid
        * Remove buildings that are duplicates (copy id) of others
        :return:
        """
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

    def calculate_house_distance_metric(self, plz: int, sample_size: int = 50, k_nearest: int = 4) -> float:
        """Computes the average inter-building distance (meters) based on a random sample
        and writes house_distance into postcode_result. Returns the computed value.
        """
        distance_query = f"""WITH some_buildings AS (SELECT objectid, centroid
                                                    FROM buildings_tem
                                                    ORDER BY RANDOM()
                                                    LIMIT {sample_size})
                            SELECT b.objectid, d.dist
                            FROM some_buildings AS b
                                     LEFT JOIN LATERAL (
                                SELECT ST_Distance(b.centroid, b2.centroid) AS dist
                                FROM buildings_tem AS b2
                                WHERE b.objectid <> b2.objectid
                                ORDER BY b.centroid <-> b2.centroid
                                LIMIT {k_nearest}) AS d
                                               ON TRUE;"""
        self.cur.execute(distance_query)
        data = self.cur.fetchall()
        if not data:
            raise ValueError("No buildings in buildings_tem for house distance calculation.")
        distance_vals = [t[1] for t in data if t[1] is not None]
        if not distance_vals:
            raise ValueError("House distance calculation returned no distances.")
        avg_dis = float(sum(distance_vals) / len(distance_vals))
        update_query = """
            UPDATE pylovo.postcode_result
            SET house_distance = %(avg)s
            WHERE version_id = %(v)s
              AND postcode_result_plz = %(p)s;"""
        self.cur.execute(update_query, {"avg": avg_dis, "v": VERSION_ID, "p": plz})
        return avg_dis

    def calculate_avg_households_per_building(self, plz: int) -> float:
        """Computes the average number of households per (residential) building from buildings_tem
        and writes avg_households_per_building into postcode_result. Returns the value.
        """
        avg_query = """
            SELECT AVG(households)::DOUBLE PRECISION
            FROM buildings_tem
            WHERE households IS NOT NULL
              AND type IN ('SFH','TH','MFH','AB');"""
        self.cur.execute(avg_query, {"p": plz})
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
        """Determines settlement_type (1=rural, 2=semi-urban, 3=urban) using a weighted (continuous) combination
        of two metrics:
          - avg_households_per_building (higher => more urban)
          - house_distance (smaller => more urban)

        Method (weighted only):
          1. Normalize household metric to [0,1]:
               0 when avg <= rural_max_households -> rural, 1 when avg >= urban_min_households -> urban, linear in between
          2. Normalize distance to [0,1] (inverted):
               0 when distance >= rural_min_distance -> rural, 1 when distance <= urban_max_distance -> urban, linear in between
          3. Score = 0.5 * hh_norm + 0.5 * dist_norm
          4. Discretize: Score < 1/3 -> 1, < 2/3 -> 2, else 3

        Parameters can be calibrated; defaults come from configuration.
        """
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

    def set_building_peak_load(self) -> int:
        """
        * Sets floor_area, type and peak_load in the buildings_tem table
        * Removes buildings with zero load from the buildings_tem table
        :return: Number of removed unloaded buildings from buildings_tem
        """
        query = """
                UPDATE buildings_tem
                SET floor_area = ST_Area(geom)
                WHERE floor_area IS NULL;
                UPDATE buildings_tem
                
                -- Update households only if it has not been set already.
                -- For InfDB data this is already set.
                SET households = (
                    CASE
                    WHEN type IN ('TH', 'Commercial', 'Public', 'Industrial') THEN 1
                    WHEN type = 'SFH' AND floor_area < 160 THEN 1
                    WHEN type = 'SFH' AND floor_area >= 160 THEN 2
                    WHEN type IN ('MFH', 'AB') THEN floor(floor_area / 50) * floor_number
                    ELSE 0
                    END
                )
                WHERE households IS NULL;
                
                UPDATE buildings_tem b
                SET peak_load_in_kw = (CASE
                                           WHEN b.type IN ('SFH', 'TH', 'MFH', 'AB') THEN b.households *
                                                                                          (SELECT peak_load FROM pylovo.consumer_categories WHERE definition = b.type)
                                           WHEN b.type IN ('Commercial', 'Public', 'Industrial') THEN b.floor_area *
                                                                                                      (SELECT peak_load_per_m2
                                                                                                       FROM pylovo.consumer_categories
                                                                                                       WHERE definition = b.type) /
                                                                                                      1000
                                           ELSE 0
                    END);"""
        self.cur.execute(query)

        count_query = ("""SELECT COUNT(*)
                          FROM buildings_tem
                          WHERE peak_load_in_kw = 0;""")
        self.cur.execute(count_query)
        count = self.cur.fetchone()[0]

        delete_query = """DELETE
                          FROM buildings_tem
                          WHERE peak_load_in_kw = 0;"""
        self.cur.execute(delete_query)

        return count

    def update_too_large_consumers_to_zero(self) -> int:
        """
        Sets the load to zero if the peak load is too large (> 100)
        :return: number of the large customers
        """
        query = """
                UPDATE buildings_tem
                SET peak_load_in_kw = 0
                WHERE peak_load_in_kw > 100
                  AND type IN ('Commercial', 'Public');
                SELECT COUNT(*)
                FROM buildings_tem
                WHERE peak_load_in_kw = 0;"""
        self.cur.execute(query)
        too_large = self.cur.fetchone()[0]

        return too_large

    def assign_close_buildings(self) -> None:
        """
        * Set peak load to zero, if a building is too near or touching to a too large customer?
        :return:
        """
        while True:
            remove_query = """WITH close (un) AS (SELECT ST_Union(geom)
                                                  FROM buildings_tem
                                                  WHERE peak_load_in_kw = 0)
                              UPDATE buildings_tem b
                              SET peak_load_in_kw = 0
                              FROM close AS c
                              WHERE ST_Touches(b.geom, c.un)
                                AND b.type IN ('Commercial', 'Public', 'Industrial')
                                AND b.peak_load_in_kw != 0;"""
            self.cur.execute(remove_query)

            count_query = """WITH close (un) AS (SELECT ST_Union(geom)
                                                 FROM buildings_tem
                                                 WHERE peak_load_in_kw = 0)
                             SELECT COUNT(*)
                             FROM buildings_tem AS b,
                                  close AS c
                             WHERE ST_Touches(b.geom, c.un)
                               AND b.type IN ('Commercial', 'Public', 'Industrial')
                               AND b.peak_load_in_kw != 0;"""
            self.cur.execute(count_query)
            count = self.cur.fetchone()[0]
            if count == 0 or count is None:
                break

        return None

    def insert_transformers(self, plz: int) -> None:
        """
        Add up the existing transformers from transformers table to the buildings_tem table
        :param plz:
        :return:
        """
        insert_query = """
                       --UPDATE pylovo.transformers SET geom = ST_Centroid(geom) WHERE ST_GeometryType(geom) =  'ST_Polygon';
                       INSERT INTO buildings_tem (objectid, geom)--(objectid,centroid)
                       SELECT osm_id, geom
                       --FROM pylovo.transformers WHERE ST_Within(geom, (SELECT geom FROM pylovo.postcode_result LIMIT 1)) IS FALSE;
                       FROM pylovo.transformers as t
                       WHERE ST_Within(t.geom, (SELECT geom
                                                FROM pylovo.postcode_result
                                                WHERE postcode_result_plz = %(p)s
                                                  AND version_id = %(v)s)); --IS FALSE;
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
        self.cur.execute(insert_query, {"p": plz, "v": VERSION_ID})

    def count_indoor_transformers(self) -> None:
        """Counts indoor transformers before deleting them"""
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
        """
        Drop transformer if it is inside a building with zero load
        :return:
        """
        query = """WITH union_table (ungeom) AS
                                (SELECT ST_Union(geom) FROM buildings_tem WHERE peak_load_in_kw = 0)
                   DELETE
                   FROM buildings_tem
                   WHERE ST_Within(centroid, (SELECT ungeom FROM union_table))
                     AND type = 'Transformer';"""
        self.cur.execute(query)   

    def set_ways_tem_table_infdb(self, ways_data: list[tuple], plz: int = None) -> int:
        """
        Insert remote ways into the local ways_tem table.

        Args:
            ways_data (list[tuple]): Each tuple should contain
                (clazz, source, target, cost, reverse_cost, geom, way_id)
            plz (int): PLZ for geometry filtering in testing mode

        Returns:
            int: Number of inserted ways
        """
        if not ways_data:
            raise ValueError("No rows to insert into ways_tem")

        # Normal mode - insert all ways
        insert_query = f"""
            INSERT INTO ways_tem
            (clazz, source, target, cost, reverse_cost, geom, way_id)
            VALUES (%s, %s, %s, %s, %s, ST_Transform(%s::geometry, {TARGET_EPSG}), %s)
        """
        self.cur.executemany(insert_query, ways_data)
        self.cur.execute("SELECT COUNT(*) FROM ways_tem")
        return self.cur.fetchone()[0]

    def set_ways_tem_table_with_geometry_filter(self, ways_data: list[tuple], plz: int) -> int:
        """
        Insert ways data with geometry filtering for testing mode.
        Only ways that intersect with the testing geometry are inserted.
        """
        if not ways_data:
            return 0
            
        # Create a temporary table to hold the ways data
        temp_table_query = """
            CREATE TEMP TABLE temp_ways (
                clazz integer,
                source integer,
                target integer,
                cost double precision,
                reverse_cost double precision,
                geom geometry,
                way_id integer
            )
        """
        self.cur.execute(temp_table_query)
        
        # Insert all ways data into temp table
        insert_query = """
            INSERT INTO temp_ways
            (clazz, source, target, cost, reverse_cost, geom, way_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        self.cur.executemany(insert_query, ways_data)

        # Filter and insert only ways that intersect with testing geometry
        filter_query = f"""
            INSERT INTO ways_tem
            (clazz, source, target, cost, reverse_cost, geom, way_id)
            SELECT tw.clazz, tw.source, tw.target, tw.cost, tw.reverse_cost, 
                    ST_Transform(tw.geom, {TARGET_EPSG}), tw.way_id
            FROM temp_ways tw
            CROSS JOIN pylovo.postcode p
            WHERE p.plz = %(plz)s
            AND p.allocated_plz IS NOT NULL
            AND ST_Intersects(tw.geom, p.geom)
        """
        self.cur.execute(filter_query, {"plz": plz})
        
        # Get count of inserted ways
        self.cur.execute("SELECT COUNT(*) FROM ways_tem")
        count = self.cur.fetchone()[0]
        
        # Drop temp table
        self.cur.execute("DROP TABLE temp_ways")
        
        return count

    def set_ways_tem_table(self, plz: int) -> int:
        """
        * Inserts ways inside the plz area to the ways_tem table
        :param plz:
        :return: number of ways in ways_tem
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

    def preprocess_ways(self) -> None:
        """
        Runs the geometric preprocessing steps for the ways_tem table using two core functions:

        1. segment_intersecting_ways():
        - Detects where roads intersect geometrically.
        - Splits intersecting road segments into new segments at the intersection point.
        - Inserts the resulting segments into the working table `ways_tem`.
        - Internally uses: 
            - insert_way_segment() for adding new segments

        2. generate_building_to_way_connections():
        - Connects each building to the closest road segment.
        - For each building, generates a connection line to the corresponding way.
        - Splits the road segment at the connection point and updates `ways_tem`.
        - Uses:
            - generate_building_way_connection_candidates() for finding potential connections
            - insert_way_segment() for adding new segments
            - split_way_at_connection_points() for splitting ways at connection points

        These two functions are executed in sequence to ensure that:
        - All intersecting ways are properly split.
        - All buildings are connected to the network via dedicated segments.

        When the flag USE_INFDB is set to "True", the function generate_transformer_to_way_connections_infdb() 
        is used instead.
        """
        if USE_INFDB:
            self.cur.execute("SELECT generate_transformer_to_way_connections_infdb();")
        else:
            self.cur.execute("SELECT segment_intersecting_ways();")
            self.cur.execute("SELECT generate_building_to_way_connections();")

    def build_pgr_network_topology(self, plz: int) -> None:
        """Builds the pgRouting-compatible network topology from the updated `ways_tem` table.

        This method uses the pgRouting 3.8+ workflow:
        1. pgr_extractVertices(): Extracts unique vertices from edge geometries
        2. UPDATE source: Links start points of edges to vertex IDs
        3. UPDATE target: Links end points of edges to vertex IDs

        This replaces the deprecated pgr_createTopology() function.
        """
        edge_table = f"ways_tem_{plz}"
        vertices_table = f"{edge_table}_vertices_pgr"

        # Align endpoints before extracting vertices so pgRouting does not split components on
        # floating-point noise introduced by geometric preprocessing.
        self.cur.execute(f"""
            UPDATE {edge_table}
            SET geom = ST_SnapToGrid(geom, 0.000001)
            WHERE geom IS NOT NULL;
        """)

        # Ensure source and target columns exist on the edge table
        # (required before pgr_extractVertices can work)
        self.cur.execute(f"""
            ALTER TABLE {edge_table} ADD COLUMN IF NOT EXISTS source integer;
            ALTER TABLE {edge_table} ADD COLUMN IF NOT EXISTS target integer;
        """)

        # Drop existing vertices table if it exists
        self.cur.execute(f"DROP TABLE IF EXISTS {vertices_table} CASCADE;")

        # Step 1: Create the vertices table using pgr_extractVertices
        self.cur.execute(f"""
            CREATE TABLE {vertices_table} AS
            SELECT id, geom
            FROM pgr_extractVertices('SELECT way_id AS id, geom FROM {edge_table} ORDER BY way_id');
        """)

        # Add primary key for performance
        self.cur.execute(f"""
            ALTER TABLE {vertices_table} ADD PRIMARY KEY (id);
        """)

        # Create spatial index on vertices for faster lookups
        self.cur.execute(f"""
            CREATE INDEX {vertices_table}_geom_idx ON {vertices_table} USING GIST (geom);
        """)

        # Step 2: Update source nodes - link start of each edge to matching vertex
        self.cur.execute(f"""
            UPDATE {edge_table} AS e
            SET source = v.id
            FROM {vertices_table} AS v
            WHERE ST_StartPoint(e.geom) = v.geom;
        """)

        # Step 3: Update target nodes - link end of each edge to matching vertex
        self.cur.execute(f"""
            UPDATE {edge_table} AS e
            SET target = v.id
            FROM {vertices_table} AS v
            WHERE ST_EndPoint(e.geom) = v.geom;
        """)

        # Create indexes on source and target for routing performance
        self.cur.execute(f"""
            CREATE INDEX IF NOT EXISTS {edge_table}_source_idx ON {edge_table} (source);
            CREATE INDEX IF NOT EXISTS {edge_table}_target_idx ON {edge_table} (target);
        """)

        # Expose vertices table through a session-local view for easier downstream queries
        self.cur.execute(
            f"CREATE TEMP VIEW ways_tem_vertices_pgr AS SELECT * FROM {vertices_table}"
        )


    def update_ways_cost(self) -> None:
        """
        Calculates the length of each way and stores in ways_tem.cost as meter
        """
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

    def get_ags_log(self) -> pd.DataFrame:
        """Get AGS log: the official municipal keys (Amtlicher Gemeindeschlüssel) of municipalities
        whose buildings have already been imported into the database.
        :return: table with column ags
        """
        query = """SELECT *
                   FROM pylovo.ags_log;"""
        df_query = pd.read_sql_query(query, con=self.conn, )
        return df_query

    def insert_equipment_data(self, equipment_df: pd.DataFrame):
        """Insert equipment_data rows for current VERSION_ID if not already present for this version."""
        self.cur.execute("SELECT 1 FROM pylovo.equipment_data WHERE version_id = %s LIMIT 1", (VERSION_ID,))
        if self.cur.fetchone():
            return

        required_cols = ['name', 'typ']
        for rc in required_cols:
            if rc not in equipment_df.columns:
                raise ValueError(f"Missing required equipment column: {rc}")

        # Ensure numeric coercion for optional integer fields
        int_cols = ['s_max_kva', 'max_i_a', 'r_mohm_per_km', 'x_mohm_per_km',
                    'z_mohm_per_km', 'cost_eur']
        for col in int_cols:
            if col in equipment_df.columns:
                equipment_df[col] = pd.to_numeric(equipment_df[col], errors='coerce').astype('Int64')

        equipment_df = equipment_df.where(~equipment_df.isna(), None)

        insert_sql = """
            INSERT INTO pylovo.equipment_data
            (version_id, name, s_max_kva, max_i_a, r_mohm_per_km, x_mohm_per_km,
             z_mohm_per_km, cost_eur, typ)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """
        rows = []
        for _, r in equipment_df.iterrows():
            rows.append((
                VERSION_ID,
                r.get('name'),
                r.get('s_max_kva'),
                r.get('max_i_a'),
                r.get('r_mohm_per_km'),
                r.get('x_mohm_per_km'),
                r.get('z_mohm_per_km'),
                r.get('cost_eur'),
                r.get('typ')
            ))
        self.cur.executemany(insert_sql, rows)
        self.logger.debug("Inserted equipment_data for version %s", VERSION_ID)

    def get_plz_for_testing(self, plz) -> list:
        """
        Get the allocated_plz allocated to our dummy testing plz when testing. Each small testing plz must be allocated
        to the larger real plz it is located in to fetch data correctly.
        """
        query = """
                SELECT allocated_plz
                FROM pylovo.postcode
                WHERE plz = %(plz)s LIMIT 1
                """

        self.cur.execute(query, {"plz": plz})
        allocated_plz = self.cur.fetchone()[0]

        return allocated_plz

    def get_transformer_positions_for_plz_trafo_ui(self, plz: int) -> list[dict]:
        """
        Get all transformer positions for a given PLZ from the transformers table.

        Args:
            plz (int): The postal code to get transformer positions for

        Returns:
            list[dict]: List of transformer position dictionaries with keys:
                - osm_id: OSM identifier
                - transformer_rated_power: Transformer power rating
                - type: Transformer type
                - geom_type: Geometry type
                - within_shopping: Within shopping area flag
                - geom_wkt: Geometry as WKT (Well-Known Text)
        """
        query = """
            SELECT
                t.osm_id,
                t.transformer_rated_power,
                t.type,
                t.geom_type,
                t.within_shopping,
                ST_AsText(ST_Transform(t.geom, 4326)) as geom_wkt
            FROM pylovo.transformers t
            JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
            WHERE p.plz = %(plz)s
            LIMIT 1000
        """
        self.cur.execute(query, {"plz": plz})
        columns = [desc[0] for desc in self.cur.description]
        return [dict(zip(columns, row)) for row in self.cur.fetchall()]

    def add_transformer_position_trafo_ui(self, plz: int, geom_wkt: str, osm_id: str = None,
                                comment: str = "Manual", kcid: int = None, bcid: int = None,
                                transformer_rated_power: int = None) -> str:
        """
        Add a new transformer to the transformers table.

        Args:
            plz (int): The postal code (for reference, not stored)
            geom_wkt (str): Geometry as Well-Known Text (Point format)
            osm_id (str, optional): OSM identifier
            comment (str): Comment for the transformer (stored in type field)
            kcid (int, optional): K-means cluster ID (not used)
            bcid (int, optional): Building cluster ID (not used)
            transformer_rated_power (int, optional): Transformer power rating

        Returns:
            str: The osm_id of the created transformer
        """
        # Generate a unique OSM ID if not provided
        if not osm_id:
            osm_id = f"manual/{int(time.time())}"

        # Insert into transformers table
        transformer_query = f"""
            INSERT INTO pylovo.transformers (osm_id, type, transformer_rated_power, geom_type, within_shopping, geom)
            VALUES (%(osm_id)s, %(type)s, %(transformer_rated_power)s, %(geom_type)s, %(within_shopping)s, ST_Transform(ST_GeomFromText(%(geom_wkt)s, 4326), {TARGET_EPSG}))
            RETURNING osm_id
        """
        self.cur.execute(transformer_query, {
            "osm_id": osm_id,
            "type": comment,  # store comment in type field
            "transformer_rated_power": transformer_rated_power,
            "geom_type": "manual",
            "within_shopping": False,
            "geom_wkt": geom_wkt
        })

        # Commit the transaction
        self.conn.commit()

        return osm_id

    def delete_transformer_position_trafo_ui(self, grid_result_id: int) -> bool:
        """
        Delete a transformer position by grid_result_id.

        Args:
            grid_result_id (int): The grid_result_id to delete

        Returns:
            bool: True if deletion was successful, False otherwise
        """
        # Check if the transformer position exists
        check_query = "SELECT 1 FROM pylovo.transformer_positions WHERE grid_result_id = %(grid_result_id)s"
        self.cur.execute(check_query, {"grid_result_id": grid_result_id})
        if not self.cur.fetchone():
            return False

        # Delete the transformer position (grid_result will be deleted via CASCADE)
        delete_query = "DELETE FROM pylovo.transformer_positions WHERE grid_result_id = %(grid_result_id)s"
        self.cur.execute(delete_query, {"grid_result_id": grid_result_id})

        # Commit the transaction to persist the deletion
        self.conn.commit()

        return True

    def delete_transformer_by_osm_id_trafo_ui(self, osm_id: str) -> bool:
        """
        Delete a transformer by osm_id from the transformers table.

        Args:
            osm_id (str): The osm_id to delete

        Returns:
            bool: True if deletion was successful, False otherwise
        """
        # Debug: Check if transformer exists before deletion
        check_query = "SELECT osm_id, type FROM pylovo.transformers WHERE osm_id = %(osm_id)s"
        self.cur.execute(check_query, {"osm_id": osm_id})
        existing = self.cur.fetchone()

        if existing:
            print(f"DEBUG: Found transformer to delete: {existing}")
        else:
            print(f"DEBUG: No transformer found with osm_id: {osm_id}")
            # Let's also check for similar OSM IDs
            similar_query = "SELECT osm_id, type FROM pylovo.transformers WHERE osm_id LIKE %(pattern)s"
            self.cur.execute(similar_query, {"pattern": f"%{osm_id.split('/')[-1]}%"})
            similar = self.cur.fetchall()
            if similar:
                print(f"DEBUG: Found similar OSM IDs: {similar}")

        delete_query = "DELETE FROM pylovo.transformers WHERE osm_id = %(osm_id)s"
        self.cur.execute(delete_query, {"osm_id": osm_id})

        rows_affected = self.cur.rowcount
        print(f"DEBUG: Deletion query affected {rows_affected} rows")

        # Commit the transaction to persist the deletion
        if rows_affected > 0:
            self.conn.commit()
            print(f"DEBUG: Transaction committed successfully")

        return rows_affected > 0

    def clear_capacities_trafo_ui(self, plz: int) -> bool:
        """
        Clear all capacity information for transformers in a PLZ area.

        Args:
            plz (int): The PLZ code

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            query = """
                UPDATE pylovo.transformers
                SET transformer_rated_power = NULL
                WHERE osm_id IN (
                    SELECT t.osm_id
                    FROM pylovo.transformers t
                    JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
                    WHERE p.plz = %(plz)s
                )
            """
            self.cur.execute(query, {"plz": plz})
            rows_updated = self.cur.rowcount
            print(f"Cleared capacities for {rows_updated} transformers")
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error in clear_capacities_trafo_ui: {str(e)}")
            return False

    def get_plz_bounds_trafo_ui(self, plz: int) -> dict:
        """
        Get the bounding box for a given PLZ.

        Args:
            plz (int): The postal code

        Returns:
            dict: Bounding box with keys: minx, miny, maxx, maxy
        """
        query = """
            SELECT ST_XMin(ST_Transform(geom, 4326)) as minx, ST_YMin(ST_Transform(geom, 4326)) as miny,
                   ST_XMax(ST_Transform(geom, 4326)) as maxx, ST_YMax(ST_Transform(geom, 4326)) as maxy
            FROM pylovo.postcode
            WHERE plz = %(plz)s
        """
        self.cur.execute(query, {"plz": plz})
        row = self.cur.fetchone()
        if row:
            return {
                "minx": float(row[0]),
                "miny": float(row[1]),
                "maxx": float(row[2]),
                "maxy": float(row[3])
            }
        return None

    def get_available_plz_list_trafo_ui(self) -> list[int]:
        """
        Get list of available PLZ codes that have been processed.

        Returns:
            list[int]: List of PLZ codes
        """
        query = """
            SELECT DISTINCT plz
            FROM pylovo.postcode
            ORDER BY plz
        """
        self.cur.execute(query)
        return [row[0] for row in self.cur.fetchall()]

    def update_transformer_capacity_trafo_ui(self, osm_id: str, transformer_rated_power: int) -> bool:
        """
        Update transformer capacity.

        Args:
            osm_id (str): The OSM ID of the transformer
            transformer_rated_power (int): The new rated power in kVA

        Returns:
            bool: True if successful, False otherwise
        """
        query = """
            UPDATE pylovo.transformers
            SET transformer_rated_power = %(transformer_rated_power)s
            WHERE osm_id = %(osm_id)s
        """
        self.cur.execute(query, {"osm_id": osm_id, "transformer_rated_power": transformer_rated_power})
        self.conn.commit()
        return self.cur.rowcount > 0

    def bulk_update_capacities_uniform_trafo_ui(self, plz: int, transformer_rated_power: int) -> bool:
        """
        Set all transformers in a PLZ area to the same capacity.

        Args:
            plz (int): The PLZ code
            transformer_rated_power (int): The rated power in kVA to set for all transformers

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # First, check if there are any transformers in this PLZ
            check_query = """
                SELECT COUNT(DISTINCT t.osm_id)
                FROM pylovo.transformers t
                JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
                WHERE p.plz = %(plz)s
            """
            self.cur.execute(check_query, {"plz": plz})
            count = self.cur.fetchone()[0]
            print(f"Found {count} transformers in PLZ {plz}")

            if count == 0:
                print("No transformers found in PLZ area")
                return False

            query = """
                UPDATE pylovo.transformers
                SET transformer_rated_power = %(transformer_rated_power)s
                WHERE osm_id IN (
                    SELECT DISTINCT t.osm_id
                    FROM pylovo.transformers t
                    JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
                    WHERE p.plz = %(plz)s
                )
            """
            self.cur.execute(query, {"plz": plz, "transformer_rated_power": transformer_rated_power})
            rows_updated = self.cur.rowcount
            print(f"Updated {rows_updated} transformers")
            self.conn.commit()
            return rows_updated > 0
        except Exception as e:
            print(f"Error in bulk_update_capacities_uniform_trafo_ui: {str(e)}")
            return False

    def bulk_update_capacities_percentage_trafo_ui(self, plz: int, capacity_distribution: dict) -> bool:
        """
        Apply percentage-based distribution of transformer capacities.

        Args:
            plz (int): The PLZ code
            capacity_distribution (dict): Dictionary with capacity values as keys and percentages as values
            Example: {400: 30, 630: 50, 1000: 20} means 30% 400kVA, 50% 630kVA, 20% 1000kVA

        Returns:
            bool: True if successful, False otherwise
        """
        import random

        try:
            # Get all transformer OSM IDs in the PLZ area
            query = """
                SELECT DISTINCT t.osm_id
                FROM pylovo.transformers t
                JOIN pylovo.postcode p ON ST_Intersects(t.geom, p.geom)
                WHERE p.plz = %(plz)s
            """
            self.cur.execute(query, {"plz": plz})
            transformer_ids = [row[0] for row in self.cur.fetchall()]
            print(f"Found {len(transformer_ids)} transformers for percentage distribution")

            if not transformer_ids:
                print("No transformers found in PLZ area for percentage distribution")
                return False

            # Create capacity list based on percentages
            capacity_list = []
            for capacity, percentage in capacity_distribution.items():
                if percentage > 0:
                    count = int(len(transformer_ids) * percentage / 100)
                    capacity_list.extend([capacity] * count)
                    print(f"Added {count} transformers with {capacity}kVA capacity ({percentage}%)")

            # Fill remaining with the most common capacity if we have fewer than expected
            if len(capacity_list) < len(transformer_ids):
                most_common_capacity = max(capacity_distribution.keys(), key=lambda k: capacity_distribution[k])
                remaining = len(transformer_ids) - len(capacity_list)
                capacity_list.extend([most_common_capacity] * remaining)
                print(f"Added {remaining} more transformers with {most_common_capacity}kVA capacity")

            print(f"Total capacity list length: {len(capacity_list)}, transformer count: {len(transformer_ids)}")

            # Shuffle to randomize distribution
            random.shuffle(capacity_list)

            # Update each transformer
            update_query = """
                UPDATE pylovo.transformers
                SET transformer_rated_power = %(transformer_rated_power)s
                WHERE osm_id = %(osm_id)s
            """

            updated_count = 0
            for i, osm_id in enumerate(transformer_ids):
                if i < len(capacity_list):
                    self.cur.execute(update_query, {
                        "osm_id": osm_id,
                        "transformer_rated_power": capacity_list[i]
                    })
                    updated_count += 1

            print(f"Updated {updated_count} transformers with new capacities")
            self.conn.commit()
            return True
        except Exception as e:
            print(f"Error in bulk_update_capacities_percentage_trafo_ui: {str(e)}")
            return False
