"""Research tools for MV grids: published values, other grid data sets and comparisons between variants.

The functions work on the generic node and line tables of :mod:`pylovo.mv_grid.metrics`, so the same code runs on
the grids of the MV stage, on the Ding0 2023 CSV grids (:func:`read_ding0_grid`), on pandapower nets such as
SimBench or ``pandapower.networks.mv_oberrhein()`` (:func:`net_to_tables`) and on external reference data converted
to the generic format. The MV stage itself does not use this module. It imports no database, configuration or
plotting module.

**Metric names** used by :data:`LITERATURE_RANGES` and :func:`compare_to_literature`:

- ring level (columns of :func:`~pylovo.mv_grid.metrics.ring_table`): ``ring_members``, ``ring_km``;
- feeder level (columns of :func:`~pylovo.mv_grid.metrics.feeder_table`): ``feeder_supply_points``, ``feeder_km``
  (half-ring length from the busbar to the NOP along the main path, without stubs);
- grid level (keys of :func:`~pylovo.mv_grid.metrics.topology_metrics` and :func:`region_metrics`):
  ``feeders_per_substation``, ``supply_points_per_substation``, ``km_per_supply_point``, ``cable_share``,
  ``km_per_km2``, ``circuit_km_per_substation``, ``station_spacing_m``, ``customer_share``;
- grid level from other sources: ``substation_peak_mva``, ``hv_mv_unit_mva``, ``peak_to_station_rating``,
  ``max_line_loading_percent``;
- sets of grids: ``open_ring_grid_share``, ``open_meshed_share``, ``length_ratio_to_real``.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable, Mapping, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid.metrics import (
    GENERIC_LINE_KINDS,
    GENERIC_NODE_KINDS,
    SUPPLY_POINT_KINDS,
    _clean,
    _key,
    _node_xy,
    _ratio,
)

__all__ = [
    "LITERATURE_RANGES",
    "LITERATURE_TOLERANCE",
    "LiteratureRange",
    "compare_to_literature",
    "earth_fault_current_a",
    "literature_ranges",
    "net_to_tables",
    "read_ding0_grid",
    "region_metrics",
    "ring_membership_ari",
    "traffic_light",
    "wasserstein_table",
]

#: Relative distance to a bound of a literature range that still counts as amber.
LITERATURE_TOLERANCE: float = 0.3

#: Earth capacitance per km of the standard MV cables (single-core screened XLPE cables: the operating capacitance
#: equals the earth capacitance): ``NA2XS2Y 1x240 RM/25 12/20 kV`` and ``6/10 kV`` of pandapower's standard types.
CABLE_EARTH_CAPACITANCE_NF_PER_KM: dict[float, float] = {20.0: 304.0, 10.0: 456.0}
#: Earth capacitance per km of an MV overhead line (typical textbook value, about 5 nF/km).
OVERHEAD_EARTH_CAPACITANCE_NF_PER_KM: float = 5.0

_HV_MIN_KV = 60.0      # pandapower buses at or above this voltage are HV (excluded from the MV tables)
_MV_MIN_KV = 1.0       # buses below this voltage are LV
_DING0_TARGET_EPSG = 25832


def _opt_float(value: Any) -> float | None:
    """Float or None (None, NaN and non-numbers give None)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


# ---------------------------------------------------------------------------------------------------------------
# Literature ranges
# ---------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LiteratureRange:
    """A published value or range of one MV grid metric.

    Attributes:
        metric: Metric name (see the module docstring for where each metric is computed).
        area_class: ``rural`` (rural and semi-urban grids), ``urban``, or None (any area).
        low: Lower bound, or None.
        high: Upper bound, or None.
        typical: Typical or mean value, or None. A range with only ``typical`` is compared as the point
            ``[typical, typical]``.
        label: Generic description of the reference (never a DSO name).
        citation: Short citation.
    """

    metric: str
    area_class: str | None
    low: float | None
    high: float | None
    typical: float | None
    label: str
    citation: str


_SIMBENCH = "Meinecke et al. 2020 (SimBench documentation, Table 4.2)"
_BMWI = "Buechner et al. 2014 (BMWi Verteilernetzstudie, Table 3)"
_DENA = "Agricola et al. 2012 (dena-Verteilnetzstudie)"
_23C = "Section 23c EnWG publications (derived)"
_KREUTMAYR = "Kreutmayr 2023"
_FGH = "FGH TB 302 (via Amme et al. 2018)"

