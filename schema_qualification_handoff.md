# Schema Qualification Handoff

## Goal

Update local-database SQL across the repository so queries do not rely on `search_path` for the main application schema. Prefer explicit schema qualification in SQL text using `TARGET_SCHEMA`

Do **not** introduce a schema helper abstraction. That approach was previously rejected because the user wants the schema visible directly in the SQL.

## Core Rules

### Qualify local persistent tables

For local permanent objects, use explicit qualification:

- `{TARGET_SCHEMA}.table_name`

### Leave InfDB and external schemas alone

Do not change references such as:

- `basedata.*`
- `opendata.*`
- anything intentionally sourced from `INFDB_SOURCE_SCHEMA`

### Preserve temp alias behavior

Session-local temp aliases should remain unqualified when they are intentionally created as local views, for example:

- `buildings_tem`
- `ways_tem`
- `ways_tem_vertices_pgr`

The underlying persisted PLZ-suffixed tables should be qualified, for example:

- `{TARGET_SCHEMA}.buildings_tem_80805`
- `{TARGET_SCHEMA}.ways_tem_80805`
- `{TARGET_SCHEMA}.ways_tem_80805_vertices_pgr`

### Dynamic helper methods need care

If a helper accepts a table name string, only prefix bare local table names. Preserve:

- already-qualified names
- aliased references like `buildings_result br`
- subqueries
- temp aliases that are meant to stay session-local

## Recommended Order Of Work

2. Sweep executable SQL with targeted search for local-table references and hardcoded `pylovo.` usages.
3. Patch the small, low-risk files first:
   - `src/pylovo/database/config_table_structure.py`
   - `src/pylovo/database/database_constructor.py`
   - `src/pylovo/infdb/infdb_client.py`
   - `src/pylovo/data_import/test_postcodes.py`
4. Then patch the smaller runtime files:
   - `src/pylovo/database/database_client.py`
   - `src/pylovo/database/utils_mixin.py`
   - `src/pylovo/database/analysis_mixin.py`
   - `src/pylovo/analysis/validation_helpers.py`
   - `src/pylovo/analysis/comparison_helpers.py`
5. Then move to medium/large mixins:
   - `src/pylovo/database/grid_mixin.py`
   - `src/pylovo/database/preprocessing_mixin.py`
6. Patch `src/pylovo/database/clustering_mixin.py` last and in very small slices only.

## Known Hotspots By File

### `src/pylovo/database/config_table_structure.py`

- permanent `CREATE TABLE` statements
- `REFERENCES` clauses
- materialized view names and source joins
- index targets
- `TEMP_CREATE_QUERIES` for real persisted PLZ tables
- `REFRESH MATERIALIZED VIEW` statements

### `src/pylovo/database/database_constructor.py`

- `DELETE` and `INSERT` statements for `postcode`
- cleanup queries for CSV imports
- `DELETE` on `transformers`
- insert into `ways`

### `src/pylovo/infdb/infdb_client.py`

- qualify local reads such as `ways_result`
- do not touch `basedata.*` or `opendata.*`

### `src/pylovo/data_import/test_postcodes.py`

- local insert into `postcode`

### `src/pylovo/database/database_client.py`

- `save_tables` inserts into `buildings_result` and `ways_result`
- join to `grid_result`
- PLZ-suffixed persisted tables in `save_tables`
- delete helpers for `postcode_result`, `version`, `classification_version`, `sample_set`
- truncate `transformers`
- insert into `ags_log`
- `is_table_empty` if it qualifies bare names by default must not break temp aliases or already-qualified input

### `src/pylovo/database/utils_mixin.py`

- temp table lifecycle for real PLZ tables
- runtime reads from `grid_result`, `consumer_categories`, `municipal_register`

### `src/pylovo/database/analysis_mixin.py`

- `plz_parameters` inserts and updates
- `grid_result` updates and reads
- `clustering_parameters` inserts and joins
- dynamic helpers `get_geo_df` and `get_geo_df_join`
- `get_grids_from_plz`

### `src/pylovo/analysis/validation_helpers.py`

- export query selecting `plz`, `kcid`, `bcid` from `grid_result`

### `src/pylovo/analysis/comparison_helpers.py`

- reads from `grid_result`
- joins `buildings_result` to `grid_result`
- writes and resets `grid_parameters`

### `src/pylovo/database/grid_mixin.py`

- `equipment_data`
- `grid_result`
- `transformer_positions`
- `ways`
- `lines_result`
- `postcode_result`

### `src/pylovo/database/preprocessing_mixin.py`

- large SQL surface including known local-schema references such as `postcode`
- persisted PLZ-specific tables and routing-related SQL need care

### `src/pylovo/database/clustering_mixin.py`

- heavy concentration of `grid_result`, `transformers`, `transformer_positions`, `equipment_data`, `consumer_categories`, `postcode_result`, and related local-table queries

## Validation Strategy

After each small file or tightly-related file group:

1. Run diagnostics for only the touched files.
2. Run focused compile validation with the local venv:

```bash
source .venv/bin/activate
python -m py_compile <touched files>
```

Validate after each slice before widening scope.

## Important Failure Mode From Prior Attempt

A previous attempt introduced repeated indentation defects in multiline triple-quoted SQL, especially in larger mixins. The practical lessons are:

- patch in very small slices
- immediately re-read touched multiline blocks after editing
- if a small file starts drifting, prefer rewriting that file atomically over stacking more micro-fixes
- leave `clustering_mixin.py` for last

## Search Targets

Use targeted searches for:

- `pylovo.`
- `FROM`
- `JOIN`
- `INSERT INTO`
- `UPDATE`
- `DELETE FROM`
- `TRUNCATE TABLE`

Common local tables to sweep:

- `grid_result`
- `buildings_result`
- `ways_result`
- `postcode`
- `postcode_result`
- `plz_parameters`
- `clustering_parameters`
- `grid_parameters`
- `transformer_positions`
- `transformers`
- `consumer_categories`
- `municipal_register`
- `equipment_data`
- `sample_set`
- `version`
- `classification_version`

## Definition Of Done

The task is done when:

- all executable local-schema SQL is explicitly qualified where needed
- external InfDB schemas remain unchanged
- temp alias semantics are preserved
- touched files pass focused compile validation
- no new indentation or formatting regressions were introduced while patching multiline SQL