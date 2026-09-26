"""Step-by-step maps of the grid generation for the documentation.

Requires a pylovo database with a completed ``pylovo-setup``, the OSM-derived demo
region PLZ 85653 in the InfDB-shaped schemas ``basedata``/``opendata`` and the
grid versions created by ``docs/scripts/make_demo_versions.py`` (``1``, ``docs_bf``
and ``docs_km``). The script only reads from the database.

Run from the repository root::

    uv run python docs/scripts/plot_generation_steps.py

Output: ``docs/source/images/generation/*.png``.
"""
from __future__ import annotations

import _figure_style as st
import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

PLZ = 85653
VERSION_DEFAULT = "1"
VERSION_BROWNFIELD = "docs_bf"
VERSION_KMEANS = "docs_km"
EPSG = 25832
SUBDIR = "generation"


def _gdf(engine, sql: str, **params) -> gpd.GeoDataFrame:
    return gpd.read_postgis(sql, engine, geom_col="geom", params=params)


def _load_common(engine) -> dict:
    data = {
        "postcode": _gdf(engine, "SELECT geom FROM pylovo.postcode WHERE plz = %(plz)s", plz=PLZ),
        "buildings_in": _gdf(
            engine,
            "SELECT objectid, building_use, geom FROM basedata.buildings WHERE postcode = %(plz)s ORDER BY objectid",
            plz=PLZ,
        ),
        "streets": _gdf(
            engine, "SELECT klasse, geom FROM basedata.ways_per_connection WHERE postcode = %(plz)s ORDER BY id", plz=PLZ
        ),
        "connections": _gdf(
            engine, "SELECT geom FROM basedata.connection_lines WHERE postcode = %(plz)s ORDER BY id", plz=PLZ
        ),
        "transformers": _gdf(
            engine,
            """SELECT t.osm_id, t.lod2, t.geom FROM pylovo.transformers t
               JOIN pylovo.postcode p ON p.plz = %(plz)s AND ST_Within(t.geom, p.geom)
               ORDER BY t.osm_id""",
            plz=PLZ,
        ),
    }
    return data


def _buildings_result(engine, version: str) -> gpd.GeoDataFrame:
    return _gdf(
        engine,
        """SELECT gr.kcid, gr.bcid, br.peak_load_in_kw, br.geom
           FROM pylovo.buildings_result br
           JOIN pylovo.grid_result gr ON gr.grid_result_id = br.grid_result_id
           WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s
           ORDER BY gr.kcid, gr.bcid, br.objectid""",
        v=version,
        plz=PLZ,
    )


def _transformer_positions(engine, version: str) -> gpd.GeoDataFrame:
    return _gdf(
        engine,
        """SELECT gr.kcid, gr.bcid, gr.transformer_rated_power, tp.osm_id, tp.comment, tp.geom
           FROM pylovo.transformer_positions tp
           JOIN pylovo.grid_result gr ON gr.grid_result_id = tp.grid_result_id
           WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s
           ORDER BY gr.kcid, gr.bcid""",
        v=version,
        plz=PLZ,
    )


def _pandapower_lines(engine, version: str) -> gpd.GeoDataFrame:
    return _gdf(
        engine,
        f"""SELECT gr.kcid, gr.bcid, pl.std_type, pl.parallel, pl.feeder_sizing_basis,
                   pl.service_sizing_basis IS NOT NULL AS is_service,
                   ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(pl.geo::text), 4326), {EPSG}) AS geom
            FROM pylovo.pandapower_line pl
            JOIN pylovo.grid_result gr ON gr.grid_result_id = pl.grid_result_id
            WHERE gr.version_id = %(v)s AND gr.plz = %(plz)s AND pl.geo IS NOT NULL
            ORDER BY gr.kcid, gr.bcid, pl.pp_index""",
        v=version,
        plz=PLZ,
    )


def _base_layers(ax, data, buildings: bool = True, building_color: str = st.BUILDING_NEUTRAL) -> None:
    data["postcode"].boundary.plot(ax=ax, color=st.INK_MUTED, lw=0.8, ls="--", zorder=1)
    data["streets"].plot(ax=ax, color=st.STREET, lw=0.9, zorder=2)
    if buildings:
        data["buildings_in"].plot(ax=ax, color=building_color, edgecolor=st.BUILDING_EDGE, lw=0.2, zorder=3)


def _bounds(data, pad: float = 60.0):
    """Extent of the built-up area plus a margin (the postcode polygon is larger)."""
    minx, miny, maxx, maxy = data["buildings_in"].total_bounds
    return minx - pad, miny - pad, maxx + pad, maxy + pad


