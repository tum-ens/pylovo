"""Opt-in integration checks for the schema review; run only on a disposable database.

    PYLOVO_REVIEW_TEST_DSN="host=127.0.0.1 port=55460 dbname=rv_review user=sandbox password=sandbox" \\
        pytest tests/test_database_review.py

The role needs CREATEDB and the right to create PostGIS and pgRouting: three tests create and drop databases
``pylovo_review_<hex>``. ``PYLOVO_REVIEW_TEST_PORT=<port>`` alone stands for database and role ``postgres``
without a password on 127.0.0.1.
"""
from __future__ import annotations

import os
import uuid

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

from pylovo.config_loader import TARGET_EPSG
from pylovo.database.database_client import DatabaseClient
from pylovo.database.database_constructor import DatabaseConstructor

TEST_PORT = os.getenv("PYLOVO_REVIEW_TEST_PORT")
TEST_DSN = os.getenv("PYLOVO_REVIEW_TEST_DSN") or (
    TEST_PORT and f"host=127.0.0.1 port={TEST_PORT} dbname=postgres user=postgres"
)
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="set PYLOVO_REVIEW_TEST_DSN or PYLOVO_REVIEW_TEST_PORT for an isolated PostgreSQL/PostGIS instance",
)


def client(dbname: str | None = None) -> DatabaseClient:
    """Client of the test database, or of ``dbname`` on the same server with the same role."""
    params = parse_dsn(TEST_DSN)
    return DatabaseClient(dbname=dbname or params["dbname"], user=params.get("user"), pw=params.get("password", ""),
                          host=params.get("host"), port=int(params.get("port", 5432)))


def admin_connection():
    """Autocommit connection to the test database for CREATE and DROP DATABASE."""
    conn = psycopg2.connect(TEST_DSN)
    conn.autocommit = True
    return conn


def test_migrations_are_repeatable_and_catalog_is_valid():
    with client() as db:
        constructor = DatabaseConstructor(db)
        constructor.migrate_schema()
        db.cur.execute("SELECT name FROM pylovo.schema_migrations ORDER BY name COLLATE \"C\"")
        assert [row[0] for row in db.cur.fetchall()] == [
            "0001_legacy_buildings",
            "0001a_line_cache_rename",
            "0001b_buildings_regular_view",
            "0002_legacy_columns",
            "0003_integrity",
            "0004_equipment_backfill",
            "0005_indexes_checks",
            "0006_legacy_building_fk",
            "0007_line_cache_compatibility_view",
            "0008_percentage_checks",
            "0009_mv_grids",
            "0010_mv_display_geometry",
            "0011_building_exclusion",
            "0012_indexes",
            "0013_schema_cleanup",
            "0014_classification_choices",
            "0015_egon_substations",
        ]
        db.cur.execute("""
            SELECT conname, convalidated
            FROM pg_constraint
            WHERE connamespace = 'pylovo'::regnamespace
              AND conname IN (
                'fk_grid_result_transformer_equipment',
                'fk_tp_grid_version',
                'fk_tp_osm_id',
                'fk_pp_line_from_bus',
                'fk_pp_line_to_bus',
                'fk_pp_load_bus',
                'fk_pp_trafo_hv_bus',
                'fk_pp_trafo_lv_bus',
                'fk_lines_result_helper_source_line',
                'fk_lines_result_view_source_line',
                'fk_buildings_result_grid_result'
              )
        """)
        assert len(db.cur.fetchall()) == 11
        db.cur.execute("""
            SELECT relname, relkind FROM pg_class
            WHERE oid IN (
                'pylovo.lines_result_cache'::regclass,
                'pylovo.lines_result_view'::regclass
            ) ORDER BY relname
        """)
        assert db.cur.fetchall() == [
            ("lines_result_cache", "r"), ("lines_result_view", "v")
        ]
        db.cur.execute("""
            SELECT relkind FROM pg_class
            WHERE oid = 'pylovo.buildings_result_with_grid'::regclass
        """)
        assert db.cur.fetchone() == ("v",)
        db.conn.rollback()


