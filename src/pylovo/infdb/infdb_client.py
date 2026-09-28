"""Read-only client for the InfDB source data (buildings, ways, postcodes)."""

import psycopg2 as psy
from psycopg2 import sql

from pylovo import utils
from pylovo.config_loader import (
    EXCLUDE_BUILDINGS_WITHOUT_ADDRESS,
    INFDB_DBNAME,
    INFDB_HOST,
    INFDB_OPENDATA_SCHEMA,
    INFDB_PASSWORD,
    INFDB_PORT,
    INFDB_SOURCE_SCHEMA,
    INFDB_USER,
    LOG_LEVEL,
)


class InfdbClient:
    """Connection to InfDB, the source of buildings, ways and postcodes when ``USE_INFDB=True``.

    The connection defaults to the pylovo database itself (``config_loader`` sets the
    ``INFDB_*`` settings to the ``.env`` connection); ``search_path`` is
    ``INFDB_SOURCE_SCHEMA, public``. Buildings come from ``basedata.buildings``, ways from
    ``ways_per_connection`` and ``connection_lines`` in ``INFDB_SOURCE_SCHEMA``, postcodes from
    ``INFDB_OPENDATA_SCHEMA.postcodes_germany``.

    Args:
        dbname: Database name; defaults to ``INFDB_DBNAME``.
        user: Database user; defaults to ``INFDB_USER``.
        pw: Password; defaults to ``INFDB_PASSWORD``.
        host: Host; defaults to ``INFDB_HOST``.
        port: Port; defaults to ``INFDB_PORT``.
        **kwargs: ``log_file``: log file path (default ``log/log.txt``).

    Raises:
        psycopg2.OperationalError: If the database cannot be reached.
    """

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
            self.db_path = f"{host}:{port}/{dbname}"
        except psy.OperationalError as err:
            self.logger.warning(f"Connecting to {dbname} was not successful."
                                f"Make sure, that you have established the SSH connection with correct port mapping.")
            raise err

        self.logger.debug(f"InfDB DatabaseClient is constructed and connected to {self.db_path}.")

    def __del__(self):
        """Close cursor and connection; safe if the constructor failed before opening them."""
        cur = getattr(self, "cur", None)
        if cur is not None:
            cur.close()
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()

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
        if EXCLUDE_BUILDINGS_WITHOUT_ADDRESS:
            # Outbuildings (sheds, garages, barns) carry no address of their own and are
            # almost never separate grid customers. An import restricted to a reviewed
            # building allowlist must not apply this filter.
            query += " AND COALESCE(street, '') <> '' AND COALESCE(house_number, '') <> ''"
        self.cur.execute(query, {"p": plz})
        buildings = self.cur.fetchall()

        return buildings
    
    def fetch_transformer_station_buildings_from_infdb(self, plz: int) -> list[tuple]:
        """Return the LoD2 transformer-station buildings of a PLZ (``building_use_id`` 31001_2523).

        Returns:
            ``(objectid, geom, centroid)`` rows in the source SRID.
        """
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
        """Return the ways of a PLZ in the tuple layout of ``PreprocessingMixin.set_ways_tem_table_infdb``.

        Rows are ``(clazz, source, target, cost, reverse_cost, geom, way_id)``; ``source`` and
        ``target`` are NULL (pgRouting topology is built later), ``geom`` is in the source SRID.
        Cycle and footpaths (``Rad- und Fußweg``, clazz 72) are left out.

        How the rows are built:

        1. **Road class.** The InfDB stores the road type as text in ``klasse`` (for example
           ``"Bundesstraße"``, ``"Fußweg"``, ``"connection_line"``); pylovo expects an integer
           ``clazz``. ``klasse_to_clazz`` is turned into an SQL ``CASE`` on ``btrim(klasse)``
           (unknown values become 99).
        2. **Column mapping.** ``klasse`` -> ``clazz``; ``source`` and ``target`` -> ``NULL``
           (they only keep the column positions); ``length_geo`` -> ``cost`` and
           ``reverse_cost`` (symmetric routing cost); ``geom`` unchanged (transformed locally
           later); the text ``id`` -> a generated integer ``way_id``.
        3. **Two source tables.** The street segments (``ways_per_connection``, split at the
           building connections) and the building connection lines (``connection_lines``) are
           combined with ``UNION ALL``.
        4. **Unique way ids across runs.** A plain ``row_number()`` would restart at 1 for every
           PLZ and collide with ids of earlier runs. The ids are therefore
           ``COALESCE(MAX(way_id), 0) FROM pylovo.ways_result`` plus
           ``row_number() OVER (ORDER BY remote_id)``, where ``remote_id`` is a stable key derived
           from the source id. Sequential runs keep ``way_id`` unique; parallel runs can still
           assign the same ids, because ``pylovo.ways_result`` only grows when a run commits.
           ``pylovo.ways_result`` is read through this InfDB connection, which therefore has to
           point to the pylovo database (the default).

        Args:
            plz: Postcode.

        Returns:
            The ways of the PLZ.

        Raises:
            ValueError: If the PLZ has no ways.
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
        """Return ``(plz, note, qkm, population, geom)`` of one PLZ from ``INFDB_OPENDATA_SCHEMA.postcodes_germany``.

        Returns:
            The row, or ``None`` if the PLZ is unknown.
        """
        query = sql.SQL("""
            SELECT plz, note, qkm, einwohner, geom
            FROM {postcodes}
            WHERE plz IN (%(plz)s::varchar, lpad(%(plz)s::varchar, 5, '0'))
            LIMIT 1;
        """).format(postcodes=sql.Identifier(INFDB_OPENDATA_SCHEMA, "postcodes_germany"))
        self.cur.execute(query, {"plz": plz})
        return self.cur.fetchone()

    def fetch_all_postcodes_from_infdb(self) -> list[tuple]:
        """Return all postcodes of ``INFDB_OPENDATA_SCHEMA.postcodes_germany`` for ``pylovo-setup``.

        Returns:
            ``(plz, note, qkm, population, geom)`` rows ordered by PLZ.

        Raises:
            ValueError: If the table is empty.
        """
        query = sql.SQL("""
            SELECT plz, note, qkm, einwohner, geom
            FROM {postcodes}
            ORDER BY plz;
        """).format(postcodes=sql.Identifier(INFDB_OPENDATA_SCHEMA, "postcodes_germany"))
        self.cur.execute(query)
        rows = self.cur.fetchall()
        if not rows:
            raise ValueError("No postcode found in infdb")

        return rows