#: Published values and ranges of MV grid metrics (DESIGN section 7.3); labels are generic.
LITERATURE_RANGES: tuple[LiteratureRange, ...] = (
    # ring members per ring
    LiteratureRange("ring_members", "rural", None, 20, None, "Ding0 planning limit", "Amme et al. 2018"),
    LiteratureRange("ring_members", "rural", 5, 10, None, "textbook open ring",
                    "Heuck, Dettmann and Schulz, Elektrische Energieversorgung"),
    LiteratureRange("ring_members", "urban", 5, 30, 15, "MV rings of a large German city", _KREUTMAYR),
    LiteratureRange("ring_members", "urban", 11, 27, None, "representative rings of a large German city", _KREUTMAYR),
    # supply points per feeder (half-ring)
    LiteratureRange("feeder_supply_points", "rural", 5, 22, 11.4, "SimBench rural MV grid", _SIMBENCH),
    LiteratureRange("feeder_supply_points", "rural", 6, 22, 12.3, "SimBench semi-urban MV grid", _SIMBENCH),
    LiteratureRange("feeder_supply_points", None, None, None, 15, "BMWi model grids (all classes)", _BMWI),
    LiteratureRange("feeder_supply_points", "urban", 4, 18, 9.5, "SimBench urban MV grid", _SIMBENCH),
    # feeders (busbar outlets) per substation
    LiteratureRange("feeders_per_substation", "rural", None, None, 8, "BMWi model grids", _BMWI),
    LiteratureRange("feeders_per_substation", "rural", 8, 9, None, "SimBench rural and semi-urban MV grids", _SIMBENCH),
    LiteratureRange("feeders_per_substation", "urban", None, None, 14, "SimBench urban MV grid", _SIMBENCH),
    LiteratureRange("feeders_per_substation", "urban", None, None, 20,
                    "large German city (about 10 rings per substation)", _KREUTMAYR),
    # feeder (half-ring) length in km
    LiteratureRange("feeder_km", "rural", 7.3, 22.3, 12.4, "SimBench rural MV grid", _SIMBENCH),
    LiteratureRange("feeder_km", "rural", 3.4, 11.9, 6.4, "SimBench semi-urban MV grid", _SIMBENCH),
    LiteratureRange("feeder_km", "rural", 7.4, 16.0, None, "BMWi model grid classes 2 to 8", _BMWI),
    LiteratureRange("feeder_km", "rural", None, None, 16.4, "southern German rural MV grids (median)",
                    "Almomani and Witzmann 2024"),
    LiteratureRange("feeder_km", "urban", 0.7, 4.7, 2.2, "SimBench urban MV grid", _SIMBENCH),
    LiteratureRange("feeder_km", "urban", None, None, 4.8, "BMWi model grid class 1 (urban)", _BMWI),
    # ring length in km
    LiteratureRange("ring_km", "rural", None, 60, None, "planning guide maximum", _FGH),
    LiteratureRange("ring_km", "urban", 3.6, 17.7, None, "representative rings of a large German city", _KREUTMAYR),
    # supply points per substation
    LiteratureRange("supply_points_per_substation", "rural", 70, 250, None, "realistic MV grid size", _SIMBENCH),
    LiteratureRange("supply_points_per_substation", "rural", 92, 134, None, "SimBench MV grids", _SIMBENCH),
    LiteratureRange("supply_points_per_substation", None, 61, 580, 178, "EU DSO Observatory (median, p5 to p95)",
                    "Prettico et al. 2016 (JRC)"),
    # circuit km per supply point
    LiteratureRange("km_per_supply_point", "rural", None, None, 1.1, "SimBench rural MV grid (derived)", _SIMBENCH),
    LiteratureRange("km_per_supply_point", "rural", None, None, 0.5, "SimBench semi-urban MV grid (derived)",
                    _SIMBENCH),
    LiteratureRange("km_per_supply_point", "rural", None, None, 0.75, "large rural Bavarian DSO (derived)", _23C),
    LiteratureRange("km_per_supply_point", None, None, None, 0.73, "EU DSO Observatory (median)",
                    "Prettico et al. 2016 (JRC)"),
    LiteratureRange("km_per_supply_point", "urban", None, None, 0.23, "SimBench urban MV grid (derived)", _SIMBENCH),
    # cable share of the circuit length
    LiteratureRange("cable_share", "rural", 0.57, 0.70, None, "Bavarian rural DSOs (2021 to 2025)", _23C),
    LiteratureRange("cable_share", "rural", None, None, 0.71, "median of 74 German MV grids", "Meinecke et al. 2020"),
    LiteratureRange("cable_share", None, 0.40, 0.95, None, "BMWi model grid classes", _BMWI),
    LiteratureRange("cable_share", "urban", None, None, 0.985, "large urban DSO", _23C),
    # circuit km per km2
    LiteratureRange("km_per_km2", "rural", 1.0, 1.2, None, "Bavarian rural DSOs", _23C),
    LiteratureRange("km_per_km2", "rural", None, None, 1.6, "large south-west German DSO", _23C),
    LiteratureRange("km_per_km2", "urban", None, None, 7.8, "large urban DSO", _23C),
    # substations
    LiteratureRange("substation_peak_mva", None, 60, 71, None, "German HV/MV substations (derived)",
                    _DENA + ", Table 5.5"),
    LiteratureRange("circuit_km_per_substation", None, 82, 124, None, "German HV/MV substations (derived)",
                    _DENA + ", Table 5.5"),
    LiteratureRange("hv_mv_unit_mva", "rural", None, None, 25, "SimBench rural MV grid (2 units)", _SIMBENCH),
    LiteratureRange("hv_mv_unit_mva", "rural", None, None, 40, "SimBench semi-urban MV grid (2 units)", _SIMBENCH),
    LiteratureRange("hv_mv_unit_mva", "urban", None, None, 63, "SimBench urban MV grid (2 units)", _SIMBENCH),
    LiteratureRange("peak_to_station_rating", None, 0.3, 0.5, None,
                    "substation peak over installed MV/LV ratings (indicative)", _DENA),
    # station spacing
    LiteratureRange("station_spacing_m", "rural", 500, 2000, None, "planning guide", _FGH),
    LiteratureRange("station_spacing_m", "urban", 100, 400, None, "planning guide", _FGH),
    # structure of sets of grids
    LiteratureRange("open_ring_grid_share", None, None, None, 0.843, "German MV grids operated as open rings",
                    _BMWI + " (VDE-ETG 2010)"),
    LiteratureRange("open_meshed_share", None, None, None, 0.95, "open-meshed share, median of 14 DSOs",
                    "Weisenstein and Wellssow 2021"),
    # MV customers and validation of synthetic grids
    LiteratureRange("customer_share", "urban", None, None, round(5 / 139, 4),
                    "SimBench urban MV grid (5 of 139 supply points)", _SIMBENCH),
    LiteratureRange("length_ratio_to_real", None, None, None, 0.44, "Ding0 district median against DSO data",
                    "Mueller et al. 2019 (open_eGo final report)"),
    # loading
    LiteratureRange("max_line_loading_percent", None, None, 60, None, "planning limit in normal operation",
                    _DENA + "; Harnisch et al. 2016; Wintzek et al. 2021"),
    LiteratureRange("max_line_loading_percent", None, None, 50, None, "SimBench planning limit",
                    "Meinecke et al. 2019"),
)