def test_pending_migrations_block_grid_writes():
    from pylovo.database.migrations import MIGRATION_NAMES, pending_migrations

    with client() as db:
        assert pending_migrations(db.cur) == []
        db.cur.execute("DELETE FROM pylovo.schema_migrations WHERE name = %s", (MIGRATION_NAMES[-1],))
        assert pending_migrations(db.cur) == [MIGRATION_NAMES[-1]]
        with pytest.raises(RuntimeError, match=MIGRATION_NAMES[-1]):
            db.ensure_grid_persistence_schema()
        db.conn.rollback()


def test_session_staging_is_isolated_and_postcode_lock_serializes():
    with client() as left, client() as right:
        left.create_temp_tables(12345)
        right.create_temp_tables(12345)
        left.commit_changes()
        right.commit_changes()
        left.cur.execute(
            "INSERT INTO ways_tem (way_id, geom) "
            "VALUES (1, ST_SetSRID(ST_MakeLine(ST_MakePoint(0, 0), ST_MakePoint(1, 0)), %s))",
            (TARGET_EPSG,),
        )
        right.cur.execute(
            "INSERT INTO ways_tem (way_id, geom) "
            "VALUES (2, ST_SetSRID(ST_MakeLine(ST_MakePoint(0, 0), ST_MakePoint(0, 1)), %s))",
            (TARGET_EPSG,),
        )
        DatabaseConstructor(left).load_ways_preprocessing_functions()
        left.cur.execute(
            "SELECT insert_way_segment(103, "
            "ST_SetSRID(ST_MakeLine(ST_MakePoint(1, 0), ST_MakePoint(2, 0)), %s))",
            (TARGET_EPSG,),
        )
        left.cur.execute("SELECT way_id FROM ways_tem ORDER BY way_id")
        assert left.cur.fetchall() == [(1,), (2,)]
        right.cur.execute("SELECT way_id FROM ways_tem")
        assert right.cur.fetchall() == [(2,)]
        left.index_and_analyze_staging(12345)
        left.build_pgr_network_topology(12345)
        left.cur.execute("SELECT count(*) FROM ways_tem_vertices_pgr")
        assert left.cur.fetchone() == (3,)
        left.acquire_plz_lock(12345)
        right.cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (907361, 12345))
        assert right.cur.fetchone() == (False,)
        left.release_plz_lock(12345)
        right.cur.execute("SELECT pg_try_advisory_lock(%s, %s)", (907361, 12345))
        assert right.cur.fetchone() == (True,)
        right.release_plz_lock(12345)
        left.drop_temp_tables(12345)
        right.drop_temp_tables(12345)
        left.conn.rollback()
        right.conn.rollback()
        left.cur.execute("SELECT to_regclass('pylovo.ways_tem_12345')")
        assert left.cur.fetchone() == (None,)


