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


#: InfDB road classes (``klasse``) as pylovo's integer ``clazz``; unknown classes become 99.
KLASSE_TO_CLAZZ = {
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
DEFAULT_CLAZZ = 99


def _clazz_case_sql() -> str:
    """The SQL ``CASE`` that maps ``btrim(klasse)`` to ``clazz``."""
    when_clauses = "\n".join(
        f"WHEN '{k.replace(chr(39), chr(39) * 2)}' THEN {int(v)}" for k, v in KLASSE_TO_CLAZZ.items()
    )
    return f"""
            (CASE btrim(klasse)
                {when_clauses}
                ELSE {int(DEFAULT_CLAZZ)}
            END)::int
        """


class InfdbClient:
    """Connection to InfDB, the source of buildings, ways and postcodes when ``USE_INFDB=True``.

    The connection defaults to the pylovo database itself; with ``USE_EXTERNAL_BASEDATA=True``
    ``config_loader`` takes the ``INFDB_*`` connection settings of the ``.env`` file instead.
    Buildings come from ``INFDB_SOURCE_SCHEMA.buildings``, ways from ``ways_per_connection`` and
    ``connection_lines`` in ``INFDB_SOURCE_SCHEMA``, postcodes from
    ``INFDB_OPENDATA_SCHEMA.postcodes_germany``.

    All tables are schema-qualified and no startup ``options`` are sent, so the connection also
    works through PgBouncer. Every query runs in its own read-only transaction that is closed
    right after the fetch, so the client never writes to InfDB and never stays idle in a
    transaction there.

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
            )
            # BEGIN READ ONLY per transaction; with autocommit psycopg2 would SET it for the session,
            # which a PgBouncer in transaction mode can pass on to other clients
            self.conn.set_session(readonly=True)
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
        query = sql.SQL("""
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
                    WHEN building_use = 'Mixed' THEN
                        CASE {classify}(building_use_id)
                            -- a cell-quota promotion keeps the original Residential
                            -- function code, so its non-residential share is reclassified
                            -- the same way a plain Residential building's is above
                            WHEN 'Residential' THEN 'Commercial'
                            ELSE {classify}(building_use_id)
                        END
                    WHEN building_use IN ('Commercial', 'Public', 'Unknown') THEN building_use
                    ELSE NULL
                END AS nonresidential_use,
                mix_rule,
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
            FROM {buildings}
            WHERE postcode = %(p)s
            AND building_use IN ('Commercial', 'Public', 'Residential', 'Mixed', 'Unknown')
            AND COALESCE(building_use_id, '') != '31001_2523'
        """).format(classify=sql.Identifier(INFDB_SOURCE_SCHEMA, "classify_building_use"),
                    buildings=sql.Identifier(INFDB_SOURCE_SCHEMA, "buildings"))
        if EXCLUDE_BUILDINGS_WITHOUT_ADDRESS:
            # Outbuildings (sheds, garages, barns) carry no address of their own and are
            # almost never separate grid customers. An import restricted to a reviewed
            # building allowlist must not apply this filter.
            query += sql.SQL(" AND COALESCE(street, '') <> '' AND COALESCE(house_number, '') <> ''")
        with self.conn:
            self.cur.execute(query, {"p": plz})
            buildings = self.cur.fetchall()

        return buildings
    
    def fetch_transformer_station_buildings_from_infdb(self, plz: int) -> list[tuple]:
        """Return the LoD2 transformer-station buildings of a PLZ (``building_use_id`` 31001_2523).

        Returns:
            ``(objectid, geom, centroid)`` rows in the source SRID.
        """
        query = sql.SQL("""
            SELECT
                objectid,
                geom,
                COALESCE(centroid, ST_Centroid(geom)) AS centroid
            FROM {buildings}
            WHERE postcode = %(p)s
              AND building_use_id = '31001_2523'
        """).format(buildings=sql.Identifier(INFDB_SOURCE_SCHEMA, "buildings"))
        with self.conn:
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
        4. **Way ids.** ``way_id`` is ``row_number() OVER (ORDER BY remote_id)``, where
           ``remote_id`` is a stable key derived from the source id, so it starts at 1 for every
           PLZ. ``PreprocessingMixin.set_ways_tem_table_infdb`` offsets it by the largest
           ``way_id`` of ``pylovo.ways_result`` in the pylovo database, which may be another
           database than InfDB.

        Args:
            plz: Postcode.

        Returns:
            The ways of the PLZ.

        Raises:
            ValueError: If the PLZ has no ways.
        """

        case_expr = _clazz_case_sql()
        ways_per_connection = sql.Identifier(INFDB_SOURCE_SCHEMA, "ways_per_connection").as_string(self.cur)
        connection_lines = sql.Identifier(INFDB_SOURCE_SCHEMA, "connection_lines").as_string(self.cur)
        query = f"""
            WITH base AS (
                SELECT
                    {case_expr} AS clazz,
                    NULL::bigint AS source,
                    NULL::bigint AS target,
                    length_geo AS cost,
                    length_geo AS reverse_cost,
                    geom,
                    ('ways_per_connection:' || id::text) AS remote_id
                FROM {ways_per_connection}
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
                FROM {connection_lines}
                WHERE postcode = %(plz)s
            )
            SELECT
                b.clazz,
                b.source,
                b.target,
                b.cost,
                b.reverse_cost,
                b.geom,
                (row_number() OVER (ORDER BY b.remote_id))::bigint AS way_id
            FROM base b
            WHERE b.clazz != 72
        """

        with self.conn:
            self.cur.execute(query, {"plz": plz})
            ways = self.cur.fetchall()
        if not ways:
            raise ValueError("No ways found in remote DB intersecting the given PLZ geometry")

        return ways

    def source_srid(self, table: str, schema: str | None = None) -> int:
        """SRID of the geometries of an InfDB table, read from a row (``geometry_columns`` may say 0)."""
        cache = self.__dict__.setdefault("_srid_cache", {})
        key = (schema or INFDB_SOURCE_SCHEMA, table)
        if key not in cache:
            query = sql.SQL("SELECT ST_SRID(geom) FROM {t} WHERE geom IS NOT NULL LIMIT 1").format(
                t=sql.Identifier(*key))
            with self.conn:
                self.cur.execute(query)
                row = self.cur.fetchone()
            cache[key] = int(row[0]) if row and row[0] else 0
        return cache[key]

    def fetch_halo_ways(self, plz: int, polygon_wkb: bytes, polygon_srid: int, ring_m: float,
                        first_way_id: int) -> list:
        """Return the street segments of other PLZ within ``ring_m`` of a PLZ polygon (the halo).

        Rows have the layout of :meth:`fetch_ways_from_infdb` and way ids continuing after
        ``first_way_id``. Only ``ways_per_connection`` is read (no house connection lines of other
        PLZ); segments are whole source rows found through the geometry index, and segments with
        ``postcode`` NULL count as other PLZ.

        Args:
            plz: The PLZ whose own ways are loaded separately.
            polygon_wkb: The PLZ polygon as WKB.
            polygon_srid: SRID of ``polygon_wkb``.
            ring_m: Halo width in metres.
            first_way_id: Largest way id of the own ways; the halo ids follow it.
        """
        ways = sql.Identifier(INFDB_SOURCE_SCHEMA, "ways_per_connection").as_string(self.cur)
        srid = self.source_srid("ways_per_connection")
        query = f"""
            WITH area AS (
                SELECT ST_Transform(ST_SetSRID(ST_GeomFromWKB(%(poly)s), %(poly_srid)s), %(srid)s) AS g
            ), base AS (
                SELECT
                    {_clazz_case_sql()} AS clazz,
                    NULL::bigint AS source,
                    NULL::bigint AS target,
                    w.length_geo AS cost,
                    w.length_geo AS reverse_cost,
                    w.geom,
                    ('ways_per_connection:' || w.id::text) AS remote_id
                FROM {ways} w, area a
                WHERE w.postcode IS DISTINCT FROM %(plz)s
                  AND w.geom && ST_Expand(a.g, %(ring)s)
                  AND ST_DWithin(w.geom, a.g, %(ring)s)
            )
            SELECT b.clazz, b.source, b.target, b.cost, b.reverse_cost, b.geom,
                   (row_number() OVER (ORDER BY b.remote_id))::bigint + %(offset)s AS way_id
            FROM base b
            WHERE b.clazz != 72
        """
        params = {"poly": polygon_wkb, "poly_srid": int(polygon_srid), "srid": srid or int(polygon_srid),
                  "plz": int(plz), "ring": float(ring_m), "offset": int(first_way_id)}
        with self.conn:
            self.cur.execute(query, params)
            return self.cur.fetchall()

    def fetch_scope_wkb(self, polygon_wkb: bytes, polygon_srid: int, distance_m: float) -> bytes | None:
        """Union of the import scope (``INFDB_OPENDATA_SCHEMA.scope``) near a region, as WKB in ``polygon_srid``.

        Returns ``None`` if the InfDB has no scope table or no scope row is near the region.
        """
        with self.conn:
            self.cur.execute("SELECT to_regclass(%s)", (f"{INFDB_OPENDATA_SCHEMA}.scope",))
            if self.cur.fetchone()[0] is None:
                return None
        srid = self.source_srid("scope", INFDB_OPENDATA_SCHEMA)
        query = sql.SQL("""
            WITH area AS (
                SELECT ST_Transform(ST_SetSRID(ST_GeomFromWKB(%(poly)s), %(poly_srid)s), %(srid)s) AS g
            )
            SELECT ST_AsBinary(ST_Transform(ST_Union(s.geom), %(poly_srid)s))
            FROM {scope} s, area a
            WHERE s.geom && ST_Expand(a.g, %(d)s) AND ST_DWithin(s.geom, a.g, %(d)s)
        """).format(scope=sql.Identifier(INFDB_OPENDATA_SCHEMA, "scope"))
        with self.conn:
            self.cur.execute(query, {"poly": polygon_wkb, "poly_srid": int(polygon_srid),
                                     "srid": srid or int(polygon_srid), "d": float(distance_m)})
            row = self.cur.fetchone()
        return bytes(row[0]) if row and row[0] is not None else None

    def fetch_street_length_km(self, plz: int, polygon_wkb: bytes, polygon_srid: int, ring_m: float
                               ) -> tuple[float, float]:
        """Street length inside a PLZ polygon and in the ring of ``ring_m`` around it (other PLZ only), in km."""
        ways = sql.Identifier(INFDB_SOURCE_SCHEMA, "ways_per_connection").as_string(self.cur)
        srid = self.source_srid("ways_per_connection")
        query = f"""
            WITH area AS (
                SELECT ST_Transform(ST_SetSRID(ST_GeomFromWKB(%(poly)s), %(poly_srid)s), %(srid)s) AS g
            )
            SELECT COALESCE(SUM(ST_Length(ST_Intersection(w.geom, a.g))), 0) / 1000.0,
                   COALESCE(SUM(ST_Length(w.geom)) FILTER (WHERE w.postcode IS DISTINCT FROM %(plz)s
                                                          AND NOT ST_Within(w.geom, a.g)), 0) / 1000.0
            FROM {ways} w, area a
            WHERE w.geom && ST_Expand(a.g, %(ring)s) AND ST_DWithin(w.geom, a.g, %(ring)s)
        """
        with self.conn:
            self.cur.execute(query, {"poly": polygon_wkb, "poly_srid": int(polygon_srid),
                                     "srid": srid or int(polygon_srid), "plz": int(plz), "ring": float(ring_m)})
            inside, ring = self.cur.fetchone()
        return float(inside), float(ring)
    
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
        with self.conn:
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
        with self.conn:
            self.cur.execute(query)
            rows = self.cur.fetchall()
        if not rows:
            raise ValueError("No postcode found in infdb")

        return rows

