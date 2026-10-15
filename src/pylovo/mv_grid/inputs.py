"""MV node inputs from the LV results and from MaStR (pure: pandas and numpy only, no database or configuration).

The region step of the MV stage turns the rows read by :class:`~pylovo.database.mv_input_mixin.MvInputMixin` and
:class:`~pylovo.database.mv_source_mixin.MvSourceMixin` into ``pylovo.mv_node`` rows (DESIGN §3.4–§3.6):

* :func:`station_table`: one MV node per LV grid (station) with its per-category load statistics and its own
  coincident peak ``p_own_kw`` (pylovo's station design law, :func:`~pylovo.mv_grid.coincidence.coincident_kw`).
* :func:`customer_table`: the MV customers with their fixed load ``g_c x installed`` and connection class.
* :func:`classify_level`, :func:`aggregate_locations`: MaStR units grouped per grid connection point
  (``LokationMastrNummer``) with the voltage class of the point (DSO-entered ``Spannungsebene``, else by power).
* :func:`distribute_lv_generation`: generation connected on the LV side (located units and the postcode
  aggregates without coordinates) per station.
* :func:`node_rows`: the rows for :meth:`~pylovo.database.mv_result_mixin.MvResultMixin.insert_nodes` (busbars,
  stations, customers, generators).

Loads are in kW, positions in ``TARGET_EPSG`` metres. Customers are fixed loads: their ``load_stats`` stay empty so
that :data:`~pylovo.mv_grid.model.NODE_COLUMNS` (``inst_*``/``units_*`` from ``load_stats``, ``p_fixed_kw`` from
``p_own_kw``) never pools them into the category law a second time.
"""
from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np
import pandas as pd

from pylovo.mv_grid.coincidence import INSTALLED_COLUMNS, UNITS_COLUMNS, coincident_kw
from pylovo.mv_grid.model import CATEGORIES

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

logger = logging.getLogger(__name__)

#: Voltage classes of MaStR connection points, lowest first (the class of a point is the highest of its units).
LEVEL_CLASSES: tuple[str, ...] = ("lv", "mvlv", "mv", "hvmv", "hv")
_LEVEL_RANK: dict[str, int] = {name: rank for rank, name in enumerate(LEVEL_CLASSES)}

#: MaStR ``Spannungsebene`` texts (case-insensitive) and their class; texts starting with "niederspannung" are lv.
LEVEL_TEXTS: dict[str, str] = {
    "höchstspannung": "hv",
    "hochspannung": "hv",
    "umspannebene höchstspannung/hochspannung": "hv",
    "umspannebene hochspannung/mittelspannung": "hvmv",
    "mittelspannung": "mv",
    "umspannebene mittelspannung/niederspannung": "mvlv",
}

#: Generation groups of the node table and the MaStR technologies in them (storage is never generation).
GENERATION_GROUPS: dict[str, tuple[str, ...]] = {
    "solar": ("solar",),
    "wind": ("wind",),
    "other": ("biomass", "hydro", "gsgk", "combustion"),
}
GENERATION_COLUMNS: tuple[str, ...] = ("gen_solar_kw", "gen_wind_kw", "gen_other_kw")
_GROUP_COLUMN: dict[str, str] = {"solar": "gen_solar_kw", "wind": "gen_wind_kw", "other": "gen_other_kw"}

#: Units of one MaStR location farther apart than this (single linkage, m) are split into separate connection
#: points ``<location_id>/<k>`` (WP0 D10: a few locations hold units up to 105 km apart).
LOCATION_SPLIT_M: float = 1000.0
#: LV-side generation without an LV building target goes to the nearest station within this distance (m).
LV_SIDE_STATION_MAX_M: float = 2000.0

#: Columns of :func:`station_table`.
STATION_COLUMNS: tuple[str, ...] = (
    "kind", "connection_hint", "grid_result_id", "plz", "plz_settlement_type", "station_source", "x", "y", "sn_kva",
    "power_flow_status", "lv_max_drop_pu", *[c for pair in zip(INSTALLED_COLUMNS, UNITS_COLUMNS) for c in pair],
    "p_fixed_kw", "load_stats", "installed_kw", "p_own_kw", "households", "n_buildings", *GENERATION_COLUMNS, "flags",
)
#: Columns of :func:`customer_table`.
CUSTOMER_COLUMNS: tuple[str, ...] = (
    "kind", "connection_hint", "customer_objectid", "objectid", "plz", "category", "x", "y", "mapped_station",
    "installed_kw", "p_own_kw", "p_fixed_kw", "load_stats", "households", *GENERATION_COLUMNS, "flags",
)
#: Columns of :func:`aggregate_locations`.
LOCATION_COLUMNS: tuple[str, ...] = (
    "mastr_connection_id", "location_id", "n_units", "x", "y", *GENERATION_COLUMNS, "kw", "voltage_level",
    "voltage_class", "technology", "treatment", "split", "flags",
)
#: Columns of :func:`distribute_lv_generation`.
GENERATION_RESULT_COLUMNS: tuple[str, ...] = ("grid_result_id", *GENERATION_COLUMNS)