def test_cross_grid_references_and_raw_transformer_delete(monkeypatch):
    with client() as db:
        cur = db.cur
        version = "t" + uuid.uuid4().hex[:8]
        polygon = "POLYGON((0 0,0 1,1 1,1 0,0 0))"
        cur.execute("INSERT INTO pylovo.version (version_id) VALUES (%s)", (version,))
        cur.execute(
            """INSERT INTO pylovo.postcode (plz, geom)
               VALUES (12345, ST_Multi(ST_GeomFromText(%s, %s)))""",
            (polygon, TARGET_EPSG),
        )
        cur.execute(
            """INSERT INTO pylovo.postcode_result (version_id, postcode_result_plz, geom)
               VALUES (%s, 12345, ST_Multi(ST_GeomFromText(%s, %s)))""",
            (version, polygon, TARGET_EPSG),
        )
        cur.execute(
            """INSERT INTO pylovo.grid_result (version_id, plz, kcid, bcid)
               VALUES (%s, 12345, 1, 1), (%s, 12345, 2, 1)
               RETURNING grid_result_id""",
            (version, version),
        )
        grid1, grid2 = [row[0] for row in cur.fetchall()]
        cur.execute(
            """INSERT INTO pylovo.equipment_data
               (version_id, name, s_max_kva, typ)
               VALUES (%s, 'Tr_100', 100, 'Transformer')""",
            (version,),
        )
        import pylovo.database.grid_mixin as grid_mixin

        monkeypatch.setattr(grid_mixin, "VERSION_ID", version)
        db.set_transformer_equipment_name(12345, 1, 1, 100)
        cur.execute(
            "SELECT transformer_equipment_name FROM pylovo.grid_result WHERE grid_result_id=%s",
            (grid1,),
        )
        assert cur.fetchone() == ("Tr_100",)
        cur.execute("DELETE FROM pylovo.equipment_data WHERE version_id=%s", (version,))
        cur.execute(
            """SELECT version_id, transformer_equipment_name FROM pylovo.grid_result
               WHERE grid_result_id=%s""",
            (grid1,),
        )
        assert cur.fetchone() == (version, None)

        cur.execute("INSERT INTO pylovo.transformers (osm_id) VALUES ('test/raw')")
        cur.execute(
            """INSERT INTO pylovo.transformer_positions
               (grid_result_id, version_id, osm_id, geom)
               VALUES (%s, %s, 'test/raw', ST_SetSRID(ST_MakePoint(0, 0), %s))""",
            (grid1, version, TARGET_EPSG),
        )
        cur.execute("DELETE FROM pylovo.transformers WHERE osm_id='test/raw'")
        cur.execute(
            """SELECT osm_id, geom IS NOT NULL FROM pylovo.transformer_positions
               WHERE grid_result_id=%s""",
            (grid1,),
        )
        assert cur.fetchone() == (None, True)

        cur.execute(
            """INSERT INTO pylovo.pandapower_bus (grid_result_id, pp_index)
               VALUES (%s, 1), (%s, 2)""",
            (grid1, grid2),
        )
        cur.execute("SAVEPOINT invalid_pp")
        cur.execute(
            """INSERT INTO pylovo.pandapower_line
               (grid_result_id, pp_index, from_bus, to_bus)
               VALUES (%s, 1, 1, 2)""",
            (grid1,),
        )
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute("SET CONSTRAINTS fk_pp_line_to_bus IMMEDIATE")
        cur.execute("ROLLBACK TO SAVEPOINT invalid_pp")

        cur.execute(
            """INSERT INTO pylovo.lines_result (grid_result_id)
               VALUES (%s) RETURNING lines_result_id""",
            (grid1,),
        )
        line_id = cur.fetchone()[0]
        cur.execute(
            """INSERT INTO pylovo.lines_result_cache
               (is_helper, grid_result_id, version_id, plz, kcid, bcid)
               VALUES (false, %s, %s, 12345, 1, 1)""",
            (grid1, version),
        )
        cur.execute(
            "SELECT COUNT(*) FROM pylovo.lines_result_view WHERE grid_result_id=%s",
            (grid1,),
        )
        assert cur.fetchone() == (1,)
        cur.execute("SAVEPOINT invalid_helper")
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cur.execute(
                """INSERT INTO pylovo.lines_result_helper
                   (grid_result_id, source_lines_result_id)
                   VALUES (%s, %s)""",
                (grid2, line_id),
            )
        cur.execute("ROLLBACK TO SAVEPOINT invalid_helper")
        db.conn.rollback()