def _legend_below(ax, handles, ncol: int = 3, title: str | None = None) -> None:
    ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=ncol, title=title,
              title_fontsize=8, fontsize=7.5, columnspacing=1.4, handlelength=1.8)


def _label(ax, x, y, text) -> None:
    ax.annotate(
        text, (x, y), xytext=(6, 6), textcoords="offset points", fontsize=7.5, color=st.INK, zorder=12,
        bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": st.BASELINE, "lw": 0.6, "alpha": 0.92},
    )


def _category_colors(keys) -> dict:
    return {key: st.CATEGORICAL[index % len(st.CATEGORICAL)] for index, key in enumerate(sorted(keys))}


def figure_inputs(data) -> None:
    """Step 1: postcode area, buildings by use, streets and existing transformers."""
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    use_order = ["Residential", "Commercial", "Public", "Mixed"]
    colors = dict(zip(use_order, st.CATEGORICAL))
    _base_layers(ax, data, buildings=False)
    buildings = data["buildings_in"]
    for use, group in buildings.groupby(buildings["building_use"].where(buildings["building_use"].isin(use_order), "Other")):
        group.plot(ax=ax, color=colors.get(use, st.INK_MUTED), edgecolor="white", lw=0.2, zorder=3)
    trafos = data["transformers"]
    points = trafos.geometry.centroid
    ax.scatter(points.x, points.y, marker="^", s=46, color=st.INK, edgecolor="white", lw=1.0,
               zorder=10)
    map_bounds = _bounds(data)
    st.map_axes(ax, map_bounds)
    st.add_scalebar(ax, 200)
    st.add_attribution(ax)
    counts = buildings["building_use"].where(buildings["building_use"].isin(use_order), "Other").value_counts()
    handles = [Patch(color=colors[use], label=f"{use} ({counts.get(use, 0)})") for use in use_order]
    handles.append(Patch(color=st.INK_MUTED, label=f"Other ({counts.get('Other', 0)})"))
    handles += [
        Line2D([], [], color=st.STREET, lw=1.5, label="Street network"),
        Line2D([], [], marker="^", ls="", color=st.INK, markeredgecolor="white", markersize=7,
               label=f"Transformer candidate ({len(trafos)})"),
        Line2D([], [], color=st.INK_MUTED, lw=0.8, ls="--", label="Postcode boundary"),
    ]
    _legend_below(ax, handles, ncol=3)
    ax.set_title(f"Input data of PLZ {PLZ}")
    st.save(fig, SUBDIR, "step1_inputs")


def figure_connections(data) -> None:
    """Step 2: building connection lines to the street graph (zoomed)."""
    fig, ax = plt.subplots(figsize=(7.2, 5.2))
    buildings = data["buildings_in"]
    centre = buildings.geometry.union_all().centroid
    half_w, half_h = 260, 180
    bounds = (centre.x - half_w, centre.y - half_h, centre.x + half_w, centre.y + half_h)
    data["streets"].plot(ax=ax, color=st.STREET, lw=1.6, zorder=2)
    buildings.plot(ax=ax, color=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, lw=0.4, zorder=3)
    data["connections"].plot(ax=ax, color=st.CATEGORICAL[1], lw=1.0, zorder=4)
    centroids = buildings.geometry.centroid
    ax.scatter(centroids.x, centroids.y, s=6, color=st.INK_SECONDARY, zorder=5, lw=0)
    st.map_axes(ax, bounds)
    st.add_scalebar(ax, 50)
    st.add_attribution(ax)
    handles = [
        Line2D([], [], color=st.STREET, lw=2, label="Street segment"),
        Patch(facecolor=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, label="Building"),
        Line2D([], [], color=st.CATEGORICAL[1], lw=1.5, label="Connection line (building to street)"),
        Line2D([], [], marker="o", ls="", color=st.INK_SECONDARY, markersize=3, label="Building vertex (centroid)"),
    ]
    _legend_below(ax, handles, ncol=2)
    ax.set_title("Street graph with building connection lines (detail)")
    st.save(fig, SUBDIR, "step2_connections")


