"""Opt-in integration checks for the schema review; run only on a disposable database."""
from __future__ import annotations

import os
import uuid

import psycopg2
import pytest

from pylovo.config_loader import TARGET_EPSG
from pylovo.database.database_client import DatabaseClient
from pylovo.database.database_constructor import DatabaseConstructor

TEST_PORT = os.getenv("PYLOVO_REVIEW_TEST_PORT")
pytestmark = pytest.mark.skipif(
    not TEST_PORT, reason="set PYLOVO_REVIEW_TEST_PORT for an isolated PostgreSQL/PostGIS instance"
)


def client() -> DatabaseClient:
    return DatabaseClient(
        dbname="postgres", user="postgres", pw="", host="127.0.0.1", port=int(TEST_PORT)
    )


def test_migrations_are_repeatable_and_catalog_is_valid():
    with client() as db:
        constructor = DatabaseConstructor(db)
        constructor.migrate_schema()
        db.cur.execute("SELECT name FROM pylovo.schema_migrations ORDER BY name")
        assert [row[0] for row in db.cur.fetchall()] == [
            "0001_legacy_buildings",
            "0001a_line_cache_rename",
            "0002_legacy_columns",
            "0007_line_cache_compatibility_view",
        ]
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


def test_reset_refuses_to_drop_external_dependents():
    with client() as db:
        db.cur.execute("CREATE SCHEMA IF NOT EXISTS pylovo_review_external")
        db.cur.execute(
            """CREATE VIEW pylovo_review_external.version_view AS
               SELECT version_id FROM pylovo.version"""
        )
        db.conn.commit()
        try:
            with pytest.raises(RuntimeError, match="outside pylovo"):
                DatabaseConstructor(db).reset_schema()
            db.cur.execute("SELECT to_regclass('pylovo.version')")
            assert db.cur.fetchone()[0] is not None
            db.cur.execute("SELECT to_regclass('pylovo_review_external.version_view')")
            assert db.cur.fetchone()[0] is not None
        finally:
            db.conn.rollback()
            db.cur.execute("DROP SCHEMA pylovo_review_external CASCADE")
            db.conn.commit()


def test_legacy_building_columns_and_key_migrate_without_reset():
    """Exercise the pre-baseline path in a separate disposable database."""
    from psycopg2 import sql

    name = "pylovo_review_" + uuid.uuid4().hex[:8]
    admin = psycopg2.connect(
        dbname="postgres", user="postgres", host="127.0.0.1", port=int(TEST_PORT)
    )
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        with DatabaseClient(
            dbname=name, user="postgres", pw="", host="127.0.0.1", port=int(TEST_PORT)
        ) as db:
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
            constructor.reset_schema()
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