def literature_ranges(metric: str | None = None, area_class: str | None = None) -> pd.DataFrame:
    """Return ``LITERATURE_RANGES`` as a data frame, optionally filtered.

    Args:
        metric: Keep only this metric.
        area_class: Keep the ranges of this area class and the class-free ones.

    Returns:
        One row per range with the fields of ``LiteratureRange``.
    """
    df = pd.DataFrame([r.__dict__ for r in LITERATURE_RANGES])
    if metric is not None:
        df = df[df["metric"] == metric]
    if area_class is not None:
        df = df[df["area_class"].isna() | (df["area_class"] == area_class)]
    return df.reset_index(drop=True)


def traffic_light(value: float, low: float | None, high: float | None, typical: float | None = None,
                  tolerance: float = LITERATURE_TOLERANCE) -> tuple[str, float]:
    """Classify a value against a literature range.

    Green inside the range, amber within ``tolerance`` (relative) of the violated bound, red beyond. A range with
    only ``typical`` is the point ``[typical, typical]``.

    Args:
        value: Metric value.
        low: Lower bound or None.
        high: Upper bound or None.
        typical: Typical value, used as both bounds when ``low`` and ``high`` are None.
        tolerance: Relative distance to a bound that still counts as amber.

    Returns:
        ``(status, deviation)``: status ``green``, ``amber``, ``red`` or ``n/a`` (value or range missing), and the
        relative distance to the violated bound (0 inside, NaN for ``n/a``).
    """
    low = _opt_float(low)
    high = _opt_float(high)
    typical = _opt_float(typical)
    if low is None and high is None:
        low = high = typical
    if value is None or not np.isfinite(value) or (low is None and high is None):
        return "n/a", float("nan")
    value = float(value)
    if low is not None and value < low:
        deviation = (low - value) / abs(low) if low != 0 else float("inf")
    elif high is not None and value > high:
        deviation = (value - high) / abs(high) if high != 0 else float("inf")
    else:
        deviation = 0.0
    if deviation == 0.0:
        return "green", 0.0
    return ("amber" if deviation <= tolerance else "red"), float(deviation)


def compare_to_literature(metrics: pd.DataFrame | Mapping[str, Any], area_class: str | None = None,
                          tolerance: float = LITERATURE_TOLERANCE) -> pd.DataFrame:
    """Compare metric values with ``LITERATURE_RANGES`` (traffic lights).

    Every column of ``metrics`` named like a ``LiteratureRange.metric`` is compared with every range of that metric
    that applies to the row's area class: the ranges of the class and the class-free ones. The class is
    ``area_class`` when given, else the row's ``area_class`` column, else None (then every range of the metric
    applies).

    Args:
        metrics: One row per entity (district, ring, feeder, variant, ...) with metric columns, or one mapping of
            metric values. The index identifies the entities.
        area_class: Area class for all rows (``rural`` or ``urban``), overriding an ``area_class`` column.
        tolerance: Relative distance to a bound that still counts as amber.

    Returns:
        One row per (entity, metric, range): the index levels of ``metrics`` as columns (``entity`` if unnamed),
        ``metric``, ``area_class`` (of the range), ``value``, ``low``, ``high``, ``typical``, ``label``,
        ``citation``, ``status`` and ``deviation``.
    """
    if not isinstance(metrics, pd.DataFrame):
        metrics = pd.DataFrame([dict(metrics)])
    names = [n if n is not None else ("entity" if metrics.index.nlevels == 1 else f"level_{i}")
             for i, n in enumerate(metrics.index.names)]
    ranges = literature_ranges()
    columns = list(names) + ["metric", "area_class", "value", "low", "high", "typical", "label", "citation",
                             "status", "deviation"]
    rows: list[dict] = []
    metric_columns = [m for m in dict.fromkeys(ranges["metric"]) if m in metrics.columns]
    for idx, row in metrics.iterrows():
        keys = idx if isinstance(idx, tuple) else (idx,)
        row_class = area_class
        if row_class is None and "area_class" in metrics.columns:
            row_class = row["area_class"] if isinstance(row["area_class"], str) else None
        for metric in metric_columns:
            value = _opt_float(row[metric])
            applicable = ranges[ranges["metric"] == metric]
            if row_class is not None:
                applicable = applicable[applicable["area_class"].isna() | (applicable["area_class"] == row_class)]
            for rng in applicable.itertuples(index=False):
                status, deviation = traffic_light(value if value is not None else float("nan"), rng.low, rng.high,
                                                  rng.typical, tolerance)
                rows.append({**dict(zip(names, keys)), "metric": metric,
                             "area_class": rng.area_class if isinstance(rng.area_class, str) else None,
                             "value": value, "low": _opt_float(rng.low), "high": _opt_float(rng.high),
                             "typical": _opt_float(rng.typical), "label": rng.label, "citation": rng.citation,
                             "status": status, "deviation": deviation})
    return pd.DataFrame(rows, columns=columns)


# ---------------------------------------------------------------------------------------------------------------
# Region metrics (clipped circuit length per area)
# ---------------------------------------------------------------------------------------------------------------


