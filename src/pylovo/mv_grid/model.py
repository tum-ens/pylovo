"""Shared data classes and protocols of the MV stage (contracts between its modules, no planning logic).

Every module of :mod:`pylovo.mv_grid` imports its inputs and outputs from here. The parameter objects
:class:`~pylovo.mv_grid.parameters.MvParameters` and :class:`~pylovo.mv_grid.parameters.LvVersionParameters` live
in :mod:`pylovo.mv_grid.parameters`; this module refers to them by name only.

Conventions: node keys are ``mv_node_id`` values (> 0) or temporary joint keys (< 0); lengths are in m, loads in kW
unless a name says otherwise, voltages in p.u.; geometries are shapely objects in ``TARGET_EPSG``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Protocol, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

CATEGORIES: tuple[str, ...] = ("Residential", "Commercial", "Public", "Unknown")   # order of every stats array
CASES: tuple[str, ...] = ("load", "n1a", "n1b", "feed_in_wind", "feed_in_pv")
NODE_KINDS = ("busbar", "station", "customer", "generator", "joint")
LINE_ROLES = ("ring", "stub", "dedicated", "generator", "tie")
LINE_KINDS = ("cable", "overhead", "mixed")
MEMBER_KINDS: tuple[str, ...] = ("station", "customer", "generator")   # ring members (the member cap counts them)

#: Columns of DistrictInput.nodes (index = mv_node_id, busbar row first).
NODE_COLUMNS: tuple[str, ...] = (
    "kind", "connection_hint",            # connection_hint: busbar | ring | dedicated | tee | excluded
    "x", "y", "attach_edge_id", "attach_fraction", "attach_offset_m", "bridged",
    "settlement_id", "urban", "plz", "plz_settlement_type",
    "inst_res", "units_res", "inst_com", "units_com", "inst_pub", "units_pub", "inst_unk", "units_unk",
    "p_fixed_kw",                         # customers: g_c * installed; 0 otherwise
    "gen_solar_kw", "gen_wind_kw", "gen_other_kw", "gen_level",   # gen_level: mv | lv (LV-side aggregate) | ''
    "sn_kva", "grid_result_id", "lv_design_source_vm_pu", "lv_max_drop_pu",
    "lv_sn_kva",                          # station rating of its LV grid; sn_kva is larger for an upsized station
)


@dataclass(frozen=True)
class LineType:
    """Electrical data of one MV line type (a pandapower standard type, resolved once per variant)."""

    name: str
    kind: str                 # 'cable' | 'overhead'
    vn_kv: float
    r_ohm_per_km: float
    x_ohm_per_km: float
    c_nf_per_km: float
    max_i_ka: float


@dataclass(frozen=True)
class HvMvType:
    """Data of one HV/MV transformer type (pandapower standard type, or interpolated for 31.5 MVA)."""

    name: str
    sn_mva: float
    vn_hv_kv: float
    vn_lv_kv: float
    vk_percent: float
    vkr_percent: float
    pfe_kw: float
    i0_percent: float
    shift_degree: float
    vector_group: str
    tap_side: str
    tap_neutral: int
    tap_min: int
    tap_max: int
    tap_step_percent: float
    interpolated: bool = False


@dataclass
class EdgeSet:                             # a district subgraph without geometries
    """Edges of a district subgraph of the routing graph, without geometries."""

    edge_id: np.ndarray                    # int64
    source: np.ndarray                     # int64 graph vertex ids
    target: np.ndarray                     # int64
    length_m: np.ndarray                   # float64
    in_settlement: np.ndarray              # bool
    settlement_id: np.ndarray              # int64, -1 outside settlements
    vertex_id: np.ndarray                  # int64, every vertex referenced by the edges
    vertex_xy: np.ndarray                  # (n_vertices, 2) float64, TARGET_EPSG


@dataclass
class DistrictInput:
    """Everything the pure district planner needs for one HV/MV supply district."""

    mv_grid_id: int
    substation_id: int
    vn_kv: float                           # nominal voltage of the district (mv_grid.vn_kv): its line and HV/MV types
    nodes: pd.DataFrame                    # NODE_COLUMNS, index mv_node_id, busbar first
    edges: EdgeSet
    params: "MvParameters"
    lv: "LvVersionParameters"
    known_units_mva: tuple[float, ...]     # OSM transformer ratings of the substation, or ()
    edge_geometry: Callable[[Sequence[int]], Mapping[int, Any]]   # edge ids -> shapely LineString (source→target)
    settlement_geoms: Mapping[int, Any] = field(default_factory=dict)   # settlement_id -> shapely (Multi)Polygon (P2 mixed spans)


@dataclass
class RunGraph:
    """In-memory routing graph of one district, split at the terminals."""

    csr: Any                               # scipy.sparse.csr_matrix, undirected, min weight per vertex pair
    n_vertices: int
    vertex_xy: np.ndarray                  # (n_vertices, 2) local vertices (graph vertices, split points, off-street terminals)
    terminal_vertex: np.ndarray            # local vertex per row of DistrictInput.nodes
    pieces: pd.DataFrame                   # piece_id, u, v, length_m, edge_id (-1: connector/bridge), f0, f1,
                                           # kind ('street'|'connector'|'bridge'), in_settlement, settlement_id
    pair_piece: dict[tuple[int, int], int] # (min(u, v), max(u, v)) -> piece_id of the minimum-weight piece
    n_bridges: int
    bridged_rows: np.ndarray               # bool per node row


@dataclass
class DistanceModel:
    """Link lengths and kinds between the terminals of a district, as used by the solvers."""

    terminals: np.ndarray                  # node row index per terminal; terminal 0 = busbar
    candidates: np.ndarray                 # terminal indices of the ring candidates
    dist_m: np.ndarray                     # (k, k) design distance used by the solvers; inf = unreachable
    street_m: np.ndarray                   # (k, k)
    straight_m: np.ndarray                 # (k, k)
    overhead: np.ndarray                   # (k, k) bool: the link is an overhead line
    straight: np.ndarray                   # (k, k) bool: the link geometry is a straight span
    xy: np.ndarray                         # (k, 2)
    satellite: np.ndarray | None = None    # (k,) bool: isolated rural station, a stub after the rings (no candidate)


def member_weights(kinds: Any, urban: Any, rural_ring_factor: float) -> np.ndarray:
    """Weight of each node for the member cap of a ring (:class:`RingLimits`): 1 for an urban member,
    ``1 / rural_ring_factor`` for a rural member, 0 for other kinds (busbar, joints).

    Args:
        kinds: Node kind per node (:data:`MEMBER_KINDS` count).
        urban: Urban flag per node.
        rural_ring_factor: How many times more rural than urban members a ring may hold (at least 1).
    """
    kinds = np.asarray(kinds, dtype=object).astype(str)
    urban = np.asarray(urban, dtype=bool)
    if urban.shape != kinds.shape:
        raise ValueError(f"urban must have the shape {kinds.shape} of kinds, got {urban.shape}")
    factor = float(rural_ring_factor)
    if not (np.isfinite(factor) and factor >= 1.0):
        raise ValueError(f"rural_ring_factor must be at least 1, got {rural_ring_factor!r}")
    return np.where(np.isin(kinds, MEMBER_KINDS), np.where(urban, 1.0, 1.0 / factor), 0.0)


@dataclass(frozen=True)
class RingLimits:
    """Limits of one open ring (normal operation and (n-1))."""

    max_members: int                       # cap of the weighted members (urban 1, rural 1 / rural_ring_factor)
    max_drop: float                        # p.u., normal
    max_drop_n1: float                     # p.u.
    utilization: float                     # normal
    n1_utilization: float
    max_half_m: float


@dataclass(frozen=True)
class RingCheck:
    """Result of the analytic check of one ring sequence."""

    feasible: bool
    open_section: int                      # m: section s_m (between positions m and m+1, 1-based) is open
    nop_position: int                      # 1-based ring position whose panel holds the open switch
    length_m: float
    half_a_m: float
    half_b_m: float
    drop_a: float                          # p.u.
    drop_b: float
    drop_n1: float                         # worst of the two (n-1) directions
    util_normal: float                     # max I / (I_max · parallel), normal
    util_n1: float
    p_ring_kw: float
    p_a_kw: float
    p_b_kw: float
    reason: str | None                     # count | length | current | drop | n1_current | n1_drop | unreachable | no_nop


class RingEvaluatorProtocol(Protocol):
    """What the solvers need from a ring evaluator."""

    def evaluate(self, seq: Sequence[int]) -> RingCheck: ...
    def ring_length(self, seq: Sequence[int]) -> float: ...
    def member_count(self, seq: Sequence[int]) -> float: ...


@dataclass
class SolverResult:
    """Rings found by a solver (terminal indices in ring order, busbar excluded)."""

    routes: list[list[int]]                # terminal indices in ring order, busbar excluded
    length_savings_m: float                # total length after the construction step
    length_final_m: float
    passes: int
    moves: int
    time_guard_hit: bool
    runtime_s: float
    diagnostics: dict = field(default_factory=dict)


@dataclass
class LinePlan:
    """One planned MV line (a circuit between two node keys)."""

    key: int                               # plan-local line key 0..n-1
    from_key: int                          # node key: mv_node_id (> 0) or a joint temp key (< 0)
    to_key: int
    role: str                              # LINE_ROLES
    kind: str                              # LINE_KINDS
    feeder_id: int | None
    ring_id: int | None
    length_m: float
    cable_m: float
    overhead_m: float
    straight_m: float
    bridge_m: float
    geometry: Any                          # shapely LineString, TARGET_EPSG
    std_type: str                          # pandapower name, or '<cable> + <overhead>' for mixed lines
    parallel: int = 1
    normally_open: bool = False
    open_at_key: int | None = None
    exit: str | None = None                # 'a' | 'b' for the two busbar sections of a ring, else None
    pieces: tuple[int, ...] = ()           # run-graph piece ids (trench accounting)


@dataclass
class RingPlan:
    """One planned ring, dedicated feeder, generator feeder or busbar radial."""

    ring_id: int
    kind: str                              # ring | dedicated | generator | radial
    members: list[int]                     # node keys from the A exit, joints and loop-in generators included
    nop_key: int | None
    check: RingCheck | None
    feeder_a: int | None = None
    feeder_b: int | None = None


@dataclass
class TopologyPlan:
    """The planned topology of one district: rings, lines, joints and the role of every node."""

    rings: list[RingPlan]
    lines: list[LinePlan]
    joints: dict[int, tuple[float, float]] # temp key (< 0) -> (x, y)
    node_role: dict[int, str]              # key -> busbar | ring | stub | dedicated | tee | loop_in | joint | excluded
    node_feeder: dict[int, int]
    node_ring: dict[int, int]
    node_position: dict[int, int]
    stub_parent: dict[int, int]
    diagnostics: dict


@dataclass
class CaseResult:
    """Power-flow result of one case."""

    case: str
    converged: bool
    vm_pu: pd.Series                       # by node key
    loading_percent: pd.Series             # by line key
    slack_p_mw: float
    slack_q_mvar: float


@dataclass(frozen=True)
class Violation:
    """One violated criterion of one case."""

    case: str
    kind: str                              # voltage_low | voltage_high | loading | not_converged
    element: str                           # bus | line
    key: int
    value: float
    limit: float
    ring_id: int | None


@dataclass(frozen=True)
class StationBudget:
    """Voltage budget and MV/LV tap of one station."""

    node_key: int
    vm_load_pu: float
    vm_n1_pu: float
    vm_feed_in_pu: float
    tap_pos: int
    lv_busbar_vm_pu: float
    lv_min_vm_pu: float
    lv_busbar_vm_feed_in_pu: float
    band_conflict: bool
    tap_limit: bool


@dataclass
class DistrictPlan:
    """Everything the planner returns for one district (persisted by the stage in one transaction)."""

    topology: TopologyPlan
    hv_mv_units: int
    hv_mv_unit_mva: float
    hv_mv_sizing: str
    oltc_tap: int
    cases: dict[str, CaseResult]
    violations: list[Violation]
    repairs: list[dict]
    budgets: dict[int, StationBudget]
    net: Any                               # stored pandapower net (HV side, fixed OLTC tap), columns node_key/line_key
    metrics: dict
    power_flow_status: str                 # converged | voltage_violation | loading_violation | not_converged
    peak_load_mw: float
    peak_feed_in_mw: float


@dataclass
class GridOutcome:
    """Outcome of one district worker."""

    mv_grid_id: int
    status: str                            # done | failed
    error: str | None
    runtime_s: float


def _plain(value: Any) -> Any:
    """``value`` with numpy scalars, arrays and tuples replaced by JSON-native Python objects."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    return value


