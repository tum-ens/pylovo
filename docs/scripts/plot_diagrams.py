"""Schematic diagrams for the documentation: pipeline overview and architecture.

No database is needed. Run from the repository root::

    uv run python docs/scripts/plot_diagrams.py

Output: ``docs/source/images/diagrams/{pipeline,architecture}.{svg,png}``.
"""
from __future__ import annotations

import _figure_style as st
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

SUBDIR = "diagrams"
ACCENT = st.CATEGORICAL[0]
ACCENT_FILL = "#e8f1fc"
NEUTRAL_FILL = "#f3f2ee"
INPUT_FILL = "#fdf0e9"
OUTPUT_FILL = "#e9f7f1"


def _canvas(width: float, height: float):
    fig = plt.figure(figsize=(width, height))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, width)
    ax.set_ylim(0, height)
    ax.axis("off")
    return fig, ax


def _box(ax, x, y, w, h, fill=NEUTRAL_FILL, edge=st.BASELINE, lw=0.9, ls="-", radius=0.08, zorder=2):
    patch = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={radius}", fc=fill, ec=edge,
                           lw=lw, ls=ls, zorder=zorder)
    ax.add_patch(patch)
    return patch


def _text(ax, x, y, text, size=8.5, weight="normal", color=st.INK, ha="left", va="top", zorder=5, **kwargs):
    ax.text(x, y, text, fontsize=size, fontweight=weight, color=color, ha=ha, va=va, zorder=zorder,
            linespacing=1.35, **kwargs)


def _arrow(ax, start, end, color=st.INK_MUTED, lw=1.2, style="-|>", connection="arc3", zorder=3, ls="-"):
    ax.add_patch(FancyArrowPatch(start, end, arrowstyle=style, mutation_scale=10, color=color, lw=lw,
                                 connectionstyle=connection, zorder=zorder, linestyle=ls, shrinkA=2, shrinkB=2))


def pipeline() -> None:
    """Grid generation pipeline: inputs, five phases with numbered steps, outputs."""
    width = 8.0
    phase_lines = [4, 3, 2, 3, 2]
    phase_heights = [max(0.62, 0.3 + 0.16 * lines) for lines in phase_lines]
    height = 0.55 + 0.78 + 0.42 + sum(phase_heights) + 0.3 * 4 + 0.55
    fig, ax = _canvas(width, height)
    left, right = 0.2, width - 0.2
    _text(ax, left, height - 0.15, "Grid generation for one postcode area (pylovo-generate --plz ...)",
          size=10.5, weight="bold")

    # Inputs and configuration
    top = height - 0.55
    in_h, gap = 0.78, 0.15
    in_w = (right - left - 2 * gap) / 3
    inputs = [
        ("Buildings, streets, postcodes", "InfDB schemas basedata and\nopendata, or files in data/"),
        ("Transformer candidates", "pylovo.transformers: OSM,\nLoD2 stations, DSO CSV, manual"),
        ("Configuration", "config_generation.yaml controls\nevery step; new parameters\nneed a new VERSION_ID"),
    ]
    for index, (title, body) in enumerate(inputs):
        x = left + index * (in_w + gap)
        _box(ax, x, top - in_h, in_w, in_h, fill=INPUT_FILL, edge="#e7b89f")
        _text(ax, x + 0.1, top - 0.09, title, size=8.4, weight="bold")
        _text(ax, x + 0.1, top - 0.31, body, size=7.4, color=st.INK_SECONDARY)

    phases = [
        ("1  Prepare", ("1  postcode area\n2  buildings and loads, settlement type\n3  transformer candidates\n"
                        "4  street graph and connection points"),
         "postcode_result\nbuildings_tem_<plz>\nways_tem_<plz>"),
        ("2  Partition", ("5  street components, k-means split (kcid)\n6  building clusters (bcid):\n"
                          "    brownfield and greenfield"),
         "kcid and bcid of every\nbuilding, grid_result"),
        ("3  Place", "7  transformer position and rating\n    of every cluster",
         "grid_result (rating)\ntransformer_positions"),
        ("4  Design", "8  feeder planning and cable sizing\n9  power-flow check of the\n    validation snapshot",
         "lines_result, split_points\npandapower_bus/line/\ntrafo/load"),
        ("5  Persist", "10 result tables and pandapower JSON;\n    optional PLZ analysis (ANALYZE_GRIDS)",
         "buildings_result\nways_result\ngrid_result.grid (JSON)"),
    ]
    out_w = 2.35
    ph_w = right - left - out_w - 0.45
    ph_gap = 0.3
    y = top - in_h - 0.42
    _arrow(ax, (left + ph_w / 2, top - in_h), (left + ph_w / 2, y), color=st.INK_MUTED)
    _text(ax, left + ph_w + 0.45, y + 0.3, "Tables written", size=8, weight="bold", color=st.INK_SECONDARY)
    for index, (title, steps, outputs) in enumerate(phases):
        ph_h = phase_heights[index]
        box_top = y
        _box(ax, left, box_top - ph_h, ph_w, ph_h, fill=ACCENT_FILL, edge=ACCENT, lw=1.1)
        _text(ax, left + 0.12, box_top - 0.1, title, size=9.2, weight="bold", color="#184f95")
        _text(ax, left + 1.25, box_top - 0.12, steps, size=7.8)
        ox = left + ph_w + 0.45
        _box(ax, ox, box_top - ph_h + 0.08, out_w, ph_h - 0.16, fill=OUTPUT_FILL, edge="#93d3b9")
        _text(ax, ox + 0.1, box_top - 0.18, outputs, size=7.1, color=st.INK_SECONDARY, family="DejaVu Sans Mono")
        _arrow(ax, (left + ph_w, box_top - ph_h / 2), (ox, box_top - ph_h / 2), color=st.INK_MUTED)
        if index < len(phases) - 1:
            _arrow(ax, (left + ph_w / 2, box_top - ph_h), (left + ph_w / 2, box_top - ph_h - ph_gap),
                   color=ACCENT, lw=1.5)
        y = box_top - ph_h - ph_gap
    _text(ax, left, y + ph_gap - 0.12,
          "Result: one radial LV grid per building cluster, identified by (VERSION_ID, plz, kcid, bcid); "
          "brownfield clusters have negative bcid.\nTemporary tables *_tem_<plz> are dropped after each postcode.",
          size=7.2, color=st.INK_SECONDARY)
    st.save(fig, SUBDIR, "pipeline", formats=("svg", "png"), dpi=170)


