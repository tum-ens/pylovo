"""Electrical checks of one MV district with pandapower (DESIGN §5): design net, snapshot cases, criteria, repair
loop, HV/MV transformer sizing, stored net with a fixed OLTC tap and the voltage budget of every station.

The module builds MV nets directly with pandapower (not through ``IElectricalBackend``, whose transformer specs are
fixed to 20/0.4 kV). It is pure: it imports no database, configuration or plotting module. The coincidence law and
the equipment helpers come from :mod:`pylovo.mv_grid.coincidence` and :mod:`pylovo.mv_grid.equipment`; the
topology changes of the repair loop come from :mod:`pylovo.mv_grid.topology`, imported on first use.

**Nets.** The *design net* (:func:`build_design_net`) has one bus per node key of the plan (the HV/MV busbar first,
then the nodes of ``DistrictInput.nodes``, then the joints), an ideal slack at the busbar (``busbar_vm_pu``, the
HV/MV OLTC setpoint), one line per planned circuit (ties excepted), one open load-break switch per normally-open
line, one load per station and MV customer, and one static generator per node and technology group with
generation. With an ideal busbar the rings are electrically independent, so the (n-1) cases of all rings run in two
flows (``n1a``: every A exit out, ``n1b``: every B exit out). The *stored net* (:func:`build_stored_net`) adds the
HV side and the HV/MV transformers with a fixed OLTC tap, so a plain ``pp.runpp`` reproduces the load case.

**Custom columns** (read by :func:`case_net`, the metrics and the persistence):

- ``bus``: ``node_key`` (plan key: ``mv_node_id`` > 0 or joint key < 0; NaN for the HV bus of the stored net),
  ``mv_node_id`` (NaN for joints until persistence), ``kind`` (``busbar``, ``station``, ``customer``,
  ``generator``, ``joint``; ``hv_busbar`` for the HV bus);
- ``line``: ``line_key``, ``mv_line_id``, ``role``, ``kind`` (``cable``, ``overhead``, ``mixed``), ``ring_id``,
  ``feeder_id``, ``std_type_name``, ``exit`` (``a`` or ``b``: the line is out of service in ``n1a`` or ``n1b``;
  ring exits and the two legs of a dedicated customer feeder), ``cable_km``, ``overhead_km``;
- ``switch``: ``normally_open``, ``line_key``;
- ``load``: ``node_key``, ``grid_result_id``, ``p_normal_mw``, ``q_normal_mvar`` (normal case, half-ring feeder
  scaling), ``p_n1_mw``, ``q_n1_mvar`` (whole-ring feeder scaling);
- ``sgen``: ``node_key``, ``grid_result_id``, ``technology_group`` (``solar``, ``wind``, ``other``), ``level``
  (``mv``, ``lv``), ``cos_phi`` (under-excited: ``q_mvar = -p_mw * tan(arccos(cos_phi))``).
"""
from __future__ import annotations

import copy
import dataclasses
import functools
import importlib
import logging
import math
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandapower as pp
import pandas as pd
from pandapower.auxiliary import LoadflowNotConverged
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from pylovo.mv_grid import coincidence, equipment
from pylovo.mv_grid.model import (
    CASES,
    CaseResult,
    DistrictInput,
    HvMvType,
    LinePlan,
    LineType,
    StationBudget,
    TopologyPlan,
    Violation,
)

logger = logging.getLogger(__name__)

#: Technology groups of the generation columns ``gen_<group>_kw`` (keys of the feed-in case factors).
GENERATION_GROUPS: tuple[str, ...] = ("solar", "wind", "other")
#: The (n-1) cases and the feed-in cases of :data:`~pylovo.mv_grid.model.CASES`.
N1_CASES: tuple[str, ...] = ("n1a", "n1b")
FEED_IN_CASES: tuple[str, ...] = ("feed_in_wind", "feed_in_pv")
#: Exit (``line.exit``) that is out of service in each (n-1) case.
N1_EXIT: dict[str, str] = {"n1a": "a", "n1b": "b"}
#: Tap corrections of the OLTC loop of :func:`build_stored_net` after the first flow.
OLTC_MAX_ITERATIONS: int = 3
#: Default CRS of node and line coordinates (pylovo's ``TARGET_EPSG`` default).
DEFAULT_EPSG: int = 25832
#: MV/LV station transformers of the stored LV grids, pandapower 20/0.4 kV types used by pylovo's backend
#: ``(sn_kva, vk_percent, vkr_percent)``: 0.25, 0.4 and 0.63 MVA; smaller ratings take the 0.25 MVA values (as the
#: backend does), larger ones the 0.63 MVA values. Percentages hold for 10 kV MV variants too (budgets use
#: percentages only).
STATION_TRANSFORMERS: tuple[tuple[float, float, float], ...] = ((250.0, 6.0, 1.44), (400.0, 6.0, 1.425),
                                                               (630.0, 6.0, 1.206))
#: Comparison tolerance of the criteria (p.u. for voltages, percent for loadings).
TOLERANCE: float = 1e-6
#: Node roles whose node has no (n-1) requirement (DESIGN §5.3).
NOT_N1_ROLES: frozenset[str] = frozenset({"stub", "tee"})
#: Ring kinds (``RingPlan.kind``) that are (n-1) secure.
N1_SECURE_KINDS: frozenset[str] = frozenset({"ring", "dedicated"})


# ---------------------------------------------------------------------------------------------- module access
def _module(name: str) -> Any:
    """A sibling module of the MV stage, imported on first use."""
    return importlib.import_module(f"pylovo.mv_grid.{name}")


def _topology() -> Any:
    return _module("topology")


@functools.cache
def _empty_network_template() -> pp.pandapowerNet:
    return pp.create_empty_network()


def _empty_network(name: str) -> pp.pandapowerNet:
    """A copy of a cached empty pandapower net (``create_empty_network`` takes about 50 ms, a copy 4 ms)."""
    net = copy.deepcopy(_empty_network_template())
    net["name"] = name
    return net


@functools.lru_cache(maxsize=8)
def _to_wgs84(epsg: int) -> Any:
    from pyproj import Transformer

    return Transformer.from_crs(int(epsg), 4326, always_xy=True)


# ---------------------------------------------------------------------------------------------- plan helpers
def busbar_key(inp: DistrictInput) -> int:
    """The node key of the HV/MV busbar: the row of kind ``busbar`` (the first row by contract)."""
    kinds = inp.nodes["kind"]
    busbars = kinds.index[kinds == "busbar"]
    return int(busbars[0]) if len(busbars) else int(inp.nodes.index[0])


def _is_excluded(inp: DistrictInput, plan: TopologyPlan, key: int) -> bool:
    if plan.node_role.get(key) == "excluded":
        return True
    return key in inp.nodes.index and inp.nodes.at[key, "connection_hint"] == "excluded"


def bus_keys(inp: DistrictInput, plan: TopologyPlan) -> list[int]:
    """The node keys that get a bus: the busbar, the nodes that are not excluded (input order), the joints.

    Joints are ordered -1, -2, ... Nodes without any line stay in the net (unsupplied: NaN results).
    """
    busbar = busbar_key(inp)
    keys = [busbar]
    keys += [int(k) for k in inp.nodes.index if int(k) != busbar and not _is_excluded(inp, plan, int(k))]
    keys += sorted((int(k) for k in plan.joints), reverse=True)
    return keys


def _net_lines(plan: TopologyPlan) -> list[LinePlan]:
    """The lines of the design net: every planned line except ties (they never carry flow in the cases)."""
    return [line for line in plan.lines if line.role != "tie"]


def _components(keys: Sequence[int], busbar: int, lines: Iterable[LinePlan], include_open: bool) -> dict[int, int]:
    """Component label per non-busbar key of the line graph without the busbar.

    With ``include_open`` False the normally-open lines are left out: the components are the feeders of normal
    operation (half-rings with their stubs, dedicated feeders, radials). With True they are the (n-1) feeder sets
    (whole rings with their stubs).
    """
    others = [int(k) for k in keys if int(k) != busbar]
    index = {k: i for i, k in enumerate(others)}
    rows, cols = [], []
    for line in lines:
        if line.role == "tie" or (line.normally_open and not include_open):
            continue
        a, b = int(line.from_key), int(line.to_key)
        if a in index and b in index:
            rows.append(index[a])
            cols.append(index[b])
    n = len(others)
    if n == 0:
        return {}
    matrix = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
    _, labels = connected_components(matrix, directed=False)
    return {k: int(labels[i]) for k, i in index.items()}


def _component_rings(plan: TopologyPlan, components: Mapping[int, int]) -> dict[int, int]:
    """Ring id per component label, from the lines that carry a ring id (the first one by line key)."""
    rings: dict[int, int] = {}
    for line in sorted(plan.lines, key=lambda item: item.key):
        if line.role == "tie" or line.ring_id is None:
            continue
        for key in (line.from_key, line.to_key):
            label = components.get(int(key))
            if label is not None:
                rings.setdefault(label, int(line.ring_id))
    return rings