def demand_line(anchor: Mapping[str, Any]) -> str:
    """The summary line of a regional demand anchor (``pylovo.mv_grid.inputs.demand_anchor``): a check mark, or a
    cross and ``approximate`` right after the kW per inhabitant when the population is only partly known."""
    per = anchor.get("kw_per_inhabitant")
    text = f"{per:.2f} kW per inhabitant" if per is not None else "no population"
    if anchor.get("approximate"):
        text += ", approximate"
    text = (f"{'✗' if anchor.get('approximate') else '✓'} Demand anchor: {text} "
            f"({anchor.get('coincident_mw', 0.0):,.1f} MW coincident, {anchor.get('population', 0):,} inhabitants of "
            f"{anchor.get('n_plz', 0)} PLZ; MV customers {anchor.get('customer_mw', 0.0):,.1f} MW at "
            f"{anchor.get('n_customer_nodes', 0)} nodes and {anchor.get('busbar_customer_mw', 0.0):,.1f} MW at station "
            "busbars")
    if anchor.get("excluded_mw"):
        text += f", HV customers {anchor['excluded_mw']:,.1f} MW not modelled"
    text += ")"
    if anchor.get("approximate"):
        notes = [f"{plz} ({100 * share:.0f} % of its area)" for plz, share in (anchor.get("partial_plz") or {}).items()]
        notes += [f"{plz} (no population)" for plz in anchor.get("plz_without_population") or []]
        text += "; PLZ only partly in the region or without population: " + ", ".join(notes)
    return text


