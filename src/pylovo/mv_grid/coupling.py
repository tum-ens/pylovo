"""Coupled MV+LV checks of a stored MV variant: ``pylovo-mv couple`` and the coupled export.

The main path of the MV stage estimates the LV busbar voltage of every station analytically (the voltage budget of
:func:`pylovo.mv_grid.electrical.station_budgets`). This module verifies that estimate with the stored LV nets and
adds the coupled operating point. It runs after ``pylovo-mv generate`` on a done variant and never changes the MV
or LV grids.

1. **Design-point check** (:func:`design_point_check`, one LV net at a time): the stored LV net with its own
   station-coincident loads, the MV/LV tap ``t*`` of the budget and the MV voltage of the budget with the busbar
   at the lower edge of its control band (``V_low = vm_load_pu - busbar_band_pu``) gives
   ``coupled_lv_busbar_vm_pu`` (bus ``LVbus 1``) and ``coupled_lv_min_vm_pu`` (lowest LV bus).
2. **Merged operating point** (:func:`couple_lv_nets`, :func:`run_coupled_cases`): the stored MV net without its
   HV side and with an ideal slack at the lower edge of the busbar band (``busbar_vm_pu - busbar_band_pu``), every
   LV net fused at its MV node by a bulk concat with index offsets (the LV ``MVbus 1`` and ext_grid are dropped and
   the station transformer hangs on the MV node bus), the station equivalents of the MV net removed, the
   station-busbar customers added as fixed loads at ``LVbus 1`` (:func:`add_busbar_customers`), the LV loads scaled per MV feeder
   and consumer category by pylovo's coincidence law (:func:`coupled_scaling`), so the feeder heads carry the loads
   of the MV design net, and the MV/LV taps ``t*``. The LV transformers of a 10 kV district (``mv_grid.vn_kv``,
   e.g. a dense district of ``MV_NOMINAL_VOLTAGE_KV`` auto) are re-typed to 10 kV (``vn_hv_kv``). Result per
   station: ``coupled_op_lv_busbar_vm_pu``; the difference of the MV node voltages to the MV-only net at the same
   busbar voltage is a consistency diagnostic in ``mv_grid.metrics``.

The helpers are pure (pandapower, pandas and the MV stage modules; no database or configuration). The drivers
:func:`run_coupling` and :func:`export_coupled_net` read the variant, the stored MV nets and the streamed LV nets
(``MvInputMixin.iter_lv_nets``) through a database client and store the results with
``MvResultMixin.save_coupled_results`` (one commit per district). The merged net is never stored;
:func:`export_coupled_net` rebuilds it for one district.
"""
from __future__ import annotations

import copy
import functools
import importlib
import logging
import math
import multiprocessing
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandapower as pp
import pandas as pd
from pandapower.auxiliary import LoadflowNotConverged
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from pylovo.mv_grid import coincidence, electrical
from pylovo.mv_grid.inputs import busbar_record
from pylovo.mv_grid.model import CASES, CATEGORIES, GridOutcome
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError, check_stage_version

logger = logging.getLogger(__name__)

#: Memory of one coupling worker (GB): the LV frames of a district and the merged net.
WORKER_GB: float = 1.0
#: Default number of coupling workers (``pylovo-mv couple --jobs``).
DEFAULT_JOBS: int = 2
#: LV nets fetched per query (``MvInputMixin.iter_lv_nets``).
LV_NET_CHUNK: int = 50
#: Tables of an LV net that the merge copies (:func:`lv_frames`).
FRAME_TABLES: tuple[str, ...] = ("bus", "line", "trafo", "load")
#: Bus references of the copied tables.
_BUS_REFS: dict[str, tuple[str, ...]] = {"line": ("from_bus", "to_bus"), "trafo": ("hv_bus", "lv_bus"),
                                         "load": ("bus",)}
#: Bus kinds of the MV part of a net (``bus.kind`` of the electrical module); coupled LV buses get ``lv``.
MV_BUS_KINDS: tuple[str, ...] = ("busbar", "station", "customer", "generator", "joint")
#: ``mv_node`` columns written by the coupling (``MvResultMixin.save_coupled_results``).
COUPLED_COLUMNS: tuple[str, ...] = ("coupled_lv_busbar_vm_pu", "coupled_lv_min_vm_pu", "coupled_op_lv_busbar_vm_pu")
#: Columns of the station table of :func:`couple_district` (one row per station node of a district).
STATION_COLUMNS: tuple[str, ...] = ("mv_node_id", "grid_result_id", "vm_load_pu", "mv_lv_tap_pos")
#: Optional analytic budget columns of the station table, compared with the coupled values in the metrics.
BUDGET_COLUMNS: tuple[str, ...] = ("lv_busbar_vm_pu", "lv_min_vm_pu")
#: Columns of :func:`run_coupled_cases`.
OPERATING_COLUMNS: tuple[str, ...] = ("op_mv_vm_pu", "op_lv_busbar_vm_pu", "op_lv_min_vm_pu")
#: Agreement of the analytic and the coupled LV busbar voltage that the metrics count (p.u.).
AGREEMENT_PU: float = 0.005
#: ``load`` column that marks the station-busbar customers of the merged net (fixed loads, never scaled).
BUSBAR_CUSTOMER_COLUMN: str = "busbar_customer"
#: Shortest LV line of a coupled net (km). Stored LV nets can hold lines of a few micrometres (two buses at one
#: point), whose admittance stops Newton-Raphson; they get 1 m, which changes no voltage noticeably.
MIN_LINE_KM: float = 0.001
#: Station ratings that pylovo builds as one unit, and as two units of half the rating; any other rating is
#: ``int(rating / 630)`` units of 630 kVA (``cable_installer.create_transformer``).
SINGLE_UNIT_KVA: tuple[float, ...] = (100.0, 160.0, 250.0, 400.0, 630.0)
DOUBLE_UNIT_KVA: tuple[float, ...] = (500.0, 800.0, 1260.0)


# ---------------------------------------------------------------------------------------------- small helpers
def _finite(value: Any) -> float | None:
    """``value`` as a float, or None if it is missing or not finite (JSON-native metrics)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: Any, default: float = math.nan) -> float:
    number = _finite(value)
    return default if number is None else number


def _runpp(net: pp.pandapowerNet) -> bool:
    """Newton-Raphson as in the electrical module, then Iwamoto-damped Newton-Raphson if that fails;
    non-convergence is a result, not an error."""
    for algorithm in ("nr", "iwamoto_nr"):
        try:
            pp.runpp(net, algorithm=algorithm, init="auto")
        except LoadflowNotConverged:
            continue
        if net.converged:
            return True
    return False


def _min_line_length(lines: pd.DataFrame) -> pd.DataFrame:
    """``lines`` with every ``length_km`` below :data:`MIN_LINE_KM` raised to it (in place; returned)."""
    if len(lines) and "length_km" in lines.columns:
        lines["length_km"] = lines["length_km"].astype(float).clip(lower=MIN_LINE_KM)
    return lines


def operating_busbar_vm(params: MvParameters) -> float:
    """Busbar voltage of the coupled nets: the lower edge of the OLTC band (``busbar_vm_pu - busbar_band_pu``)."""
    return float(params.busbar_vm_pu) - float(params.busbar_band_pu)


def station_buses(lv_net: pp.pandapowerNet) -> tuple[int, int]:
    """``(mv_bus, lv_busbar)`` of a pylovo LV net: the HV and LV bus of its station transformer.

    In a stored pylovo net these are ``MVbus 1`` (with the ext_grid) and ``LVbus 1``.

    Raises:
        ValueError: The net has no transformer, or its transformers do not share one HV and one LV bus.
    """
    return _trafo_buses(lv_net.trafo)


def _trafo_buses(trafo: pd.DataFrame) -> tuple[int, int]:
    if not len(trafo):
        raise ValueError("the LV net has no station transformer")
    hv = pd.unique(trafo["hv_bus"].astype(np.int64))
    lv = pd.unique(trafo["lv_bus"].astype(np.int64))
    if len(hv) != 1 or len(lv) != 1:
        raise ValueError(f"the LV net has transformers on several buses (HV {list(hv)}, LV {list(lv)})")
    return int(hv[0]), int(lv[0])


# ---------------------------------------------------------------------------------------------- upsized stations
def station_units(sn_kva: float) -> tuple[float, int]:
    """``(unit kVA, units)`` of a station rating as pylovo builds its transformer (:data:`SINGLE_UNIT_KVA`,
    :data:`DOUBLE_UNIT_KVA`, else ``int(rating / 630)`` units, at least one, of ``rating / units`` each, so the
    units always total the rating)."""
    rating = float(sn_kva)
    if any(math.isclose(rating, r) for r in SINGLE_UNIT_KVA):
        return rating, 1
    if any(math.isclose(rating, r) for r in DOUBLE_UNIT_KVA):
        return rating / 2.0, 2
    units = max(1, int(rating / 630.0 + 1e-9))
    return rating / units, units


@functools.lru_cache(maxsize=1)
def _trafo_types() -> dict[str, dict]:
    """pandapower's standard transformer types."""
    return pp.create_empty_network().std_types["trafo"]


