"""Maps of the MV grids of the MV stage (:mod:`pylovo.mv_grid`).

- :func:`plot_mv_overview`: supply districts, HV/MV substations, lines and normally-open points of a variant.
- :func:`plot_mv_grid_map`: one MV grid (supply district), coloured by ring, line kind, loading, voltage or tap.
- :func:`plot_mv_ring`: one ring with its two halves, open point, joints, stubs and numbered members.
- :func:`plot_lv_busbar_voltage_map`: LV busbar voltage per station (voltage budget or coupled) and band conflicts.
- :func:`plot_variants_side_by_side`: the same district in several MV variants.
- :func:`interactive_mv_net`: a stored MV net on an interactive map (plotly, through pandapower).
- :func:`plot_mv_grid`: read the layers of one grid from the database and draw them.
- :func:`ring_paths`: halves, main path, member order and feeding distances of one ring.

**Layers.** The drawing functions take the dictionary returned by ``MvResultMixin.read_mv_layers``: GeoDataFrames
in ``TARGET_EPSG`` with the keys ``nodes`` (rows of ``pylovo.mv_node``), ``lines`` (``pylovo.mv_line``),
``substations`` (``pylovo.mv_substation``), ``grids`` (``pylovo.mv_grid`` without ``net``) and ``settlements``
(``pylovo.mv_graph_settlement``), with the column names of these tables. Only ``nodes`` and ``lines`` are required;
missing layers and optional columns are skipped. Nodes may also be plain data frames with ``x`` and ``y`` columns;
lines without a geometry are drawn as straight segments between their nodes.

**Drawing conventions.** Overhead lines are dashed, mixed lines dash-dotted, stub lines thinner, ties to a
neighbouring district grey and dotted. A normally-open point (NOP) is an open red circle at the node that holds the
open switch (``open_at_node``, else ``to_node``). Busbars are triangles, stations dots, MV customers diamonds,
generators stars and joints small grey points. Basemaps come from OpenStreetMap through contextily (internet access
needed); a basemap that cannot be loaded is skipped with a warning, so the maps also render offline.

Needs the ``plots`` extra. The functions return a matplotlib ``Figure`` (plotly ``go.Figure`` for
:func:`interactive_mv_net`) and never call ``plt.show()``.
"""

from __future__ import annotations

import math
import warnings
from typing import Any, Mapping, Optional, Sequence, Tuple

import contextily as cx
import geopandas as gpd
import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.colors import BoundaryNorm, ListedColormap, Normalize
from matplotlib.cm import ScalarMappable
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from pylovo.config_loader import TARGET_EPSG
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.utils import OSM_TILE_HEADERS, pandapower_on_map

__all__ = [
    "LINE_KIND_COLORS",
    "LINE_KIND_STYLES",
    "NODE_STYLES",
    "OSM_ATTRIBUTION",
    "interactive_mv_net",
    "plot_lv_busbar_voltage_map",
    "plot_mv_grid",
    "plot_mv_grid_map",
    "plot_mv_overview",
    "plot_mv_ring",
    "plot_variants_side_by_side",
    "ring_paths",
]

#: Attribution drawn on every basemap.
OSM_ATTRIBUTION = "Map data © OpenStreetMap contributors (ODbL)"

#: Marker, size and colour of each node kind (``mv_node.kind``).
NODE_STYLES: dict[str, dict[str, Any]] = {
    "busbar": {"marker": "^", "s": 150, "color": "#2c3e50", "zorder": 7},
    "station": {"marker": "o", "s": 18, "color": "#1f77b4", "zorder": 5},
    "customer": {"marker": "D", "s": 30, "color": "#ff7f0e", "zorder": 6},
    "generator": {"marker": "*", "s": 80, "color": "#2ca02c", "zorder": 6},
    "joint": {"marker": "s", "s": 9, "color": "#7f7f7f", "zorder": 4},
}
#: Colour of each line kind (``mv_line.kind``) when maps are coloured by kind.
LINE_KIND_COLORS: dict[str, str] = {"cable": "#1f77b4", "overhead": "#d62728", "mixed": "#9467bd"}
#: Line style of each line kind.
LINE_KIND_STYLES: dict[str, str] = {"cable": "-", "overhead": "--", "mixed": "-."}

_TIE_COLOR = "#9a9a9a"
_MUTED_LINE_COLOR = "#8c8c8c"
_NOP_COLOR = "#d62728"
_HALF_COLORS = {"a": "#1f77b4", "b": "#ff7f0e"}
_BASE_WIDTH = 1.6
_STUB_WIDTH = 0.8
_COLOR_MODES = ("ring", "kind", "loading", "voltage", "tap")
_MEMBER_KINDS = ("station", "customer", "generator")
_LV_COLUMN_LABELS = {
    "lv_busbar_vm_pu": "LV busbar voltage at the LV design point (p.u.)",
    "lv_min_vm_pu": "Lowest LV voltage at the LV design point (p.u.)",
    "lv_busbar_vm_feed_in_pu": "LV busbar voltage in feed-in (p.u.)",
    "coupled_lv_busbar_vm_pu": "LV busbar voltage, coupled design point (p.u.)",
    "coupled_lv_min_vm_pu": "Lowest LV voltage, coupled design point (p.u.)",
    "coupled_lv_busbar_vm_feed_in_pu": "LV busbar voltage in feed-in, coupled (p.u.)",
    "coupled_lv_max_vm_pu": "Highest LV voltage in feed-in, coupled (p.u.)",
    "coupled_op_lv_busbar_vm_pu": "LV busbar voltage, merged operating point (p.u.)",
}


# ---------------------------------------------------------------------------------------------------------------
# Table helpers
# ---------------------------------------------------------------------------------------------------------------


def _geometry(frame: Any) -> Optional[gpd.GeoSeries]:
    """The active geometry of ``frame``, or None for a plain data frame or one without an active geometry."""
    if not isinstance(frame, gpd.GeoDataFrame):
        return None
    try:
        return frame.geometry
    except AttributeError:
        return None


def _as_bool(series: pd.Series, default: bool = False) -> pd.Series:
    """Booleans with NULL as ``default``."""
    return series.astype("boolean").fillna(default).astype(bool)


def _column(frame: pd.DataFrame, name: str, default: Any = np.nan) -> pd.Series:
    """Column ``name`` of ``frame``, or a constant series if it is missing."""
    if name in frame.columns:
        return frame[name]
    return pd.Series(default, index=frame.index, dtype=object if isinstance(default, str) or default is None
                     else None)


def _node_table(nodes: Any) -> pd.DataFrame:
    """Nodes indexed by ``mv_node_id`` (or their index) with float ``x`` and ``y`` columns."""
    if nodes is None or len(nodes) == 0:
        return pd.DataFrame(columns=["kind", "x", "y"], index=pd.Index([], name="mv_node_id"))
    geom = _geometry(nodes)
    table = pd.DataFrame(nodes).copy()
    if geom is not None:
        table = table.drop(columns=[geom.name], errors="ignore")
        table["x"] = geom.x.to_numpy(dtype=float)
        table["y"] = geom.y.to_numpy(dtype=float)
    elif not {"x", "y"} <= set(table.columns):
        raise ValueError("MV nodes need a point geometry or x and y columns")
    if "mv_node_id" in table.columns:
        table = table.set_index("mv_node_id", drop=False)
    table.index.name = "mv_node_id"
    if "kind" not in table.columns:
        table["kind"] = "station"
    return table


