"""Compare the LV grids of two datasets that supply the same buildings, e.g. pylovo grids and the grids of a DSO.

A *reference* dataset (in a validation: the grids of the distribution system operator, DSO) and a *candidate* dataset
(the pylovo grids under test) are compared as distributions over their grids: every grid contributes one value per
metric, and the grids are not paired. The metrics and the scores follow the structural validation of pylovo against
DSO grids; :mod:`pylovo.plotting.comparison` draws the figures.

- :data:`METRICS`: eight metrics as a matrix of two categories (network topology, electrical sizing) and four rows
  (station, feeders, per building, route).
- :func:`grid_metrics`: the metrics of every grid from its cables and its station. The topology is rebuilt from the
  cable geometries, so cables and stations exported from a DSO's GIS work the same way as pylovo grids.
- :func:`compare`, :func:`scorecard`, :func:`score_details`, :func:`ranking`: medians, typical difference,
  Wasserstein-1 distance, sampling noise and permutation test per metric.
- :func:`area_totals`, :func:`cable_table`, :func:`cable_summary`: totals over all grids of a dataset and its cables.

The notebook ``notebook_tutorials/analysis/basic_comparison.ipynb`` shows the workflow.
"""
from __future__ import annotations

import math
import zlib
from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Mapping, Optional, Sequence

import networkx as nx
import numpy as np
import pandas as pd
from scipy.stats import wasserstein_distance

__all__ = ["ALPHA", "BY_KEY", "CATEGORIES", "METRICS", "REFERENCE_CABLES", "ROWS", "Metric", "area_totals",
           "cable_resistances", "cable_summary", "cable_table", "compare", "equivalent_cross_section", "grid_metrics",
           "metric_catalogue", "metric_values", "ranking", "reference_rating_a", "score", "score_details", "scorecard"]


# --------------------------------------------------------------------------- metrics
@dataclass(frozen=True)
class Metric:
    """One compared metric: column of :func:`grid_metrics`, display name, unit and one-line meaning."""
    key: str
    name: str
    unit: str
    meaning: str
    category: str      # matrix column
    row: str           # matrix row
    factor: float = 1.0     # display value = stored value * factor
    decimals: int = 0


#: Matrix columns: category and the question it answers.
CATEGORIES = {
    "Network topology": "How large, how branched and how long is the network?",
    "Electrical sizing": "How much capacity and resistance does it have?",
}
#: Matrix rows in display order: the part of the grid a pair of metrics describes.
ROWS = ("Station", "Feeders", "Per building", "Route")

METRICS = (
    Metric("building_count", "Buildings per grid", "buildings",
           "Size of a transformer supply area", "Network topology", "Station"),
    Metric("feeder_branching_endpoints", "Feeder branch ends", "count",
           "Ends of the feeder network without house connection cables; more = more branched",
           "Network topology", "Feeders"),
    Metric("cable_length_per_building_m", "Cable length per building", "m",
           "Installed cable to the houses, parallel cables counted", "Network topology", "Per building"),
    Metric("max_supply_path_m", "Longest route", "m",
           "Cable distance from the station to the farthest house", "Network topology", "Route"),
    Metric("transformer_bank_capacity_kva", "Transformer size", "kVA",
           "Rated capacity of the station", "Electrical sizing", "Station"),
    Metric("feeder_capacity_a", "Feeder capacity", "A per feeder",
           "Current rating of a cable leaving the station; parallel cables add up",
           "Electrical sizing", "Feeders"),
    Metric("transformer_capacity_per_building_kva", "Transformer capacity per building", "kVA",
           "Station capacity divided by its buildings", "Electrical sizing", "Per building", decimals=1),
    Metric("max_series_resistance_ohm", "Highest route resistance", "mΩ",
           "Cable resistance from the station to the worst-supplied house", "Electrical sizing", "Route",
           factor=1000.),
)
BY_KEY = {m.key: m for m in METRICS}

#: Aluminium reference cables (NAYY, laid in ground): cross-section (mm²), resistance (ohm/km), rated current (A).
#: Every cable is rated through its resistance on this one basis, so neither a dataset's own rating table nor copper
#: versus aluminium decides a comparison of feeder capacities.
REFERENCE_CABLES = ((50, .642, 142), (70, .443, 173), (95, .320, 215), (120, .225, 242),
                    (150, .208, 270), (185, .164, 313), (240, .127, 357), (300, .100, 425))
