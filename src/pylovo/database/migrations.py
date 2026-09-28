"""Ordered, transactional migrations for existing PyLovo schemas.

Run through DatabaseConstructor.migrate_schema. A failed migration rolls back and is
not recorded. Existing data is validated before stronger constraints are committed.
"""
from __future__ import annotations

from collections.abc import Callable

from psycopg2.extensions import cursor


def legacy_buildings(cur: cursor) -> None:
    """Move the old building column renames out of the create-table definitions."""
    cur.execute("""
        DROP MATERIALIZED VIEW IF EXISTS pylovo.lines_result_with_grid;
        DROP MATERIALIZED VIEW IF EXISTS pylovo.buildings_result_with_grid;
        DROP VIEW IF EXISTS pylovo.transformer_positions_with_grid;
        DO $$
        BEGIN
            IF to_regclass('pylovo.buildings_result') IS NOT NULL THEN
                ALTER TABLE pylovo.buildings_result DROP CONSTRAINT IF EXISTS fk_buildings_result_type;
                ALTER TABLE pylovo.buildings_result DROP CONSTRAINT IF EXISTS buildings_result_pkey;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema='pylovo' AND table_name='buildings_result'
                             AND column_name='osm_id')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_schema='pylovo' AND table_name='buildings_result'
                                     AND column_name='objectid') THEN
                    ALTER TABLE pylovo.buildings_result RENAME COLUMN osm_id TO objectid;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema='pylovo' AND table_name='buildings_result'
                             AND column_name='area')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_schema='pylovo' AND table_name='buildings_result'
                                     AND column_name='floor_area') THEN
                    ALTER TABLE pylovo.buildings_result RENAME COLUMN area TO floor_area;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema='pylovo' AND table_name='buildings_result'
                             AND column_name='floors')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_schema='pylovo' AND table_name='buildings_result'
                                     AND column_name='floor_number') THEN
                    ALTER TABLE pylovo.buildings_result RENAME COLUMN floors TO floor_number;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema='pylovo' AND table_name='buildings_result'
                             AND column_name='households_per_building')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_schema='pylovo' AND table_name='buildings_result'
                                     AND column_name='households') THEN
                    ALTER TABLE pylovo.buildings_result
                        RENAME COLUMN households_per_building TO households;
                END IF;
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema='pylovo' AND table_name='buildings_result'
                             AND column_name='center')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_schema='pylovo' AND table_name='buildings_result'
                                     AND column_name='centroid') THEN
                    ALTER TABLE pylovo.buildings_result RENAME COLUMN center TO centroid;
                END IF;
            END IF;
        END $$;
    """)


# The legacy create-table definitions used ADD COLUMN IF NOT EXISTS as an informal
# migration. Keep the explicit list here so the baseline DDL describes only new tables.
LEGACY_COLUMNS = {
    "version": {"generation_parameters": "jsonb"},
    "grid_result": {
        "ampacity_max_feeder_voltage_drop_percent": "double precision",
        "selected_max_feeder_voltage_drop_percent": "double precision",
        "feeder_voltage_drop_limit_met": "boolean",
        "ampacity_max_service_voltage_drop_percent": "double precision",
        "selected_max_service_voltage_drop_percent": "double precision",
        "service_voltage_drop_limit_met": "boolean",
        "service_voltage_upgraded_count": "integer",
        "long_service_connection_count": "integer",
        "max_total_design_voltage_drop_percent": "double precision",
        "max_feeder_voltage_drop_pu": "double precision",
        "max_service_voltage_drop_pu": "double precision",
        "max_total_lv_voltage_drop_pu": "double precision",
    },
    "lines_result": {"feeder_section_id": "integer"},
    "lines_result_view": {"feeder_section_id": "integer"},
    "buildings_result": {
        "id": "integer", "feature_id": "integer", "objectid": "text",
        "height": "double precision", "floor_area": "double precision",
        "floor_number": "integer", "residential_floor_area": "double precision",
        "nonresidential_floor_area": "double precision",
        "nonresidential_use": "varchar(30)", "mix_score": "double precision",
        "mix_rule": "text", "mix_confidence": "text", "building_use": "text",
        "building_use_id": "text", "building_type": "text", "type": "varchar(30)",
        "occupants": "integer", "households": "integer", "construction_year": "text",
        "postcode": "integer", "address_street_id": "bigint", "street": "text",
        "house_number": "text", "geom": "geometry(MultiPolygon,{epsg})",
        "centroid": "geometry(Point,{epsg})", "gemeindeschluessel": "text",
        "changelog_id": "bigint", "assigned_way_id": "text",
        "residential_peak_load_in_kw": "double precision",
        "nonresidential_peak_load_in_kw": "double precision",
        "nonresidential_mv_direct": "boolean NOT NULL DEFAULT false",
        "peak_load_in_kw": "double precision", "vertice_id": "integer",
        "connection_point": "integer", "agg_connection_point": "integer",
    },
    "pandapower_line": {
        "feeder_section_id": "integer", "feeder_sizing_basis": "varchar(32)",
        "ampacity_std_type": "varchar(100)", "ampacity_parallel": "integer",
        "service_sizing_basis": "varchar(32)",
        "service_ampacity_voltage_drop_percent": "double precision",
        "service_selected_voltage_drop_percent": "double precision",
        "service_voltage_drop_limit_met": "boolean", "service_length_review": "boolean",
        "total_design_voltage_drop_percent": "double precision",
    },
    "pandapower_load": {
        "category": "varchar(30)", "load_units": "double precision",
        "consumer_vertex": "bigint", "service_design_p_mw": "double precision",
        "operating_point_basis": "varchar(64)",
    },
    "transformers": {
        "osm": "boolean NOT NULL DEFAULT true", "lod2": "boolean NOT NULL DEFAULT false",
        "lod2_objectid": "text",
    },
    "transformer_positions": {
        "osm": "boolean NOT NULL DEFAULT false", "lod2": "boolean NOT NULL DEFAULT false",
        "lod2_objectid": "text",
    },
}


def legacy_columns(cur: cursor, epsg: int) -> None:
    """Add historical columns before views and constraints are created."""
    from psycopg2 import sql

    for table, columns in LEGACY_COLUMNS.items():
        cur.execute("SELECT to_regclass(%s)", (f"pylovo.{table}",))
        if cur.fetchone()[0] is None:
            continue
        for name, data_type in columns.items():
            cur.execute(sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} " + data_type.format(epsg=epsg)).format(
                sql.Identifier("pylovo", table), sql.Identifier(name)
            ))
    cur.execute("""
        DO $$
        BEGIN
            IF to_regclass('pylovo.buildings_result') IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM pg_constraint
                               WHERE conrelid=to_regclass('pylovo.buildings_result')
                                 AND conname='buildings_result_pkey') THEN
                ALTER TABLE pylovo.buildings_result
                    ADD CONSTRAINT buildings_result_pkey PRIMARY KEY (version_id, objectid);
            END IF;
        END $$;
    """)


PRE_SCHEMA_MIGRATIONS: tuple[tuple[str, Callable[[cursor], None]], ...] = (
    ("0001_legacy_buildings", legacy_buildings),
)
POST_SCHEMA_MIGRATIONS: tuple[tuple[str, Callable[[cursor], None]], ...] = (
)
