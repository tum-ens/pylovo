"""Metrics of medium-voltage (MV) grids in one generic table format, literature ranges and readers.

The same metric code runs on the grids of the MV stage, on the Ding0 2023 CSV grids (``read_ding0_grid``), on
pandapower nets such as SimBench or ``pandapower.networks.mv_oberrhein()`` (``net_to_tables``) and on external
reference data converted to the generic format. The module is pure: it imports no database, configuration or
plotting module.

**Generic format.** Two data frames:

- ``nodes``: ``node_id``, ``kind`` (``busbar``, ``station``, ``customer``, ``generator`` or ``joint``), ``x``, ``y``
  (metres in a projected CRS, TARGET_EPSG for pylovo). Optional: ``area_class`` (``urban`` or ``rural``),
  ``n_transformers`` and ``transformer_mva`` (HV/MV units of a busbar), ``sn_kva`` (station rating).
- ``lines``: ``line_id``, ``from_node``, ``to_node``, ``length_km`` (circuit length, parallel cables count once),
  ``kind`` (``cable``, ``overhead`` or ``mixed``), ``cable_km``, ``overhead_km``, ``normally_open``, ``parallel``.
  Optional: ``open_at_node`` (the end of a normally-open line that holds the open switch) and ``role``
  (``ring``, ``stub``, ``dedicated``, ``generator`` or ``tie``; ``tie`` marks a tie to another substation).

``cable_km`` and ``overhead_km`` default to the line length by ``kind``; ``normally_open`` defaults to False and
``parallel`` to 1. ``normalise_tables`` checks the format and fills these defaults.

**Feeders, rings and stubs** (DESIGN section 7.2). Remove the busbars and every normally-open line (NOP line): the
connected components of the rest are the feeders; the closed lines between a busbar and a feeder are its outlets. A
NOP line counts with the feeder of its closed end (the end without the open switch; ``from_node`` when
``open_at_node`` is unknown). Two feeders of the same busbar joined by one NOP line form an open ring (``ring``);
larger groups of feeders joined by NOP lines are open-meshed (``mesh``). A feeder joined to its own busbar by a NOP
line is a two-leg feeder (``two_leg``: a 2-leg customer feeder, or a ring whose open section ends at the busbar,
as in some Ding0 grids); a feeder with two or more closed outlets is a closed ring
(``closed_ring``); a feeder without any of these is ``radial``; a component without an outlet is an ``island``
(unsupplied in normal operation). NOP lines between feeders of different busbars, and lines with role ``tie``, are
ties. The **main path** of a feeder runs from the busbar along the shortest closed path to every end of its NOP
lines (radial and closed-ring feeders also to their farthest node). Nodes off the main paths are stub nodes and
lines off the main paths are stub lines. The **ring members** of a ring are the stations, customers and generators
on the main paths of its feeders (the cap definition of the MV stage; generators on a main path are looped in);
joints and stub nodes do not count.

**Metric names** used by ``LITERATURE_RANGES`` and ``compare_to_literature``:

- ring level (columns of ``ring_table``): ``ring_members``, ``ring_km``;
- feeder level (columns of ``feeder_table``): ``feeder_supply_points``, ``feeder_km`` (half-ring length from the
  busbar to the NOP along the main path, without stubs);
- grid level (keys of ``topology_metrics`` and ``region_metrics``): ``feeders_per_substation``,
  ``supply_points_per_substation``, ``km_per_supply_point``, ``cable_share``, ``km_per_km2``,
  ``circuit_km_per_substation``, ``station_spacing_m``, ``customer_share``;
- grid level from other sources: ``substation_peak_mva``, ``hv_mv_unit_mva``, ``peak_to_station_rating``,
  ``max_line_loading_percent``;
- sets of grids: ``open_ring_grid_share``, ``open_meshed_share``, ``length_ratio_to_real``.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Hashable, Iterable, Mapping, Sequence

import networkx as nx
import numpy as np
import pandas as pd

__all__ = [
    "GENERIC_NODE_COLUMNS",
    "GENERIC_LINE_COLUMNS",
    "GENERIC_NODE_KINDS",
    "GENERIC_LINE_KINDS",
    "LITERATURE_RANGES",
    "LITERATURE_TOLERANCE",
    "LiteratureRange",
    "compare_to_literature",
    "earth_fault_current_a",
    "feeder_table",
    "literature_ranges",
    "net_to_tables",
    "node_roles",
    "normalise_tables",
    "read_ding0_grid",
    "region_metrics",
    "ring_membership_ari",
    "ring_table",
    "station_spacing_m",
    "topology_metrics",
    "traffic_light",
    "wasserstein_table",
]

#: Required columns of the generic node table.
GENERIC_NODE_COLUMNS: tuple[str, ...] = ("node_id", "kind", "x", "y")
#: Columns of the generic line table (``cable_km``, ``overhead_km``, ``normally_open`` and ``parallel`` have defaults).
GENERIC_LINE_COLUMNS: tuple[str, ...] = (
    "line_id", "from_node", "to_node", "length_km", "kind", "cable_km", "overhead_km", "normally_open", "parallel",
)
#: Node kinds of the generic format.
GENERIC_NODE_KINDS: tuple[str, ...] = ("busbar", "station", "customer", "generator", "joint")
#: Line kinds of the generic format.
GENERIC_LINE_KINDS: tuple[str, ...] = ("cable", "overhead", "mixed")

#: Node kinds that are supply points (SimBench and BMWi basis).
SUPPLY_POINT_KINDS: tuple[str, ...] = ("station", "customer")
#: Node kinds that hold switchgear and count as ring members on a main path.
MEMBER_KINDS: tuple[str, ...] = ("station", "customer", "generator")
#: Feeder kinds whose main-path supply points are (n-1) secure.
SECURE_FEEDER_KINDS: tuple[str, ...] = ("ring", "mesh", "two_leg", "closed_ring")
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
# Generic format
# ---------------------------------------------------------------------------------------------------------------


def normalise_tables(nodes: pd.DataFrame, lines: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Check the generic format and fill the optional columns.

    Args:
        nodes: Node table (``node_id`` and ``kind`` required; ``x`` and ``y`` default to NaN, or to the
            coordinates of a ``geometry`` column of points).
        lines: Line table (``from_node``, ``to_node`` and ``length_km`` required; ``line_id`` defaults to the
            index).

    Returns:
        Copies of both tables with every column of ``GENERIC_NODE_COLUMNS`` and ``GENERIC_LINE_COLUMNS``, plus
        ``open_at_node`` (None where unknown).

    Raises:
        ValueError: A required column is missing, a kind is unknown, a node id is duplicated, or a mixed line has
            no ``cable_km`` and ``overhead_km``.
    """
    for column in ("node_id", "kind"):
        if column not in nodes.columns:
            raise ValueError(f"nodes table needs a {column!r} column")
    nodes = pd.DataFrame(nodes).copy()
    if "x" not in nodes.columns or "y" not in nodes.columns:
        xy = _node_xy(nodes)
        nodes["x"], nodes["y"] = xy[:, 0], xy[:, 1]
    bad = sorted(set(nodes["kind"]) - set(GENERIC_NODE_KINDS), key=str)
    if bad:
        raise ValueError(f"unknown node kinds {bad}; expected {GENERIC_NODE_KINDS}")
    if nodes["node_id"].duplicated().any():
        dup = nodes.loc[nodes["node_id"].duplicated(), "node_id"].tolist()[:5]
        raise ValueError(f"duplicate node ids, e.g. {dup}")

    for column in ("from_node", "to_node", "length_km"):
        if column not in lines.columns:
            raise ValueError(f"lines table needs a {column!r} column")
    lines = pd.DataFrame(lines).copy()
    if "line_id" not in lines.columns:
        lines["line_id"] = lines.index
    lines["length_km"] = lines["length_km"].astype(float)
    if "kind" not in lines.columns:
        lines["kind"] = "cable"
    bad = sorted(set(lines["kind"]) - set(GENERIC_LINE_KINDS), key=str)
    if bad:
        raise ValueError(f"unknown line kinds {bad}; expected {GENERIC_LINE_KINDS}")
    has_split = "cable_km" in lines.columns and "overhead_km" in lines.columns
    if not has_split:
        if (lines["kind"] == "mixed").any():
            raise ValueError("mixed lines need cable_km and overhead_km")
        lines["cable_km"] = np.where(lines["kind"] == "cable", lines["length_km"], 0.0)
        lines["overhead_km"] = np.where(lines["kind"] == "overhead", lines["length_km"], 0.0)
    lines["cable_km"] = lines["cable_km"].astype(float)
    lines["overhead_km"] = lines["overhead_km"].astype(float)
    if "normally_open" not in lines.columns:
        lines["normally_open"] = False
    lines["normally_open"] = lines["normally_open"].fillna(False).astype(bool)
    if "parallel" not in lines.columns:
        lines["parallel"] = 1
    lines["parallel"] = lines["parallel"].fillna(1).astype(int)
    if "open_at_node" not in lines.columns:
        lines["open_at_node"] = None
    lines["open_at_node"] = lines["open_at_node"].astype(object).where(lines["open_at_node"].notna(), None)
    return nodes.reset_index(drop=True), lines.reset_index(drop=True)