#: Links from the station this short (m) belong to the station; the feeders start where they end.
STATION_LINK_M = 2.0
#: Cable ends closer than this (m) are one node.
NODE_PRECISION_M = 0.01


def metric_catalogue(metrics: Sequence[Metric] = METRICS) -> pd.DataFrame:
    """The metrics as a table: category, row, name, unit and meaning."""
    return pd.DataFrame([{"category": m.category, "row": m.row, "metric": m.name, "unit": m.unit,
                          "meaning": m.meaning} for m in metrics]).set_index(["category", "row"])


def cable_resistances(parameters: Mapping) -> dict[str, float]:
    """``{cable type: resistance in ohm/km}`` from the cable catalogue in the generation parameters of a version."""
    return {row["name"]: row["r_mohm_per_km"] / 1000 for row in parameters.get("equipment_data", [])
            if row.get("typ") == "Cable" and row.get("r_mohm_per_km") is not None}


def reference_rating_a(r_ohm_per_km: Optional[float]) -> Optional[float]:
    """Rated current (A) of the aluminium reference cable with the same resistance per km.

    Log-log interpolation in :data:`REFERENCE_CABLES` by resistance, continued with the end slopes beyond it.
    """
    if r_ohm_per_km is None or r_ohm_per_km <= 0:
        return None
    table = sorted(REFERENCE_CABLES, key=lambda row: row[1])
    xs = [math.log(r) for _, r, _ in table]
    ys = [math.log(i) for _, _, i in table]
    x = math.log(r_ohm_per_km)
    if x < xs[0]:
        y = ys[0] + (ys[1] - ys[0]) / (xs[1] - xs[0]) * (x - xs[0])
    elif x > xs[-1]:
        y = ys[-1] + (ys[-1] - ys[-2]) / (xs[-1] - xs[-2]) * (x - xs[-1])
    else:
        y = float(np.interp(x, xs, ys))
    return math.exp(y)


def equivalent_cross_section(r_ohm_per_km: Optional[float]) -> Optional[int]:
    """Cross-section (mm²) of the aluminium reference cable closest in resistance (on a log scale)."""
    if r_ohm_per_km is None or r_ohm_per_km <= 0:
        return None
    return min(REFERENCE_CABLES, key=lambda row: abs(math.log(row[1] / r_ohm_per_km)))[0]


def _node(point) -> tuple[int, int]:
    return round(point[0] / NODE_PRECISION_M), round(point[1] / NODE_PRECISION_M)


def _grid(lines: pd.DataFrame, station, capacity_kva: float, resistance: Mapping[str, float]) -> dict:
    """The metrics of one grid; see :func:`grid_metrics`."""
    # node pair -> cables between them: (length km, resistance ohm/km, parallel circuits)
    bundles = defaultdict(list)
    for geometry, length, std_type, parallel in zip(lines.geometry, lines["length_km"], lines["std_type"],
                                                    lines["parallel"]):
        a, b = _node(geometry.coords[0]), _node(geometry.coords[-1])
        if a != b:       # a link drawn without length (the 1 m station stubs of pylovo) joins its two ends
            bundles[min(a, b), max(a, b)].append((float(length), resistance.get(std_type), int(parallel)))
    graph = nx.Graph(list(bundles))
    target = _node(station.coords[0])
    root = min(graph, key=lambda n: (n[0] - target[0]) ** 2 + (n[1] - target[1]) ** 2)
    # breadth-first from the station: a spanning tree if the cables form loops
    parent = {node: up for up, node in nx.bfs_edges(graph, root)}
    order = [root, *parent]
    children = defaultdict(list)
    for node, up in parent.items():
        children[up].append(node)

    def section(node):
        return bundles[min(node, parent[node]), max(node, parent[node])]

    distance, ohm_to = {root: 0.0}, {root: 0.0}
    for node in order[1:]:
        cables = section(node)
        distance[node] = distance[parent[node]] + 1000 * min(length for length, _, _ in cables)
        ohm = [None if r is None else r * length / p for length, r, p in cables]
        if ohm_to[parent[node]] is None or None in ohm:
            ohm_to[node] = None
        else:
            ohm_to[node] = ohm_to[parent[node]] + (0.0 if 0.0 in ohm else 1 / sum(1 / z for z in ohm))
    # every cable end without a further cable is a house connection
    houses = [n for n in order[1:] if graph.degree(n) == 1]
    behind = dict.fromkeys(order, 0) | dict.fromkeys(houses, 1)
    for node in reversed(order[1:]):
        behind[parent[node]] += behind[node]
    # the shared network carries at least two houses; its ends are the branch ends
    shared = [n for n in order if behind[n] >= 2]
    continued = {parent[n] for n in shared if n != root}
    station_nodes, stack = {root}, [root]
    while stack:
        node = stack.pop()
        for child in children[node]:
            if distance[child] - distance[node] <= STATION_LINK_M:
                station_nodes.add(child)
                stack.append(child)
    feeders = [c for s in station_nodes for c in children[s] if c not in station_nodes and behind[c] >= 2]
    ratings = [[None if r is None else reference_rating_a(r) * p for _, r, p in section(c)] for c in feeders]
    head = None if any(None in r for r in ratings) else sum(sum(r) for r in ratings)
    buildings = len(houses)
    cable_km = float((lines["length_km"] * lines["parallel"]).sum())
    resistances = [ohm_to[n] for n in houses]
    return {
        "building_count": buildings,
        "feeder_branching_endpoints": sum(n != root and n not in continued for n in shared),
        "cable_length_per_building_m": 1000 * cable_km / buildings if buildings else None,
        "max_supply_path_m": max(distance[n] for n in houses) if houses else None,
        "transformer_bank_capacity_kva": capacity_kva,
        "feeder_capacity_a": head / len(feeders) if head is not None and feeders else None,
        "transformer_capacity_per_building_kva": capacity_kva / buildings if buildings else None,
        "max_series_resistance_ohm": None if not houses or None in resistances else max(resistances),
        # the sums behind the area totals
        "feeders": len(feeders),
        "cable_km": cable_km,
        "route_km": sum(distance[n] - distance[parent[n]] for n in order[1:]) / 1000,
        "feeder_head_capacity_a": head,
        "buildings_behind_feeders": sum(behind[c] for c in feeders),
    }


