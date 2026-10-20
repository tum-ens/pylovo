"""DB-free checks of the MV stage schema: DDL, migration 0009_mv_grids, PLZ delete hook and docs placement."""
from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from pylovo.config_loader import TARGET_EPSG
from pylovo.database.config_table_structure import CREATE_QUERIES
from pylovo.database.migrations import MIGRATION_NAMES, MV_GRID_OBJECTS, POST_SCHEMA_MIGRATIONS, mv_grids
from pylovo.database.results_mixin import ResultsMixin

REPO_ROOT = Path(__file__).resolve().parents[1]
TABLE_MODULE = REPO_ROOT / "src" / "pylovo" / "database" / "config_table_structure.py"
MIGRATIONS_MODULE = REPO_ROOT / "src" / "pylovo" / "database" / "migrations.py"
DIAGRAM_SCRIPT = REPO_ROOT / "docs" / "scripts" / "plot_database_diagram.py"

#: The MV stage entries of CREATE_QUERIES, in their dict order.
MV_STAGE_ENTRIES = ("mv_graph", "mv_graph_vertex", "mv_graph_edge", "mv_graph_settlement", "mv_variant",
                    "mv_substation", "mv_grid", "mv_node", "mv_line", "mv_ring", "mv_variant_stats")
MV_RESULT_TABLES = ("mv_variant", "mv_substation", "mv_grid", "mv_node", "mv_line", "mv_ring")
MV_TABLES = tuple(name for name in MV_STAGE_ENTRIES if name != "mv_variant_stats")

FK_PATTERN = re.compile(r"CONSTRAINT (\w+) FOREIGN KEY \(([^)]*)\) REFERENCES pylovo\.(\w+) ?\(([^)]*)\)")


def _compact(query: str) -> str:
    """Query without comments, whitespace collapsed."""
    return " ".join(re.sub(r"--[^\n]*", "", query).split())


def _cols(text: str) -> tuple[str, ...]:
    return tuple(part.strip().strip('"') for part in text.split(","))


def _table_body(query: str) -> list[str]:
    """Top-level items (columns and constraints) of the CREATE TABLE statement of an entry."""
    text = _compact(query)
    start = re.search(r"CREATE TABLE IF NOT EXISTS pylovo\.\w+ \(", text).end()
    items, depth, current = [], 1, []
    for char in text[start:]:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0:
            break
        if char == "," and depth == 1:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    items.append("".join(current).strip())
    return items


def _columns(query: str) -> list[str]:
    return [item.split()[0] for item in _table_body(query) if not item.upper().startswith("CONSTRAINT")]


def _foreign_keys(query: str) -> list[tuple[str, tuple[str, ...], str, tuple[str, ...]]]:
    return [(name, _cols(cols), target, _cols(ref_cols))
            for name, cols, target, ref_cols in FK_PATTERN.findall(_compact(query))]


def _indexed_column_sets(name: str, query: str) -> list[tuple[str, ...]]:
    """Column lists of the primary key, unique constraints and indexes of a table."""
    text = _compact(query)
    sets = [_cols(cols) for cols in re.findall(r"(?:PRIMARY KEY|UNIQUE) \(([^)]*)\)", text)]
    sets += [_cols(cols) for cols in re.findall(rf"ON pylovo\.{name} (?:USING \w+ )?\(([^)]*)\)", text)]
    for item in _table_body(query):
        if " PRIMARY KEY" in item.upper() and not item.upper().startswith("CONSTRAINT"):
            sets.append((item.split()[0],))
    return sets


def _raw_create_queries() -> dict[str, str]:
    """CREATE_QUERIES as written in the source, before str.format(TARGET_EPSG=...)."""
    tree = ast.parse(TABLE_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(isinstance(target, ast.Name) and target.id == "CREATE_QUERIES" for target in node.targets)):
            return {ast.literal_eval(key): ast.literal_eval(value)
                    for key, value in zip(node.value.keys, node.value.values)}
    raise AssertionError("CREATE_QUERIES dict literal not found")