# ---------------------------------------------------------------------------------------------------------------
# Topology analysis
# ---------------------------------------------------------------------------------------------------------------


def _key(value: Any) -> tuple:
    """Sort key for ids that may be numbers or strings."""
    if isinstance(value, (int, np.integer, float, np.floating)) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0.0, str(value))


def _opt_float(value: Any) -> float | None:
    """Float or None (None, NaN and non-numbers give None)."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) else out


@dataclass
class _Analysis:
    nodes: pd.DataFrame          # normalised nodes
    lines: pd.DataFrame          # normalised internal lines without self loops, with columns feeder, nop_class, on_main
    feeders: pd.DataFrame
    rings: pd.DataFrame
    roles: pd.DataFrame
    diagnostics: dict


def _majority(values: Iterable[Any]) -> str | None:
    counts = Counter(v for v in values if isinstance(v, str))
    if not counts:
        return None
    best = max(counts.values())
    return sorted(k for k, c in counts.items() if c == best)[0]


def _analyse(nodes: pd.DataFrame, lines: pd.DataFrame) -> _Analysis:
    """Detect feeders, rings, main paths and stubs (see the module docstring)."""
    nodes, lines = normalise_tables(nodes, lines)
    kind = dict(zip(nodes["node_id"], nodes["kind"]))
    area = dict(zip(nodes["node_id"], nodes["area_class"])) if "area_class" in nodes.columns else {}
    known = set(kind)
    internal = lines["from_node"].isin(known) & lines["to_node"].isin(known)
    self_loop = internal & (lines["from_node"] == lines["to_node"])
    external = lines[~internal]
    lines = lines[internal & ~self_loop].copy()
    busbars = sorted((n for n, k in kind.items() if k == "busbar"), key=_key)
    is_busbar = set(busbars)
    roles = lines["role"] if "role" in lines.columns else pd.Series(None, index=lines.index, dtype=object)

    # feeders: components of the closed lines without the busbars
    graph = nx.MultiGraph()
    graph.add_nodes_from(n for n in sorted(known, key=_key) if n not in is_busbar)
    outlet_rows: list[tuple[int, Hashable, Hashable]] = []          # (line index, busbar, feeder node)
    busbar_links: list[int] = []
    closed = lines[~lines["normally_open"]]
    for idx, u, v, length in zip(closed.index, closed["from_node"], closed["to_node"], closed["length_km"]):
        if u in is_busbar and v in is_busbar:
            busbar_links.append(idx)
        elif u in is_busbar or v in is_busbar:
            outlet_rows.append((idx, u if u in is_busbar else v, v if u in is_busbar else u))
        else:
            graph.add_edge(u, v, key=idx, length=float(length))
    components = [sorted(c, key=_key) for c in nx.connected_components(graph)]
    comp_of_node = {n: i for i, comp in enumerate(components) for n in comp}
    comp_busbars: dict[int, set] = defaultdict(set)
    comp_outlets: dict[int, list] = defaultdict(list)
    for idx, bb, node in outlet_rows:
        comp_busbars[comp_of_node[node]].add(bb)
        comp_outlets[comp_of_node[node]].append(idx)
    comp_busbar = {i: (sorted(comp_busbars[i], key=_key)[0] if comp_busbars[i] else None)
                   for i in range(len(components))}
    order = sorted(range(len(components)),
                   key=lambda i: (comp_busbar[i] is None, _key(comp_busbar[i]) if comp_busbar[i] is not None else (),
                                  _key(components[i][0])))
    feeder_of_comp = {c: f for f, c in enumerate(order)}
    feeder_of_node = {n: feeder_of_comp[c] for n, c in comp_of_node.items()}
    n_feeders = len(order)
    feeder_nodes = [components[c] for c in order]
    feeder_busbar = [comp_busbar[c] for c in order]
    feeder_busbars = [sorted(comp_busbars[c], key=_key) for c in order]
    feeder_outlets = [sorted(comp_outlets[c]) for c in order]

    # NOP lines
    lines["feeder"] = pd.Series([None] * len(lines), index=lines.index, dtype=object)
    lines["nop_class"] = pd.Series([None] * len(lines), index=lines.index, dtype=object)
    for idx, u, v in zip(closed.index, closed["from_node"], closed["to_node"]):
        if idx in busbar_links:
            continue
        node = v if u in is_busbar else u
        lines.at[idx, "feeder"] = feeder_of_node[node]
    ring_edges: Counter = Counter()
    feeder_targets: dict[int, set] = defaultdict(set)
    feeder_flags: dict[int, set] = defaultdict(set)
    nops = lines[lines["normally_open"]]
    for idx, u, v, open_at in zip(nops.index, nops["from_node"], nops["to_node"], nops["open_at_node"]):
        closed_end = v if (open_at is not None and open_at == u) else u
        side_u = ("b", u) if u in is_busbar else ("f", feeder_of_node[u])
        side_v = ("b", v) if v in is_busbar else ("f", feeder_of_node[v])
        assigned: int | None = None
        if roles.get(idx) == "tie":
            cls = "tie"
        elif side_u[0] == "f" and side_v[0] == "f":
            fu, fv = side_u[1], side_v[1]
            if fu == fv:
                cls, assigned = "loop", fu
            elif feeder_busbar[fu] == feeder_busbar[fv] and feeder_busbar[fu] is not None:
                cls, assigned = "ring", feeder_of_node[closed_end]
                ring_edges[(min(fu, fv), max(fu, fv))] += 1
            elif feeder_busbar[fu] is None or feeder_busbar[fv] is None:
                cls, assigned = "island_link", feeder_of_node[closed_end]
            else:
                cls = "tie"
        elif side_u[0] == "f" or side_v[0] == "f":
            f = side_u[1] if side_u[0] == "f" else side_v[1]
            bb = side_v[1] if side_u[0] == "f" else side_u[1]
            cls = "two_leg" if feeder_busbar[f] in (bb, None) else "tie"
            assigned = f if cls == "two_leg" else None
        else:
            cls = "tie"
        lines.at[idx, "nop_class"] = cls
        lines.at[idx, "feeder"] = assigned
        for end, side in ((u, side_u), (v, side_v)):
            if side[0] == "f":
                feeder_targets[side[1]].add(end)
                if cls == "tie":
                    feeder_flags[side[1]].add("tie")
                elif cls == "loop":
                    feeder_flags[side[1]].add("loop")
                elif cls == "two_leg":
                    feeder_flags[side[1]].add("two_leg")

    # ring groups: feeders of one busbar joined by NOP lines
    ring_graph = nx.Graph()
    ring_graph.add_nodes_from(range(n_feeders))
    ring_graph.add_edges_from(ring_edges)
    groups = sorted((sorted(c) for c in nx.connected_components(ring_graph) if len(c) >= 2), key=lambda g: g[0])
    group_of = {f: g for g, members in enumerate(groups) for f in members}
    group_kind = []
    for members in groups:
        n_edges = sum(c for (a, b), c in ring_edges.items() if a in members and b in members)
        group_kind.append("ring" if len(members) == 2 and n_edges == 1 else "mesh")

    feeder_kind = []
    for f in range(n_feeders):
        if feeder_busbar[f] is None:
            feeder_kind.append("island")
        elif f in group_of:
            feeder_kind.append(group_kind[group_of[f]])
        elif "two_leg" in feeder_flags[f]:
            feeder_kind.append("two_leg")
        elif len(feeder_outlets[f]) >= 2:
            feeder_kind.append("closed_ring")
        else:
            feeder_kind.append("radial")

    # main paths
    length = lines["length_km"].to_dict()
    main_nodes: list[set] = [set() for _ in range(n_feeders)]
    main_lines: list[set] = [set() for _ in range(n_feeders)]
    for f in range(n_feeders):
        if feeder_busbar[f] is None:
            continue
        sub = nx.MultiGraph()
        sub.add_nodes_from(feeder_nodes[f])
        sub.add_nodes_from(feeder_busbars[f])
        for idx in feeder_outlets[f]:
            sub.add_edge(lines.at[idx, "from_node"], lines.at[idx, "to_node"], key=idx, length=length[idx])
        for u, v, idx in graph.subgraph(feeder_nodes[f]).edges(keys=True):
            sub.add_edge(u, v, key=idx, length=length[idx])
        dist, paths = nx.multi_source_dijkstra(sub, set(feeder_busbars[f]), weight="length")
        targets = set(feeder_targets[f])
        if feeder_kind[f] in ("radial", "closed_ring") or not targets:
            reachable = [n for n in feeder_nodes[f] if n in dist]
            if reachable:                                   # farthest node, ties: lowest id
                farthest = max(dist[n] for n in reachable)
                targets.add(min((n for n in reachable if dist[n] == farthest), key=_key))
        if feeder_kind[f] == "closed_ring":
            for idx in feeder_outlets[f]:
                u, v = lines.at[idx, "from_node"], lines.at[idx, "to_node"]
                targets.add(v if u in is_busbar else u)
        for target in sorted(targets, key=_key):
            path = paths.get(target)
            if not path:
                continue
            main_nodes[f].update(n for n in path if n not in is_busbar)
            for a, b in zip(path[:-1], path[1:]):
                edges = sub.get_edge_data(a, b)
                main_lines[f].add(min(edges, key=lambda k: (edges[k]["length"], k)))
    lines["on_main"] = False
    for f in range(n_feeders):
        if main_lines[f]:
            lines.loc[sorted(main_lines[f]), "on_main"] = True

    # feeder rows
    feeder_rows = []
    by_feeder = {int(f): df for f, df in lines[lines["feeder"].notna()].groupby(
        lines.loc[lines["feeder"].notna(), "feeder"].astype(int))}
    no_lines = lines.iloc[0:0]
    for f in range(n_feeders):
        comp = feeder_nodes[f]
        kinds = Counter(kind[n] for n in comp)
        stub_nodes = [n for n in comp if n not in main_nodes[f] and kind[n] in MEMBER_KINDS]
        members = [n for n in main_nodes[f] if kind[n] in MEMBER_KINDS]
        own = by_feeder.get(f, no_lines)
        own_closed = own[~own["normally_open"]]
        own_nop = own[own["normally_open"]]
        main_km = float(own_closed.loc[own_closed["on_main"], "length_km"].sum())
        nop_km = float(own_nop.loc[own_nop["nop_class"].isin(["ring", "loop", "island_link"]), "length_km"].sum())
        stub_km = float(own_closed.loc[~own_closed["on_main"], "length_km"].sum())
        busbar_nops = own_nop[own_nop["nop_class"] == "two_leg"]
        feeder_rows.append({
            "feeder": f,
            "busbar": feeder_busbar[f],
            "kind": feeder_kind[f],
            "ring": None,                                   # filled below
            "n_outlets": len(feeder_outlets[f]),
            "n_bays": len(feeder_outlets[f]) + len(busbar_nops),
            "n_nodes": len(comp),
            "n_stations": kinds.get("station", 0),
            "n_customers": kinds.get("customer", 0),
            "n_generators": kinds.get("generator", 0),
            "n_joints": kinds.get("joint", 0),
            "feeder_supply_points": kinds.get("station", 0) + kinds.get("customer", 0),
            "n_members": len(members),
            "n_stub_nodes": len(stub_nodes),
            "n_stub_supply_points": sum(kind[n] in SUPPLY_POINT_KINDS for n in stub_nodes),
            "n_main_supply_points": sum(kind[n] in SUPPLY_POINT_KINDS for n in members),
            "feeder_km": main_km + nop_km,
            "stub_km": stub_km,
            "feeder_km_incl_stubs": main_km + nop_km + stub_km,
            "length_km": float(own["length_km"].sum()),
            "cable_km": float(own["cable_km"].sum()),
            "overhead_km": float(own["overhead_km"].sum()),
            "two_leg_km": float(busbar_nops["length_km"].sum()),
            "n_nops": int(len(own_nop)),
            "has_tie": "tie" in feeder_flags[f],
            "has_loop": "loop" in feeder_flags[f],
            "multi_busbar": len(feeder_busbars[f]) > 1,
            "area_class": _majority(area.get(n) for n in comp if kind[n] in SUPPLY_POINT_KINDS) if area else None,
        })
    feeders = pd.DataFrame(feeder_rows, columns=_FEEDER_COLUMNS)
    feeders["busbar"] = pd.Series(feeder_busbar, index=feeders.index, dtype=object)   # keep the id type

    # ring rows: one per ring group, one per other feeder
    ring_rows = []
    units: list[list[int]] = []
    seen: set[int] = set()
    for f in range(n_feeders):
        if f in seen:
            continue
        unit = groups[group_of[f]] if f in group_of else [f]
        seen.update(unit)
        units.append(unit)
    for r, unit in enumerate(units):
        part = feeders.loc[unit]
        feeders.loc[unit, "ring"] = r
        kind_r = group_kind[group_of[unit[0]]] if unit[0] in group_of else feeder_kind[unit[0]]
        n_members = int(part["n_members"].sum())
        nodes_r = [n for f in unit for n in feeder_nodes[f]]
        ring_rows.append({
            "ring": r,
            "busbar": feeder_busbar[unit[0]],
            "kind": kind_r,
            "feeders": tuple(unit),
            "n_feeders": len(unit),
            "ring_members": n_members,
            "ring_members_incl_stubs": n_members + int(part["n_stub_nodes"].sum()),
            "n_stations": int(part["n_stations"].sum()),
            "n_customers": int(part["n_customers"].sum()),
            "n_generators": int(part["n_generators"].sum()),
            "n_stub_nodes": int(part["n_stub_nodes"].sum()),
            "supply_points": int(part["feeder_supply_points"].sum()),
            "ring_km": float(part["feeder_km"].sum() + part["two_leg_km"].sum()),
            "ring_km_incl_stubs": float(part["feeder_km_incl_stubs"].sum() + part["two_leg_km"].sum()),
            "stub_km": float(part["stub_km"].sum()),
            "cable_km": float(part["cable_km"].sum()),
            "overhead_km": float(part["overhead_km"].sum()),
            "n_nops": int(part["n_nops"].sum()),
            "has_tie": bool(part["has_tie"].any()),
            "area_class": _majority(area.get(n) for n in nodes_r if kind[n] in SUPPLY_POINT_KINDS) if area else None,
        })
    rings = pd.DataFrame(ring_rows, columns=_RING_COLUMNS)
    rings["busbar"] = pd.Series([feeder_busbar[u[0]] for u in units], index=rings.index, dtype=object)
    feeders["ring"] = feeders["ring"].astype("Int64")

    # node roles
    role_rows = []
    for n in nodes["node_id"]:
        if n in is_busbar:
            role_rows.append((n, kind[n], None, None, "busbar"))
            continue
        f = feeder_of_node[n]
        if feeder_busbar[f] is None:
            position = "unsupplied"
        else:
            position = "main" if n in main_nodes[f] else "stub"
        role_rows.append((n, kind[n], f, int(feeders.at[f, "ring"]), position))
    roles_df = pd.DataFrame(role_rows, columns=["node_id", "kind", "feeder", "ring", "position"])
    roles_df["feeder"] = roles_df["feeder"].astype("Int64")
    roles_df["ring"] = roles_df["ring"].astype("Int64")

    tie_lines = lines[lines["nop_class"] == "tie"]
    external_ties = external[external["normally_open"]] if len(external) else external
    diagnostics = {
        "n_self_loops": int(self_loop.sum()),
        "n_external_lines": int(len(external)),
        "n_busbar_links": len(busbar_links),
        "n_ties": int(len(tie_lines) + len(external_ties)),
        "tie_km": float(tie_lines["length_km"].sum() + (external_ties["length_km"].sum() if len(external) else 0.0)),
    }
    return _Analysis(nodes, lines, feeders, rings, roles_df, diagnostics)


_FEEDER_COLUMNS = [
    "feeder", "busbar", "kind", "ring", "n_outlets", "n_bays", "n_nodes", "n_stations", "n_customers",
    "n_generators", "n_joints", "feeder_supply_points", "n_members", "n_stub_nodes", "n_stub_supply_points",
    "n_main_supply_points", "feeder_km", "stub_km", "feeder_km_incl_stubs", "length_km", "cable_km", "overhead_km",
    "two_leg_km", "n_nops", "has_tie", "has_loop", "multi_busbar", "area_class",
]
_RING_COLUMNS = [
    "ring", "busbar", "kind", "feeders", "n_feeders", "ring_members", "ring_members_incl_stubs", "n_stations",
    "n_customers", "n_generators", "n_stub_nodes", "supply_points", "ring_km", "ring_km_incl_stubs", "stub_km",
    "cable_km", "overhead_km", "n_nops", "has_tie", "area_class",
]


def feeder_table(nodes: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """One row per feeder (component of the closed lines without the busbars), with its ring and stubs.

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        Columns ``feeder``, ``busbar``, ``kind`` (``ring``, ``mesh``, ``two_leg``, ``closed_ring``, ``radial`` or
        ``island``), ``ring`` (row of ``ring_table``), ``n_outlets`` (closed busbar lines), ``n_bays`` (busbar
        lines incl. the open leg of a two-leg feeder), node counts by kind, ``feeder_supply_points`` (stations and
        customers incl. stubs), ``n_members`` (stations, customers and generators on the main path),
        ``n_stub_nodes``, ``n_stub_supply_points``, ``n_main_supply_points``, ``feeder_km`` (busbar to the NOP
        along the main path, incl. the NOP line of its closed end), ``stub_km``, ``feeder_km_incl_stubs``,
        ``length_km``, ``cable_km`` and ``overhead_km`` (every line counted with the feeder), ``two_leg_km``
        (open leg of a two-leg feeder), ``n_nops``, ``has_tie``, ``has_loop``, ``multi_busbar`` and
        ``area_class`` (majority of its supply points, None without an ``area_class`` node column).
    """
    return _analyse(nodes, lines).feeders


def ring_table(nodes: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """One row per ring (two feeders joined by a NOP line), open mesh, or other feeder (like ``mv_ring``).

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        Columns ``ring``, ``busbar``, ``kind`` (``ring``, ``mesh``, ``two_leg``, ``closed_ring``, ``radial`` or
        ``island``), ``feeders``, ``n_feeders``, ``ring_members`` (stations, customers and generators on the main
        paths: the cap definition), ``ring_members_incl_stubs``, node counts, ``supply_points``, ``ring_km``
        (closed loop incl. both busbar sections, without stubs; a two-leg feeder counts both legs),
        ``ring_km_incl_stubs``, ``stub_km``, ``cable_km``, ``overhead_km``, ``n_nops``, ``has_tie`` and
        ``area_class``.
    """
    return _analyse(nodes, lines).rings


def node_roles(nodes: pd.DataFrame, lines: pd.DataFrame) -> pd.DataFrame:
    """Feeder, ring and position of every node.

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        Columns ``node_id``, ``kind``, ``feeder``, ``ring`` (row of ``ring_table``) and ``position``
        (``busbar``, ``main``, ``stub`` or ``unsupplied``).
    """
    return _analyse(nodes, lines).roles


def _stats(prefix: str, values: Sequence[float] | pd.Series) -> dict:
    arr = np.asarray(pd.Series(values, dtype=float).dropna(), dtype=float)
    if arr.size == 0:
        return {f"{prefix}_mean": None, f"{prefix}_median": None, f"{prefix}_min": None, f"{prefix}_max": None}
    return {f"{prefix}_mean": float(arr.mean()), f"{prefix}_median": float(np.median(arr)),
            f"{prefix}_min": float(arr.min()), f"{prefix}_max": float(arr.max())}


def _ratio(num: float, den: float) -> float | None:
    return float(num) / float(den) if den else None


def _clean(value: Any) -> Any:
    """JSON-safe Python scalars (NaN and infinity become None)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    return value