def grid_metrics(lines: pd.DataFrame, stations: pd.DataFrame, resistance: Mapping[str, float]) -> pd.DataFrame:
    """The metrics of every grid, one row per grid.

    The topology is rebuilt from the geometries: cables that meet share an end point (within
    :data:`NODE_PRECISION_M`), the network of a grid is followed from the cable end next to its station, and every
    cable end without a further cable is a house connection. Besides the :data:`METRICS`, each row holds the sums
    that :func:`area_totals` adds up.

    Args:
        lines: One row per cable section: the grid (``kcid``, ``bcid``), ``std_type``, ``length_km``, ``parallel``
            (circuits laid side by side) and a line geometry from one end to the other, e.g. the ``lines`` layer of
            :class:`pylovo.examples.TutorialData`.
        stations: One row per grid: ``kcid``, ``bcid``, ``transformer_rated_power`` (kVA) and the station as a point,
            e.g. the ``grids`` layer.
        resistance: ``{std_type: resistance in ohm/km}``, e.g. :func:`cable_resistances`. Cables of other types make
            the resistance and the feeder capacity of their grid unavailable.
    """
    stations = stations.set_index(["kcid", "bcid"])
    rows = []
    for (kcid, bcid), grid_lines in lines.groupby(["kcid", "bcid"], sort=True):
        station = stations.loc[(kcid, bcid)]
        rows.append({"kcid": int(kcid), "bcid": int(bcid),
                     **_grid(grid_lines, station["geometry"], float(station["transformer_rated_power"]), resistance)})
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- scores
#: Significance level of "the candidate differs from the reference".
ALPHA = 0.05
#: Resamples of the sampling-noise floor and label permutations of the test.
FLOOR_RESAMPLES = 500
PERMUTATIONS = 1000


def metric_values(frame: pd.DataFrame, metric: Metric) -> np.ndarray:
    """The finite values of ``metric`` over the grids of ``frame``, as stored (without the display factor)."""
    result = pd.to_numeric(frame[metric.key], errors="coerce").to_numpy(dtype=float)
    return result[np.isfinite(result)]


def _scale(reference: np.ndarray) -> float:
    """Interquartile range of the reference; its standard deviation, then 1, when that is nearly zero."""
    iqr = float(np.percentile(reference, 75) - np.percentile(reference, 25))
    if iqr > 1e-6:
        return iqr
    deviation = float(np.std(reference))
    return deviation if deviation > 1e-6 else 1.0


