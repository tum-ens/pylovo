"""From the HV grid to the house: how one LV grid is connected through the voltage levels.

- :func:`read_hv_grid`: the 110-380 kV lines and buses of Germany from eGon-data (eTraGo), as downloaded by
  ``pylovo-viewer fetch etrago``.
- :func:`plot_voltage_levels`: three maps that zoom in from the HV grid and the MV supply areas over one MV
  supply area with its rings to one LV grid, under a strip that names every link of the chain.
- :func:`chain_table`: the links of the chain (``DatabaseClient.read_voltage_chain``) as a table.

The HV grid is a context layer: pylovo's MV stage connects every MV supply area to an HV/MV substation and
matches the substation to its 110 kV bus of the eTraGo grid (``mv_substation.egon_bus_id``); the HV grid itself is
not generated. The functions return a matplotlib figure or a table, never call ``plt.show()`` and read no database.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import ConnectionPatch, Patch, Rectangle

from pylovo.plotting import style as st

__all__ = ["chain_table", "default_hv_dir", "plot_voltage_levels", "read_hv_grid"]


def default_hv_dir(scenario: str = "eGon2035") -> Path:
    """Folder of the eTraGo grid that ``pylovo-viewer fetch etrago`` downloads (the viewer's cache)."""
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pylovo-viewer" / "etrago" / scenario


def read_hv_grid(path: Optional[Path] = None, bounds: Optional[Sequence[float]] = None,
                 crs: str = "EPSG:25832") -> dict[str, gpd.GeoDataFrame]:
    """The HV/EHV grid of eGon-data (eTraGo): ``{"lines", "buses"}`` with ``v_nom`` (kV).

    Args:
        path: Folder with ``lines.geojson`` and ``buses.geojson``; default :func:`default_hv_dir`.
        bounds: Keep only what intersects ``(minx, miny, maxx, maxy)`` in ``crs``.
        crs: CRS of the result and of ``bounds``.

    Raises:
        FileNotFoundError: The grid has not been downloaded (``pylovo-viewer fetch etrago``).
    """
    folder = default_hv_dir() if path is None else Path(path)
    result = {}
    for part in ("lines", "buses"):
        file = folder / f"{part}.geojson"
        if not file.is_file():
            raise FileNotFoundError(f"{file} is missing: download the HV grid once with: pylovo-viewer fetch etrago")
        frame = gpd.read_file(file).to_crs(crs)
        if bounds is not None:
            minx, miny, maxx, maxy = bounds
            frame = frame.cx[minx:maxx, miny:maxy]
        result[part] = frame.reset_index(drop=True)
    return result


def chain_table(chain: Mapping[str, Any]) -> pd.DataFrame:
    """The links of the chain of one LV grid, from the 110 kV bus down to the LV grid, as a table."""
    rows = [
        ("HV grid", "110 kV bus of the eTraGo grid", "–" if chain.get("egon_bus_id") is None
         else f"bus {chain['egon_bus_id']}"),
        ("HV/MV substation", chain.get("substation_name") or f"substation {chain.get('substation_id')}",
         f"{chain.get('hv_mv_transformers') or '?'} transformers, {chain.get('hv_mv_transformer_mva') or 0:g} MVA"),
        ("MV supply area", f"MV grid {chain.get('mv_grid_id')} at {chain.get('vn_kv') or 0:g} kV",
         f"{chain.get('n_stations')} stations, {chain.get('n_rings')} rings, {chain.get('circuit_km') or 0:.0f} km, "
         f"{chain.get('peak_load_mw') or 0:.1f} MW peak"),
        ("MV ring", f"ring {chain.get('ring_id')}" + (f", position {chain['ring_position']}"
                                                     if chain.get("ring_position") is not None else ""),
         f"{chain.get('vm_load_pu') or 0:.3f} p.u. at the station in the load case"),
        ("MV/LV station", f"{chain.get('transformer_rated_power') or 0:g} kVA",
         f"tap {chain.get('mv_lv_tap_pos')}, LV busbar {chain.get('lv_busbar_vm_pu') or 0:.3f} p.u."),
        ("LV grid", f"grid {chain.get('kcid')}/{chain.get('bcid')} of postcode area {chain.get('plz')}", "0.4 kV"),
    ]
    return pd.DataFrame(rows, columns=["level", "element", "key figures"])


# --------------------------------------------------------------------------- figure
def _bounds_of(frames: Sequence[Optional[gpd.GeoDataFrame]]) -> Optional[np.ndarray]:
    parts = [np.asarray(f.total_bounds) for f in frames if f is not None and len(f)]
    if not parts:
        return None
    parts = np.array(parts)
    return np.array([parts[:, 0].min(), parts[:, 1].min(), parts[:, 2].max(), parts[:, 3].max()])


def _square(bounds: Sequence[float], pad: float, min_size: float, aspect: float = 1.0) -> tuple:
    """``bounds`` padded and widened to the aspect ratio (width / height) of a panel."""
    minx, miny, maxx, maxy = st.padded_bounds(bounds, pad=pad, min_size=min_size)
    width, height = maxx - minx, maxy - miny
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    if width / height < aspect:
        width = height * aspect
    else:
        height = width / aspect
    return cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2


def _clip(frame: Optional[gpd.GeoDataFrame], bounds: Sequence[float]) -> Optional[gpd.GeoDataFrame]:
    if frame is None or len(frame) == 0:
        return None
    minx, miny, maxx, maxy = bounds
    return frame.cx[minx:maxx, miny:maxy]


def _zoom_box(fig: Figure, ax_from: plt.Axes, ax_to: plt.Axes, box: Sequence[float], color: str) -> None:
    """A rectangle around ``box`` in ``ax_from`` and two lines from it to the left corners of ``ax_to``."""
    minx, miny, maxx, maxy = box
    ax_from.add_patch(Rectangle((minx, miny), maxx - minx, maxy - miny, fill=False, ec=color, lw=1.2, zorder=30))
    for corner_from, corner_to in (((maxx, maxy), (0, 1)), ((maxx, miny), (0, 0))):
        fig.add_artist(ConnectionPatch(xyA=corner_from, coordsA=ax_from.transData, xyB=corner_to,
                                       coordsB=ax_to.transAxes, color=color, lw=0.6, alpha=0.55, ls=(0, (3, 2)),
                                       zorder=30))


def _chip(fig: Figure, x: float, y: float, title: str, text: str, color: str) -> None:
    fig.text(x, y, f"{title}\n{text}", ha="center", va="center", fontsize=7.6, color=st.INK,
             bbox={"boxstyle": "round,pad=0.45", "fc": "white", "ec": color, "lw": 1.4})


def plot_voltage_levels(chain: Mapping[str, Any], hv: Mapping[str, gpd.GeoDataFrame],
                        mv_context: Mapping[str, gpd.GeoDataFrame], mv_grid: Mapping[str, gpd.GeoDataFrame],
                        lv: Mapping[str, gpd.GeoDataFrame], figsize: Tuple[float, float] = (15.0, 6.9),
                        region_km: float = 40.0, attribution: Optional[str] = None,
                        title: Optional[str] = None) -> Figure:
    """Three maps from the HV grid down to one LV grid, with the chain of the connection above them.

    1. **HV grid and MV supply areas**: the 110 kV (and 220/380 kV) lines of eTraGo, the HV/MV substations and the
       MV supply areas of the region; the supply area of the grid is highlighted.
    2. **MV supply area**: its MV lines (overhead dashed, normally-open ties and links dotted), the stations and
       the HV/MV substation; the ring that supplies the LV grid is drawn darker.
    3. **LV grid**: its cables, buildings and MV/LV station.

    Args:
        chain: The links of the chain (``DatabaseClient.read_voltage_chain``).
        hv: ``{"lines", "buses"}`` of :func:`read_hv_grid` (``v_nom`` in kV); may be empty.
        mv_context: ``{"substations", "grids"}`` of the MV variant (``read_mv_layers``), at least the region.
        mv_grid: ``{"nodes", "lines", "substations", "grids"}`` of the supply area (``read_mv_layers`` with
            ``mv_grid_id``).
        lv: The LV layers of the postcode area or of the grid (``read_lv_layers``).
        figsize: Size of the figure.
        region_km: Minimum width of the first map.
        attribution: Data attribution of the maps; default the LV data and the HV grid.
        title: Title of the figure.

    Returns:
        The figure.
    """
    kcid, bcid = int(chain["kcid"]), int(chain["bcid"])
    ring_id = chain.get("ring_id")
    lv_lines = lv["lines"][(lv["lines"]["kcid"].astype(int) == kcid) & (lv["lines"]["bcid"].astype(int) == bcid)]
    lv_grid_row = lv["grids"][(lv["grids"]["kcid"].astype(int) == kcid) & (lv["grids"]["bcid"].astype(int) == bcid)]
    station = lv_grid_row.geometry.iloc[0] if len(lv_grid_row) else lv_lines.geometry.union_all().centroid
    district = mv_grid.get("grids")
    substation = mv_grid.get("substations")
    hv_lines = hv.get("lines") if hv else None
    with st.style():
        fig = plt.figure(figsize=figsize)
        axes = [fig.add_axes((0.01 + i * 0.335, 0.08, 0.31, 0.62)) for i in range(3)]
        aspect = (0.31 * figsize[0]) / (0.62 * figsize[1])

        # bounds of the three maps
        lv_box = _square(lv_lines.total_bounds, pad=0.08, min_size=250, aspect=aspect)
        district_bounds = _bounds_of([district, mv_grid.get("lines")])
        mv_box = _square(district_bounds if district_bounds is not None else lv_box, pad=0.05, min_size=2000,
                         aspect=aspect)
        region_box = _square(mv_box, pad=0.6, min_size=region_km * 1000, aspect=aspect)

        # 1: HV grid and MV supply areas
        ax = axes[0]
        areas = _clip(mv_context.get("grids"), region_box)
        if areas is not None and len(areas):
            areas.plot(ax=ax, color="#f1f0eb", edgecolor=st.BASELINE, lw=0.5, zorder=1)
        if district is not None and len(district):
            district.plot(ax=ax, color=st.LEVEL_COLORS["MV"], alpha=0.22, edgecolor=st.LEVEL_COLORS["MV"], lw=1.2,
                          zorder=2)
        if hv_lines is not None and len(hv_lines):
            lines = _clip(hv_lines, region_box)
            kv = pd.to_numeric(lines["v_nom"], errors="coerce")
            lines[kv <= 110].plot(ax=ax, color=st.LEVEL_COLORS["HV"], lw=1.0, zorder=3)
            lines[kv > 110].plot(ax=ax, color=st.LEVEL_COLORS["EHV"], lw=1.8, zorder=3)
        subs = _clip(mv_context.get("substations"), region_box)
        if subs is not None and len(subs):
            ax.scatter(subs.geometry.x, subs.geometry.y, marker="s", s=16, color=st.LEVEL_COLORS["HV"],
                       edgecolor="white", lw=0.6, zorder=4)
        if substation is not None and len(substation):
            point = substation.geometry.iloc[0]
            ax.scatter([point.x], [point.y], marker="s", s=70, color=st.LEVEL_COLORS["HV"], edgecolor="white",
                       lw=1.0, zorder=5)
        st.station_marker(ax, [station.x], [station.y], size=90, zorder=6)
        st.map_axes(ax, region_box)
        st.add_scalebar(ax)
        ax.set_title("HV grid and MV supply areas")
        ax.legend(handles=[Line2D([], [], color=st.LEVEL_COLORS["EHV"], lw=1.8, label="220/380 kV"),
                           Line2D([], [], color=st.LEVEL_COLORS["HV"], lw=1.0, label="110 kV"),
                           Line2D([], [], marker="s", ls="", color=st.LEVEL_COLORS["HV"], markeredgecolor="white",
                                  label="HV/MV substation"),
                           Patch(facecolor=st.LEVEL_COLORS["MV"], alpha=0.35, edgecolor=st.LEVEL_COLORS["MV"],
                                 label="MV supply area of the grid"),
                           Patch(facecolor="#f1f0eb", edgecolor=st.BASELINE, label="Other MV supply areas")],
                  loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=2, fontsize=7)

        # 2: the MV supply area
        ax = axes[1]
        if district is not None and len(district):
            district.plot(ax=ax, color=st.LEVEL_COLORS["MV"], alpha=0.07, edgecolor="none", zorder=0)
            district.boundary.plot(ax=ax, color=st.BASELINE, lw=0.9, zorder=1)
        mv_lines = mv_grid.get("lines")
        if mv_lines is not None and len(mv_lines):
            open_ = mv_lines["normally_open"].fillna(False).astype(bool) | mv_lines["role"].isin(["tie", "link"])
            on_ring = (mv_lines["ring_id"] == ring_id) & ~open_ if ring_id is not None else pd.Series(False,
                                                                                                  index=mv_lines.index)
            overhead = mv_lines["kind"].eq("overhead")
            mv_lines[open_].plot(ax=ax, color=st.INK_MUTED, lw=0.8, ls=":", zorder=2)
            mv_lines[~open_ & ~on_ring & ~overhead].plot(ax=ax, color=st.LEVEL_COLORS["MV"], lw=1.0, zorder=3)
            mv_lines[~open_ & ~on_ring & overhead].plot(ax=ax, color=st.LEVEL_COLORS["MV"], lw=1.0, ls="--",
                                                        zorder=3)
            if on_ring.any():
                mv_lines[on_ring].plot(ax=ax, color="#9a3d15", lw=2.2, zorder=4)
        nodes = mv_grid.get("nodes")
        if nodes is not None and len(nodes):
            stations = nodes[nodes["kind"] == "station"]
            ax.scatter(stations.geometry.x, stations.geometry.y, s=7, color=st.INK_SECONDARY, lw=0, zorder=5)
            customers = nodes[nodes["kind"] == "customer"]
            if len(customers):
                ax.scatter(customers.geometry.x, customers.geometry.y, marker="D", s=12, color=st.INK,
                           edgecolor="white", lw=0.4, zorder=5)
        if substation is not None and len(substation):
            point = substation.geometry.iloc[0]
            ax.scatter([point.x], [point.y], marker="s", s=90, color=st.LEVEL_COLORS["HV"], edgecolor="white",
                       lw=1.0, zorder=6)
        st.station_marker(ax, [station.x], [station.y], size=160, zorder=7)
        st.map_axes(ax, mv_box)
        st.add_scalebar(ax)
        ax.set_title(f"MV supply area at {chain.get('vn_kv') or 0:g} kV")
        handles = [Patch(facecolor=st.LEVEL_COLORS["MV"], alpha=0.15, edgecolor=st.BASELINE, label="Supply area"),
                   Line2D([], [], color=st.LEVEL_COLORS["MV"], lw=1.2, label="MV cable"),
                   Line2D([], [], color=st.LEVEL_COLORS["MV"], lw=1.2, ls="--", label="MV overhead line"),
                   Line2D([], [], color="#9a3d15", lw=2.2, label="Ring that supplies the grid"),
                   Line2D([], [], color=st.INK_MUTED, lw=1, ls=":", label="Normally open tie or link"),
                   Line2D([], [], marker="o", ls="", color=st.INK_SECONDARY, markersize=3, label="MV/LV station"),
                   Line2D([], [], marker="D", ls="", color=st.INK, markersize=4, label="MV customer")]
        ax.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=2, fontsize=7)

        # 3: the LV grid
        ax = axes[2]
        streets = _clip(lv.get("streets"), lv_box)
        if streets is not None and len(streets):
            streets.plot(ax=ax, color=st.STREET, lw=0.9, zorder=1)
        buildings = _clip(lv.get("buildings"), lv_box)
        if buildings is not None and len(buildings):
            mine = (buildings["kcid"].astype(int) == kcid) & (buildings["bcid"].astype(int) == bcid)
            buildings[~mine].plot(ax=ax, color=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, lw=0.2, zorder=2)
            buildings[mine].plot(ax=ax, color="#cfe0f6", edgecolor=st.LEVEL_COLORS["LV"], lw=0.4, zorder=3)
        service = lv_lines["is_service"].fillna(False).astype(bool)
        lv_lines[service].plot(ax=ax, color=st.LEVEL_COLORS["LV"], lw=0.6, zorder=4)
        lv_lines[~service].plot(ax=ax, color=st.LEVEL_COLORS["LV"], lw=1.8, zorder=5)
        st.station_marker(ax, [station.x], [station.y], size=200, zorder=7)
        st.map_axes(ax, lv_box)
        st.add_scalebar(ax)
        st.add_attribution(ax, attribution if attribution is not None else st.DATA_ATTRIBUTION)
        ax.set_title(f"LV grid {kcid}/{bcid} at 0.4 kV")
        ax.legend(handles=[Line2D([], [], color=st.LEVEL_COLORS["LV"], lw=1.8, label="LV feeder"),
                           Line2D([], [], color=st.LEVEL_COLORS["LV"], lw=0.6, label="House connection"),
                           Patch(facecolor="#cfe0f6", edgecolor=st.LEVEL_COLORS["LV"], label="Supplied building"),
                           Line2D([], [], marker="*", ls="", color=st.INK, markeredgecolor="white", markersize=9,
                                  label="MV/LV station")],
                  loc="upper center", bbox_to_anchor=(0.5, -0.01), ncol=2, fontsize=7)
        axes[0].text(0.005, 0.005, st.HV_ATTRIBUTION, transform=axes[0].transAxes, fontsize=5.8,
                     color=st.INK_SECONDARY, va="bottom", ha="left", zorder=20,
                     bbox={"boxstyle": "square,pad=0.2", "fc": st.SURFACE, "ec": "none", "alpha": 0.85})

        _zoom_box(fig, axes[0], axes[1], mv_box, st.LEVEL_COLORS["MV"])
        _zoom_box(fig, axes[1], axes[2], lv_box, st.LEVEL_COLORS["LV"])

        # the chain above the maps
        chips = [
            ("110 kV · HV grid", "–" if chain.get("egon_bus_id") is None else f"eTraGo bus {chain['egon_bus_id']}",
             st.LEVEL_COLORS["HV"]),
            ("HV/MV substation", f"{chain.get('substation_name') or chain.get('substation_id')}\n"
                                 f"{chain.get('hv_mv_transformers') or '?'} × transformer, "
                                 f"{chain.get('hv_mv_transformer_mva') or 0:g} MVA", st.LEVEL_COLORS["HV"]),
            (f"{chain.get('vn_kv') or 0:g} kV · MV supply area", f"{chain.get('n_stations')} stations, "
                                                                f"{chain.get('n_rings')} rings\n"
                                                                f"{chain.get('circuit_km') or 0:.0f} km of MV lines",
             st.LEVEL_COLORS["MV"]),
            ("MV/LV station", f"{chain.get('transformer_rated_power') or 0:g} kVA on ring {ring_id}\n"
                              f"LV busbar {chain.get('lv_busbar_vm_pu') or 0:.3f} p.u.", st.INK),
            ("0.4 kV · LV grid", f"grid {kcid}/{bcid}, {len(lv_lines[~service])} feeder sections\n"
                                 f"{int(service.sum())} house connections", st.LEVEL_COLORS["LV"]),
        ]
        xs = np.linspace(0.1, 0.9, len(chips))
        for x, (head, text, color) in zip(xs, chips):
            _chip(fig, x, 0.82, head, text, color)
        for left, right in zip(xs[:-1], xs[1:]):
            fig.add_artist(Line2D([left + 0.075, right - 0.075], [0.82, 0.82], color=st.INK_MUTED, lw=1.0))
            fig.text(right - 0.075, 0.82, "▶", fontsize=7, color=st.INK_MUTED, ha="center", va="center")
        fig.text(0.01, 0.97, title if title is not None else
                 f"From the HV grid to the house: LV grid {kcid}/{bcid} of postcode area {chain.get('plz')}",
                 fontsize=11, fontweight="bold", va="top")
    return fig