def test_equipment_catalogue_is_read_for_the_current_version_only(monkeypatch):
    """Another version's rows neither overwrite a cable of the same name nor repeat capacities."""
    import pylovo.database.clustering_mixin as clustering_mixin
    import pylovo.database.grid_mixin as grid_mixin

    with client() as db:
        cur = db.cur
        current, other = ("t" + uuid.uuid4().hex[:8] for _ in range(2))
        for version, cable_a, transformer_kva in ((current, 270, 250), (other, 999, 160)):
            cur.execute("INSERT INTO pylovo.version (version_id) VALUES (%s)", (version,))
            cur.execute(
                """INSERT INTO pylovo.equipment_data
                   (version_id, name, s_max_kva, max_i_a, r_mohm_per_km, x_mohm_per_km, cost_eur, typ)
                   VALUES (%s, 'NAYY_4_150', NULL, %s, 208, 80, 25, 'Cable'),
                          (%s, 'Tr_100', 100, NULL, NULL, NULL, 1, 'Transformer'),
                          (%s, %s, %s, NULL, NULL, NULL, 1, 'Transformer')""",
                (version, cable_a, version, version, f"Tr_{transformer_kva}", transformer_kva),
            )
        monkeypatch.setattr(grid_mixin, "VERSION_ID", current)
        monkeypatch.setattr(clustering_mixin, "VERSION_ID", current)
        # Allows the other version's 160 kVA, so only the version filter keeps it out.
        monkeypatch.setattr(clustering_mixin, "TRANSFORMER_MAPPING", {1: [100, 160, 250]})

        cables = db.fetch_cables()
        assert [(name, float(max_i_ka)) for name, _, _, max_i_ka, _ in cables] == [("NAYY_4_150", 0.27)]
        capacities, _ = db.get_transformer_data(1)
        assert capacities.tolist() == [100, 250]
        db.conn.rollback()



def test_split_points_count_feeder_lines_by_section_not_by_cable_name(monkeypatch):
    """A service cable of a feeder cable type leaving a split node is not an outgoing feeder."""
    import pylovo.database.grid_mixin as grid_mixin

    with client() as db:
        cur = db.cur
        version = "t" + uuid.uuid4().hex[:8]
        polygon = "POLYGON((0 0,0 1,1 1,1 0,0 0))"
        cur.execute("INSERT INTO pylovo.version (version_id) VALUES (%s)", (version,))
        cur.execute(
            """INSERT INTO pylovo.postcode (plz, geom)
               VALUES (12345, ST_Multi(ST_GeomFromText(%s, %s)))""",
            (polygon, TARGET_EPSG),
        )
        cur.execute(
            """INSERT INTO pylovo.postcode_result (version_id, postcode_result_plz, geom)
               VALUES (%s, 12345, ST_Multi(ST_GeomFromText(%s, %s)))""",
            (version, polygon, TARGET_EPSG),
        )
        cur.execute(
            """INSERT INTO pylovo.grid_result (version_id, plz, kcid, bcid, ont_vertice_id)
               VALUES (%s, 12345, 1, 1, 1) RETURNING grid_result_id""",
            (version,),
        )
        grid = cur.fetchone()[0]
        for to_bus, x, section in ((11, 20, 1), (12, 30, 2), (99, 10, None)):
            cur.execute(
                """INSERT INTO pylovo.lines_result (grid_result_id, geom, std_type, from_bus, to_bus, feeder_section_id)
                   VALUES (%s, ST_SetSRID(ST_MakeLine(ST_MakePoint(10, 0), ST_MakePoint(%s, 5)), %s),
                           'NAYY_4_150', 10, %s, %s)""",
                (grid, x, TARGET_EPSG, to_bus, section),
            )
        monkeypatch.setattr(grid_mixin, "VERSION_ID", version)
        db.rebuild_split_points_for_split_topology(12345, 1, 1, [10])
        cur.execute("SELECT split_bus, outgoing_count FROM pylovo.split_points WHERE grid_result_id = %s", (grid,))
        assert cur.fetchall() == [(10, 2)]
        db.conn.rollback()


