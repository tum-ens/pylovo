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
            "0002_legacy_columns",
        ]
        db.conn.rollback()


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
            db.conn.rollback()
    finally:
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(name)
            ))
        admin.close()