def _line_table(lines: Any) -> pd.DataFrame:
    """Lines indexed by ``mv_line_id`` (or their index), with the geometry in column ``_geom`` when present."""
    if lines is None or len(lines) == 0:
        return pd.DataFrame(columns=["from_node", "to_node", "_geom"], index=pd.Index([], name="mv_line_id"))
    geom = _geometry(lines)
    table = pd.DataFrame(lines).copy()
    if geom is not None:
        table = table.drop(columns=[geom.name], errors="ignore")
        table["_geom"] = list(geom)
    else:
        table["_geom"] = None
    if "mv_line_id" in table.columns:
        table = table.set_index("mv_line_id", drop=False)
    table.index.name = "mv_line_id"
    for name in ("from_node", "to_node"):
        if name not in table.columns:
            raise ValueError(f"MV lines need the column {name!r}")
    return table


def _layer(layers: Mapping[str, Any], key: str) -> Any:
    value = layers.get(key) if layers is not None else None
    if value is None or len(value) == 0:
        return None
    return value


def _crs(layers: Mapping[str, Any]) -> Any:
    """CRS of the first layer that has one, else ``EPSG:TARGET_EPSG``."""
    for key in ("nodes", "lines", "grids", "substations", "settlements"):
        layer = _layer(layers, key)
        if isinstance(layer, gpd.GeoDataFrame) and _geometry(layer) is not None and layer.crs is not None:
            return layer.crs
    return f"EPSG:{TARGET_EPSG}"


def _parts(geom: Any) -> list[np.ndarray]:
    """Coordinate arrays of the line parts of ``geom`` (LineString or MultiLineString)."""
    if geom is None or getattr(geom, "is_empty", True):
        return []
    if hasattr(geom, "geoms"):
        return [np.asarray(part.coords)[:, :2] for part in geom.geoms if not part.is_empty]
    return [np.asarray(geom.coords)[:, :2]]


def _segments(lines: pd.DataFrame, nodes: pd.DataFrame) -> list[list[np.ndarray]]:
    """Coordinate arrays per line: its geometry, else the straight segment between its nodes."""
    out: list[list[np.ndarray]] = []
    geoms = lines["_geom"] if "_geom" in lines.columns else [None] * len(lines)
    for geom, a, b in zip(geoms, lines["from_node"], lines["to_node"]):
        parts = _parts(geom)
        if not parts and a in nodes.index and b in nodes.index:
            start = nodes.loc[a, ["x", "y"]].to_numpy(dtype=float)
            end = nodes.loc[b, ["x", "y"]].to_numpy(dtype=float)
            parts = [np.vstack([start, end])]
        out.append(parts)
    return out


def _figure(ax: Optional[plt.Axes], figsize: Tuple[float, float]) -> tuple[Figure, plt.Axes]:
    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()
    return fig, ax


def _draw_lines(ax: plt.Axes, lines: pd.DataFrame, nodes: pd.DataFrame, colors: Sequence[Any],
                widths: Sequence[float], styles: Sequence[str], *, zorder: float = 2, alpha: float = 1.0
                ) -> Optional[LineCollection]:
    """Draw the lines as one LineCollection with per-line colour, width and style."""
    segs, cols, lws, lss = [], [], [], []
    for parts, color, width, style in zip(_segments(lines, nodes), colors, widths, styles):
        for part in parts:
            segs.append(part)
            cols.append(color)
            lws.append(width)
            lss.append(style)
    if not segs:
        return None
    collection = LineCollection(segs, colors=cols, linewidths=lws, linestyles=lss, zorder=zorder, alpha=alpha)
    ax.add_collection(collection)
    ax.autoscale_view()
    return collection


def _line_styles(lines: pd.DataFrame) -> tuple[list[float], list[str]]:
    """Width (stubs thinner, ties thin) and style (by kind, ties dotted) of every line."""
    role = _column(lines, "role", "ring").fillna("ring").astype(str)
    kind = _column(lines, "kind", "cable").fillna("cable").astype(str)
    parallel = pd.to_numeric(_column(lines, "parallel", 1), errors="coerce").fillna(1).clip(lower=1)
    widths, styles = [], []
    for r, k, p in zip(role, kind, parallel):
        if r == "tie":
            widths.append(1.0)
            styles.append(":")
            continue
        width = _STUB_WIDTH if r == "stub" else _BASE_WIDTH
        widths.append(width * (1.0 + 0.5 * (p - 1)))
        styles.append(LINE_KIND_STYLES.get(k, "-"))
    return widths, styles


def _palette() -> list[Any]:
    """The 20 ``tab20`` colours, the ten dark shades first."""
    cmap = plt.get_cmap("tab20")
    return [cmap(i) for i in (*range(0, 20, 2), *range(1, 20, 2))]


def _ring_colors(values: pd.Series) -> list[Any]:
    """A categorical colour per ring id (NaN: grey)."""
    palette = _palette()
    return [_MUTED_LINE_COLOR if pd.isna(value) else palette[int(value) % len(palette)] for value in values]


def _draw_nodes(ax: plt.Axes, nodes: pd.DataFrame, colors: Optional[Sequence[Any]] = None, *,
                kinds: Sequence[str] = ("joint", "station", "customer", "generator", "busbar"),
                size_scale: float = 1.0, edgecolor: Any = "white") -> None:
    """Scatter the nodes by kind (marker and size from ``NODE_STYLES``), optionally with one colour per node."""
    if nodes.empty:
        return
    kind = nodes["kind"].astype(str)
    colors = list(colors) if colors is not None else None
    for k in kinds:
        mask = (kind == k).to_numpy()
        if not mask.any():
            continue
        style = NODE_STYLES.get(k, NODE_STYLES["station"])
        if colors is None or k == "busbar":
            color: Any = style["color"]
        else:
            color = [c for c, m in zip(colors, mask) if m]
        ax.scatter(nodes["x"].to_numpy()[mask], nodes["y"].to_numpy()[mask], marker=style["marker"],
                   s=style["s"] * size_scale, c=color, edgecolors=edgecolor if k != "joint" else "none",
                   linewidths=0.4, zorder=style["zorder"])


def _nop_points(lines: pd.DataFrame, nodes: pd.DataFrame) -> pd.DataFrame:
    """Positions of the normally-open points of the non-tie lines (``open_at_node``, else ``to_node``)."""
    if lines.empty or "normally_open" not in lines.columns:
        return pd.DataFrame(columns=["node", "x", "y"])
    is_open = _as_bool(lines["normally_open"])
    role = _column(lines, "role", "ring").fillna("ring").astype(str)
    sel = lines[is_open & (role != "tie")]
    if sel.empty:
        return pd.DataFrame(columns=["node", "x", "y"])
    at = _column(sel, "open_at_node")
    node_ids = [int(a) if pd.notna(a) else int(t) for a, t in zip(at, sel["to_node"])]
    rows = [(n, nodes.at[n, "x"], nodes.at[n, "y"]) for n in node_ids if n in nodes.index]
    return pd.DataFrame(rows, columns=["node", "x", "y"])


def _draw_nops(ax: plt.Axes, lines: pd.DataFrame, nodes: pd.DataFrame, size: float = 120) -> int:
    points = _nop_points(lines, nodes)
    if not points.empty:
        ax.scatter(points["x"], points["y"], s=size, facecolors="none", edgecolors=_NOP_COLOR, linewidths=1.5,
                   zorder=8)
    return len(points)