def figure_kmeans(engine, data) -> None:
    """Step 3: connected components split by k-means (version docs_km)."""
    buildings = _buildings_result(engine, VERSION_KMEANS)
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_layers(ax, data, buildings=False)
    colors = _category_colors(buildings["kcid"].unique())
    for kcid, group in buildings.groupby("kcid"):
        group.plot(ax=ax, color=colors[kcid], edgecolor="white", lw=0.2, zorder=3)
        point = group.geometry.union_all().centroid
        _label(ax, point.x, point.y, f"kcid {kcid}")
    st.map_axes(ax, _bounds(data))
    st.add_scalebar(ax, 200)
    st.add_attribution(ax)
    handles = [Patch(color=colors[k], label=f"kcid {k} ({(buildings.kcid == k).sum()} buildings)") for k in sorted(colors)]
    _legend_below(ax, handles, ncol=2)
    ax.set_title("Street component split by k-means (MAX_BUILDINGS_PER_KCID = 150)")
    st.save(fig, SUBDIR, "step3_kmeans")


def _cluster_panel(ax, engine, data, version: str, title: str) -> None:
    buildings = _buildings_result(engine, version)
    trafos = _transformer_positions(engine, version)
    _base_layers(ax, data, buildings=False)
    colors = _category_colors(buildings["bcid"].unique())
    for bcid, group in buildings.groupby("bcid"):
        group.plot(ax=ax, color=colors[bcid], edgecolor="white", lw=0.2, zorder=3)
    ax.scatter(trafos.geometry.x, trafos.geometry.y, marker="*", s=150, color=st.INK, edgecolor="white", lw=1.0,
               zorder=10)
    for row in trafos.itertuples():
        _label(ax, row.geom.x, row.geom.y, f"bcid {row.bcid} · {row.transformer_rated_power} kVA")
    st.map_axes(ax, _bounds(data))
    handles = [Patch(color=colors[b], label=f"bcid {b}") for b in sorted(colors)]
    handles.append(Line2D([], [], marker="*", ls="", color=st.INK, markeredgecolor="white", markersize=10,
                          label="Transformer position"))
    _legend_below(ax, handles, ncol=3)
    ax.set_title(title)


def figure_building_clusters(engine, data) -> None:
    """Step 4: building clusters and transformer positions, greenfield vs. brownfield."""
    fig, axes = plt.subplots(1, 2, figsize=(11.5, 5.4))
    _cluster_panel(axes[0], engine, data, VERSION_DEFAULT, "Greenfield (version 1, default)")
    _cluster_panel(axes[1], engine, data, VERSION_BROWNFIELD,
                   "Brownfield (docs_bf, USE_OPEN_TRANSFORMER_POSITIONS: True)")
    st.add_scalebar(axes[0], 200)
    st.add_attribution(axes[0])
    st.add_scalebar(axes[1], 200)
    st.add_attribution(axes[1])
    fig.tight_layout()
    st.save(fig, SUBDIR, "step4_building_clusters")


def figure_grids_by_grid(engine, data) -> None:
    """Step 5: final grids coloured by grid (version 1)."""
    lines = _pandapower_lines(engine, VERSION_DEFAULT)
    trafos = _transformer_positions(engine, VERSION_DEFAULT)
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_layers(ax, data)
    colors = _category_colors(lines["bcid"].unique())
    for bcid, group in lines.groupby("bcid"):
        group.plot(ax=ax, color=colors[bcid], lw=1.1, zorder=5)
    ax.scatter(trafos.geometry.x, trafos.geometry.y, marker="*", s=150, color=st.INK, edgecolor="white", lw=1.0,
               zorder=10)
    for row in trafos.itertuples():
        _label(ax, row.geom.x, row.geom.y, f"bcid {row.bcid} · {row.transformer_rated_power} kVA")
    st.map_axes(ax, _bounds(data))
    st.add_scalebar(ax, 200)
    st.add_attribution(ax)
    handles = [Line2D([], [], color=colors[b], lw=2, label=f"Grid kcid 1 / bcid {b}") for b in sorted(colors)]
    handles.append(Line2D([], [], marker="*", ls="", color=st.INK, markeredgecolor="white", markersize=10,
                          label="Transformer"))
    _legend_below(ax, handles, ncol=3)
    ax.set_title("Generated LV grids (version 1)")
    st.save(fig, SUBDIR, "step5_grids_by_grid")


def _cross_section(std_type: str) -> int:
    try:
        return int(str(std_type).split("_")[-1])
    except ValueError:
        return 0