def architecture() -> None:
    """Module architecture: interfaces, workflows, engines, data access, storage."""
    width, height = 8.0, 7.45
    fig, ax = _canvas(width, height)
    left, right = 0.2, width - 0.2
    _text(ax, left, height - 0.15, "pylovo architecture", size=10.5, weight="bold")

    band_top, band_h = height - 0.52, 0.5
    _box(ax, left, band_top - band_h, right - left, band_h, fill=INPUT_FILL, edge="#e7b89f")
    _text(ax, left + 0.12, band_top - 0.09, "config_loader", size=8.4, weight="bold")
    _text(ax, left + 1.3, band_top - 0.1,
          "config/config_generation.yaml, config/config_analysis.yaml and .env (database, USE_INFDB, TARGET_EPSG);\n"
          "read once at import and used by all layers below",
          size=7.4, color=st.INK_SECONDARY)

    layers = [
        ("Interfaces", [
            ("pylovo-* commands", "pylovo.cli: setup, generate,\nanalyze, import, export, delete"),
            ("HTTP API", "api/ (pylovo-api), runs\nthe commands as jobs"),
            ("Python API, notebooks", "GridGenerator, DatabaseClient;\nnotebook_tutorials/"),
        ]),
        ("Workflows", [
            ("GridGenerator", "grid_generator.py:\npipeline per postcode"),
            ("DatabaseConstructor", "database_constructor.py:\nschema and reference data"),
            ("ParameterCalculator", "analysis/: postcode and\nper-grid key figures"),
            ("Data import", "data_import/: OSM, DSO,\nmunicipal register, files"),
            ("Plotting, export", "plotting/: maps, graphs,\nGIS CSV export"),
        ]),
        ("Engines", [
            ("Feeder planning", "feeder_planning.py: branches,\nsections, cable sizing"),
            ("CableInstaller", "cable_installer.py: buses,\nloads, feeder, service lines"),
            ("Electrical backends", "pandapower (default),\nOpenDSS (in progress)"),
            ("Load model", "utils.py: load components,\ncoincidence factor"),
        ]),
        ("Data access", [
            ("DatabaseClient", "database/: mixins Preprocessing,\nClustering, Grid, Analysis,\nResults, TransformerUi, Utils"),
            ("InfdbClient", "infdb/: buildings, streets,\npostcodes from InfDB"),
            ("SQL functions", "ways_preprocessing_functions/:\nsegments, connection lines"),
        ]),
        ("Storage", [
            ("PostgreSQL, PostGIS, pgRouting", "schema pylovo: inputs, versions, results"),
            ("InfDB schemas (same database)", "basedata: buildings, ways; opendata: postcodes"),
        ]),
    ]
    label_w = 0.95
    span_left = left + label_w
    row_h, row_gap, box_gap = 0.72, 0.2, 0.12
    y = band_top - band_h - 0.25
    for label, boxes in layers:
        per_row = {4: 2, 5: 3, 6: 3}.get(len(boxes), len(boxes))
        rows = [boxes[i:i + per_row] for i in range(0, len(boxes), per_row)]
        layer_h = len(rows) * row_h + (len(rows) - 1) * box_gap
        _text(ax, left, y - layer_h / 2, label, size=8.4, weight="bold", color=st.INK_SECONDARY, va="center")
        for r, row in enumerate(rows):
            box_w = (right - span_left - (per_row - 1) * box_gap) / per_row
            by = y - r * (row_h + box_gap)
            for index, (title, body) in enumerate(row):
                x = span_left + index * (box_w + box_gap)
                fill, edge = (ACCENT_FILL, ACCENT) if label in ("Workflows", "Engines") else (NEUTRAL_FILL, st.BASELINE)
                if label == "Storage":
                    fill, edge = OUTPUT_FILL, "#93d3b9"
                _box(ax, x, by - row_h, box_w, row_h, fill=fill, edge=edge)
                _text(ax, x + 0.1, by - 0.08, title, size=8.2, weight="bold")
                _text(ax, x + 0.1, by - 0.29, body, size=7.3, color=st.INK_SECONDARY)
        y -= layer_h
        if label != "Storage":
            mid = span_left + (right - span_left) / 2
            _arrow(ax, (mid, y), (mid, y - row_gap), color=st.INK_MUTED, lw=1.1)
        y -= row_gap
    st.save(fig, SUBDIR, "architecture", formats=("svg", "png"), dpi=170)


def main() -> None:
    st.apply_style()
    pipeline()
    architecture()


if __name__ == "__main__":
    main()