def _add_basemap(ax: plt.Axes, crs: Any) -> None:
    """Add an OpenStreetMap basemap; a failure (e.g. no internet access) is a warning, not an error."""
    crs = crs.to_string() if hasattr(crs, "to_string") else crs
    try:
        cx.add_basemap(ax, crs=crs, source=cx.providers.OpenStreetMap.Mapnik, headers=OSM_TILE_HEADERS,
                       attribution=OSM_ATTRIBUTION)
    except Exception as exc:   # noqa: BLE001 - the map is still useful without tiles
        warnings.warn(f"OpenStreetMap basemap not drawn: {exc}", stacklevel=3)


def _finish_map(ax: plt.Axes) -> None:
    ax.set_aspect("equal")
    ax.set_xticks([])
    ax.set_yticks([])


def _grid_of_nodes(nodes: pd.DataFrame, mv_grid_id: int) -> pd.DataFrame:
    if "mv_grid_id" not in nodes.columns:
        return nodes
    return nodes[nodes["mv_grid_id"] == mv_grid_id]


def _grid_lines(lines: pd.DataFrame, node_ids: Any, mv_grid_id: int) -> pd.DataFrame:
    """Lines owned by the grid plus the ties that end at one of its nodes."""
    if lines.empty:
        return lines
    if "mv_grid_id" in lines.columns:
        own = lines["mv_grid_id"] == mv_grid_id
    else:
        own = lines["from_node"].isin(node_ids) | lines["to_node"].isin(node_ids)
    role = _column(lines, "role", "ring").fillna("ring").astype(str)
    tie = (role == "tie") & (lines["from_node"].isin(node_ids) | lines["to_node"].isin(node_ids))
    return lines[own | tie]


def _bounds(nodes: pd.DataFrame, lines: pd.DataFrame, margin: float = 0.05) -> tuple[float, float, float, float]:
    xs, ys = list(nodes["x"].dropna()), list(nodes["y"].dropna())
    for parts in _segments(lines, nodes):
        for part in parts:
            xs.extend(part[:, 0])
            ys.extend(part[:, 1])
    if not xs:
        return (0.0, 0.0, 1.0, 1.0)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    pad = margin * max(x1 - x0, y1 - y0, 200.0)
    return (x0 - pad, y0 - pad, x1 + pad, y1 + pad)


def _grid_row(layers: Mapping[str, Any], mv_grid_id: int) -> Optional[pd.Series]:
    grids = _layer(layers, "grids")
    if grids is None or "mv_grid_id" not in grids.columns:
        return None
    rows = grids[grids["mv_grid_id"] == mv_grid_id]
    return None if rows.empty else rows.iloc[0]


def _draw_settlements(ax: plt.Axes, layers: Mapping[str, Any], bounds: tuple[float, float, float, float]) -> bool:
    settlements = _layer(layers, "settlements")
    if settlements is None or _geometry(settlements) is None:
        return False
    x0, y0, x1, y1 = bounds
    sel = settlements.cx[x0:x1, y0:y1]
    if sel.empty:
        return False
    urban = _as_bool(_column(sel, "urban", False))
    if (~urban).any():
        sel[~urban.to_numpy()].plot(ax=ax, facecolor="#efe6d6", edgecolor="none", alpha=0.7, zorder=0)
    if urban.any():
        sel[urban.to_numpy()].plot(ax=ax, facecolor="#e0cfb1", edgecolor="none", alpha=0.7, zorder=0)
    return True


def _legend_handles(lines: pd.DataFrame, nodes: pd.DataFrame, n_nops: int, *, kind_colors: bool = False
                    ) -> list[Any]:
    """Legend proxies for the line kinds and roles and the node kinds present (kind colours or neutral)."""
    handles: list[Any] = []
    kind = set(_column(lines, "kind", "cable").dropna().astype(str))
    role = set(_column(lines, "role", "ring").dropna().astype(str))
    for k in ("cable", "overhead", "mixed"):
        if k in kind:
            handles.append(Line2D([], [], color=LINE_KIND_COLORS[k] if kind_colors else "#333333",
                                  linestyle=LINE_KIND_STYLES[k], linewidth=_BASE_WIDTH, label=f"{k} line"))
    if "stub" in role:
        handles.append(Line2D([], [], color="#333333", linewidth=_STUB_WIDTH, label="stub line"))
    if "tie" in role:
        handles.append(Line2D([], [], color=_TIE_COLOR, linestyle=":", linewidth=1.0, label="tie (normally open)"))
    present = set(nodes["kind"].astype(str)) if not nodes.empty else set()
    names = {"busbar": "HV/MV busbar", "station": "MV/LV station", "customer": "MV customer",
             "generator": "MV generator", "joint": "joint"}
    for k in ("busbar", "station", "customer", "generator", "joint"):
        if k in present:
            style = NODE_STYLES[k]
            handles.append(Line2D([], [], marker=style["marker"], linestyle="none",
                                  markerfacecolor=style["color"] if kind_colors or k == "busbar" else "#bbbbbb",
                                  markeredgecolor="white", markersize=math.sqrt(style["s"]) + 1, label=names[k]))
    if n_nops:
        handles.append(Line2D([], [], marker="o", linestyle="none", markerfacecolor="none",
                              markeredgecolor=_NOP_COLOR, markersize=10, label="normally-open point"))
    return handles


# ---------------------------------------------------------------------------------------------------------------
# Ring analysis
# ---------------------------------------------------------------------------------------------------------------


def _graph(rows: pd.DataFrame) -> nx.Graph:
    """Undirected graph of ``rows`` (``from_node``, ``to_node``, ``_len``) keeping the shortest of parallel lines."""
    graph = nx.Graph()
    for a, b, w in zip(rows["from_node"], rows["to_node"], rows["_len"]):
        a, b, w = int(a), int(b), float(w)
        if graph.has_edge(a, b) and graph[a][b]["weight"] <= w:
            continue
        graph.add_edge(a, b, weight=w)
    return graph


def _distances(rows: pd.DataFrame, source: int) -> dict[int, float]:
    graph = _graph(rows)
    if source not in graph:
        return {}
    return nx.single_source_dijkstra_path_length(graph, source, weight="weight")


