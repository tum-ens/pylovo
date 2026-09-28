"""Ordered, transactional migrations for existing PyLovo schemas.

Run through DatabaseConstructor.migrate_schema. A failed migration rolls back and is
not recorded. Existing data is validated before stronger constraints are committed.
"""
from __future__ import annotations

from collections.abc import Callable

from psycopg2.extensions import cursor


def drop_rebuilt_views(cur: cursor) -> None:
    """Drop, without CASCADE, only the views that the baseline must rebuild.

    These are the old materialized views, and transformer_positions_with_grid while
    transformer_positions still lacks columns that change ``tp.*``. Otherwise CREATE OR
    REPLACE VIEW keeps views of other schemas (e.g. GridExpand's QGIS views) that depend on it.
    """
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_class
                       WHERE oid = to_regclass('pylovo.lines_result_with_grid') AND relkind = 'm') THEN
                DROP MATERIALIZED VIEW pylovo.lines_result_with_grid;
            END IF;
            IF EXISTS (SELECT 1 FROM pg_class
                       WHERE oid = to_regclass('pylovo.buildings_result_with_grid') AND relkind = 'm') THEN
                DROP MATERIALIZED VIEW pylovo.buildings_result_with_grid;
            END IF;
            IF to_regclass('pylovo.transformer_positions_with_grid') IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM unnest(ARRAY['osm', 'lod2', 'lod2_objectid']) AS required(name)
                   WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns
                                     WHERE table_schema='pylovo' AND table_name='transformer_positions'
                                       AND column_name = required.name)
               ) THEN
                DROP VIEW pylovo.transformer_positions_with_grid;
            END IF;
        END $$;
    """)


def legacy_buildings(cur: cursor) -> None:
    """Move the old building column renames out of the create-table definitions."""
    drop_rebuilt_views(cur)
    cur.execute("""
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


