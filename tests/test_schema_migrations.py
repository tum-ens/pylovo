"""DB-free checks of DatabaseConstructor.migrate_schema with a recording connection."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from pylovo.database.config_table_structure import CREATE_QUERIES
from pylovo.database.database_constructor import DatabaseConstructor

INDEX = re.compile(r"CREATE (?:UNIQUE )?INDEX IF NOT EXISTS (\w+) ON pylovo\.(\w+)(?: USING \w+)? \(([^)]*)\)")


class Cursor:
    """Records every statement; answers the server version query, finds no applied migration."""

    def __init__(self, conn):
        self.conn, self.last = conn, ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.last = " ".join(str(query).split())
        self.conn.log.append(self.last)

    def fetchone(self):
        return self.conn.server if "server_version_num" in self.last else None


class Connection:
    def __init__(self, server=(170006, "17.6")):
        self.server, self.log = server, []

    def cursor(self):
        return Cursor(self)

    def commit(self):
        self.log.append("COMMIT")

    def rollback(self):
        self.log.append("ROLLBACK")


def test_setup_refuses_postgresql_before_15_before_any_change():
    """ON DELETE SET NULL (column) is PostgreSQL 15 syntax; older servers fail in the middle of the DDL."""
    conn = Connection(server=(140013, "14.13 (Ubuntu 14.13-0ubuntu0.22.04.1)"))
    with pytest.raises(RuntimeError, match=r"PostgreSQL 15 or newer, the server runs PostgreSQL 14\.13 "):
        DatabaseConstructor(SimpleNamespace(conn=conn)).migrate_schema()
    assert len(conn.log) == 1 and "server_version_num" in conn.log[0]
    DatabaseConstructor(SimpleNamespace(conn=Connection(server=(150000, "15.0")))).check_server_version()


def test_migration_on_violating_rows_stops_with_the_setup_message(monkeypatch):
    """A VALIDATE step that meets bad rows ends in "Migration X stopped" (setup prints it), not a traceback."""
    import psycopg2.errors

    from pylovo.database import database_constructor

    class Violation(psycopg2.errors.ForeignKeyViolation):
        diag = SimpleNamespace(
            message_primary='insert or update on table "pandapower_line" violates foreign key constraint '
                            '"fk_pp_line_from_bus"',
            message_detail='Key (grid_result_id, from_bus)=(5, 3) is not present in table "pandapower_bus".')

    def bad_rows(cur):
        raise Violation()

    monkeypatch.setattr(database_constructor, "PRE_SCHEMA_MIGRATIONS", (("0003_integrity", bad_rows),))
    conn = Connection()
    with pytest.raises(RuntimeError) as stopped:
        DatabaseConstructor(SimpleNamespace(conn=conn)).migrate_schema()
    assert str(stopped.value) == (
        'Migration 0003_integrity stopped, nothing was changed: insert or update on table "pandapower_line" '
        'violates foreign key constraint "fk_pp_line_from_bus". Key (grid_result_id, from_bus)=(5, 3) is not '
        'present in table "pandapower_bus". Repair or delete these rows, then run pylovo-setup again.')
    assert "ROLLBACK" in conn.log and not any("INSERT INTO pylovo.schema_migrations" in q for q in conn.log)


def _statements(action) -> list[str]:
    """The statements a migration step sends, whitespace collapsed."""
    conn = Connection()
    action(Cursor(conn))
    return [" ".join(part.split()) for query in conn.log for part in query.split(";") if part.strip()]


def _indexes(statements) -> dict[str, tuple[str, tuple[str, ...]]]:
    """Index name -> (table, columns) of the CREATE INDEX statements."""
    return {name: (table, tuple(column.strip() for column in columns.split(",")))
            for name, table, columns in (INDEX.fullmatch(s).groups() for s in statements if INDEX.fullmatch(s))}


def _baseline_indexes() -> dict[str, tuple[str, tuple[str, ...]]]:
    return _indexes(" ".join(re.sub(r"--[^\n]*", "", query).split()).strip()
                    for queries in CREATE_QUERIES.values() for query in queries.split(";"))


def test_migration_0012_leaves_migrated_schemas_with_the_indexes_of_a_fresh_setup():
    """A fresh setup runs the baseline and then every migration, an old schema the same: both must match."""
    from pylovo.database.migrations import MIGRATION_NAMES, POST_SCHEMA_MIGRATIONS, indexes_and_cleanup

    assert dict(POST_SCHEMA_MIGRATIONS)["0012_indexes"] is indexes_and_cleanup and "0012_indexes" in MIGRATION_NAMES
    statements = _statements(indexes_and_cleanup)
    created = _indexes(statements)
    dropped = {m.group(1) for m in (re.fullmatch(r"DROP INDEX IF EXISTS pylovo\.(\w+)", s) for s in statements) if m}
    baseline = _baseline_indexes()
    assert {name: baseline.get(name) for name in created} == created
    assert len(dropped) == 9 and not dropped & baseline.keys()  # the baseline runs on every setup
    assert len(statements) == len(created) + len(dropped) + 1  # every statement is idempotent
    assert statements[-1] == "DROP FUNCTION IF EXISTS pylovo.generate_building_way_connection_candidates_infdb()"


def test_deferred_bus_keys_and_lookups_over_all_versions_have_a_leading_index():
    """Every deleted bus checks its lines, loads and transformers at commit; two lookups ignore the version."""
    from pylovo.database.migrations import integrity

    leading = {(table, columns[:n]) for table, columns in _baseline_indexes().values()
               for n in range(1, len(columns) + 1)}
    keys = re.findall(r"ALTER TABLE pylovo\.(pandapower_\w+) ADD CONSTRAINT fk_pp_\w+ FOREIGN KEY \(([^)]*)\)",
                      " ".join(_statements(integrity)))
    assert len(keys) == 5
    assert all((table, tuple(c.strip() for c in columns.split(","))) in leading for table, columns in keys)
    assert ("ways_result", ("way_id",)) in leading  # SELECT MAX(way_id) FROM pylovo.ways_result, every PLZ
    assert ("postcode_result", ("postcode_result_plz",)) in leading


def test_migration_0013_drops_the_unused_tables_and_renames_the_line_cache_keys_of_v080():
    """A fresh setup has none of the three tables and the key names ``lines_result_cache_*``; an old schema must match."""
    from pylovo.database.migrations import MIGRATION_NAMES, POST_SCHEMA_MIGRATIONS, schema_cleanup

    assert dict(POST_SCHEMA_MIGRATIONS)["0013_schema_cleanup"] is schema_cleanup
    assert MIGRATION_NAMES.count("0013_schema_cleanup") == 1
    conn = Connection()
    schema_cleanup(Cursor(conn))
    sql = " ".join(" ".join(conn.log).split())
    dropped = ("mastr_postcode_aggregates", "mastr_units", "grid_parameters")
    assert f"DROP TABLE IF EXISTS {', '.join('pylovo.' + table for table in dropped)};" in sql
    assert not set(dropped) & CREATE_QUERIES.keys()          # the baseline of every setup does not create them again
    assert "starts_with(conname, 'lines_result_view_')" in sql and "RENAME CONSTRAINT %I TO %I" in sql
    assert "'lines_result_cache_' || substr(old_name.conname, length('lines_result_view_') + 1)" in sql
    assert sql.endswith("ALTER SEQUENCE IF EXISTS pylovo.lines_result_view_id_seq RENAME TO lines_result_cache_id_seq;")
    assert len(conn.log) == 1                                # one statement batch: it commits with its ledger row


def test_migration_0014_appends_the_classification_choices_of_a_fresh_setup():
    """A fresh setup creates the four columns with the table, an older schema gets them appended: same names, types, order."""
    from pylovo.database.migrations import MIGRATION_NAMES, POST_SCHEMA_MIGRATIONS, classification_choices

    assert POST_SCHEMA_MIGRATIONS[-1] == ("0014_classification_choices", classification_choices)
    assert MIGRATION_NAMES[-1] == "0014_classification_choices"
    assert MIGRATION_NAMES.count("0014_classification_choices") == 1
    conn = Connection()
    classification_choices(Cursor(conn))
    sql = " ".join(conn.log)
    added = re.findall(r"ADD COLUMN IF NOT EXISTS (\w+) ([\w\[\]]+)", sql)
    fresh = re.findall(r"^\s*(\w+) ([\w\[\]]+),$", CREATE_QUERIES["classification_version"], re.M)
    assert added == fresh[-4:] == [("list_of_clustering_parameters", "text[]"), ("n_clusters_kmeans", "integer"),
                                   ("n_clusters_gmm", "integer"), ("additional_filtering", "boolean")]
    assert sql.startswith("ALTER TABLE pylovo.classification_version ADD COLUMN IF NOT EXISTS")
    assert len(conn.log) == 1                                # one statement: it commits with its ledger row