def test_mv_entries_sit_between_mastr_and_municipal_register():
    keys = list(CREATE_QUERIES)
    start = keys.index("mastr_postcode_aggregates") + 1
    assert tuple(keys[start:start + len(MV_STAGE_ENTRIES)]) == MV_STAGE_ENTRIES
    assert keys[start + len(MV_STAGE_ENTRIES)] == "municipal_register"
    assert keys.index("mv_variant_stats") == keys.index("mv_ring") + 1


def test_mv_ddl_formats_without_stray_braces():
    raw = _raw_create_queries()
    for name in MV_STAGE_ENTRIES:
        assert not re.search(r"[{}]", raw[name].replace("{TARGET_EPSG}", "")), name
        assert "{" not in CREATE_QUERIES[name] and "}" not in CREATE_QUERIES[name], name
    with_geometry = set()
    for name in MV_TABLES:
        geometry_types = re.findall(r"geometry\((\w+),(\w+)\)", CREATE_QUERIES[name])
        assert {epsg for _, epsg in geometry_types} <= {str(TARGET_EPSG)}, name
        if geometry_types:
            with_geometry.add(name)
    assert with_geometry == {"mv_graph_vertex", "mv_graph_edge", "mv_graph_settlement", "mv_substation", "mv_grid",
                             "mv_node", "mv_line"}


def test_foreign_key_and_view_sources_come_earlier_in_dict_order():
    keys = list(CREATE_QUERIES)
    for name, query in CREATE_QUERIES.items():
        text = _compact(query)
        targets = set(re.findall(r"REFERENCES pylovo\.(\w+)", text))
        if re.search(r"CREATE (?:OR REPLACE |MATERIALIZED )?VIEW", text):
            targets |= set(re.findall(r"(?:FROM|JOIN) pylovo\.(\w+)", text))
        for target in targets:
            assert keys.index(target) < keys.index(name), f"{name} needs {target} first"


def test_every_mv_result_table_carries_version_and_variant():
    for name in MV_RESULT_TABLES:
        columns = _columns(CREATE_QUERIES[name])
        assert {"version_id", "mv_variant_id"} <= set(columns), name
    # the comparison view groups by both keys
    assert "GROUP BY g.version_id, g.mv_variant_id" in _compact(CREATE_QUERIES["mv_variant_stats"])
    # the routing graph is unversioned reference data
    for name in ("mv_graph", "mv_graph_vertex", "mv_graph_edge", "mv_graph_settlement"):
        assert "version_id" not in _columns(CREATE_QUERIES[name]), name


def test_load_edit_still_follows_pandapower_load():
    keys = list(CREATE_QUERIES)
    assert keys.index("load_edit") == keys.index("pandapower_load") + 1


def test_every_mv_foreign_key_is_a_named_constraint():
    all_names = [fk[0] for query in CREATE_QUERIES.values() for fk in _foreign_keys(query)]
    assert len(all_names) == len(set(all_names))
    for name in MV_TABLES:
        text = _compact(CREATE_QUERIES[name])
        fks = _foreign_keys(CREATE_QUERIES[name])
        assert len(fks) == text.count("REFERENCES"), name
        for fk_name, columns, _target, ref_columns in fks:
            assert fk_name.startswith(f"fk_{name}_"), fk_name
            assert len(columns) == len(ref_columns), fk_name
    assert "CHECK" not in " ".join(_compact(CREATE_QUERIES[name]) for name in MV_STAGE_ENTRIES)