def rename_line_cache(cur: cursor) -> None:
    """Keep all stored line rows while giving the physical cache an accurate name."""
    cur.execute("""
        DO $$
        BEGIN
            IF to_regclass('pylovo.lines_result_cache') IS NULL
               AND EXISTS (
                   SELECT 1 FROM pg_class
                   WHERE oid = to_regclass('pylovo.lines_result_view')
                     AND relkind IN ('r', 'p')
               ) THEN
                ALTER TABLE pylovo.lines_result_view RENAME TO lines_result_cache;
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
    "lines_result_cache": {"feeder_section_id": "integer"},
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


def integrity(cur: cursor) -> None:
    """Fix deletion semantics and bind every network reference to its own grid."""
    cur.execute("""
        ALTER TABLE pylovo.grid_result
            DROP CONSTRAINT IF EXISTS fk_grid_result_transformer_equipment;
        ALTER TABLE pylovo.grid_result
            ADD CONSTRAINT fk_grid_result_transformer_equipment
            FOREIGN KEY (version_id, transformer_equipment_name)
            REFERENCES pylovo.equipment_data(version_id, name)
            ON DELETE SET NULL (transformer_equipment_name) NOT VALID;
        ALTER TABLE pylovo.grid_result
            VALIDATE CONSTRAINT fk_grid_result_transformer_equipment;

        UPDATE pylovo.transformer_positions tp
        SET version_id = gr.version_id
        FROM pylovo.grid_result gr
        WHERE tp.grid_result_id = gr.grid_result_id AND tp.version_id IS NULL;
        ALTER TABLE pylovo.transformer_positions ALTER COLUMN version_id SET NOT NULL;
        ALTER TABLE pylovo.transformer_positions
            DROP CONSTRAINT IF EXISTS fk_tp_version_id;
        ALTER TABLE pylovo.transformer_positions
            DROP CONSTRAINT IF EXISTS fk_tp_grid_result_id;
        ALTER TABLE pylovo.transformer_positions
            DROP CONSTRAINT IF EXISTS fk_tp_osm_id;
        ALTER TABLE pylovo.transformer_positions
            ADD CONSTRAINT fk_tp_grid_version
            FOREIGN KEY (version_id, grid_result_id)
            REFERENCES pylovo.grid_result(version_id, grid_result_id)
            ON DELETE CASCADE NOT VALID;
        ALTER TABLE pylovo.transformer_positions
            VALIDATE CONSTRAINT fk_tp_grid_version;
        ALTER TABLE pylovo.transformer_positions
            ADD CONSTRAINT fk_tp_osm_id FOREIGN KEY (osm_id)
            REFERENCES pylovo.transformers(osm_id)
            ON DELETE SET NULL NOT VALID;
        ALTER TABLE pylovo.transformer_positions
            VALIDATE CONSTRAINT fk_tp_osm_id;

        ALTER TABLE pylovo.lines_result
            ADD CONSTRAINT uq_lines_result_grid_id UNIQUE (grid_result_id, lines_result_id);
        ALTER TABLE pylovo.lines_result_helper
            DROP CONSTRAINT IF EXISTS fk_lines_result_helper_source_line;
        ALTER TABLE pylovo.lines_result_helper
            ADD CONSTRAINT fk_lines_result_helper_source_line
            FOREIGN KEY (grid_result_id, source_lines_result_id)
            REFERENCES pylovo.lines_result(grid_result_id, lines_result_id)
            ON DELETE CASCADE NOT VALID;
        ALTER TABLE pylovo.lines_result_helper
            VALIDATE CONSTRAINT fk_lines_result_helper_source_line;
        ALTER TABLE pylovo.lines_result_cache
            DROP CONSTRAINT IF EXISTS fk_lines_result_view_source_line;
        ALTER TABLE pylovo.lines_result_cache
            ADD CONSTRAINT fk_lines_result_view_source_line
            FOREIGN KEY (grid_result_id, source_lines_result_id)
            REFERENCES pylovo.lines_result(grid_result_id, lines_result_id)
            ON DELETE CASCADE NOT VALID;
        ALTER TABLE pylovo.lines_result_cache
            VALIDATE CONSTRAINT fk_lines_result_view_source_line;

        ALTER TABLE pylovo.pandapower_line
            ADD CONSTRAINT fk_pp_line_from_bus FOREIGN KEY (grid_result_id, from_bus)
            REFERENCES pylovo.pandapower_bus(grid_result_id, pp_index)
            DEFERRABLE INITIALLY DEFERRED NOT VALID;
        ALTER TABLE pylovo.pandapower_line VALIDATE CONSTRAINT fk_pp_line_from_bus;
        ALTER TABLE pylovo.pandapower_line
            ADD CONSTRAINT fk_pp_line_to_bus FOREIGN KEY (grid_result_id, to_bus)
            REFERENCES pylovo.pandapower_bus(grid_result_id, pp_index)
            DEFERRABLE INITIALLY DEFERRED NOT VALID;
        ALTER TABLE pylovo.pandapower_line VALIDATE CONSTRAINT fk_pp_line_to_bus;
        ALTER TABLE pylovo.pandapower_load
            ADD CONSTRAINT fk_pp_load_bus FOREIGN KEY (grid_result_id, bus)
            REFERENCES pylovo.pandapower_bus(grid_result_id, pp_index)
            DEFERRABLE INITIALLY DEFERRED NOT VALID;
        ALTER TABLE pylovo.pandapower_load VALIDATE CONSTRAINT fk_pp_load_bus;
        ALTER TABLE pylovo.pandapower_trafo
            ADD CONSTRAINT fk_pp_trafo_hv_bus FOREIGN KEY (grid_result_id, hv_bus)
            REFERENCES pylovo.pandapower_bus(grid_result_id, pp_index)
            DEFERRABLE INITIALLY DEFERRED NOT VALID;
        ALTER TABLE pylovo.pandapower_trafo VALIDATE CONSTRAINT fk_pp_trafo_hv_bus;
        ALTER TABLE pylovo.pandapower_trafo
            ADD CONSTRAINT fk_pp_trafo_lv_bus FOREIGN KEY (grid_result_id, lv_bus)
            REFERENCES pylovo.pandapower_bus(grid_result_id, pp_index)
            DEFERRABLE INITIALLY DEFERRED NOT VALID;
        ALTER TABLE pylovo.pandapower_trafo VALIDATE CONSTRAINT fk_pp_trafo_lv_bus;

        ALTER TABLE pylovo.sample_set ALTER COLUMN ags SET NOT NULL;
    """)


def backfill_equipment(cur: cursor) -> None:
    """Map only known standard station configurations to a unique catalog row."""
    cur.execute("""
        WITH candidates AS (
            SELECT gr.grid_result_id, MIN(ed.name) AS name, COUNT(*) AS matches
            FROM pylovo.grid_result gr
            JOIN pylovo.equipment_data ed ON ed.version_id = gr.version_id
                AND ed.typ = 'Transformer'
                AND ed.s_max_kva = CASE
                    WHEN gr.transformer_rated_power IN (500, 800, 1260)
                        THEN gr.transformer_rated_power / 2
                    ELSE gr.transformer_rated_power
                END
            WHERE gr.transformer_equipment_name IS NULL
              AND gr.transformer_rated_power IN (100, 160, 250, 400, 500, 630, 800, 1260)
            GROUP BY gr.grid_result_id
        )
        UPDATE pylovo.grid_result gr SET transformer_equipment_name = c.name
        FROM candidates c WHERE gr.grid_result_id = c.grid_result_id AND c.matches = 1;
    """)


def indexes_and_checks(cur: cursor) -> None:
    """Add measured search support and checks whose semantics are unambiguous."""
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_postcode_geom
            ON pylovo.postcode USING gist (geom);
        CREATE INDEX IF NOT EXISTS idx_buildings_result_geom
            ON pylovo.buildings_result USING gist (geom);
        CREATE INDEX IF NOT EXISTS idx_transformer_positions_geom
            ON pylovo.transformer_positions USING gist (geom);
        ALTER TABLE pylovo.equipment_data
            ADD CONSTRAINT chk_equipment_positive_rating CHECK (s_max_kva IS NULL OR s_max_kva > 0) NOT VALID;
        ALTER TABLE pylovo.equipment_data VALIDATE CONSTRAINT chk_equipment_positive_rating;
        ALTER TABLE pylovo.equipment_data
            ADD CONSTRAINT chk_equipment_positive_ampacity CHECK (max_i_a IS NULL OR max_i_a > 0) NOT VALID;
        ALTER TABLE pylovo.equipment_data VALIDATE CONSTRAINT chk_equipment_positive_ampacity;
        ALTER TABLE pylovo.lines_result
            ADD CONSTRAINT chk_lines_result_length CHECK (length_km IS NULL OR length_km >= 0) NOT VALID;
        ALTER TABLE pylovo.lines_result VALIDATE CONSTRAINT chk_lines_result_length;
        ALTER TABLE pylovo.lines_result
            ADD CONSTRAINT chk_lines_result_parallel CHECK (parallel IS NULL OR parallel > 0) NOT VALID;
        ALTER TABLE pylovo.lines_result VALIDATE CONSTRAINT chk_lines_result_parallel;
        ALTER TABLE pylovo.pandapower_line
            ADD CONSTRAINT chk_pp_line_length CHECK (length_km IS NULL OR length_km >= 0) NOT VALID;
        ALTER TABLE pylovo.pandapower_line VALIDATE CONSTRAINT chk_pp_line_length;
        ALTER TABLE pylovo.pandapower_line
            ADD CONSTRAINT chk_pp_line_parallel CHECK (parallel IS NULL OR parallel > 0) NOT VALID;
        ALTER TABLE pylovo.pandapower_line VALIDATE CONSTRAINT chk_pp_line_parallel;
        ALTER TABLE pylovo.buildings_result
            ADD CONSTRAINT chk_building_peak_load CHECK (peak_load_in_kw IS NULL OR peak_load_in_kw >= 0) NOT VALID;
        ALTER TABLE pylovo.buildings_result VALIDATE CONSTRAINT chk_building_peak_load;
        ALTER TABLE pylovo.postcode
            ADD CONSTRAINT chk_postcode_geom CHECK (geom IS NOT NULL) NOT VALID;
        ALTER TABLE pylovo.postcode VALIDATE CONSTRAINT chk_postcode_geom;
        ALTER TABLE pylovo.buildings_result
            ADD CONSTRAINT chk_buildings_result_geom CHECK (geom IS NOT NULL) NOT VALID;
        ALTER TABLE pylovo.buildings_result VALIDATE CONSTRAINT chk_buildings_result_geom;
        ALTER TABLE pylovo.ways_result
            ADD CONSTRAINT chk_ways_result_geom CHECK (geom IS NOT NULL) NOT VALID;
        ALTER TABLE pylovo.ways_result VALIDATE CONSTRAINT chk_ways_result_geom;
        ALTER TABLE pylovo.transformer_positions
            ADD CONSTRAINT chk_transformer_positions_geom CHECK (geom IS NOT NULL) NOT VALID;
        ALTER TABLE pylovo.transformer_positions VALIDATE CONSTRAINT chk_transformer_positions_geom;
    """)