def test_line_cache_keeps_one_named_row_per_line(monkeypatch):
    """Feeder lines of one section stay separate rows, so GridExpand can find them by line name."""
    import pylovo.database.utils_mixin as utils_mixin

    with client() as db:
        cur = db.cur
        version = "t" + uuid.uuid4().hex[:8]
        polygon = "POLYGON((0 0,0 1,1 1,1 0,0 0))"
        cur.execute("INSERT INTO pylovo.version (version_id) VALUES (%s)", (version,))
        cur.execute(
            """INSERT INTO pylovo.postcode (plz, geom)
               VALUES (12345, ST_Multi(ST_GeomFromText(%s, %s)))""",
            (polygon, TARGET_EPSG),
        )
        cur.execute(
            """INSERT INTO pylovo.postcode_result (version_id, postcode_result_plz, geom)
               VALUES (%s, 12345, ST_Multi(ST_GeomFromText(%s, %s)))""",
            (version, polygon, TARGET_EPSG),
        )
        cur.execute(
            """INSERT INTO pylovo.grid_result (version_id, plz, kcid, bcid, ont_vertice_id)
               VALUES (%s, 12345, 1, 1, 1) RETURNING grid_result_id""",
            (version,),
        )
        grid = cur.fetchone()[0]
        for name, std_type, from_bus, to_bus, section in (
            ("L2", "NAYY_4_150", 1, 2, 1), ("L3", "NAYY_4_150", 2, 3, 1), ("S9", "NAYY_4_50", 3, 9, None),
        ):
            cur.execute(
                """INSERT INTO pylovo.lines_result
                   (grid_result_id, geom, line_name, std_type, from_bus, to_bus, feeder_section_id)
                   VALUES (%s, ST_SetSRID(ST_MakeLine(ST_MakePoint(%s, 0), ST_MakePoint(%s, 0)), %s),
                           %s, %s, %s, %s, %s)""",
                (grid, 10 * from_bus, 10 * to_bus, TARGET_EPSG, name, std_type, from_bus, to_bus, section),
            )
        monkeypatch.setattr(utils_mixin, "VERSION_ID", version)
        db.rebuild_lines_result_view_for_grid(12345, 1, 1)
        cur.execute(
            """SELECT line_name, is_helper, feeder_section_id FROM pylovo.lines_result_cache
               WHERE grid_result_id = %s ORDER BY line_name""",
            (grid,),
        )
        assert cur.fetchall() == [("L2", False, 1), ("L3", False, 1), ("S9", False, None)]
        db.conn.rollback()

def test_transformer_view_reports_unit_and_station_rating():
    """An 800 kVA double station maps to the 400 kVA unit; consumers need the station rating."""
    with client() as db:
        cur = db.cur
        version = "t" + uuid.uuid4().hex[:8]
        polygon = "POLYGON((0 0,0 1,1 1,1 0,0 0))"
        cur.execute("INSERT INTO pylovo.version (version_id) VALUES (%s)", (version,))
        cur.execute("INSERT INTO pylovo.postcode (plz, geom) VALUES (12345, ST_Multi(ST_GeomFromText(%s, %s)))",
                    (polygon, TARGET_EPSG))
        cur.execute("""INSERT INTO pylovo.postcode_result (version_id, postcode_result_plz, geom)
                       VALUES (%s, 12345, ST_Multi(ST_GeomFromText(%s, %s)))""", (version, polygon, TARGET_EPSG))
        cur.execute("""INSERT INTO pylovo.equipment_data (version_id, name, s_max_kva, typ)
                       VALUES (%s, 'Tr_400', 400, 'Transformer')""", (version,))
        cur.execute("""INSERT INTO pylovo.grid_result
                           (version_id, plz, kcid, bcid, transformer_rated_power, transformer_equipment_name)
                       VALUES (%s, 12345, 1, 1, 800, 'Tr_400') RETURNING grid_result_id""", (version,))
        grid = cur.fetchone()[0]
        cur.execute("""INSERT INTO pylovo.transformer_positions (grid_result_id, version_id, geom)
                       VALUES (%s, %s, ST_SetSRID(ST_MakePoint(0, 0), %s))""", (grid, version, TARGET_EPSG))
        cur.execute("""SELECT transformer_rated_power, s_max_kva, transformer_units
                       FROM pylovo.transformer_positions_with_grid WHERE grid_result_id = %s""", (grid,))
        assert cur.fetchone() == (800, 400, 2)
        db.conn.rollback()