def _ring_exits(plan: TopologyPlan, busbar: int) -> dict[int, str]:
    """``exit`` per line key: ``a``/``b`` for the busbar sections of rings and the two legs of dedicated feeders.

    A line's own ``exit`` is kept. Otherwise a ring's A exit is its busbar line to ``members[0]`` and its B exit
    the busbar line to ``members[-1]`` (one-member rings: the closed line is A, the normally-open line B); the
    closed leg of a 2-leg dedicated feeder is A, the normally-open leg B.
    """
    exits = {int(line.key): line.exit for line in plan.lines if line.exit in ("a", "b") and line.role != "tie"}
    by_ring: dict[int, list[LinePlan]] = {}
    for line in plan.lines:
        if line.role == "tie" or line.ring_id is None or busbar not in (line.from_key, line.to_key):
            continue
        by_ring.setdefault(int(line.ring_id), []).append(line)
    for ring in plan.rings:
        if ring.kind not in N1_SECURE_KINDS:
            continue
        candidates = sorted(by_ring.get(int(ring.ring_id), []), key=lambda item: item.key)
        if len(candidates) < 2 or any(int(line.key) in exits for line in candidates):
            continue
        members = [int(m) for m in ring.members]
        first, last = (members[0], members[-1]) if members else (None, None)
        if ring.kind == "ring" and first is not None and first != last:
            for line in candidates:
                other = line.to_key if line.from_key == busbar else line.from_key
                if other == first:
                    exits.setdefault(int(line.key), "a")
                elif other == last:
                    exits.setdefault(int(line.key), "b")
        else:
            closed = [line for line in candidates if not line.normally_open]
            opened = [line for line in candidates if line.normally_open]
            if closed and opened:
                exits[int(closed[0].key)] = "a"
                exits[int(opened[0].key)] = "b"
            else:
                exits[int(candidates[0].key)] = "a"
                exits[int(candidates[1].key)] = "b"
    return exits


def n1_nodes(inp: DistrictInput, plan: TopologyPlan) -> set[int]:
    """Node keys with an (n-1) requirement: the path members of rings and 2-leg dedicated feeders.

    Stub and T-off nodes, busbar radials and generator feeders have none (DESIGN §5.3).
    """
    out: set[int] = set()
    for ring in plan.rings:
        if ring.kind not in N1_SECURE_KINDS:
            continue
        out.update(int(m) for m in ring.members if plan.node_role.get(int(m)) not in NOT_N1_ROLES)
    out.discard(busbar_key(inp))
    return out


def _value(row: Mapping[str, Any] | pd.Series, column: str, default: float = 0.0) -> float:
    value = row.get(column, default) if hasattr(row, "get") else default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(value) else value


def _tan_phi(cos_phi: float) -> float:
    return coincidence.tan_phi(min(max(float(cos_phi), 1e-6), 1.0))


def _label(nodes: pd.DataFrame, key: int, column: str) -> str:
    """The value of an optional identifier column of a node (e.g. ``grid_result_id``), else the node key."""
    if column in nodes.columns:
        value = nodes.at[key, column]
        if value is not None and not (isinstance(value, float) and math.isnan(value)):
            return str(int(value)) if isinstance(value, (float, np.floating)) and float(value).is_integer() \
                else str(value)
    return str(key)