def region_metrics(lines_gdf: Any, region_geom: Any, nodes: pd.DataFrame | None = None) -> dict:
    """Circuit length per area of a region, with the line geometries clipped to the region (section 23c style).

    Each line contributes ``length_km`` times its ``parallel`` count and the share of its geometry inside the
    region, so detour factors and the cable/overhead split are kept. Ties and normally-open lines count: the
    regional installed inventory includes every circuit whose geometry is inside the region.

    Args:
        lines_gdf: GeoDataFrame of lines in a projected CRS (metres); ``length_km`` (default: geometry length),
            ``cable_km`` and ``overhead_km`` or ``kind`` (default: cable) are used when present;
            ``parallel`` defaults to 1 and must be a positive integer.
        region_geom: Region polygon (shapely geometry in the CRS of ``lines_gdf``, or a GeoSeries or GeoDataFrame,
            reprojected to the CRS of ``lines_gdf`` and dissolved).
        nodes: Optional generic node table; its stations and customers inside the region give the supply points.

    Returns:
        JSON-safe dict: ``region_area_km2``, ``route_km_in_region``, ``circuit_km_in_region``, ``cable_km_in_region``,
        ``overhead_km_in_region``, ``km_per_km2``, ``cable_share``, and with ``nodes`` also
        ``n_supply_points_in_region``, ``n_stations_in_region`` and ``km_per_supply_point``.

    Raises:
        ValueError: The line CRS is geographic.
    """
    import geopandas as gpd
    import shapely

    gdf = lines_gdf if isinstance(lines_gdf, gpd.GeoDataFrame) else gpd.GeoDataFrame(lines_gdf)
    crs = gdf.crs
    if crs is not None and crs.is_geographic:
        raise ValueError("region_metrics needs lines in a projected CRS (metres)")
    if isinstance(region_geom, (gpd.GeoSeries, gpd.GeoDataFrame)):
        series = region_geom.geometry if isinstance(region_geom, gpd.GeoDataFrame) else region_geom
        if crs is not None and series.crs is not None and series.crs != crs:
            series = series.to_crs(crs)
        region = shapely.union_all(series.to_numpy())
    else:
        region = region_geom
    geoms = gdf.geometry.to_numpy()
    total = shapely.length(geoms)
    inside = shapely.length(shapely.intersection(geoms, region))
    within = shapely.within(shapely.point_on_surface(geoms), region)
    share = np.where(total > 0, inside / np.where(total > 0, total, 1.0), within.astype(float))
    length_km = gdf["length_km"].to_numpy(dtype=float) if "length_km" in gdf.columns else total / 1000.0
    if "cable_km" in gdf.columns and "overhead_km" in gdf.columns:
        cable = gdf["cable_km"].to_numpy(dtype=float)
        overhead = gdf["overhead_km"].to_numpy(dtype=float)
    else:
        kinds = gdf["kind"].to_numpy() if "kind" in gdf.columns else np.full(len(gdf), "cable")
        cable = np.where(kinds == "overhead", 0.0, length_km)
        overhead = np.where(kinds == "overhead", length_km, 0.0)
    parallel = pd.to_numeric(gdf["parallel"].fillna(1), errors="coerce") if "parallel" in gdf else pd.Series(
        np.ones(len(gdf)))
    if (~np.isfinite(parallel) | (parallel < 1) | (parallel % 1 != 0)).any():
        raise ValueError("parallel must be a positive integer")
    weighted_share = share * parallel.to_numpy(dtype=float)
    area_km2 = float(region.area) / 1e6
    circuit = float(np.sum(length_km * weighted_share))
    out: dict[str, Any] = {
        "region_area_km2": area_km2,
        "route_km_in_region": float(np.sum(length_km * share)),
        "circuit_km_in_region": circuit,
        "cable_km_in_region": float(np.sum(cable * weighted_share)),
        "overhead_km_in_region": float(np.sum(overhead * weighted_share)),
        "km_per_km2": _ratio(circuit, area_km2),
        "cable_share": _ratio(float(np.sum(cable * weighted_share)), circuit),
    }
    if nodes is not None:
        pts = nodes[nodes["kind"].isin(SUPPLY_POINT_KINDS)]
        xy = _node_xy(pts)
        ok = np.isfinite(xy).all(axis=1)
        inside_pts = np.zeros(len(pts), dtype=bool)
        inside_pts[ok] = shapely.contains_xy(region, xy[ok, 0], xy[ok, 1])
        n_supply = int(inside_pts.sum())
        out["n_supply_points_in_region"] = n_supply
        out["n_stations_in_region"] = int((inside_pts & (pts["kind"] == "station").to_numpy()).sum())
        out["km_per_supply_point"] = _ratio(circuit, n_supply)
    return _clean(out)


# ---------------------------------------------------------------------------------------------------------------
# Electrical and comparison helpers
# ---------------------------------------------------------------------------------------------------------------


def earth_fault_current_a(cable_km: float, overhead_km: float, vn_kv: float, *,
                          cable_c_nf_per_km: float | None = None,
                          overhead_c_nf_per_km: float = OVERHEAD_EARTH_CAPACITANCE_NF_PER_KM,
                          f_hz: float = 50.0) -> float:
    """Capacitive earth-fault current of a galvanically connected MV grid (flag only).

    ``I_CE = sqrt(3) * 2 * pi * f * U_n * (C_E,cable * l_cable + C_E,overhead * l_overhead)``; about 3.3 A per km
    of ``NA2XS2Y 1x240 RM/25 12/20 kV`` at 20 kV.

    Args:
        cable_km: Cable circuit length (km).
        overhead_km: Overhead circuit length (km).
        vn_kv: Nominal voltage (kV).
        cable_c_nf_per_km: Earth capacitance of the cables; default from ``CABLE_EARTH_CAPACITANCE_NF_PER_KM``
            for 10 and 20 kV.
        overhead_c_nf_per_km: Earth capacitance of the overhead lines.
        f_hz: Grid frequency.

    Returns:
        The capacitive earth-fault current in A.

    Raises:
        ValueError: No default cable capacitance for ``vn_kv``.
    """
    if cable_c_nf_per_km is None:
        if float(vn_kv) not in CABLE_EARTH_CAPACITANCE_NF_PER_KM:
            raise ValueError(f"no default cable earth capacitance for {vn_kv} kV; pass cable_c_nf_per_km")
        cable_c_nf_per_km = CABLE_EARTH_CAPACITANCE_NF_PER_KM[float(vn_kv)]
    c_farad = (cable_c_nf_per_km * float(cable_km) + overhead_c_nf_per_km * float(overhead_km)) * 1e-9
    return float(math.sqrt(3.0) * 2.0 * math.pi * f_hz * float(vn_kv) * 1e3 * c_farad)