def figure_grids_by_cable(engine, data) -> None:
    """Step 6: final grids coloured by cable type (ordered by cross-section)."""
    lines = _pandapower_lines(engine, VERSION_DEFAULT)
    trafos = _transformer_positions(engine, VERSION_DEFAULT)
    types = sorted(lines["std_type"].unique(), key=_cross_section)
    ramp = st.SEQUENTIAL_BLUE[-len(types):] if len(types) <= len(st.SEQUENTIAL_BLUE) else st.SEQUENTIAL_BLUE
    colors = dict(zip(types, ramp))
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_layers(ax, data)
    for is_service, width in ((True, 0.6), (False, 1.6)):
        subset = lines[lines["is_service"] == is_service]
        for (std_type, parallel), group in subset.groupby(["std_type", "parallel"]):
            group.plot(ax=ax, color=colors[std_type], lw=width * min(int(parallel), 3), zorder=5 if is_service else 6)
    ax.scatter(trafos.geometry.x, trafos.geometry.y, marker="*", s=150, color=st.INK, edgecolor="white", lw=1.0,
               zorder=10)
    st.map_axes(ax, _bounds(data))
    st.add_scalebar(ax, 200)
    st.add_attribution(ax)
    length = lines.assign(km=lines.geometry.length * lines["parallel"] / 1000).groupby("std_type")["km"].sum()
    handles = [Line2D([], [], color=colors[t], lw=2.5, label=f"{t} ({length[t]:.1f} km)") for t in types]
    handles += [
        Line2D([], [], color=st.INK_SECONDARY, lw=1.6, label="Feeder (thick)"),
        Line2D([], [], color=st.INK_SECONDARY, lw=0.6, label="Service connection (thin)"),
    ]
    _legend_below(ax, handles, ncol=3, title="Cable type (installed length incl. parallel cables)")
    ax.set_title("Generated LV grids by cable type (version 1)")
    st.save(fig, SUBDIR, "step6_grids_by_cable")


def figure_voltage_upsizing(engine, data) -> None:
    """Detail of one grid: feeder sections sized by ampacity vs. voltage drop."""
    lines = _pandapower_lines(engine, VERSION_DEFAULT)
    grid = lines[lines["bcid"] == 2]
    trafos = _transformer_positions(engine, VERSION_DEFAULT)
    trafo = trafos[trafos["bcid"] == 2]
    fig, ax = plt.subplots(figsize=(7.2, 5.6))
    data["streets"].plot(ax=ax, color=st.STREET, lw=1.0, zorder=2)
    data["buildings_in"].plot(ax=ax, color=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, lw=0.2, zorder=3)
    services = grid[grid["is_service"]]
    feeders = grid[~grid["is_service"]]
    services.plot(ax=ax, color=st.INK_MUTED, lw=0.6, zorder=5)
    basis_colors = {"ampacity": st.CATEGORICAL[0], "end_to_end_voltage": st.CATEGORICAL[1]}
    for basis, group in feeders.groupby(feeders["feeder_sizing_basis"].fillna("ampacity")):
        group.plot(ax=ax, color=basis_colors.get(basis, st.CATEGORICAL[0]), lw=1.8, zorder=6)
    ax.scatter(trafo.geometry.x, trafo.geometry.y, marker="*", s=180, color=st.INK, edgecolor="white", lw=1.0,
               zorder=10)
    minx, miny, maxx, maxy = grid.total_bounds
    st.map_axes(ax, (minx - 40, miny - 40, maxx + 40, maxy + 40))
    st.add_scalebar(ax, 100)
    st.add_attribution(ax)
    handles = [
        Line2D([], [], color=basis_colors["ampacity"], lw=2.5, label="Feeder sized by ampacity"),
        Line2D([], [], color=basis_colors["end_to_end_voltage"], lw=2.5, label="Feeder upsized for the voltage-drop envelope"),
        Line2D([], [], color=st.INK_MUTED, lw=1, label="Service connection"),
        Line2D([], [], marker="*", ls="", color=st.INK, markeredgecolor="white", markersize=10, label="Transformer"),
    ]
    _legend_below(ax, handles, ncol=2)
    ax.set_title("Feeder sizing basis in grid kcid 1 / bcid 2 (version 1)")
    st.save(fig, SUBDIR, "grid_detail_sizing_basis")


def main() -> None:
    st.apply_style()
    engine = st.sql_engine()
    data = _load_common(engine)
    figure_inputs(data)
    figure_connections(data)
    figure_kmeans(engine, data)
    figure_building_clusters(engine, data)
    figure_grids_by_grid(engine, data)
    figure_grids_by_cable(engine, data)
    figure_voltage_upsizing(engine, data)


if __name__ == "__main__":
    pd.options.mode.chained_assignment = None
    main()