def _node_xy(nodes: pd.DataFrame) -> np.ndarray:
    """(n, 2) coordinates from ``x``, ``y`` or a point ``geometry`` column (NaN where unknown)."""
    if "x" in nodes.columns and "y" in nodes.columns:
        return nodes[["x", "y"]].to_numpy(dtype=float)
    if "geometry" in nodes.columns:
        return np.array([(g.x, g.y) if g is not None and not g.is_empty else (np.nan, np.nan)
                         for g in nodes["geometry"]], dtype=float).reshape(-1, 2)
    return np.full((len(nodes), 2), np.nan)


def station_spacing_m(nodes: pd.DataFrame) -> float | None:
    """Median nearest-neighbour distance between stations (m), None with fewer than two located stations.

    Args:
        nodes: Generic node table with ``x`` and ``y`` in metres.

    Returns:
        The median distance from each station to its nearest other station, or None.
    """
    from scipy.spatial import cKDTree

    xy = _node_xy(nodes[nodes["kind"] == "station"])
    xy = xy[np.isfinite(xy).all(axis=1)]
    if len(xy) < 2:
        return None
    dist, _ = cKDTree(xy).query(xy, k=2)
    return float(np.median(dist[:, 1]))


def topology_metrics(nodes: pd.DataFrame, lines: pd.DataFrame) -> dict:
    """Structural metrics of one MV grid (or of several grids together) in the generic format.

    Per-substation values divide by the number of busbars. Ring statistics use rings and open meshes (not
    ``two_leg`` feeders), feeder statistics the supplied feeders with at least one supply point. Ties (NOP lines
    between feeders of different busbars, lines with role ``tie`` and lines to nodes outside the table) are
    excluded from ``circuit_km`` and reported as ``n_ties`` and ``tie_km``.

    Args:
        nodes: Generic node table.
        lines: Generic line table.

    Returns:
        A flat, JSON-safe dict (NaN becomes None): counts by node kind, ``n_hv_mv_transformers`` (with an
        ``n_transformers`` node column), line and NOP counts, feeder and ring counts by kind, ``n_feeder_bays``,
        ``feeders_per_substation``, ``circuit_km``, ``cable_km``, ``overhead_km``, ``cable_share``,
        ``circuit_km_per_substation``, ``supply_points_per_substation``, ``km_per_supply_point``,
        ``km_per_station``, statistics (``_mean``, ``_median``, ``_min``, ``_max``) of ``ring_members``,
        ``ring_members_incl_stubs``, ``ring_km``, ``feeder_km``, ``feeder_km_incl_stubs`` and
        ``feeder_supply_points``, ``mean_section_km`` (a ring of k members has k + 1 sections), stub and
        (n-1) shares of the supply points, ``station_spacing_m``, ``customer_share``, ``area_class`` and
        diagnostics.
    """
    a = _analyse(nodes, lines)
    nodes_n, internal = a.nodes, a.lines
    kinds = Counter(nodes_n["kind"])
    n_busbars = kinds.get("busbar", 0)
    n_supply = kinds.get("station", 0) + kinds.get("customer", 0)
    circuits = internal[internal["nop_class"] != "tie"]
    circuit_km = float(circuits["length_km"].sum())
    cable_km = float(circuits["cable_km"].sum())
    overhead_km = float(circuits["overhead_km"].sum())
    feeders, rings = a.feeders, a.rings
    supplied = feeders[feeders["busbar"].notna()]
    with_supply = supplied[supplied["feeder_supply_points"] > 0]
    true_rings = rings[rings["kind"].isin(["ring", "mesh"])]
    secure = supplied[supplied["kind"].isin(SECURE_FEEDER_KINDS)]
    n_secure = int(secure["n_main_supply_points"].sum())
    n_unsupplied = int(feeders.loc[feeders["busbar"].isna(), "feeder_supply_points"].sum())
    n_stub_side = n_supply - n_secure - n_unsupplied
    n_nops = int((internal["normally_open"] & (internal["nop_class"] != "tie")).sum())
    plain_rings = rings[rings["kind"] == "ring"]
    sections = float((plain_rings["ring_members"] + 1).sum())
    out: dict[str, Any] = {
        "n_busbars": n_busbars,
        "n_stations": kinds.get("station", 0),
        "n_customers": kinds.get("customer", 0),
        "n_generators": kinds.get("generator", 0),
        "n_joints": kinds.get("joint", 0),
        "n_supply_points": n_supply,
        "n_hv_mv_transformers": (int(pd.to_numeric(nodes_n.loc[nodes_n["kind"] == "busbar", "n_transformers"],
                                                   errors="coerce").fillna(0).sum())
                                 if "n_transformers" in nodes_n.columns else None),
        "n_lines": int(len(circuits)),
        "n_nops": n_nops,
        "n_feeders": int(len(supplied)),
        "n_feeder_bays": int(supplied["n_bays"].sum()),
        "feeders_per_substation": _ratio(supplied["n_bays"].sum(), n_busbars),
        "n_rings": int((rings["kind"] == "ring").sum()),
        "n_meshes": int((rings["kind"] == "mesh").sum()),
        "n_two_leg": int((rings["kind"] == "two_leg").sum()),
        "n_closed_rings": int((rings["kind"] == "closed_ring").sum()),
        "n_radial": int((rings["kind"] == "radial").sum()),
        "n_islands": int((rings["kind"] == "island").sum()),
        "circuit_km": circuit_km,
        "cable_km": cable_km,
        "overhead_km": overhead_km,
        "cable_share": _ratio(cable_km, circuit_km),
        "circuit_km_per_substation": _ratio(circuit_km, n_busbars),
        "supply_points_per_substation": _ratio(n_supply, n_busbars),
        "km_per_supply_point": _ratio(circuit_km, n_supply),
        "km_per_station": _ratio(circuit_km, kinds.get("station", 0)),
        **_stats("ring_members", true_rings["ring_members"]),
        **_stats("ring_members_incl_stubs", true_rings["ring_members_incl_stubs"]),
        **_stats("ring_km", true_rings["ring_km"]),
        **_stats("feeder_km", with_supply["feeder_km"]),
        **_stats("feeder_km_incl_stubs", with_supply["feeder_km_incl_stubs"]),
        **_stats("feeder_supply_points", with_supply["feeder_supply_points"]),
        "mean_section_km": _ratio(plain_rings["ring_km"].sum(), sections),
        "n_stub_nodes": int(feeders["n_stub_nodes"].sum()),
        "stub_km": float(feeders["stub_km"].sum()),
        "n1_secure_share": _ratio(n_secure, n_supply),
        "stub_share": _ratio(n_stub_side, n_supply),
        "unsupplied_share": _ratio(n_unsupplied, n_supply),
        "station_spacing_m": station_spacing_m(nodes_n),
        "customer_share": _ratio(kinds.get("customer", 0), n_supply),
        "area_class": (_majority(nodes_n.loc[nodes_n["kind"].isin(SUPPLY_POINT_KINDS), "area_class"])
                       if "area_class" in nodes_n.columns else None),
        **a.diagnostics,
    }
    return _clean(out)