# ---------------------------------------------------------------------------------------------- loads
def _load_arrays(inp: DistrictInput, keys: Sequence[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Installed kW and units per category (k, 4) and fixed kW (k,) of the node keys (joints: zeros).

    MV customers carry only their fixed load (``p_fixed_kw`` = g_c x installed); their category statistics are
    never pooled into the coincidence law again (DESIGN §3.5).
    """
    nodes = inp.nodes
    installed = np.zeros((len(keys), len(coincidence.INSTALLED_COLUMNS)))
    units = np.zeros_like(installed)
    fixed = np.zeros(len(keys))
    for i, key in enumerate(keys):
        if key not in nodes.index:
            continue
        row = nodes.loc[key]
        fixed[i] = max(_value(row, "p_fixed_kw"), 0.0)
        if row["kind"] == "customer":
            continue
        installed[i] = [max(_value(row, c), 0.0) for c in coincidence.INSTALLED_COLUMNS]
        units[i] = [max(_value(row, c), 0.0) for c in coincidence.UNITS_COLUMNS]
    return installed, units, fixed


def node_loads(inp: DistrictInput, plan: TopologyPlan) -> pd.DataFrame:
    """Feeder-proportional loads of the stations and MV customers of the net (DESIGN §5.2).

    Per feeder set F and category c, ``u_c(F) = coincident_c(I_F, N_F, g) / I_c(F)`` and ``P_station = Σ_c u_c(F) ·
    I_station,c``; customers keep ``p_fixed_kw``; so the loads of F sum to the coincident load of F. Normal case:
    F = the feeders of normal operation (half-rings with their stubs, dedicated feeders, radials); (n-1): F = the
    whole ring. ``Q = P · tan(arccos(cos φ))`` with the power factor of the LV version.

    Returns:
        Frame indexed by node key (stations and customers with a bus): ``p_kw``, ``q_kvar``, ``p_n1_kw``,
        ``q_n1_kvar``, ``feeder`` and ``feeder_n1`` (component labels).
    """
    columns = ["p_kw", "q_kvar", "p_n1_kw", "q_n1_kvar", "feeder", "feeder_n1"]
    keys = bus_keys(inp, plan)
    busbar = keys[0]
    load_keys = [k for k in keys[1:] if k in inp.nodes.index and inp.nodes.at[k, "kind"] in ("station", "customer")]
    if not load_keys:
        return pd.DataFrame(columns=columns, index=pd.Index([], name="node_key"), dtype=float)
    lines = _net_lines(plan)
    normal = _components(keys, busbar, lines, include_open=False)
    n1 = _components(keys, busbar, lines, include_open=True)
    installed, units, fixed = _load_arrays(inp, load_keys)
    g = inp.lv.g()
    feeder = np.array([normal[k] for k in load_keys])
    feeder_n1 = np.array([n1[k] for k in load_keys])
    allocate = coincidence.allocate
    p = np.asarray(allocate(installed, units, fixed, np.unique(feeder, return_inverse=True)[1], g), dtype=float)
    p_n1 = np.asarray(allocate(installed, units, fixed, np.unique(feeder_n1, return_inverse=True)[1], g),
                      dtype=float)
    tan_phi = _tan_phi(inp.lv.power_factor)
    return pd.DataFrame({"p_kw": p, "q_kvar": p * tan_phi, "p_n1_kw": p_n1, "q_n1_kvar": p_n1 * tan_phi,
                         "feeder": feeder, "feeder_n1": feeder_n1},
                        index=pd.Index(load_keys, name="node_key"))


def district_load_kw(inp: DistrictInput, plan: TopologyPlan | None = None) -> float:
    """Coincident load of all district nodes (stations and customers, dedicated ones included; excluded HV
    customers not): the category law on the summed statistics plus the fixed customer loads (DESIGN §4.6).

    Never the sum of feeder heads (that overstates the district peak).
    """
    keys = [int(k) for k in inp.nodes.index
            if inp.nodes.at[k, "kind"] in ("station", "customer")
            and not (inp.nodes.at[k, "connection_hint"] == "excluded"
                     or (plan is not None and plan.node_role.get(int(k)) == "excluded"))]
    if not keys:
        return 0.0
    installed, units, fixed = _load_arrays(inp, keys)
    return coincidence.pooled_kw(installed, units, fixed, inp.lv.g())


def _generation_rows(inp: DistrictInput, keys: Sequence[int]) -> list[dict[str, Any]]:
    """One static generator per node and technology group with generation (MV generators and LV-side sums)."""
    params = inp.params
    if not params.include_generation:
        return []
    nodes = inp.nodes
    rows = []
    for key in keys:
        if key not in nodes.index:
            continue
        row = nodes.loc[key]
        is_mv = row["kind"] == "generator"
        level = "mv" if is_mv else "lv"
        cos_phi = params.mv_gen_cos_phi if is_mv else params.lv_pv_cos_phi
        name = f"Generator {_label(nodes, key, 'mastr_connection_id')}" if is_mv else \
            f"LV generation {_label(nodes, key, 'grid_result_id')}"
        for group in GENERATION_GROUPS:
            kw = _value(row, f"gen_{group}_kw")
            if kw <= 0:
                continue
            rows.append({"node_key": key, "name": name, "p_mw": kw / 1000.0,
                         "q_mvar": -kw / 1000.0 * _tan_phi(cos_phi), "technology_group": group, "level": level,
                         "cos_phi": float(cos_phi), "grid_result_id": _value(row, "grid_result_id", math.nan)})
    return rows


# ---------------------------------------------------------------------------------------------- design net
def _line_type(name: str, types: tuple[LineType, LineType], cache: dict[str, LineType]) -> LineType:
    """The type of a name: the district's cable or overhead type (resolved values of the variant), else pandapower."""
    if name not in cache:
        known = {lt.name: lt for lt in types}
        cache[name] = known[name] if name in known else equipment.line_type(name)
    return cache[name]


def line_types(inp: DistrictInput) -> tuple[LineType, LineType]:
    """``(cable, overhead)`` of the district: the variant's types for the district's nominal voltage."""
    return inp.params.cable_at(inp.vn_kv), inp.params.overhead_at(inp.vn_kv)


def _line_parameters(line: LinePlan, types: tuple[LineType, LineType], cache: dict[str, LineType]
                     ) -> tuple[float, float, float, float]:
    """``(r_ohm_per_km, x_ohm_per_km, c_nf_per_km, max_i_ka)`` of a planned line; mixed lines length-weighted.

    ``types`` are the district's ``(cable, overhead)`` types (:func:`line_types`).
    """
    name = (line.std_type or "").strip()
    if line.kind == "mixed" or " + " in name:
        if " + " in name:
            cable_name, overhead_name = (part.strip() for part in name.split(" + ", 1))
            cable, overhead = _line_type(cable_name, types, cache), _line_type(overhead_name, types, cache)
        else:
            cable, overhead = types
        if line.cable_m + line.overhead_m <= 0:
            lt = cable
        else:
            mixed = equipment.mixed_parameters(cable, overhead, float(line.cable_m), float(line.overhead_m))
            return (float(mixed["r_ohm_per_km"]), float(mixed["x_ohm_per_km"]), float(mixed["c_nf_per_km"]),
                    float(mixed["max_i_ka"]))
    elif name:
        lt = _line_type(name, types, cache)
    else:
        lt = types[1] if line.kind == "overhead" else types[0]
    return float(lt.r_ohm_per_km), float(lt.x_ohm_per_km), float(lt.c_nf_per_km), float(lt.max_i_ka)


def _line_coordinates(line: LinePlan, xy: Mapping[int, tuple[float, float]]) -> np.ndarray:
    geometry = line.geometry
    if geometry is not None and not getattr(geometry, "is_empty", True):
        coords = np.asarray(geometry.coords, dtype=float)[:, :2]
        if len(coords) >= 2:
            return coords
    return np.array([xy[int(line.from_key)], xy[int(line.to_key)]], dtype=float)


def _set_column(df: pd.DataFrame, column: str, index: Sequence[int], values: Sequence[Any], dtype: str) -> None:
    """Write a custom column with a fixed dtype; ``index`` holds every row of ``df`` (one bulk create per table)."""
    df[column] = pd.Series(list(values), index=list(index), dtype=dtype).reindex(df.index)


def build_design_net(inp: DistrictInput, plan: TopologyPlan, *, epsg: int = DEFAULT_EPSG) -> pp.pandapowerNet:
    """The MV-only design net of a district in the load-case state (DESIGN §5.1).

    Args:
        inp: The district input (nodes, parameters, LV version values).
        plan: The planned topology.
        epsg: CRS of the node coordinates and line geometries (pylovo's ``TARGET_EPSG``); bus and line ``geo``
            are WGS84 GeoJSON as in pylovo's LV nets.

    Returns:
        A pandapower net with the custom columns of the module docstring: ext_grid ``vm_pu = busbar_vm_pu`` at
        the busbar, normally-open line switches open, loads at the normal-case values, static generators at
        ``scaling = 0``.

    Raises:
        ValueError: If a line refers to a node key without a bus (unknown or excluded node).
    """
    params = inp.params
    nodes = inp.nodes
    keys = bus_keys(inp, plan)
    busbar = keys[0]
    bus_of = {key: i for i, key in enumerate(keys)}
    xy: dict[int, tuple[float, float]] = {}
    kinds: list[str] = []
    for key in keys:
        if key in nodes.index:
            xy[key] = (float(nodes.at[key, "x"]), float(nodes.at[key, "y"]))
            kinds.append("busbar" if key == busbar else str(nodes.at[key, "kind"]))
        else:
            joint = plan.joints[key]
            xy[key] = (float(joint[0]), float(joint[1]))
            kinds.append("joint")
    lines = _net_lines(plan)
    for line in lines:
        for key in (line.from_key, line.to_key):
            if int(key) not in bus_of:
                raise ValueError(f"MV line {line.key} of grid {inp.mv_grid_id} refers to node {key}, which has no "
                                 "bus (unknown or excluded node)")

    # coordinates of buses and line vertices, transformed to WGS84 in one call
    line_coords = [_line_coordinates(line, xy) for line in lines]
    points = np.vstack([np.array([xy[key] for key in keys], dtype=float), *line_coords]) if lines else \
        np.array([xy[key] for key in keys], dtype=float)
    lon, lat = _to_wgs84(epsg).transform(points[:, 0], points[:, 1])
    lonlat = np.column_stack([lon, lat]).round(8).tolist()   # plain floats: pandapower writes their repr

    net = _empty_network(f"MV grid {inp.mv_grid_id}")
    names = [f"MV busbar {inp.substation_id}" if key == busbar else f"MV node {key}" for key in keys]
    bus_index = pp.create_buses(net, len(keys), vn_kv=float(inp.vn_kv), name=names,
                                type=["b" if key == busbar else "n" for key in keys],
                                geodata=[tuple(p) for p in lonlat[:len(keys)]], index=list(range(len(keys))))
    _set_column(net.bus, "node_key", bus_index, keys, "int64")
    _set_column(net.bus, "mv_node_id", bus_index, [float(k) if k > 0 else math.nan for k in keys], "float64")
    _set_column(net.bus, "kind", bus_index, kinds, "object")
    pp.create_ext_grid(net, bus_of[busbar], vm_pu=float(params.busbar_vm_pu), name="OLTC setpoint")

    if lines:
        cache: dict[str, LineType] = {}
        types = line_types(inp)
        electrical = [_line_parameters(line, types, cache) for line in lines]
        exits = _ring_exits(plan, busbar)
        offsets = np.cumsum([len(keys)] + [len(c) for c in line_coords])
        geodata = [[tuple(p) for p in lonlat[offsets[i]:offsets[i + 1]]] for i in range(len(lines))]
        line_index = pp.create_lines_from_parameters(
            net,
            from_buses=[bus_of[int(line.from_key)] for line in lines],
            to_buses=[bus_of[int(line.to_key)] for line in lines],
            length_km=[max(float(line.length_m), 1.0) / 1000.0 for line in lines],
            r_ohm_per_km=[e[0] for e in electrical], x_ohm_per_km=[e[1] for e in electrical],
            c_nf_per_km=[e[2] for e in electrical], max_i_ka=[e[3] for e in electrical],
            name=[f"MV line {line.key}" for line in lines],
            type=["ol" if (line.kind == "overhead" or (line.kind == "mixed" and line.overhead_m > line.cable_m))
                  else "cs" for line in lines],
            geodata=geodata, parallel=[max(int(line.parallel), 1) for line in lines],
            index=list(range(len(lines))))
        df = net.line
        _set_column(df, "line_key", line_index, [int(line.key) for line in lines], "int64")
        _set_column(df, "mv_line_id", line_index, [math.nan] * len(lines), "float64")
        _set_column(df, "role", line_index, [line.role for line in lines], "object")
        _set_column(df, "kind", line_index, [line.kind for line in lines], "object")
        _set_column(df, "ring_id", line_index, [math.nan if line.ring_id is None else float(line.ring_id)
                                                for line in lines], "float64")
        _set_column(df, "feeder_id", line_index, [math.nan if line.feeder_id is None else float(line.feeder_id)
                                                  for line in lines], "float64")
        _set_column(df, "std_type_name", line_index, [line.std_type for line in lines], "object")
        _set_column(df, "exit", line_index, [exits.get(int(line.key)) for line in lines], "object")
        _set_column(df, "cable_km", line_index, [float(line.cable_m) / 1000.0 for line in lines], "float64")
        _set_column(df, "overhead_km", line_index, [float(line.overhead_m) / 1000.0 for line in lines], "float64")

        open_lines = [(i, line) for i, line in zip(line_index, lines) if line.normally_open]
        if open_lines:
            at = []
            for _, line in open_lines:
                key = int(line.open_at_key) if line.open_at_key is not None else int(line.to_key)
                if key not in (int(line.from_key), int(line.to_key)):
                    raise ValueError(f"MV line {line.key} of grid {inp.mv_grid_id} is open at node {key}, which is "
                                     "not one of its ends")
                at.append(bus_of[key])
            switch_index = pp.create_switches(net, buses=at, elements=[i for i, _ in open_lines], et="l",
                                              closed=False, type="LBS",
                                              name=[f"NOP line {line.key}" for _, line in open_lines])
            switch_index = list(np.atleast_1d(switch_index))
            _set_column(net.switch, "normally_open", switch_index, [True] * len(open_lines), "bool")
            _set_column(net.switch, "line_key", switch_index, [int(line.key) for _, line in open_lines], "int64")

    loads = node_loads(inp, plan)
    if len(loads):
        load_keys = [int(k) for k in loads.index]
        load_names = [f"Station {_label(nodes, k, 'grid_result_id')}" if nodes.at[k, "kind"] == "station"
                      else f"MV customer {_label(nodes, k, 'customer_objectid')}" for k in load_keys]
        p_mw, q_mvar = loads["p_kw"].to_numpy() / 1000.0, loads["q_kvar"].to_numpy() / 1000.0
        load_index = pp.create_loads(net, buses=[bus_of[k] for k in load_keys], p_mw=p_mw, q_mvar=q_mvar,
                                     name=load_names, index=list(range(len(load_keys))))
        _set_column(net.load, "node_key", load_index, load_keys, "int64")
        _set_column(net.load, "grid_result_id", load_index,
                    [_value(nodes.loc[k], "grid_result_id", math.nan) for k in load_keys], "float64")
        _set_column(net.load, "p_normal_mw", load_index, p_mw, "float64")
        _set_column(net.load, "q_normal_mvar", load_index, q_mvar, "float64")
        _set_column(net.load, "p_n1_mw", load_index, loads["p_n1_kw"].to_numpy() / 1000.0, "float64")
        _set_column(net.load, "q_n1_mvar", load_index, loads["q_n1_kvar"].to_numpy() / 1000.0, "float64")

    generation = _generation_rows(inp, keys[1:])
    if generation:
        frame = pd.DataFrame(generation)
        sgen_index = pp.create_sgens(net, buses=[bus_of[int(k)] for k in frame["node_key"]],
                                     p_mw=frame["p_mw"].to_numpy(), q_mvar=frame["q_mvar"].to_numpy(),
                                     name=frame["name"].tolist(), scaling=0.0, index=list(range(len(frame))))
        _set_column(net.sgen, "node_key", sgen_index, frame["node_key"].astype(int).tolist(), "int64")
        _set_column(net.sgen, "grid_result_id", sgen_index, frame["grid_result_id"].tolist(), "float64")
        _set_column(net.sgen, "technology_group", sgen_index, frame["technology_group"].tolist(), "object")
        _set_column(net.sgen, "level", sgen_index, frame["level"].tolist(), "object")
        _set_column(net.sgen, "cos_phi", sgen_index, frame["cos_phi"].tolist(), "float64")
    return net


# ---------------------------------------------------------------------------------------------- cases
def _column(df: pd.DataFrame, column: str, fallback: pd.Series) -> pd.Series:
    if column not in df.columns:
        return fallback
    values = pd.to_numeric(df[column], errors="coerce")
    return values.where(values.notna(), fallback)


def case_net(net: pp.pandapowerNet, case: str, params: Any, *, busbar_vm_pu: float | None = None
             ) -> pp.pandapowerNet:
    """A copy of ``net`` set to one snapshot case (DESIGN §5.2); shared by the main path and ``couple``.

    - ``load``: normal loads, no generation, normal topology (normally-open switches open);
    - ``n1a`` / ``n1b``: (n-1) loads (``p_n1_mw``, ``q_n1_mvar``), no generation, every line with ``exit`` ``a`` /
      ``b`` out of service and every normally-open switch closed;
    - ``feed_in_wind`` / ``feed_in_pv``: loads times ``load_p`` and ``load_q``, generation scaled per technology
      group by ``params.feed_in_cases``, normal topology.

    Apply it to the base net (design, stored or coupled), not to a case net: rows without the custom base
    columns take their current ``p_mw`` and ``q_mvar`` as the normal values, rows without ``technology_group``
    count as ``other``, switches without ``normally_open`` and lines without ``exit`` keep their state.

    Args:
        net: The base net.
        case: One of :data:`~pylovo.mv_grid.model.CASES`.
        params: The MV parameters (``busbar_vm_pu``, ``feed_in_cases``).
        busbar_vm_pu: Setpoint of the ext_grid at the MV busbar (bus ``kind`` ``busbar``; default
            ``params.busbar_vm_pu``). An ext_grid on the HV side (stored net) keeps its setpoint.

    Raises:
        ValueError: For an unknown case.
    """
    if case not in CASES:
        raise ValueError(f"unknown MV case {case!r}, expected one of {', '.join(CASES)}")
    if case not in params.active_cases:
        raise ValueError(f"case {case!r} was not requested; available cases: {', '.join(params.active_cases)}")
    out = copy.deepcopy(net)
    setpoint = float(params.busbar_vm_pu if busbar_vm_pu is None else busbar_vm_pu)
    if len(out.ext_grid) and "kind" in out.bus.columns:
        busbars = out.bus.index[out.bus["kind"] == "busbar"]
        on_busbar = out.ext_grid["bus"].isin(busbars)
        out.ext_grid.loc[on_busbar, "vm_pu"] = setpoint

    load = out.load
    if len(load):
        base_p = _column(load, "p_normal_mw", load["p_mw"].astype(float))
        base_q = _column(load, "q_normal_mvar", load["q_mvar"].astype(float))
        if case in N1_CASES:
            load["p_mw"] = _column(load, "p_n1_mw", base_p)
            load["q_mvar"] = _column(load, "q_n1_mvar", base_q)
        elif case in FEED_IN_CASES:
            factors = params.feed_in_cases[case]
            load["p_mw"] = base_p * float(factors["load_p"])
            load["q_mvar"] = base_q * float(factors["load_q"])
        else:
            load["p_mw"], load["q_mvar"] = base_p, base_q

    sgen = out.sgen
    if len(sgen):
        if case in FEED_IN_CASES:
            factors = params.feed_in_cases[case]
            groups = sgen["technology_group"] if "technology_group" in sgen.columns else \
                pd.Series("other", index=sgen.index)
            sgen["scaling"] = [float(factors.get(g if g in GENERATION_GROUPS else "other", factors["other"]))
                               for g in groups]
        else:
            sgen["scaling"] = 0.0

    line = out.line
    if len(line) and "exit" in line.columns:
        has_exit = line["exit"].isin(("a", "b"))
        line.loc[has_exit, "in_service"] = True
        if case in N1_CASES:
            line.loc[line["exit"] == N1_EXIT[case], "in_service"] = False
    switch = out.switch
    if len(switch) and "normally_open" in switch.columns:
        nop = switch["normally_open"].fillna(False).astype(bool)
        switch.loc[nop, "closed"] = case in N1_CASES
    return out


def _runpp(net: pp.pandapowerNet) -> bool:
    try:
        pp.runpp(net, algorithm="nr", init="auto")
    except LoadflowNotConverged:
        return False
    return bool(net.converged)


def _case_result(net: pp.pandapowerNet, case: str, converged: bool) -> CaseResult:
    bus = net.bus
    mv = bus.index if "node_key" not in bus.columns else bus.index[bus["node_key"].notna()]
    keys = bus.loc[mv, "node_key"].astype("int64").to_numpy() if "node_key" in bus.columns else mv.to_numpy()
    line_keys = net.line["line_key"].astype("int64").to_numpy() if "line_key" in net.line.columns else \
        net.line.index.to_numpy()
    if converged:
        vm = pd.Series(net.res_bus.loc[mv, "vm_pu"].to_numpy(dtype=float), index=keys, name="vm_pu")
        loading = pd.Series(net.res_line.loc[net.line.index, "loading_percent"].to_numpy(dtype=float),
                            index=line_keys, name="loading_percent")
        slack_p = float(net.res_ext_grid["p_mw"].sum())
        slack_q = float(net.res_ext_grid["q_mvar"].sum())
    else:
        vm = pd.Series(np.nan, index=keys, name="vm_pu", dtype=float)
        loading = pd.Series(np.nan, index=line_keys, name="loading_percent", dtype=float)
        slack_p = slack_q = math.nan
    vm.index.name, loading.index.name = "node_key", "line_key"
    return CaseResult(case=case, converged=converged, vm_pu=vm, loading_percent=loading, slack_p_mw=slack_p,
                      slack_q_mvar=slack_q)


def run_cases(net: pp.pandapowerNet, inp: DistrictInput, plan: TopologyPlan) -> dict[str, CaseResult]:
    """Run the variant's requested cases on copies of the design net (non-convergence is a result).

    ``CaseResult.vm_pu`` is indexed by node key with the busbar first, ``loading_percent`` by line key (0 for lines
    out of service); a case that does not converge has NaN values and NaN slack powers.
    """
    results: dict[str, CaseResult] = {}
    for case in inp.params.active_cases:
        current = case_net(net, case, inp.params)
        converged = _runpp(current)
        if not converged:
            logger.warning(f"MV grid {inp.mv_grid_id}: the {case} case did not converge")
        results[case] = _case_result(current, case, converged)
    return results


# ---------------------------------------------------------------------------------------------- criteria
def check(cases: Mapping[str, CaseResult], inp: DistrictInput, plan: TopologyPlan) -> list[Violation]:
    """The violated criteria of the cases (DESIGN §5.3), ordered by case, element and key.

    - ``load``: every MV node ``vm >= busbar_vm_pu - MV_MAX_VOLTAGE_DROP_PERCENT/100``; lines
      ``loading <= 100 · MV_LINE_PLANNING_UTILIZATION``;
    - ``n1a``, ``n1b``: nodes with an (n-1) requirement (:func:`n1_nodes`) ``vm >= busbar_vm_pu -
      max_drop_n1_percent/100``; lines ``loading <= 100 · n1_utilization``;
    - feed-in cases: every MV node ``vm - busbar_vm_pu <= max_rise_percent/100``; lines ``<= 100 · n1_utilization``;
    - a case that did not converge is one ``not_converged`` violation at the busbar.

    Unsupplied nodes (NaN voltage) are no violation; :func:`case_metrics` counts them.
    """
    params = inp.params
    busbar = busbar_key(inp)
    v0 = float(params.busbar_vm_pu)
    secure = n1_nodes(inp, plan)
    node_ring = {int(k): int(v) for k, v in plan.node_ring.items() if v is not None}
    line_ring = {int(line.key): (None if line.ring_id is None else int(line.ring_id)) for line in plan.lines}
    violations: list[Violation] = []
    for case in params.active_cases:
        result = cases.get(case)
        if result is None:
            continue
        if not result.converged:
            violations.append(Violation(case, "not_converged", "bus", busbar, math.nan, math.nan,
                                        node_ring.get(busbar)))
            continue
        vm = result.vm_pu.drop(busbar, errors="ignore")
        if case == "load":
            limit, kind = v0 - params.max_drop_percent / 100.0, "voltage_low"
            bad = vm[vm < limit - TOLERANCE]
            line_limit = 100.0 * params.line_utilization
        elif case in N1_CASES:
            limit, kind = v0 - params.max_drop_n1_percent / 100.0, "voltage_low"
            bad = vm[(vm < limit - TOLERANCE) & vm.index.isin(list(secure))]
            line_limit = 100.0 * params.n1_utilization
        else:
            limit, kind = v0 + params.max_rise_percent / 100.0, "voltage_high"
            bad = vm[vm > limit + TOLERANCE]
            line_limit = 100.0 * params.n1_utilization
        for key, value in sorted(bad.items()):
            violations.append(Violation(case, kind, "bus", int(key), float(value), float(limit),
                                        node_ring.get(int(key))))
        loading = result.loading_percent
        for key, value in sorted(loading[loading > line_limit + TOLERANCE].items()):
            violations.append(Violation(case, "loading", "line", int(key), float(value), float(line_limit),
                                        line_ring.get(int(key))))
    return violations


def power_flow_status(cases: Mapping[str, CaseResult], violations: Sequence[Violation]) -> str:
    """``not_converged`` (some case), else ``voltage_violation``, else ``loading_violation``, else ``converged``."""
    if any(not result.converged for result in cases.values()) or \
            any(v.kind == "not_converged" for v in violations):
        return "not_converged"
    if any(v.kind in ("voltage_low", "voltage_high") for v in violations):
        return "voltage_violation"
    if any(v.kind == "loading" for v in violations):
        return "loading_violation"
    return "converged"


def _finite(value: float) -> float | None:
    return None if value is None or not math.isfinite(float(value)) else float(value)


def case_metrics(cases: Mapping[str, CaseResult]) -> dict:
    """Extremes per case and the district maxima of ``pylovo.mv_grid`` (JSON-native, None for missing values).

    Per case ``<case>_converged``, ``<case>_min_vm_pu``, ``<case>_max_vm_pu``, ``<case>_max_loading_percent``,
    ``<case>_slack_p_mw``, ``<case>_slack_q_mvar``, ``<case>_unsupplied_nodes``; and ``max_voltage_drop_percent``
    (load case), ``max_voltage_drop_n1_percent``, ``max_voltage_rise_percent``, ``max_line_loading_percent``,
    ``max_line_loading_n1_percent``, ``max_line_loading_feed_in_percent`` and ``peak_feed_in_mw`` (largest reverse
    flow into the HV grid of the feed-in cases, 0 without reverse flow). Drops and rises are relative to the busbar
    voltage of the case (the first entry of ``vm_pu``).
    """
    out: dict[str, Any] = {}
    drop: dict[str, float] = {}
    rise: dict[str, float] = {}
    loading: dict[str, float] = {}
    for case, result in cases.items():
        vm = result.vm_pu
        values = vm.to_numpy(dtype=float)
        finite = values[np.isfinite(values)]
        load_values = result.loading_percent.to_numpy(dtype=float)
        load_finite = load_values[np.isfinite(load_values)]
        out[f"{case}_converged"] = bool(result.converged)
        out[f"{case}_min_vm_pu"] = _finite(finite.min()) if len(finite) else None
        out[f"{case}_max_vm_pu"] = _finite(finite.max()) if len(finite) else None
        out[f"{case}_max_loading_percent"] = _finite(load_finite.max()) if len(load_finite) else None
        out[f"{case}_slack_p_mw"] = _finite(result.slack_p_mw)
        out[f"{case}_slack_q_mvar"] = _finite(result.slack_q_mvar)
        out[f"{case}_unsupplied_nodes"] = int((~np.isfinite(values)).sum()) if result.converged else None
        if result.converged and len(finite) and np.isfinite(values[0]):
            drop[case] = 100.0 * (values[0] - finite.min())
            rise[case] = 100.0 * (finite.max() - values[0])
        if result.converged and len(load_finite):
            loading[case] = float(load_finite.max())

    def _max(names: Iterable[str], source: Mapping[str, float]) -> float | None:
        found = [source[name] for name in names if name in source]
        return _finite(max(found)) if found else None

    out["max_voltage_drop_percent"] = _max(["load"], drop)
    out["max_voltage_drop_n1_percent"] = _max(N1_CASES, drop)
    out["max_voltage_rise_percent"] = _max(FEED_IN_CASES, rise)
    out["max_line_loading_percent"] = _max(["load"], loading)
    out["max_line_loading_n1_percent"] = _max(N1_CASES, loading)
    out["max_line_loading_feed_in_percent"] = _max(FEED_IN_CASES, loading)
    reverse = [-cases[c].slack_p_mw for c in FEED_IN_CASES if c in cases and cases[c].converged]
    out["peak_feed_in_mw"] = _finite(max(0.0, max(reverse))) if reverse else None
    return out


# ---------------------------------------------------------------------------------------------- repair
def _json(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else float(value)
    return value


def _record(round_no: int, name: str, violation: Violation | None, **extra: Any) -> dict:
    record: dict[str, Any] = {"round": round_no, "action": name}
    if violation is not None:
        record.update(case=violation.case, violation=violation.kind, element=violation.element,
                      key=int(violation.key), value=_json(violation.value), limit=_json(violation.limit))
    record.update({k: ([_json(v) for v in value] if isinstance(value, (list, tuple)) else _json(value))
                   for k, value in extra.items()})
    return record


def _member_count(inp: DistrictInput, members: Sequence[int]) -> int:
    nodes = inp.nodes
    return sum(1 for m in members if int(m) in nodes.index and nodes.at[int(m), "kind"] in
               ("station", "customer", "generator"))


def _generator_power(inp: DistrictInput, key: int, case: str) -> float:
    row = inp.nodes.loc[key]
    factors = inp.params.feed_in_cases.get(case, {})
    return sum(_value(row, f"gen_{g}_kw") * float(factors.get(g, 1.0)) for g in GENERATION_GROUPS)


def _parallel(plan: TopologyPlan) -> dict[int, int]:
    return {int(line.key): int(line.parallel) for line in plan.lines}


def _signature(plan: TopologyPlan) -> tuple:
    """What a structural repair changes: rings, lines (ends, role, parallels, open state) and node roles."""
    return (tuple((int(r.ring_id), r.kind, tuple(int(m) for m in r.members), r.nop_key) for r in plan.rings),
            tuple((int(line.key), int(line.from_key), int(line.to_key), line.role, int(line.parallel),
                   bool(line.normally_open), line.open_at_key) for line in plan.lines),
            tuple(sorted((int(k), str(v)) for k, v in plan.node_role.items())))


def repair(inp: DistrictInput, rg: Any, model: Any, ev: Any, plan: TopologyPlan, violations: Sequence[Violation]
           ) -> tuple[TopologyPlan, list[dict]]:
    """One round of the repair loop (DESIGN §5.4): applies all non-conflicting actions, at most one per ring.

    In this order:

    1. Voltage (``load``, ``n1a``, ``n1b``) on a node of ring r, a ladder over the rounds: move the NOP to the best
       feasible position (``topology.move_nop``); if it does not move (or moved in an earlier round and the ring
       still violates): split the ring (``topology.split_ring``, rings with at least 2 members); then ``parallel``
       + 1 on the first section of each half (the two exits). Rings made by a split continue at the third step.
    2. Loading (``load``, ``n1*``) on a section of ring r: split the ring if the section is a busbar exit and the
       ring has at least 4 members, else ``parallel`` + 1 (up to ``max_parallel_cables``).
    3. Loading or voltage on a dedicated feeder, a generator feeder or a busbar radial: ``parallel`` + 1 on its
       lines (up to the cap), else the report ``dedicated_overload``.
    4. Rise or loading in a feed-in case on a feeder with a T-off or loop-in MV generator: convert the generator with
       the largest feed-in to a dedicated feeder (``topology.convert_to_dedicated``), one per feeder; without such a
       generator the report ``lv_pv_rise`` (rise or loading driven by LV-side generation, not repaired).
    5. ``not_converged``: report, the topology is kept.

    Args:
        inp, rg, model, ev: Passed to the topology functions (district input, run graph, distance model, ring
            evaluator).
        plan: The current plan.
        violations: The violations of :func:`check` for ``plan``.

    Returns:
        ``(plan, actions)``: the repaired plan and one JSON-native record per applied action (``round``,
        ``action``, the triggering violation, ``ring_id``/``node_key``/``line_keys``). An empty list means nothing
        could be repaired. Reports of violations that are not repaired are kept in
        ``plan.diagnostics["repair_reports"]`` (the latest round), the ladder state per ring in
        ``plan.diagnostics["repair_stage"]`` and the round count in ``plan.diagnostics["repair_round"]``.
    """
    params = inp.params
    topology = _topology()
    round_no = int(plan.diagnostics.get("repair_round", 0)) + 1
    stage: dict[int, int] = {int(k): int(v) for k, v in (plan.diagnostics.get("repair_stage") or {}).items()}
    busbar = busbar_key(inp)
    keys = bus_keys(inp, plan)
    rings = {int(r.ring_id): r for r in plan.rings}
    lines = {int(line.key): line for line in plan.lines}
    normal = _components(keys, busbar, _net_lines(plan), include_open=False)
    n1 = _components(keys, busbar, _net_lines(plan), include_open=True)
    ring_of_component = _component_rings(plan, n1)
    exits = _ring_exits(plan, busbar)

    def ring_of_node(key: int) -> int | None:
        ring = plan.node_ring.get(key)
        if ring is not None:
            return int(ring)
        label = n1.get(key)
        return ring_of_component.get(label) if label is not None else None

    def ring_of_line(line: LinePlan) -> int | None:
        if line.ring_id is not None:
            return int(line.ring_id)
        for key in (line.from_key, line.to_key):
            if int(key) != busbar:
                return ring_of_node(int(key))
        return None

    def feeder_kind(ring_id: int | None, line: LinePlan | None = None) -> str:
        if ring_id is not None and ring_id in rings:
            return rings[ring_id].kind
        if line is not None and line.role in ("dedicated", "generator"):
            return line.role
        return "radial"

    # ---- classify
    ring_voltage: dict[int, Violation] = {}
    ring_loading: dict[int, list[tuple[int, Violation]]] = {}
    feeder_lines: dict[int, Violation] = {}           # ring id (non-ring feeders) -> first violation
    feed_in: dict[int, Violation] = {}                 # normal component -> first feed-in violation
    reports: list[dict] = []
    for v in violations:
        if v.kind == "not_converged":
            reports.append(_record(round_no, "not_converged", v))
            continue
        if v.element == "bus":
            ring_id = v.ring_id if v.ring_id is not None else ring_of_node(int(v.key))
            line = None
        else:
            line = lines.get(int(v.key))
            if line is None:
                continue
            ring_id = v.ring_id if v.ring_id is not None else ring_of_line(line)
        kind = feeder_kind(ring_id, line)
        if v.case in FEED_IN_CASES and kind == "ring":
            if v.element == "bus":
                label = normal.get(int(v.key))
            else:
                ends = [int(k) for k in (line.from_key, line.to_key) if int(k) != busbar]
                label = normal.get(ends[0]) if ends else None
            if label is not None:
                feed_in.setdefault(label, v)
            continue
        if kind == "ring":
            if v.element == "bus":
                ring_voltage.setdefault(int(ring_id), v)
            else:
                ring_loading.setdefault(int(ring_id), []).append((int(v.key), v))
        elif ring_id is not None:
            feeder_lines.setdefault(int(ring_id), v)
        else:
            reports.append(_record(round_no, "unassigned", v))

    touched: set[int] = set()
    parallel_requests: list[tuple[list[int], int | None, Violation, str]] = []
    structural: list[tuple[str, Any, Violation]] = []   # ("move_nop" | "split_ring", ring_id) or ("convert", key)

    def exit_lines(ring_id: int) -> list[int]:
        return sorted(k for k, line in lines.items()
                      if line.ring_id is not None and int(line.ring_id) == ring_id and exits.get(k) in ("a", "b"))

    # 1. voltage on rings: the ladder
    for ring_id, v in sorted(ring_voltage.items()):
        ring = rings.get(ring_id)
        if ring is None:
            continue
        level = stage.get(ring_id, 0)
        if level < 1:
            stage[ring_id] = 1
            structural.append(("move_nop", ring_id, v))
            touched.add(ring_id)
            continue
        if level < 2 and _member_count(inp, ring.members) >= 2:
            stage[ring_id] = 2
            structural.append(("split_ring", ring_id, v))
            touched.add(ring_id)
            continue
        if level < 3:
            stage[ring_id] = 3
            parallel_requests.append((exit_lines(ring_id), ring_id, v, "voltage"))
            touched.add(ring_id)
            continue
        reports.append(_record(round_no, "unresolved_voltage", v, ring_id=ring_id))

    # 2. loading on ring sections
    for ring_id, items in sorted(ring_loading.items()):
        ring = rings.get(ring_id)
        if ring is None or ring_id in touched:
            continue
        on_exit = [(k, v) for k, v in items if exits.get(k) in ("a", "b")]
        if on_exit and _member_count(inp, ring.members) >= 4:
            structural.append(("split_ring", ring_id, on_exit[0][1]))
            stage[ring_id] = max(stage.get(ring_id, 0), 2)
        else:
            parallel_requests.append(([k for k, _ in items], ring_id, items[0][1], "loading"))
        touched.add(ring_id)

    # 3. dedicated feeders, generator feeders and busbar radials
    for ring_id, v in sorted(feeder_lines.items()):
        if ring_id in touched:
            continue
        keys_of_feeder = sorted(k for k, line in lines.items()
                                if line.ring_id is not None and int(line.ring_id) == ring_id)
        parallel_requests.append((keys_of_feeder, ring_id, v, "dedicated"))
        touched.add(ring_id)

    # 4. feed-in: convert the largest T-off or loop-in generator of the feeder (one per feeder)
    for label, v in sorted(feed_in.items()):
        members = sorted(k for k, lab in normal.items() if lab == label)
        ring_id = next((r for r in (ring_of_node(k) for k in members) if r is not None), None)
        generators = [k for k in members if k in inp.nodes.index and inp.nodes.at[k, "kind"] == "generator"
                      and plan.node_role.get(k) in ("tee", "loop_in")]
        if not generators:
            reports.append(_record(round_no, "lv_pv_rise", v, ring_id=ring_id))
            continue
        if ring_id is not None and ring_id in touched:
            continue
        key = max(generators, key=lambda k: (_generator_power(inp, k, v.case), -k))
        structural.append(("convert_to_dedicated", key, v))

    # ---- apply: parallel cables first (line keys stay valid), then the structural changes
    actions: list[dict] = []
    for line_keys, ring_id, v, reason in parallel_requests:
        before = _parallel(plan)
        if line_keys:
            plan, _ = topology.add_parallel(plan, line_keys, int(params.max_parallel_cables))
        after = _parallel(plan)
        changed = [k for k in line_keys if after.get(k, 0) > before.get(k, 0)]
        if changed:
            actions.append(_record(round_no, "parallel", v, ring_id=ring_id, line_keys=changed, reason=reason))
        else:
            reports.append(_record(round_no, "dedicated_overload" if reason == "dedicated" else
                                   "unresolved_" + reason, v, ring_id=ring_id, line_keys=line_keys))
    def exit_parallel(target: int, v: Violation) -> None:
        """Last step of the voltage ladder: one more cable on the two exits of ring ``target``."""
        nonlocal plan
        stage[target] = 3
        current_exits = _ring_exits(plan, busbar)
        line_keys = sorted(int(line.key) for line in plan.lines if line.ring_id is not None
                           and int(line.ring_id) == target and current_exits.get(int(line.key)) in ("a", "b"))
        before = _parallel(plan)
        if line_keys:
            plan, _ = topology.add_parallel(plan, line_keys, int(params.max_parallel_cables))
        changed = [k for k in line_keys if _parallel(plan).get(k, 0) > before.get(k, 0)]
        if changed:
            actions.append(_record(round_no, "parallel", v, ring_id=target, line_keys=changed, reason="voltage"))
        else:
            reports.append(_record(round_no, "unresolved_voltage", v, ring_id=target))

    for name, target, v in structural:
        ring_ids = {int(r.ring_id) for r in plan.rings}
        if name in ("move_nop", "split_ring") and target not in ring_ids:
            continue
        if name == "move_nop":
            new_plan, moved = topology.move_nop(inp, rg, model, ev, plan, target)
            if moved:
                plan = new_plan
                actions.append(_record(round_no, "move_nop", v, ring_id=target))
                continue
            ring = next(r for r in plan.rings if int(r.ring_id) == target)
            if _member_count(inp, ring.members) < 2:
                exit_parallel(target, v)
                continue
            name = "split_ring"                 # the open point is already at its best position
        if name == "split_ring":
            before = _signature(plan)
            new_plan = topology.split_ring(inp, rg, model, ev, plan, target)
            if _signature(new_plan) == before:  # the ring cannot be split (fewer than two members)
                exit_parallel(target, v)
                continue
            plan = new_plan
            new_ids = {int(r.ring_id) for r in plan.rings}
            for ring_id in new_ids - ring_ids:
                stage[ring_id] = 2
            if target in new_ids:
                stage[target] = 2
            else:
                stage.pop(target, None)
            actions.append(_record(round_no, "split_ring", v, ring_id=target, new_ring_ids=sorted(new_ids - ring_ids)))
        elif name == "convert_to_dedicated":
            before = _signature(plan)
            new_plan = topology.convert_to_dedicated(inp, rg, model, plan, target)
            if _signature(new_plan) == before:  # the generator already has its own feeder
                reports.append(_record(round_no, "unresolved_feed_in", v, node_key=target))
                continue
            plan = new_plan
            actions.append(_record(round_no, "convert_to_dedicated", v, node_key=target))

    plan.diagnostics["repair_round"] = round_no
    plan.diagnostics["repair_stage"] = {str(k): v for k, v in sorted(stage.items())}
    plan.diagnostics["repair_reports"] = reports
    return plan, actions


# ---------------------------------------------------------------------------------------------- HV/MV sizing
def substation_loads(inp: DistrictInput, plan: TopologyPlan, cases: Mapping[str, CaseResult]
                     ) -> tuple[float, float]:
    """``(S_load, S_feed)`` in MVA: the district load by the category law over cos φ, and the largest slack ``|S|`` of
    the converged feed-in cases (0 if none converged)."""
    s_load = district_load_kw(inp, plan) / 1000.0 / float(inp.lv.power_factor)
    feeds = [math.hypot(cases[c].slack_p_mw, cases[c].slack_q_mvar) for c in FEED_IN_CASES
             if c in cases and cases[c].converged]
    return s_load, (max(feeds) if feeds else 0.0)


def size_substation(inp: DistrictInput, plan: TopologyPlan, cases: Mapping[str, CaseResult]
                    ) -> tuple[int, float, str]:
    """``(units, unit_mva, sizing)`` of the HV/MV transformers (DESIGN §4.6, ``equipment.size_hv_mv``).

    ``S_load`` is the coincident load of all district nodes (never the sum of feeder heads), ``S_feed`` the largest
    ``|S|`` at the busbar in the feed-in cases; OSM ratings of the substation (``inp.known_units_mva``) win when two or
    more are known.
    """
    params = inp.params
    s_load, s_feed = substation_loads(inp, plan, cases)
    units, unit_mva, sizing = equipment.size_hv_mv(s_load, s_feed, tuple(params.hv_mv_sizes_mva),
                                                       int(params.hv_mv_units), float(params.hv_mv_n1_utilization),
                                                       tuple(inp.known_units_mva))
    return int(units), float(unit_mva), str(sizing)


def substation_metrics(inp: DistrictInput, plan: TopologyPlan, cases: Mapping[str, CaseResult], units: int,
                       unit_mva: float) -> dict:
    """Peak load and feed-in of the district and the HV/MV transformer loadings (``mv_substation`` columns).

    ``loading_n0_percent = S_load / (n · sn)`` and ``loading_n1_percent = S_load / ((n - 1) · sn)`` (DESIGN
    §4.6); ``loading_n1_sizing_percent = max(S_load, S_feed) / ((n - 1) · sn)`` is the sizing criterion. The (n-1)
    values are None for a single unit.
    """
    s_load, s_feed = substation_loads(inp, plan, cases)
    reverse = [-cases[c].slack_p_mw for c in FEED_IN_CASES if c in cases and cases[c].converged]
    capacity = units * unit_mva
    return {
        "peak_load_mw": district_load_kw(inp, plan) / 1000.0,
        "peak_feed_in_mw": max(0.0, max(reverse)) if reverse else None,
        "s_load_mva": s_load,
        "s_feed_mva": s_feed,
        "loading_n0_percent": 100.0 * s_load / capacity if capacity > 0 else None,
        "loading_n1_percent": 100.0 * s_load / ((units - 1) * unit_mva) if units > 1 and unit_mva > 0 else None,
        "loading_n1_sizing_percent": (100.0 * max(s_load, s_feed) / ((units - 1) * unit_mva)
                                      if units > 1 and unit_mva > 0 else None),
    }


def _hv_mv_type(params: Any, vn_kv: float, unit_mva: float) -> HvMvType:
    """The HV/MV transformer type of a rating for the district's voltage (``110/20 kV`` or ``110/10 kV``): from the
    variant's types, else ``equipment.hv_mv_types``, else the nearest type with the rating replaced (no-load losses
    scaled, flagged ``interpolated``)."""
    types = params.hv_mv_types_at(vn_kv)
    for item in types:
        if abs(float(item.sn_mva) - unit_mva) < 1e-6:
            return item
    try:
        for item in equipment.hv_mv_types(vn_kv, (unit_mva,)):
            if abs(float(item.sn_mva) - unit_mva) < 1e-6:
                return item
    except (LookupError, ValueError, UserWarning) as exc:
        logger.debug(f"no HV/MV type for {unit_mva:g} MVA: {exc}")
    base = min(types, key=lambda t: (abs(float(t.sn_mva) - unit_mva), float(t.sn_mva)))
    return dataclasses.replace(base, name=f"{unit_mva:g} MVA {base.vn_hv_kv:g}/{base.vn_lv_kv:g} kV",
                               sn_mva=float(unit_mva), pfe_kw=float(base.pfe_kw) * unit_mva / float(base.sn_mva),
                               interpolated=True)


def build_stored_net(design_net: pp.pandapowerNet, units: int, unit_mva: float, inp: DistrictInput
                     ) -> tuple[pp.pandapowerNet, int]:
    """The stored net: the design net with the HV side and a fixed OLTC tap (DESIGN §5.5).

    The ext_grid of the MV busbar is replaced by an HV bus (``HV busbar <substation_id>``, ``kind`` ``hv_busbar``)
    with ``ext_grid(vm_pu = 1.0)`` and ``units`` rows ``HV/MV transformer 1..n`` of the sized type (its values are
    written into ``net.std_types`` from the variant snapshot; a 31.5 MVA type is created there). The discrete OLTC tap
    follows ``tap += round((vm(busbar) - busbar_vm_pu) / tap_step)``, clipped to the type's range, for at most
    :data:`OLTC_MAX_ITERATIONS` corrections (a negative tap raises the MV side). The returned net holds the load-case
    results at that tap, so a plain ``pp.runpp`` reproduces them without a controller.

    Returns:
        ``(net, tap)``.
    """
    params = inp.params
    net = copy.deepcopy(design_net)
    item = _hv_mv_type(params, float(inp.vn_kv), float(unit_mva))
    busbars = net.bus.index[net.bus["kind"] == "busbar"] if "kind" in net.bus.columns else net.ext_grid["bus"]
    busbar = int(busbars[0])
    net.ext_grid = net.ext_grid.iloc[0:0]
    geo = net.bus.at[busbar, "geo"] if "geo" in net.bus.columns else None
    hv = pp.create_bus(net, vn_kv=float(item.vn_hv_kv), name=f"HV busbar {inp.substation_id}", type="b")
    if geo is not None:
        net.bus.at[hv, "geo"] = geo
    if "kind" in net.bus.columns:
        net.bus.at[hv, "kind"] = "hv_busbar"
    pp.create_ext_grid(net, hv, vm_pu=1.0, name="HV grid")
    std_type = equipment.add_hv_mv_std_type(net, item)
    rows = [pp.create_transformer(net, hv, busbar, std_type=std_type, name=f"HV/MV transformer {i + 1}",
                                  tap_pos=int(item.tap_neutral)) for i in range(max(int(units), 1))]
    tap = int(item.tap_neutral)
    step = float(item.tap_step_percent) / 100.0
    converged = _runpp(net)
    for _ in range(OLTC_MAX_ITERATIONS):
        if not converged or step <= 0:
            break
        vm = float(net.res_bus.at[busbar, "vm_pu"])
        new = int(np.clip(tap + int(round((vm - float(params.busbar_vm_pu)) / step)), item.tap_min, item.tap_max))
        if new == tap:
            break
        tap = new
        net.trafo.loc[rows, "tap_pos"] = tap
        converged = _runpp(net)
    if not converged:
        logger.warning(f"MV grid {inp.mv_grid_id}: the stored net does not converge at OLTC tap {tap}")
    return net, tap


# ---------------------------------------------------------------------------------------------- voltage budget
def station_transformer(sn_kva: float) -> tuple[float, float]:
    """``(vk_percent, vkr_percent)`` of an MV/LV station transformer of the stored LV grids
    (:data:`STATION_TRANSFORMERS`: the smallest type at least as large, else the largest)."""
    for rating, vk, vkr in STATION_TRANSFORMERS:
        if sn_kva <= rating + 1e-9:
            return vk, vkr
    return STATION_TRANSFORMERS[-1][1], STATION_TRANSFORMERS[-1][2]


def station_drop_ratio(lv_sn_kva: float, sn_kva: float, power_factor: float) -> float:
    """The transformer drop of a station with the rating ``sn_kva`` of the MV model per drop with the rating
    ``lv_sn_kva`` of its LV grid, at the same load: ``(vkr cos φ + vkx sin φ) / S_n`` of the two ratings
    (:func:`station_transformer`). It scales the stored LV requirement of a station upsized for its
    station-busbar customers (``inputs.upsize_busbar_stations``); 1 without two positive ratings."""
    if not (math.isfinite(lv_sn_kva) and math.isfinite(sn_kva) and lv_sn_kva > 0 and sn_kva > 0) \
            or abs(sn_kva - lv_sn_kva) < 1e-9:
        return 1.0
    sin_phi = math.sqrt(max(1.0 - float(power_factor) ** 2, 0.0))

    def drop(rating: float) -> float:
        vk, vkr = station_transformer(rating)
        return (vkr * float(power_factor) + math.sqrt(max(vk * vk - vkr * vkr, 0.0)) * sin_phi) / rating

    return drop(sn_kva) / drop(lv_sn_kva)


def _transformer_rise(row: pd.Series, p_kw: float, q_kvar: float, params: Any) -> float:
    """Voltage rise (p.u.) over the station transformer in the feed-in cases (reverse flow), the largest case.

    ``((Σ_g P_gen,g · f_g − load_p · P_load) · vkr − (load_q · Q_load + tan φ_LV · Σ_g P_gen,g · f_g) · vkx) / S_n /
    100`` (DESIGN §5.6; the second Q term is 0 at the default LV cos φ 1).
    """
    sn = _value(row, "sn_kva")
    if sn <= 0:
        return 0.0
    vk, vkr = station_transformer(sn)
    vkx = math.sqrt(max(vk * vk - vkr * vkr, 0.0))
    tan_phi = _tan_phi(params.lv_pv_cos_phi)
    gen = {g: _value(row, f"gen_{g}_kw") for g in GENERATION_GROUPS}
    rises = []
    for case in FEED_IN_CASES:
        factors = params.feed_in_cases[case]
        p_gen = sum(gen[g] * float(factors[g]) for g in GENERATION_GROUPS)
        p = p_gen - float(factors["load_p"]) * p_kw
        q = float(factors["load_q"]) * q_kvar + tan_phi * p_gen
        rises.append((p * vkr - q * vkx) / sn / 100.0)
    return max(rises)


def _vm(cases: Mapping[str, CaseResult], names: Sequence[str], how: str) -> pd.Series:
    series = [cases[c].vm_pu for c in names if c in cases and cases[c].converged]
    if not series:
        return pd.Series(dtype=float)
    frame = pd.concat(series, axis=1)
    return frame.min(axis=1) if how == "min" else frame.max(axis=1)


def station_budgets(inp: DistrictInput, plan: TopologyPlan, cases: Mapping[str, CaseResult]
                    ) -> dict[int, StationBudget]:
    """Voltage budget and MV/LV off-load tap of every station (DESIGN §5.6), keyed by node key.

    Per station with the stored LV requirement ``r = lv_design_source_vm_pu`` (the MV voltage that puts its LV busbar
    at ``lv_reference_voltage_pu`` at its own coincident load, tap 0)::

        dT         = r - lv_ref
        V_low      = vm_load - busbar_band_pu              V_high = max(vm_feed_in) + busbar_band_pu
        k(t)       = 1 / (1 + t · mvlv_tap_step_percent / 100)
        lv_bus(t)  = V_low · k(t) - dT                     lv_feed(t) = V_high · k(t) + rise_T
        margin_low(t)  = lv_bus(t) - (lv_ref - lv_design_tolerance_pu)
        margin_high(t) = lv_feed_in_max_pu - lv_feed(t)
        t* = argmax min(margin_low, margin_high) over [mvlv_tap_min, mvlv_tap_max]; ties: the tap closest to 0
        band_conflict = min(margin_low(t*), margin_high(t*)) < 0;  tap_limit = margin_low(t*) < 0

    ``rise_T`` is the reverse-flow rise over the station transformer (:func:`station_transformer` values, the
    station's normal-case load and its LV-side generation). A station upsized in the MV model for its
    station-busbar customers (``sn_kva`` above ``lv_sn_kva``) scales ``dT`` by :func:`station_drop_ratio`.
    Without a converged feed-in case the tap is the one closest to 0 that meets the LV design point (else the most
    raising one) and the feed-in values are NaN.

    Stations are skipped (no entry) when the LV version has no ``lv_reference_voltage_pu``, the station has no
    stored requirement, or the load case gives it no voltage (not converged or unsupplied); :func:`node_flags`
    names the reason.
    """
    params = inp.params
    lv_ref = inp.lv.lv_reference_voltage_pu
    load = cases.get("load")
    if lv_ref is None or load is None or not load.converged:
        return {}
    keys = bus_keys(inp, plan)
    stations = [k for k in keys[1:] if k in inp.nodes.index and inp.nodes.at[k, "kind"] == "station"]
    if not stations:
        return {}
    loads = node_loads(inp, plan)
    vm_load = load.vm_pu
    vm_n1 = _vm(cases, N1_CASES, "min")
    vm_feed = _vm(cases, FEED_IN_CASES, "max")
    taps = np.arange(int(params.mvlv_tap_min), int(params.mvlv_tap_max) + 1)
    k = 1.0 / (1.0 + taps * float(params.mvlv_tap_step_percent) / 100.0)
    order = sorted(range(len(taps)), key=lambda i: (abs(int(taps[i])), int(taps[i])))
    band = float(params.busbar_band_pu)
    floor = float(lv_ref) - float(params.lv_design_tolerance_pu)
    budgets: dict[int, StationBudget] = {}
    for key in stations:
        row = inp.nodes.loc[key]
        r = _value(row, "lv_design_source_vm_pu", math.nan)
        v_load = float(vm_load.get(key, math.nan))
        if not (math.isfinite(r) and math.isfinite(v_load)):
            continue
        ratio = station_drop_ratio(_value(row, "lv_sn_kva", math.nan), _value(row, "sn_kva", math.nan),
                                   float(inp.lv.power_factor))
        d_t = (r - float(lv_ref)) * ratio
        lv_bus = (v_load - band) * k - d_t
        margin_low = lv_bus - floor
        v_feed = float(vm_feed.get(key, math.nan)) if len(vm_feed) else math.nan
        if math.isfinite(v_feed):
            p_kw = float(loads.at[key, "p_kw"]) if key in loads.index else 0.0
            q_kvar = float(loads.at[key, "q_kvar"]) if key in loads.index else 0.0
            lv_feed = (v_feed + band) * k + _transformer_rise(row, p_kw, q_kvar, params)
            margin_high = float(params.lv_feed_in_max_pu) - lv_feed
            score = np.minimum(margin_low, margin_high)
            best_score = max(score)
            best = next(i for i in order if score[i] >= best_score - 1e-12)
        else:
            lv_feed = np.full(len(taps), math.nan)
            margin_high = np.full(len(taps), math.inf)
            feasible = [i for i in order if margin_low[i] >= 0]
            best = feasible[0] if feasible else int(np.argmax(margin_low))
        lv_max_drop = _value(row, "lv_max_drop_pu", math.nan)
        budgets[key] = StationBudget(
            node_key=int(key),
            vm_load_pu=v_load,
            vm_n1_pu=float(vm_n1.get(key, math.nan)) if len(vm_n1) else math.nan,
            vm_feed_in_pu=v_feed,
            tap_pos=int(taps[best]),
            lv_busbar_vm_pu=float(lv_bus[best]),
            lv_min_vm_pu=float(lv_bus[best] - lv_max_drop) if math.isfinite(lv_max_drop) else math.nan,
            lv_busbar_vm_feed_in_pu=float(lv_feed[best]),
            band_conflict=bool(min(margin_low[best], margin_high[best]) < 0),
            tap_limit=bool(margin_low[best] < 0),
        )
    return budgets


def node_flags(inp: DistrictInput, plan: TopologyPlan, cases: Mapping[str, CaseResult],
               budgets: Mapping[int, StationBudget]) -> dict[int, list[str]]:
    """Electrical flags per node key with a bus (for ``mv_node.flags``); nodes without flags are left out.

    ``unsupplied`` (no load-case voltage although the case converged), ``not_n1_secure`` (stations and customers
    without an (n-1) requirement: stubs, T-offs, radials), ``band_conflict``, ``tap_limit``, and for stations
    without a budget ``no_lv_reference`` or ``no_lv_requirement``.
    """
    flags: dict[int, list[str]] = {}
    keys = bus_keys(inp, plan)
    secure = n1_nodes(inp, plan)
    load = cases.get("load")
    lv_ref = inp.lv.lv_reference_voltage_pu
    for key in keys[1:]:
        if key not in inp.nodes.index:
            continue
        kind = inp.nodes.at[key, "kind"]
        found: list[str] = []
        if load is not None and load.converged and not math.isfinite(float(load.vm_pu.get(key, math.nan))):
            found.append("unsupplied")
        if kind in ("station", "customer") and key not in secure:
            found.append("not_n1_secure")
        if kind == "station":
            budget = budgets.get(key)
            if budget is not None:
                if budget.band_conflict:
                    found.append("band_conflict")
                if budget.tap_limit:
                    found.append("tap_limit")
            elif lv_ref is None:
                found.append("no_lv_reference")
            elif not math.isfinite(_value(inp.nodes.loc[key], "lv_design_source_vm_pu", math.nan)):
                found.append("no_lv_requirement")
        if found:
            flags[int(key)] = found
    return flags