def test_reset_refuses_to_drop_external_dependents():
    with client() as db:
        db.cur.execute("CREATE SCHEMA IF NOT EXISTS pylovo_review_external")
        db.cur.execute(
            """CREATE VIEW pylovo_review_external.version_view AS
               SELECT version_id FROM pylovo.version"""
        )
        db.conn.commit()
        try:
            with pytest.raises(RuntimeError, match=r"outside pylovo \(.*pylovo_review_external\.version_view"):
                DatabaseConstructor(db).move_schema_to_backup()     # the first step of pylovo-setup reset
            db.cur.execute("SELECT to_regclass('pylovo.version')")
            assert db.cur.fetchone()[0] is not None
            db.cur.execute("SELECT to_regclass('pylovo_review_external.version_view')")
            assert db.cur.fetchone()[0] is not None
        finally:
            db.conn.rollback()
            db.cur.execute("DROP SCHEMA pylovo_review_external CASCADE")
            db.conn.commit()


def test_transformer_view_is_replaced_in_place_for_external_dependents():
    """GridExpand's QGIS views depend on transformer_positions_with_grid; migrations keep them."""
    from pylovo.database.migrations import drop_rebuilt_views

    with client() as db:
        db.cur.execute("CREATE SCHEMA pylovo_review_gridexpand")
        db.cur.execute("""CREATE MATERIALIZED VIEW pylovo_review_gridexpand.transformer_mv AS
                          SELECT grid_result_id, s_max_kva, geom FROM pylovo.transformer_positions_with_grid""")
        try:
            drop_rebuilt_views(db.cur)
            DatabaseConstructor(db).create_table("transformer_positions_with_grid")
            db.cur.execute("SELECT to_regclass('pylovo_review_gridexpand.transformer_mv')")
            assert db.cur.fetchone()[0] is not None
        finally:
            db.conn.rollback()
            db.cur.execute("DROP SCHEMA IF EXISTS pylovo_review_gridexpand CASCADE")
            db.conn.commit()


def test_blocked_migration_names_the_dependent_and_changes_nothing():
    from psycopg2 import sql

    name = "pylovo_review_" + uuid.uuid4().hex[:8]
    admin = admin_connection()
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with client(name) as db:
            db.cur.execute("CREATE SCHEMA pylovo")
            db.cur.execute("""CREATE TABLE pylovo.buildings_result (
                                  version_id varchar(10) NOT NULL, osm_id text NOT NULL,
                                  grid_result_id bigint NOT NULL, area double precision)""")
            db.cur.execute("""CREATE MATERIALIZED VIEW pylovo.buildings_result_with_grid AS
                              SELECT version_id, osm_id, area FROM pylovo.buildings_result""")
            db.cur.execute("CREATE SCHEMA pylovo_review_external")
            db.cur.execute("""CREATE VIEW pylovo_review_external.building_layer AS
                              SELECT * FROM pylovo.buildings_result_with_grid""")
            db.conn.commit()
            with pytest.raises(RuntimeError, match="0001_legacy_buildings.*pylovo_review_external.building_layer"):
                DatabaseConstructor(db).migrate_schema()
            db.cur.execute("SELECT count(*) FROM pylovo.schema_migrations")
            assert db.cur.fetchone() == (0,)
            db.cur.execute("""SELECT column_name FROM information_schema.columns
                              WHERE table_schema='pylovo' AND table_name='buildings_result'
                                AND column_name IN ('osm_id', 'objectid')""")
            assert db.cur.fetchall() == [("osm_id",)]
            db.conn.rollback()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()