def wasserstein_table(values_by_variant: Mapping[str, Sequence[float]], reference: str) -> pd.DataFrame:
    """Distribution summary per variant and the Wasserstein distance to a reference variant.

    Args:
        values_by_variant: Metric values (e.g. ring members per ring) per variant; NaN and infinite values are
            dropped.
        reference: Variant to compare with.

    Returns:
        One row per variant (in the given order): ``variant``, ``n``, ``mean``, ``median``, ``p10``, ``p90``,
        ``wasserstein`` (``scipy.stats.wasserstein_distance`` to the reference, NaN if a side is empty) and
        ``wasserstein_rel`` (divided by the absolute reference mean).

    Raises:
        KeyError: ``reference`` is not a key of ``values_by_variant``.
    """
    from scipy.stats import wasserstein_distance

    if reference not in values_by_variant:
        raise KeyError(f"reference variant {reference!r} not in values_by_variant")

    def clean(values: Sequence[float]) -> np.ndarray:
        arr = np.asarray(list(values), dtype=float)
        return arr[np.isfinite(arr)]

    ref = clean(values_by_variant[reference])
    ref_mean = float(ref.mean()) if ref.size else float("nan")
    rows = []
    for variant, values in values_by_variant.items():
        arr = clean(values)
        w = float(wasserstein_distance(arr, ref)) if arr.size and ref.size else float("nan")
        rows.append({
            "variant": variant,
            "n": int(arr.size),
            "mean": float(arr.mean()) if arr.size else float("nan"),
            "median": float(np.median(arr)) if arr.size else float("nan"),
            "p10": float(np.percentile(arr, 10)) if arr.size else float("nan"),
            "p90": float(np.percentile(arr, 90)) if arr.size else float("nan"),
            "wasserstein": w,
            "wasserstein_rel": w / abs(ref_mean) if np.isfinite(w) and ref_mean not in (0.0,) and np.isfinite(ref_mean)
            else float("nan"),
        })
    return pd.DataFrame(rows, columns=["variant", "n", "mean", "median", "p10", "p90", "wasserstein",
                                       "wasserstein_rel"])


def ring_membership_ari(rings_a: Mapping[Hashable, Hashable], rings_b: Mapping[Hashable, Hashable]) -> float:
    """Adjusted Rand index of the "same ring" membership of the nodes two groupings share.

    Args:
        rings_a: Ring label per node (e.g. station) of grouping A.
        rings_b: Ring label per node of grouping B.

    Returns:
        ``sklearn.metrics.adjusted_rand_score`` over the common nodes, NaN with fewer than two common nodes.
    """
    from sklearn.metrics import adjusted_rand_score

    common = sorted(set(rings_a) & set(rings_b), key=_key)
    if len(common) < 2:
        return float("nan")
    labels_a = pd.factorize(pd.Series([rings_a[k] for k in common], dtype=object))[0]
    labels_b = pd.factorize(pd.Series([rings_b[k] for k in common], dtype=object))[0]
    return float(adjusted_rand_score(labels_a, labels_b))


# ---------------------------------------------------------------------------------------------------------------
# Readers: pandapower nets and Ding0 CSV grids
# ---------------------------------------------------------------------------------------------------------------


def _transformer(epsg: int | None):
    if epsg is None:
        return None
    from pyproj import Transformer

    return Transformer.from_crs(4326, epsg, always_xy=True)


def _bus_xy(net: Any, buses: pd.Index, epsg: int | None) -> tuple[np.ndarray, np.ndarray]:
    """Coordinates of buses from the WGS84 GeoJSON ``bus.geo`` column (NaN where missing)."""
    import json

    x = np.full(len(buses), np.nan)
    y = np.full(len(buses), np.nan)
    if "geo" in net.bus.columns:
        for i, geo in enumerate(net.bus.loc[buses, "geo"]):
            if isinstance(geo, str) and geo:
                coords = json.loads(geo).get("coordinates")
                if coords:
                    x[i], y[i] = float(coords[0]), float(coords[1])
    transformer = _transformer(epsg)
    ok = np.isfinite(x) & np.isfinite(y)
    if transformer is not None and ok.any():
        x[ok], y[ok] = transformer.transform(x[ok], y[ok])
    return x, y


def _line_kinds(net: Any) -> pd.Series:
    """Line kind per pandapower line: custom ``kind`` column, else ``type`` (cs/ol), else the std type."""
    line = net.line
    if "kind" in line.columns and line["kind"].isin(GENERIC_LINE_KINDS).all():
        return line["kind"].astype(str)
    std = net.std_types.get("line", {}) if hasattr(net, "std_types") else {}
    out = []
    for idx in line.index:
        t = line.at[idx, "type"] if "type" in line.columns else None
        if not isinstance(t, str):
            name = line.at[idx, "std_type"] if "std_type" in line.columns else None
            t = std.get(name, {}).get("type") if isinstance(name, str) else None
        out.append("overhead" if t == "ol" else "cable")
    return pd.Series(out, index=line.index, dtype=object)


def _mixed_split(net: Any, idx: Any) -> float | None:
    """Cable share of a mixed line from its length-weighted resistance and the std types '<cable> + <overhead>'."""
    line = net.line
    name = line.at[idx, "std_type_name"] if "std_type_name" in line.columns else line.at[idx, "std_type"]
    std = net.std_types.get("line", {})
    if not isinstance(name, str) or " + " not in name:
        return None
    cable_name, overhead_name = (s.strip() for s in name.split(" + ", 1))
    if cable_name not in std or overhead_name not in std:
        return None
    r_c, r_o = float(std[cable_name]["r_ohm_per_km"]), float(std[overhead_name]["r_ohm_per_km"])
    if r_c == r_o:
        return None
    return float(np.clip((r_o - float(line.at[idx, "r_ohm_per_km"])) / (r_o - r_c), 0.0, 1.0))