def score(reference: np.ndarray, candidate: np.ndarray, seed: int = 0) -> dict:
    """Medians, typical difference, Wasserstein-1 distance, sampling-noise floor and permutation test of one metric.

    - ``shift``: the Hodges–Lehmann shift, the median of all differences candidate grid minus reference grid.
    - ``wasserstein``: the Wasserstein-1 distance W1 in the unit of the metric, the area between the two empirical
      distribution functions; ``normalized``: W1 over the interquartile range of the reference (W̃1).
    - ``floor_p50``, ``floor_p90``: W̃1 between resamples of the reference alone, which even a perfect generator
      reaches with these sample sizes.
    - ``p``: share of label permutations whose W1 is at least the observed one; ``verdict`` "same" when
      ``p >= ALPHA``, else "lower", "higher" or "differs" (no typical difference).
    """
    result = dict(reference_n=len(reference), candidate_n=len(candidate), reference_median=None,
                  candidate_median=None, shift=None, relative_shift=None, wasserstein=None, normalized=None,
                  floor_p50=None, floor_p90=None, p=None, verdict=None)
    if not len(reference) or not len(candidate):
        return result
    rng = np.random.default_rng(seed)
    scale = _scale(reference)
    distance = float(wasserstein_distance(reference, candidate))
    shift = float(np.median(candidate[:, None] - reference[None, :]))
    reference_median = float(np.median(reference))
    floor = [wasserstein_distance(rng.choice(reference, len(reference)), rng.choice(reference, len(candidate)))
             / scale for _ in range(FLOOR_RESAMPLES)]
    pooled, exceed = np.concatenate((reference, candidate)), 0
    for _ in range(PERMUTATIONS):
        rng.shuffle(pooled)
        exceed += wasserstein_distance(pooled[:len(reference)], pooled[len(reference):]) >= distance - 1e-12
    p = (exceed + 1) / (PERMUTATIONS + 1)
    result.update(reference_median=reference_median, candidate_median=float(np.median(candidate)), shift=shift,
                  relative_shift=shift / reference_median if reference_median else None, wasserstein=distance,
                  normalized=distance / scale, floor_p50=float(np.percentile(floor, 50)),
                  floor_p90=float(np.percentile(floor, 90)), p=float(p),
                  verdict="same" if p >= ALPHA else "lower" if shift < 0 else "higher" if shift > 0 else "differs")
    return result


def compare(reference: pd.DataFrame, candidate: pd.DataFrame, metrics: Sequence[Metric] = METRICS) -> pd.DataFrame:
    """:func:`score` of every metric, one row per metric key. The resampling is seeded per metric (reproducible)."""
    return pd.DataFrame([{"metric": m.key, **score(metric_values(reference, m), metric_values(candidate, m),
                                                   zlib.crc32(m.key.encode()))} for m in metrics]).set_index("metric")


def _display(metric: Metric, value: Optional[float]) -> str:
    return "–" if value is None or pd.isna(value) else f"{value * metric.factor:,.{metric.decimals}f}"


def scorecard(scores: pd.DataFrame, names: Sequence[str] = ("reference", "candidate"),
              metrics: Sequence[Metric] = METRICS) -> pd.DataFrame:
    """Per metric the grid medians of both datasets and Δ.

    Δ is the typical difference of a candidate grid against a reference grid (Hodges–Lehmann shift) in % of the
    reference median: "≈" when the distributions do not differ beyond sampling noise, "differs" when they do but the
    typical difference is zero, e.g. for standard transformer sizes.
    """
    first = scores.iloc[0]
    rows = [{"metric": "Grids", "unit": "count", names[0]: str(first["reference_n"]),
             names[1]: str(first["candidate_n"]), "Δ": f"{first['candidate_n'] / first['reference_n'] - 1:+.0%}"}]
    for metric in metrics:
        s = scores.loc[metric.key]
        if s["verdict"] is None:
            delta = "–"
        elif s["verdict"] == "same":
            delta = "≈"
        elif s["verdict"] == "differs" or s["relative_shift"] is None:
            delta = "differs"
        else:
            delta = f"{s['relative_shift']:+.0%}"
        rows.append({"metric": metric.name, "unit": metric.unit, names[0]: _display(metric, s["reference_median"]),
                     names[1]: _display(metric, s["candidate_median"]), "Δ": delta})
    return pd.DataFrame(rows).set_index("metric")


