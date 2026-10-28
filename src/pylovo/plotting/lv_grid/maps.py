"""Maps of the LV grids of a postcode area, drawn from the layers of ``DatabaseClient.read_lv_layers``.

- :func:`plot_lv_overview`: all grids of a postcode area, coloured by grid, station rating, cable type or
  power-flow status.
- :func:`plot_lv_grid`: one grid, coloured by cable type, feeder, sizing basis, line loading or voltage.
- :func:`plot_building_uses`: the buildings by use, the MV customers and the stations.
- :func:`plot_partition`: the k-means clusters (``kcid``) or building clusters (``bcid``) with their stations.
- :func:`plot_connections`: a detail of the street graph and the connection lines from the buildings.

**Layers.** ``layers`` is the dictionary of ``read_lv_layers`` (or of the example data,
:class:`pylovo.examples.TutorialData`): GeoDataFrames in a metric CRS with the column names of the pylovo tables.
The functions draw what is there and skip missing optional layers.

**Drawing conventions.** Maps are drawn from vector layers, without basemap tiles: streets in light grey,
buildings as footprints, the postcode boundary dashed. Cables of the feeders are drawn thicker than house
connections, MV/LV stations are black stars. Every map has a scale bar and the attribution of its data.

The functions return their matplotlib figure and never call ``plt.show()``; they read no database.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, Tuple

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from pylovo.plotting import style as st
from pylovo.plotting.lv_grid.electrical import _feeder_numbers, feeder_ids

__all__ = [
    "core_bounds",
    "grid_colors",
    "plot_building_uses",
    "plot_connections",
    "plot_lv_grid",
    "plot_lv_overview",
    "plot_partition",
]

_OVERVIEW_COLOR_MODES = ("grid", "rating", "cable", "status")
_GRID_COLOR_MODES = ("cable", "feeder", "sizing", "loading", "voltage")
_SIZING_LABELS = {"ampacity": "Feeder sized by ampacity",
                  "end_to_end_voltage": "Feeder upsized for the voltage-drop limit"}


# --------------------------------------------------------------------------- helpers
def _figure(ax: Optional[plt.Axes], figsize: Tuple[float, float]) -> tuple[Figure, plt.Axes]:
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
        return fig, ax
    return ax.get_figure(), ax


def _layer(layers: Mapping[str, Any], name: str) -> Optional[gpd.GeoDataFrame]:
    frame = layers.get(name)
    if frame is None or len(frame) == 0 or not isinstance(frame, gpd.GeoDataFrame):
        return None
    return frame


def _key(frame: pd.DataFrame) -> pd.Series:
    """``(kcid, bcid)`` of every row."""
    return pd.Series(list(zip(frame["kcid"].astype(int), frame["bcid"].astype(int))), index=frame.index)


def grid_colors(grids: gpd.GeoDataFrame) -> dict:
    """``{(kcid, bcid): colour}``: categorical colours, neighbouring stations get different ones."""
    points = {}
    for kcid, bcid, geom in zip(grids["kcid"], grids["bcid"], grids.geometry):
        if geom is not None and not geom.is_empty:
            points[(int(kcid), int(bcid))] = (geom.x, geom.y)
    return st.neighbour_colors(points)


def _bounds(layers: Mapping[str, Any], pad: float = 0.04, min_size: float = 200.0) -> tuple:
    """Extent of the supplied buildings and cables (the postcode polygon is often much larger)."""
    frames = [f for f in (_layer(layers, "buildings"), _layer(layers, "lines")) if f is not None]
    if not frames:
        frames = [f for f in (_layer(layers, "postcode"),) if f is not None]
    if not frames:
        raise ValueError("the layers hold no buildings, cables or postcode polygon to draw")
    bounds = np.array([f.total_bounds for f in frames])
    total = (bounds[:, 0].min(), bounds[:, 1].min(), bounds[:, 2].max(), bounds[:, 3].max())
    return st.padded_bounds(total, pad=pad, min_size=min_size)


def core_bounds(layers: Mapping[str, Any], share: float = 0.8, pad: float = 0.06) -> tuple:
    """Extent of the densest part of the supplied buildings: the ``share`` of buildings closest to their median
    position (the main village of a postcode area with several hamlets)."""
    buildings = _layer(layers, "buildings")
    if buildings is None:
        return _bounds(layers)
    centres = buildings.geometry.centroid
    x, y = centres.x.to_numpy(), centres.y.to_numpy()
    distance = np.hypot(x - np.median(x), y - np.median(y))
    keep = distance <= np.quantile(distance, share)
    return st.padded_bounds((x[keep].min(), y[keep].min(), x[keep].max(), y[keep].max()), pad=pad, min_size=300)


def _extent(layers: Mapping[str, Any], extent: Any) -> tuple:
    if extent is None or extent == "all":
        return _bounds(layers)
    if extent == "core":
        return core_bounds(layers)
    return tuple(float(v) for v in extent)


def _clip(frame: Optional[gpd.GeoDataFrame], bounds: Sequence[float]) -> Optional[gpd.GeoDataFrame]:
    if frame is None:
        return None
    minx, miny, maxx, maxy = bounds
    return frame.cx[minx:maxx, miny:maxy]


def _base(ax: plt.Axes, layers: Mapping[str, Any], bounds: Sequence[float], buildings: bool = True,
          postcode: bool = True) -> None:
    """Postcode boundary, streets and (neutral) buildings."""
    if postcode and _layer(layers, "postcode") is not None:
        layers["postcode"].boundary.plot(ax=ax, color=st.INK_MUTED, lw=0.8, ls="--", zorder=1)
    streets = _clip(_layer(layers, "streets"), bounds)
    if streets is not None and len(streets):
        streets.plot(ax=ax, color=st.STREET, lw=0.8, zorder=2)
    if buildings:
        frame = _clip(_layer(layers, "buildings"), bounds)
        if frame is not None and len(frame):
            frame.plot(ax=ax, color=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, lw=0.2, zorder=3)


def _finish(ax: plt.Axes, bounds: Sequence[float], attribution: Optional[str], scalebar: bool = True) -> None:
    st.map_axes(ax, bounds)
    if scalebar:
        st.add_scalebar(ax)
    st.add_attribution(ax, attribution)


def _draw_lines(ax: plt.Axes, lines: gpd.GeoDataFrame, colors: Sequence, feeder_width: float = 1.3,
                service_width: float = 0.5, by_parallel: bool = False, zorder: int = 5) -> None:
    """Feeders thick, house connections thin; with ``by_parallel`` the width grows with the parallel cables."""
    if lines is None or len(lines) == 0:
        return
    service = lines["is_service"].fillna(False).astype(bool).to_numpy() if "is_service" in lines else \
        np.zeros(len(lines), dtype=bool)
    parallel = pd.to_numeric(lines.get("parallel", 1), errors="coerce").fillna(1).clip(1, 3).to_numpy() \
        if by_parallel else np.ones(len(lines))
    widths = np.where(service, service_width, feeder_width * (0.6 + 0.4 * parallel))
    colors = list(colors)
    for flag, z in ((True, zorder), (False, zorder + 1)):
        mask = service == flag
        if mask.any():
            lines[mask].plot(ax=ax, color=[colors[i] for i in np.flatnonzero(mask)],
                             linewidth=list(widths[mask]), zorder=z)


def _stations(ax: plt.Axes, grids: Optional[gpd.GeoDataFrame], labels: bool, size: float = 140,
              bounds: Optional[Sequence[float]] = None) -> int:
    """Draw the stations; with ``labels`` label them ``kcid/bcid · rating`` where the labels do not overlap."""
    if grids is None:
        return 0
    points = grids[grids.geometry.notna() & ~grids.geometry.is_empty]
    if len(points) == 0:
        return 0
    st.station_marker(ax, points.geometry.x, points.geometry.y, size=size)
    if labels:
        texts = [(geom.x, geom.y, f"{int(kcid)}/{int(bcid)}" + (f" · {rating:g} kVA" if pd.notna(rating) else ""))
                 for kcid, bcid, rating, geom in zip(points["kcid"], points["bcid"], points["transformer_rated_power"],
                                                      points.geometry)]
        distance = None if bounds is None else 0.07 * max(bounds[2] - bounds[0], bounds[3] - bounds[1])
        st.place_labels(ax, texts, min_distance=distance, fontsize=6.8)
    return len(points)


def _station_handle(count: Optional[int] = None) -> Line2D:
    text = "MV/LV station" + (f" ({count})" if count is not None else "")
    return Line2D([], [], marker="*", ls="", color=st.INK, markeredgecolor="white", markersize=10, label=text)


def _plz_of(layers: Mapping[str, Any]) -> Optional[int]:
    for name in ("postcode", "grids"):
        frame = layers.get(name)
        if frame is not None and len(frame) and "plz" in frame:
            return int(frame["plz"].iloc[0])
    return None


def _installed_km(lines: pd.DataFrame) -> pd.Series:
    parallel = pd.to_numeric(lines.get("parallel", 1), errors="coerce").fillna(1)
    return pd.to_numeric(lines["length_km"], errors="coerce").fillna(0) * parallel


# --------------------------------------------------------------------------- overview
def plot_lv_overview(layers: Mapping[str, Any], color_by: str = "grid", labels: Optional[bool] = None,
                     extent: Any = None, ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (7.4, 6.8),
                     title: Optional[str] = None, attribution: Optional[str] = None) -> Figure:
    """All LV grids of a postcode area on one map.

    Args:
        layers: The layers of the postcode area (``read_lv_layers``).
        color_by: ``"grid"`` (one colour per grid; buildings tinted like their grid), ``"rating"`` (station
            rating), ``"cable"`` (cable type by cross-section) or ``"status"`` (power-flow status of the grid).
        labels: Label the stations with ``kcid/bcid · rating``; default: only up to 20 grids.
        extent: ``None`` or ``"all"`` (all supplied buildings), ``"core"`` (the densest part, see
            :func:`core_bounds`) or ``(minx, miny, maxx, maxy)``.
        ax: Axes to draw into; a new figure if None.
        figsize: Size of a new figure.
        title: Title; default names the number of grids and the postcode area.
        attribution: Data attribution in the corner; default :data:`pylovo.plotting.style.DATA_ATTRIBUTION`.

    Returns:
        The figure.
    """
    if color_by not in _OVERVIEW_COLOR_MODES:
        raise ValueError(f"color_by must be one of {', '.join(_OVERVIEW_COLOR_MODES)}")
    grids = _layer(layers, "grids")
    lines = _layer(layers, "lines")
    buildings = _layer(layers, "buildings")
    n_grids = 0 if grids is None else len(grids)
    labels = n_grids <= 20 if labels is None else labels
    with st.style():
        fig, ax = _figure(ax, figsize)
        bounds = _extent(layers, extent)
        handles: list = []
        _base(ax, layers, bounds, buildings=color_by != "grid")
        if color_by == "grid":
            colors = grid_colors(grids) if grids is not None else {}
            if buildings is not None:
                keys = _key(buildings)
                buildings.plot(ax=ax, color=[colors.get(k, st.INK_MUTED) for k in keys], alpha=0.45,
                               edgecolor="none", zorder=3)
            if lines is not None:
                _draw_lines(ax, lines, [colors.get(k, st.INK_MUTED) for k in _key(lines)])
            handles.append(Line2D([], [], color=st.CATEGORICAL[0], lw=2,
                                  label=f"{n_grids} LV grids (neighbouring grids differ in colour)"))
        elif color_by == "rating":
            ratings = grids.set_index(_key(grids))["transformer_rated_power"] if grids is not None else pd.Series()
            colors = st.rating_colors(ratings.dropna())
            if lines is not None:
                rating_of = _key(lines).map(ratings)
                _draw_lines(ax, lines, [colors.get(float(r), st.INK_MUTED) if pd.notna(r) else st.INK_MUTED
                                        for r in rating_of])
            counts = ratings.value_counts()
            handles += [Line2D([], [], color=colors[r], lw=2.5, label=f"{r:g} kVA ({counts.get(r, 0)} grids)")
                        for r in colors]
        elif color_by == "cable":
            if lines is not None:
                colors = st.cable_colors(lines["std_type"].dropna())
                _draw_lines(ax, lines, [colors.get(t, st.INK_MUTED) for t in lines["std_type"]], by_parallel=True)
                km = _installed_km(lines).groupby(lines["std_type"]).sum()
                handles += [Line2D([], [], color=colors[t], lw=2.5, label=f"{t} ({km.get(t, 0):.1f} km)")
                            for t in colors]
        else:
            status = grids.set_index(_key(grids))["power_flow_status"] if grids is not None else pd.Series()
            if lines is not None:
                status_of = _key(lines).map(status)
                _draw_lines(ax, lines, [st.POWER_FLOW_STATUS_COLORS.get(s, st.INK_MUTED) for s in status_of])
            for value, count in status.value_counts().items():
                handles.append(Line2D([], [], color=st.POWER_FLOW_STATUS_COLORS.get(value, st.INK_MUTED), lw=2.5,
                                      label=f"{str(value).replace('_', ' ')} ({count} grids)"))
        n_stations = _stations(ax, grids, labels, bounds=bounds)
        handles += [_station_handle(n_stations),
                    Line2D([], [], color=st.INK_MUTED, lw=0.8, ls="--", label="Postcode boundary")]
        _finish(ax, bounds, attribution)
        st.legend_below(ax, handles, ncol=2 if color_by == "grid" else 3,
                        title="Cable type (installed length, parallel cables counted)" if color_by == "cable" else None)
        plz = _plz_of(layers)
        ax.set_title(title if title is not None else
                     f"{n_grids} LV grids of postcode area {plz}" if plz else f"{n_grids} LV grids")
    return fig


# --------------------------------------------------------------------------- one grid
def _grid_rows(frame: Optional[gpd.GeoDataFrame], kcid: int, bcid: int) -> Optional[gpd.GeoDataFrame]:
    if frame is None or "kcid" not in frame:
        return None
    return frame[(frame["kcid"].astype(int) == int(kcid)) & (frame["bcid"].astype(int) == int(bcid))]


def _consumer_voltages(buildings: gpd.GeoDataFrame, net: Any) -> pd.Series:
    """Voltage (p.u.) at the consumer bus of every building, through the loads of the net (``consumer_vertex``)."""
    loads = net.load
    if "consumer_vertex" not in loads or net.res_bus.empty:
        return pd.Series(np.nan, index=buildings.index)
    vm = net.res_bus["vm_pu"]
    vertex_vm = loads.assign(vm=loads["bus"].map(vm)).groupby("consumer_vertex")["vm"].min()
    return buildings["vertice_id"].map(vertex_vm) if "vertice_id" in buildings else pd.Series(np.nan, index=buildings.index)


def plot_lv_grid(layers: Mapping[str, Any], kcid: int, bcid: int, color_by: str = "cable",
                 buildings: Optional[str] = "peak_load", net: Any = None, ax: Optional[plt.Axes] = None,
                 figsize: Tuple[float, float] = (7.4, 6.6), title: Optional[str] = None,
                 attribution: Optional[str] = None, split_points: bool = True) -> Figure:
    """One LV grid on its streets.

    Args:
        layers: The layers of the postcode area or of the grid (``read_lv_layers``).
        kcid: K-means cluster id of the grid.
        bcid: Building cluster id of the grid.
        color_by: Colour of the cables: ``"cable"`` (type by cross-section, width by parallel cables),
            ``"feeder"`` (one colour per feeder leaving the station), ``"sizing"`` (sized by ampacity or upsized
            for the voltage-drop limit), ``"loading"`` (line loading of the power flow, needs ``net``) or
            ``"voltage"`` (buildings by the voltage at their house connection, needs ``net``).
        buildings: Colour of the buildings of the grid: ``"peak_load"``, ``"use"`` or None (neutral).
            Ignored for ``color_by="voltage"``.
        net: The pandapower net of the grid with power-flow results (``read_net_db``), for ``"loading"``,
            ``"voltage"`` and ``"feeder"`` (the feeders follow the topology of the net if given).
        ax: Axes to draw into; a new figure if None.
        figsize: Size of a new figure.
        title: Title; default names the grid and its station rating.
        attribution: Data attribution; default :data:`pylovo.plotting.style.DATA_ATTRIBUTION`.
        split_points: Mark the branch points of the feeders.

    Returns:
        The figure.

    Raises:
        ValueError: An unknown ``color_by``, a grid without cables, or ``"loading"``/``"voltage"`` without ``net``.
    """
    if color_by not in _GRID_COLOR_MODES:
        raise ValueError(f"color_by must be one of {', '.join(_GRID_COLOR_MODES)}")
    if color_by in ("loading", "voltage") and net is None:
        raise ValueError(f"color_by={color_by!r} needs the pandapower net of the grid (net=...)")
    lines = _grid_rows(_layer(layers, "lines"), kcid, bcid)
    if lines is None or len(lines) == 0:
        raise ValueError(f"no cables of grid kcid={kcid}, bcid={bcid} in the layers")
    grid_buildings = _grid_rows(_layer(layers, "buildings"), kcid, bcid)
    grid_row = _grid_rows(_layer(layers, "grids"), kcid, bcid)
    with st.style():
        fig, ax = _figure(ax, figsize)
        minx, miny, maxx, maxy = lines.total_bounds
        bounds = st.padded_bounds((minx, miny, maxx, maxy), pad=0.06, min_size=150)
        _base(ax, layers, bounds, postcode=False)
        handles: list = []
        colorbars: list = []

        # buildings of the grid
        if grid_buildings is not None and len(grid_buildings):
            if color_by == "voltage":
                vm = _consumer_voltages(grid_buildings, net)
                low = min(np.nanmin(vm) if vm.notna().any() else 0.9, 0.95)
                norm = Normalize(vmin=low, vmax=float(np.nanmax(vm)) if vm.notna().any() else 1.0)
                cmap = st.voltage_cmap()
                grid_buildings.plot(ax=ax, color=[cmap(norm(v)) if pd.notna(v) else st.INK_MUTED for v in vm],
                                    edgecolor=st.SURFACE, lw=0.3, zorder=4)
                colorbars.append((ScalarMappable(norm=norm, cmap=cmap), "Voltage at the house connection (p.u.)"))
            elif color_by == "loading" or buildings is None:
                grid_buildings.plot(ax=ax, color="#cfd9e6", edgecolor=st.BUILDING_EDGE, lw=0.3, zorder=4)
            elif buildings == "peak_load":
                values = pd.to_numeric(grid_buildings["peak_load_in_kw"], errors="coerce")
                norm = Normalize(vmin=0, vmax=max(float(values.quantile(0.95)), 1.0))
                cmap = st.sequential_cmap("load")
                grid_buildings.plot(ax=ax, color=[cmap(norm(v)) for v in values.fillna(0)], edgecolor=st.SURFACE,
                                    lw=0.3, zorder=4)
                colorbars.append((ScalarMappable(norm=norm, cmap=cmap), "Building peak load (kW)"))
            elif buildings == "use":
                uses = grid_buildings["building_use"].where(grid_buildings["building_use"].isin(st.USE_COLORS),
                                                            "Other")
                grid_buildings.plot(ax=ax, color=[st.USE_COLORS.get(u, st.INK_MUTED) for u in uses],
                                    edgecolor=st.SURFACE, lw=0.3, zorder=4)
                counts = uses.value_counts()
                handles += [Patch(color=st.USE_COLORS.get(u, st.INK_MUTED), label=f"{u} ({n})")
                            for u, n in counts.items()]

        # cables
        if color_by == "cable":
            colors = st.cable_colors(lines["std_type"].dropna())
            _draw_lines(ax, lines, [colors.get(t, st.INK_MUTED) for t in lines["std_type"]], feeder_width=1.8,
                        service_width=0.7, by_parallel=True)
            km = _installed_km(lines).groupby(lines["std_type"]).sum()
            handles += [Line2D([], [], color=colors[t], lw=2.5, label=f"{t} ({km.get(t, 0) * 1000:.0f} m)")
                        for t in colors]
        elif color_by == "feeder":
            feeders = feeder_ids(net) if net is not None else _feeders_of_lines(lines, grid_row)
            feeder_of = lines["pp_index"].map(feeders) if "pp_index" in lines else pd.Series(np.nan, index=lines.index)
            palette = st.categorical_colors(feeder_of.dropna().astype(int))
            _draw_lines(ax, lines, [palette.get(int(f), st.INK_MUTED) if pd.notna(f) else st.INK_MUTED
                                    for f in feeder_of], feeder_width=1.8, service_width=0.7)
            handles += [Line2D([], [], color=palette[f], lw=2.5, label=f"Feeder {f}") for f in sorted(palette)]
        elif color_by == "sizing":
            basis = lines["feeder_sizing_basis"].fillna("ampacity")
            colors = {"ampacity": st.CATEGORICAL[0], "end_to_end_voltage": st.CATEGORICAL[1]}
            line_colors = [st.INK_MUTED if s else colors.get(b, st.CATEGORICAL[0])
                           for s, b in zip(lines["is_service"].fillna(False), basis)]
            _draw_lines(ax, lines, line_colors, feeder_width=1.8, service_width=0.7)
            handles += [Line2D([], [], color=colors[b], lw=2.5, label=_SIZING_LABELS[b]) for b in colors]
            handles.append(Line2D([], [], color=st.INK_MUTED, lw=1, label="House connection"))
        elif color_by == "loading":
            loading = net.res_line["loading_percent"] if not net.res_line.empty else pd.Series(dtype=float)
            values = lines["pp_index"].map(loading)
            norm = Normalize(vmin=0, vmax=max(100.0, float(np.nanmax(values)) if values.notna().any() else 100.0))
            cmap = st.sequential_cmap("loading")
            _draw_lines(ax, lines, [cmap(norm(v)) if pd.notna(v) else st.INK_MUTED for v in values],
                        feeder_width=2.0, service_width=0.8)
            colorbars.append((ScalarMappable(norm=norm, cmap=cmap), "Line loading (%)"))
        else:
            _draw_lines(ax, lines, [st.INK_SECONDARY] * len(lines), feeder_width=1.4, service_width=0.5)

        if split_points:
            points = _grid_rows(_layer(layers, "split_points"), kcid, bcid)
            if points is not None and len(points):
                ax.scatter(points.geometry.x, points.geometry.y, marker="s", s=14, color=st.SURFACE,
                           edgecolor=st.INK_SECONDARY, lw=0.9, zorder=8)
                handles.append(Line2D([], [], marker="s", ls="", color=st.SURFACE, markeredgecolor=st.INK_SECONDARY,
                                      markersize=5, label="Branch point"))
        _stations(ax, grid_row, labels=False, size=220)
        handles.append(_station_handle())
        _finish(ax, bounds, attribution)
        for mappable, text in colorbars:
            bar = fig.colorbar(mappable, ax=ax, shrink=0.55, pad=0.01, aspect=25)
            bar.set_label(text, fontsize=8, color=st.INK_SECONDARY)
            bar.outline.set_visible(False)
            bar.ax.tick_params(labelsize=7)
        if handles:
            st.legend_below(ax, handles, ncol=3)
        if title is None:
            rating = None if grid_row is None or len(grid_row) == 0 else grid_row["transformer_rated_power"].iloc[0]
            n_buildings = 0 if grid_buildings is None else len(grid_buildings)
            title = (f"Grid {int(kcid)}/{int(bcid)}" + (f" · {rating:g} kVA" if rating is not None and pd.notna(rating)
                                                        else "") + f" · {n_buildings} buildings")
        ax.set_title(title)
    return fig


def _feeders_of_lines(lines: gpd.GeoDataFrame, grid_row: Optional[gpd.GeoDataFrame]) -> pd.Series:
    """Feeder number of every line (by ``pp_index``) from the line topology of the layer: each line that leaves
    the LV bus of the transformer (``grids.lv_bus``) starts a feeder."""
    import networkx as nx

    if grid_row is None or len(grid_row) == 0 or "lv_bus" not in grid_row or pd.isna(grid_row["lv_bus"].iloc[0]):
        return pd.Series(dtype=float)
    root = int(grid_row["lv_bus"].iloc[0])
    graph = nx.Graph()
    for row in lines.itertuples():
        graph.add_edge(int(row.from_bus), int(row.to_bus), index=int(row.pp_index))
    return _feeder_numbers(graph, root)


# --------------------------------------------------------------------------- pipeline steps
def plot_building_uses(layers: Mapping[str, Any], extent: Any = None, ax: Optional[plt.Axes] = None,
                       figsize: Tuple[float, float] = (7.4, 6.8), title: Optional[str] = None,
                       attribution: Optional[str] = None) -> Figure:
    """The supplied buildings by use, the buildings supplied from the MV grid and the MV/LV stations.

    ``extent`` as for :func:`plot_lv_overview`.
    """
    buildings = _layer(layers, "buildings")
    with st.style():
        fig, ax = _figure(ax, figsize)
        bounds = _extent(layers, extent)
        _base(ax, layers, bounds, buildings=False)
        handles: list = []
        if buildings is not None:
            uses = buildings["building_use"].where(buildings["building_use"].isin(st.USE_COLORS), "Other")
            use_colors = [st.USE_COLORS.get(u, st.INK_MUTED) for u in uses]
            buildings.plot(ax=ax, color=use_colors, edgecolor="white", lw=0.2, zorder=3)
            if (bounds[2] - bounds[0]) > 1500:
                centres = buildings.geometry.centroid
                ax.scatter(centres.x, centres.y, s=4, color=use_colors, lw=0, zorder=4)
            counts = uses.value_counts()
            handles += [Patch(color=st.USE_COLORS.get(u, st.INK_MUTED), label=f"{u} ({counts.get(u, 0)})")
                        for u in [*st.USE_COLORS, "Other"] if counts.get(u, 0)]
        customers = _layer(layers, "mv_customers")
        if customers is not None:
            customers.plot(ax=ax, color=st.INK, edgecolor=st.INK, lw=0.6, zorder=4)
            handles.append(Patch(color=st.INK, label=f"Supplied from the MV grid ({len(customers)})"))
        n = _stations(ax, _layer(layers, "grids"), labels=False, size=90)
        handles += [_station_handle(n), Line2D([], [], color=st.STREET, lw=1.5, label="Street network")]
        _finish(ax, bounds, attribution)
        st.legend_below(ax, handles, ncol=3)
        plz = _plz_of(layers)
        ax.set_title(title if title is not None else f"Buildings of postcode area {plz} by use")
    return fig


def plot_partition(layers: Mapping[str, Any], level: str = "bcid", labels: Optional[bool] = None,
                   extent: Any = None, ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (7.4, 6.8),
                   title: Optional[str] = None, attribution: Optional[str] = None) -> Figure:
    """The buildings coloured by their k-means cluster (``level="kcid"``) or by their grid (``"bcid"``).

    The k-means clusters bound the size of the distance matrices; each of them is divided into building
    clusters, one per MV/LV station and thus one per grid. A dot at every building keeps small footprints
    visible on large maps. ``extent`` as for :func:`plot_lv_overview`.
    """
    if level not in ("kcid", "bcid"):
        raise ValueError("level must be 'kcid' or 'bcid'")
    buildings = _layer(layers, "buildings")
    grids = _layer(layers, "grids")
    if buildings is None:
        raise ValueError("the layers hold no buildings")
    with st.style():
        fig, ax = _figure(ax, figsize)
        bounds = _extent(layers, extent)
        _base(ax, layers, bounds, buildings=False)
        handles: list = []
        centres = buildings.geometry.centroid
        dot = 4 if (bounds[2] - bounds[0]) > 1500 else 0
        if level == "bcid":
            colors = grid_colors(grids) if grids is not None else {}
            keys = _key(buildings)
            building_colors = [colors.get(k, st.INK_MUTED) for k in keys]
            buildings.plot(ax=ax, color=building_colors, edgecolor="white", lw=0.2, zorder=3)
            if dot:
                ax.scatter(centres.x, centres.y, s=dot, color=building_colors, lw=0, zorder=4)
            labels = (grids is not None and len(grids) <= 20) if labels is None else labels
            n = _stations(ax, grids, labels, bounds=bounds)
            handles += [Patch(color=st.CATEGORICAL[0], label=f"{keys.nunique()} building clusters, one per grid"),
                        _station_handle(n)]
        else:
            clusters = sorted(buildings["kcid"].astype(int).unique())
            cluster_centres = {k: (g.geometry.centroid.x.mean(), g.geometry.centroid.y.mean())
                               for k, g in buildings.groupby(buildings["kcid"].astype(int))}
            colors = st.neighbour_colors(cluster_centres)
            building_colors = [colors[int(k)] for k in buildings["kcid"]]
            buildings.plot(ax=ax, color=building_colors, edgecolor="white", lw=0.2, zorder=3)
            if dot:
                ax.scatter(centres.x, centres.y, s=dot, color=building_colors, lw=0, zorder=4)
            for k in clusters:
                st.label(ax, *cluster_centres[k], f"kcid {k}")
            counts = buildings["kcid"].astype(int).value_counts()
            handles += [Patch(color=colors[k], label=f"kcid {k} ({counts[k]} buildings)") for k in clusters]
            _stations(ax, grids, labels=False, size=70)
            handles.append(_station_handle())
        _finish(ax, bounds, attribution)
        st.legend_below(ax, handles, ncol=3)
        plz = _plz_of(layers)
        what = "k-means clusters" if level == "kcid" else "Building clusters and their stations"
        ax.set_title(title if title is not None else f"{what} of postcode area {plz}")
    return fig


def plot_connections(layers: Mapping[str, Any], center: Optional[Tuple[float, float]] = None,
                     size_m: Tuple[float, float] = (520.0, 360.0), ax: Optional[plt.Axes] = None,
                     figsize: Tuple[float, float] = (7.4, 5.4), title: Optional[str] = None,
                     attribution: Optional[str] = None) -> Figure:
    """A detail of the street graph with the connection lines from the supplied buildings to the streets.

    Args:
        center: Map centre in map units; default the station of the grid with the most buildings, else the centre
            of the buildings.
        size_m: Width and height of the detail in metres.
    """
    buildings = _layer(layers, "buildings")
    if center is None:
        grids = _layer(layers, "grids")
        points = None if grids is None else grids[grids.geometry.notna() & ~grids.geometry.is_empty]
        if points is not None and len(points):
            largest = points.sort_values("buildings", ascending=False).geometry.iloc[0] \
                if "buildings" in points else points.geometry.iloc[0]
            center = (largest.x, largest.y)
        elif buildings is not None:
            c = buildings.geometry.union_all().centroid
            center = (c.x, c.y)
        else:
            raise ValueError("pass center=(x, y): the layers hold no station or building")
    half_w, half_h = size_m[0] / 2, size_m[1] / 2
    bounds = (center[0] - half_w, center[1] - half_h, center[0] + half_w, center[1] + half_h)
    with st.style():
        fig, ax = _figure(ax, figsize)
        streets = _clip(_layer(layers, "streets"), bounds)
        if streets is not None and len(streets):
            streets.plot(ax=ax, color=st.STREET, lw=1.6, zorder=2)
        visible = _clip(buildings, bounds)
        if visible is not None and len(visible):
            visible.plot(ax=ax, color=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, lw=0.4, zorder=3)
        connections = _clip(_layer(layers, "connections"), bounds)
        if connections is not None and buildings is not None and "vertice_id" in buildings and "source" in connections:
            vertices = set(buildings["vertice_id"].dropna().astype(int))
            connections = connections[connections["source"].isin(vertices) | connections["target"].isin(vertices)]
        if connections is not None and len(connections):
            connections.plot(ax=ax, color=st.CATEGORICAL[1], lw=1.0, zorder=4)
        if visible is not None and len(visible):
            centroids = visible.geometry.centroid
            ax.scatter(centroids.x, centroids.y, s=6, color=st.INK_SECONDARY, zorder=5, lw=0)
        _finish(ax, bounds, attribution)
        handles = [
            Line2D([], [], color=st.STREET, lw=2, label="Street segment"),
            Patch(facecolor=st.BUILDING_NEUTRAL, edgecolor=st.BUILDING_EDGE, label="Building"),
            Line2D([], [], color=st.CATEGORICAL[1], lw=1.5, label="Connection line (building to street)"),
            Line2D([], [], marker="o", ls="", color=st.INK_SECONDARY, markersize=3, label="Building centre"),
        ]
        st.legend_below(ax, handles, ncol=2)
        ax.set_title(title if title is not None else "Street graph and connection lines (detail)")
    return fig
