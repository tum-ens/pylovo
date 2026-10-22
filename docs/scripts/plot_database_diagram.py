"""Database diagram and column reference generated from the table definitions.

The script parses ``src/pylovo/database/config_table_structure.py`` statically
(no import, so neither a database nor a ``.env`` file is needed) and applies the constraint
changes of the migrations in ``src/pylovo/database/migrations.py`` (``ALTER TABLE ... ADD/DROP
CONSTRAINT``), so the constraints shown are those of a database after all migrations. It writes

* ``docs/source/images/diagrams/database_schema.{svg,png}`` - entity diagram
* ``docs/source/concepts/database_tables.rst`` - generated column reference

Tables of the classification and validation modules are left out on purpose.
Run from the repository root::

    uv run python docs/scripts/plot_database_diagram.py
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

import _figure_style as st
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

TABLE_MODULE = st.REPO_ROOT / "src" / "pylovo" / "database" / "config_table_structure.py"
MIGRATIONS_MODULE = st.REPO_ROOT / "src" / "pylovo" / "database" / "migrations.py"
CONSTRUCTOR_MODULE = st.REPO_ROOT / "src" / "pylovo" / "database" / "database_constructor.py"
RST_OUTPUT = st.REPO_ROOT / "docs" / "source" / "concepts" / "database_tables.rst"

#: Tables and views that belong to classification or validation workflows.
EXCLUDED = {"classification_version", "sample_set", "transformer_classified", "transformer_classified_with_grid"}

DESCRIPTIONS = {
    "version": "One row per ``VERSION_ID`` with its comment and the frozen generation-parameter snapshot "
               "(``generation_parameters``). Deleting a version cascades to all of its results.",
    "equipment_data": "Transformer and cable catalogue of a version, copied from ``TRANSFORMERS``, "
                      "``FEEDER_CABLES`` and ``CONSUMER_CONNECTION_CABLES``.",
    "consumer_categories": "Electrical load categories (Residential, Commercial, Public, Unknown) from "
                           "``CONSUMER_CATEGORIES``; synchronised at the start of every generation run.",
    "schema_migrations": "Ledger of the applied schema migrations: one row per migration name with the time it was "
                         "applied (``pylovo-setup``). Created before the other tables.",
    "postcode": "Postcode polygons. Filled by ``pylovo-setup`` (from InfDB or ``data/postcode.csv``); "
                "missing InfDB postcodes are added on demand during generation.",
    "municipal_register": "PLZ to AGS mapping with population, area and RegioStaR classes; used to resolve "
                          "``--ags`` and to select building files in file-based mode.",
    "transformers": "Transformer candidates: OSM download, LoD2 transformer-station buildings, DSO CSV imports "
                    "and manual edits. Not versioned.",
    "postcode_result": "Postcode geometry per version plus the settlement metrics (house distance, households "
                       "per building, settlement type).",
    "grid_result": "One row per generated grid (version, plz, kcid, bcid): transformer rating, power-flow status, "
                   "planning and solved voltage-drop diagnostics and the pandapower net as JSON (``grid``).",
    "building_exclusion": "Loaded buildings of a generated postcode that are in no grid, with the step that left "
                          "them out; with buildings_result it accounts for every loaded building "
                          "(``pylovo-analyze --coverage``).",
    "ways_result": "Street segments of the processed routing graph of a postcode, including connection lines.",
    "plz_parameters": "PLZ-level key figures written by ``pylovo-analyze`` (or ``ANALYZE_GRIDS``), stored as JSON "
                      "per transformer size.",
    "buildings_result": "Supplied buildings with their load components, household counts and routing vertices.",
    "transformer_positions": "Transformer location of each grid; ``osm_id`` links brownfield positions to "
                             "``transformers`` and becomes NULL when that raw transformer is deleted.",
    "lines_result": "Feeder and service lines of each grid as projected line geometries.",
    "lines_result_helper": "Offset helper geometries that make parallel and split feeders visible in GIS.",
    "lines_result_cache": "Physical per-grid cache of lines plus helper geometries for QGIS layers.",
    "lines_result_view": "Compatibility SQL view over lines_result_cache for existing QGIS and API readers.",
    "split_points": "Street nodes at which a feeder branches.",
    "pandapower_bus": "Buses of the pandapower net with GeoJSON coordinates (``geo``).",
    "pandapower_line": "Lines of the pandapower net with sizing provenance (feeder section, sizing basis, "
                       "ampacity-only type, service voltage drops).",
    "pandapower_trafo": "Transformer of the pandapower net.",
    "pandapower_load": "Loads of the validation snapshot per consumer and category, including the service design "
                       "load and installed peak (``max_p_mw``).",
    "clustering_parameters": "Per-grid key figures written by ``pylovo-analyze --per-grid``.",
    "load_edit": "Audit rows of manual load edits made through the HTTP API (``pylovo.load_editing``): the changed "
                 "inputs, the building, loads and pandapower net before the edit (for the exact undo) and the "
                 "removed analysis rows. Append-only; an undo stamps ``undone_at``.",
    "ags_log": "AGS codes whose building files were imported (file-based mode).",
    "res": "Residential building polygons imported from shapefiles (file-based mode only).",
    "oth": "Other building polygons imported from shapefiles (file-based mode only).",
    "ways": "Street network imported from the osm2po SQL dump (file-based mode only).",
    "buildings_tem": "Temporary building table of one postcode, created as ``buildings_tem_<plz>`` and "
                     "dropped after the run.",
    "ways_tem": "Temporary street table of one postcode, created as ``ways_tem_<plz>``; pgRouting adds "
                "``ways_tem_<plz>_vertices_pgr``.",
    "excluded_tem": "Temporary table of the buildings removed during the generation of one postcode, with the "
                    "reason; saved to ``building_exclusion``.",
    "transformer_positions_with_grid": "View: transformer positions with grid identifiers, the station rating "
                                       "(``transformer_rated_power``) and the equipment data of one unit "
                                       "(``transformer_units`` per station).",
    "buildings_result_with_grid": "Regular view: live buildings with kcid, bcid and plz; base geometry is indexed.",
    "mv_customers": "Buildings of a version whose non-residential load is connected directly to MV "
                    "(``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``), written per PLZ by the LV stage; the residential "
                    "part of a mixed building stays in its LV grid.",
    "mv_graph": "One row per MV routing graph built from the regional street graph of InfDB, content-addressed by "
                "``graph_id``, with its build parameters and QA figures. Not versioned.",
    "mv_graph_vertex": "Vertices of an MV routing graph with their degree and connected component.",
    "mv_graph_edge": "Edges of an MV routing graph (street pieces) with length, component and the settlement flags "
                     "``in_settlement``, ``settlement_id`` and ``settlement_urban``.",
    "mv_graph_settlement": "Settlements of an MV routing graph (connected settlement edges) with building count, "
                           "length, urban class and buffer geometry.",
    "mv_variant": "One row per MV variant of a version: the MV parameter snapshot (``mv_parameters``), the PLZ set, "
                  "the routing graph, status, LV fingerprint and run summary. Deleting it cascades to all MV results "
                  "of the variant.",
    "mv_substation": "HV/MV substations of an MV variant (OSM or eGon) with busbar point, graph attachment, "
                     "transformer sizing and loading.",
    "mv_grid": "One MV grid per supplying HV/MV substation: status, HV/MV transformers, peak load, counts, line "
               "lengths, voltage and loading extremes, metrics and the stored pandapower net (``net``).",
    "mv_node": "Nodes of the MV grids (busbars, LV stations, MV customers, joints) with loads, ring "
               "position, MV voltages and the voltage budget of each station (MV/LV tap, LV busbar voltage).",
    "mv_line": "MV lines (ring, stub, dedicated, tie and link lines) with kind, standard type, cable and "
               "overhead length, normally-open flag and loadings.",
    "mv_ring": "One row per ring, dedicated feeder or busbar radial with member counts, lengths, "
               "coincident loads, normally-open point and voltage and loading extremes.",
    "mv_variant_stats": "View: grid counts by status and sums of stations, rings, line lengths, cable share, km per "
                        "supply point and peak load per version and MV variant.",
}


@dataclass
class ForeignKey:
    name: str
    columns: list[str]
    table: str
    ref_columns: list[str]
    on_delete: str
    deferred: bool = False


@dataclass
class Table:
    name: str
    columns: list[tuple[str, str]] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    unique: dict[str, list[str]] = field(default_factory=dict)   # constraint name -> columns
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    checks: dict[str, str] = field(default_factory=dict)         # constraint name -> condition
    temporary: bool = False

    def set_foreign_key(self, fk: ForeignKey) -> None:
        """Add a foreign key, or replace the one of the same name."""
        self.foreign_keys = [old for old in self.foreign_keys if old.name != fk.name] + [fk]

    def drop_constraint(self, name: str) -> None:
        """Remove the foreign key, unique or check constraint of this name (nothing happens if there is none)."""
        self.foreign_keys = [fk for fk in self.foreign_keys if fk.name != name]
        self.unique.pop(name, None)
        self.checks.pop(name, None)


def _string_dict(tree: ast.Module, name: str) -> dict[str, str]:
    """Return the first dict literal of string constants assigned to ``name``."""
    for node in tree.body:
        is_target = isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        if is_target and isinstance(node.value, ast.Dict):
            return {ast.literal_eval(k): ast.literal_eval(v) for k, v in zip(node.value.keys, node.value.values)}
    raise ValueError(f"{name} not found in {TABLE_MODULE}")


def _split_top_level(body: str) -> list[str]:
    parts, depth, current = [], 0, []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if "".join(current).strip():
        parts.append("".join(current).strip())
    return parts


def _columns(text: str) -> list[str]:
    return [c.strip().strip('"') for c in text.split(",")]


def _group(text: str, start: int) -> tuple[str, int]:
    """Return the text inside the parentheses that open at ``text[start]`` and the index after the closing one."""
    depth, index = 0, start
    while True:
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        index += 1
        if depth == 0:
            return text[start + 1:index - 1], index


#: ``REFERENCES [pylovo.]table (columns)`` with its optional ``ON DELETE`` action (``SET NULL`` may list columns)
#: and ``DEFERRABLE INITIALLY DEFERRED``.
_REFERENCES = (r"REFERENCES (?:pylovo\.)?(\w+) ?\(([^)]*)\)"
               r"(?: ON DELETE (CASCADE|SET NULL(?: \([^)]*\))?|SET DEFAULT|RESTRICT|NO ACTION))?"
               r"( DEFERRABLE INITIALLY DEFERRED)?")


def _foreign_key(name: str, columns: str, groups: tuple) -> ForeignKey:
    """Build a foreign key from the column list and the four groups of :data:`_REFERENCES`."""
    table, ref_columns, on_delete, deferred = groups[0], groups[1], groups[2], groups[3]
    return ForeignKey(name, _columns(columns), table, _columns(ref_columns), on_delete or "NO ACTION",
                      deferred=bool(deferred))


def _parse_table(name: str, sql: str, temporary: bool = False) -> Table | None:
    sql = re.sub(r"--[^\n]*", "", sql)
    match = re.search(r"CREATE (?:TEMP )?TABLE IF NOT EXISTS (?:pylovo\.)?(\w+)\s*\(", sql)
    if not match:
        return None
    start = match.end()
    depth, index = 1, start
    while depth:
        depth += {"(": 1, ")": -1}.get(sql[index], 0)
        index += 1
    table = Table(name=name, temporary=temporary)
    for item in _split_top_level(sql[start:index - 1]):
        compact = " ".join(item.split())
        upper = compact.upper()
        if upper.startswith("CONSTRAINT"):
            constraint = compact.split()[1].strip('"')
            if "PRIMARY KEY" in upper:
                table.primary_key = _columns(re.search(r"PRIMARY KEY \(([^)]*)\)", compact, re.IGNORECASE).group(1))
            elif "FOREIGN KEY" in upper:
                fk = re.search(r"FOREIGN KEY \(([^)]*)\) " + _REFERENCES, compact, re.IGNORECASE)
                table.foreign_keys.append(_foreign_key(constraint, fk.group(1), fk.groups()[1:]))
            elif "UNIQUE" in upper:
                table.unique[constraint] = _columns(re.search(r"UNIQUE \(([^)]*)\)", compact, re.IGNORECASE).group(1))
            elif re.search(r"\bCHECK \(", compact, re.IGNORECASE):
                start = re.search(r"\bCHECK \(", compact, re.IGNORECASE).end() - 1
                table.checks[constraint] = _group(compact, start)[0]
            continue
        column, _, rest = compact.partition(" ")
        column = column.strip('"')
        col_type = re.split(r" (?:PRIMARY KEY|NOT NULL|UNIQUE|DEFAULT|GENERATED|CHECK)", rest, maxsplit=1, flags=re.IGNORECASE)[0]
        col_type = col_type.replace("{TARGET_EPSG}", "<TARGET_EPSG>")
        table.columns.append((column, col_type))
        if "PRIMARY KEY" in rest.upper():
            table.primary_key = [column]
        if re.search(r"\bUNIQUE\b", rest, re.IGNORECASE):
            table.unique[f"{name}_{column}_key"] = [column]
        inline_check = re.search(r"\bCHECK \(", rest, re.IGNORECASE)
        if inline_check:
            table.checks[f"{name}_{column}_check"] = _group(rest, inline_check.end() - 1)[0]
    for column, col_type in re.findall(r"ADD COLUMN IF NOT EXISTS (\w+) ([^;]+?)(?: NOT NULL[^;]*)?;", sql):
        if column not in {c for c, _ in table.columns}:
            table.columns.append((column, col_type.strip().replace("{TARGET_EPSG}", "<TARGET_EPSG>")))
    return table


def _migration_statements() -> list[str]:
    """Return the SQL texts of the post-schema migrations that contain ``ALTER TABLE``, in migration order.

    The migrations are listed in ``POST_SCHEMA_MIGRATIONS`` (name, function); the pre-schema migrations run before
    the baseline tables exist and are not applied here.
    """
    tree = ast.parse(MIGRATIONS_MODULE.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    order = []
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "POST_SCHEMA_MIGRATIONS":
            order = [entry.elts[1].id for entry in node.value.elts]
    texts = []
    for function in order:
        texts += [" ".join(const.value.split()) for const in ast.walk(functions[function])
                  if isinstance(const, ast.Constant) and isinstance(const.value, str) and "ALTER TABLE" in const.value]
    return texts


def apply_migrations(tables: dict[str, Table]) -> None:
    """Apply the ``ALTER TABLE ... DROP/ADD CONSTRAINT`` statements of the migrations to the parsed tables.

    Only foreign keys, unique and check constraints are modelled (other statements, such as indexes and
    ``VALIDATE CONSTRAINT``, do not change what the reference shows).
    """
    alter = r"ALTER TABLE (?:ONLY )?pylovo\.(\w+) "
    for sql in _migration_statements():
        for match in re.finditer(alter + r"DROP CONSTRAINT (?:IF EXISTS )?(\w+)", sql):
            if match.group(1) in tables:
                tables[match.group(1)].drop_constraint(match.group(2))
        for match in re.finditer(alter + r"ADD CONSTRAINT (\w+) FOREIGN KEY \(([^)]*)\) " + _REFERENCES, sql):
            if match.group(1) in tables:
                tables[match.group(1)].set_foreign_key(_foreign_key(match.group(2), match.group(3), match.groups()[3:]))
        for match in re.finditer(alter + r"ADD CONSTRAINT (\w+) UNIQUE \(([^)]*)\)", sql):
            if match.group(1) in tables:
                tables[match.group(1)].unique[match.group(2)] = _columns(match.group(3))
        for match in re.finditer(alter + r"ADD CONSTRAINT (\w+) CHECK \(", sql):
            if match.group(1) in tables:
                tables[match.group(1)].checks[match.group(2)] = _group(sql, match.end() - 1)[0]


def _constructor_table(name: str) -> Table:
    """Parse the ``CREATE TABLE`` of ``pylovo.<name>`` that ``DatabaseConstructor`` runs outside ``CREATE_QUERIES``."""
    tree = ast.parse(CONSTRUCTOR_MODULE.read_text(encoding="utf-8"))
    for const in ast.walk(tree):
        if (isinstance(const, ast.Constant) and isinstance(const.value, str)
                and re.search(rf"CREATE TABLE IF NOT EXISTS pylovo\.{name}\s*\(", const.value)):
            return _parse_table(name, const.value)
    raise ValueError(f"CREATE TABLE of {name} not found in {CONSTRUCTOR_MODULE}")


def parse_schema() -> tuple[dict[str, Table], list[str]]:
    tree = ast.parse(TABLE_MODULE.read_text(encoding="utf-8"))
    create = _string_dict(tree, "CREATE_QUERIES")
    temp = _string_dict(tree, "TEMP_CREATE_QUERIES")
    tables, views = {}, []
    for name, sql in create.items():
        if name in EXCLUDED:
            continue
        table = _parse_table(name, sql)
        if table is None:
            views.append(name)
        else:
            tables[name] = table
    tables["schema_migrations"] = _constructor_table("schema_migrations")
    apply_migrations(tables)
    for name, sql in temp.items():
        tables[name] = _parse_table(name, sql, temporary=True)
    return tables, views


# ---------------------------------------------------------------------------------------------------------------
# Diagram
# ---------------------------------------------------------------------------------------------------------------
BOX_W = 2.42
COL_X = [0.25, 2.93, 5.61]
LINE_H = 0.148
TITLE_H = 0.22

#: Zones of the diagram: (label, row gap, rows); each row lists the tables of the three columns.
ZONES = [
    ("Reference data and per-postcode results", 0.42, [
        ["version", "postcode", "transformers"],
        ["equipment_data", "postcode_result", "ways_result"],
        ["consumer_categories", "grid_result", "plz_parameters"],
        [None, None, "building_exclusion"],
    ]),
    ("Per-grid results: every table references grid_result.grid_result_id (ON DELETE CASCADE)", 0.2, [
        ["buildings_result", "clustering_parameters", "transformer_positions"],
        ["lines_result_helper", "lines_result", "lines_result_cache"],
        ["split_points", "pandapower_bus", "pandapower_line"],
        ["pandapower_trafo", "pandapower_load", "load_edit"],
    ]),
    ("MV stage: results hang on mv_variant (ON DELETE CASCADE, also through mv_grid); mv_node references grid_result",
     0.2, [
        ["mv_graph_vertex", "mv_graph", "mv_graph_edge"],
        ["mv_graph_settlement", "mv_variant", "mv_substation"],
        ["mv_customers", "mv_node", "mv_grid"],
        [None, "mv_line", "mv_ring"],
    ]),
    ("Standalone tables (municipal register, migration ledger; file-based input with USE_INFDB=False)", 0.2, [
        ["municipal_register", "ags_log", "schema_migrations"],
        ["res", "oth", "ways"],
    ]),
    ("Temporary tables of one generation run (dropped afterwards)", 0.2, [
        ["buildings_tem", "ways_tem", "excluded_tem"],
    ]),
]


def _fk_lines(table: Table) -> list[str]:
    lines = []
    for fk in table.foreign_keys:
        cols = ", ".join(fk.columns)
        text = f"FK {cols} → {fk.table}"
        if len(text) > 40:
            lines += [f"FK {cols}", f"   → {fk.table}"]
        else:
            lines.append(text)
    return lines


def _box_lines(table: Table) -> list[str]:
    lines = []
    if table.primary_key:
        lines.append("PK " + ", ".join(table.primary_key))
    lines += _fk_lines(table)
    keyed = set(table.primary_key) | {c for fk in table.foreign_keys for c in fk.columns}
    rest = len([c for c, _ in table.columns if c not in keyed])
    lines.append(f"+ {rest} column" + ("" if rest == 1 else "s"))
    return lines


def _box_height(table: Table) -> float:
    return TITLE_H + LINE_H * len(_box_lines(table)) + 0.08


def _draw_table(ax, table: Table, x: float, y: float) -> tuple[float, float, float, float]:
    """Draw one table box with its upper edge at ``y``; return (x, bottom, width, height)."""
    lines = _box_lines(table)
    h = _box_height(table)
    y = y - h
    dashed = table.temporary or table.name in {"res", "oth", "ways", "ags_log"}
    ax.add_patch(FancyBboxPatch((x, y), BOX_W, h, boxstyle="round,pad=0,rounding_size=0.05", fc="white",
                                ec=st.BASELINE, lw=0.9, ls="--" if dashed else "-", zorder=3))
    ax.add_patch(FancyBboxPatch((x, y + h - TITLE_H), BOX_W, TITLE_H, boxstyle="round,pad=0,rounding_size=0.05",
                                fc="#e8f1fc" if not dashed else "#f3f2ee", ec="none", zorder=3.5))
    title = table.name + ("_<plz>" if table.temporary else "")
    ax.text(x + 0.08, y + h - TITLE_H / 2, title, fontsize=7.6, fontweight="bold", va="center", zorder=5,
            family="DejaVu Sans Mono")
    for index, line in enumerate(lines):
        color = st.INK_MUTED if line.startswith("+") else st.INK_SECONDARY
        ax.text(x + 0.08, y + h - TITLE_H - 0.03 - LINE_H * (index + 0.5), line, fontsize=6.4, va="center",
                color=color, zorder=5, family="DejaVu Sans Mono")
    return x, y, BOX_W, h


def _zone(ax, x, y, w, h, label):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0,rounding_size=0.1", fc="#f6f6f3",
                                ec=st.GRID, lw=0.8, zorder=1))
    ax.text(x + 0.12, y + h - 0.1, label, fontsize=7.6, fontweight="bold", color=st.INK_SECONDARY, va="top",
            zorder=2)


def _edge(ax, start, end, connection="arc3", color=st.INK_MUTED, lw=1.0):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle="-|>", mutation_scale=9, color=color, lw=lw,
                                 connectionstyle=connection, shrinkA=1, shrinkB=1, zorder=2.5))


def draw_diagram(tables: dict[str, Table]) -> None:
    placed = {name for _, _, rows in ZONES for row in rows for name in row if name}
    missing = set(tables) - placed
    if missing:
        raise ValueError(f"No diagram position for tables: {sorted(missing)}")

    zone_header, zone_pad, zone_gap = 0.34, 0.14, 0.14
    layout, cursor = [], 0.62
    for label, row_gap, rows in ZONES:
        zone_top = cursor
        cursor += zone_header
        placed_rows = []
        for row in rows:
            height = max(_box_height(tables[name]) for name in row if name)
            placed_rows.append((cursor, row))
            cursor += height + row_gap
        cursor += zone_pad - row_gap
        layout.append((label, zone_top, cursor, placed_rows))
        cursor += zone_gap

    width, height = 8.3, cursor
    fig = plt.figure(figsize=(width, height))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.axis("off")
    ax.text(0.25, height - 0.2, "pylovo database schema (schema pylovo)", fontsize=10.5, fontweight="bold", va="top")
    ax.text(0.25, height - 0.42, "Arrows point from a foreign key to the referenced table. Dashed boxes: file-based "
            "input or temporary tables.", fontsize=7, color=st.INK_SECONDARY, va="top")

    boxes = {}
    zone_tops = {}
    for label, zone_top, zone_bottom, placed_rows in layout:
        _zone(ax, 0.12, height - zone_bottom, width - 0.24, zone_bottom - zone_top, label)
        zone_tops[label] = height - zone_top
        for row_y, row in placed_rows:
            for col, name in enumerate(row):
                if name:
                    boxes[name] = _draw_table(ax, tables[name], COL_X[col], height - row_y)

    def side(name, where, offset=0.35):
        x, y, w, h = boxes[name]
        return {"top": (x + w / 2, y + h), "bottom": (x + w / 2, y), "left": (x, y + h / 2), "right": (x + w, y + h / 2),
                "top_left": (x + offset, y + h), "top_right": (x + w - offset, y + h),
                "bottom_left": (x + offset, y), "bottom_right": (x + w - offset, y)}[where]

    _edge(ax, side("equipment_data", "top"), side("version", "bottom"))
    _edge(ax, side("postcode_result", "top"), side("postcode", "bottom"))
    _edge(ax, side("postcode_result", "top_left"), side("version", "bottom_right"))
    _edge(ax, side("ways_result", "left"), side("postcode_result", "right"))
    _edge(ax, side("plz_parameters", "top_left"), side("postcode_result", "bottom_right"))
    _edge(ax, side("grid_result", "top"), side("postcode_result", "bottom"))
    _edge(ax, side("grid_result", "top_left"), side("equipment_data", "bottom_right"))
    # the per-grid zone as a whole references grid_result
    gx, gy, gw, _ = boxes["grid_result"]
    per_grid_top = zone_tops[ZONES[1][0]]
    _edge(ax, (gx + gw / 2, per_grid_top), (gx + gw / 2, gy), color=st.CATEGORICAL[0], lw=1.8)
    _edge(ax, side("lines_result_helper", "right"), side("lines_result", "left"))
    _edge(ax, side("lines_result_cache", "left"), side("lines_result", "right"))
    # building_exclusion -> postcode_result through the gutter between the second and the third column
    bx, by, bw, bh = boxes["building_exclusion"]
    px, py, pw, _ = boxes["postcode_result"]
    gutter_x = COL_X[2] - (COL_X[2] - COL_X[1] - BOX_W) / 2
    end_y = py + 0.14
    ax.plot([bx, gutter_x, gutter_x], [by + bh / 2, by + bh / 2, end_y], color=st.INK_MUTED, lw=1.0, zorder=2.5)
    _edge(ax, (gutter_x, end_y), (px + pw, end_y))
    # transformer_positions -> transformers along the right margin
    tx, ty, tw, th = boxes["transformer_positions"]
    rx, ry, rw, rh = boxes["transformers"]
    margin_x = width - 0.19
    start_y = ty + th - TITLE_H / 2
    end_y = ry + rh / 2
    ax.plot([tx + tw, margin_x, margin_x], [start_y, start_y, end_y], color=st.INK_MUTED, lw=1.0, zorder=2.5)
    _edge(ax, (margin_x, end_y), (rx + rw, end_y))
    # MV stage: variant -> routing graph, substation -> variant, grid -> substation; nodes, lines and rings
    _edge(ax, side("mv_variant", "top"), side("mv_graph", "bottom"))
    _edge(ax, side("mv_substation", "left"), side("mv_variant", "right"))
    _edge(ax, side("mv_grid", "top"), side("mv_substation", "bottom"))
    _edge(ax, side("mv_node", "top"), side("mv_variant", "bottom"))
    _edge(ax, side("mv_node", "left"), side("mv_customers", "right"))
    _edge(ax, side("mv_node", "right"), side("mv_grid", "left"))
    _edge(ax, side("mv_line", "top"), side("mv_node", "bottom"))
    _edge(ax, side("mv_ring", "top"), side("mv_grid", "bottom"))
    _edge(ax, side("mv_graph_vertex", "right"), side("mv_graph", "left"))
    _edge(ax, side("mv_graph_edge", "left"), side("mv_graph", "right"))
    _edge(ax, side("mv_graph_settlement", "top_right"), side("mv_graph", "bottom_left"))
    st.save(fig, "diagrams", "database_schema", formats=("svg", "png"), dpi=170)


# ---------------------------------------------------------------------------------------------------------------
# Generated column reference
# ---------------------------------------------------------------------------------------------------------------
def _rst_table(table: Table) -> list[str]:
    fk_by_column = {}
    for fk in table.foreign_keys:
        for column in fk.columns:
            targets = fk_by_column.setdefault(column, [])
            if fk.table not in targets:
                targets.append(fk.table)
    out = [".. list-table::", "   :header-rows: 1", "   :widths: 34 38 28", "   :class: fixed-table", "",
           "   * - Column", "     - Type",
           "     - Key"]
    for column, col_type in table.columns:
        keys = []
        if column in table.primary_key:
            keys.append("PK")
        keys += [f"FK → ``{target}``" for target in fk_by_column.get(column, [])]
        out += [f"   * - ``{column}``", f"     - ``{col_type}``", f"     - {', '.join(keys)}"]
    return out


def _foreign_key_sentence(fk: ForeignKey) -> str:
    """One sentence for a foreign key, with its ON DELETE action and, if deferred, the check at commit."""
    text = (f"Foreign key ({', '.join(fk.columns)}) references ``{fk.table}`` ({', '.join(fk.ref_columns)}), "
            f"ON DELETE {fk.on_delete}")
    return text + (", checked at commit (deferred)." if fk.deferred else ".")


def write_rst(tables: dict[str, Table], views: list[str]) -> None:
    lines = [
        ".. This file is generated by docs/scripts/plot_database_diagram.py - do not edit by hand.",
        "",
        "Database table reference",
        "========================",
        "",
        "Generated from ``src/pylovo/database/config_table_structure.py`` and the constraint changes of",
        "``src/pylovo/database/migrations.py``: the keys and checks are those of a database after all migrations.",
        "All tables live in the schema ``pylovo``; ``<TARGET_EPSG>`` is the projected CRS from ``.env`` (default",
        "25832). Tables used only by the classification and validation modules are not listed.",
        "",
    ]
    groups = [
        ("Reference and input tables", ["version", "equipment_data", "consumer_categories", "postcode",
                                        "municipal_register", "transformers", "mv_graph", "mv_graph_vertex",
                                        "mv_graph_edge",
                                        "mv_graph_settlement", "ags_log", "res", "oth", "ways",
                                        "schema_migrations"]),
        ("Result tables", ["postcode_result", "grid_result", "ways_result", "plz_parameters", "building_exclusion",
                           "buildings_result",
                           "transformer_positions", "lines_result", "lines_result_helper", "lines_result_cache",
                           "split_points", "pandapower_bus", "pandapower_line", "pandapower_trafo",
                           "pandapower_load", "clustering_parameters", "load_edit"]),
        ("MV stage tables", ["mv_customers", "mv_variant", "mv_substation", "mv_grid", "mv_node", "mv_line",
                             "mv_ring"]),
        ("Temporary tables", ["buildings_tem", "ways_tem", "excluded_tem"]),
    ]
    listed = set()
    for title, names in groups:
        lines += [title, "-" * len(title), ""]
        for name in names:
            table = tables[name]
            listed.add(name)
            heading = f"``{name}_<plz>``" if table.temporary else f"``{name}``"
            lines += [heading, "~" * len(heading), "", DESCRIPTIONS.get(name, ""), ""]
            if table.unique:
                uniques = "; ".join("(" + ", ".join(u) + ")" for u in table.unique.values())
                lines += [f"Unique: {uniques}.", ""]
            for fk in table.foreign_keys:
                lines += [_foreign_key_sentence(fk), ""]
            for check_name, condition in table.checks.items():
                lines += [f"Check ``{check_name}``: ``{condition}``.", ""]
            lines += _rst_table(table) + [""]
    lines += ["Views", "-----", ""]
    for name in views:
        lines += [f"``{name}``", "   " + DESCRIPTIONS.get(name, ""), ""]
    unlisted = set(tables) - listed
    if unlisted:
        raise ValueError(f"Tables missing from the reference groups: {sorted(unlisted)}")
    RST_OUTPUT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"wrote {RST_OUTPUT.relative_to(st.REPO_ROOT)}")


def main() -> None:
    st.apply_style()
    tables, views = parse_schema()
    draw_diagram(tables)
    write_rst(tables, views)


if __name__ == "__main__":
    main()