def ties_line(ties: Mapping[str, Any]) -> str:
    """The summary line of the normally-open ties (``pylovo.mv_grid.interconnection.tie_summary``): count, km, the
    share of rings with a tie and the tie ends whose (n-1) takeover stays within the limits."""
    n = int(ties.get("n_ties") or 0)
    if not n:
        limit = float(ties.get("tie_max_length_m") or 0.0) / 1000.0
        return f"✓ Ties: none (no rings of different districts of the same voltage within {limit:g} km)"
    text = f"✓ Ties: {n:,} normally-open ties, {float(ties.get('tie_km') or 0.0):,.1f} km"
    if ties.get("n_rings"):
        share = ties.get("ring_share_with_tie")
        text += (f", {ties.get('n_rings_with_tie', 0):,} of {ties['n_rings']:,} rings with a tie"
                 + (f" ({100 * share:.0f} %)" if share is not None else ""))
    if ties.get("takeover_checked"):
        text += (f", takeover within the (n-1) limits at {ties.get('takeover_ok', 0):,} of "
                 f"{ties['takeover_checked']:,} tie ends")
    return text


def voltage_line(voltages: Mapping[str, Any]) -> str:
    """The summary line of the nominal voltages of the districts (``MV_NOMINAL_VOLTAGE_KV``)."""
    grids = {float(k): int(v) for k, v in (voltages.get("grids") or {}).items()}
    counts = ", ".join(f"{n:,} at {vn:g} kV" for vn, n in sorted(grids.items(), reverse=True)) or "no districts"
    if voltages.get("rule") == "auto":
        return (f"✓ Nominal voltage auto (10 kV from {float(voltages.get('threshold_mva_km2') or 0.0):g} MVA/km² "
                f"installed): {counts}")
    return f"✓ Nominal voltage: {counts}"