def legacy_building_fk(cur: cursor) -> None:
    """Restore the composite building-to-grid FK on pre-baseline tables."""
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'pylovo.buildings_result'::regclass
                  AND conname = 'fk_buildings_result_grid_result'
            ) THEN
                ALTER TABLE pylovo.buildings_result
                    ADD CONSTRAINT fk_buildings_result_grid_result
                    FOREIGN KEY (version_id, grid_result_id)
                    REFERENCES pylovo.grid_result(version_id, grid_result_id)
                    ON DELETE CASCADE NOT VALID;
                ALTER TABLE pylovo.buildings_result
                    VALIDATE CONSTRAINT fk_buildings_result_grid_result;
            END IF;
        END $$;
    """)


def convert_buildings_view(cur: cursor) -> None:
    """Replace the refresh-heavy building copy with a live join view.

    RESTRICT is intentional: an external SQL dependency must be reviewed first.
    """
    cur.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_class
                WHERE oid = to_regclass('pylovo.buildings_result_with_grid')
                  AND relkind = 'm'
            ) THEN
                DROP MATERIALIZED VIEW pylovo.buildings_result_with_grid;
            END IF;
        END $$;
    """)


def line_cache_compatibility_view(cur: cursor) -> None:
    """Retain the established read name for QGIS and API consumers."""
    cur.execute("""
        CREATE OR REPLACE VIEW pylovo.lines_result_view AS
        SELECT * FROM pylovo.lines_result_cache
    """)