def score_details(scores: pd.DataFrame, metrics: Sequence[Metric] = METRICS) -> pd.DataFrame:
    """The scores for reading: W1 in the display unit of each metric, W̃1, the noise floor and the p-value."""
    rows = []
    for metric in metrics:
        s = scores.loc[metric.key]
        rows.append({"metric": metric.name, "grids": f"{s['reference_n']} | {s['candidate_n']}",
                     "W1": _display(replace(metric, decimals=2), s["wasserstein"]), "unit": metric.unit,
                     "W̃1": s["normalized"], "floor (p90)": s["floor_p90"], "p": s["p"], "verdict": s["verdict"]})
    return pd.DataFrame(rows).set_index("metric").round(3)


def ranking(scores: Mapping[str, pd.DataFrame], metrics: Sequence[Metric] = METRICS) -> pd.DataFrame:
    """Per candidate: metrics within sampling noise, median W̃1 and the metric with the lowest p-value, the closest
    candidate first.

    The lowest p rather than the largest W̃1 names the most different metric: a metric whose reference grids mostly
    share one value (e.g. one standard cable) has a small interquartile range, so its W̃1 and its noise floor are large.
    """
    rows = []
    for name, table in scores.items():
        table = table.loc[[m.key for m in metrics]]
        lowest = table["p"].idxmin()
        rows.append({"version": name, "metrics within sampling noise": int((table["verdict"] == "same").sum()),
                     "median W̃1": table["normalized"].median(), "most different metric": BY_KEY[lowest].name,
                     "its p": table.loc[lowest, "p"]})
    return (pd.DataFrame(rows).set_index("version")
            .sort_values(["metrics within sampling noise", "median W̃1"], ascending=[False, True]).round(3))


# --------------------------------------------------------------------------- totals
def area_totals(groups: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Totals over all grids of each dataset (tables of :func:`grid_metrics`), one column per dataset.

    Each total sums over the grids before dividing, so it does not depend on how the buildings are split into grids.
    With two datasets, the last column is the ratio of the second to the first.
    """
    columns = {}
    for name, frame in groups.items():
        buildings = frame["building_count"].sum()
        known = frame.dropna(subset=["feeder_head_capacity_a"])
        columns[name] = {
            "grids": len(frame),
            "buildings": buildings,
            "transformer capacity (MVA)": frame["transformer_bank_capacity_kva"].sum() / 1000,
            "cable (km, parallel cables counted)": frame["cable_km"].sum(),
            "feeder branch ends per 100 buildings": 100 * frame["feeder_branching_endpoints"].sum() / buildings,
            "cable length per building (m)": 1000 * frame["cable_km"].sum() / buildings,
            "route length per building (m, parallel cables once)": 1000 * frame["route_km"].sum() / buildings,
            "transformer capacity per building (kVA)": frame["transformer_bank_capacity_kva"].sum() / buildings,
            "feeder capacity per building behind it (A)":
                known["feeder_head_capacity_a"].sum() / known["buildings_behind_feeders"].sum(),
        }
    table = pd.DataFrame(columns)
    if len(groups) == 2:
        first, second = list(groups)
        table["ratio"] = table[second] / table[first]
    return table


def cable_table(lines: pd.DataFrame, resistance: Mapping[str, float]) -> pd.DataFrame:
    """One row per cable section: feeder or house service (column ``is_service``, all feeders without it), equivalent
    aluminium cross-section, installed km (parallel circuits counted) and whether it is laid in parallel."""
    service = lines["is_service"].astype(bool) if "is_service" in lines else pd.Series(False, index=lines.index)
    return pd.DataFrame({
        "kind": np.where(service, "house services", "feeders"),
        "cross_section": [equivalent_cross_section(resistance.get(t)) for t in lines["std_type"]],
        "km": lines["length_km"] * lines["parallel"],
        "parallel": lines["parallel"] > 1,
    })


def cable_summary(tables: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Installed km of feeders and house services (:func:`cable_table`), and the share of feeder cable laid in
    parallel."""
    rows = {}
    for name, table in tables.items():
        feeders = table[table["kind"] == "feeders"]
        rows[name] = {"feeders (km)": feeders["km"].sum(),
                      "house services (km)": table.loc[table["kind"] == "house services", "km"].sum(),
                      "feeder cable in parallel (%)": 100 * feeders.loc[feeders["parallel"], "km"].sum()
                      / feeders["km"].sum()}
    return pd.DataFrame(rows).round(1)