@dataclass
class MvRunSummary:
    """Summary of one run of the MV stage for one MV variant of an LV version.

    ``cable_share`` is the cable share of the circuit length as a fraction (0 to 1); ``demand`` is the regional
    demand anchor (``pylovo.mv_grid.inputs.demand_anchor``), empty when it was not computed; ``ties`` the summary of
    the normally-open ties (``pylovo.mv_grid.interconnection.tie_summary``), empty without the tie step;
    ``voltages`` the nominal voltage rule (``rule`` auto or fixed, ``threshold_mva_km2``) and the districts per
    voltage (``grids``: ``{"20": n, "10": m}``).
    """

    version_id: str
    variant_id: str
    status: str                            # done | partial | failed
    plz_requested: list[int]
    plz_used: list[int]
    plz_missing: list[int]
    grids_total: int
    grids_done: int
    grids_failed: int
    grids_pending: int
    n_stations: int
    n_customers: int
    n_generators: int
    circuit_km: float
    cable_share: float
    n_violation_grids: int
    n_band_conflicts: int
    warnings: list[str]
    elapsed_s: float
    demand: dict = field(default_factory=dict)
    ties: dict = field(default_factory=dict)
    voltages: dict = field(default_factory=dict)
    active_cases: tuple[str, ...] = CASES

    def describe(self) -> str:
        """The summary lines printed by the CLI; each starts with a check mark (variant done) or a cross."""
        mark = "✓" if self.status == "done" else "✗"
        counts = [f"{self.grids_done} done"]
        if self.grids_failed:
            counts.append(f"{self.grids_failed} failed")
        if self.grids_pending:
            counts.append(f"{self.grids_pending} pending")
        mode = "generation included" if any(case.startswith("feed_in_") for case in self.active_cases) else "load-only"
        lines = [
            f"{mark} MV variant {self.variant_id} of version {self.version_id}: {self.grids_total} grids "
            f"({', '.join(counts)}), {self.n_stations:,} stations, {self.n_customers:,} customers, "
            f"{self.n_generators:,} generators, {self.circuit_km:,.0f} km (cable {100 * self.cable_share:.0f} %), "
            f"{self.n_violation_grids:,} grids with violations, {self.n_band_conflicts:,} band conflicts, "
            f"{self.elapsed_s:,.0f} s, {mode} planning"
        ]
        if self.plz_missing:
            lines.append(f"✗ {len(self.plz_missing)} requested PLZ have no LV grids in version {self.version_id}: "
                         + ", ".join(str(plz) for plz in self.plz_missing))
        if self.voltages:
            lines.append(voltage_line(self.voltages))
        if self.ties:
            lines.append(ties_line(self.ties))
        if self.demand:
            lines.append(demand_line(self.demand))
        lines.extend(f"✗ {warning}" for warning in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """JSON-native dictionary of all fields (for ``mv_variant.summary``)."""
        return _plain(asdict(self))