def net_to_tables(net: Any, epsg: int | None = 25832) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Convert a pandapower MV net into the generic node and line tables.

    MV buses (1 kV to below 60 kV) become nodes; buses joined by closed bus-bus switches are fused (the lowest
    index represents them) and open bus-bus switches become zero-length normally-open lines. Kinds come from a
    custom ``bus.kind`` column (MV stage nets) or are inferred: ``busbar`` = LV side of an HV/MV transformer or an
    MV bus with an external grid; ``station`` = MV side of an MV/LV transformer, or, in nets without MV/LV
    transformers, a bus with loads (a load ``type`` without "station", such as "MV Load", makes it a
    ``customer``); ``customer`` = a bus with loads in nets with MV/LV transformers; ``generator`` = a bus with
    static or synchronous generators or storage; ``joint`` = the rest. Lines with an open line switch, and lines out
    of service, are normally open (``open_at_node`` = the switch bus).

    Args:
        net: pandapower net (``pandapower.networks.mv_oberrhein()``, a SimBench MV net, a stored MV stage net).
        epsg: Target CRS of ``x``, ``y`` (the bus ``geo`` column is WGS84 GeoJSON); None keeps longitude and
            latitude.

    Returns:
        ``(nodes, lines)`` in the generic format. Nodes carry ``bus_name``, ``vn_kv``, ``n_transformers`` and
        ``transformer_mva`` (HV/MV units at a busbar), ``sn_kva`` (MV/LV units at a station) and the custom
        columns ``node_key`` and ``mv_node_id`` when present; lines carry ``name``, ``std_type`` and the custom
        columns ``line_key``, ``role`` and ``ring_id`` when present. ``lines.attrs["mixed_split_unknown"]``
        counts mixed lines whose split could not be derived (counted as cable).
    """
    bus = net.bus
    in_service = bus["in_service"] if "in_service" in bus.columns else pd.Series(True, index=bus.index)
    mv = bus.index[(bus["vn_kv"] >= _MV_MIN_KV) & (bus["vn_kv"] < _HV_MIN_KV) & in_service.astype(bool)]
    mv_set = set(mv)

    # fuse buses joined by closed bus-bus switches
    parent = {b: b for b in mv}

    def find(b: Any) -> Any:
        while parent[b] != b:
            parent[b] = parent[parent[b]]
            b = parent[b]
        return b

    switch = net.switch if hasattr(net, "switch") else pd.DataFrame(columns=["bus", "element", "et", "closed"])
    bb_switch = switch[(switch["et"] == "b") & switch["bus"].isin(mv_set) & switch["element"].isin(mv_set)]
    for b1, b2 in zip(bb_switch.loc[bb_switch["closed"].astype(bool), "bus"],
                      bb_switch.loc[bb_switch["closed"].astype(bool), "element"]):
        r1, r2 = find(b1), find(b2)
        if r1 != r2:
            parent[max(r1, r2)] = min(r1, r2)
    rep = {b: find(b) for b in mv}
    reps = sorted(set(rep.values()))

    # kinds
    trafo = net.trafo if hasattr(net, "trafo") else pd.DataFrame()
    if len(trafo):
        t_on = trafo[trafo["in_service"].astype(bool)] if "in_service" in trafo.columns else trafo
        hvmv = t_on[(t_on["vn_hv_kv"] >= _HV_MIN_KV) & t_on["lv_bus"].isin(mv_set)]
        mvlv = t_on[(t_on["vn_lv_kv"] < _MV_MIN_KV) & t_on["hv_bus"].isin(mv_set)]
    else:
        hvmv = mvlv = pd.DataFrame(columns=["hv_bus", "lv_bus", "sn_mva"])
    has_kind = "kind" in bus.columns and bus.loc[mv, "kind"].isin(GENERIC_NODE_KINDS).all()
    kind: dict[Any, str] = {}
    if has_kind:
        for b in mv:
            current = kind.get(rep[b])
            new = bus.at[b, "kind"]
            if current is None or GENERIC_NODE_KINDS.index(new) < GENERIC_NODE_KINDS.index(current):
                kind[rep[b]] = new
    else:
        busbars = {rep[b] for b in hvmv["lv_bus"]}
        if hasattr(net, "ext_grid") and len(net.ext_grid):
            busbars |= {rep[b] for b in net.ext_grid["bus"] if b in mv_set}
        stations = {rep[b] for b in mvlv["hv_bus"]}
        customers: set = set()
        load = net.load[net.load["bus"].isin(mv_set)] if hasattr(net, "load") else pd.DataFrame(columns=["bus"])
        if "in_service" in load.columns:
            load = load[load["in_service"].astype(bool)]
        for b, ltype in zip(load["bus"], load["type"] if "type" in load.columns else [None] * len(load)):
            r = rep[b]
            if len(mvlv):
                customers.add(r)
            elif isinstance(ltype, str) and ltype and "station" not in ltype.lower():
                customers.add(r)
            else:
                stations.add(r)
        generators: set = set()
        for table in ("sgen", "gen", "storage"):
            df = getattr(net, table, None)
            if df is not None and len(df):
                on = df[df["in_service"].astype(bool)] if "in_service" in df.columns else df
                generators |= {rep[b] for b in on["bus"] if b in mv_set}
        for r in reps:
            if r in busbars:
                kind[r] = "busbar"
            elif r in stations:
                kind[r] = "station"
            elif r in customers:
                kind[r] = "customer"
            elif r in generators:
                kind[r] = "generator"
            else:
                kind[r] = "joint"

    x, y = _bus_xy(net, pd.Index(reps), epsg)
    nodes = pd.DataFrame({"node_id": reps, "kind": [kind[r] for r in reps], "x": x, "y": y,
                          "bus_name": [bus.at[r, "name"] for r in reps],
                          "vn_kv": [float(bus.at[r, "vn_kv"]) for r in reps]})
    n_tr = Counter(rep[b] for b in hvmv["lv_bus"])
    mva = defaultdict(float)
    for b, s in zip(hvmv["lv_bus"], hvmv["sn_mva"]):
        mva[rep[b]] += float(s)
    kva = defaultdict(float)
    for b, s in zip(mvlv["hv_bus"], mvlv["sn_mva"]):
        kva[rep[b]] += float(s) * 1000.0
    nodes["n_transformers"] = [n_tr.get(r, 0) if kind[r] == "busbar" else np.nan for r in reps]
    nodes["transformer_mva"] = [mva.get(r, np.nan) if kind[r] == "busbar" else np.nan for r in reps]
    nodes["sn_kva"] = [kva.get(r, np.nan) if kind[r] == "station" else np.nan for r in reps]
    for column in ("node_key", "mv_node_id"):
        if column in bus.columns:
            nodes[column] = [bus.at[r, column] for r in reps]

    # lines
    line = net.line
    keep = line["from_bus"].isin(mv_set) & line["to_bus"].isin(mv_set)
    line = line[keep]
    kinds = _line_kinds(net).loc[line.index]
    open_at: dict[Any, Any] = {}
    line_switch = switch[(switch["et"] == "l") & switch["element"].isin(line.index)]
    for b, element, is_closed in zip(line_switch["bus"], line_switch["element"], line_switch["closed"]):
        if not bool(is_closed):
            open_at.setdefault(element, rep.get(b))
    out_of_service = ~line["in_service"].astype(bool) if "in_service" in line.columns else pd.Series(False,
                                                                                                    index=line.index)
    cable_km = np.where(kinds == "cable", line["length_km"], 0.0).astype(float)
    overhead_km = np.where(kinds == "overhead", line["length_km"], 0.0).astype(float)
    unknown_split = 0
    for pos, idx in enumerate(line.index):
        if kinds.loc[idx] != "mixed":
            continue
        if "cable_km" in line.columns and "overhead_km" in line.columns:
            cable_km[pos], overhead_km[pos] = float(line.at[idx, "cable_km"]), float(line.at[idx, "overhead_km"])
            continue
        share = _mixed_split(net, idx)
        if share is None:
            unknown_split += 1
            share = 1.0
        cable_km[pos] = share * float(line.at[idx, "length_km"])
        overhead_km[pos] = float(line.at[idx, "length_km"]) - cable_km[pos]
    lines = pd.DataFrame({
        "line_id": list(line.index),
        "from_node": [rep[b] for b in line["from_bus"]],
        "to_node": [rep[b] for b in line["to_bus"]],
        "length_km": line["length_km"].astype(float).to_numpy(),
        "kind": kinds.to_numpy(),
        "cable_km": cable_km,
        "overhead_km": overhead_km,
        "normally_open": [idx in open_at or bool(out_of_service.loc[idx]) for idx in line.index],
        "parallel": line["parallel"].astype(int).to_numpy() if "parallel" in line.columns else 1,
        "open_at_node": [open_at.get(idx) for idx in line.index],
        "name": line["name"].to_numpy() if "name" in line.columns else None,
        "std_type": line["std_type"].to_numpy() if "std_type" in line.columns else None,
    })
    for column in ("line_key", "role", "ring_id"):
        if column in line.columns:
            lines[column] = line[column].to_numpy()
    bb_open = bb_switch[~bb_switch["closed"].astype(bool)]
    extra = [{"line_id": f"switch {s}", "from_node": rep[b], "to_node": rep[e], "length_km": 0.0, "kind": "cable",
              "cable_km": 0.0, "overhead_km": 0.0, "normally_open": True, "parallel": 1, "open_at_node": rep[b],
              "name": f"open bus-bus switch {s}", "std_type": None}
             for s, b, e in zip(bb_open.index, bb_open["bus"], bb_open["element"]) if rep[b] != rep[e]]
    if extra:
        lines = pd.concat([lines, pd.DataFrame(extra)], ignore_index=True)
    lines.attrs["mixed_split_unknown"] = unknown_split
    return nodes, lines


def read_ding0_grid(folder: str | Path, epsg: int | None = _DING0_TARGET_EPSG) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read one Ding0 MV grid (PyPSA-style CSV export, e.g. Ding0 v0.3.0-alpha of 2023) into the generic tables.

    MV buses (``v_nom >= 1`` kV) become nodes, except the HV side of ``transformers_hvmv.csv`` and the virtual
    buses of the switches. ``busbar`` = MV side of ``transformers_hvmv.csv``; ``station`` = MV side of an MV/LV
    transformer in ``transformers.csv``; ``customer`` = an MV bus with loads; ``generator`` = an MV bus with
    generators; ``joint`` = the rest (branch tees). Each row of ``switches.csv`` is a NOP: the line named in
    ``branch`` ends at the virtual bus ``bus_open``, which is mapped to ``bus_closed`` (the bus whose panel holds
    the open switch, ``open_at_node``). Line ``kind`` ``line`` becomes ``overhead``; ``length`` is in km.

    Args:
        folder: Grid folder (containing ``topology/``) or the ``topology`` folder itself.
        epsg: Target CRS of ``x``, ``y`` (the CSVs are WGS84); None keeps longitude and latitude.

    Returns:
        ``(nodes, lines)`` in the generic format; ids are the Ding0 bus and line names. Nodes carry
        ``n_transformers`` and ``transformer_mva`` (busbar), ``sn_kva`` (station), ``p_load_kw`` and ``p_gen_kw``
        (MV loads and generators at the bus); lines carry ``std_type`` (``type_info``).
        ``lines.attrs["n_self_loops"]`` counts dropped self loops.

    Raises:
        FileNotFoundError: ``buses.csv`` or ``lines.csv`` is missing.
    """
    topo = Path(folder)
    if (topo / "topology").is_dir():
        topo = topo / "topology"
    for name in ("buses.csv", "lines.csv"):
        if not (topo / name).is_file():
            raise FileNotFoundError(f"{topo / name} not found")

    def read(name: str, columns: Sequence[str]) -> pd.DataFrame:
        path = topo / name
        if not path.is_file():
            return pd.DataFrame(columns=list(columns))
        df = pd.read_csv(path)
        for column in columns:
            if column not in df.columns:
                df[column] = np.nan
        return df

    buses = read("buses.csv", ["name", "v_nom", "x", "y"])
    lines_raw = read("lines.csv", ["name", "bus0", "bus1", "length", "kind", "num_parallel", "type_info"])
    switches = read("switches.csv", ["name", "bus_closed", "bus_open", "branch"])
    trafos = read("transformers.csv", ["bus0", "bus1", "s_nom"])
    hvmv = read("transformers_hvmv.csv", ["bus0", "bus1", "s_nom"])
    loads = read("loads.csv", ["bus", "p_set"])
    gens = read("generators.csv", ["bus", "p_nom"])

    mv = buses[(buses["v_nom"].astype(float) >= _MV_MIN_KV) & ~buses["name"].isin(set(hvmv["bus0"]))]
    alias = {o: c for o, c in zip(switches["bus_open"], switches["bus_closed"]) if isinstance(o, str)}
    nodes_raw = mv[~mv["name"].isin(alias)].reset_index(drop=True)
    names = set(nodes_raw["name"])
    busbar = set(hvmv["bus1"]) & names
    station = set(trafos["bus0"]) & names
    customer = set(loads["bus"]) & names
    generator = set(gens["bus"]) & names
    kinds = ["busbar" if n in busbar else "station" if n in station else "customer" if n in customer
             else "generator" if n in generator else "joint" for n in nodes_raw["name"]]
    x = nodes_raw["x"].to_numpy(dtype=float).copy()
    y = nodes_raw["y"].to_numpy(dtype=float).copy()
    transformer = _transformer(epsg)
    ok = np.isfinite(x) & np.isfinite(y)
    if transformer is not None and ok.any():
        x[ok], y[ok] = transformer.transform(x[ok], y[ok])
    n_hvmv = Counter(hvmv["bus1"])
    mva_hvmv = hvmv.groupby("bus1")["s_nom"].sum().to_dict() if len(hvmv) else {}
    kva = (trafos.groupby("bus0")["s_nom"].sum() * 1000.0).to_dict() if len(trafos) else {}
    p_load = (loads[loads["bus"].isin(names)].groupby("bus")["p_set"].sum() * 1000.0).to_dict() if len(loads) else {}
    p_gen = (gens[gens["bus"].isin(names)].groupby("bus")["p_nom"].sum() * 1000.0).to_dict() if len(gens) else {}
    nodes = pd.DataFrame({
        "node_id": nodes_raw["name"].to_numpy(),
        "kind": kinds,
        "x": x,
        "y": y,
        "vn_kv": nodes_raw["v_nom"].astype(float).to_numpy(),
        "n_transformers": [n_hvmv.get(n, 0) if k == "busbar" else np.nan for n, k in zip(nodes_raw["name"], kinds)],
        "transformer_mva": [float(mva_hvmv.get(n, np.nan)) if k == "busbar" else np.nan
                            for n, k in zip(nodes_raw["name"], kinds)],
        "sn_kva": [float(kva.get(n, np.nan)) if k == "station" else np.nan for n, k in zip(nodes_raw["name"], kinds)],
        "p_load_kw": [float(p_load.get(n, 0.0)) for n in nodes_raw["name"]],
        "p_gen_kw": [float(p_gen.get(n, 0.0)) for n in nodes_raw["name"]],
    })
    if "mv_grid_id" in buses.columns and len(buses):
        nodes.attrs["mv_grid_id"] = buses["mv_grid_id"].dropna().iloc[0] if buses["mv_grid_id"].notna().any() else None

    mv_names = set(mv["name"])
    raw = lines_raw[lines_raw["bus0"].isin(mv_names) & lines_raw["bus1"].isin(mv_names)].copy()
    raw["from_node"] = raw["bus0"].map(lambda b: alias.get(b, b))
    raw["to_node"] = raw["bus1"].map(lambda b: alias.get(b, b))
    open_at = {}
    for branch, closed_bus in zip(switches["branch"], switches["bus_closed"]):
        open_at.setdefault(branch, closed_bus)
    self_loop = raw["from_node"] == raw["to_node"]
    raw = raw[~self_loop]
    kind = np.where(raw["kind"].astype(str) == "line", "overhead", "cable")
    length = raw["length"].astype(float).to_numpy()
    lines = pd.DataFrame({
        "line_id": raw["name"].to_numpy(),
        "from_node": raw["from_node"].to_numpy(),
        "to_node": raw["to_node"].to_numpy(),
        "length_km": length,
        "kind": kind,
        "cable_km": np.where(kind == "cable", length, 0.0),
        "overhead_km": np.where(kind == "overhead", length, 0.0),
        "normally_open": raw["name"].isin(open_at).to_numpy(),
        "parallel": raw["num_parallel"].fillna(1).astype(float).round().astype(int).to_numpy(),
        "open_at_node": [open_at.get(n) for n in raw["name"]],
        "std_type": raw["type_info"].to_numpy(),
    })
    lines.attrs["n_self_loops"] = int(self_loop.sum())
    return nodes, lines
