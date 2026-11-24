"""The input of one district for the pure planner, read in the district worker.

:func:`district_input` reads the nodes, the LV requirements and the routing subgraph of one supply district and
returns the :class:`~pylovo.mv_grid.model.DistrictInput` of
:func:`~pylovo.mv_grid.planner.plan_district`; :func:`attachment_points` places each node on its street (also used
by the ties and links of :mod:`~pylovo.mv_grid.interconnection`).
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid.model import DistrictInput
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError


def attachment_points(nodes: pd.DataFrame, geometries: dict[int, Any]) -> tuple[np.ndarray, np.ndarray]:
    """The street point of every node: its attach edge's geometry at ``attach_fraction`` (NaN without one).

    ``graph.build_run_graph`` places the split vertex of an attach edge there; without these coordinates it falls
    back to the straight chord between the edge ends, which lies off a curved street, and the line geometries then
    zigzag between the chord point and the street.
    """
    edge = pd.to_numeric(nodes["attach_edge_id"], errors="coerce").to_numpy(dtype=float)
    fraction = pd.to_numeric(nodes["attach_fraction"], errors="coerce").to_numpy(dtype=float)
    ax = np.full(len(nodes), np.nan)
    ay = np.full(len(nodes), np.nan)
    for i, (e, f) in enumerate(zip(edge, fraction)):
        geom = geometries.get(int(e)) if np.isfinite(e) and np.isfinite(f) else None
        if geom is not None and not geom.is_empty:
            point = geom.interpolate(min(max(float(f), 0.0), 1.0), normalized=True)
            ax[i], ay[i] = point.x, point.y
    return ax, ay


def district_input(dbc: Any, version_id: str, variant_id: str, mv_grid_id: int, graph_id: str,
                   params: MvParameters, lv: LvVersionParameters) -> tuple[DistrictInput, dict[int, float]]:
    """Read one district for the planner (worker side).

    The nodes come from :meth:`MvResultMixin.load_district_nodes`; the stored LV requirement (``ext_grid.vm_pu``
    of each station's LV net) is read for the district's stations only; the subgraph is the part of the routing
    graph within ``max(subgraph_buffer_m, largest attach offset + 100 m)`` of the convex hull of the node points,
    plus every attach edge. Edge geometries are read lazily (once per edge) through ``edge_geometry``; the attach
    edges are read at once for the street points ``ax``, ``ay`` of the nodes (:func:`attachment_points`).

    Returns:
        ``(district input, {grid_result_id: requirement})``.
    """
    import shapely

    nodes, grid = dbc.load_district_nodes(mv_grid_id)
    if grid["version_id"] != version_id or grid["mv_variant_id"] != variant_id:
        raise ValueError(f"MV grid {mv_grid_id} belongs to variant {grid['mv_variant_id']} of version "
                         f"{grid['version_id']}, not to {variant_id} of {version_id}")
    if not len(nodes) or nodes.iloc[0]["kind"] != "busbar":
        raise MvStageError(f"MV grid {mv_grid_id} has no busbar node")
    stations = nodes["kind"].eq("station") & nodes["grid_result_id"].notna()
    station_ids = sorted({int(g) for g in nodes.loc[stations, "grid_result_id"]})
    requirements = {int(k): float(v) for k, v in (dbc.lv_design_source_vm(version_id, station_ids)
                                                  if station_ids else {}).items() if v is not None}
    nodes = nodes.copy()
    nodes["lv_design_source_vm_pu"] = [requirements.get(int(g), np.nan) if pd.notna(g) else np.nan
                                       for g in nodes["grid_result_id"]]
    xy = nodes[["x", "y"]].to_numpy(dtype=float)
    hull = shapely.MultiPoint(xy).convex_hull
    offsets = pd.to_numeric(nodes["attach_offset_m"], errors="coerce")
    buffer_m = max(float(params.subgraph_buffer_m), (float(offsets.max()) if offsets.notna().any() else 0.0) + 100.0)
    must = sorted({int(e) for e in pd.to_numeric(nodes["attach_edge_id"], errors="coerce").dropna()})
    edges = dbc.subgraph_edges(graph_id, shapely.to_wkb(hull), buffer_m, must)
    cache: dict[int, Any] = {}

    def edge_geometry(edge_ids: Sequence[int]) -> dict[int, Any]:
        wanted = [int(e) for e in edge_ids]
        missing = sorted({e for e in wanted if e not in cache})
        if missing:
            cache.update({int(k): v for k, v in dbc.edge_geometries(graph_id, missing).items()})
        return {e: cache[e] for e in wanted if e in cache}

    nodes["ax"], nodes["ay"] = attachment_points(nodes, edge_geometry(must) if must else {})
    hv_bus = grid.get("egon_bus_id")
    inp = DistrictInput(mv_grid_id=int(mv_grid_id), substation_id=int(grid["substation_id"]),
                        vn_kv=float(grid["vn_kv"]), nodes=nodes, edges=edges, params=params, lv=lv,
                        edge_geometry=edge_geometry,
                        hv_bus_id=int(hv_bus) if hv_bus is not None and not pd.isna(hv_bus) else None)
    return inp, requirements
