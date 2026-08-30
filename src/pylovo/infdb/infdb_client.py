from typing import Any

import psycopg2 as psy

from pylovo import utils
from pylovo.config_loader import *
from pylovo.database.database_client import DatabaseClient


class InfdbClient:
    """Responsible for connecting to InfDB database."""

    def __init__(self, dbname=INFDB_DBNAME, user=INFDB_USER, pw=INFDB_PASSWORD, host=INFDB_HOST, port=INFDB_PORT, **kwargs):
        self.logger = utils.create_logger(
            "DatabaseClient", log_file=kwargs.get("log_file", "log/log.txt"), log_level=LOG_LEVEL
        )
        try:
            self.conn = psy.connect(
                database=dbname,
                user=user,
                password=pw,
                host=host,
                port=port,
                options=f"-c search_path={INFDB_SOURCE_SCHEMA},public",
            )
            self.cur = self.conn.cursor()
            self.db_path = f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{dbname}"
        except psy.OperationalError as err:
            self.logger.warning(f"Connecting to {dbname} was not successful."
                                f"Make sure, that you have established the SSH connection with correct port mapping.")
            raise err

        self.logger.debug(f"InfDB DatabaseClient is constructed and connected to {self.db_path}.")

    def __del__(self):
        self.cur.close()
        self.conn.close()

    def fetch_buildings_from_infdb(self, plz: int) -> list[tuple]:
        """
        Retrieve all buildings whose centroids are contained within a specified postcode (PLZ).

        Args:
            plz (str): The plz of the buildings to get

        Returns:
            list[tuple]: A list of tuples containing the source building columns plus
                a derived type used by the generation pipeline.
        """
        query = """
            SELECT
                id,
                feature_id,
                objectid,
                height,
                floor_area,
                floor_number,
                residential_floor_area,
                nonresidential_floor_area,
                CASE
                    WHEN COALESCE(nonresidential_floor_area, 0) <= 0 THEN NULL
                    WHEN building_use = 'Residential' THEN 'Commercial'
                    WHEN building_use = 'Mixed' THEN basedata.classify_building_use(building_use_id)
                    WHEN building_use IN ('Commercial', 'Public') THEN building_use
                    ELSE NULL
                END AS nonresidential_use,
                mix_score,
                mix_rule,
                mix_confidence,
                building_use,
                building_use_id,
                building_type,
                occupants,
                households,
                construction_year,
                postcode,
                address_street_id,
                street,
                house_number,
                geom,
                COALESCE(centroid, ST_Centroid(geom)) AS centroid,
                gemeindeschluessel,
                changelog_id,
                assigned_way_id,
                COALESCE(building_type, building_use) AS type
            FROM basedata.buildings
            WHERE postcode = %(p)s
            AND building_use IN ('Commercial', 'Public', 'Residential', 'Mixed')
            AND COALESCE(building_use_id, '') != '31001_2523'
        """
        self.cur.execute(query, {"p": plz})
        buildings = self.cur.fetchall()

        return buildings
    
    def fetch_transformer_station_buildings_from_infdb(self, plz: int) -> list[tuple]:
        """Retrieve LoD2 buildings that represent transformer stations."""
        query = """
            SELECT
                objectid,
                geom,
                COALESCE(centroid, ST_Centroid(geom)) AS centroid
            FROM basedata.buildings
            WHERE postcode = %(p)s
              AND building_use_id = '31001_2523'
        """
        self.cur.execute(query, {"p": plz})
        return self.cur.fetchall()

    def fetch_ways_from_infdb(self, plz) -> list:
        """
        Fetch ways from the remote DB for a given postcode (PLZ) and return them in the
        exact tuple layout expected by the pylovo import/insert pipeline.

        What this function does (and why):

        1) klasse_to_clazz (string -> int mapping)
        - In the source schema, the road type is stored as a string column `klasse`
            (e.g., "Bundesstraße", "Fußweg", "connection_line").
        - In pylovo, the downstream tables/operators expect an integer road class
            (`clazz`).
        - Therefore, we define `klasse_to_clazz` once in Python and generate a SQL
            CASE expression from it. We also use `btrim(klasse)` so trailing/leading
            spaces in the raw strings do not break the mapping.

        2) Column mapping from source tables -> pylovo.ways tuple layout
        The insert into the local temp/result tables expects rows shaped like:
            (clazz, source, target, cost, reverse_cost, geom, way_id)

        We construct that shape from the source columns as follows:
        - klasse (string)      -> clazz (int)
            via CASE mapping using `klasse_to_clazz` (defaulting to 99 if unknown).
        - source (missing/unused in source) -> NULL
            we return `NULL::bigint AS source` to preserve the expected column position/type.
        - target (missing/unused in source) -> NULL
            we return `NULL::bigint AS target` for the same reason.
        - length_geo -> cost
            we use the geometric length as the routing cost.
        - length_geo -> reverse_cost
            we set reverse_cost equal to cost (symmetric) because direction-specific
            costs are not available/needed here.
        - geom -> geom
            geometry is passed through unchanged and later transformed locally.
        - id (source is md5 text) -> way_id (generated int)
            pylovo expects an integer id, so we generate numeric ids during fetch.

        3) Combining ways_segmented + connection_lines (UNION ALL)
        - Previously, all relevant ways lived in a single table.
        - Now the road network is split across:
            * ways_segmented      (regular road segments)
            * connection_lines    (synthetic connection segments, e.g., building-to-road)
        - To ensure pylovo inserts a complete network, we combine both tables in a
            single result set using `UNION ALL` so we keep all rows.

        4) Ensuring unique integer way_id across multiple runs
        - The source `id` is an md5 string (created for parallel-safe uniqueness), but
            pylovo target expects an integer way_id.
        - A plain `row_number()` would restart at 1 on every run/PLZ, causing collisions
            if you generate 80803 first and later generate 80802 in a separate run.
        - To avoid collisions, we:
            a) read the current maximum used id from `pylovo.ways_result`:
                    base_id = COALESCE(MAX(way_id), 0)
            b) assign ids as:
                    way_id = base_id + row_number() OVER (ORDER BY remote_id)
            where remote_id is a stable ordering key derived from the source id.
        - Result: each run continues numbering from the last used id, so way_id remains
            globally unique across sequential executions.
        """

        klasse_to_clazz = {
            "Bundesautobahn": 11,
            "Bundesstraße": 13,
            "Landesstraße, Staatsstraße": 15,
            "Kreisstraße": 21,
            "Gemeindestraße": 41,
            "Nicht öffentliche Straße": 51,
            "Wirtschaftsweg": 71,
            "Hauptwirtschaftsweg": 71,
            "Rad- und Fußweg": 72,
            "Radweg": 81,
            "Fußweg": 91,
            "connection_line": 110,
        }
        default_clazz = 99

        def _sql_literal(s: str) -> str:
            return s.replace("'", "''")

        when_clauses = "\n".join(
            f"WHEN '{_sql_literal(k)}' THEN {int(v)}"
            for k, v in klasse_to_clazz.items()
        )

        case_expr = f"""
            (CASE btrim(klasse)
                {when_clauses}
                ELSE {int(default_clazz)}
            END)::int
        """

        query = f"""
            WITH max_id AS (
                SELECT COALESCE(MAX(way_id), 0) AS base_id
                FROM pylovo.ways_result
            ),
            base AS (
                SELECT
                    {case_expr} AS clazz,
                    NULL::bigint AS source,
                    NULL::bigint AS target,
                    length_geo AS cost,
                    length_geo AS reverse_cost,
                    geom,
                    ('ways_per_connection:' || id::text) AS remote_id
                FROM ways_per_connection
                WHERE postcode = %(plz)s

                UNION ALL

                SELECT
                    {case_expr} AS clazz,
                    NULL::bigint AS source,
                    NULL::bigint AS target,
                    length_geo AS cost,
                    length_geo AS reverse_cost,
                    geom,
                    ('connection_lines:' || id::text) AS remote_id
                FROM connection_lines
                WHERE postcode = %(plz)s
            )
            SELECT
                b.clazz,
                b.source,
                b.target,
                b.cost,
                b.reverse_cost,
                b.geom,
                (m.base_id + row_number() OVER (ORDER BY b.remote_id))::bigint AS way_id
            FROM base b
            CROSS JOIN max_id m
            WHERE b.clazz != 72
        """

        self.cur.execute(query, {"plz": plz})

        ways = self.cur.fetchall()
        if not ways:
            raise ValueError("No ways found in remote DB intersecting the given PLZ geometry")

        return ways
    
    def fetch_postcode_from_infdb(self, plz: int) -> tuple | None:
        """
        Fetch the postcode geometry row for a single PLZ from opendata schema.
        Returns a tuple of (plz, note, qkm, population, geom) or None if not found.
        """
        query = """
            SELECT plz, note, qkm, einwohner, geom
            FROM opendata.postcodes_germany
            WHERE plz = %(plz)s::varchar
            LIMIT 1;
        """
        self.cur.execute(query, {"plz": plz})
        return self.cur.fetchone()

    def fetch_all_postcodes_from_infdb(self) -> list[tuple]:
        """
        Bulk-fetch all postcode rows from opendata schema for use during pylovo-setup.
        Returns tuples of (plz, note, qkm, population, geom).
        """
        query = """
            SELECT plz, note, qkm, einwohner, geom
            FROM opendata.postcodes_germany
            ORDER BY plz;
        """
        self.cur.execute(query)
        rows = self.cur.fetchall()
        if not rows:
            raise ValueError("No postcode found in infdb")

        return rows