def test_mv_foreign_keys_have_the_designed_targets_and_actions():
    fks = {fk[0]: fk[1:] for name in MV_TABLES for fk in _foreign_keys(CREATE_QUERIES[name])}
    assert fks["fk_mv_variant_version"] == (("version_id",), "version", ("version_id",))
    assert fks["fk_mv_node_grid_result"] == (("version_id", "grid_result_id"), "grid_result",
                                             ("version_id", "grid_result_id"))
    assert fks["fk_mv_node_customer"] == (("version_id", "customer_objectid"), "mv_customers",
                                          ("version_id", "objectid"))
    for name in MV_RESULT_TABLES[1:]:
        if name != "mv_ring":
            assert fks[f"fk_{name}_variant"] == (("version_id", "mv_variant_id"), "mv_variant",
                                                 ("version_id", "mv_variant_id"))
    text = " ".join(_compact(CREATE_QUERIES[name]) for name in MV_TABLES)
    actions = re.findall(r"CONSTRAINT (\w+) FOREIGN KEY \([^)]*\) REFERENCES pylovo\.\w+ ?\([^)]*\) ON DELETE "
                         r"(CASCADE|SET NULL)", text)
    assert len(actions) == len(fks)
    assert {name for name, action in actions if action == "SET NULL"} == {"fk_mv_variant_graph"}


def test_every_mv_foreign_key_column_set_is_indexed():
    for name in MV_TABLES:
        query = CREATE_QUERIES[name]
        indexed = _indexed_column_sets(name, query)
        for fk_name, columns, _target, _ref in _foreign_keys(query):
            assert any(index[:len(columns)] == columns for index in indexed), fk_name


def test_mv_grid_objects_exist_in_dict_order_and_cover_the_mv_stage():
    keys = list(CREATE_QUERIES)
    assert set(MV_GRID_OBJECTS) <= set(keys)
    assert len(MV_GRID_OBJECTS) == len(set(MV_GRID_OBJECTS))
    assert sorted(MV_GRID_OBJECTS, key=keys.index) == list(MV_GRID_OBJECTS)
    assert set(MV_STAGE_ENTRIES) | {"mv_customers", "mastr_units", "mastr_postcode_aggregates"} == set(
        MV_GRID_OBJECTS)


def test_migration_0009_runs_the_mv_ddl_in_order():
    names = [name for name, _ in POST_SCHEMA_MIGRATIONS]
    assert names[names.index("0009_mv_grids") + 1] == "0010_mv_display_geometry"
    assert dict(POST_SCHEMA_MIGRATIONS)["0009_mv_grids"] is mv_grids and "0009_mv_grids" in MIGRATION_NAMES

    class Cursor:
        def __init__(self):
            self.queries = []

        def execute(self, query, params=None):
            assert params is None
            self.queries.append(query)

    cur = Cursor()
    mv_grids(cur)
    assert cur.queries == [CREATE_QUERIES[name] for name in MV_GRID_OBJECTS]


def test_migrations_module_imports_no_pylovo_module_at_import_time():
    tree = ast.parse(MIGRATIONS_MODULE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("pylovo"), node.module
        elif isinstance(node, ast.Import):
            assert not any(alias.name.startswith("pylovo") for alias in node.names)


def test_assert_schema_requires_the_mv_variant_foreign_key():
    from pylovo.database.database_constructor import DatabaseConstructor

    class Cursor:
        def __init__(self):
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, query, params=None):
            required = params[0]
            self.rows = [(name, True, "ON DELETE SET NULL (transformer_equipment_name)")
                         for name in required if name != "fk_mv_variant_version"]

        def fetchall(self):
            return self.rows

    class Connection:
        def cursor(self):
            return Cursor()

    class Client:
        conn = Connection()

    with pytest.raises(RuntimeError, match=r"missing=\['fk_mv_variant_version'\]"):
        DatabaseConstructor(Client()).assert_schema()