def _num(frame: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    """``frame[column]`` as float (missing column: ``default`` everywhere)."""
    if column not in frame.columns:
        return pd.Series(default, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce").astype(float)


def _flag_lists(frame: pd.DataFrame) -> list[list[str]]:
    """The ``flags`` column as fresh lists (missing column or empty cells: empty lists)."""
    if "flags" not in frame.columns:
        return [[] for _ in range(len(frame))]
    out = []
    for value in frame["flags"]:
        if isinstance(value, (list, tuple, np.ndarray)):
            out.append([str(v) for v in value if v is not None and str(v)])
        elif isinstance(value, str) and value:
            out.append([value])
        else:
            out.append([])
    return out


def _category(value: Any) -> str | None:
    """A consumer category of :data:`~pylovo.mv_grid.model.CATEGORIES`, or None for anything else."""
    return value if isinstance(value, str) and value in CATEGORIES else None


# ---------------------------------------------------------------------------------------------- stations
def stats_wide(stats: pd.DataFrame) -> pd.DataFrame:
    """Per-category load statistics of the stations as one row per ``grid_result_id``.

    Args:
        stats: Rows of :meth:`MvInputMixin.station_load_stats`: ``grid_result_id``, ``category``, ``installed_kw``,
            ``units`` and optionally ``grid_n_buildings`` (distinct buildings with a load component in the grid).
            A category outside :data:`~pylovo.mv_grid.model.CATEGORIES` counts as ``Unknown`` (logged).

    Returns:
        Index ``grid_result_id``; columns :data:`~pylovo.mv_grid.coincidence.INSTALLED_COLUMNS`,
        :data:`~pylovo.mv_grid.coincidence.UNITS_COLUMNS` (0 where a category is absent) and ``n_buildings``.
    """
    columns = [*INSTALLED_COLUMNS, *UNITS_COLUMNS, "n_buildings"]
    if stats is None or not len(stats):
        return pd.DataFrame(columns=columns, index=pd.Index([], name="grid_result_id", dtype="int64"), dtype=float)
    frame = pd.DataFrame(stats).copy()
    category = frame["category"].map(_category)
    unknown = frame.loc[category.isna(), "category"]
    if len(unknown):
        logger.warning(f"{len(unknown)} station load components have a category outside {', '.join(CATEGORIES)} "
                       f"({dict(Counter(map(str, unknown)))}); counted as Unknown")
    frame["category"] = category.fillna("Unknown")
    frame["grid_result_id"] = pd.to_numeric(frame["grid_result_id"]).astype("int64")
    frame["installed_kw"] = _num(frame, "installed_kw").fillna(0.0)
    frame["units"] = _num(frame, "units").fillna(0.0)
    sums = frame.groupby(["grid_result_id", "category"])[["installed_kw", "units"]].sum()
    wide = pd.DataFrame(index=pd.Index(sorted(frame["grid_result_id"].unique()), name="grid_result_id"))
    for category_name, inst_col, units_col in zip(CATEGORIES, INSTALLED_COLUMNS, UNITS_COLUMNS):
        part = sums.xs(category_name, level="category") if category_name in sums.index.get_level_values(1) \
            else pd.DataFrame(columns=["installed_kw", "units"], dtype=float)
        wide[inst_col] = part["installed_kw"].reindex(wide.index).fillna(0.0).astype(float)
        wide[units_col] = part["units"].reindex(wide.index).fillna(0.0).astype(float)
    if "grid_n_buildings" in frame.columns:
        wide["n_buildings"] = _num(frame, "grid_n_buildings").groupby(frame["grid_result_id"]).max() \
            .reindex(wide.index).fillna(0.0)
    elif "n_buildings" in frame.columns:   # per category: a lower bound for mixed buildings
        wide["n_buildings"] = _num(frame, "n_buildings").groupby(frame["grid_result_id"]).max() \
            .reindex(wide.index).fillna(0.0)
    else:
        wide["n_buildings"] = 0.0
    return wide[columns]


def load_stats_dicts(installed: np.ndarray, units: np.ndarray) -> list[dict[str, list[float]]]:
    """``mv_node.load_stats`` per row: ``{category: [installed_kw, units]}`` for the categories with a load."""
    out = []
    for inst_row, units_row in zip(np.asarray(installed, dtype=float), np.asarray(units, dtype=float)):
        out.append({category: [float(i), float(n)] for category, i, n in zip(CATEGORIES, inst_row, units_row)
                    if i > 0 or n > 0})
    return out


def station_table(stations: pd.DataFrame, stats: pd.DataFrame, lv: "LvVersionParameters") -> pd.DataFrame:
    """One MV node row per LV grid: position, rating, load statistics and the station's own coincident peak.

    ``p_own_kw`` is pylovo's station design load: the category law on the summed statistics of the grid's buildings
    with the sim factors of the LV version (equal to the pooled ``utils.simultaneous_peak_load`` of the grid).
    ``households`` is the residential load units (never ``buildings_result.households``, which is 1 for
    non-residential buildings). Stations whose LV power flow did not converge get the flag ``lv_not_converged``,
    those with another LV status than ``converged`` the flag ``lv_<status>`` (e.g. ``lv_voltage_violation``).

    Args:
        stations: Rows of :meth:`MvInputMixin.station_nodes` (``grid_result_id``, ``plz``, ``sn_kva``, ``x``,
            ``y``, ``station_source``, ``power_flow_status``, ``lv_max_drop_pu``, ``plz_settlement_type``, ...).
            Rows without a position are dropped (logged).
        stats: Rows of :meth:`MvInputMixin.station_load_stats` (long form, see :func:`stats_wide`).
        lv: Parameters of the LV version (sim factors).

    Returns:
        :data:`STATION_COLUMNS`, one row per station, sorted by ``grid_result_id``; stations without load
        components have zero statistics and ``p_own_kw`` 0.
    """
    frame = pd.DataFrame(stations).copy()
    if not len(frame):
        return pd.DataFrame(columns=list(STATION_COLUMNS))
    frame["grid_result_id"] = pd.to_numeric(frame["grid_result_id"]).astype("int64")
    frame["x"] = _num(frame, "x")
    frame["y"] = _num(frame, "y")
    no_position = ~(np.isfinite(frame["x"]) & np.isfinite(frame["y"]))
    if no_position.any():
        logger.warning(f"{int(no_position.sum())} stations have no position and are not MV nodes: grid_result_id "
                       + ", ".join(str(g) for g in frame.loc[no_position, "grid_result_id"].head(20)))
        frame = frame[~no_position]
    duplicated = frame["grid_result_id"].duplicated(keep="first")
    if duplicated.any():
        logger.warning(f"{int(duplicated.sum())} duplicated station rows dropped")
        frame = frame[~duplicated]
    frame = frame.sort_values("grid_result_id", kind="mergesort").reset_index(drop=True)
    wide = stats_wide(stats).reindex(frame["grid_result_id"].to_numpy()).fillna(0.0)
    installed = wide[list(INSTALLED_COLUMNS)].to_numpy(dtype=float)
    units = wide[list(UNITS_COLUMNS)].to_numpy(dtype=float)
    p_own = np.asarray(coincident_kw(installed, units, lv.g()), dtype=float).reshape(len(frame))
    out = pd.DataFrame({"kind": "station", "connection_hint": "ring"}, index=frame.index)
    out["grid_result_id"] = frame["grid_result_id"].astype("int64")
    out["plz"] = _num(frame, "plz").astype("Int64")
    out["plz_settlement_type"] = _num(frame, "plz_settlement_type").astype("Int64")
    out["station_source"] = frame["station_source"].astype(object) if "station_source" in frame.columns else None
    out["x"] = frame["x"].astype(float)
    out["y"] = frame["y"].astype(float)
    out["sn_kva"] = _num(frame, "sn_kva")
    out["power_flow_status"] = frame["power_flow_status"].astype(object) if "power_flow_status" in frame.columns \
        else None
    out["lv_max_drop_pu"] = _num(frame, "lv_max_drop_pu")
    for column in (*INSTALLED_COLUMNS, *UNITS_COLUMNS):
        out[column] = wide[column].to_numpy(dtype=float)
    out["p_fixed_kw"] = 0.0
    out["load_stats"] = load_stats_dicts(installed, units)
    out["installed_kw"] = installed.sum(axis=1)
    out["p_own_kw"] = p_own
    out["households"] = wide["units_res"].to_numpy(dtype=float)
    out["n_buildings"] = wide["n_buildings"].to_numpy(dtype=float).round().astype("int64")
    for column in GENERATION_COLUMNS:
        out[column] = 0.0
    flags = _flag_lists(frame)
    status = out["power_flow_status"]
    for i, value in enumerate(status):
        if isinstance(value, str) and value and value != "converged":
            flags[i].append(value if value.startswith("lv_") else f"lv_{value}")
    out["flags"] = flags
    return out[list(STATION_COLUMNS)]


# ---------------------------------------------------------------------------------------------- customers
def customer_table(rows: pd.DataFrame, lv: "LvVersionParameters", params: "MvParameters") -> pd.DataFrame:
    """The MV customers as node rows with their fixed load and connection class (DESIGN §3.5).

    ``p_own_kw = p_fixed_kw = g_c x installed_kw`` with the LV-version sim factor of the customer's
    non-residential use (the asymptote of pylovo's law). Class (``connection_hint``): above ``hv_customer_kw`` ->
    ``excluded`` with flag ``hv_customer`` (listed, not modelled); above ``dedicated_feeder_kw`` -> ``dedicated``;
    else ``ring``. ``load_stats`` stays empty (a fixed load is never pooled again).

    Args:
        rows: Rows of :meth:`MvInputMixin.mv_customer_rows`: ``objectid``, ``plz``, ``nonresidential_use``,
            ``installed_kw``, ``x``, ``y``, ``mapped_station`` and optionally ``source`` (``mv_customers`` or
            ``buildings_result`` for versions without ``pylovo.mv_customers`` rows; those get no
            ``customer_objectid``, which references ``pylovo.mv_customers``, and the flag ``not_in_mv_customers``).
            A use outside Commercial, Public, Unknown counts as Unknown (flag ``use_unknown``).
        lv: Parameters of the LV version (sim factors).
        params: MV parameters (``dedicated_feeder_kw``, ``hv_customer_kw``).

    Returns:
        :data:`CUSTOMER_COLUMNS`, sorted by ``objectid``; rows without a positive installed load or without a
        position are dropped (logged).
    """
    frame = pd.DataFrame(rows).copy()
    if not len(frame):
        return pd.DataFrame(columns=list(CUSTOMER_COLUMNS))
    frame["installed_kw"] = _num(frame, "installed_kw")
    frame["x"] = _num(frame, "x")
    frame["y"] = _num(frame, "y")
    keep = (frame["installed_kw"] > 0) & np.isfinite(frame["x"]) & np.isfinite(frame["y"])
    if (~keep).any():
        logger.warning(f"{int((~keep).sum())} MV customer rows without load or position dropped")
        frame = frame[keep]
    frame["objectid"] = frame["objectid"].astype(str)
    frame = frame.sort_values("objectid", kind="mergesort").drop_duplicates("objectid").reset_index(drop=True)
    uses = frame["nonresidential_use"] if "nonresidential_use" in frame.columns \
        else pd.Series(None, index=frame.index, dtype=object)
    category = uses.map(lambda u: u if isinstance(u, str) and u in CATEGORIES and u != "Residential" else None)
    flags = _flag_lists(frame)
    for i in np.flatnonzero(category.isna().to_numpy()):
        flags[i].append("use_unknown")
    category = category.fillna("Unknown")
    g = category.map(lambda c: float(lv.sim_factor[c])).astype(float)
    p_kw = g * frame["installed_kw"]
    hint = np.where(p_kw > params.hv_customer_kw, "excluded",
                    np.where(p_kw > params.dedicated_feeder_kw, "dedicated", "ring"))
    source = frame["source"].astype(object) if "source" in frame.columns \
        else pd.Series("mv_customers", index=frame.index, dtype=object)
    in_table = source.fillna("mv_customers").eq("mv_customers").to_numpy()
    mapped = frame["mapped_station"].map(lambda v: bool(v) if pd.notna(v) else False) \
        if "mapped_station" in frame.columns else pd.Series(False, index=frame.index)
    for i in range(len(frame)):
        if hint[i] == "excluded":
            flags[i].append("hv_customer")
        if not in_table[i]:
            flags[i].append("not_in_mv_customers")
        if mapped.iloc[i]:
            flags[i].append("mapped_station")
    out = pd.DataFrame({"kind": "customer", "connection_hint": hint}, index=frame.index)
    out["customer_objectid"] = np.where(in_table, frame["objectid"].to_numpy(dtype=object), None)
    out["objectid"] = frame["objectid"]
    out["plz"] = _num(frame, "plz").astype("Int64")
    out["category"] = category
    out["x"] = frame["x"].astype(float)
    out["y"] = frame["y"].astype(float)
    out["mapped_station"] = mapped.astype(bool).to_numpy()
    out["installed_kw"] = frame["installed_kw"].astype(float)
    out["p_own_kw"] = p_kw.astype(float)
    out["p_fixed_kw"] = p_kw.astype(float)
    out["load_stats"] = None
    out["households"] = 0.0
    for column in GENERATION_COLUMNS:
        out[column] = 0.0
    out["flags"] = flags
    return out[list(CUSTOMER_COLUMNS)]


# ---------------------------------------------------------------------------------------------- MaStR
def _level_text_class(voltage_level: Any) -> str | None:
    """The class of a known MaStR ``Spannungsebene`` text, or None (NULL or unknown text)."""
    if not isinstance(voltage_level, str):
        return None
    text = " ".join(voltage_level.split()).casefold()
    if not text:
        return None
    if text in LEVEL_TEXTS:
        return LEVEL_TEXTS[text]
    if text.startswith("niederspannung"):
        return "lv"
    return None


def classify_level(voltage_level: str | None, kw: float, params: "MvParameters") -> str:
    """Voltage class of a MaStR unit or connection point: ``hv``, ``hvmv``, ``mv``, ``mvlv`` or ``lv``.

    The DSO-entered ``Spannungsebene`` decides (MaStRV § 14): Höchst-/Hochspannung and the HöS/HS transformation
    level -> ``hv``; HS/MS transformation level -> ``hvmv``; Mittelspannung -> ``mv``; MS/NS transformation level ->
    ``mvlv``; Niederspannung... -> ``lv``. NULL or an unknown text is classified by power with
    ``generator_fallback_kw``: up to ``lv`` -> lv, up to ``mvlv`` -> mvlv, up to ``mv`` -> mv, up to ``hvmv`` ->
    hvmv, above -> hv (eGon-data thresholds).

    Args:
        voltage_level: MaStR ``Spannungsebene`` text or None.
        kw: Net power in kW (of the connection point, for the fallback).
        params: MV parameters (``generator_fallback_kw``).
    """
    known = _level_text_class(voltage_level)
    if known is not None:
        return known
    limits = params.generator_fallback_kw
    power = float(kw) if kw is not None and np.isfinite(kw) else 0.0
    for name in ("lv", "mvlv", "mv", "hvmv"):
        if power <= float(limits[name]):
            return name
    return "hv"


def treatment(voltage_class: str, kw: float, params: "MvParameters") -> str:
    """How a connection point of a class is modelled: ``node`` (own MV node), ``lv_side`` or ``excluded`` (HV).

    ``hvmv`` and ``mv`` are own MV nodes; ``mvlv`` points are own nodes above ``generator_lv_side_kw`` (their own
    MV/LV station), else LV side; ``lv`` is LV side; ``hv`` is excluded (counted).
    """
    if voltage_class == "hv":
        return "excluded"
    if voltage_class in ("hvmv", "mv"):
        return "node"
    if voltage_class == "mvlv":
        return "node" if float(kw) > params.generator_lv_side_kw else "lv_side"
    return "lv_side"


def _group_of(technology: Any) -> str | None:
    tech = str(technology).strip().casefold() if technology is not None else ""
    for group, members in GENERATION_GROUPS.items():
        if tech in members:
            return group
    return None if tech == "storage" else "other"


def _single_linkage(xy: np.ndarray, distance_m: float) -> np.ndarray:
    """Component label per point of the single-linkage clustering at ``distance_m`` (labels 0.., by first point)."""
    n = len(xy)
    parent = np.arange(n)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    if n > 1:
        from scipy.spatial import cKDTree

        for a, b in sorted(cKDTree(xy).query_pairs(distance_m)):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(n)])
    _, labels = np.unique(roots, return_inverse=True)    # roots are the smallest index: labels follow point order
    return labels.astype(np.int64)


