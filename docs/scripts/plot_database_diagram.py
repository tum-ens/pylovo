"""Database diagram and column reference generated from the table definitions.

The script parses ``src/pylovo/database/config_table_structure.py`` statically
(no import, so neither a database nor a ``.env`` file is needed) and writes

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
RST_OUTPUT = st.REPO_ROOT / "docs" / "source" / "concepts" / "database_tables.rst"

#: Tables and views that belong to classification or validation workflows.
EXCLUDED = {"classification_version", "sample_set", "transformer_classified", "transformer_classified_with_grid",
            "grid_parameters"}

DESCRIPTIONS = {
    "version": "One row per ``VERSION_ID`` with its comment and the frozen generation-parameter snapshot "
               "(``generation_parameters``). Deleting a version cascades to all of its results.",
    "equipment_data": "Transformer and cable catalogue of a version, copied from ``TRANSFORMERS``, "
                      "``FEEDER_CABLES`` and ``CONSUMER_CONNECTION_CABLES``.",
    "consumer_categories": "Electrical load categories (Residential, Commercial, Public) from "
                           "``CONSUMER_CATEGORIES``; synchronised at the start of every generation run.",
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
    "ways_result": "Street segments of the processed routing graph of a postcode, including connection lines.",
    "plz_parameters": "PLZ-level key figures written by ``pylovo-analyze`` (or ``ANALYZE_GRIDS``), stored as JSON "
                      "per transformer size.",
    "buildings_result": "Supplied buildings with their load components, household counts and routing vertices.",
    "transformer_positions": "Transformer location of each grid; ``osm_id`` links brownfield positions to "
                             "``transformers``.",
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
    "load_edit": "Audit rows of manual load edits made in the browser UI (``pylovo.load_editing``): the changed "
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
    "transformer_positions_with_grid": "View: transformer positions joined with grid identifiers and equipment data.",
    "buildings_result_with_grid": "Materialised view: buildings with kcid, bcid and plz, refreshed after "
                                  "generation and deletion.",
}


@dataclass
class ForeignKey:
    columns: list[str]
    table: str
    ref_columns: list[str]
    on_delete: str


@dataclass
class Table:
    name: str
    columns: list[tuple[str, str]] = field(default_factory=list)
    primary_key: list[str] = field(default_factory=list)
    unique: list[list[str]] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    temporary: bool = False


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
            if "PRIMARY KEY" in upper:
                table.primary_key = _columns(re.search(r"PRIMARY KEY \(([^)]*)\)", compact, re.IGNORECASE).group(1))
            elif "FOREIGN KEY" in upper:
                fk = re.search(r"FOREIGN KEY \(([^)]*)\) REFERENCES (?:pylovo\.)?(\w+) ?\(([^)]*)\)(?: ON DELETE ([A-Z ]+))?",
                               compact, re.IGNORECASE)
                table.foreign_keys.append(ForeignKey(_columns(fk.group(1)), fk.group(2), _columns(fk.group(3)),
                                                     (fk.group(4) or "NO ACTION").strip().upper()))
            elif "UNIQUE" in upper:
                table.unique.append(_columns(re.search(r"UNIQUE \(([^)]*)\)", compact, re.IGNORECASE).group(1)))
            continue
        column, _, rest = compact.partition(" ")
        column = column.strip('"')
        col_type = re.split(r" (?:PRIMARY KEY|NOT NULL|UNIQUE|DEFAULT|GENERATED|CHECK)", rest, maxsplit=1, flags=re.IGNORECASE)[0]
        col_type = col_type.replace("{TARGET_EPSG}", "<TARGET_EPSG>")
        table.columns.append((column, col_type))
        if "PRIMARY KEY" in rest.upper():
            table.primary_key = [column]
        if re.search(r"\bUNIQUE\b", rest, re.IGNORECASE):
            table.unique.append([column])
    for column, col_type in re.findall(r"ADD COLUMN IF NOT EXISTS (\w+) ([^;]+?)(?: NOT NULL[^;]*)?;", sql):
        if column not in {c for c, _ in table.columns}:
            table.columns.append((column, col_type.strip().replace("{TARGET_EPSG}", "<TARGET_EPSG>")))
    return table


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
    ]),
    ("Per-grid results: every table references grid_result.grid_result_id (ON DELETE CASCADE)", 0.2, [
        ["buildings_result", "clustering_parameters", "transformer_positions"],
        ["lines_result_helper", "lines_result", "lines_result_cache"],
        ["split_points", "pandapower_bus", "pandapower_line"],
        ["pandapower_trafo", "pandapower_load", "load_edit"],
    ]),
    ("Standalone tables (municipal register; file-based input with USE_INFDB=False)", 0.2, [
        ["municipal_register", "ags_log", None],
        ["res", "oth", "ways"],
    ]),
    ("Temporary tables of one generation run (dropped afterwards)", 0.2, [
        ["buildings_tem", "ways_tem", None],
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
    lines.append(f"+ {rest} columns")
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
                "top_left": (x + offset, y + h), "bottom_right": (x + w - offset, y)}[where]

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
    # transformer_positions -> transformers along the right margin
    tx, ty, tw, th = boxes["transformer_positions"]
    rx, ry, rw, rh = boxes["transformers"]
    margin_x = width - 0.19
    start_y = ty + th - TITLE_H / 2
    end_y = ry + rh / 2
    ax.plot([tx + tw, margin_x, margin_x], [start_y, start_y, end_y], color=st.INK_MUTED, lw=1.0, zorder=2.5)
    _edge(ax, (margin_x, end_y), (rx + rw, end_y))
    st.save(fig, "diagrams", "database_schema", formats=("svg", "png"), dpi=170)


# ---------------------------------------------------------------------------------------------------------------
# Generated column reference
# ---------------------------------------------------------------------------------------------------------------
def _rst_table(table: Table) -> list[str]:
    fk_by_column = {}
    for fk in table.foreign_keys:
        for column in fk.columns:
            fk_by_column.setdefault(column, []).append(fk.table)
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


def write_rst(tables: dict[str, Table], views: list[str]) -> None:
    lines = [
        ".. This file is generated by docs/scripts/plot_database_diagram.py - do not edit by hand.",
        "",
        "Database table reference",
        "========================",
        "",
        "Generated from ``src/pylovo/database/config_table_structure.py``. All tables live in the schema",
        "``pylovo``; ``<TARGET_EPSG>`` is the projected CRS from ``.env`` (default 25832). Tables used only by",
        "the classification and validation modules are not listed.",
        "",
    ]
    groups = [
        ("Reference and input tables", ["version", "equipment_data", "consumer_categories", "postcode",
                                        "municipal_register", "transformers", "ags_log", "res", "oth", "ways"]),
        ("Result tables", ["postcode_result", "grid_result", "ways_result", "plz_parameters", "buildings_result",
                           "transformer_positions", "lines_result", "lines_result_helper", "lines_result_cache",
                           "split_points", "pandapower_bus", "pandapower_line", "pandapower_trafo",
                           "pandapower_load", "clustering_parameters", "load_edit"]),
        ("Temporary tables", ["buildings_tem", "ways_tem"]),
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
                uniques = "; ".join("(" + ", ".join(u) + ")" for u in table.unique)
                lines += [f"Unique: {uniques}.", ""]
            for fk in table.foreign_keys:
                lines += [(f"Foreign key ({', '.join(fk.columns)}) references ``{fk.table}`` "
                           f"({', '.join(fk.ref_columns)}), ON DELETE {fk.on_delete}."), ""]
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