def _unit_type(unit_kva: float) -> tuple[str, dict]:
    """Name and data of the pandapower 20/0.4 kV type of a station unit (pylovo's names). A unit without its own
    type takes the next larger 20/0.4 kV type (below 250 kVA :data:`SMALL_UNIT_BASE`, as pylovo's backend) with the
    rating replaced and the no-load losses scaled."""
    types = _trafo_types()
    name = f"{unit_kva / 1000.0:g} MVA 20/0.4 kV"
    if name in types:
        return name, dict(types[name])
    candidates = sorted((float(d["sn_mva"]), n) for n, d in types.items()
                        if float(d["vn_hv_kv"]) == 20.0 and float(d["vn_lv_kv"]) == 0.4)
    larger = [n for sn, n in candidates if sn * 1000.0 >= unit_kva]
    base = dict(types[larger[0] if larger else candidates[-1][1]])
    scale = unit_kva / 1000.0 / float(base["sn_mva"])
    return name, {**base, "sn_mva": unit_kva / 1000.0, "pfe_kw": float(base["pfe_kw"]) * scale}


def upsize_station_transformer(lv_net: pp.pandapowerNet, sn_kva: float) -> pp.pandapowerNet:
    """A copy of a stored LV net with the station transformer of the rating ``sn_kva``: the rating of a station
    that the MV model upsized for its station-busbar customers (``inputs.upsize_busbar_stations``).

    The transformer becomes pylovo's units of that rating (:func:`station_units`) of the 20/0.4 kV type of the unit
    (:func:`_unit_type`): ``std_type``, ``sn_mva``, ``vk_percent``, ``vkr_percent``, ``pfe_kw``, ``i0_percent``
    and ``parallel`` change; its buses, tap settings and ``vn_hv_kv`` stay. A net with several transformer rows keeps
    the first one.

    Raises:
        ValueError: The net has no station transformer or the rating is not positive.
    """
    station_buses(lv_net)
    if not (math.isfinite(float(sn_kva)) and float(sn_kva) > 0):
        raise ValueError(f"a station rating must be positive, got {sn_kva!r}")
    unit_kva, parallel = station_units(sn_kva)
    name, data = _unit_type(unit_kva)
    net = copy.deepcopy(lv_net)
    first = net.trafo.index[0]
    net.trafo = net.trafo.loc[[first]].copy()
    for column in ("sn_mva", "vk_percent", "vkr_percent", "pfe_kw", "i0_percent"):
        net.trafo.loc[first, column] = float(data[column])
    net.trafo.loc[first, "std_type"] = name
    net.trafo.loc[first, "parallel"] = int(parallel)
    return net


def upsized_ratings(stations: pd.DataFrame) -> dict[int, float]:
    """``{grid_result_id: kVA}`` of the stations that the MV model upsized for their station-busbar customers
    (``upsized_sn_kva`` of the busbar record in ``load_stats``)."""
    if "load_stats" not in stations.columns or "grid_result_id" not in stations.columns:
        return {}
    out: dict[int, float] = {}
    for gid, stats in zip(pd.to_numeric(stations["grid_result_id"], errors="coerce"), stations["load_stats"]):
        rating = _finite(busbar_record(stats).get("upsized_sn_kva"))
        if rating is not None and rating > 0 and pd.notna(gid):
            out[int(gid)] = rating
    return out


# ---------------------------------------------------------------------------------------------- design point
def design_point_check(lv_net: pp.pandapowerNet, vm_low: float, tap: int, *,
                       busbar_loads: Mapping[str, float] | None = None,
                       power_factor: float = 1.0) -> dict[str, float]:
    """The LV verdict of one station at its design point; ``lv_net`` is not changed.

    Lines shorter than :data:`MIN_LINE_KM` get that length in the check's copy. The MV side (ext_grid) at
    ``vm_low``, the station transformer at ``tap``, the stored station-coincident LV loads and fixed station-busbar
    customer loads give ``coupled_lv_busbar_vm_pu`` (``LVbus 1``, the LV side of the station transformer) and
    ``coupled_lv_min_vm_pu`` (lowest LV bus).

    Args:
        lv_net: A stored pylovo LV net (pruned to bus, line, trafo, load and ext_grid is enough).
        vm_low: MV voltage of the station in the load case with the busbar at its lower band edge (NaN: skipped).
        tap: MV/LV tap position (HV side; a negative tap raises the LV side).
        busbar_loads: Fixed station-busbar customer kW by category, added to the check's copy of the LV net.
        power_factor: Power factor of those customer loads.

    Returns:
        ``coupled_lv_busbar_vm_pu``, ``coupled_lv_min_vm_pu`` (NaN when skipped or not converged) and
        ``design_converged`` (1.0, 0.0, or NaN when skipped).

    Raises:
        ValueError: The net has no station transformer.
    """
    mv_bus, lv_busbar = station_buses(lv_net)
    out = {"coupled_lv_busbar_vm_pu": math.nan, "coupled_lv_min_vm_pu": math.nan, "design_converged": math.nan}
    net = copy.deepcopy(lv_net)
    _min_line_length(net.line)
    if busbar_loads:
        categories = [(str(category), float(kw)) for category, kw in sorted(busbar_loads.items())
                      if float(kw) > 0]
        if categories:
            p_mw = np.array([kw for _, kw in categories]) / 1000.0
            pp.create_loads(net, buses=[lv_busbar] * len(categories), p_mw=p_mw,
                            q_mvar=p_mw * coincidence.tan_phi(float(power_factor)),
                            name=[f"Busbar customers {category}" for category, _ in categories], scaling=1.0)
    if not len(net.ext_grid):
        pp.create_ext_grid(net, mv_bus, vm_pu=1.0, name="External grid")
    net.trafo["tap_pos"] = float(tap)
    lv_buses = net.bus.index[net.bus.index != mv_bus]
    if math.isfinite(_number(vm_low)):
        net.ext_grid["vm_pu"] = float(vm_low)
        converged = _runpp(net)
        out["design_converged"] = float(converged)
        if converged:
            vm = net.res_bus["vm_pu"]
            out["coupled_lv_busbar_vm_pu"] = float(vm.at[lv_busbar])
            out["coupled_lv_min_vm_pu"] = float(vm.loc[lv_buses].min())
    return out


# ---------------------------------------------------------------------------------------------- merge
def lv_frames(lv_net: pp.pandapowerNet, grid_result_id: int) -> dict[str, pd.DataFrame]:
    """The bus, line, trafo and load tables of an LV net (copies, with a ``grid_result_id`` column) for the merge.

    Results and the ext_grid are not kept (the LV net is dropped right after); the station transformer's HV bus
    (``MVbus 1``) identifies the bus that :func:`couple_lv_nets` replaces by the MV node. Lines shorter than
    :data:`MIN_LINE_KM` get that length.
    """
    frames = {}
    for table in FRAME_TABLES:
        frame = lv_net[table].copy()
        frame["grid_result_id"] = float(grid_result_id)
        frames[table] = frame
    _min_line_length(frames["line"])
    return frames


def _clear_results(net: pp.pandapowerNet) -> None:
    for key in list(net.keys()):
        value = net[key]
        if key.startswith("res_") and isinstance(value, pd.DataFrame) and len(value):
            net[key] = value.iloc[0:0]
    net["converged"] = False