def aggregate_locations(units: pd.DataFrame, params: "MvParameters", *,
                        split_m: float = LOCATION_SPLIT_M) -> pd.DataFrame:
    """Group MaStR units per grid connection point (DESIGN §3.6).

    Key: ``location_id`` (``LokationMastrNummer``: one wind or PV park is one connection point), else
    ``unit/<mastr_id>``. Units of one location farther apart than ``split_m`` (single linkage) form separate points
    ``<location_id>/<k>`` (k from 1, in the order of their smallest ``mastr_id``; flag ``location_split``). Per point:
    kW per group (solar, wind, other), the power-weighted position, the class (the highest class of the known
    ``Spannungsebene`` texts of its units; a point without any known text is classified by its total power with
    :func:`classify_level`) and the :func:`treatment`. Storage and units without power or position are left out.

    Args:
        units: Rows of :meth:`MvSourceMixin.mastr_units_in_region` (``mastr_id``, ``location_id``, ``technology``,
            ``net_power_kw``, ``voltage_level``, ``x``, ``y``).
        params: MV parameters (``generator_fallback_kw``, ``generator_lv_side_kw``).
        split_m: Single-linkage distance of the location split (m).

    Returns:
        :data:`LOCATION_COLUMNS`, one row per connection point sorted by ``mastr_connection_id``. ``attrs``:
        ``class_counts`` (points per class), ``class_kw``, ``unknown_levels`` (unknown texts with unit counts),
        ``n_split_locations``.
    """
    frame = pd.DataFrame(units).copy()
    empty = pd.DataFrame(columns=list(LOCATION_COLUMNS))
    empty.attrs.update(class_counts={}, class_kw={}, unknown_levels={}, n_split_locations=0)
    if not len(frame):
        return empty
    frame["net_power_kw"] = _num(frame, "net_power_kw")
    frame["x"] = _num(frame, "x")
    frame["y"] = _num(frame, "y")
    frame["group"] = frame["technology"].map(_group_of) if "technology" in frame.columns else "other"
    keep = (frame["group"].notna() & (frame["net_power_kw"] > 0) & np.isfinite(frame["x"])
            & np.isfinite(frame["y"]))
    frame = frame[keep].copy()
    if not len(frame):
        return empty
    frame["mastr_id"] = frame["mastr_id"].astype(str)
    location = frame["location_id"] if "location_id" in frame.columns else pd.Series(None, index=frame.index)
    location = location.map(lambda v: str(v).strip() if isinstance(v, str) and str(v).strip() else None)
    frame["location_id"] = location
    frame["key"] = [loc if loc is not None else f"unit/{mid}" for loc, mid in zip(location, frame["mastr_id"])]
    frame = frame.sort_values(["key", "mastr_id"], kind="mergesort").reset_index(drop=True)
    unknown_levels: Counter = Counter()
    if "voltage_level" not in frame.columns:
        frame["voltage_level"] = None
    for text in frame["voltage_level"]:
        if isinstance(text, str) and text.strip() and _level_text_class(text) is None:
            unknown_levels[text] += 1
    # split spread-out locations into connection points
    frame["point"] = frame["key"]
    frame["split"] = False
    n_split = 0
    for key, index in frame.groupby("key", sort=True).groups.items():
        if len(index) < 2:
            continue
        labels = _single_linkage(frame.loc[index, ["x", "y"]].to_numpy(dtype=float), split_m)
        if labels.max() > 0:
            n_split += 1
            frame.loc[index, "point"] = [f"{key}/{int(k) + 1}" for k in labels]
            frame.loc[index, "split"] = True
    records = []
    for point, part in frame.groupby("point", sort=True):
        kw = part["net_power_kw"].to_numpy(dtype=float)
        total = float(kw.sum())
        group_kw = {group: float(part.loc[part["group"] == group, "net_power_kw"].sum()) for group in GENERATION_GROUPS}
        levels = [t for t in part["voltage_level"] if isinstance(t, str) and t.strip()]
        level_text = None
        if levels:
            ranked = Counter(levels).most_common()
            level_text = sorted(t for t, n in ranked if n == ranked[0][1])[0]
        # the known levels of the point decide; only a point without any known level is classified by power
        known = [c for c in (_level_text_class(t) for t in levels) if c is not None]
        cls = max(known, key=_LEVEL_RANK.__getitem__) if known else classify_level(None, total, params)
        label = "+".join(group for group in GENERATION_GROUPS if group_kw[group] > 0)
        records.append({
            "mastr_connection_id": str(point),
            "location_id": part["location_id"].iloc[0],
            "n_units": int(len(part)),
            "x": float(np.average(part["x"].to_numpy(dtype=float), weights=kw)),
            "y": float(np.average(part["y"].to_numpy(dtype=float), weights=kw)),
            **{_GROUP_COLUMN[group]: value for group, value in group_kw.items()},
            "kw": total,
            "voltage_level": level_text,
            "voltage_class": cls,
            "technology": label,
            "treatment": treatment(cls, total, params),
            "split": bool(part["split"].iloc[0]),
            "flags": ["location_split"] if bool(part["split"].iloc[0]) else [],
        })
    out = pd.DataFrame.from_records(records, columns=list(LOCATION_COLUMNS))
    out = out.sort_values("mastr_connection_id", kind="mergesort").reset_index(drop=True)
    class_counts = {name: int((out["voltage_class"] == name).sum()) for name in LEVEL_CLASSES}
    class_kw = {name: float(out.loc[out["voltage_class"] == name, "kw"].sum()) for name in LEVEL_CLASSES}
    if unknown_levels:
        logger.warning("MaStR units with an unknown Spannungsebene (classified by power): "
                       + ", ".join(f"{text!r} {count}" for text, count in sorted(unknown_levels.items())))
    if n_split:
        logger.info(f"{n_split} MaStR locations with units more than {split_m:g} m apart were split")
    logger.info("MaStR connection points per class: "
                + ", ".join(f"{name} {class_counts[name]} ({class_kw[name] / 1000:.1f} MW)" for name in LEVEL_CLASSES))
    out.attrs.update(class_counts=class_counts, class_kw=class_kw, unknown_levels=dict(unknown_levels),
                     n_split_locations=n_split)
    return out


