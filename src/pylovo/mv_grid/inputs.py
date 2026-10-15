"""MV node inputs from the LV results and from MaStR (pure: pandas and numpy only, no database or configuration).

The region step of the MV stage turns the rows read by :class:`~pylovo.database.mv_input_mixin.MvInputMixin` and
:class:`~pylovo.database.mv_source_mixin.MvSourceMixin` into ``pylovo.mv_node`` rows (DESIGN §3.4–§3.6):

* :func:`station_table`: one MV node per LV grid (station) with its per-category load statistics and its own
  coincident peak ``p_own_kw`` (pylovo's station design law, :func:`~pylovo.mv_grid.coincidence.coincident_kw`).
* :func:`customer_table`: the MV customers with their MV load (halls count one storey), fixed load
  ``g_c x installed`` and connection class.
* :func:`busbar_customers`: customers below ``customer_min_mv_kw`` get no MV node; they hang on the busbar of the
  nearest station within ``busbar_customer_max_m``; :func:`upsize_busbar_stations` gives a station that they
  overload the next catalogue rating (MV model only).
* :func:`demand_anchor`: the regional coincident MV load per inhabitant.
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

import json
import logging
import math
from collections import Counter
from typing import TYPE_CHECKING, Any, Mapping

import numpy as np
import pandas as pd

from pylovo.mv_grid.coincidence import INSTALLED_COLUMNS, UNITS_COLUMNS, coincident_kw, node_stats, pooled_kw
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
    "lv_installed_kw", "installed_kw", "p_own_kw", "p_fixed_kw", "load_stats", "households", *GENERATION_COLUMNS,
    "flags",
)
#: Key of the station-busbar customers in ``load_stats`` of a station node (:func:`busbar_customers`).
BUSBAR_CUSTOMERS: str = "busbar_customers"
#: Unit rating of pylovo's stations above the catalogue (kVA): they get n x 630 kVA (``update_transformer_rated_power``).
STATION_UNIT_KVA: float = 630.0
#: A PLZ polygon with less than this share of the PLZ area of the municipal register lies only partly in the
#: planning region (the data extent cuts it): its population counts pro rata and the demand anchor is approximate.
PLZ_FULL_AREA_SHARE: float = 0.95
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
    """The MV customers as node rows with their MV load, fixed load and connection class (DESIGN §3.5).

    **MV load (hall rule).** The LV stage gives a customer ``nonresidential_floor_area x W/m²`` of its category on
    the gross floor area, whose storeys the building data derive from the height, so large single-storey halls
    count several storeys. A building with a footprint of at least ``customer_hall_min_footprint_m2`` (None: no
    hall rule) is a hall: its floor area for the MV load is one storey, ``min(footprint,
    nonresidential_floor_area)`` (flag ``hall_one_storey`` where this lowers it). ``installed_kw`` = that area x
    the W/m² of the category in the
    LV snapshot (``lv.peak_load_per_m2``); the stored LV value is kept as ``lv_installed_kw``. A row without a
    floor area takes the area of its LV load (``lv_installed_kw / W/m²``).

    ``p_own_kw = p_fixed_kw = g_c x installed_kw`` with the LV-version sim factor of the customer's
    non-residential use (the asymptote of pylovo's law). Class (``connection_hint``, on these corrected values):
    above ``hv_customer_kw`` -> ``excluded`` with flag ``hv_customer`` (listed, not modelled); above
    ``dedicated_feeder_kw`` -> ``dedicated``; else ``ring``. ``load_stats`` stays empty (a fixed load is never pooled
    again). Customers below ``customer_min_mv_kw`` are split off afterwards (:func:`busbar_customers`).

    Args:
        rows: Rows of :meth:`MvInputMixin.mv_customer_rows`: ``objectid``, ``plz``, ``nonresidential_use``,
            ``installed_kw``, ``x``, ``y``, ``mapped_station``, optionally ``footprint_m2`` and
            ``nonresidential_floor_area`` (without a footprint the hall rule does not apply) and ``source``
            (``mv_customers`` or ``buildings_result`` for versions without ``pylovo.mv_customers`` rows; those get no
            ``customer_objectid``, which references ``pylovo.mv_customers``, and the flag ``not_in_mv_customers``).
            A use outside Commercial, Public, Unknown counts as Unknown (flag ``use_unknown``).
        lv: Parameters of the LV version (sim factors, ``peak_load_per_m2``).
        params: MV parameters (``customer_hall_min_footprint_m2``, ``dedicated_feeder_kw``, ``hv_customer_kw``).

    Returns:
        :data:`CUSTOMER_COLUMNS`, sorted by ``objectid``; rows without a positive installed load or without a
        position are dropped (logged).

    Raises:
        ValueError: If ``lv`` has no positive peak load per m² for a category of the customers.
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
    w_per_m2 = category.map(lambda c: lv.peak_load_per_m2.get(c, np.nan)).astype(float)
    unknown_w = sorted(set(category[~(w_per_m2 > 0)]))
    if unknown_w:
        raise ValueError(f"LV version {lv.version_id} has no peak load per m² for {', '.join(unknown_w)} "
                         "(LvVersionParameters.peak_load_per_m2)")
    lv_installed = frame["installed_kw"].astype(float)
    area = _num(frame, "nonresidential_floor_area")
    area = area.where(area > 0, lv_installed * 1000.0 / w_per_m2)
    footprint = _num(frame, "footprint_m2")
    hall_m2 = params.customer_hall_min_footprint_m2                       # None: no hall rule
    hall = (footprint >= hall_m2) & (footprint < area) if hall_m2 is not None \
        else pd.Series(False, index=frame.index)
    area = area.where(~hall, footprint)
    for i in np.flatnonzero(hall.to_numpy()):
        flags[i].append("hall_one_storey")
    installed = area * w_per_m2 / 1000.0
    g = category.map(lambda c: float(lv.sim_factor[c])).astype(float)
    p_kw = g * installed
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
    out["lv_installed_kw"] = lv_installed
    out["installed_kw"] = installed.astype(float)
    out["p_own_kw"] = p_kw.astype(float)
    out["p_fixed_kw"] = p_kw.astype(float)
    out["load_stats"] = None
    out["households"] = 0.0
    for column in GENERATION_COLUMNS:
        out[column] = 0.0
    out["flags"] = flags
    return out[list(CUSTOMER_COLUMNS)]


def busbar_record(load_stats: Any) -> dict:
    """The station-busbar customer record (:func:`busbar_customers`) in a station's ``load_stats`` (a dict or its
    JSON text), or an empty dict."""
    if isinstance(load_stats, str):
        load_stats = json.loads(load_stats)
    record = load_stats.get(BUSBAR_CUSTOMERS) if isinstance(load_stats, Mapping) else None
    return dict(record) if isinstance(record, Mapping) else {}


def busbar_customers(stations: pd.DataFrame, customers: pd.DataFrame,
                     params: "MvParameters") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Hang the MV customers below ``customer_min_mv_kw`` on the busbar of their nearest station (DESIGN §3.5).

    A customer whose own peak is below ``customer_min_mv_kw`` gets no MV node (eGon-data: MV level above 200 kW
    building peak); the own peak of one building is its ``installed_kw`` after the hall rule (pylovo's law gives
    factor 1 for one unit). It becomes a station-busbar customer of the nearest station node by straight-line
    distance (k-d tree), so it lies in that station's district, if that station is at most
    ``busbar_customer_max_m`` away (pylovo's default greenfield station distance); a farther one keeps an own MV
    node with the flag ``remote_small_customer``. Its MV-level load stays its fixed load
    ``p_fixed_kw = g_c x installed_kw``; the station gets the sum as ``p_fixed_kw`` and the record
    ``load_stats["busbar_customers"]``: ``n``, ``kw`` (fixed load), ``installed_kw``, ``categories``
    (``{category: {"n", "installed_kw", "kw"}}``), ``objectids`` and ``max_distance_m``, plus the flag
    ``busbar_customers``. Without a station node every customer keeps its own node (logged).

    Args:
        stations: :func:`station_table` rows (``x``, ``y``, ``load_stats``, ``p_fixed_kw``, ``flags``).
        customers: :func:`customer_table` rows.
        params: MV parameters (``customer_min_mv_kw``, ``busbar_customer_max_m``).

    Returns:
        ``(stations, customers)``: new frames; ``customers`` keeps the customers with an own MV node. ``attrs`` of
        the customers (for :func:`describe_customers`): ``n_customers``, ``n_halls``, ``lv_kw`` (fixed load of the
        LV installed loads), ``kw`` (after the hall rule), ``n_busbar``, ``busbar_kw``, ``busbar_installed_kw``,
        ``n_busbar_stations``, ``max_busbar_distance_m``, ``n_remote`` and ``remote_kw`` (small customers beyond
        ``busbar_customer_max_m``).
    """
    st = pd.DataFrame(stations).copy()
    cu = pd.DataFrame(customers).copy()
    fixed = _num(cu, "p_fixed_kw").fillna(0.0)
    installed = _num(cu, "installed_kw")
    lv_installed = _num(cu, "lv_installed_kw").fillna(installed)
    attrs = {"n_customers": int(len(cu)),
             "n_halls": int(sum("hall_one_storey" in f for f in _flag_lists(cu))),
             "lv_kw": float((fixed * lv_installed / installed.where(installed > 0)).fillna(0.0).sum()),
             "kw": float(fixed.sum()), "n_busbar": 0, "busbar_kw": 0.0, "busbar_installed_kw": 0.0,
             "n_busbar_stations": 0, "max_busbar_distance_m": 0.0, "n_remote": 0, "remote_kw": 0.0}
    if not len(cu):
        cu.attrs.update(attrs)
        return st, cu
    small = (_num(cu, "installed_kw").fillna(0.0) < params.customer_min_mv_kw).to_numpy()
    xy = st[["x", "y"]].to_numpy(dtype=float) if len(st) else np.zeros((0, 2))
    located = np.isfinite(xy).all(axis=1)
    if not small.any() or not located.any():
        if small.any():
            logger.warning(f"{int(small.sum())} MV customers below {params.customer_min_mv_kw:g} kW keep their own "
                           "MV node: there is no station node")
        cu.attrs.update(attrs)
        return st, cu
    from scipy.spatial import cKDTree

    candidates = np.flatnonzero(located)
    small_rows = np.flatnonzero(small)
    distance, nearest = cKDTree(xy[located]).query(cu.iloc[small_rows][["x", "y"]].to_numpy(dtype=float))
    limit = params.busbar_customer_max_m
    near = np.ones(len(small_rows), dtype=bool) if limit is None else np.asarray(distance) <= float(limit)
    remote = np.zeros(len(cu), dtype=bool)
    remote[small_rows[~near]] = True
    if remote.any():
        cu["flags"] = _flag_lists(cu)
        for i in np.flatnonzero(remote):
            cu["flags"].iloc[i].append("remote_small_customer")
        attrs.update(n_remote=int(remote.sum()), remote_kw=float(fixed[remote].sum()))
    small = small & ~remote
    if not small.any():
        cu.attrs.update(attrs)
        return st, cu
    part = cu[small]
    distance = np.asarray(distance)[near]
    row_of = candidates[np.asarray(nearest, dtype=np.int64)[near]]
    st["load_stats"] = [dict(s) if isinstance(s, Mapping) else {} for s in st.get("load_stats", [None] * len(st))]
    st["p_fixed_kw"] = _num(st, "p_fixed_kw").fillna(0.0)
    st["flags"] = _flag_lists(st)
    for row in np.unique(row_of):
        mine = row_of == row
        group = part[mine]
        categories: dict[str, dict] = {}
        for category, sub in group.groupby("category", sort=True):
            categories[str(category)] = {"n": int(len(sub)), "installed_kw": float(sub["installed_kw"].sum()),
                                         "kw": float(sub["p_fixed_kw"].sum())}
        kw = float(group["p_fixed_kw"].sum())
        st.at[st.index[row], "load_stats"] = {**st["load_stats"].iloc[row], BUSBAR_CUSTOMERS: {
            "n": int(len(group)), "kw": kw, "installed_kw": float(group["installed_kw"].sum()),
            "categories": categories, "objectids": [str(o) for o in group["objectid"]],
            "max_distance_m": float(np.max(distance[mine]))}}
        st.at[st.index[row], "p_fixed_kw"] = float(st["p_fixed_kw"].iloc[row]) + kw
        if BUSBAR_CUSTOMERS not in st["flags"].iloc[row]:
            st["flags"].iloc[row].append(BUSBAR_CUSTOMERS)
    kept = cu[~small].reset_index(drop=True)
    attrs.update(n_busbar=int(small.sum()), busbar_kw=float(part["p_fixed_kw"].sum()),
                 busbar_installed_kw=float(part["installed_kw"].sum()), n_busbar_stations=int(len(np.unique(row_of))),
                 max_busbar_distance_m=float(np.max(distance)))
    kept.attrs.update(attrs)
    return st, kept


def describe_customers(attrs: Mapping[str, Any], params: "MvParameters") -> str:
    """One log line of the MV customers from the ``attrs`` of :func:`busbar_customers` (and ``n_upsized_stations``
    of :func:`upsize_busbar_stations`)."""
    own = attrs.get("n_customers", 0) - attrs.get("n_busbar", 0)
    text = (f"MV customers: {attrs.get('n_customers', 0)} ({attrs.get('lv_kw', 0.0) / 1000:.1f} MW coincident with "
            f"the LV loads, {attrs.get('kw', 0.0) / 1000:.1f} MW with {attrs.get('n_halls', 0)} halls counted one "
            f"storey); {own} own MV nodes ({(attrs.get('kw', 0.0) - attrs.get('busbar_kw', 0.0)) / 1000:.1f} MW), "
            f"{attrs.get('n_busbar', 0)} below {params.customer_min_mv_kw:g} kW at "
            f"{attrs.get('n_busbar_stations', 0)} station busbars ({attrs.get('busbar_kw', 0.0) / 1000:.1f} MW, at "
            f"most {attrs.get('max_busbar_distance_m', 0.0):,.0f} m away)")
    if attrs.get("n_remote"):
        limit = params.busbar_customer_max_m
        text += (f", {attrs['n_remote']} below {params.customer_min_mv_kw:g} kW farther than "
                 f"{limit:,.0f} m from every station keep an own MV node ({attrs.get('remote_kw', 0.0) / 1000:.1f} MW)"
                 if limit is not None else f", {attrs['n_remote']} remote small customers keep an own MV node")
    if attrs.get("n_upsized_stations"):
        text += f"; {attrs['n_upsized_stations']} stations upsized for their busbar customers (MV model only)"
    return text


def upsize_busbar_stations(stations: pd.DataFrame, lv: "LvVersionParameters") -> pd.DataFrame:
    """Give a station that its station-busbar customers overload a larger rating in the MV model (DESIGN §3.5).

    A station with station-busbar customers (:func:`busbar_customers`) is overloaded when its LV coincident peak
    (``p_own_kw``, pylovo's station design load) plus the fixed load of its busbar customers exceeds its rating
    times ``TRANSFORMER_PLANNING_UTILIZATION`` of the LV version (pylovo compares the kW with the kVA). It then
    gets pylovo's rating for that load: the smallest catalogue rating of the LV version for its PLZ settlement type
    above ``load / utilization`` (``TRANSFORMER_MAPPING``, :meth:`LvVersionParameters.station_ratings`), above the
    catalogue n x 630 kVA (:data:`STATION_UNIT_KVA`). The new rating replaces ``sn_kva``; its busbar record keeps
    ``lv_sn_kva`` (the rating of its LV grid), ``upsized_sn_kva`` and ``load_kw``; the flag is
    ``station_busbar_overload``. The LV grid is not changed; ``pylovo-mv couple`` uses the upsized transformer.

    Args:
        stations: Station rows after :func:`busbar_customers` (``sn_kva``, ``p_own_kw``, ``load_stats``,
            ``plz_settlement_type``, ``flags``).
        lv: Parameters of the LV version (``transformer_planning_utilization``, ``transformer_mapping``).

    Returns:
        A new frame; ``attrs['n_upsized']`` counts the upsized stations.
    """
    st = pd.DataFrame(stations).copy()
    st.attrs["n_upsized"] = 0
    if not len(st) or "load_stats" not in st.columns:
        return st
    utilization = float(lv.transformer_planning_utilization)
    rating = _num(st, "sn_kva")
    own = _num(st, "p_own_kw").fillna(0.0)
    settlement = st["plz_settlement_type"] if "plz_settlement_type" in st.columns \
        else pd.Series(None, index=st.index, dtype=object)
    stats = list(st["load_stats"])
    flags = _flag_lists(st)
    sn_kva = rating.to_numpy(dtype=float).copy()
    n = 0
    for i in range(len(st)):
        record = busbar_record(stats[i])
        sn = float(sn_kva[i])
        if not record or not (math.isfinite(sn) and sn > 0):
            continue
        load = float(own.iloc[i]) + float(record.get("kw") or 0.0)
        if load <= sn * utilization * (1.0 + 1e-9):
            continue
        need = load / utilization
        larger = [r for r in lv.station_ratings(settlement.iloc[i]) if r > need]
        new = larger[0] if larger else math.ceil(need / STATION_UNIT_KVA - 1e-9) * STATION_UNIT_KVA
        record.update(lv_sn_kva=sn, upsized_sn_kva=float(new), load_kw=load)
        stats[i] = {**(json.loads(stats[i]) if isinstance(stats[i], str) else dict(stats[i])),
                    BUSBAR_CUSTOMERS: record}
        sn_kva[i] = float(new)
        flags[i].append("station_busbar_overload")
        n += 1
    st["load_stats"] = stats
    st["flags"] = flags
    st["sn_kva"] = sn_kva
    st.attrs["n_upsized"] = n
    return st


def demand_anchor(nodes: pd.DataFrame, population: pd.DataFrame, lv: "LvVersionParameters") -> dict:
    """The regional demand anchor: the coincident MV load per inhabitant (DESIGN §7.2, judge method 4).

    **Load:** pylovo's category law on the summed statistics of all modelled stations plus the fixed loads of the MV
    customer nodes and of the station-busbar customers (the regional coincident peak, not the sum of the district
    peaks). HV customers (``connection_hint`` excluded) are left out and reported as ``excluded_mw``.

    **Population:** ``municipal_register.pop`` per PLZ. The planning region is the union of the PLZ polygons; a PLZ
    whose polygon covers less than :data:`PLZ_FULL_AREA_SHARE` of its register area lies only partly in the region
    (the data extent cuts it): its population counts pro rata to the area share and the anchor is approximate, as
    it is with PLZ without a register row (no population).

    Args:
        nodes: The node table of the variant in NODE_COLUMNS form (``MvResultMixin.variant_node_table``).
        population: Rows of :meth:`MvInputMixin.plz_population`: ``plz``, ``pop``, ``area_km2`` (register) and
            ``polygon_km2``.
        lv: LV version parameters (sim factors).

    Returns:
        JSON-native: ``kw_per_inhabitant`` (None without population), ``coincident_mw``, ``population`` (counted),
        ``n_plz``, ``customer_mw`` and ``n_customer_nodes`` (own MV nodes), ``busbar_customer_mw``,
        ``excluded_mw``, ``approximate``, ``partial_plz`` (``{plz: area share}``) and ``plz_without_population``.
    """
    frame = pd.DataFrame(nodes)
    kind = frame["kind"].astype(object) if len(frame) else pd.Series(dtype=object)
    hint = frame["connection_hint"].astype(object) if len(frame) else pd.Series(dtype=object)
    load = kind.isin(("station", "customer"))
    modelled = frame[load & hint.ne("excluded")]
    installed, units, fixed = node_stats(modelled) if len(modelled) else (np.zeros((0, 4)), np.zeros((0, 4)),
                                                                          np.zeros(0))
    coincident_kw_total = pooled_kw(installed, units, fixed, lv.g()) if len(modelled) else 0.0
    fixed_of = pd.Series(fixed, index=modelled.index)
    customer = modelled["kind"].eq("customer") if len(modelled) else pd.Series(dtype=bool)
    excluded = frame[load & hint.eq("excluded")]
    pop = pd.DataFrame(population)
    people, register, polygon = _num(pop, "pop"), _num(pop, "area_km2"), _num(pop, "polygon_km2")
    share = (polygon / register).where((register > 0) & polygon.notna(), 1.0).clip(upper=1.0)
    cut = people.notna() & (share < PLZ_FULL_AREA_SHARE)
    counted = float((people * share.where(cut, 1.0)).sum(skipna=True)) if len(pop) else 0.0
    partial = {str(int(p)): round(float(s), 4) for p, s in zip(pop.loc[cut, "plz"], share[cut])} if len(pop) else {}
    missing = [int(p) for p in pop.loc[people.isna(), "plz"]] if len(pop) else []
    return {
        "kw_per_inhabitant": coincident_kw_total / counted if counted > 0 else None,
        "coincident_mw": coincident_kw_total / 1000.0,
        "population": round(counted),
        "n_plz": int(len(pop)),
        "customer_mw": float(fixed_of[customer.to_numpy()].sum()) / 1000.0 if len(modelled) else 0.0,
        "n_customer_nodes": int(customer.sum()) if len(modelled) else 0,
        "busbar_customer_mw": float(fixed_of[~customer.to_numpy()].sum()) / 1000.0 if len(modelled) else 0.0,
        "excluded_mw": float(pd.to_numeric(excluded["p_fixed_kw"], errors="coerce").fillna(0.0).sum()) / 1000.0
        if len(excluded) else 0.0,
        "approximate": bool(partial or missing),
        "partial_plz": partial,
        "plz_without_population": missing,
    }


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