def percentage_checks(cur: cursor) -> None:
    """Validate fractions and percentages using their documented units."""
    cur.execute("""
        ALTER TABLE pylovo.consumer_categories
            ADD CONSTRAINT chk_consumer_sim_factor
            CHECK (sim_factor BETWEEN 0 AND 1) NOT VALID;
        ALTER TABLE pylovo.consumer_categories
            VALIDATE CONSTRAINT chk_consumer_sim_factor;
        ALTER TABLE pylovo.pandapower_load
            ADD CONSTRAINT chk_pp_load_const_z_percent
            CHECK (const_z_percent IS NULL OR const_z_percent BETWEEN 0 AND 100) NOT VALID;
        ALTER TABLE pylovo.pandapower_load
            VALIDATE CONSTRAINT chk_pp_load_const_z_percent;
        ALTER TABLE pylovo.pandapower_load
            ADD CONSTRAINT chk_pp_load_const_i_percent
            CHECK (const_i_percent IS NULL OR const_i_percent BETWEEN 0 AND 100) NOT VALID;
        ALTER TABLE pylovo.pandapower_load
            VALIDATE CONSTRAINT chk_pp_load_const_i_percent;
        ALTER TABLE pylovo.pandapower_trafo
            ADD CONSTRAINT chk_pp_trafo_vk_percent
            CHECK (vk_percent IS NULL OR vk_percent BETWEEN 0 AND 100) NOT VALID;
        ALTER TABLE pylovo.pandapower_trafo
            VALIDATE CONSTRAINT chk_pp_trafo_vk_percent;
        ALTER TABLE pylovo.pandapower_trafo
            ADD CONSTRAINT chk_pp_trafo_vkr_percent
            CHECK (vkr_percent IS NULL OR vkr_percent BETWEEN 0 AND 100) NOT VALID;
        ALTER TABLE pylovo.pandapower_trafo
            VALIDATE CONSTRAINT chk_pp_trafo_vkr_percent;
        ALTER TABLE pylovo.pandapower_trafo
            ADD CONSTRAINT chk_pp_trafo_i0_percent
            CHECK (i0_percent IS NULL OR i0_percent BETWEEN 0 AND 100) NOT VALID;
        ALTER TABLE pylovo.pandapower_trafo
            VALIDATE CONSTRAINT chk_pp_trafo_i0_percent;
        ALTER TABLE pylovo.pandapower_trafo
            ADD CONSTRAINT chk_pp_trafo_parallel
            CHECK (parallel IS NULL OR parallel > 0) NOT VALID;
        ALTER TABLE pylovo.pandapower_trafo
            VALIDATE CONSTRAINT chk_pp_trafo_parallel;
    """)


PRE_SCHEMA_MIGRATIONS: tuple[tuple[str, Callable[[cursor], None]], ...] = (
    ("0001_legacy_buildings", legacy_buildings),
    ("0001a_line_cache_rename", rename_line_cache),
    ("0001b_buildings_regular_view", convert_buildings_view),
)
POST_SCHEMA_MIGRATIONS: tuple[tuple[str, Callable[[cursor], None]], ...] = (
    ("0003_integrity", integrity),
    ("0004_equipment_backfill", backfill_equipment),
    ("0005_indexes_checks", indexes_and_checks),
    ("0006_legacy_building_fk", legacy_building_fk),
    ("0007_line_cache_compatibility_view", line_cache_compatibility_view),
    ("0008_percentage_checks", percentage_checks),
)