def test_plz_delete_removes_the_mv_variants_of_the_plz_first(caplog):
    calls = []

    class Cursor:
        def execute(self, query, params=None):
            calls.append((" ".join(query.split()), params))

        def fetchall(self):
            return [("d1",), ("d0",)]

    class Connection:
        def commit(self):
            calls.append(("commit", None))

    class Client(ResultsMixin):
        def refresh_materialized_views(self):
            calls.append(("refresh", None))

    client = object.__new__(Client)
    client.cur = Cursor()
    client.conn = Connection()
    client.logger = logging.getLogger("mv-schema-test")

    with caplog.at_level(logging.INFO, logger="mv-schema-test"):
        client.delete_plz_from_all_tables("12345", "z1")

    query, params = calls[0]
    assert query == ("DELETE FROM pylovo.mv_variant WHERE version_id = %(v)s AND %(p)s = ANY(plz_list) "
                     "RETURNING mv_variant_id;")
    assert params == {"v": "z1", "p": 12345}
    assert [query for query, _ in calls[1:]][-2:] == ["refresh", "commit"]
    assert any("postcode_result" in query for query, _ in calls[1:])
    assert "MV variant(s) d0, d1 of version z1 deleted because they contain PLZ 12345" in caplog.text


def test_database_client_registers_the_mv_mixins_after_the_lv_mixins():
    from pylovo.database.database_client import DatabaseClient
    from pylovo.database.load_edit_mixin import LoadEditMixin
    from pylovo.database.mv_graph_mixin import MvGraphMixin
    from pylovo.database.mv_input_mixin import MvInputMixin
    from pylovo.database.mv_result_mixin import MvResultMixin
    from pylovo.database.mv_source_mixin import MvSourceMixin

    bases = DatabaseClient.__bases__
    assert bases[-5:] == (LoadEditMixin, MvGraphMixin, MvSourceMixin, MvInputMixin, MvResultMixin)


def test_diagram_script_places_every_table():
    """docs/scripts/plot_database_diagram.py raises for unplaced tables; it is only run by hand."""
    tree = ast.parse(DIAGRAM_SCRIPT.read_text(encoding="utf-8"))
    module_values = {target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
                     for target in node.targets if isinstance(target, ast.Name)}
    excluded = ast.literal_eval(module_values["EXCLUDED"])
    zones = ast.literal_eval(module_values["ZONES"])
    descriptions = ast.literal_eval(module_values["DESCRIPTIONS"])
    groups = next(ast.literal_eval(node.value) for node in ast.walk(tree)
                  if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "groups"
                                                          for t in node.targets))
    placed = {name for _, _, rows in zones for row in rows for name in row if name}
    listed = {name for _, names in groups for name in names}
    tables = {name for name, query in CREATE_QUERIES.items()
              if "CREATE TABLE" in query and name not in excluded}
    assert tables <= placed
    assert tables <= listed
    assert set(MV_GRID_OBJECTS) <= set(descriptions)


def test_migration_0010_adds_the_display_geometry_to_existing_tables():
    from pylovo.config_loader import TARGET_EPSG
    from pylovo.database.migrations import mv_display_geometry

    assert dict(POST_SCHEMA_MIGRATIONS)["0010_mv_display_geometry"] is mv_display_geometry
    assert "0010_mv_display_geometry" in MIGRATION_NAMES

    class Cursor:
        def __init__(self, existing):
            self.existing, self.queries, self.last = existing, [], None

        def execute(self, query, params=None):
            self.queries.append(query)
            self.last = params[0] if params else None

        def fetchone(self):
            return ("x" if self.last in self.existing else None,)

    cur = Cursor({"pylovo.mv_line"})                      # an MV schema without mv_substation yet
    mv_display_geometry(cur)
    alters = [q for q in cur.queries if q.startswith("ALTER")]
    assert alters == [f"ALTER TABLE pylovo.mv_line ADD COLUMN IF NOT EXISTS display_geom "
                      f"geometry(LineString,{int(TARGET_EPSG)})"]
    for table in ("mv_line", "mv_substation"):
        assert "display_geom geometry(" in CREATE_QUERIES[table]
