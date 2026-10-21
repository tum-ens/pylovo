"""Postcode-border and data-edge diagnostics of a version (read-only).

Per PLZ: the share of its polygon inside the InfDB import scope (``scope_share``), the share of the
ring of neighboring streets around it inside the scope (``context_share``) and the street length inside
the polygon and in the ring. The two shares differ: a PLZ can lie fully inside the scope while its
surroundings were never imported.

Per grid: the number of buildings and the distance from its buildings to the nearest PLZ border of
each kind, and to the edge of the import scope:

* ``generated``: border with a PLZ that has results in the version,
* ``open``: border with a PLZ without results in the version,
* ``outer``: border without a neighbouring PLZ in ``pylovo.postcode``.

Two PLZ are neighbours when their polygons are at most ``BORDER_TOLERANCE_M`` (10 m) apart: the same
distance selects the neighbouring polygons and matches the shared border, so a gap of a few metres between
two PLZ polygons still counts as a shared border. A shared border can include pieces of up to this length
of the edges that meet it at its ends.

These are diagnostics. Report flagged and unflagged grids side by side; do not use them to hide grids.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import shapely
from shapely import wkb as shapely_wkb
from shapely.ops import unary_union

from pylovo import postcode_border
from pylovo.config_loader import TARGET_EPSG, VERSION_ID

#: Grids with at most this many buildings count as small (the validation cohort drops them).
SMALL_GRID_MAX_BUILDINGS = 4
#: A grid is "near" a border or edge within this distance.
NEAR_M = 250.0
#: Maximum distance of two neighbouring PLZ polygons: the search distance of the neighbours and the
#: tolerance of their shared border.
BORDER_TOLERANCE_M = 10.0
#: Shared border parts shorter than this are corner stubs of the tolerance zone.
_MIN_SEGMENT_M = 2 * BORDER_TOLERANCE_M + 1.0


def postcode_context(dbc, inf_dbc, plz_list: list[int], ring_m: float | None = None) -> pd.DataFrame:
    """Scope and street context of each PLZ (see the module docstring)."""
    ring_m = postcode_border.neighboring_street_width_m() if ring_m is None else float(ring_m)
    rows = []
    for plz in plz_list:
        polygon_wkb = dbc.get_postcode_polygon_wkb(plz)
        polygon = shapely_wkb.loads(polygon_wkb)
        ring = polygon.buffer(ring_m).difference(polygon)
        scope_wkb = inf_dbc.fetch_scope_wkb(polygon_wkb, TARGET_EPSG, ring_m)
        scope = shapely_wkb.loads(scope_wkb) if scope_wkb else None
        inside_km, ring_km = inf_dbc.fetch_street_length_km(plz, polygon_wkb, TARGET_EPSG, ring_m)
        rows.append({
            "plz": int(plz),
            "area_km2": polygon.area / 1e6,
            "scope_share": polygon.intersection(scope).area / polygon.area if scope is not None else np.nan,
            "context_share": ring.intersection(scope).area / ring.area if scope is not None else np.nan,
            "streets_inside_km": inside_km,
            "streets_ring_km": ring_km,
        })
    return pd.DataFrame(rows)


def _postcode_polygons(dbc, plz_list: list[int], distance_m: float = BORDER_TOLERANCE_M) -> dict[int, shapely.Geometry]:
    """Polygons of ``plz_list`` and of every PLZ within ``distance_m`` of them (from ``pylovo.postcode``)."""
    dbc.cur.execute(
        """WITH own AS (SELECT ST_Union(geom) AS g FROM pylovo.postcode WHERE plz = ANY(%(p)s))
           SELECT p.plz, ST_AsBinary(p.geom)
           FROM pylovo.postcode p, own
           WHERE p.plz = ANY(%(p)s) OR ST_DWithin(p.geom, own.g, %(d)s)""",
        {"p": [int(plz) for plz in plz_list], "d": float(distance_m)})
    return {int(plz): shapely_wkb.loads(bytes(geom)) for plz, geom in dbc.cur.fetchall()}


def border_kinds(polygons: dict[int, shapely.Geometry], plz_list: list[int], generated: set[int]
                 ) -> dict[str, shapely.Geometry]:
    """The borders of ``plz_list`` by kind: ``generated``, ``open`` and ``outer`` (see the module docstring)."""
    parts: dict[str, list] = {"generated": [], "open": [], "outer": []}
    for plz in plz_list:
        if plz not in polygons:
            raise ValueError(f"PLZ {plz} has no polygon in pylovo.postcode")
        boundary = polygons[plz].boundary
        matched = []
        for other, polygon in polygons.items():
            if other == plz:
                continue
            zone = polygon.buffer(BORDER_TOLERANCE_M)
            if not zone.intersects(boundary):
                continue
            # the tolerance zone also catches stubs of the edges that meet the shared border at its ends
            segment = [line for line in getattr(boundary.intersection(zone), "geoms", [boundary.intersection(zone)])
                       if line.length >= _MIN_SEGMENT_M]
            if not segment:
                continue
            parts["generated" if other in generated else "open"].append(unary_union(segment))
            matched.append(zone)
        rest = boundary.difference(unary_union(matched)) if matched else boundary
        if not rest.is_empty:
            parts["outer"].append(rest)
    return {kind: unary_union(items) if items else shapely.GeometryCollection() for kind, items in parts.items()}


def grid_edges(dbc, inf_dbc, plz_list: list[int], version_id: str = VERSION_ID) -> pd.DataFrame:
    """One row per grid of ``plz_list`` with its size and distances to the borders and the scope edge."""
    plz_list = [int(plz) for plz in plz_list]
    dbc.cur.execute("SELECT postcode_result_plz FROM pylovo.postcode_result WHERE version_id = %s", (version_id,))
    generated = {int(row[0]) for row in dbc.cur.fetchall()}
    polygons = _postcode_polygons(dbc, plz_list)
    kinds = border_kinds(polygons, plz_list, generated)
    region = unary_union([polygons[plz] for plz in plz_list])
    scope_wkb = inf_dbc.fetch_scope_wkb(shapely_wkb.dumps(region), TARGET_EPSG, 2000.0)
    scope_edge = shapely_wkb.loads(scope_wkb).boundary if scope_wkb else None
    grids = pd.read_sql_query(
        """SELECT gr.grid_result_id, gr.plz, gr.kcid, gr.bcid, count(br.objectid) AS n_buildings,
                  ST_AsBinary(ST_Collect(br.centroid)) AS points
           FROM pylovo.grid_result gr
           JOIN pylovo.buildings_result br ON br.version_id = gr.version_id AND br.grid_result_id = gr.grid_result_id
           WHERE gr.version_id = %(v)s AND gr.plz = ANY(%(p)s)
           GROUP BY gr.grid_result_id, gr.plz, gr.kcid, gr.bcid
           ORDER BY gr.grid_result_id""",
        con=dbc.conn, params={"v": version_id, "p": plz_list})
    points = [shapely_wkb.loads(bytes(value)) for value in grids.pop("points")]
    for kind, geometry in kinds.items():
        grids[f"{kind}_border_m"] = [geometry.distance(p) if not geometry.is_empty else np.nan for p in points]
    grids["scope_edge_m"] = [scope_edge.distance(p) if scope_edge is not None else np.nan for p in points]
    grids["small"] = grids["n_buildings"] <= SMALL_GRID_MAX_BUILDINGS
    return grids


def summarize(grids: pd.DataFrame) -> dict:
    """Counts of grids and small grids, near each border kind and the scope edge."""
    small = grids[grids["small"]]
    out = {"grids": int(len(grids)), "buildings": int(grids["n_buildings"].sum()),
           "small_grids": int(len(small)), "small_grid_buildings": int(small["n_buildings"].sum())}
    for column in ("generated_border_m", "open_border_m", "outer_border_m", "scope_edge_m"):
        near = grids[column] <= NEAR_M
        out[f"grids_near_{column[:-2]}"] = int(near.sum())
        out[f"small_grids_near_{column[:-2]}"] = int((near & grids["small"]).sum())
    return out