# ---------------------------------------------------------------------------------------------- LV-side generation
def _generation_columns(frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({column: _num(frame, column).fillna(0.0) for column in GENERATION_COLUMNS}, index=frame.index)


def _postcode_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def distribute_lv_generation(stations: pd.DataFrame, targets: pd.DataFrame, aggregates: pd.DataFrame,
                             building_counts: pd.DataFrame, *,
                             max_station_m: float = LV_SIDE_STATION_MAX_M) -> pd.DataFrame:
    """LV-side generation per station (DESIGN §3.6).

    * **Located points** (``targets``, rows of :meth:`MvInputMixin.lv_side_targets`): to the grid of the nearest
      LV building within 100 m (``grid_result_id``); without one, or when that grid is not a station of
      ``stations``, to the nearest station within ``max_station_m`` (k-d tree); else dropped (logged).
    * **Postcode aggregates** (units without coordinates): ``net_power_kw x area_ratio`` (the share of the PLZ
      polygon inside the planning region, WP0 D11) split over the stations of the PLZ in proportion to their
      number of buildings in that PLZ (rooftop PV follows buildings); a PLZ without counted buildings is split
      equally over the stations with that ``plz``; else dropped (logged).

    Args:
        stations: :func:`station_table` rows (``grid_result_id``, ``x``, ``y``, ``plz``).
        targets: Points with ``x``, ``y``, :data:`GENERATION_COLUMNS` and ``grid_result_id`` (NaN: no LV building
            within reach).
        aggregates: Rows of :meth:`MvSourceMixin.mastr_postcode_aggregates` (``postcode`` or ``plz``,
            ``technology``, ``net_power_kw``, optional ``area_ratio``).
        building_counts: Rows of :meth:`MvInputMixin.station_building_counts` (``grid_result_id``, ``postcode``,
            ``n_buildings``).
        max_station_m: Largest distance of the nearest-station fallback (m).

    Returns:
        :data:`GENERATION_RESULT_COLUMNS`, one row per station of ``stations`` (zeros without generation), sorted
        by ``grid_result_id``. ``attrs``: ``points_building``, ``points_station``, ``points_dropped``,
        ``kw_dropped``, ``aggregate_kw``, ``aggregate_kw_dropped``.
    """
    st = pd.DataFrame(stations)
    ids = np.sort(pd.to_numeric(st["grid_result_id"]).astype("int64").unique()) if len(st) \
        else np.array([], dtype=np.int64)
    result = pd.DataFrame(0.0, index=pd.Index(ids, name="grid_result_id"), columns=list(GENERATION_COLUMNS))
    attrs = {"points_building": 0, "points_station": 0, "points_dropped": 0, "kw_dropped": 0.0,
             "aggregate_kw": 0.0, "aggregate_kw_dropped": 0.0}
    station_set = set(ids.tolist())
    # located LV-side points
    tg = pd.DataFrame(targets).copy() if targets is not None else pd.DataFrame()
    if len(tg):
        gen = _generation_columns(tg)
        grid = pd.to_numeric(tg["grid_result_id"], errors="coerce") if "grid_result_id" in tg.columns \
            else pd.Series(np.nan, index=tg.index)
        target = np.full(len(tg), -1, dtype=np.int64)
        known = grid.notna().to_numpy() & grid.fillna(-1).astype("int64").isin(station_set).to_numpy()
        target[known] = grid[known].astype("int64").to_numpy()
        attrs["points_building"] = int(known.sum())
        rest = np.flatnonzero(~known)
        if len(rest) and len(ids):
            from scipy.spatial import cKDTree

            st_sorted = st.assign(grid_result_id=pd.to_numeric(st["grid_result_id"]).astype("int64")) \
                .drop_duplicates("grid_result_id").sort_values("grid_result_id")
            st_xy = st_sorted[["x", "y"]].to_numpy(dtype=float)
            ok = np.isfinite(st_xy).all(axis=1)
            st_ids = st_sorted["grid_result_id"].to_numpy(dtype=np.int64)[ok]
            pts = tg.iloc[rest][["x", "y"]].to_numpy(dtype=float)
            finite = np.isfinite(pts).all(axis=1)
            if ok.any() and finite.any():
                dist, idx = cKDTree(st_xy[ok]).query(pts[finite], distance_upper_bound=max_station_m)
                hit = np.isfinite(dist)
                chosen = rest[np.flatnonzero(finite)[hit]]
                target[chosen] = st_ids[idx[hit]]
                attrs["points_station"] = int(hit.sum())
        dropped = target < 0
        attrs["points_dropped"] = int(dropped.sum())
        attrs["kw_dropped"] = float(gen.to_numpy()[dropped].sum())
        if dropped.any():
            logger.warning(f"{int(dropped.sum())} LV-side generation points ({attrs['kw_dropped'] / 1000:.2f} MW) "
                           f"have no station within {max_station_m:g} m and are dropped")
        if (~dropped).any():
            sums = gen[~dropped].groupby(target[~dropped]).sum()
            result = result.add(sums.reindex(result.index).fillna(0.0), fill_value=0.0)
    # postcode aggregates
    ag = pd.DataFrame(aggregates).copy() if aggregates is not None else pd.DataFrame()
    if len(ag):
        ag["plz_int"] = (ag["plz"] if "plz" in ag.columns else ag["postcode"]).map(_postcode_int)
        ag["group"] = ag["technology"].map(_group_of)
        ag = ag[ag["group"].notna() & ag["plz_int"].notna()]
        ratio = _num(ag, "area_ratio", 1.0).fillna(1.0).clip(lower=0.0, upper=1.0)
        ag["kw"] = _num(ag, "net_power_kw").fillna(0.0) * ratio
        attrs["aggregate_kw"] = float(ag["kw"].sum())
        counts = pd.DataFrame(building_counts).copy() if building_counts is not None else pd.DataFrame()
        if len(counts):
            counts["plz_int"] = counts["postcode"].map(_postcode_int)
            counts["grid_result_id"] = pd.to_numeric(counts["grid_result_id"]).astype("int64")
            counts["n_buildings"] = _num(counts, "n_buildings").fillna(0.0)
            counts = counts[counts["grid_result_id"].isin(station_set) & (counts["n_buildings"] > 0)]
        station_plz = pd.Series(_num(st, "plz").to_numpy(), index=pd.to_numeric(st["grid_result_id"]).astype("int64")) \
            if len(st) else pd.Series(dtype=float)
        for (plz, group), part in ag.groupby(["plz_int", "group"], sort=True):
            kw = float(part["kw"].sum())
            if kw <= 0:
                continue
            share = counts[counts["plz_int"] == plz].groupby("grid_result_id")["n_buildings"].sum() \
                if len(counts) else pd.Series(dtype=float)
            if not len(share) or share.sum() <= 0:
                same = station_plz.index[station_plz.to_numpy() == plz]
                share = pd.Series(1.0, index=pd.Index(sorted(set(same.tolist())), dtype="int64"))
            if not len(share):
                attrs["aggregate_kw_dropped"] += kw
                continue
            column = _GROUP_COLUMN[group]
            add = kw * share / float(share.sum())
            result.loc[add.index, column] += add.to_numpy(dtype=float)
        if attrs["aggregate_kw_dropped"] > 0:
            logger.warning(f"{attrs['aggregate_kw_dropped'] / 1000:.2f} MW of MaStR postcode aggregates have no "
                           "station in their PLZ and are dropped")
    out = result.reset_index()
    out["grid_result_id"] = out["grid_result_id"].astype("int64")
    out = out[list(GENERATION_RESULT_COLUMNS)]
    out.attrs.update(attrs)
    return out


# ---------------------------------------------------------------------------------------------- node rows
def generator_hint(voltage_class: str, kw: float, params: "MvParameters") -> str:
    """``connection_hint`` of an MV generator node: ``dedicated`` for the HS/MS level or above
    ``dedicated_feeder_kw``, else ``tee`` (the topology decides between loop-in and T-off)."""
    if voltage_class == "hvmv" or float(kw) > params.dedicated_feeder_kw:
        return "dedicated"
    return "tee"


def _busbar_rows(sites: pd.DataFrame) -> pd.DataFrame:
    sites = pd.DataFrame(sites)
    out = pd.DataFrame({"kind": "busbar", "connection_hint": "busbar", "connection": "busbar"},
                       index=sites.index)
    if "substation_id" in sites.columns:
        out["substation_id"] = pd.to_numeric(sites["substation_id"]).astype("Int64")
    out["x"] = _num(sites, "x")
    out["y"] = _num(sites, "y")
    out["flags"] = [[] for _ in range(len(sites))]
    if "substation_id" in out.columns:
        out = out.sort_values("substation_id", kind="mergesort")
    return out


def _generator_rows(generators: pd.DataFrame, params: "MvParameters") -> pd.DataFrame:
    gen = pd.DataFrame(generators).copy()
    if not len(gen):
        return pd.DataFrame(columns=["kind", "connection_hint", "x", "y"])
    if "treatment" in gen.columns:
        not_node = ~gen["treatment"].astype(object).isin(["node"])
        if not_node.any():
            logger.info(f"{int(not_node.sum())} MaStR connection points are LV side or HV level: no MV nodes")
            gen = gen[~not_node]
    kw = _generation_columns(gen).sum(axis=1) if len(gen) else pd.Series(dtype=float)
    cls = gen["voltage_class"].astype(object) if "voltage_class" in gen.columns \
        else pd.Series("mv", index=gen.index, dtype=object)
    out = pd.DataFrame({"kind": "generator"}, index=gen.index)
    out["connection_hint"] = [generator_hint(c, k, params) for c, k in zip(cls, kw)]
    out["mastr_connection_id"] = gen["mastr_connection_id"].astype(str)
    out["voltage_class"] = cls
    out["technology"] = gen["technology"].astype(object) if "technology" in gen.columns else None
    out["x"] = _num(gen, "x")
    out["y"] = _num(gen, "y")
    for column in GENERATION_COLUMNS:
        out[column] = _num(gen, column).fillna(0.0)
    out["flags"] = _flag_lists(gen)
    return out.sort_values("mastr_connection_id", kind="mergesort")


def node_rows(stations: pd.DataFrame, customers: pd.DataFrame, generators: pd.DataFrame, sites: pd.DataFrame,
              params: "MvParameters") -> pd.DataFrame:
    """The ``pylovo.mv_node`` rows of a region: busbars, stations, customers and MV generators.

    Order (it fixes the ``mv_node_id`` order, so the result is deterministic): one busbar per site (by
    ``substation_id``), the stations (by ``grid_result_id``), the customers (by ``objectid``), the generators (by
    ``mastr_connection_id``). Generator rows are made from :func:`aggregate_locations` rows with treatment
    ``node`` (rows of another treatment are left out); their ``connection_hint`` is :func:`generator_hint`.

    Args:
        stations: :func:`station_table` rows (with the LV-side generation merged in).
        customers: :func:`customer_table` rows.
        generators: :func:`aggregate_locations` rows.
        sites: Substation sites (``substation_id``, ``x``, ``y``), e.g. ``substations.sites_frame`` plus ids.
        params: MV parameters.

    Returns:
        One row per node with ``kind``, ``connection_hint``, ``x``, ``y`` and the ``mv_node`` columns of each
        kind; ``gen_*_kw`` are 0 where not set and ``flags`` is a list in every row; customers keep
        ``mapped_station`` (used for the street transfer point). Index 0..n-1.
    """
    parts = [_busbar_rows(sites)]
    station_part = pd.DataFrame(stations).copy()
    if len(station_part):
        station_part = station_part.sort_values("grid_result_id", kind="mergesort")
    parts.append(station_part)
    customer_part = pd.DataFrame(customers).copy()
    if len(customer_part):
        customer_part = customer_part.sort_values("objectid" if "objectid" in customer_part.columns
                                                  else "customer_objectid", kind="mergesort")
    parts.append(customer_part)
    parts.append(_generator_rows(generators, params))
    parts = [p for p in parts if len(p)]
    if not parts:
        return pd.DataFrame(columns=["kind", "connection_hint", "x", "y", *GENERATION_COLUMNS, "flags"])
    rows = pd.concat(parts, ignore_index=True, sort=False)
    for column in GENERATION_COLUMNS:
        rows[column] = _num(rows, column).fillna(0.0)
    rows["flags"] = _flag_lists(rows)
    if "mapped_station" in rows.columns:
        rows["mapped_station"] = rows["mapped_station"].map(lambda v: bool(v) if pd.notna(v) else False)
    if "customer_objectid" in rows.columns:
        rows["customer_objectid"] = rows["customer_objectid"].astype(object).where(rows["customer_objectid"].notna(),
                                                                                   None)
    return rows.reset_index(drop=True)


def summary_counts(rows: pd.DataFrame) -> Mapping[str, int]:
    """Node counts per kind and connection hint (for logs and acceptance checks)."""
    if not len(rows):
        return {}
    return {f"{k}/{h}": int(n) for (k, h), n in rows.groupby(["kind", "connection_hint"]).size().items()}