def test_legacy_building_columns_and_key_migrate_without_reset():
    """Exercise the pre-baseline path in a separate disposable database."""
    from psycopg2 import sql

    name = "pylovo_review_" + uuid.uuid4().hex[:8]
    admin = admin_connection()
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with client(name) as db:
            db.cur.execute("CREATE SCHEMA pylovo")
            db.cur.execute("""
                CREATE TABLE pylovo.buildings_result (
                    version_id varchar(10) NOT NULL,
                    osm_id text NOT NULL,
                    grid_result_id bigint NOT NULL,
                    area double precision,
                    CONSTRAINT buildings_result_pkey PRIMARY KEY (version_id, osm_id)
                )
            """)
            db.cur.execute("""
                CREATE MATERIALIZED VIEW pylovo.buildings_result_with_grid AS
                SELECT version_id, osm_id, area FROM pylovo.buildings_result
            """)
            db.conn.commit()
            constructor = DatabaseConstructor(db)
            constructor.migrate_schema()
            db.cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='pylovo' AND table_name='buildings_result'
                  AND column_name IN ('osm_id', 'objectid', 'area', 'floor_area')
                ORDER BY column_name
            """)
            assert [row[0] for row in db.cur.fetchall()] == ["floor_area", "objectid"]
            db.cur.execute("""
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conrelid='pylovo.buildings_result'::regclass
                  AND conname='buildings_result_pkey'
            """)
            assert "(version_id, objectid)" in db.cur.fetchone()[0]
            constructor.migrate_schema()
            constructor.drop_backup(constructor.move_schema_to_backup())     # what pylovo-setup reset does
            db.cur.execute("SELECT to_regnamespace('pylovo')")
            assert db.cur.fetchone() == (None,)
            db.cur.execute(
                "SELECT extname FROM pg_extension WHERE extname IN ('postgis', 'pgrouting')"
            )
            assert {row[0] for row in db.cur.fetchall()} == {"postgis", "pgrouting"}
            db.conn.rollback()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            ))
        admin.close()


def test_reset_backup_is_restored_on_failure_and_dropped_after_success():
    from psycopg2 import sql

    name = "pylovo_review_" + uuid.uuid4().hex[:8]
    admin = admin_connection()
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with client(name) as db:
            constructor = DatabaseConstructor(db)
            constructor.migrate_schema()
            db.cur.execute("INSERT INTO pylovo.version (version_id) VALUES ('keep')")
            db.conn.commit()

            backup = constructor.move_schema_to_backup()
            assert backup.startswith("pylovo_backup_") and constructor.backup_schemas() == [backup]
            constructor.migrate_schema()  # a rebuild that fails later
            constructor.restore_backup(backup)
            db.cur.execute("SELECT version_id FROM pylovo.version")
            assert db.cur.fetchall() == [("keep",)] and constructor.backup_schemas() == []

            backup = constructor.move_schema_to_backup()
            constructor.migrate_schema()  # a rebuild that succeeds
            constructor.drop_backup(backup)
            db.cur.execute("SELECT count(*) FROM pylovo.version")
            assert db.cur.fetchone() == (0,) and constructor.backup_schemas() == []

            db.cur.execute("CREATE SCHEMA pylovo_review_external")
            db.cur.execute("CREATE VIEW pylovo_review_external.versions AS SELECT version_id FROM pylovo.version")
            db.conn.commit()
            with pytest.raises(RuntimeError, match="outside pylovo"):
                constructor.move_schema_to_backup()
            assert constructor.backup_schemas() == []
            db.cur.execute("SELECT to_regclass('pylovo_review_external.versions') IS NOT NULL")
            assert db.cur.fetchone() == (True,)
            db.conn.rollback()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        admin.close()
