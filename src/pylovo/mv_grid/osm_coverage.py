"""Completeness of the OSM power layer (after Babli et al. 2025): the MV/LV station sites and MV overhead lines that
OpenStreetMap maps, against the stations of an MV variant, per settlement class and per land-use class.

A diagnostic of the input data, not of the plan; the planning never reads it. It shows how far the OSM-based parts of
the stage can be trusted in a region: the overhead lines on OSM corridors (``MV_OVERHEAD_LINES`` osm) and the
brownfield station candidates of the LV stage. Babli et al. (2025) compare mapped stations with an estimate per
land-use class; here the stations of the variant take the place of the estimate. Against the station sites of two
DSOs (2026-10-19), the ratio of mapped to planned stations per settlement class tracked the true share of mapped
stations within 2 points where the planned count was right, and overstated it by 5 to 9 points elsewhere.

* **Station sites** (:func:`station_objects`): ``power=substation`` features that are no site of a higher level (no
  voltage above :data:`MAX_STATION_KV`; not transmission, traction, converter, generation or compensation; railway
  current and DB operators excluded; a polygon of at least :data:`LARGE_SITE_M2` counts as an HV/MV site unless its
  voltages are MV only), ``power=transformer`` features up to :data:`MAX_STATION_KV` outside the HV/MV sites,
  buildings tagged ``transformer_tower`` and ``power=pole`` nodes with a ``transformer`` tag. Features out of service
  (lifecycle prefixes) do not count, and features closer than :data:`MERGE_M` are one site.
* **Classes**: the settlement classes of the routing graph (``town``: an urban settlement, ``village``: another
  settlement, ``open_field``) and the OSM ``landuse`` classes (:data:`LAND_USE`; overlaps go to the first class of
  :data:`LAND_USE_ORDER`; ``other`` is any other land use, ``none`` the area without one).
* **Per class** (:func:`coverage_table`): the area, the stations of the variant, the OSM station sites and their ratio
  (``osm_per_station``), and the OSM MV line km and km per km².

The module is pure (numpy, pandas, shapely, SciPy); the database reads are in ``MvSourceMixin`` and ``MvGraphMixin``.
"""
from __future__ import annotations

import json
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid.osm_lines import LIFECYCLE_PREFIXES, is_railway_current, parse_voltages

#: Highest voltage of an MV/LV station in kV (MV networks run at 10 to 30 kV).
MAX_STATION_KV: float = 30.0
#: A substation with a voltage of at least this (kV) is a site of a higher level.
HV_MIN_KV: float = 60.0
#: A substation polygon at least this large (m²) is an HV/MV site unless its voltages are MV only.
LARGE_SITE_M2: float = 1000.0
#: ``substation`` tags that are never an MV/LV station.
EXCLUDED_TAGS: frozenset[str] = frozenset({"transmission", "traction", "converter", "generation", "compensation"})
#: Margin around an HV/MV site (m) inside which a transformer belongs to the site.
HV_SITE_MARGIN_M: float = 10.0
#: Station features closer than this are one site (m).
MERGE_M: float = 15.0
#: Land-use classes: OSM ``landuse`` values per class (after Babli et al. 2025, with the rural classes added).
LAND_USE: dict[str, tuple[str, ...]] = {
    "industrial": ("industrial",),
    "commercial": ("commercial", "retail"),
    "residential": ("residential",),
    "agricultural": ("farmland", "farmyard", "meadow", "orchard", "vineyard", "greenhouse_horticulture",
                     "plant_nursery", "animal_keeping"),
    "forest": ("forest",),
}
#: Order of the land-use classes where polygons overlap, then the rest.
LAND_USE_ORDER: tuple[str, ...] = (*LAND_USE, "other", "none")
#: Settlement classes of the routing graph.
SETTLEMENT_ORDER: tuple[str, ...] = ("town", "village", "open_field")
#: Columns of :func:`coverage_table`.
COVERAGE_COLUMNS: tuple[str, ...] = ("area_km2", "stations", "osm_stations", "osm_per_station", "osm_line_km",
                                     "osm_line_km_per_km2")