def ring_paths(nodes: Any, lines: Any, mv_grid_id: int, ring_id: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Halves, main path, member order and feeding distances of one ring (or feeder) of an MV grid.

    The ring consists of the lines of ``mv_grid_id`` with ``ring_id`` (ties excluded); its busbar is the node of
    kind ``busbar`` at one of their ends. The busbar exits are the ring lines at the busbar: exit A is the one whose
    first node has the smallest ``ring_position`` (else the smaller line id). In normal operation (normally-open
    lines open) every node is fed through one exit: its half (``a`` or ``b``) and its feeding distance
    ``dist_km``. The main path runs from exit A to exit B through the open point; nodes off it are stub nodes. For
    the (n-1) cases each exit is taken out in turn with the open point closed; ``dist_n1_km`` is the longer of the
    two feeding distances, i.e. the feeding path of the worse (n-1) case of the node (the ring fed from the far
    side). Feeders with one exit (radial, generator) have no (n-1) distance.

    Args:
        nodes: MV nodes (``mv_node_id``, ``kind``, point geometry or ``x``/``y``; optional ``ring_position``).
        lines: MV lines (``mv_line_id``, ``mv_grid_id``, ``from_node``, ``to_node``, ``ring_id``, ``role``,
            ``length_km``, ``normally_open``).
        mv_grid_id: The MV grid.
        ring_id: The ring (``mv_ring.ring_id``).

    Returns:
        ``(node_table, line_table)``. ``node_table`` is indexed by ``mv_node_id`` (busbar included) with the
        columns ``kind``, ``x``, ``y``, ``half`` (``a``, ``b`` or None for the busbar and unsupplied nodes),
        ``main`` (on the main path), ``order`` (1-based position of the stations, customers and generators on
        the main path from exit A; ``ring_position`` when stored), ``dist_km`` and ``dist_n1_km``.
        ``line_table`` is indexed by ``mv_line_id`` with ``from_node``, ``to_node``, ``role``, ``kind``,
        ``half`` (``a``, ``b`` or ``nop`` for a normally-open line), ``main`` and ``exit`` (``a``, ``b`` or None).

    Raises:
        ValueError: If the grid has no line of the ring or the ring has no busbar.
    """
    node_tab = _node_table(nodes)
    line_tab = _line_table(lines)
    sel = line_tab
    if "mv_grid_id" in sel.columns:
        sel = sel[sel["mv_grid_id"] == mv_grid_id]
    if "ring_id" not in sel.columns:
        raise ValueError("MV lines need the column 'ring_id'")
    role = _column(sel, "role", "ring").fillna("ring").astype(str)
    ring = sel[(sel["ring_id"] == ring_id) & (role != "tie")].copy()
    if ring.empty:
        raise ValueError(f"MV grid {mv_grid_id} has no lines of ring {ring_id}")
    ring["_len"] = pd.to_numeric(_column(ring, "length_km", 0.0), errors="coerce").fillna(0.0)
    ring["_open"] = _as_bool(_column(ring, "normally_open", False))
    ends = pd.unique(pd.concat([ring["from_node"], ring["to_node"]]).astype("int64"))
    kinds = node_tab["kind"].astype(str) if not node_tab.empty else pd.Series(dtype=str)
    busbars = [int(n) for n in ends if n in kinds.index and kinds.get(n) == "busbar"]
    if not busbars:
        raise ValueError(f"ring {ring_id} of MV grid {mv_grid_id} has no busbar node")
    busbar = busbars[0]

    at_busbar = (ring["from_node"] == busbar) | (ring["to_node"] == busbar)
    exits = ring[at_busbar].copy()
    exits["_first"] = np.where(exits["from_node"] == busbar, exits["to_node"], exits["from_node"]).astype("int64")
    position = pd.to_numeric(_column(node_tab, "ring_position"), errors="coerce")
    exits["_pos"] = [position.get(n, np.nan) for n in exits["_first"]]
    exits["_id"] = exits.index.to_numpy()
    role_all = _column(ring, "role", "ring").fillna("ring").astype(str)
    kind_all = _column(ring, "kind", "cable").fillna("cable").astype(str)
    exits = exits.sort_values(["_pos", "_id"], na_position="last")
    exit_label = {line_id: label for line_id, label in zip(exits.index, ("a", "b"))}

    closed = ring[~ring["_open"]]
    d0: dict[int, float] = {}
    half: dict[int, Optional[str]] = {busbar: None}
    graph = _graph(closed)
    if busbar in graph:
        d0, paths = nx.single_source_dijkstra(graph, busbar, weight="weight")
        first_label = {}
        for line_id, row in exits.iterrows():
            if not row["_open"] and line_id in exit_label:
                first_label.setdefault(int(row["_first"]), exit_label[line_id])
        for node, path in paths.items():
            if node != busbar:
                half[node] = first_label.get(path[1])

    n1: dict[int, float] = {}
    through_a: dict[int, float] = {}
    if len(exits) >= 2:
        for k, line_id in enumerate(exits.index[:2]):
            dist = _distances(ring.drop(index=line_id), busbar)
            if k == 1:
                through_a = dist          # exit B out: the ring is fed through exit A
            for node, value in dist.items():
                n1[node] = max(n1.get(node, value), value)

    full = _graph(ring)
    main: set[int] = {busbar}
    if len(exits) >= 2:
        a1, b1 = int(exits["_first"].iloc[0]), int(exits["_first"].iloc[1])
        inner = full.copy()
        inner.remove_node(busbar)
        if a1 == b1:
            main.add(a1)
        elif a1 in inner and b1 in inner and nx.has_path(inner, a1, b1):
            main.update(nx.shortest_path(inner, a1, b1, weight="weight"))
    elif d0:
        far = max((n for n in d0 if n != busbar), key=lambda n: (d0[n], -n), default=None)
        if far is not None:
            main.update(nx.shortest_path(graph, busbar, far, weight="weight"))

    rows = []
    for node in ends:
        node = int(node)
        row = node_tab.loc[node] if node in node_tab.index else None
        rows.append({
            "mv_node_id": node,
            "kind": str(row["kind"]) if row is not None else "station",
            "x": float(row["x"]) if row is not None else np.nan,
            "y": float(row["y"]) if row is not None else np.nan,
            "half": half.get(node),
            "main": node in main,
            "dist_km": d0.get(node, np.nan),
            "dist_n1_km": n1.get(node, np.nan) if node != busbar else 0.0,
        })
    table = pd.DataFrame(rows).set_index("mv_node_id").sort_index()
    if len(exits) < 2:
        table["dist_n1_km"] = np.nan
    members = table[table["main"] & table["kind"].isin(_MEMBER_KINDS)]
    stored = position.reindex(members.index)
    if len(members) and stored.notna().all():
        order = stored
    else:
        along = through_a if through_a else d0
        ranked = sorted(members.index, key=lambda n: (along.get(n, np.inf), n))
        order = pd.Series(np.arange(1, len(ranked) + 1), index=ranked, dtype=float)
    table["order"] = order.reindex(table.index)

    line_rows = []
    for line_id, row in ring.iterrows():
        a, b = int(row["from_node"]), int(row["to_node"])
        if row["_open"]:
            label: Optional[str] = "nop"
        else:
            far_end = max((a, b), key=lambda n: d0.get(n, -1.0))
            label = half.get(far_end)
        line_rows.append({"mv_line_id": line_id, "from_node": a, "to_node": b,
                          "role": role_all[line_id], "kind": kind_all[line_id],
                          "half": label, "main": a in main and b in main, "exit": exit_label.get(line_id)})
    line_table = pd.DataFrame(line_rows).set_index("mv_line_id")
    return table, line_table


# ---------------------------------------------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------------------------------------------


def plot_mv_overview(layers: Mapping[str, Any], ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (11, 11),
                     basemap: bool = True, title: Optional[str] = None) -> Figure:
    """Map of all MV grids of a variant: supply districts, substations, lines and normally-open points.

    District cells (``grids``) are filled in one colour per grid (hatched when ``complete`` is False: the
    substation lies outside the planning region or the cell is cut by its border); the lines of a grid are drawn
    thin in the darker grid colour, overhead lines dashed, ties grey and dotted; substations are triangles and
    normally-open points open red circles.

    Args:
        layers: Layers of ``read_mv_layers`` (see the module docstring).
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        basemap: Add an OpenStreetMap basemap (skipped with a warning if the tiles cannot be loaded).
        title: Plot title; default: number of grids and circuit length.

    Returns:
        The figure that contains the map.
    """
    fig, ax = _figure(ax, figsize)
    nodes = _node_table(_layer(layers, "nodes"))
    lines = _line_table(_layer(layers, "lines"))
    grids = _layer(layers, "grids")

    grid_ids: list[Any] = []
    if grids is not None and "mv_grid_id" in grids.columns:
        grid_ids = list(pd.unique(grids["mv_grid_id"]))
    elif "mv_grid_id" in nodes.columns:
        grid_ids = list(pd.unique(nodes["mv_grid_id"].dropna()))
    palette = _palette()
    grid_color = {gid: palette[i % len(palette)]
                  for i, gid in enumerate(sorted(grid_ids, key=lambda v: (str(type(v)), v)))}

    if grids is not None and _geometry(grids) is not None:
        colors = [grid_color.get(g, "#cccccc") for g in grids["mv_grid_id"]] if "mv_grid_id" in grids.columns \
            else ["#cccccc"] * len(grids)
        complete = _as_bool(_column(grids, "complete", True), default=True)
        valid = ~grids.geometry.isna().to_numpy()
        for flag, hatch in ((True, None), (False, "//")):
            mask = valid & (complete.to_numpy() == flag)
            if mask.any():
                grids[mask].plot(ax=ax, color=[c for c, m in zip(colors, mask) if m], alpha=0.22,
                                 edgecolor="#555555", linewidth=0.8, hatch=hatch, zorder=1)

    if not lines.empty:
        widths, styles = _line_styles(lines)
        widths = [0.5 * w for w in widths]
        role = _column(lines, "role", "ring").fillna("ring").astype(str)
        owner = _column(lines, "mv_grid_id")
        colors = [_TIE_COLOR if r == "tie" else grid_color.get(g, "#333333") for r, g in zip(role, owner)]
        _draw_lines(ax, lines, nodes, colors, widths, styles, zorder=3)
    n_nops = _draw_nops(ax, lines, nodes, size=40)

    substations = _layer(layers, "substations")
    if substations is not None and _geometry(substations) is not None:
        ax.scatter(substations.geometry.x, substations.geometry.y, marker="^", s=110, c=NODE_STYLES["busbar"]["color"],
                   edgecolors="white", linewidths=0.8, zorder=9)
    elif not nodes.empty:
        _draw_nodes(ax, nodes, kinds=("busbar",))

    handles = [Line2D([], [], marker="^", linestyle="none", markerfacecolor=NODE_STYLES["busbar"]["color"],
                      markeredgecolor="white", markersize=11, label="HV/MV substation"),
               Line2D([], [], color="#333333", linewidth=0.8, label="MV line (cable)")]
    kinds = set(_column(lines, "kind", "cable").dropna().astype(str))
    if "overhead" in kinds:
        handles.append(Line2D([], [], color="#333333", linewidth=0.8, linestyle="--", label="overhead line"))
    if "tie" in set(_column(lines, "role", "ring").dropna().astype(str)):
        handles.append(Line2D([], [], color=_TIE_COLOR, linewidth=0.8, linestyle=":", label="tie"))
    if n_nops:
        handles.append(Line2D([], [], marker="o", linestyle="none", markerfacecolor="none",
                              markeredgecolor=_NOP_COLOR, markersize=7, label="normally-open point"))
    if grids is not None and "complete" in grids.columns and (~_as_bool(grids["complete"], default=True)).any():
        handles.append(Patch(facecolor="#dddddd", edgecolor="#555555", hatch="//", label="incomplete district"))
    ax.legend(handles=handles, loc="upper right", fontsize=9, framealpha=0.9)

    if title is None:
        km = pd.to_numeric(_column(lines, "length_km", 0.0), errors="coerce").fillna(0.0)
        not_tie = _column(lines, "role", "ring").fillna("ring").astype(str) != "tie"
        title = f"MV grids: {len(grid_ids)} supply districts, {km[not_tie].sum():,.0f} km"
    ax.set_title(title, fontsize=14, fontweight="bold")
    _finish_map(ax)
    if basemap:
        _add_basemap(ax, _crs(layers))
    return fig


def plot_mv_grid_map(layers: Mapping[str, Any], mv_grid_id: int, color_by: str = "ring",
                     ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (10, 10), basemap: bool = False,
                     settlements: bool = True, title: Optional[str] = None, cmap: Optional[str] = None,
                     vmin: Optional[float] = None, vmax: Optional[float] = None) -> Figure:
    """Map of one MV grid (supply district).

    ``color_by`` selects the colours: ``ring`` (one colour per ring or feeder, ``ring_id``), ``kind`` (line kind
    and node kind), ``loading`` (``loading_load_percent`` of the lines), ``voltage`` (``vm_load_pu`` of the
    nodes) or ``tap`` (``mv_lv_tap_pos`` of the stations, band conflicts crossed out in red). Overhead lines are
    dashed, stub lines thinner, ties grey and dotted, settlements light polygons, the district cell a dashed
    outline.

    Args:
        layers: Layers of ``read_mv_layers`` (see the module docstring).
        mv_grid_id: The MV grid to draw.
        color_by: ``ring``, ``kind``, ``loading``, ``voltage`` or ``tap``.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        basemap: Add an OpenStreetMap basemap (skipped with a warning if the tiles cannot be loaded).
        settlements: Draw the settlement polygons of the ``settlements`` layer.
        title: Plot title; default: grid id, rings and circuit length.
        cmap: Colormap name of the ``loading``, ``voltage`` and ``tap`` modes.
        vmin: Lower end of the colour scale of the ``loading`` and ``voltage`` modes.
        vmax: Upper end of the colour scale of the ``loading`` and ``voltage`` modes.

    Returns:
        The figure that contains the map.

    Raises:
        ValueError: For an unknown ``color_by`` or a grid without nodes and lines in ``layers``.
    """
    if color_by not in _COLOR_MODES:
        raise ValueError(f"color_by must be one of {', '.join(_COLOR_MODES)}, got {color_by!r}")
    all_nodes = _node_table(_layer(layers, "nodes"))
    nodes = _grid_of_nodes(all_nodes, mv_grid_id)
    lines = _grid_lines(_line_table(_layer(layers, "lines")), nodes.index, mv_grid_id)
    if nodes.empty and lines.empty:
        raise ValueError(f"MV grid {mv_grid_id} has no nodes or lines in the layers")
    fig, ax = _figure(ax, figsize)
    bounds = _bounds(nodes, lines)
    if settlements:
        _draw_settlements(ax, layers, bounds)
    grid = _grid_row(layers, mv_grid_id)
    grids = _layer(layers, "grids")
    if grid is not None and grids is not None and _geometry(grids) is not None:
        geom = grid[grids.geometry.name]
        if geom is not None and not geom.is_empty:
            gpd.GeoSeries([geom]).boundary.plot(ax=ax, color="#444444", linewidth=0.9, linestyle="--", zorder=1)

    role = _column(lines, "role", "ring").fillna("ring").astype(str)
    widths, styles = _line_styles(lines)
    colorbar: Optional[tuple[ScalarMappable, str, Optional[list[int]]]] = None
    node_colors: Optional[list[Any]] = None
    if color_by == "ring":
        line_colors = _ring_colors(_column(lines, "ring_id"))
        node_colors = _ring_colors(_column(nodes, "ring_id"))
    elif color_by == "kind":
        kind = _column(lines, "kind", "cable").fillna("cable").astype(str)
        line_colors = [LINE_KIND_COLORS.get(k, "#333333") for k in kind]
    elif color_by == "loading":
        values = pd.to_numeric(_column(lines, "loading_load_percent"), errors="coerce")
        top = float(np.nanmax(values)) if values.notna().any() else 100.0
        norm = Normalize(vmin=0.0 if vmin is None else vmin, vmax=max(100.0, top) if vmax is None else vmax)
        if cmap is None:      # YlOrRd without its palest third, so that lightly loaded lines stay visible
            colormap: Any = ListedColormap(plt.get_cmap("YlOrRd")(np.linspace(0.3, 1.0, 256)))
        else:
            colormap = plt.get_cmap(cmap)
        mapper = ScalarMappable(norm=norm, cmap=colormap)
        line_colors = [mapper.to_rgba(v) if pd.notna(v) else _MUTED_LINE_COLOR for v in values]
        colorbar = (mapper, "Line loading, load case (%)", None)
        node_colors = ["#bbbbbb"] * len(nodes)
    else:
        line_colors = [_MUTED_LINE_COLOR] * len(lines)
    line_colors = [_TIE_COLOR if r == "tie" else c for r, c in zip(role, line_colors)]
    _draw_lines(ax, lines, all_nodes, line_colors, widths, styles, zorder=2)

    if color_by == "voltage":
        values = pd.to_numeric(_column(nodes, "vm_load_pu"), errors="coerce")
        low = float(np.nanmin(values)) if values.notna().any() else 0.97
        high = float(np.nanmax(values)) if values.notna().any() else 1.02
        norm = Normalize(vmin=min(low, 0.97) if vmin is None else vmin, vmax=max(high, 1.0) if vmax is None else vmax)
        mapper = ScalarMappable(norm=norm, cmap=plt.get_cmap(cmap or "RdYlGn"))
        node_colors = [mapper.to_rgba(v) if pd.notna(v) else "#bbbbbb" for v in values]
        colorbar = (mapper, "MV voltage, load case (p.u.)", None)
    elif color_by == "tap":
        taps = pd.to_numeric(_column(nodes, "mv_lv_tap_pos"), errors="coerce")
        low = int(min(-2, np.nanmin(taps))) if taps.notna().any() else -2
        high = int(max(2, np.nanmax(taps))) if taps.notna().any() else 2
        base = plt.get_cmap(cmap or "coolwarm")
        listed = ListedColormap([base(i / max(high - low, 1)) for i in range(high - low + 1)])
        norm = BoundaryNorm(np.arange(low - 0.5, high + 1.0, 1.0), listed.N)
        mapper = ScalarMappable(norm=norm, cmap=listed)
        node_colors = [mapper.to_rgba(v) if pd.notna(v) else "#bbbbbb" for v in taps]
        colorbar = (mapper, "MV/LV tap position", list(range(low, high + 1)))
    valued = color_by in ("voltage", "tap")
    _draw_nodes(ax, nodes, node_colors, size_scale=2.2 if valued else 1.0,
                edgecolor="#333333" if valued else "white")
    if color_by == "tap" and "band_conflict" in nodes.columns:
        conflict = nodes[_as_bool(nodes["band_conflict"])]
        if not conflict.empty:
            ax.scatter(conflict["x"], conflict["y"], marker="x", s=60, c=_NOP_COLOR, linewidths=1.5, zorder=9,
                       label="band conflict")
    n_nops = _draw_nops(ax, lines, all_nodes)

    handles = _legend_handles(lines, nodes, n_nops, kind_colors=color_by == "kind")
    if color_by == "tap" and "band_conflict" in nodes.columns and _as_bool(nodes["band_conflict"]).any():
        handles.append(Line2D([], [], marker="x", linestyle="none", color=_NOP_COLOR, label="band conflict"))
    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9)
    if colorbar is not None:
        fig.colorbar(colorbar[0], ax=ax, shrink=0.6, label=colorbar[1], ticks=colorbar[2])

    if title is None:
        not_tie = role != "tie"
        km = pd.to_numeric(_column(lines, "length_km", 0.0), errors="coerce").fillna(0.0)[not_tie].sum()
        rings = pd.unique(_column(lines[not_tie], "ring_id").dropna())
        title = f"MV grid {mv_grid_id}: {len(rings)} rings and feeders, {km:,.1f} km"
    ax.set_title(title, fontsize=13, fontweight="bold")
    x0, y0, x1, y1 = bounds
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    _finish_map(ax)
    if basemap:
        _add_basemap(ax, _crs(layers))
    return fig


def plot_mv_ring(layers: Mapping[str, Any], mv_grid_id: int, ring_id: int, ax: Optional[plt.Axes] = None,
                 figsize: Tuple[float, float] = (9, 9), basemap: bool = False, numbers: bool = True,
                 title: Optional[str] = None) -> Figure:
    """Map of one ring: its two halves in two colours, the open point, joints, stubs and numbered members.

    The halves and the member numbers come from :func:`ring_paths` (numbers along the ring from exit A). The other
    lines of the grid are drawn light grey for orientation.

    Args:
        layers: Layers of ``read_mv_layers`` (see the module docstring).
        mv_grid_id: The MV grid.
        ring_id: The ring (``ring_id`` of ``mv_line`` and ``mv_ring``).
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        basemap: Add an OpenStreetMap basemap (skipped with a warning if the tiles cannot be loaded).
        numbers: Write the position of each ring member next to it.
        title: Plot title; default: ring, members and length.

    Returns:
        The figure that contains the map.

    Raises:
        ValueError: If the grid has no line of the ring.
    """
    all_nodes = _node_table(_layer(layers, "nodes"))
    all_lines = _line_table(_layer(layers, "lines"))
    node_t, line_t = ring_paths(all_nodes, all_lines, mv_grid_id, ring_id)
    fig, ax = _figure(ax, figsize)
    grid_nodes = _grid_of_nodes(all_nodes, mv_grid_id)
    context = _grid_lines(all_lines, grid_nodes.index, mv_grid_id).drop(index=line_t.index, errors="ignore")
    if not context.empty:
        _draw_lines(ax, context, all_nodes, ["#d0d0d0"] * len(context), [0.8] * len(context),
                    ["-"] * len(context), zorder=1)

    ring_lines = all_lines.loc[line_t.index]
    widths, styles = _line_styles(ring_lines)
    colors = [_HALF_COLORS.get(h, "#555555") if h != "nop" else "#555555" for h in line_t["half"]]
    styles = [":" if h == "nop" else s for h, s in zip(line_t["half"], styles)]
    _draw_lines(ax, ring_lines, all_nodes, colors, [w * 1.3 for w in widths], styles, zorder=3)

    members = node_t.copy()
    node_colors = [_HALF_COLORS.get(h, NODE_STYLES.get(k, NODE_STYLES["station"])["color"])
                   for h, k in zip(members["half"], members["kind"])]
    _draw_nodes(ax, members, node_colors, size_scale=2.0, edgecolor="#333333")
    stub = members[~members["main"] & members["kind"].isin(_MEMBER_KINDS)]
    if not stub.empty:
        ax.scatter(stub["x"], stub["y"], s=90, facecolors="none", edgecolors="#555555", linewidths=0.8,
                   linestyle="--", zorder=4)
    n_nops = _draw_nops(ax, ring_lines, all_nodes, size=220)
    if numbers:
        for node, row in members[members["order"].notna()].iterrows():
            ax.annotate(f"{int(row['order'])}", (row["x"], row["y"]), xytext=(5, 5), textcoords="offset points",
                        fontsize=8, zorder=10)

    handles = [Line2D([], [], color=_HALF_COLORS["a"], linewidth=2, label="half A (exit A)"),
               Line2D([], [], color=_HALF_COLORS["b"], linewidth=2, label="half B (exit B)")]
    if (line_t["half"] == "nop").any():
        handles.append(Line2D([], [], color="#555555", linestyle=":", linewidth=2, label="normally-open section"))
    if (line_t["role"] == "stub").any():
        handles.append(Line2D([], [], color="#555555", linewidth=_STUB_WIDTH * 1.3, label="stub line"))
    if not stub.empty:
        handles.append(Line2D([], [], marker="o", linestyle="none", markerfacecolor="none",
                              markeredgecolor="#555555", markersize=9, label="stub node"))
    if (members["kind"] == "joint").any():
        handles.append(Line2D([], [], marker="s", linestyle="none", color="#333333", markersize=4, label="joint"))
    if n_nops:
        handles.append(Line2D([], [], marker="o", linestyle="none", markerfacecolor="none",
                              markeredgecolor=_NOP_COLOR, markersize=12, label="normally-open point"))
    ax.legend(handles=handles, loc="best", fontsize=8, framealpha=0.9)
    if title is None:
        n_members = int((members["main"] & members["kind"].isin(_MEMBER_KINDS)).sum())
        km = pd.to_numeric(_column(ring_lines, "length_km", 0.0), errors="coerce").fillna(0.0)
        main_km = float(km[line_t["main"].to_numpy()].sum())
        stub_km = float(km.sum()) - main_km
        title = f"Ring {ring_id} of MV grid {mv_grid_id}: {n_members} members, {main_km:,.1f} km"
        if stub_km > 0:
            title += f" (+{stub_km:,.1f} km stubs)"
    ax.set_title(title, fontsize=13, fontweight="bold")
    x0, y0, x1, y1 = _bounds(members, ring_lines, margin=0.08)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    _finish_map(ax)
    if basemap:
        _add_basemap(ax, _crs(layers))
    return fig


def plot_lv_busbar_voltage_map(layers: Mapping[str, Any], column: str = "lv_busbar_vm_pu",
                               ax: Optional[plt.Axes] = None, figsize: Tuple[float, float] = (10, 10),
                               mv_grid_id: Optional[int] = None, basemap: bool = False, cmap: Optional[str] = None,
                               vmin: Optional[float] = None, vmax: Optional[float] = None,
                               title: Optional[str] = None) -> Figure:
    """Map of an LV-side voltage per station (voltage budget or ``couple`` result), band conflicts marked.

    Stations are coloured by ``column`` (red is critical: low voltages for the load columns, high voltages for
    the feed-in columns); stations with ``band_conflict`` get a red cross; MV lines are drawn grey.

    Args:
        layers: Layers of ``read_mv_layers`` (see the module docstring).
        column: Node column, e.g. ``lv_busbar_vm_pu``, ``lv_min_vm_pu``, ``lv_busbar_vm_feed_in_pu`` or a
            ``coupled_*`` column.
        ax: Axes to draw into; a new figure is created if None.
        figsize: Figure size in inches if a new figure is created.
        mv_grid_id: Draw only this MV grid; None draws every grid of the layers.
        basemap: Add an OpenStreetMap basemap (skipped with a warning if the tiles cannot be loaded).
        cmap: Colormap name; default ``RdYlGn`` (``RdYlGn_r`` for feed-in columns).
        vmin: Lower end of the colour scale (default: 2nd percentile).
        vmax: Upper end of the colour scale (default: 98th percentile).
        title: Plot title; default: column, station count and band conflicts.

    Returns:
        The figure that contains the map.

    Raises:
        ValueError: If ``column`` is not a node column.
    """
    all_nodes = _node_table(_layer(layers, "nodes"))
    if column not in all_nodes.columns:
        raise ValueError(f"the MV nodes have no column {column!r}")
    nodes = all_nodes
    lines = _line_table(_layer(layers, "lines"))
    if mv_grid_id is not None:
        nodes = _grid_of_nodes(nodes, mv_grid_id)
        lines = _grid_lines(lines, nodes.index, mv_grid_id)
    fig, ax = _figure(ax, figsize)
    if not lines.empty:
        widths, styles = _line_styles(lines)
        _draw_lines(ax, lines, all_nodes, [_MUTED_LINE_COLOR] * len(lines),
                    [0.6 * w for w in widths], styles, zorder=1, alpha=0.8)
    _draw_nodes(ax, nodes, kinds=("busbar",))
    values = pd.to_numeric(nodes[column], errors="coerce")
    stations = nodes[values.notna()]
    values = values[values.notna()]
    feed_in = "feed_in" in column or column == "coupled_lv_max_vm_pu"
    if not values.empty:
        lo = float(np.nanpercentile(values, 2)) if vmin is None else vmin
        hi = float(np.nanpercentile(values, 98)) if vmax is None else vmax
        if hi <= lo:
            lo, hi = lo - 0.005, hi + 0.005
        sc = ax.scatter(stations["x"], stations["y"], c=values, cmap=cmap or ("RdYlGn_r" if feed_in else "RdYlGn"),
                        vmin=lo, vmax=hi, s=28, edgecolors="#333333", linewidths=0.3, zorder=5)
        fig.colorbar(sc, ax=ax, shrink=0.6, label=_LV_COLUMN_LABELS.get(column, f"{column} (p.u.)"))
    n_conflicts = 0
    if "band_conflict" in nodes.columns:
        conflict = nodes[_as_bool(nodes["band_conflict"])]
        n_conflicts = len(conflict)
        if n_conflicts:
            ax.scatter(conflict["x"], conflict["y"], marker="x", s=70, c=_NOP_COLOR, linewidths=1.6, zorder=8)
    handles = [Line2D([], [], marker="^", linestyle="none", markerfacecolor=NODE_STYLES["busbar"]["color"],
                      markeredgecolor="white", markersize=11, label="HV/MV busbar"),
               Line2D([], [], color=_MUTED_LINE_COLOR, label="MV line")]
    if n_conflicts:
        handles.append(Line2D([], [], marker="x", linestyle="none", color=_NOP_COLOR, label="band conflict"))
    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9)
    if title is None:
        scope = f"MV grid {mv_grid_id}" if mv_grid_id is not None else "all MV grids"
        title = f"{column}: {len(values)} stations, {n_conflicts} band conflicts ({scope})"
    ax.set_title(title, fontsize=12, fontweight="bold")
    x0, y0, x1, y1 = _bounds(nodes, lines)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    _finish_map(ax)
    if basemap:
        _add_basemap(ax, _crs(layers))
    return fig


def _busbar_point(layers: Mapping[str, Any], mv_grid_id: int) -> Optional[tuple[float, float]]:
    """Position of the busbar of a grid: its busbar node, else its substation."""
    nodes = _node_table(_layer(layers, "nodes"))
    if "mv_grid_id" in nodes.columns:
        busbar = nodes[(nodes["mv_grid_id"] == mv_grid_id) & (nodes["kind"].astype(str) == "busbar")]
        if not busbar.empty:
            return float(busbar["x"].iloc[0]), float(busbar["y"].iloc[0])
    grid = _grid_row(layers, mv_grid_id)
    substations = _layer(layers, "substations")
    if grid is not None and substations is not None and _geometry(substations) is not None \
            and "substation_id" in substations.columns and "substation_id" in grid.index:
        row = substations[substations["substation_id"] == grid["substation_id"]]
        if not row.empty:
            point = row.geometry.iloc[0]
            return float(point.x), float(point.y)
    return None


def _grid_ids(layers: Mapping[str, Any]) -> list[int]:
    grids = _layer(layers, "grids")
    if grids is not None and "mv_grid_id" in grids.columns:
        return [int(g) for g in pd.unique(grids["mv_grid_id"].dropna())]
    nodes = _layer(layers, "nodes")
    if nodes is not None and "mv_grid_id" in nodes.columns:
        return [int(g) for g in pd.unique(nodes["mv_grid_id"].dropna())]
    return []


def _match_grids(layers_by_variant: Mapping[str, Mapping[str, Any]], mv_grid_id: Any
                 ) -> dict[str, tuple[int, Optional[float]]]:
    """Grid id per variant, with the distance (m) of its busbar to the reference busbar (None: given)."""
    if isinstance(mv_grid_id, Mapping):
        return {variant: (int(mv_grid_id[variant]), None) for variant in layers_by_variant}
    reference = None
    for layers in layers_by_variant.values():
        if int(mv_grid_id) in _grid_ids(layers):
            reference = _busbar_point(layers, int(mv_grid_id))
            break
    if reference is None:
        raise ValueError(f"MV grid {mv_grid_id} is in none of the variants")
    out: dict[str, tuple[int, Optional[float]]] = {}
    for variant, layers in layers_by_variant.items():
        ids = _grid_ids(layers)
        if int(mv_grid_id) in ids:
            out[variant] = (int(mv_grid_id), 0.0)
            continue
        best: Optional[tuple[float, int]] = None
        for gid in ids:
            point = _busbar_point(layers, gid)
            if point is not None:
                dist = math.hypot(point[0] - reference[0], point[1] - reference[1])
                if best is None or (dist, gid) < best:
                    best = (dist, gid)
        if best is None:
            raise ValueError(f"variant {variant!r} has no MV grid with a busbar position")
        out[variant] = (best[1], best[0])
    return out


def plot_variants_side_by_side(layers_by_variant: Mapping[str, Mapping[str, Any]], mv_grid_id: Any,
                               figsize: Tuple[float, float] = (16, 8), color_by: str = "ring",
                               basemap: bool = False, settlements: bool = True) -> Figure:
    """The same supply district in several MV variants, side by side on one map extent.

    ``mv_grid_id`` values are identities of one variant only. An integer names the grid in the variant whose
    layers contain it; in every other variant the grid whose busbar lies nearest to that busbar is drawn (the
    distance is shown in the panel title when it is not zero). A mapping gives the grid id per variant.

    Args:
        layers_by_variant: Layers of ``read_mv_layers`` per variant label (e.g. ``{"d0": ..., "d1": ...}``).
        mv_grid_id: Grid id in one of the variants, or ``{variant: mv_grid_id}``.
        figsize: Figure size in inches.
        color_by: Colour mode of :func:`plot_mv_grid_map`.
        basemap: Add an OpenStreetMap basemap to every panel.
        settlements: Draw the settlement polygons.

    Returns:
        The figure with one panel per variant.

    Raises:
        ValueError: If no variant is given or the grid cannot be found.
    """
    if not layers_by_variant:
        raise ValueError("layers_by_variant is empty")
    matched = _match_grids(layers_by_variant, mv_grid_id)
    fig, axes = plt.subplots(1, len(layers_by_variant), figsize=figsize, squeeze=False)
    extents = []
    for ax, (variant, layers) in zip(axes[0], layers_by_variant.items()):
        gid, dist = matched[variant]
        note = f" (busbar {dist / 1000:.1f} km away)" if dist else ""
        nodes = _grid_of_nodes(_node_table(_layer(layers, "nodes")), gid)
        lines = _grid_lines(_line_table(_layer(layers, "lines")), nodes.index, gid)
        not_tie = _column(lines, "role", "ring").fillna("ring").astype(str) != "tie"
        km = pd.to_numeric(_column(lines, "length_km", 0.0), errors="coerce").fillna(0.0)[not_tie].sum()
        plot_mv_grid_map(layers, gid, color_by=color_by, ax=ax, basemap=False, settlements=settlements,
                         title=f"{variant}: grid {gid}, {km:,.1f} km{note}")
        extents.append(_bounds(nodes, lines))
    x0 = min(e[0] for e in extents)
    y0 = min(e[1] for e in extents)
    x1 = max(e[2] for e in extents)
    y1 = max(e[3] for e in extents)
    for ax, layers in zip(axes[0], layers_by_variant.values()):
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        if basemap:
            _add_basemap(ax, _crs(layers))
    fig.tight_layout()
    return fig


def interactive_mv_net(net: Any, on_map: bool = True, **kwargs: Any) -> Any:
    """Interactive plotly map of a stored MV net (``MvResultMixin.read_mv_grid_net``).

    Uses ``pandapower.plotting.plotly.simple_plotly`` on an OpenStreetMap background inside
    :func:`~pylovo.plotting.utils.pandapower_on_map` (the bus geodata of stored MV nets are WGS84). No HTML file is
    written and no browser is opened unless ``filename`` and ``auto_open`` are passed.

    Args:
        net: pandapower net with WGS84 ``geo`` columns.
        on_map: Draw on a map background; False draws the plain coordinates.
        **kwargs: Passed to ``simple_plotly`` (e.g. ``map_style``, ``bus_size``, ``line_width``).

    Returns:
        plotly.graph_objects.Figure: The figure.
    """
    from pandapower.plotting.plotly import simple_plotly

    kwargs.setdefault("filename", None)
    kwargs.setdefault("auto_open", False)
    if on_map:
        kwargs.setdefault("map_style", "open-street-map")
        with pandapower_on_map():
            fig = simple_plotly(net, on_map=True, **kwargs)
    else:
        fig = simple_plotly(net, **kwargs)
    fig.update_layout(template="plotly_white")
    return fig


def plot_mv_grid(version_id: str, variant_id: str, mv_grid_id: int, **kwargs: Any) -> Figure:
    """Read one MV grid from the database and draw it with :func:`plot_mv_grid_map`.

    The layers are read with ``read_mv_layers`` of a plain :class:`~pylovo.database.database_client.DatabaseClient`
    (read-only, nothing is committed).

    Args:
        version_id: LV version of the MV variant.
        variant_id: MV variant.
        mv_grid_id: The MV grid.
        **kwargs: Passed to :func:`plot_mv_grid_map` (e.g. ``color_by``, ``ax``, ``basemap``).

    Returns:
        The figure that contains the map.
    """
    with DatabaseClient() as dbc:
        layers = dbc.read_mv_layers(version_id, variant_id, mv_grid_id)
    return plot_mv_grid_map(layers, mv_grid_id, **kwargs)