def _busbars(net: pp.pandapowerNet) -> pd.Index:
    if "kind" in net.bus.columns:
        return net.bus.index[net.bus["kind"].astype(object) == "busbar"]
    return pd.Index(net.ext_grid["bus"].astype(np.int64).unique())


def mv_part(net: pp.pandapowerNet, *, vm_pu: float = 1.0) -> pp.pandapowerNet:
    """A copy of a stored (or design) MV net without its HV side and with an ideal slack at the MV busbar.

    The HV bus (``kind`` ``hv_busbar``), the HV/MV transformers and the HV ext_grid are removed; the MV busbar
    (``kind`` ``busbar``) gets an ext_grid at ``vm_pu`` (an existing one is kept and set). Results are cleared.

    Raises:
        ValueError: The net has no MV busbar.
    """
    out = copy.deepcopy(net)
    kinds = out.bus["kind"].astype(object) if "kind" in out.bus.columns else pd.Series(None, index=out.bus.index,
                                                                                         dtype=object)
    hv = out.bus.index[kinds == "hv_busbar"]
    if len(hv):
        out.trafo = out.trafo[~(out.trafo["hv_bus"].isin(hv) | out.trafo["lv_bus"].isin(hv))]
        out.ext_grid = out.ext_grid[~out.ext_grid["bus"].isin(hv)]
        out.bus = out.bus.drop(index=hv)
    busbars = _busbars(out)
    if not len(busbars):
        raise ValueError("the MV net has no busbar (bus kind 'busbar')")
    _clear_results(out)
    on_busbar = out.ext_grid["bus"].isin(busbars)
    if on_busbar.any():
        out.ext_grid.loc[on_busbar, "vm_pu"] = float(vm_pu)
    else:
        pp.create_ext_grid(out, int(busbars[0]), vm_pu=float(vm_pu), name="MV busbar (ideal OLTC)")
    return out


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate a pandapower table with the LV rows; the columns and dtypes of the first part are restored."""
    base = parts[0]
    rows = [part for part in parts if len(part)]
    if not rows:
        return base
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)     # all-NA columns of some parts
        merged = pd.concat(rows, sort=False) if len(rows) > 1 else rows[0].copy()
    columns = list(base.columns) + [c for c in merged.columns if c not in base.columns]
    merged = merged.reindex(columns=columns)
    for column, dtype in base.dtypes.items():
        if merged[column].dtype == dtype:
            continue
        if pd.api.types.is_bool_dtype(dtype) and merged[column].isna().any():
            continue                                      # NaN would become True
        try:
            merged[column] = merged[column].astype(dtype)
        except (TypeError, ValueError):
            pass
    return merged


def couple_lv_nets(mv_net: pp.pandapowerNet, frames: Mapping[int, tuple[int, dict[str, pd.DataFrame]]],
                   mv_vn_kv: float) -> pp.pandapowerNet:
    """The merged MV+LV net: the LV nets fused at their MV node buses (bulk concat with index offsets).

    The MV part comes first (:func:`mv_part`: HV side removed, ideal slack at the busbar), so its indices, switch
    elements and custom columns stay valid. Per coupled station: the MV station-equivalent load (the load at the
    MV bus with the station's ``grid_result_id``, or named ``Station <grid_result_id>``) is removed; the LV buses
    except the transformer's HV bus (``MVbus 1``) are appended with ``kind`` ``lv`` and the station's
    ``grid_result_id``; lines, transformers and loads follow with their bus references re-mapped, the transformer's
    HV side on the MV node bus. Transformers whose ``vn_hv_kv`` differs from ``mv_vn_kv`` are re-typed to it (10 kV districts; the
    impedances in % are unchanged). LV results and ext_grids are never copied; the pandapower dtypes are restored.

    Args:
        mv_net: The stored MV net (or a design net); not changed.
        frames: ``{mv_bus: (grid_result_id, lv_frames)}``, ``mv_bus`` an index of ``mv_net.bus``.
        mv_vn_kv: Nominal voltage of the MV net in kV.

    Raises:
        ValueError: A frame set has no single station transformer, refers to an unknown bus, or an MV bus does
            not exist.
    """
    net = mv_part(mv_net)
    if not frames:
        return net
    gid_of_bus = {int(bus): int(gid) for bus, (gid, _) in frames.items()}
    missing = sorted(set(gid_of_bus) - set(int(b) for b in net.bus.index))
    if missing:
        raise ValueError(f"MV buses {missing} of the coupled stations are not in the MV net")
    load = net.load
    if len(load):
        expected = load["bus"].astype(np.int64).map(gid_of_bus)
        if "grid_result_id" in load.columns:
            station = pd.to_numeric(load["grid_result_id"], errors="coerce").eq(expected)
        else:
            names = load["name"].astype(str)
            station = pd.Series([pd.notna(e) and n == f"Station {int(e)}" for n, e in zip(names, expected)],
                                index=load.index)
        net.load = load[~station.fillna(False).astype(bool)]

    tables = ("bus", *_BUS_REFS)
    parts = {table: [net[table]] for table in tables}
    nxt = {table: (int(net[table].index.max()) + 1 if len(net[table]) else 0) for table in tables}
    for mv_bus in sorted(frames):
        gid, frame = frames[mv_bus]
        gid = int(gid)
        try:
            hv, lv_busbar = _trafo_buses(frame["trafo"])
        except ValueError as exc:
            raise ValueError(f"LV grid {gid}: {exc}") from exc
        bus = frame["bus"]
        if hv not in bus.index or lv_busbar not in bus.index:
            raise ValueError(f"LV grid {gid}: the station transformer refers to an unknown bus")
        keep = bus.index[bus.index != hv]
        look = pd.Series(np.arange(nxt["bus"], nxt["bus"] + len(keep), dtype=np.int64), index=keep)
        look.loc[hv] = int(mv_bus)
        new_bus = bus.loc[keep].copy()
        new_bus.index = pd.Index(look.loc[keep].to_numpy(dtype=np.int64))
        new_bus["kind"] = "lv"
        new_bus["grid_result_id"] = float(gid)
        parts["bus"].append(new_bus)
        nxt["bus"] += len(new_bus)
        for table, columns in _BUS_REFS.items():
            df = frame[table].copy()
            for column in columns:
                mapped = look.reindex(df[column].astype(np.int64).to_numpy())
                if mapped.isna().any():
                    raise ValueError(f"LV grid {gid}: {table}.{column} refers to an unknown bus")
                df[column] = mapped.to_numpy(dtype=np.int64)
            df.index = pd.Index(np.arange(nxt[table], nxt[table] + len(df), dtype=np.int64))
            df["grid_result_id"] = float(gid)
            nxt[table] += len(df)
            parts[table].append(df)
    for table in tables:
        net[table] = _concat(parts[table])

    trafo = net.trafo
    coupled = pd.to_numeric(trafo["grid_result_id"], errors="coerce").notna()
    retype = coupled & ((trafo["vn_hv_kv"].astype(float) - float(mv_vn_kv)).abs() > 1e-9)
    if retype.any():
        trafo.loc[retype, "vn_hv_kv"] = float(mv_vn_kv)
    net["name"] = f"{mv_net.name} with {len(frames)} LV grids" if getattr(mv_net, "name", "") else \
        f"MV grid with {len(frames)} LV grids"
    return net


# ---------------------------------------------------------------------------------------------- scaling, taps
def mv_feeders(net: pp.pandapowerNet, *, include_open: bool = False) -> pd.Series:
    """Feeder label per MV bus (busbar excepted): the connected components of the in-service MV lines without the
    busbar.

    With ``include_open`` False the lines with an open switch are left out (the feeders of normal operation:
    half-rings with their stubs, dedicated feeders, radials); with True the open points count as closed (the
    (n-1) feeder sets: whole rings). MV buses are those of a kind in :data:`MV_BUS_KINDS`.
    """
    bus = net.bus
    kinds = bus["kind"].astype(object) if "kind" in bus.columns else pd.Series("station", index=bus.index)
    busbars = set(int(b) for b in bus.index[kinds == "busbar"])
    others = [int(b) for b in bus.index[kinds.isin(MV_BUS_KINDS)] if int(b) not in busbars]
    if not others:
        return pd.Series(dtype=np.int64)
    position = pd.Series(np.arange(len(others)), index=others)
    line = net.line
    usable = line["in_service"].astype(bool)
    if not include_open and len(net.switch):
        switch = net.switch
        opened = switch.loc[(switch["et"] == "l") & ~switch["closed"].astype(bool), "element"]
        usable &= ~line.index.isin(opened.astype(np.int64))
    a = position.reindex(line.loc[usable, "from_bus"].astype(np.int64).to_numpy())
    b = position.reindex(line.loc[usable, "to_bus"].astype(np.int64).to_numpy())
    valid = a.notna().to_numpy() & b.notna().to_numpy()
    matrix = coo_matrix((np.ones(int(valid.sum())), (a.to_numpy()[valid].astype(np.int64),
                                                     b.to_numpy()[valid].astype(np.int64))),
                        shape=(len(others), len(others)))
    _, labels = connected_components(matrix, directed=False)
    return pd.Series(labels.astype(np.int64), index=pd.Index(others, dtype=np.int64))


def coupled_scaling(loads: pd.DataFrame, feeder_of_grid: Mapping[int, int], g: Mapping[str, float]) -> np.ndarray:
    """Load scaling per row of an LV load table so each MV feeder carries pylovo's coincident load.

    Per feeder F and category c over the LV loads of the stations of F:
    ``scaling(F, c) = coincident_kw(Σ max_p_mw · 1000, Σ load_units, g_c) / (Σ p_mw · 1000)``, the category law of
    :mod:`pylovo.mv_grid.coincidence`, so ``Σ p_mw · scaling`` over the loads of F and c is the coincident load of
    the category on the feeder.

    Args:
        loads: Load rows with ``grid_result_id``, ``category``, ``max_p_mw`` (installed), ``load_units`` and
            ``p_mw`` (the station-coincident share of the LV design).
        feeder_of_grid: Feeder label per ``grid_result_id``.
        g: Sim factor per category (the LV version's ``sim_factor``).

    Returns:
        One scaling per row; 1.0 for rows of other grids, of categories without a sim factor, and of feeder
        categories without installed load, units or stored load.
    """
    n = len(loads)
    out = np.ones(n)
    if n == 0 or not feeder_of_grid or "grid_result_id" not in loads.columns or "category" not in loads.columns:
        return out
    gid = pd.to_numeric(loads["grid_result_id"], errors="coerce")
    feeder = gid.map({float(k): int(v) for k, v in feeder_of_grid.items()})
    category = loads["category"].astype(object)
    known = [c for c in CATEGORIES if c in g]
    rows = (feeder.notna() & category.isin(known)).to_numpy()
    if not rows.any():
        return out

    def column(name: str) -> np.ndarray:
        values = pd.to_numeric(loads[name], errors="coerce").to_numpy(dtype=float)[rows] if name in loads.columns \
            else np.zeros(int(rows.sum()))
        return np.where(np.isfinite(values), values, 0.0)

    frame = pd.DataFrame({"feeder": feeder.to_numpy()[rows].astype(np.int64),
                          "category": category.to_numpy()[rows],
                          "installed_kw": 1000.0 * column("max_p_mw"), "units": column("load_units"),
                          "p_kw": 1000.0 * column("p_mw")})
    sums = frame.groupby(["feeder", "category"], sort=True)[["installed_kw", "units", "p_kw"]].sum()
    slot = np.array([CATEGORIES.index(c) for c in sums.index.get_level_values("category")])
    installed = np.zeros((len(sums), len(CATEGORIES)))
    units = np.zeros_like(installed)
    installed[np.arange(len(sums)), slot] = sums["installed_kw"].to_numpy()
    units[np.arange(len(sums)), slot] = sums["units"].to_numpy()
    factors = np.array([float(g.get(c, 1.0)) for c in CATEGORIES])
    coincident = coincidence.category_kw(installed, units, factors)[np.arange(len(sums)), slot]
    stored = sums["p_kw"].to_numpy()
    valid = (sums["installed_kw"].to_numpy() > 0) & (sums["units"].to_numpy() > 0) & (stored > 0)
    scaling = pd.Series(np.where(valid, coincident / np.where(valid, stored, 1.0), 1.0), index=sums.index)
    keys = pd.MultiIndex.from_arrays([frame["feeder"], frame["category"]])
    out[rows] = scaling.reindex(keys).to_numpy(dtype=float)
    return out


def coupled_feeders(net: pp.pandapowerNet, *, include_open: bool = False) -> dict[int, int]:
    """Feeder label per coupled ``grid_result_id`` (the feeder of the MV bus its station transformer hangs on)."""
    trafo = net.trafo
    if not len(trafo) or "grid_result_id" not in trafo.columns:
        return {}
    gid = pd.to_numeric(trafo["grid_result_id"], errors="coerce")
    feeders = mv_feeders(net, include_open=include_open)
    out: dict[int, int] = {}
    for g, hv in zip(gid[gid.notna()], trafo.loc[gid.notna(), "hv_bus"].astype(np.int64)):
        label = feeders.get(int(hv))
        if label is not None and int(g) not in out:
            out[int(g)] = int(label)
    return out


def scale_lv_loads(net: pp.pandapowerNet, g: Mapping[str, float], *, include_open: bool = False) -> None:
    """Set ``net.load.scaling`` of the coupled LV loads by :func:`coupled_scaling` over the MV feeders.

    The station-busbar customers (:data:`BUSBAR_CUSTOMER_COLUMN`) are fixed loads like the MV customers: they keep
    their scaling and do not enter the feeder sums.
    """
    load = net.load
    if not len(load) or "grid_result_id" not in load.columns:
        return
    feeder_of_grid = coupled_feeders(net, include_open=include_open)
    busbar = load[BUSBAR_CUSTOMER_COLUMN].eq(True).to_numpy() if BUSBAR_CUSTOMER_COLUMN in load.columns \
        else np.zeros(len(load), dtype=bool)
    lv_rows = pd.to_numeric(load["grid_result_id"], errors="coerce").isin(list(map(float, feeder_of_grid))) \
        & ~busbar
    if "category" in load.columns:
        lv_rows &= load["category"].notna()
    scaling = np.ones(len(load))
    scaling[~busbar] = coupled_scaling(load[~busbar], feeder_of_grid, g)
    net.load["scaling"] = np.where(lv_rows.to_numpy(), scaling, load["scaling"].to_numpy(dtype=float))


def busbar_customer_loads(stations: pd.DataFrame) -> dict[int, dict[str, float]]:
    """The fixed load per category of the station-busbar customers of each station.

    Args:
        stations: Station rows with ``grid_result_id`` and ``load_stats`` (a dict or its JSON text, as stored in
            ``mv_node``); a frame without ``load_stats`` has none.

    Returns:
        ``{grid_result_id: {category: kW}}`` of the stations with station-busbar customers.
    """
    if "load_stats" not in stations.columns or "grid_result_id" not in stations.columns:
        return {}
    out: dict[int, dict[str, float]] = {}
    for gid, stats in zip(pd.to_numeric(stations["grid_result_id"], errors="coerce"), stations["load_stats"]):
        categories = busbar_record(stats).get("categories") or {}
        kw = {str(c): float(v.get("kw") or 0.0) for c, v in categories.items() if isinstance(v, Mapping)}
        kw = {c: value for c, value in kw.items() if value > 0}
        if kw and pd.notna(gid):
            out[int(gid)] = kw
    return out


def add_busbar_customers(net: pp.pandapowerNet, loads: Mapping[int, Mapping[str, float]],
                         power_factor: float) -> float:
    """Add the station-busbar customers as fixed loads at the LV busbar of their coupled station.

    One load per station and category at the LV side of the station transformer (the ``trafo`` rows with the
    station's ``grid_result_id``): ``p_mw = kW / 1000``, ``q_mvar = p_mw * tan(arccos(power_factor))``, named
    ``Busbar customers <grid_result_id> <category>``, with ``grid_result_id``, ``category`` and
    :data:`BUSBAR_CUSTOMER_COLUMN` True. Their MV-level load is fixed (``g_c x installed``), so
    :func:`scale_lv_loads` leaves them unscaled. Stations that are not coupled into ``net`` are skipped: their MV
    station equivalent carries these loads.

    Returns:
        The added load in kW.
    """
    trafo = net.trafo
    if not loads or not len(trafo) or "grid_result_id" not in trafo.columns:
        return 0.0
    gid = pd.to_numeric(trafo["grid_result_id"], errors="coerce")
    lv_bus = {int(g): int(b) for g, b in zip(gid[gid.notna()], trafo.loc[gid.notna(), "lv_bus"])}
    rows = [(lv_bus[int(g)], int(g), str(c), float(kw)) for g, by_category in sorted(loads.items())
            if int(g) in lv_bus for c, kw in sorted(by_category.items()) if float(kw) > 0]
    if not rows:
        return 0.0
    p_mw = np.array([kw for *_, kw in rows]) / 1000.0
    start = int(net.load.index.max()) + 1 if len(net.load) else 0
    index = list(range(start, start + len(rows)))
    pp.create_loads(net, buses=[bus for bus, *_ in rows], p_mw=p_mw,
                    q_mvar=p_mw * coincidence.tan_phi(float(power_factor)),
                    name=[f"Busbar customers {g} {c}" for _, g, c, _ in rows], index=index)
    net.load.loc[index, "grid_result_id"] = [float(g) for _, g, _, _ in rows]
    net.load.loc[index, "category"] = [c for _, _, c, _ in rows]
    net.load.loc[index, BUSBAR_CUSTOMER_COLUMN] = True
    return float(p_mw.sum() * 1000.0)


def set_lv_taps(net: pp.pandapowerNet, taps: Mapping[int, int]) -> None:
    """Set the MV/LV tap position of the coupled station transformers (``{grid_result_id: tap}``)."""
    trafo = net.trafo
    if not len(trafo) or "grid_result_id" not in trafo.columns or not taps:
        return
    gid = pd.to_numeric(trafo["grid_result_id"], errors="coerce")
    value = gid.map({float(k): float(v) for k, v in taps.items()})
    rows = value.notna()
    if rows.any():
        net.trafo.loc[rows, "tap_pos"] = value[rows].astype(float)


def build_coupled_net(mv_net: pp.pandapowerNet, frames: Mapping[int, tuple[int, dict[str, pd.DataFrame]]],
                      taps: Mapping[int, int], lv: LvVersionParameters, mv_vn_kv: float, *,
                      include_open: bool = False,
                      busbar_loads: Mapping[int, Mapping[str, float]] | None = None) -> pp.pandapowerNet:
    """:func:`couple_lv_nets`, the station-busbar customers (:func:`add_busbar_customers`, ``busbar_loads`` of
    :func:`busbar_customer_loads`), the taps ``t*`` (:func:`set_lv_taps`) and the feeder scaling
    (:func:`scale_lv_loads` over the normal feeders, or the (n-1) feeder sets with ``include_open``)."""
    net = couple_lv_nets(mv_net, frames, mv_vn_kv)
    if busbar_loads:
        add_busbar_customers(net, busbar_loads, lv.power_factor)
    set_lv_taps(net, taps)
    scale_lv_loads(net, lv.sim_factor, include_open=include_open)
    return net


# ---------------------------------------------------------------------------------------------- operating point
def run_coupled_cases(net: pp.pandapowerNet, params: MvParameters) -> pd.DataFrame:
    """The merged operating point: the load case on a copy of the coupled net
    (``electrical.case_net``) with the busbar at its band edge (:func:`operating_busbar_vm`).

    Returns:
        One row per coupled ``grid_result_id`` with :data:`OPERATING_COLUMNS`: ``op_mv_vm_pu`` (the station's MV
        node), ``op_lv_busbar_vm_pu`` (``LVbus 1``) and ``op_lv_min_vm_pu`` (lowest LV bus); NaN where the case did
        not converge. ``attrs``: ``converged`` and ``slack_p_mw`` of the case (``{"load": ...}``), and ``mv_vm_pu``
        (load case voltages of the MV buses, by bus index).
    """
    trafo = net.trafo
    gid = pd.to_numeric(trafo["grid_result_id"], errors="coerce") if "grid_result_id" in trafo.columns else \
        pd.Series(np.nan, index=trafo.index)
    stations = trafo.loc[gid.notna(), ["hv_bus", "lv_bus"]].astype(np.int64)
    stations.index = gid[gid.notna()].astype(np.int64).to_numpy()
    stations = stations[~stations.index.duplicated()].sort_index()
    result = pd.DataFrame(np.nan, index=pd.Index(stations.index, name="grid_result_id"),
                          columns=list(OPERATING_COLUMNS))
    bus_gid = pd.to_numeric(net.bus["grid_result_id"], errors="coerce") if "grid_result_id" in net.bus.columns else \
        pd.Series(np.nan, index=net.bus.index)
    lv_rows = bus_gid.notna()
    slack: float | None = None
    mv_vm = pd.Series(dtype=float)
    current = electrical.case_net(net, "load", params, busbar_vm_pu=operating_busbar_vm(params))
    converged = _runpp(current)
    if converged:
        slack = _finite(current.res_ext_grid["p_mw"].sum())
        vm = current.res_bus["vm_pu"]
        busbar_vm = pd.Series(vm.reindex(stations["lv_bus"].to_numpy()).to_numpy(), index=stations.index)
        lv_vm = vm[lv_rows]
        grouped = lv_vm.groupby(bus_gid[lv_rows].astype(np.int64))
        result["op_mv_vm_pu"] = pd.Series(vm.reindex(stations["hv_bus"].to_numpy()).to_numpy(), index=stations.index)
        result["op_lv_busbar_vm_pu"] = busbar_vm
        result["op_lv_min_vm_pu"] = grouped.min().reindex(result.index)
        mv_vm = vm[~lv_rows].copy()
    result.attrs["converged"] = {"load": converged}
    result.attrs["slack_p_mw"] = {"load": slack}
    result.attrs["mv_vm_pu"] = mv_vm
    return result


# ---------------------------------------------------------------------------------------------- one district
@dataclass
class CoupledDistrict:
    """The coupling result of one district: ``mv_node`` rows, ``coupled_status`` and the metrics."""

    rows: pd.DataFrame                     # mv_node_id + COUPLED_COLUMNS, one row per station
    status: str                            # done | partial
    metrics: dict
    net: Any = None                        # the merged net (only with keep_net)


def _station_table(stations: pd.DataFrame) -> pd.DataFrame:
    """The station rows (``kind`` station if the column exists) indexed by ``mv_node_id`` with numeric columns."""
    table = pd.DataFrame(stations).copy()
    if "kind" in table.columns:
        table = table[table["kind"].astype(object) == "station"]
    missing = [c for c in STATION_COLUMNS if c not in table.columns]
    if missing:
        raise ValueError(f"the station table has no column {', '.join(missing)}")
    for column in (*STATION_COLUMNS, *BUDGET_COLUMNS):
        if column in table.columns:
            table[column] = pd.to_numeric(table[column], errors="coerce")
        elif column in BUDGET_COLUMNS:
            table[column] = np.nan
    table = table.dropna(subset=["mv_node_id"])
    table.index = pd.Index(table["mv_node_id"].astype(np.int64), name="mv_node_id")
    return table.sort_index()


def _node_buses(net: pp.pandapowerNet) -> dict[int, int]:
    """Bus index per ``mv_node_id`` of an MV net (column ``mv_node_id``, else ``node_key`` > 0)."""
    bus = net.bus
    out: dict[int, int] = {}
    for column in ("node_key", "mv_node_id"):           # mv_node_id wins (joints get ids at persistence)
        if column in bus.columns:
            values = pd.to_numeric(bus[column], errors="coerce")
            for index, value in values[values > 0].items():
                out[int(value)] = int(index)
    return out


def _diff_stats(coupled: pd.Series, analytic: pd.Series) -> dict:
    both = coupled.notna() & analytic.notna()
    diff = (coupled[both] - analytic[both]).astype(float)
    if not len(diff):
        return {"n": 0, "max_abs_diff_pu": None, "mean_diff_pu": None, "share_within": None}
    return {"n": int(len(diff)), "max_abs_diff_pu": _finite(diff.abs().max()), "mean_diff_pu": _finite(diff.mean()),
            "share_within": _finite((diff.abs() < AGREEMENT_PU).mean())}


def couple_district(stored_net: pp.pandapowerNet, stations: pd.DataFrame,
                    lv_nets: Iterable[tuple[int, pp.pandapowerNet]], params: MvParameters, lv: LvVersionParameters,
                    *, vn_kv: float, merged: bool = True, keep_net: bool = False,
                    log: logging.Logger | None = None) -> CoupledDistrict:
    """Couple the stations of one district with their LV nets (pure).

    Every LV net is checked at its design point (:func:`design_point_check`, MV voltages and taps from the station
    table) and reduced to its frames (:func:`lv_frames`) at once, so one LV net is in memory at a time; the LV net of
    a station upsized in the MV model for its station-busbar customers gets the upsized transformer first
    (:func:`upsized_ratings`, :func:`upsize_station_transformer`). With
    ``merged`` the frames are fused under the MV net (:func:`build_coupled_net`) and the operating point is solved
    (:func:`run_coupled_cases`); the MV-only net at the same busbar voltage is the reference of the consistency
    diagnostic. Stations without an LV net keep their MV equivalent (``not_coupled``).

    Args:
        stored_net: The stored MV net of the district (or a design net); not changed.
        stations: One row per station node: :data:`STATION_COLUMNS`, optionally :data:`BUDGET_COLUMNS` (the
            analytic budget, compared in the metrics) and ``kind`` (other kinds are ignored).
        lv_nets: ``(grid_result_id, net)`` pairs, e.g. ``MvInputMixin.iter_lv_nets``.
        params: The MV parameters of the variant.
        lv: The LV version parameters (sim factors of the feeder scaling).
        vn_kv: Nominal MV voltage of the district.
        merged: Solve the merged operating point.
        keep_net: Return the merged net (load-case scaling, taps set, not solved).
        log: Logger for skipped LV nets.

    Returns:
        :class:`CoupledDistrict`: ``rows`` (``mv_node_id`` and :data:`COUPLED_COLUMNS` per station), ``status``
        (``done``, or ``partial`` when a station with an LV grid could not be coupled, lacks a voltage budget,
        has a skipped or non-converging design check, or the merged flow did not converge) and JSON-native
        ``metrics``. Every LV net that loads is merged, also one whose design-point check does not converge on its
        own (it stays in ``design_not_converged``): the MV operating point always carries the real LV grids.
    """
    log = log or logger
    start = time.time()
    table = _station_table(stations)
    band = float(params.busbar_band_pu)
    node_of_grid = {int(g): int(k) for k, g in table["grid_result_id"].dropna().items()}
    bus_of_node = _node_buses(stored_net)
    upsized = upsized_ratings(table)
    busbar_loads = busbar_customer_loads(table)
    checks: dict[int, dict[str, float]] = {}
    frames: dict[int, tuple[int, dict[str, pd.DataFrame]]] = {}
    lists: dict[str, list[int]] = {"unknown_lv_grid": [], "lv_error": [], "no_mv_bus": [],
                                   "lv_stored_not_converged": [], "design_not_converged": [], "no_budget": []}
    for gid, lv_net in lv_nets:
        key = node_of_grid.get(int(gid))
        if key is None:
            lists["unknown_lv_grid"].append(int(gid))
            continue
        row = table.loc[key]
        tap = row["mv_lv_tap_pos"]
        if not math.isfinite(_number(tap)):
            lists["no_budget"].append(key)
            tap = 0
        try:
            if int(gid) in upsized:
                lv_net = upsize_station_transformer(lv_net, upsized[int(gid)])
            check = design_point_check(lv_net, _number(row["vm_load_pu"]) - band, int(tap),
                                       busbar_loads=busbar_loads.get(int(gid)), power_factor=lv.power_factor)
            part = lv_frames(lv_net, int(gid)) if merged else None
        except Exception as exc:     # one broken LV net never stops the district
            log.warning(f"LV grid {gid} (MV node {key}) not coupled: {type(exc).__name__}: {exc}")
            lists["lv_error"].append(key)
            continue
        checks[key] = check
        if getattr(lv_net, "converged", True) is False:
            lists["lv_stored_not_converged"].append(key)
        if check["design_converged"] == 0.0:
            lists["design_not_converged"].append(key)
        if merged:
            bus = bus_of_node.get(key)
            if bus is None:
                lists["no_mv_bus"].append(key)
            else:
                frames[bus] = (int(gid), part)
    with_grid = set(node_of_grid.values())
    not_coupled = sorted(with_grid - set(checks)) + sorted(lists["no_mv_bus"])

    rows = pd.DataFrame({"mv_node_id": table.index.to_numpy(dtype=np.int64)})
    for column in COUPLED_COLUMNS:
        rows[column] = np.nan
    rows = rows.set_index("mv_node_id", drop=False)
    for key, check in checks.items():
        for column in ("coupled_lv_busbar_vm_pu", "coupled_lv_min_vm_pu"):
            rows.at[key, column] = check[column]

    metrics: dict[str, Any] = {
        "n_stations": int(len(table)), "n_with_lv_grid": len(with_grid), "n_checked": len(checks),
        "n_merged": len(frames), "not_coupled": not_coupled,
        "upsized_stations": sorted(node_of_grid[g] for g in upsized if g in node_of_grid),
        **{name: sorted(values) for name, values in lists.items()},
        "design_point": {
            "busbar": _diff_stats(rows["coupled_lv_busbar_vm_pu"], table["lv_busbar_vm_pu"]),
            "minimum": _diff_stats(rows["coupled_lv_min_vm_pu"], table["lv_min_vm_pu"]),
            "min_lv_vm_pu": _finite(rows["coupled_lv_min_vm_pu"].min()),
        },
        "agreement_pu": AGREEMENT_PU,
    }
    status = "partial" if (not_coupled or lists["no_budget"] or lists["design_not_converged"]
                           or any(math.isnan(check["design_converged"]) for check in checks.values())) else "done"
    net = None
    if merged and frames:
        taps = {int(g): int(_number(table.at[node_of_grid[int(g)], "mv_lv_tap_pos"], 0.0))
                for g, _ in frames.values()}
        t0 = time.time()
        busbar = {g: kw for g, kw in busbar_loads.items() if g in taps}
        net = build_coupled_net(stored_net, frames, taps, lv, vn_kv, busbar_loads=busbar)
        frames = {}                                       # the merged net holds the LV rows now
        op = run_coupled_cases(net, params)
        reference = mv_part(stored_net)
        reference = electrical.case_net(reference, "load", params, busbar_vm_pu=operating_busbar_vm(params))
        ref_ok = _runpp(reference)
        op_by_node = op.rename(index={g: node_of_grid[int(g)] for g in op.index if int(g) in node_of_grid})
        rows["coupled_op_lv_busbar_vm_pu"] = op_by_node["op_lv_busbar_vm_pu"].reindex(rows.index)
        mv_vm = op.attrs.get("mv_vm_pu", pd.Series(dtype=float))
        mv_diff: dict[str, Any] = {"n": 0, "max_abs_diff_pu": None, "mean_diff_pu": None}
        if ref_ok and len(mv_vm):
            ref_vm = reference.res_bus["vm_pu"]
            common = mv_vm.index.intersection(ref_vm.index)
            diff = (mv_vm.loc[common] - ref_vm.loc[common]).dropna()
            if len(diff):
                mv_diff = {"n": int(len(diff)), "max_abs_diff_pu": _finite(diff.abs().max()),
                           "mean_diff_pu": _finite(diff.mean())}
        converged = op.attrs.get("converged", {})
        metrics["operating_point"] = {
            "converged": {case: bool(ok) for case, ok in converged.items()},
            "slack_p_mw": op.attrs.get("slack_p_mw", {}),
            "mv_only_slack_p_mw": _finite(reference.res_ext_grid["p_mw"].sum()) if ref_ok else None,
            "mv_vm_vs_mv_only": mv_diff,
            "min_lv_vm_pu": _finite(op["op_lv_min_vm_pu"].min()),
            "busbar_vs_design_point": _diff_stats(rows["coupled_op_lv_busbar_vm_pu"],
                                                  rows["coupled_lv_busbar_vm_pu"]),
            "n_buses": int(len(net.bus)), "n_lines": int(len(net.line)),
            "busbar_customer_mw": sum(sum(kw.values()) for kw in busbar.values()) / 1000.0,
            "runtime_s": round(time.time() - t0, 3),
        }
        if not all(converged.values()):
            status = "partial"
        if not keep_net:
            net = None
    metrics["runtime_s"] = round(time.time() - start, 3)
    metrics["status"] = status
    return CoupledDistrict(rows=rows.reset_index(drop=True), status=status, metrics=metrics, net=net)


# ---------------------------------------------------------------------------------------------- drivers
@dataclass
class CouplingSummary:
    """Outcome of ``pylovo-mv couple`` for one MV variant.

    ``done`` counts the grids with ``coupled_status`` done, ``partial`` those coupled in part, ``failed`` the
    failed ones and the requested grids that are not done in the variant. ``status``: ``done`` when every grid is
    done, ``partial`` when some grid is coupled at least in part, else ``failed``.
    """

    version_id: str
    variant_id: str
    status: str
    grids: int
    done: int
    failed: int
    elapsed_s: float
    partial: int = 0
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """The summary lines (each starts with a check mark when the coupling is done, else with a cross)."""
        mark = "✓" if self.status == "done" else "✗"
        lines = [f"{mark} Coupling of MV variant {self.variant_id} of version {self.version_id}: {self.status}, "
                 f"{self.done} of {self.grids} grids done, {self.partial} partial, {self.failed} failed, "
                 f"{self.elapsed_s:.0f} s"]
        lines.extend(f"✗ {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return asdict(self)


def _config() -> ModuleType:
    return importlib.import_module("pylovo.config_loader")


def _database_client(log_file: str):
    from pylovo.database.database_client import DatabaseClient

    return DatabaseClient(log_file=log_file)


def _logger(name: str, log_file: str) -> logging.Logger:
    from pylovo import utils

    return utils.create_logger(name, log_file, _config().LOG_LEVEL)


def _close_logger(log: logging.Logger) -> None:
    for handler in list(log.handlers):
        handler.close()
    log.handlers.clear()


def _one_line(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:2000]


def _worker_count(jobs: int, n_grids: int) -> int:
    """``min(jobs, n_grids)`` capped by the free memory at :data:`WORKER_GB` per worker (``stage.worker_count``)."""
    stage = importlib.import_module("pylovo.mv_grid.stage")
    return stage.worker_count(max(int(jobs), 1), n_grids, per_worker_gb=WORKER_GB)


def _plain_frame(frame: Any) -> pd.DataFrame:
    """A GeoDataFrame layer as a plain DataFrame without geometry (picklable, light)."""
    df = pd.DataFrame(frame)
    return df.drop(columns=[c for c in ("geom", "geometry") if c in df.columns])


def _variant_inputs(dbc: Any, version_id: str, variant_id: str) -> tuple[dict, MvParameters, dict]:
    """The variant row, its parameters and the LV parameters (``LvVersionParameters.as_json()``).

    Raises:
        LookupError: The variant does not exist.
        MvStageError: The variant was planned with another MV stage version (``check_stage_version``).
    """
    row = dbc.variant_row(version_id, variant_id)
    if row is None:
        raise LookupError(f"MV variant {variant_id} of version {version_id} does not exist")
    check_stage_version(row["mv_parameters"], version_id, variant_id)
    params = MvParameters.from_snapshot(variant_id, row.get("mv_variant_comment") or "", row["mv_parameters"])
    lv_json = row.get("lv_parameters")
    if not lv_json:
        lv_json = LvVersionParameters.from_generation_parameters(version_id, dbc.lv_snapshot(version_id)).as_json()
    LvVersionParameters.from_json(lv_json)               # fails early on an incomplete copy
    return row, params, dict(lv_json)


def _couple_worker(version_id: str, variant_id: str, mv_grid_id: int, params_snapshot: dict, comment: str,
                   lv_json: dict, stations: pd.DataFrame, vn_kv: float, merged: bool, log_file: str) -> GridOutcome:
    """Couple one district and store its results (runs in a worker process, or in process).

    Only picklable arguments; the worker opens its own database connection. It logs to
    ``log_mv_couple_<version>_<variant>_<mv_grid_id>.txt`` next to ``log_file``. Every exception is caught: the
    grid's ``coupled_status`` becomes ``failed`` with the error in ``metrics.coupling``.
    """
    start = time.time()
    worker_log = str(Path(log_file).parent / f"log_mv_couple_{version_id}_{variant_id}_{mv_grid_id}.txt")
    log = _logger(f"MvCouple.{version_id}.{variant_id}.{mv_grid_id}", worker_log)
    dbc = None
    try:
        dbc = _database_client(log_file)
        params = MvParameters.from_snapshot(variant_id, comment, params_snapshot)
        lv = LvVersionParameters.from_json(lv_json)
        stored = dbc.read_mv_grid_net(mv_grid_id)
        gids = sorted({int(g) for g in pd.to_numeric(stations["grid_result_id"], errors="coerce").dropna()})
        lv_nets = dbc.iter_lv_nets(version_id, gids, chunk=LV_NET_CHUNK)
        result = couple_district(stored, stations, lv_nets, params, lv, vn_kv=vn_kv, merged=merged, log=log)
        dbc.rollback_changes()                           # end the read transaction
        runtime = time.time() - start
        result.metrics["worker_runtime_s"] = round(runtime, 3)
        dbc.save_coupled_results(mv_grid_id, result.rows, result.status, result.metrics)
        log.info(f"MV grid {mv_grid_id} coupled ({result.status}): {result.metrics['n_checked']} of "
                 f"{result.metrics['n_stations']} stations, {runtime:.1f} s")
        return GridOutcome(int(mv_grid_id), result.status, None, runtime)
    except Exception as exc:
        runtime = time.time() - start
        error = _one_line(exc)
        log.exception(f"Coupling of MV grid {mv_grid_id} failed: {error}")
        if dbc is not None:
            try:
                dbc.rollback_changes()
                dbc.save_coupled_results(mv_grid_id, pd.DataFrame({"mv_node_id": []}), "failed", {"error": error})
            except Exception as mark_exc:   # the connection may be gone
                log.error(f"MV grid {mv_grid_id}: could not store the failed coupling: {_one_line(mark_exc)}")
        return GridOutcome(int(mv_grid_id), "failed", error, runtime)
    finally:
        if dbc is not None:
            dbc.close()
        _close_logger(log)


def _run_tasks(tasks: Sequence[tuple], n: int, log: logging.Logger, warnings_: list[str]) -> list[GridOutcome]:
    """Run the coupling tasks in a fork pool (``n`` > 1) or in process; one outcome per finished grid."""
    outcomes: list[GridOutcome] = []
    total = len(tasks)

    def report(outcome: GridOutcome) -> None:
        outcomes.append(outcome)
        if outcome.status == "failed":
            log.warning(f"Coupling of MV grid {outcome.mv_grid_id} failed after {outcome.runtime_s:.0f} s "
                        f"({len(outcomes)}/{total}): {outcome.error}")
        else:
            log.info(f"MV grid {outcome.mv_grid_id} coupled ({outcome.status}) in {outcome.runtime_s:.0f} s "
                     f"({len(outcomes)}/{total})")

    if n <= 1 or total <= 1:
        for task in tasks:
            report(_couple_worker(*task))
        return outcomes
    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=n, mp_context=context) as executor:
        futures = {executor.submit(_couple_worker, *task): task[2] for task in tasks}
        try:
            for future in as_completed(futures):
                mv_grid_id = futures[future]
                try:
                    outcome = future.result()
                except BrokenProcessPool as exc:
                    message = (f"the coupling worker pool broke ({_one_line(exc)}; out of memory?): rerun the "
                               "remaining grids with fewer --jobs")
                    log.error(message)
                    warnings_.append(message)
                    for other in futures:
                        other.cancel()
                    break
                except Exception as exc:     # an error outside the worker's own handling (e.g. pickling)
                    outcome = GridOutcome(int(mv_grid_id), "failed", _one_line(exc), 0.0)
                report(outcome)
        except KeyboardInterrupt:
            log.warning("Interrupted: cancelling the remaining couplings")
            for future in futures:
                future.cancel()
            raise
    return outcomes


def run_coupling(version_id: str, variant_id: str, mv_grid_ids: Sequence[int] | None = None, *,
                 jobs: int = DEFAULT_JOBS, merged: bool = True, log_file: str = "log/log.txt") -> CouplingSummary:
    """``pylovo-mv couple``: the LV design-point check and the merged operating point of the done grids of a
    variant, one commit per district.

    Prints the database target, checks the schema, takes the variant lock (907362, so it never runs beside a
    ``generate`` of the same variant), reads the variant and its node results, and couples the done grids (all,
    or ``mv_grid_ids``) in at most ``jobs`` fork workers (memory guard at :data:`WORKER_GB` per worker), largest
    first. Each worker streams the LV nets of its district and stores ``coupled_*`` of its stations,
    ``mv_grid.coupled_status`` and ``metrics.coupling``.

    Args:
        version_id: The LV version.
        variant_id: The MV variant.
        mv_grid_ids: Grids to couple (default: every done grid of the variant).
        jobs: Number of workers (default 2).
        merged: Also solve the merged MV+LV operating point (``--no-merged`` skips it).
        log_file: Log file of the run (workers log next to it).

    Returns:
        The summary; ``status`` is ``done`` when every requested grid is coupled completely.

    Raises:
        MvStageError: Another run of the variant is active, or the variant does not exist.
    """
    start = time.time()
    config = _config()
    log = _logger("MvCouple", log_file)
    print(f"✓ MV coupling on {config.DBNAME}@{config.HOST}:{config.PORT}, version {version_id}, variant {variant_id}")
    dbc = _database_client(log_file)
    locked = False
    try:
        dbc.ensure_grid_persistence_schema()
        if not dbc.try_lock_variant(version_id, variant_id):
            raise MvStageError(f"another MV run of variant {variant_id} of version {version_id} is active")
        locked = True
        dbc.commit_changes()
        return _run_locked(dbc, version_id, variant_id, mv_grid_ids, jobs=jobs, merged=merged, log_file=log_file,
                           log=log, start=start)
    finally:
        if locked:
            try:
                dbc.rollback_changes()
                dbc.unlock_variant(version_id, variant_id)
                dbc.commit_changes()
            except Exception as exc:   # the lock ends with the session anyway
                log.warning(f"Could not release the lock of MV variant {variant_id}: {_one_line(exc)}")
        dbc.close()


def _run_locked(dbc: Any, version_id: str, variant_id: str, mv_grid_ids: Sequence[int] | None, *, jobs: int,
                merged: bool, log_file: str, log: logging.Logger, start: float) -> CouplingSummary:
    try:
        _row, params, lv_json = _variant_inputs(dbc, version_id, variant_id)
    except LookupError as exc:
        raise MvStageError(str(exc)) from exc
    layers = dbc.read_mv_layers(version_id, variant_id)
    dbc.commit_changes()                                 # no open transaction is inherited by the workers
    grids = _plain_frame(layers["grids"])
    nodes = _plain_frame(layers["nodes"])
    status_of = {int(g): str(s) for g, s in zip(grids["mv_grid_id"], grids["status"])}
    vn_of = {int(g): float(v) for g, v in zip(grids["mv_grid_id"], grids["vn_kv"])}
    warnings_: list[str] = []
    if mv_grid_ids is None:
        requested = sorted(g for g, s in status_of.items() if s == "done")
    else:
        requested = sorted({int(g) for g in mv_grid_ids})
        unknown = [g for g in requested if g not in status_of]
        not_done = [g for g in requested if g in status_of and status_of[g] != "done"]
        if unknown:
            warnings_.append(f"MV grids {', '.join(map(str, unknown))} are not in variant {variant_id} of version "
                             f"{version_id}")
        if not_done:
            warnings_.append(f"MV grids {', '.join(map(str, not_done))} are not done (run pylovo-mv generate)")
    todo = [g for g in requested if status_of.get(g) == "done"]
    snapshot = params.to_snapshot()
    stations = nodes[nodes["kind"].astype(object) == "station"]
    tasks = []
    for mv_grid_id in todo:
        part = stations[pd.to_numeric(stations["mv_grid_id"], errors="coerce") == mv_grid_id]
        tasks.append((version_id, variant_id, mv_grid_id, snapshot, params.comment, lv_json,
                      part.reset_index(drop=True), vn_of[mv_grid_id], merged, log_file))
    tasks.sort(key=lambda task: (-len(task[6]), task[2]))
    n = _worker_count(jobs, len(tasks)) if tasks else 0
    log.info(f"Coupling {len(tasks)} MV grids of variant {variant_id} of version {version_id} with {n} "
             f"worker{'s' if n != 1 else ''}" + ("" if merged else " (no merged operating point)"))
    outcomes = _run_tasks(tasks, n, log, warnings_)
    done = sum(1 for o in outcomes if o.status == "done")
    partial = sum(1 for o in outcomes if o.status == "partial")
    failed = len(requested) - done - partial
    grids_total = len(requested)
    if grids_total and done == grids_total:
        status = "done"
    elif done + partial > 0:
        status = "partial"
    else:
        status = "failed"
    if not grids_total:
        warnings_.append(f"MV variant {variant_id} of version {version_id} has no done grids to couple")
    summary = CouplingSummary(version_id=version_id, variant_id=variant_id, status=status, grids=grids_total,
                              done=done, failed=failed, elapsed_s=time.time() - start, partial=partial,
                              warnings=warnings_)
    log.info(summary.describe().splitlines()[0])
    return summary


def export_coupled_net(version_id: str, variant_id: str, mv_grid_id: int, output: Path, case: str = "load") -> Path:
    """Write the merged MV+LV net of one district to a JSON file (``pylovo-mv export --coupled``).

    The net is rebuilt from the stored MV net and the stored LV nets (:func:`build_coupled_net`; the (n-1) cases
    scale the LV loads over whole rings), set to ``case`` with the busbar at its band edge
    (:func:`operating_busbar_vm`) and solved; a case that does not converge is written without results.

    Returns:
        The written path.

    Raises:
        ValueError: Unknown case.
        LookupError: The variant or grid does not exist, or the grid has no stored net.
    """
    if case not in CASES:
        raise ValueError(f"case must be one of {', '.join(CASES)}, got {case!r}")
    output = Path(output)
    with _database_client("log/log.txt") as dbc:
        dbc.ensure_grid_persistence_schema()
        _, params, lv_json = _variant_inputs(dbc, version_id, variant_id)
        layers = dbc.read_mv_layers(version_id, variant_id, int(mv_grid_id))
        grids = _plain_frame(layers["grids"])
        if not len(grids):
            raise LookupError(f"MV grid {mv_grid_id} does not belong to variant {variant_id} of version {version_id}")
        vn_kv = float(grids["vn_kv"].iloc[0])
        table = _station_table(_plain_frame(layers["nodes"]))
        stored = dbc.read_mv_grid_net(int(mv_grid_id))
        node_of_grid = {int(g): int(k) for k, g in table["grid_result_id"].dropna().items()}
        bus_of_node = _node_buses(stored)
        frames = {}
        upsized = upsized_ratings(table)
        for gid, lv_net in dbc.iter_lv_nets(version_id, sorted(node_of_grid), chunk=LV_NET_CHUNK):
            key = node_of_grid.get(int(gid))
            if key is None or key not in bus_of_node:
                continue
            try:
                station_buses(lv_net)
                if int(gid) in upsized:
                    lv_net = upsize_station_transformer(lv_net, upsized[int(gid)])
            except ValueError as exc:
                logger.warning(f"LV grid {gid} not coupled: {exc}")
                continue
            frames[bus_of_node[key]] = (int(gid), lv_frames(lv_net, int(gid)))
        dbc.rollback_changes()
    lv = LvVersionParameters.from_json(lv_json)
    taps = {int(g): int(_number(table.at[k, "mv_lv_tap_pos"], 0.0)) for g, k in node_of_grid.items()}
    net = build_coupled_net(stored, frames, taps, lv, vn_kv, include_open=case in electrical.N1_CASES,
                            busbar_loads=busbar_customer_loads(table))
    net = electrical.case_net(net, case, params, busbar_vm_pu=operating_busbar_vm(params))
    if not _runpp(net):
        logger.warning(f"Case {case} of the coupled MV grid {mv_grid_id} did not converge: written without results")
    output.parent.mkdir(parents=True, exist_ok=True)
    pp.to_json(net, str(output))
    return output