# ---------------------------------------------------------------------------------------------- station sites
def _tags(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _vmax(tags: Mapping[str, Any]) -> float | None:
    volts = [v for v in parse_voltages(tags.get("voltage")) if v > 0]
    return max(volts) if volts else None


def _out_of_service(tags: Mapping[str, Any]) -> bool:
    return str(tags.get("abandoned", "")).lower() == "yes" or any(
        str(k).partition(":")[0] in LIFECYCLE_PREFIXES and str(k).endswith((":power", ":building")) for k in tags)


def _railway(tags: Mapping[str, Any]) -> bool:
    return (is_railway_current(tags.get("frequency")) or tags.get("substation") == "traction"
            or str(tags.get("operator", "")).startswith(("DB Energie", "Deutsche Bahn")))


def station_objects(objects: pd.DataFrame, merge_m: float = MERGE_M) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The MV/LV station sites among OSM power features, and the HV/MV sites.

    Args:
        objects: Columns ``source_id``, ``kind`` (``substation``, ``transformer``, ``transformer_tower`` or
            ``pole``), ``tags`` (dict or JSON text), ``x``, ``y`` (the point, point-on-surface of a polygon) and
            ``area_m2`` (0 for nodes).
        merge_m: Station features closer than this are one site (the first in the order tower, substation,
            transformer, pole is kept).

    Returns:
        ``(stations, hv_sites)``: ``source_id``, ``kind``, ``x``, ``y`` of the station sites; ``source_id``, ``x``,
        ``y``, ``area_m2`` of the HV/MV sites.
    """
    frame = pd.DataFrame(objects)
    stations, hv = [], []
    for row in frame.itertuples(index=False):
        tags = _tags(getattr(row, "tags", None))
        if _out_of_service(tags) or not (math.isfinite(float(row.x)) and math.isfinite(float(row.y))):
            continue
        vmax = _vmax(tags)
        area = float(getattr(row, "area_m2", 0.0) or 0.0)
        record = {"source_id": row.source_id, "kind": row.kind, "x": float(row.x), "y": float(row.y)}
        if row.kind == "substation":
            if (vmax is not None and vmax >= HV_MIN_KV * 1000.0) or tags.get("substation") == "transmission" or \
                    (area >= LARGE_SITE_M2 and not (vmax is not None and vmax <= MAX_STATION_KV * 1000.0)):
                hv.append({**record, "area_m2": area})
            elif not (_railway(tags) or tags.get("substation") in EXCLUDED_TAGS
                      or (vmax is not None and vmax > MAX_STATION_KV * 1000.0)):
                stations.append(record)
        elif row.kind == "transformer":
            if not (_railway(tags) or (vmax is not None and vmax > MAX_STATION_KV * 1000.0)):
                stations.append(record)
        elif row.kind == "transformer_tower":
            stations.append(record)
        elif row.kind == "pole" and str(tags.get("transformer", "no")).lower() not in ("no", ""):
            stations.append(record)
    hv_sites = pd.DataFrame(hv, columns=["source_id", "kind", "x", "y", "area_m2"]).drop(columns="kind")
    out = pd.DataFrame(stations, columns=["source_id", "kind", "x", "y"])
    if not len(out):
        return out, hv_sites
    from scipy.spatial import cKDTree

    xy = out[["x", "y"]].to_numpy(dtype=float)
    if len(hv_sites):
        radius = np.sqrt(hv_sites["area_m2"].to_numpy(dtype=float) / math.pi) + HV_SITE_MARGIN_M
        tree = cKDTree(hv_sites[["x", "y"]].to_numpy(dtype=float))
        inside = np.zeros(len(out), dtype=bool)
        for i, near in enumerate(tree.query_ball_point(xy, float(radius.max()))):
            inside[i] = any(np.hypot(*(xy[i] - tree.data[j])) <= radius[j] for j in near)
        out = out[~inside | (out["kind"] == "transformer_tower")].reset_index(drop=True)
        xy = out[["x", "y"]].to_numpy(dtype=float)
    rank = out["kind"].map({"transformer_tower": 0, "substation": 1, "transformer": 2, "pole": 3}).fillna(4)
    order = np.lexsort((out["source_id"].astype(str).to_numpy(), rank.to_numpy()))
    taken = np.zeros(len(out), dtype=bool)
    keep = np.zeros(len(out), dtype=bool)
    tree = cKDTree(xy)
    for i in order:
        if taken[i]:
            continue
        keep[i] = True
        taken[tree.query_ball_point(xy[i], merge_m)] = True
    return out[keep].reset_index(drop=True), hv_sites


# ---------------------------------------------------------------------------------------------- classes
def land_use_class(osm_type: Any) -> str:
    """The class of an OSM ``landuse`` value (:data:`LAND_USE`), ``other`` for any other value."""
    value = str(osm_type).strip().lower() if osm_type is not None else ""
    return next((name for name, values in LAND_USE.items() if value in values), "other")


def _exclusive(parts: Mapping[str, list[Any]], order: Sequence[str], region: Any, rest: str) -> dict[str, Any]:
    """One geometry per class inside ``region`` without overlaps (each class loses the area of the classes before
    it); the class ``rest`` is the remaining region."""
    import shapely

    taken = shapely.Polygon()
    out: dict[str, Any] = {}
    for name in order:
        if name == rest:
            continue
        geoms = [g for g in parts.get(name, []) if g is not None and not g.is_empty]
        geom = shapely.union_all(shapely.make_valid(np.asarray(geoms, dtype=object))) if geoms \
            else shapely.Polygon()
        geom = shapely.difference(shapely.intersection(geom, region), taken)
        out[name] = geom
        taken = shapely.union(taken, geom)
    out[rest] = shapely.difference(region, taken)
    return {name: out[name] for name in order}


def land_use_geometries(landuse: pd.DataFrame, region: Any) -> dict[str, Any]:
    """The land-use classes (:data:`LAND_USE_ORDER`) of OSM ``landuse`` polygons (``osm_type``, ``geometry``)."""
    parts: dict[str, list[Any]] = {}
    for osm_type, geom in zip(landuse["osm_type"], landuse["geometry"]) if len(landuse) else ():
        parts.setdefault(land_use_class(osm_type), []).append(geom)
    return _exclusive(parts, LAND_USE_ORDER, region, "none")


def settlement_geometries(settlements: pd.DataFrame, region: Any) -> dict[str, Any]:
    """The settlement classes (:data:`SETTLEMENT_ORDER`) of the settlement polygons of the routing graph (``urban``,
    ``geometry``)."""
    parts: dict[str, list[Any]] = {"town": [], "village": []}
    for urban, geom in zip(settlements["urban"], settlements["geometry"]) if len(settlements) else ():
        parts["town" if bool(urban) else "village"].append(geom)
    return _exclusive(parts, SETTLEMENT_ORDER, region, "open_field")


def classify_points(xy: np.ndarray, geometries: Mapping[str, Any]) -> np.ndarray:
    """The class of every point: the first class geometry that contains it, else the last class."""
    import shapely

    xy = np.asarray(xy, dtype=float).reshape(-1, 2)
    names = list(geometries)
    out = np.full(len(xy), names[-1], dtype=object)
    open_ = np.ones(len(xy), dtype=bool)
    for name in names[:-1]:
        geom = geometries[name]
        if geom is None or geom.is_empty or not open_.any():
            continue
        shapely.prepare(geom)
        inside = np.zeros(len(xy), dtype=bool)
        inside[open_] = shapely.contains_xy(geom, xy[open_, 0], xy[open_, 1])
        out[inside] = name
        open_ &= ~inside
    return out


# ---------------------------------------------------------------------------------------------- tables
def coverage_table(region: Any, geometries: Mapping[str, Any], stations_xy: np.ndarray, osm_xy: np.ndarray,
                   osm_lines: Iterable[Any] = ()) -> pd.DataFrame:
    """OSM completeness per class (index: the classes of ``geometries`` and ``all``, columns
    :data:`COVERAGE_COLUMNS`).

    Args:
        region: The planning region (shapely polygon, ``TARGET_EPSG``).
        geometries: Class geometries without overlaps (:func:`land_use_geometries`, :func:`settlement_geometries`).
        stations_xy: The stations of the variant, (n, 2).
        osm_xy: The OSM station sites (:func:`station_objects`), (m, 2).
        osm_lines: The OSM MV overhead lines (shapely lines).

    Returns:
        ``area_km2``, ``stations`` and ``osm_stations`` (counts inside the region), ``osm_per_station`` (NaN without
        stations), ``osm_line_km`` and ``osm_line_km_per_km2``.
    """
    import shapely

    def inside(xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        xy = np.asarray(xy, dtype=float).reshape(-1, 2)
        keep = shapely.contains_xy(region, xy[:, 0], xy[:, 1]) if len(xy) else np.zeros(0, dtype=bool)
        return classify_points(xy, geometries), keep

    station_class, station_in = inside(stations_xy)
    osm_class, osm_in = inside(osm_xy)
    lines = [g for g in osm_lines if g is not None and not g.is_empty]
    merged = shapely.intersection(shapely.union_all(np.asarray(lines, dtype=object)), region) if lines else None
    rows = {}
    for name, geom in geometries.items():
        rows[name] = {"area_km2": float(geom.area) / 1e6,
                      "stations": int(((station_class == name) & station_in).sum()),
                      "osm_stations": int(((osm_class == name) & osm_in).sum()),
                      "osm_line_km": float(shapely.intersection(merged, geom).length) / 1000.0
                      if merged is not None else 0.0}
    table = pd.DataFrame.from_dict(rows, orient="index")
    table.loc["all"] = table.sum()
    table["stations"] = table["stations"].astype(int)
    table["osm_stations"] = table["osm_stations"].astype(int)
    table["osm_per_station"] = table["osm_stations"] / table["stations"].where(table["stations"] > 0)
    table["osm_line_km_per_km2"] = table["osm_line_km"] / table["area_km2"].where(table["area_km2"] > 0)
    return table[list(COVERAGE_COLUMNS)]


def _rows(table: pd.DataFrame) -> list[dict]:
    def number(value: Any) -> float | int | None:
        if isinstance(value, (int, np.integer)):
            return int(value)
        value = float(value)
        return round(value, 4) if math.isfinite(value) else None

    return [{"class": str(name), **{column: number(table.at[name, column]) for column in COVERAGE_COLUMNS}}
            for name in table.index]


def coverage_record(settlement: pd.DataFrame, land_use: pd.DataFrame, n_hv_sites: int = 0) -> dict:
    """The JSON-native ``mv_variant.summary['osm_coverage']``: ``settlement`` and ``land_use`` (one dict per class,
    ``all`` last), the number of HV/MV sites found and the rule constants."""
    return {"settlement": _rows(settlement), "land_use": _rows(land_use), "n_hv_sites": int(n_hv_sites),
            "max_station_kv": MAX_STATION_KV, "merge_m": MERGE_M}


def coverage_line(record: Mapping[str, Any]) -> str:
    """The summary line of an OSM coverage record: mapped station sites per planned station, overall and per
    settlement class, and the OSM MV line km."""
    classes = {c["class"]: c for c in record.get("settlement") or []}
    total = classes.get("all")
    if not total:
        return "✗ OSM coverage: not computed"

    def share(c: Mapping[str, Any]) -> str:
        ratio = c.get("osm_per_station")
        return "–" if ratio is None else f"{100 * ratio:.0f} %"

    parts = [f"{name.replace('_', ' ')} {share(classes[name])}" for name in SETTLEMENT_ORDER
             if name in classes and (classes[name].get("stations") or 0) > 0]
    return (f"✓ OSM coverage: {total['osm_stations']:,} mapped MV/LV station sites for {total['stations']:,} planned "
            f"stations ({share(total)}; {', '.join(parts)}), OSM MV lines {total['osm_line_km']:,.0f} km "
            f"({(total.get('osm_line_km_per_km2') or 0.0):.2f} km/km²)")


def describe_table(table: pd.DataFrame) -> str:
    """A plain-text table of :func:`coverage_table` (for logs and notebooks)."""
    shown = table.copy()
    shown["osm_per_station"] = (100 * shown["osm_per_station"]).round(0)
    return shown.round({"area_km2": 1, "osm_line_km": 1, "osm_line_km_per_km2": 3}).to_string()


def summarise(region: Any, objects: pd.DataFrame, stations_xy: np.ndarray, settlements: pd.DataFrame,
              landuse: pd.DataFrame, osm_lines: Sequence[Any] = ()
              ) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Both coverage tables of the raw OSM power features (:func:`station_objects` first) and their record."""
    stations, hv_sites = station_objects(objects)
    osm_xy = stations[["x", "y"]].to_numpy(dtype=float)
    lines = list(osm_lines)
    by_settlement = coverage_table(region, settlement_geometries(settlements, region), stations_xy, osm_xy, lines)
    by_land_use = coverage_table(region, land_use_geometries(landuse, region), stations_xy, osm_xy, lines)
    return by_settlement, by_land_use, coverage_record(by_settlement, by_land_use, len(hv_sites))