# ---------------------------------------------------------------------------------------------------------------
# Region metrics (clipped circuit length per area)
# ---------------------------------------------------------------------------------------------------------------


def region_metrics(lines_gdf: Any, region_geom: Any, nodes: pd.DataFrame | None = None) -> dict:
    """Circuit length per area of a region, with the line geometries clipped to the region (section 23c style).

    Each line contributes ``length_km`` times the share of its geometry inside the region, so detour factors of
    straight spans and the cable/overhead split of mixed lines are kept. Ties and normally-open lines count: the
    published circuit length contains every circuit.

    Args:
        lines_gdf: GeoDataFrame of lines in a projected CRS (metres); ``length_km`` (default: geometry length),
            ``cable_km`` and ``overhead_km`` or ``kind`` (default: cable) are used when present.
        region_geom: Region polygon (shapely geometry in the CRS of ``lines_gdf``, or a GeoSeries or GeoDataFrame,
            reprojected to the CRS of ``lines_gdf`` and dissolved).
        nodes: Optional generic node table; its stations and customers inside the region give the supply points.

    Returns:
        JSON-safe dict: ``region_area_km2``, ``circuit_km_in_region``, ``cable_km_in_region``,
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
    area_km2 = float(region.area) / 1e6
    circuit = float(np.sum(length_km * share))
    out: dict[str, Any] = {
        "region_area_km2": area_km2,
        "circuit_km_in_region": circuit,
        "cable_km_in_region": float(np.sum(cable * share)),
        "overhead_km_in_region": float(np.sum(overhead * share)),
        "km_per_km2": _ratio(circuit, area_km2),
        "cable_share": _ratio(float(np.sum(cable * share)), circuit),
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
