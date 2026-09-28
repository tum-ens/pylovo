"""Grid diagnostics: DSO-style indicators that explain voltage and loading problems.

The Grid Inspector shows *why* a grid misses a limit, in the words a distribution-grid
planner would use: "the feeder is too long for its load", "one large load at the feeder end
causes 46 % of the drop", "the station takes a quarter of the voltage band".

Structure of the result (see :func:`diagnose`):

* **Symptoms** say what is wrong: VT-01 voltage band, LD-01 overloaded cable (needs the
  on-demand power flow), TR-01 transformer loading, DE-01 pylovo's feeder design limit,
  DA-03 generation check.
* **Causes** say why. Each carries ``explains`` (the symptom ids), a contribution in
  percentage points (pp) and as a share of the symptom, and a ``basis``.
* **Practice findings** flag deviations from DSO planning practice without a symptom
  (at most ``warning``), **data findings** flag implausible building data (``info``, raised
  to ``warning`` when the building is a top-3 cause).

Voltage attribution is exact with a power flow (telescoping sum of bus voltage differences
along the path). Before the power flow, the drops of the stored operating point are replayed
with the same linear model that pylovo's feeder planning uses
(``dU = (P R + Q X) / Vn^2``), scaled by ``kappa`` so that the replay reproduces the stored
generation check at the weakest consumer. Loads are attributed by superposition: the
contribution of load *j* to the drop at bus *k* is ``(P_j R_c + Q_j X_c) / Vn^2`` with the
impedance of the path that *j* and *k* share.

This module is pure Python (no pandapower, no database). Its inputs are built by
:mod:`pylovo_api.diagnostics_data`. pylovo-owned limits come from the version's stored
``generation_parameters``; every other threshold is a heuristic listed in
:data:`DEFAULT_THRESHOLDS` and can be overridden with a ``GRID_DIAGNOSTICS`` block in
``config_analysis.yaml``.
"""
from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from collections import defaultdict
from typing import Any

from pylovo_api import topology
from pylovo_api.cabinets import number_cabinets, vertex_of

SQRT3 = math.sqrt(3.0)
SEVERITIES = ("critical", "warning", "info")
SEV_RANK = {"critical": 0, "warning": 1, "info": 2}

# Rule catalogue: order = display order within one severity and impact.
RULES: dict[str, dict[str, Any]] = {
    "VT-01": {"title": "Voltage band at the weakest consumer", "category": "voltage", "kind": "symptom", "scope": "feeder"},
    "LD-01": {"title": "Overloaded cable", "category": "loading", "kind": "symptom", "scope": "section", "needs_power_flow": True},
    "TR-01": {"title": "Transformer loading and headroom", "category": "transformer", "kind": "symptom", "scope": "grid"},
    "DE-01": {"title": "Feeder design voltage limit missed", "category": "design", "kind": "symptom", "scope": "feeder"},
    "DA-03": {"title": "Generation check status", "category": "data", "kind": "symptom", "scope": "grid"},
    "TR-02": {"title": "Transformer share of the voltage band", "category": "transformer", "kind": "cause", "scope": "grid"},
    "VT-02": {"title": "Section drop hotspot", "category": "voltage", "kind": "cause", "scope": "section"},
    "VT-03": {"title": "Dominant load", "category": "voltage", "kind": "cause", "scope": "building"},
    "VT-04": {"title": "Drop spread over many loads", "category": "voltage", "kind": "cause", "scope": "feeder"},
    "TP-01": {"title": "Long feeder", "category": "topology", "kind": "cause", "scope": "feeder"},
    "TP-02": {"title": "Unbalanced feeders at the station", "category": "topology", "kind": "cause", "scope": "grid"},
    "TP-03": {"title": "Station off the load centre", "category": "topology", "kind": "cause", "scope": "grid"},
    "TP-04": {"title": "Cable route detour", "category": "topology", "kind": "cause", "scope": "bus"},
    "TP-05": {"title": "Outlets at the station", "category": "topology", "kind": "practice", "scope": "grid"},
    "TP-06": {"title": "Consumers closer to another station", "category": "topology", "kind": "cause", "scope": "bus"},
    "LD-02": {"title": "Section sized without thermal reserve", "category": "loading", "kind": "practice", "scope": "section"},
    "LD-03": {"title": "Bottleneck compared with neighbouring sections", "category": "loading", "kind": "cause", "scope": "section"},
    "LD-04": {"title": "Feeder too heavy for one outlet", "category": "loading", "kind": "practice", "scope": "feeder"},
    "LD-05": {"title": "Large service connection", "category": "loading", "kind": "practice", "scope": "building"},
    "LD-06": {"title": "Heavy or dense feeder", "category": "loading", "kind": "cause", "scope": "feeder"},
    "DE-02": {"title": "Voltage-driven upsizing", "category": "design", "kind": "cause", "scope": "feeder"},
    "DE-03": {"title": "Design budget with the transformer exceeds the band", "category": "design", "kind": "cause", "scope": "bus"},
    "DE-04": {"title": "Service cable design", "category": "design", "kind": "practice", "scope": "line"},
    "DE-05": {"title": "Fault-loop tripping at the feeder end", "category": "design", "kind": "practice", "scope": "feeder"},
    "DA-01": {"title": "Household count implausible", "category": "data", "kind": "data", "scope": "building"},
    "DA-02": {"title": "Building load implausible or dominating", "category": "data", "kind": "data", "scope": "building"},
}
RULE_ORDER = {rule: i for i, rule in enumerate(RULES)}
CATEGORIES = ("transformer", "loading", "voltage", "design", "topology", "data")

# Heuristic thresholds (overridable through GRID_DIAGNOSTICS in config_analysis.yaml).
DEFAULT_THRESHOLDS: dict[str, Any] = {
    "voltage_margin_pu": 0.01,             # VT-01 warning margin with the power flow
    "voltage_margin_estimate_pu": 0.012,   # ... and before it (the estimate reads up to 0.2 pp optimistic)
    "overload_warning": 0.90,              # LD-01 warning above 90 % of the ampacity
    "trafo_planning_tolerance": 0.02,      # TR-01 tolerance on TRANSFORMER_PLANNING_UTILIZATION
    "trafo_pf_warning": 0.90,              # TR-01 power-flow loading warning
    "trafo_oversized": 0.30,               # TR-01 oversizing floor
    "trafo_drop_pp": 2.5,                  # TR-02 transformer drop that is worth a warning
    "cause_share_critical": 0.40,          # cause inherits the symptom severity from this share
    "cause_share_warning": 0.25,           # cause is a warning from this share
    "section_share": 0.15,                 # VT-02 minimum share of the drop
    "section_density_factor": 1.25,        # VT-02 drop per 100 m against the path mean
    "load_share": 0.20,                    # VT-03 dominant load
    "feeder_end_fraction": 0.70,           # VT-03 "at the feeder end"
    "load_current_share": 0.30,            # VT-03 current variant for overloaded sections
    "spread_n50": 8,                       # VT-04 loads that give half of the drop
    "long_feeder_m": 500.0,                # TP-01
    "imbalance_share": 0.60,               # TP-02
    "imbalance_even_factor": 1.5,          # TP-02 (1.5 x the even share)
    "light_feeder_utilisation": 0.50,      # TP-02 light feeder head utilisation
    "light_feeder_margin_pu": 0.03,        # TP-02 light feeder voltage margin
    "transfer_gap_m": 40.0,                # TP-02 transfer candidates
    "eccentricity": 1.0,                   # TP-03 offset / load radius
    "eccentricity_min_consumers": 10,
    "eccentricity_min_radius_m": 50.0,
    "detour_ratio": 3.0,                   # TP-04 info
    "detour_extra_m": 150.0,
    "detour_ratio_path": 2.0,              # TP-04 warning on a violated path
    "detour_min_air_m": 50.0,
    "outlets_warning": 8,                  # TP-05 (10 for 630 kVA and more)
    "outlets_warning_large": 10,
    "outlets_info": 6,
    "cabinet_ways_info": 6,
    "stub_max_consumers": 2,
    "stub_max_length_m": 30.0,
    "closer_station_ratio": 0.5,           # TP-06
    "closer_station_min_m": 100.0,
    "reserve_warning": 0.95,               # LD-02
    "reserve_info": 0.85,
    "station_zone_m": 30.0,                # LD-02 grouping note / LD-04 station exit
    "step_down_min": 0.80,                 # LD-03 (a)
    "step_down_delta": 0.15,
    "peer_ratio": 1.5,                     # LD-03 (b)
    "peer_window": 0.15,
    "peer_min": 5,
    "outlet_warning_a": 400.0,             # LD-04 (NH2 outlet fuse)
    "outlet_info_a": 315.0,
    "service_info_a": 63.0,                # LD-05 direct metering limit
    "households_info_fraction": 0.80,      # LD-06 (a)
    "density_factor": 2.0,                 # LD-06 (b)
    "density_min_length_m": 150.0,
    "density_head_utilisation": 0.80,
    "upsizing_share": 0.50,                # DE-02
    "upsizing_ampacity_factor": 1.5,
    "design_band_info_pp": 1.0,            # DE-03
    "service_drop_pf_pp": 1.0,             # DE-04 solved service drop
    "service_length_factor": 3.0,
    "service_length_min_m": 50.0,
    "fault_c_min": 0.95,                   # DE-05
    "fault_conductor_factor": 1.24,
    "fuse_ratings_a": [100, 125, 160, 200, 250, 315, 400, 500, 630],
    "fuse_trip_5s_a": {100: 580, 125: 715, 160: 950, 200: 1250, 250: 1650, 315: 2200, 400: 2840, 500: 3800,
                       630: 5100},
    # LD-02 derating of cables leaving the station together (approximates DIN VDE 0276-1000), by cable count
    "grouping_factors": {1: 1.0, 2: 0.85, 3: 0.75, 4: 0.68, 5: 0.64, 6: 0.60},
    "area_per_household_warning_m2": 30.0,  # DA-01
    "area_per_household_info_m2": 50.0,
    "households_factor": 3.0,
    "hall_footprint_m2": 800.0,
    "shed_footprint_m2": 30.0,
    "building_trafo_share": 0.25,          # DA-02 (info; warning from half a station)
    "building_trafo_share_warning": 0.50,
    "building_feeder_share": 0.40,
    "building_peak_factor": 5.0,
    "mv_threshold_band": 0.90,
    "stored_check_tolerance_pp": 0.1,      # DA-03
    "max_voltage_targets": 50,
    "max_causes_per_rule": 5,
}


def effective_thresholds(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """:data:`DEFAULT_THRESHOLDS` with the known keys of ``overrides`` applied.

    Tables (``fuse_trip_5s_a``, ``grouping_factors``) can be overridden entry by entry.
    """
    th = dict(DEFAULT_THRESHOLDS)
    for key, value in (overrides or {}).items():
        if key in th and value is not None:
            th[key] = {**th[key], **value} if isinstance(th[key], dict) and isinstance(value, dict) else value
    return th


def _x(scaling: float) -> str:
    """Load scaling as shown in the UI (×1.0, ×1.3)."""
    return f"{scaling:.1f}" if abs(scaling * 10 - round(scaling * 10)) < 1e-9 else f"{scaling:g}"

# Fallbacks for versions without stored generation parameters (pylovo's defaults).
_FALLBACK = {"vn": 400.0, "cos_phi": 0.95, "peak_load_household": 16.825, "min_vm_pu": 0.9, "max_vm_pu": 1.1,
             "planning_utilisation": 0.8, "feeder_drop_limit": 8.0, "service_drop_limit": 3.0,
             "split_max_ka": 0.85, "mv_threshold_kw": 100.0,
             "sim_factor": {"Residential": 0.07, "Commercial": 0.5, "Public": 0.6}}


# =========================================================================== helpers
def _tap_lift(tr: dict) -> tuple[float, int]:
    """LV voltage lift of the stored off-load tap: ``(pp, steps towards a higher LV voltage)``.

    pylovo's validation power flow may set the tap (``pylovo.station_voltage``). With the tap on
    the HV side the ratio becomes ``1 + n·step`` and the LV voltage scales with its inverse; with
    it on the LV side the LV voltage scales with ``1 + n·step``.
    """
    pos, neutral = _f(tr.get("tap_pos")), _f(tr.get("tap_neutral"))
    if pos is None or neutral is None:
        return 0.0, 0
    step = (_f(tr.get("tap_step_percent"), 2.5) or 0.0) / 100
    n = pos - neutral
    if str(tr.get("tap_side") or "hv").lower() == "lv":
        return 100 * n * step, round(n)
    return (100 * (1 / (1 + n * step) - 1) if 1 + n * step > 0 else 0.0), round(-n)


def _f(value: Any, default: float | None = None) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _r(value: Any, digits: int = 2) -> float | None:
    value = _f(value)
    return None if value is None else round(value, digits)


def _pct(value: float | None, digits: int = 0) -> str:
    return "–" if value is None else f"{value * 100:.{digits}f} %"


def _n(value: float | None, digits: int = 0) -> str:
    return "–" if value is None else f"{value:,.{digits}f}"


def _cap(severity: str, limit: str) -> str:
    """The milder of two severities."""
    return severity if SEV_RANK[severity] >= SEV_RANK[limit] else limit


def _worst(*severities: str | None) -> str | None:
    present = [s for s in severities if s]
    return min(present, key=lambda s: SEV_RANK[s]) if present else None


def _share_severity(share: float, symptom: str, th: dict) -> str:
    """Cause severity from its share of the symptom (capped by the symptom)."""
    if share >= th["cause_share_critical"]:
        return symptom
    if share >= th["cause_share_warning"]:
        return _cap("warning", symptom)
    return "info"


def _local_m(lon: float, lat: float, lon0: float, lat0: float) -> tuple[float, float]:
    return ((lon - lon0) * 111_320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 110_540.0)


def _dist_m(a: tuple[float, float] | None, b: tuple[float, float] | None) -> float | None:
    if not a or not b or None in a or None in b:
        return None
    x, y = _local_m(a[0], a[1], b[0], b[1])
    return math.hypot(x, y)


def _compass(dx: float, dy: float) -> str:
    angle = (math.degrees(math.atan2(dx, dy)) + 360) % 360
    return ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"][
        int((angle + 22.5) // 45) % 8]


def _quantile(values: list[float], q: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


_vertex = vertex_of  # shared with the grid detail payload (pylovo_api.cabinets)


def _upper(path: str) -> str:
    return path.split(".")[-1].upper()


# =========================================================================== parameters
class Params:
    """pylovo-owned parameters of a version, with their source for the 'Why?' popover."""

    def __init__(self, gp: dict | None):
        gp = gp or {}
        self.stored = bool(gp)
        lc = gp.get("load_calculation") or {}
        cd = gp.get("cable_dimensioning") or {}
        tp = gp.get("transformer_placement") or {}
        pfa = gp.get("power_flow_assessment") or {}
        self.source: dict[str, str] = {}

        def pick(key: str, value: Any, fallback: Any, path: str) -> Any:
            ok = _f(value) is not None if not isinstance(fallback, dict) else bool(value)
            self.source[key] = (f"version parameter {_upper(path)}" if ok else f"pylovo default ({_upper(path)})")
            return value if ok else fallback

        self.vn = float(pick("vn", cd.get("vn"), _FALLBACK["vn"], "cable_dimensioning.vn"))
        self.cos = float(pick("cos_phi", lc.get("default_power_factor"), _FALLBACK["cos_phi"],
                              "load_calculation.default_power_factor"))
        self.sin = math.sqrt(max(0.0, 1 - self.cos ** 2))
        self.tan = self.sin / self.cos if self.cos else 0.0
        self.peak_hh = float(pick("peak_load_household", lc.get("peak_load_household"),
                                  _FALLBACK["peak_load_household"], "load_calculation.peak_load_household"))
        sim = dict(_FALLBACK["sim_factor"])
        for cat in lc.get("consumer_categories") or []:
            if cat.get("definition") and _f(cat.get("sim_factor")) is not None:
                sim[cat["definition"]] = float(cat["sim_factor"])
        for key, value in (lc.get("sim_factor") or {}).items():
            if _f(value) is not None:
                sim[key] = float(value)
        self.sim = sim
        self.source["sim_factor"] = ("version parameter SIM_FACTOR" if lc.get("sim_factor") else "pylovo default")
        self.fallback = lc.get("household_fallback") or {}
        self.min_vm = float(pick("min_vm_pu", pfa.get("min_vm_pu"), _FALLBACK["min_vm_pu"],
                                 "power_flow_voltage_limits.min_vm_pu"))
        self.max_vm = float(pick("max_vm_pu", pfa.get("max_vm_pu"), _FALLBACK["max_vm_pu"],
                                 "power_flow_voltage_limits.max_vm_pu"))
        self.u_plan = float(pick("planning_utilisation", tp.get("transformer_planning_utilization"),
                                 _FALLBACK["planning_utilisation"], "transformer_placement.transformer_planning_utilization"))
        self.mapping = {str(k): sorted(v) for k, v in (tp.get("transformer_mapping") or {}).items()}
        self.position_tolerance = _f(tp.get("greenfield_trafo_position_tolerance"))
        self.feeder_limit = float(pick("feeder_drop_limit", cd.get("max_end_to_end_feeder_voltage_drop_percent"),
                                       _FALLBACK["feeder_drop_limit"],
                                       "cable_dimensioning.max_end_to_end_feeder_voltage_drop_percent"))
        self.service_limit = float(pick("service_drop_limit", cd.get("max_service_design_voltage_drop_percent"),
                                        _FALLBACK["service_drop_limit"],
                                        "cable_dimensioning.max_service_design_voltage_drop_percent"))
        self.split_ka = float(pick("split_max_ka", cd.get("feeder_split_max_current_ka"), _FALLBACK["split_max_ka"],
                                   "cable_dimensioning.feeder_split_max_current_ka"))
        self.mv_kw = float(pick("mv_threshold_kw", cd.get("mv_direct_connection_load_threshold_kw"),
                                _FALLBACK["mv_threshold_kw"], "cable_dimensioning.mv_direct_connection_load_threshold_kw"))
        self.shared_prefix_m = _f(cd.get("min_shared_prefix_length_m"))
        cables: dict[str, dict] = {}
        services: dict[str, dict] = {}
        for item in gp.get("equipment_data") or []:
            if item.get("typ") != "Cable" or _f(item.get("max_i_a")) is None:
                continue
            entry = {"name": item["name"], "max_i_a": float(item["max_i_a"]), "cost": _f(item.get("cost_eur"), 0.0),
                     "r": _f(item.get("r_mohm_per_km"), 0.0) / 1000, "x": _f(item.get("x_mohm_per_km"), 0.0) / 1000}
            (cables if item.get("grid_role") == "feeder" else services)[item["name"]] = entry
        self.feeder_cables = sorted(cables.values(), key=lambda c: c["max_i_a"])
        self.service_cables = services
        self.cable = {**services, **cables}

    def coincident_kw(self, cats: dict[str, list[float]]) -> float:
        """pylovo's grouped simultaneity: sum over categories of P (g + (1 - g) N^-3/4)."""
        total = 0.0
        for cat, (installed, units) in cats.items():
            if installed > 0 and units > 0:
                g = self.sim.get(cat, 1.0)
                total += installed * (g + (1 - g) * units ** -0.75)
        return total

    def design_current_a(self, kw: float) -> float:
        return kw * 1000.0 / (SQRT3 * self.vn * self.cos)

    def z(self, r: float, x: float) -> float:
        """Effective impedance for the voltage drop, R cos(phi) + X sin(phi)."""
        return r * self.cos + x * self.sin

    def household_capacity(self, max_i_a: float) -> int:
        """Largest household count one cable carries under pylovo's coincidence model."""
        limit_kw = SQRT3 * self.vn * max_i_a * self.cos / 1000.0
        g = self.sim.get("Residential", 0.07)
        lo, hi = 0, 20000
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.peak_hh * mid * (g + (1 - g) * mid ** -0.75) <= limit_kw:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def largest_feeder_cable(self) -> dict | None:
        return self.feeder_cables[-1] if self.feeder_cables else None

    def next_cable(self, std_type: str) -> dict | None:
        names = [c["name"] for c in self.feeder_cables]
        if std_type not in names:
            return None
        i = names.index(std_type)
        return self.feeder_cables[i + 1] if i + 1 < len(names) else None


# =========================================================================== grid model
class GridModel:
    """Directed radial tree of one grid with loads, currents and voltages.

    Args:
        inputs: Compact grid inputs (:func:`pylovo_api.diagnostics_data.load_inputs`).
        params: Version parameters.
        pf: Converged result of :func:`pylovo_api.powerflow.run`, or ``None``.
    """

    def __init__(self, inputs: dict, params: Params, pf: dict | None = None):
        p = self.p = params
        self.g = inputs.get("grid") or {}
        self.pf = pf if pf and pf.get("converged") else None
        self.scaling = float(self.pf.get("load_scaling") or 1.0) if self.pf else 1.0
        self.vm_ext = _f(inputs.get("vm_ext"), 1.0)
        self.buses = {b["pp_index"]: dict(b, role=topology.bus_role(b.get("name"))) for b in inputs.get("buses", [])}
        self.lines: dict[int, dict] = {}
        for raw in inputs.get("lines", []):
            line = dict(raw)
            line["length_km"] = (_f(line.get("length_m"), 0.0) or 0.0) / 1000
            line["role"] = line.get("role") or topology.line_role(line)
            line["parallel"] = int(line.get("parallel") or 1)
            if _f(line.get("r_ohm_per_km")) is None or _f(line.get("x_ohm_per_km")) is None:
                cable = p.cable.get(line.get("std_type")) or {}
                line["r_ohm_per_km"] = cable.get("r", 0.0)
                line["x_ohm_per_km"] = cable.get("x", 0.0)
            line["R"] = line["r_ohm_per_km"] * line["length_km"] / line["parallel"]
            line["X"] = line["x_ohm_per_km"] * line["length_km"] / line["parallel"]
            line["I_max"] = (_f(line.get("max_i_ka"), 0.0) or 0.0) * 1000 * line["parallel"] * (_f(line.get("df"), 1.0) or 1.0)
            self.lines[line["pp_index"]] = line
        topo = topology.analyse(list(self.buses.values()), list(self.lines.values()))
        self.station = set(topo["station"])
        self.bus_feeder = topo["bus_feeder"]
        self.line_feeder = topo["line_feeder"]
        self.dist = {b: d * 1000 for b, d in topo["distance_km"].items()}
        self.topo_feeders = topo["feeders"]
        # House connections straight at the station are not feeders (topology.analyse) but use an outlet.
        self.topo_direct = topo.get("direct", [])
        self.trafo = (inputs.get("trafo") or [{}])[0]
        self._build_tree()
        self._attach_buildings(inputs.get("buildings") or [])
        self._aggregate_loads(inputs.get("loads") or [])
        self._currents()
        self._sections()
        self._cabinets(inputs.get("splits") or [])
        self._feeders()
        self._voltages()

    # ---------------------------------------------------------------- structure
    def _build_tree(self) -> None:
        roots = [b for b, bus in self.buses.items() if bus["role"] == "lv_busbar"]
        self.root = roots[0] if roots else (min(self.station) if self.station else None)
        adjacency: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for lid, line in self.lines.items():
            adjacency[line["from_bus"]].append((line["to_bus"], lid))
            adjacency[line["to_bus"]].append((line["from_bus"], lid))
        self.parent: dict[int, int] = {}
        self.pline: dict[int, int] = {}
        self.children: dict[int, list[int]] = defaultdict(list)
        self.order: list[int] = []
        if self.root is None:
            return
        seen = {self.root}
        queue = [self.root]
        head = 0
        while head < len(queue):
            bus = queue[head]
            head += 1
            self.order.append(bus)
            for other, lid in adjacency[bus]:
                if other in seen:
                    continue
                seen.add(other)
                self.parent[other] = bus
                self.pline[other] = lid
                self.children[bus].append(other)
                queue.append(other)
        self.child_of = {lid: bus for bus, lid in self.pline.items()}
        self.depth = {self.root: 0}
        for bus in self.order[1:]:
            self.depth[bus] = self.depth[self.parent[bus]] + 1

    def path_lines(self, bus: int) -> list[int]:
        """Lines from the LV busbar to ``bus`` (station first)."""
        out = []
        while bus in self.pline:
            out.append(self.pline[bus])
            bus = self.parent[bus]
        return out[::-1]

    def subtree(self, bus: int) -> list[int]:
        out, stack = [], [bus]
        while stack:
            b = stack.pop()
            out.append(b)
            stack.extend(self.children.get(b, []))
        return out

    def _attach_buildings(self, buildings: list[dict]) -> None:
        by_vertex: dict[int, list[dict]] = defaultdict(list)
        for building in buildings:
            if building.get("vertice_id") is not None:
                by_vertex[int(building["vertice_id"])].append(building)
        self.bus_buildings: dict[int, list[dict]] = {}
        self.vertex_bus: dict[int, int] = {}
        self.building_bus: dict[str, int] = {}
        for b, bus in self.buses.items():
            vertex = _vertex(bus.get("name"))
            if vertex is None:
                continue
            if bus["role"] == "consumer":
                self.bus_buildings[b] = by_vertex.get(vertex, [])
                for building in self.bus_buildings[b]:
                    self.building_bus[str(building["objectid"])] = b
            elif bus["role"] == "connection":
                self.vertex_bus[vertex] = b

    def address(self, bus: int) -> str:
        names = [f"{x['street']} {x.get('house_number') or ''}".strip() for x in self.bus_buildings.get(bus, [])
                 if x.get("street")]
        return ", ".join(dict.fromkeys(names)) or (self.buses.get(bus, {}).get("name") or f"bus {bus}")

    def building_type(self, bus: int) -> str | None:
        types = sorted({x["type"] for x in self.bus_buildings.get(bus, []) if x.get("type")})
        return ", ".join(types) or None

    def households(self, bus: int) -> int:
        return int(sum(x.get("households") or 0 for x in self.bus_buildings.get(bus, [])))

    def building_ids(self, bus: int) -> list[str]:
        return [str(x["objectid"]) for x in self.bus_buildings.get(bus, [])]

    # ---------------------------------------------------------------- loads
    def _aggregate_loads(self, loads: list[dict]) -> None:
        s = self.scaling
        own: dict[int, dict] = {}
        for load in loads:
            bus = load["bus"]
            entry = own.setdefault(bus, {"p": 0.0, "q": 0.0, "cats": defaultdict(lambda: [0.0, 0.0]), "res": 0.0,
                                         "design": 0.0, "installed": 0.0})
            p_kw = (_f(load.get("p_mw"), 0.0) or 0.0) * 1000
            entry["p"] += p_kw * s
            entry["q"] += (_f(load.get("q_mvar"), 0.0) or 0.0) * 1000 * s
            installed = (_f(load.get("max_p_mw"), 0.0) or 0.0) * 1000
            units = _f(load.get("load_units"), 1.0) or 0.0
            cat = load.get("category") or "Residential"
            entry["cats"][cat][0] += installed
            entry["cats"][cat][1] += units
            entry["installed"] += installed
            entry["design"] += (_f(load.get("service_design_p_mw"), 0.0) or 0.0) * 1000
            if cat == "Residential":
                entry["res"] += units
        self.own = own
        self.consumers = [b for b in self.order if self.buses[b]["role"] == "consumer"]
        down: dict[int, dict] = {}
        for bus in reversed(self.order):
            base = own.get(bus)
            agg = {"p": base["p"] if base else 0.0, "q": base["q"] if base else 0.0,
                   "cats": {c: list(v) for c, v in (base["cats"].items() if base else [])},
                   "res": base["res"] if base else 0.0, "n": 1 if bus in own else 0}
            for child in self.children.get(bus, []):
                sub = down[child]
                agg["p"] += sub["p"]
                agg["q"] += sub["q"]
                agg["res"] += sub["res"]
                agg["n"] += sub["n"]
                for cat, (installed, units) in sub["cats"].items():
                    target = agg["cats"].setdefault(cat, [0.0, 0.0])
                    target[0] += installed
                    target[1] += units
            down[bus] = agg
        self.down = down

    def _currents(self) -> None:
        p = self.p
        pf = self.pf
        loading = {int(k): v for k, v in (pf or {}).get("line_loading_percent", {}).items()}
        current = {int(k): v for k, v in (pf or {}).get("line_current_a", {}).items()}
        for lid, line in self.lines.items():
            child = self.child_of.get(lid)
            sub = self.down.get(child) if child is not None else None
            if sub is None:
                line.update(I_d=0.0, I_s=0.0, u_d=0.0, u_s=0.0, P=0.0, Q=0.0, dU_lin=0.0)
                continue
            line["P"], line["Q"] = sub["p"], sub["q"]
            line["I_d"] = p.design_current_a(p.coincident_kw(sub["cats"]))
            line["I_s"] = math.hypot(sub["p"], sub["q"]) * 1000 / (SQRT3 * p.vn)
            line["u_d"] = line["I_d"] / line["I_max"] if line["I_max"] else 0.0
            line["u_s"] = line["I_s"] / line["I_max"] if line["I_max"] else 0.0
            line["dU_lin"] = 100 * (sub["p"] * 1000 * line["R"] + sub["q"] * 1000 * line["X"]) / p.vn ** 2
            line["I_pf"] = _f(current.get(lid)) if pf else None
            load = _f(loading.get(lid)) if pf else None
            line["u_pf"] = load / 100 if load is not None else None

    def _sections(self) -> None:
        sections: dict[int, dict] = {}
        for lid, line in self.lines.items():
            sid = line.get("feeder_section_id")
            if sid is None or lid not in self.child_of:
                continue
            sec = sections.setdefault(int(sid), {"id": int(sid), "lines": []})
            sec["lines"].append(lid)
        for sec in sections.values():
            sec["lines"].sort(key=lambda lid: self.dist.get(self.child_of[lid], 0))
            first, last = self.lines[sec["lines"][0]], self.lines[sec["lines"][-1]]
            sec["from_bus"] = self.parent[self.child_of[first["pp_index"]]]
            sec["to_bus"] = self.child_of[last["pp_index"]]
            sec["std_type"] = first["std_type"]
            sec["parallel"] = first["parallel"]
            sec["ampacity_std_type"] = first.get("ampacity_std_type")
            sec["ampacity_parallel"] = first.get("ampacity_parallel")
            sec["basis"] = first.get("feeder_sizing_basis")
            sec["length_m"] = sum(self.lines[lid]["length_m"] or 0 for lid in sec["lines"])
            sec["I_max"] = min(self.lines[lid]["I_max"] for lid in sec["lines"])
            worst = max(sec["lines"], key=lambda lid: self.lines[lid]["u_d"])
            sec["u_d"] = self.lines[worst]["u_d"]
            sec["I_d"] = max(self.lines[lid]["I_d"] for lid in sec["lines"])
            sec["I_s"] = max(self.lines[lid]["I_s"] for lid in sec["lines"])
            pf_lines = [lid for lid in sec["lines"] if self.lines[lid].get("u_pf") is not None]
            if pf_lines:
                top = max(pf_lines, key=lambda lid: self.lines[lid]["u_pf"])
                sec["u_pf"] = self.lines[top]["u_pf"]
                sec["pf_line"] = top
                sec["I_pf"] = self.lines[top].get("I_pf")
            else:
                sec["u_pf"], sec["pf_line"], sec["I_pf"] = None, None, None
            sec["feeder"] = self.line_feeder.get(first["pp_index"])
            sec["z"] = self.p.z(first["r_ohm_per_km"], first["x_ohm_per_km"]) / first["parallel"]
            up = self.pline.get(sec["from_bus"])
            sec["parent"] = self.lines[up].get("feeder_section_id") if up is not None else None
        self.sections = sections
        self.section_children: dict[int, list[int]] = defaultdict(list)
        for sid, sec in sections.items():
            if sec["parent"] is not None and sec["parent"] in sections:
                self.section_children[sec["parent"]].append(sid)

    def _cabinets(self, splits: list[dict]) -> None:
        # K1…Kn by cable distance: the same names as the inspector and the map (pylovo_api.cabinets)
        self.cabinets = {c["bus"]: c for c in number_cabinets(splits, self.vertex_bus, self.station, self.dist)}

    def node_label(self, bus: int, end: bool = False) -> str:
        if bus in self.station:
            return "station"
        if bus in self.cabinets:
            return self.cabinets[bus]["name"]
        feeder_children = [c for c in self.children.get(bus, []) if self.lines[self.pline[c]]["role"] == "feeder"]
        if end and not feeder_children:
            return "feeder end"
        return f"node {bus}"

    def section_label(self, sid: int) -> str:
        sec = self.sections[sid]
        par = f" ×{sec['parallel']}" if sec["parallel"] > 1 else ""
        return (f"S{sid} ({sec['std_type']}{par}, {sec['length_m']:.0f} m, "
                f"{self.node_label(sec['from_bus'])} → {self.node_label(sec['to_bus'], end=True)})")

    def _feeders(self) -> None:
        feeders: dict[int, dict] = {}
        for item in self.topo_feeders:
            feeders[item["feeder"]] = {"feeder": item["feeder"], "head": None, "consumers": [], "lines": [],
                                       "feeder_m": 0.0, "length_km": item["length_km"]}
        for lid, line in self.lines.items():
            f = self.line_feeder.get(lid)
            if f not in feeders:
                continue
            feeders[f]["lines"].append(lid)
            if line["role"] == "feeder":
                feeders[f]["feeder_m"] += line["length_m"] or 0.0
            if line["role"] != "link" and (line["from_bus"] in self.station or line["to_bus"] in self.station):
                head = feeders[f]["head"]
                if head is None or line["I_d"] > self.lines[head]["I_d"]:
                    feeders[f]["head"] = lid
        for bus in self.consumers:
            f = self.bus_feeder.get(bus)
            if f in feeders:
                feeders[f]["consumers"].append(bus)
        for f, info in feeders.items():
            head = self.lines.get(info["head"]) if info["head"] is not None else None
            cons = info["consumers"]
            info["head_role"] = head["role"] if head else None
            info["n_cons"] = len(cons)
            info["real"] = bool(head and head["role"] == "feeder" and len(cons) >= 3)
            info["reach_m"] = max((self.dist.get(b, 0.0) for b in cons), default=0.0)
            info["p_kw"] = sum(self.own.get(b, {}).get("p", 0.0) for b in cons)
            info["households"] = sum(self.households(b) for b in cons)
            info["res_units"] = sum(self.own.get(b, {}).get("res", 0.0) for b in cons)
            info["I_head_d"] = head["I_d"] if head else 0.0
            info["I_head_pf"] = head.get("I_pf") if head else None
            info["design_kw"] = info["I_head_d"] * SQRT3 * self.p.vn * self.p.cos / 1000
        self.feeders = feeders

    # ---------------------------------------------------------------- voltages
    def _voltages(self) -> None:
        lin: dict[int, float] = {}
        if self.root is not None:
            lin[self.root] = 0.0
            for bus in self.order[1:]:
                lin[bus] = lin[self.parent[bus]] + self.lines[self.pline[bus]]["dU_lin"]
        self.lin = lin
        tr = self.trafo
        self.sr_kva = (_f(tr.get("sn_mva"), 0.0) or 0.0) * 1000 * int(tr.get("parallel") or 1) or (_f(self.g.get("kva")) or 0.0)
        self.u_S = _f(self.g.get("utilisation"))
        if self.u_S is None and self.sr_kva:
            self.u_S = math.hypot(self.down.get(self.root, {}).get("p", 0.0), self.down.get(self.root, {}).get("q", 0.0)) / self.sr_kva
        self.vk = _f(tr.get("vk_percent"), 6.0)
        self.vkr = _f(tr.get("vkr_percent"), 1.2)
        self.tap_lift_pp, self.tap_steps = _tap_lift(tr)
        self.vm_lv_noload = self.vm_ext * (1 + self.tap_lift_pp / 100)   # LV busbar without load
        # Loading estimate for the transformer drop before the power flow: the coincident kVA plus
        # the line losses of the linear replay, at the reduced LV voltage (current rises as 1/vm).
        p_load = self.down.get(self.root, {}).get("p", 0.0) if self.root is not None else 0.0
        loss_kw = sum(3 * line["I_s"] ** 2 * line["R"] for line in self.lines.values()) / 1000
        u_s = self.u_S or 0.0
        vm_lv0 = self.vm_lv_noload - self.trafo_drop(u_s, self.vk) / 100
        self.u_T_est = u_s * (1 + (loss_kw / p_load if p_load > 0 else 0.0)) / (vm_lv0 or 1.0)
        self.dU_T_est = self.trafo_drop(self.u_T_est, self.vk)
        stored = _f(self.g.get("max_total_drop_pct"))
        worst_lin = max((lin.get(b, 0.0) for b in self.consumers), default=0.0)
        self.kappa = stored / worst_lin if stored and worst_lin > 1e-9 else 1.0
        self.kappa_basis = "stored" if stored and worst_lin > 1e-9 else "unit"
        self.vm_pf = {int(k): _f(v) for k, v in (self.pf or {}).get("bus_vm_pu", {}).items()} if self.pf else {}
        hv = tr.get("hv_bus")
        if self.pf and hv is not None and self.vm_pf.get(hv) is not None and self.vm_pf.get(self.root) is not None:
            self.vm_hv = self.vm_pf[hv]
            self.dU_T = 100 * (self.vm_hv - self.vm_pf[self.root])
            self.trafo_loading = max((_f(t.get("loading_percent"), 0.0) for t in self.pf.get("trafo", [])), default=0.0) / 100
        else:
            self.vm_hv = self.vm_ext
            self.dU_T = self.dU_T_est - self.tap_lift_pp * self.vm_ext   # the transformer including its tap
            self.trafo_loading = None
        self.dU_MV = 100 * (1 - self.vm_hv)
        self.estimated = self.pf is None

    def trafo_drop(self, loading: float, vk: float) -> float:
        """Transformer drop in pp at a loading (kVA / rating), from vk and vkr (with the second-order term)."""
        vkr = min(self.vkr, vk)
        vkx = math.sqrt(max(0.0, vk ** 2 - vkr ** 2))
        return (loading * (vkr * self.p.cos + vkx * self.p.sin)
                + (loading * (vkx * self.p.cos - vkr * self.p.sin)) ** 2 / 200)

    def vm(self, bus: int) -> float | None:
        if self.pf:
            return self.vm_pf.get(bus)
        if bus not in self.lin:
            return None
        return self.vm_lv_noload - (self.dU_T_est + self.kappa * self.lin[bus]) / 100

    def line_drop(self, lid: int) -> float:
        """Drop along one line in pp (power flow, or the anchored linear replay)."""
        child = self.child_of.get(lid)
        if child is None:
            return 0.0
        if self.pf:
            a, b = self.vm_pf.get(self.parent[child]), self.vm_pf.get(child)
            return 100 * (a - b) if a is not None and b is not None else 0.0
        return self.kappa * self.lines[lid]["dU_lin"]

    def lv_drop(self, bus: int) -> float:
        """Drop from the LV busbar to ``bus`` in pp."""
        if self.pf:
            a, b = self.vm_pf.get(self.root), self.vm_pf.get(bus)
            return 100 * (a - b) if a is not None and b is not None else 0.0
        return self.kappa * self.lin.get(bus, 0.0)

    def kappa_at(self, bus: int) -> float:
        if self.pf:
            lin = self.lin.get(bus, 0.0)
            return self.lv_drop(bus) / lin if lin > 1e-9 else 1.0
        return self.kappa

    def lv_buses(self, feeder: int | None = None) -> list[int]:
        return [b for b in self.order if float(self.buses[b].get("vn_kv") or 0.4) < 1.0
                and (feeder is None or self.bus_feeder.get(b) == feeder)]

    # ---------------------------------------------------------------- attribution
    def load_contributions(self, target: int) -> list[dict]:
        """Linear contribution of every loaded bus to the LV drop at ``target`` (superposition).

        Returns:
            Rows ``{bus, c_lin, shared_km, R, X}`` sorted by contribution; the ``c_lin`` add up
            to the linear LV drop at ``target``.
        """
        on_path: dict[int, tuple[float, float, float, int]] = {}
        if self.root is None:
            return []
        acc_r = acc_x = acc_km = 0.0
        on_path[self.root] = (0.0, 0.0, 0.0, 0)
        for i, lid in enumerate(self.path_lines(target), start=1):
            line = self.lines[lid]
            acc_r += line["R"]
            acc_x += line["X"]
            acc_km += line["length_km"]
            on_path[self.child_of[lid]] = (acc_r, acc_x, acc_km, i)
        attach: dict[int, int] = {}
        for bus in self.order:
            attach[bus] = bus if bus in on_path else attach.get(self.parent.get(bus), self.root)
        rows = []
        vn2 = self.p.vn ** 2
        for bus, own in self.own.items():
            if bus not in attach:
                continue
            r, x, km, idx = on_path[attach[bus]]
            c = 100 * (own["p"] * 1000 * r + own["q"] * 1000 * x) / vn2
            rows.append({"bus": bus, "c_lin": c, "shared_km": km, "attach_index": idx})
        rows.sort(key=lambda row: -row["c_lin"])
        return rows


# =========================================================================== findings
class Findings:
    """Collects findings, de-duplicates them per id and links causes to symptoms."""

    def __init__(self, model: GridModel, th: dict):
        self.m = model
        self.th = th
        self.items: dict[str, dict] = {}

    def add(self, rule: str, key: str, severity: str, title: str, message: str, *, metrics: dict | None = None,
            targets: dict | None = None, explains: list[str] | None = None, impact: float = 0.0,
            basis: str | None = None, remedy: str = "", why: str = "", thresholds: list[dict] | None = None,
            share: float | None = None, contribution_pp: float | None = None, estimated: bool = False,
            related: list[str] | None = None, headline: str | None = None, formula: str | None = None) -> dict:
        meta = RULES[rule]
        fid = f"{rule}:{key}"
        finding = {
            "id": fid, "rule": rule, "kind": meta["kind"], "severity": severity, "category": meta["category"],
            "scope": meta["scope"], "rule_title": meta["title"], "title": title, "message": message,
            "remedy": remedy, "why": why, "metrics": metrics or {}, "thresholds": thresholds or [],
            "targets": self._targets(targets or {}), "explains": list(dict.fromkeys(explains or [])),
            "related": list(dict.fromkeys(related or [])), "impact": round(float(impact or 0.0), 3),
            "basis": basis or ("estimate" if self.m.estimated else "power_flow"),
            "share": _r(share, 3), "contribution_pp": _r(contribution_pp, 2), "estimated": bool(estimated),
        }
        if headline:
            finding["headline"] = headline
        if formula:
            finding["formula"] = formula
        existing = self.items.get(fid)
        if existing is None or SEV_RANK[severity] < SEV_RANK[existing["severity"]]:
            if existing:
                finding["explains"] = list(dict.fromkeys(existing["explains"] + finding["explains"]))
            self.items[fid] = finding
        else:
            existing["explains"] = list(dict.fromkeys(existing["explains"] + finding["explains"]))
        return self.items[fid]

    def _targets(self, t: dict) -> dict:
        m = self.m
        lines = list(t.get("lines") or [])
        for sid in t.get("sections") or []:
            lines += m.sections.get(sid, {}).get("lines", [])
        buses = list(t.get("buses") or [])
        buildings = list(t.get("buildings") or [])
        if t.get("pin") is not None:  # the building of the pinned consumer (not of every listed bus)
            buildings += m.building_ids(t["pin"])
        cabinets = [m.cabinets[b]["split_bus"] for b in t.get("cabinet_buses") or [] if b in m.cabinets]
        return {"lines": sorted(set(int(x) for x in lines)), "buses": sorted(set(int(x) for x in buses)),
                "sections": sorted(set(int(x) for x in t.get("sections") or [])), "feeder": t.get("feeder"),
                "buildings": sorted(set(str(x) for x in buildings)), "cabinets": sorted(set(int(x) for x in cabinets)),
                "pin": t.get("pin"), "trafo": bool(t.get("trafo"))}

    def of(self, rule: str) -> list[dict]:
        return [f for f in self.items.values() if f["rule"] == rule]

    def get(self, fid: str) -> dict | None:
        return self.items.get(fid)


def _threshold(name: str, value: Any, source: str) -> dict:
    return {"name": name, "value": value, "source": source}


def _heur(name: str, key: str, th: dict, value: Any = None) -> dict:
    return _threshold(name, th[key] if value is None else value, f"heuristic, GRID_DIAGNOSTICS.{key.upper()}")


# =========================================================================== symptom rules
def _rule_vt01(m: GridModel, F: Findings, th: dict) -> dict[int, dict]:
    """VT-01 per feeder; returns the voltage targets {feeder: {bus, severity, id}}."""
    p = m.p
    targets: dict[int, dict] = {}
    status = m.g.get("power_flow_status")
    estimates = {}
    for f in m.feeders:
        buses = [b for b in m.lv_buses(f) if b not in m.station]
        cons = [b for b in m.feeders[f]["consumers"]]
        if not cons:
            continue
        vms = {b: m.vm(b) for b in buses}
        vms = {b: v for b, v in vms.items() if v is not None}
        k = min(cons, key=lambda b: vms.get(b, 9.9))
        estimates[f] = (k, vms.get(k))
    if not estimates:
        return targets
    global_worst = min(estimates, key=lambda f: estimates[f][1] if estimates[f][1] is not None else 9.9)
    margin = th["voltage_margin_estimate_pu"] if m.estimated else th["voltage_margin_pu"]
    for f, (k, vm_k) in estimates.items():
        if vm_k is None:
            continue
        buses = [b for b in m.lv_buses(f) if b not in m.station]
        low = [b for b in buses if (m.vm(b) or 9.9) < p.min_vm]
        high = [b for b in buses if (m.vm(b) or 0) > p.max_vm]
        if m.estimated:
            severity = None
            if status == "voltage_violation" and (f == global_worst or vm_k < p.min_vm):
                severity = "critical"
            elif vm_k < p.min_vm + margin:
                severity = "warning"
        else:
            severity = "critical" if (low or high) else ("warning" if vm_k < p.min_vm + margin else None)
        if severity is None:
            continue
        fid = f"VT-01:f{f}"
        budget = _budget(m, k)
        n_cons = sum(1 for b in low if m.buses[b]["role"] == "consumer")
        top = max((bar for bar in budget["bars"] if bar["key"].startswith("sec:")), key=lambda bar: bar["pp"],
                  default=None)
        feeder_pp = sum(bar["pp"] for bar in budget["bars"] if bar["key"].startswith("sec:") or bar["key"] == "other")
        service_pp = sum(bar["pp"] for bar in budget["bars"] if bar["key"] == "service")
        link_pp = sum(bar["pp"] for bar in budget["bars"] if bar["key"] == "link")
        address = m.address(k)
        tilde = "≈" if m.estimated else ""
        basis_text = ("stored generation check (loads ×1.0); voltages estimated with the linearised model"
                      if m.estimated else f"power flow ×{_x(m.scaling)}")
        if severity == "critical" and (low or m.estimated):
            if low:
                head = (f"Feeder {f}: {len(low)} buses ({n_cons} connections) below {p.min_vm:g} p.u.; weakest "
                        f"{address} ({m.dist.get(k, 0):.0f} m from the station) at {tilde}{vm_k:.3f} p.u.")
            else:
                head = (f"Feeder {f}: the generation check left the {p.min_vm:g}–{p.max_vm:g} p.u. band; weakest "
                        f"{address} ({m.dist.get(k, 0):.0f} m) at {tilde}{vm_k:.3f} p.u.")
            title = f"Voltage below {p.min_vm:g} p.u. on feeder {f}"
        elif severity == "critical":
            head = f"Feeder {f}: {len(high)} buses above {p.max_vm:g} p.u."
            title = f"Voltage above {p.max_vm:g} p.u. on feeder {f}"
        else:
            head = (f"Feeder {f} comes within {100 * (vm_k - p.min_vm):.1f} pp of the {p.min_vm:g} p.u. limit: weakest "
                    f"{address} ({m.dist.get(k, 0):.0f} m) at {tilde}{vm_k:.3f} p.u.")
            title = f"Voltage close to the limit on feeder {f}"
        top_text = f" (largest S{top['section']}: {top['pp']:.1f})" if top else ""
        message = (f"{head}{'' if head.endswith('.') else '.'} Budget: MV {m.dU_MV:.1f} + transformer {m.dU_T:.1f} + feeder {feeder_pp + link_pp:.1f}"
                   f"{top_text} + service {service_pp:.1f} pp = {100 * (1 - vm_k):.1f} % below 1.0 p.u. "
                   f"Basis: {basis_text}.")
        impact = 100 * (p.min_vm - vm_k)
        F.add("VT-01", f"f{f}", severity, title, message,
              metrics={"feeder": f, "vm_pu": _r(vm_k, 4), "bus": k, "address": address,
                       "distance_m": _r(m.dist.get(k), 0), "buses_below": len(low), "connections_below": n_cons,
                       "buses_above": len(high), "dU_MV": _r(m.dU_MV), "dU_T": _r(m.dU_T), "dU_feeder": _r(feeder_pp + link_pp),
                       "dU_service": _r(service_pp), "min_vm_pu": p.min_vm, "margin_pp": _r(100 * (vm_k - p.min_vm)),
                       "stored_status": status},
              targets={"buses": sorted(set(low) | {k}) if len(low) < 400 else [k], "feeder": f, "pin": k,
                       "lines": m.path_lines(k)},
              impact=impact, basis="stored_check" if m.estimated else "power_flow",
              thresholds=[_threshold("min_vm_pu", p.min_vm, p.source["min_vm_pu"]),
                          _threshold("max_vm_pu", p.max_vm, p.source["max_vm_pu"]),
                          _heur("warning margin (p.u.)", "voltage_margin_estimate_pu" if m.estimated else "voltage_margin_pu", th)],
              remedy="Work through the linked causes in order of contribution (see the voltage budget). Typical "
                     "measures: split the feeder at a cabinet into a second outlet, move or add a station, correct "
                     "implausible loads, or raise the transformer tap as a scenario.",
              why=(f"DIN EN 50160 allows ±10 % Un at the customer; pylovo classifies its validation power flow with "
                   f"POWER_FLOW_VOLTAGE_LIMITS {p.min_vm:g}/{p.max_vm:g} p.u. Critical: a bus leaves the band"
                   f"{' (stored generation check status = voltage_violation)' if m.estimated else ''}; warning: the "
                   f"weakest consumer is within {margin * 100:.1f} pp of it. The budget adds up the drop from the "
                   f"1.0 p.u. MV setpoint to {address}."),
              estimated=m.estimated,
              headline=f"Voltage {tilde}{vm_k:.3f} p.u. at {address}, feeder {f}")
        targets[f] = {"bus": k, "severity": severity, "id": fid, "vm": vm_k, "budget": budget}
    return targets


def _budget(m: GridModel, k: int) -> dict:
    """Voltage budget from the MV setpoint to bus ``k`` (bars in pp)."""
    per_section: dict[int, float] = defaultdict(float)
    link = service = other_feeder = 0.0
    lengths: dict[int, float] = defaultdict(float)
    for lid in m.path_lines(k):
        line = m.lines[lid]
        drop = m.line_drop(lid)
        if line["role"] == "service":
            service += drop
        elif line["role"] == "link":
            link += drop
        elif line.get("feeder_section_id") is not None:
            per_section[int(line["feeder_section_id"])] += drop
            lengths[int(line["feeder_section_id"])] += line["length_m"] or 0
        else:
            other_feeder += drop
    ranked = sorted(per_section.items(), key=lambda kv: -kv[1])
    bars = [{"key": "mv", "label": "MV setpoint", "pp": round(m.dU_MV, 3)},
            {"key": "trafo", "label": (f"Transformer ({m.g.get('size_label') or ''})".replace(" ()", "")
                                       + (f", tap {m.tap_steps:+d}" if m.tap_steps else "")),
             "pp": round(m.dU_T, 3), "targets": {"trafo": True}}]
    if link:
        bars.append({"key": "link", "label": "Busbar link", "pp": round(link, 3)})
    for sid, drop in ranked[:3]:
        bars.append({"key": f"sec:{sid}", "section": sid, "label": m.section_label(sid), "pp": round(drop, 3),
                     "path_m": round(lengths[sid], 1), "targets": {"lines": m.sections[sid]["lines"], "sections": [sid]}})
    rest = sum(drop for _, drop in ranked[3:]) + other_feeder
    if ranked[3:] or other_feeder:
        bars.append({"key": "other", "label": f"{len(ranked[3:])} other sections", "pp": round(rest, 3),
                     "targets": {"lines": [lid for sid, _ in ranked[3:] for lid in m.sections[sid]["lines"]]}})
    bars.append({"key": "service", "label": "Service cable", "pp": round(service, 3),
                 "targets": {"lines": [m.pline[k]] if k in m.pline else []}})
    vm_k = m.vm(k)
    return {"bus": k, "address": m.address(k), "feeder": m.bus_feeder.get(k), "vm_pu": _r(vm_k, 4),
            "min_vm_pu": m.p.min_vm, "margin_pp": _r(100 * ((vm_k or 0) - m.p.min_vm), 2), "bars": bars,
            "basis": "estimate" if m.estimated else "power_flow"}


def _rule_ld01(m: GridModel, F: Findings, th: dict, vt: dict[int, dict]) -> dict[int, dict]:
    """LD-01 per section (and per service); returns {section id or -line: info}."""
    out: dict[int, dict] = {}
    warn = th["overload_warning"]

    def emit(key: str, lids: list[int], lid: int, u: float, label: str, section: int | None, estimated: bool,
             link: bool = False) -> None:
        line = m.lines[lid]
        if estimated:
            severity = "warning"
        elif link:
            severity = "info"
        else:
            severity = "critical" if u > 1.0 else "warning"
        child = m.child_of.get(lid)
        sub = m.subtree(child) if child is not None else []
        loads = [b for b in sub if b in m.own]
        total_s = sum(math.hypot(m.own[b]["p"], m.own[b]["q"]) for b in loads) or 1.0
        top = sorted(loads, key=lambda b: -math.hypot(m.own[b]["p"], m.own[b]["q"]))[:3]
        top_text = ", ".join(f"{m.address(b)} {math.hypot(m.own[b]['p'], m.own[b]['q']) / total_s:.0%}" for b in top)
        vm_low = min((m.vm(b) for b in sub if m.vm(b) is not None), default=None)
        u_d = line["u_d"]
        c = line["I_s"] / line["I_d"] if line["I_d"] else None
        v = (line["I_pf"] / line["I_s"]) if (line.get("I_pf") and line["I_s"]) else None
        feeder = m.line_feeder.get(lid)
        par = f" ×{line['parallel']}" if line["parallel"] > 1 else ""
        start = f", from {m.node_label(m.parent[child])}" if child is not None and section is not None else ""
        if estimated:
            current = line["I_s"]
            message = (f"{label} ({line['std_type']}{par}, feeder {feeder}{start}) carries about {current:.0f} A "
                       f"= {u:.0%} of {line['I_max']:.0f} A at the stored operating point (estimated at nominal "
                       f"voltage; the power flow adds the current rise at low voltage). Design utilisation "
                       f"{u_d:.0%}. Largest downstream loads: {top_text or '–'}.")
        else:
            current = line.get("I_pf") or 0.0
            decomposition = (f": design {u_d:.0%} × coincidence {c:.2f} × voltage/loss {v:.2f}" if c and v else "")
            message = (f"{label} ({line['std_type']}{par}, feeder {feeder}{start}) carries {current:.0f} A "
                       f"= {u * 100:.1f} % of {line['I_max']:.0f} A{decomposition}"
                       f"{f' (lowest {vm_low:.3f} p.u. downstream)' if vm_low else ''}. "
                       f"Largest downstream loads: {top_text or '–'}.")
        if link:
            message += " The 1 m busbar link is a modelling artefact; it is reported for information."
        if u_d >= 1.0:
            remedy = "The section is undersized for its own design current: upsize it, add a parallel cable or split the feeder (LD-02, LD-04)."
        elif v and v > 1.0 and not estimated:
            remedy = ("The design current fits the rating; the overload comes from the current rise at low voltage "
                      "and the losses downstream. Fix the voltage causes of this feeder first (VT-*), otherwise "
                      "upsize the section or split the feeder.")
        else:
            remedy = "Upsize the section, add a parallel cable or move part of the downstream branch to another outlet."
        title = (f"{'Cable' if not link else 'Busbar link'} loaded to {u * 100:.0f} %" +
                 (f" (S{section})" if section is not None else f" ({m.address(child) if child is not None else ''})"))
        if estimated:
            title = f"Estimated cable loading {u * 100:.0f} %" + (f" (S{section})" if section is not None else "")
        fid = F.add(
            "LD-01", key, severity, title, message,
            formula=(f"u = I_s / I_max = {line['I_s']:.0f} / {line['I_max']:.0f} A = {u:.1%} (estimate at nominal voltage)"
                     if estimated else
                     (f"u = I / I_max = {current:.0f} / {line['I_max']:.0f} A = {u:.1%} = u_d {u_d:.3f} × c {c:.3f} × v {v:.3f}"
                      if c and v else f"u = I / I_max = {current:.0f} / {line['I_max']:.0f} A = {u:.1%}")),
            metrics={"section": section, "line": lid, "std_type": line["std_type"], "parallel": line["parallel"],
                     "I_a": _r(current, 1), "I_max_a": _r(line["I_max"], 1), "loading": _r(u, 4), "u_d": _r(u_d, 3),
                     "coincidence": _r(c, 3), "voltage_loss": _r(v, 3), "vm_low": _r(vm_low, 4), "feeder": feeder},
            targets={"lines": lids, "sections": [section] if section is not None else [], "feeder": feeder},
            impact=100 * (u - 1.0), basis="estimate" if estimated else "power_flow", estimated=estimated,
            remedy=remedy, related=[vt[feeder]["id"]] if feeder in vt and v and v > 1.0 else [],
            thresholds=[_threshold("ampacity", "max_i_ka × parallel", "cable catalogue (FEEDER_CABLES / CONSUMER_CONNECTION_CABLES)"),
                        _heur("warning from", "overload_warning", th)],
            why=("pandapower's loading is i / (max_i_ka × df × parallel); the max_i_a values of the catalogue apply "
                 "to cables laid in ground at load factor 0.7. The loading factorises into the design utilisation "
                 "(pylovo's cable sizing), the ratio of the snapshot current to the cable-level design current and "
                 "the current rise at reduced voltage plus the downstream losses."),
            headline=(f"{'Estimated loading' if estimated else 'Loading'} {u * 100:.0f} % on "
                      f"{('S' + str(section)) if section is not None else m.address(child)}"))["id"]
        out[section if section is not None else -lid] = {"id": fid, "severity": severity, "line": lid,
                                                         "section": section, "estimated": estimated}

    for sid, sec in m.sections.items():
        if m.pf:
            u = sec["u_pf"]
            if u is not None and u > warn:
                emit(f"s{sid}", sec["lines"], sec["pf_line"], u, f"S{sid}", sid, False)
        else:
            lid = max(sec["lines"], key=lambda x: m.lines[x]["u_s"])
            u = m.lines[lid]["u_s"]
            if u >= warn:
                emit(f"s{sid}", sec["lines"], lid, u, f"S{sid}", sid, True)
    for lid, line in m.lines.items():
        if line.get("feeder_section_id") is not None:
            continue
        u = line.get("u_pf") if m.pf else line["u_s"]
        if u is None or (u <= warn if m.pf else u < warn):
            continue
        child = m.child_of.get(lid)
        if line["role"] == "link":
            emit(f"l{lid}", [lid], lid, u, "Busbar link", None, m.estimated, link=True)
        else:
            emit(f"l{lid}", [lid], lid, u, f"Service to {m.address(child) if child is not None else lid}", None,
                 m.estimated)
    return out


def _rule_tr01(m: GridModel, F: Findings, th: dict) -> dict | None:
    p, g = m.p, m.g
    kva = _f(g.get("kva"))
    if not kva:
        return None
    s = m.scaling
    coincident_kw = _f(g.get("coincident_kw"), 0.0) or 0.0
    coincident_kva = _f(g.get("coincident_kva"), 0.0) or 0.0
    u_P = coincident_kw / kva
    u_S = coincident_kva / kva
    beta = m.trafo_loading
    severity = None
    if s * u_S > 1.0 or (beta is not None and beta > 1.0):
        severity = "critical"
    elif s * u_P > p.u_plan + th["trafo_planning_tolerance"] or (beta is not None and beta > th["trafo_pf_warning"]):
        severity = "warning"
    oversized = None
    ratings = p.mapping.get(str(g.get("settlement_type"))) or []
    smaller = [r for r in ratings if r < kva]
    if u_P < th["trafo_oversized"] and smaller and coincident_kw / max(smaller) <= p.u_plan and (_f(g.get("units")) or 1) <= 1:
        oversized = max(smaller)
        severity = severity or "info"
    if severity is None:
        return None
    n_res = sum(own.get("res", 0.0) for own in m.own.values())
    g_res = p.sim.get("Residential", 0.07)
    h_plan = p.u_plan * kva - s * coincident_kw
    h_th = kva - s * coincident_kva
    marginal = p.peak_hh * (g_res + 0.25 * (1 - g_res) * max(n_res, 1) ** -0.75)
    d_n = h_plan / marginal if marginal else None
    total = sum(f["p_kw"] for f in m.feeders.values()) or 1.0
    f_max = max(m.feeders.values(), key=lambda f: f["p_kw"], default=None)
    share_max = (f_max["p_kw"] / total) if f_max else None
    pf_text = f"; power flow ×{_x(s)}: {beta * 100:.0f} %" if beta is not None else ""
    next_kva = min((r for r in ratings if r > kva), default=None)
    message = (f"Station {g.get('size_label')}: {s * coincident_kw:.0f} kW / {s * coincident_kva:.0f} kVA coincident"
               f"{f' (loads ×{_x(s)})' if s != 1 else ''} = {s * u_P:.0%} of the rating in kW (planning target "
               f"{p.u_plan:.0%}){pf_text}. Headroom: {h_plan:.0f} kW to the target (about {d_n:.0f} households), "
               f"{h_th:.0f} kVA to the nameplate."
               + (f" Feeder {f_max['feeder']} carries {share_max:.0%} of the station load." if f_max else "")
               + (f" A {oversized} kVA unit would still meet the target." if oversized else ""))
    if severity == "info":
        title = f"Station oversized ({s * u_P:.0%} of {g.get('size_label')})"
        remedy = "Use the smaller rating, or enable MERGE_GREENFIELD_CLUSTERS for a new version."
    else:
        title = f"Station loaded to {max(s * u_S, beta or 0):.0%}" if severity == "critical" else \
            f"Station above its planning target ({max(s * u_P, beta or 0):.0%})"
        remedy = (f"The next rating of TRANSFORMER_MAPPING[{g.get('settlement_type')}]"
                  f"{f' ({next_kva} kVA)' if next_kva else ''}, a second station, or moving a branch at a cabinet to a "
                  f"neighbouring station (see TP-06). For a brownfield station, check transformer_rated_power.")
    F.add("TR-01", "grid", severity, title, message,
          formula=(f"u_P = coincident kW / kVA = {s * coincident_kw:.0f} / {kva:.0f} = {s * u_P:.0%} against "
                   f"{p.u_plan:.0%} + {th['trafo_planning_tolerance']:g}; u_S = {s * u_S:.0%}"
                   + (f"; power flow {beta:.0%}" if beta is not None else "")),
          metrics={"u_P": _r(u_P, 3), "u_S": _r(u_S, 3), "beta_pf": _r(beta, 3), "scaling": s, "kva": kva,
                   "headroom_kw": _r(h_plan, 1), "headroom_kva": _r(h_th, 1), "households_headroom": _r(d_n, 0),
                   "oversized_alternative_kva": oversized},
          targets={"trafo": True}, impact=100 * max(s * u_S - 1, (beta or 0) - 1, s * u_P - p.u_plan),
          basis="power_flow" if beta is not None else "design",
          thresholds=[_threshold("planning utilisation", p.u_plan, p.source["planning_utilisation"]),
                      _heur("tolerance", "trafo_planning_tolerance", th), _heur("power-flow warning", "trafo_pf_warning", th),
                      _heur("oversizing floor", "trafo_oversized", th)],
          remedy=remedy,
          why=("pylovo sizes stations with coincident kW / TRANSFORMER_PLANNING_UTILIZATION ≤ rating in kVA, so the "
               "planning test uses kW against kVA. 100 % is the ONAN nameplate; IEC 60076-7 allows cyclic overload, "
               "but not as a planning basis. Parallel units count as one station."),
          headline=f"Transformer {max(s * u_S, beta or 0):.0%} loaded" if severity != "info" else None)
    return {"severity": severity}


def _rule_de01(m: GridModel, F: Findings, th: dict, vt: dict[int, dict]) -> dict[int, dict]:
    p, g = m.p, m.g
    out: dict[int, dict] = {}
    if g.get("feeder_limit_met") is not False:
        return out
    selected = _f(g.get("design_feeder_drop_pct"))
    ampacity = _f(g.get("ampacity_feeder_drop_pct"))
    per_feeder: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for lid, line in m.lines.items():
        if line["role"] != "service":
            continue
        total, service = _f(line.get("design_drop_pct")), _f(line.get("service_drop_pct"), 0.0)
        if total is None:
            continue
        feeder_drop = total - (service or 0.0)
        if feeder_drop > p.feeder_limit + 1e-9:
            child = m.child_of.get(lid)
            per_feeder[m.line_feeder.get(lid)].append((feeder_drop, child))
    if not per_feeder:
        per_feeder[None] = []
    largest = p.largest_feeder_cable()
    for f, rows in per_feeder.items():
        rows.sort(key=lambda r: -r[0])
        worst = rows[0][1] if rows else None
        exhausted = None
        if worst is not None and largest:
            secs = {m.lines[lid].get("feeder_section_id") for lid in m.path_lines(worst)} - {None}
            exhausted = all(m.sections[s]["std_type"] == largest["name"] and
                            m.sections[s]["parallel"] == (m.sections[s].get("ampacity_parallel") or m.sections[s]["parallel"])
                            for s in secs if s in m.sections)
        linked = vt.get(f)
        severity = "critical" if linked and linked["severity"] == "critical" else "warning"
        message = (f"pylovo could not meet its {p.feeder_limit:g} % feeder design limit: {_n(selected, 2)} % after "
                   f"upsizing, {_n(ampacity, 2)} % with ampacity sizing only."
                   + (f" {len(rows)} connection points on feeder {f} exceed it." if f is not None else "")
                   + (f" The whole worst path already uses {largest['name']}." if exhausted else ""))
        key = f"f{f}" if f is not None else "grid"
        fid = F.add("DE-01", key, severity, f"Feeder design limit missed{f' on feeder {f}' if f else ''}", message,
                    metrics={"selected_pct": selected, "ampacity_pct": ampacity, "limit_pct": p.feeder_limit,
                             "connections_over": len(rows), "catalogue_exhausted": exhausted, "feeder": f},
                    targets={"buses": [r[1] for r in rows[:200] if r[1] is not None], "feeder": f, "pin": worst,
                             "lines": m.path_lines(worst) if worst is not None else []},
                    impact=(selected or 0) - p.feeder_limit, basis="design",
                    thresholds=[_threshold("feeder design limit (%)", p.feeder_limit, p.source["feeder_drop_limit"])],
                    remedy=("Conductor upsizing is exhausted, so change the topology: split the feeder, or add or move a "
                            "station (see VT-04, LD-04, TP-02, TP-03) and regenerate as a new version. Alternatively "
                            "extend FEEDER_CABLES or allow voltage-driven parallel cables in pylovo." if exhausted else
                            "Split the feeder, add or move a station, or extend FEEDER_CABLES, then regenerate."),
                    why=("MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT is pylovo's own planning limit (80 % of the "
                         "0.10 p.u. band). It covers the feeder only, at cable-level coincidence and nominal voltage. "
                         "pylovo upsizes conductors greedily but never adds parallel cables for voltage, so the limit "
                         "can stay unmet."),
                    related=[linked["id"]] if linked else [],
                    headline=f"Design drop {_n(selected, 2)} % > {p.feeder_limit:g} % limit")["id"]
        out[f] = {"id": fid, "severity": severity, "bus": worst}
    return out


def _rule_de03(m: GridModel, F: Findings, th: dict) -> dict[int, dict]:
    p, g = m.p, m.g
    out: dict[int, dict] = {}
    band = 100 * (1 - p.min_vm)
    dU_T = m.trafo_drop(_f(g.get("utilisation"), m.u_S or 0.0) or 0.0, m.vk)
    dU_MV = 100 * (1 - m.vm_ext)
    per_feeder: dict[int, list[tuple[float, int, int]]] = defaultdict(list)
    for lid, line in m.lines.items():
        if line["role"] != "service" or _f(line.get("design_drop_pct")) is None:
            continue
        budget = dU_MV + dU_T - m.tap_lift_pp * m.vm_ext + float(line["design_drop_pct"])
        per_feeder[m.line_feeder.get(lid)].append((budget, m.child_of.get(lid), lid))
    agrees = g.get("power_flow_status") == "voltage_violation"
    for f, rows in per_feeder.items():
        rows.sort(key=lambda r: -r[0])
        best, k, lid = rows[0]
        if best <= band - th["design_band_info_pp"]:
            continue
        severity = "warning" if best > band else "info"
        over = [r for r in rows if r[0] > band]
        line = m.lines[lid]
        service = _f(line.get("service_drop_pct"), 0.0) or 0.0
        feeder_drop = float(line["design_drop_pct"]) - service
        message = (f"Design budget to {m.address(k)}: transformer about {dU_T:.1f} + feeder {feeder_drop:.1f} + "
                   f"service {service:.1f} = {best:.1f} %, {'above' if best > band else 'close to'} the {band:.0f} % band"
                   f"{f' ({len(over)} connections above it)' if len(over) > 1 else ''}. "
                   + ("The generation check confirms a violation." if agrees else
                      "The generation check stayed in the band because the snapshot load is below the cable-level "
                      "design load."))
        fid = F.add("DE-03", f"f{f}", severity, f"Design voltage budget {best:.1f} % on feeder {f}", message,
                    formula=(f"B = dU_MV + dU_T,est + total design drop = {dU_MV:.2f} + {dU_T:.2f} + "
                             f"{float(line['design_drop_pct']):.2f} = {best:.2f} % against {band:.0f} %"),
                    metrics={"budget_pct": _r(best), "band_pct": band, "dU_T": _r(dU_T), "feeder_pct": _r(feeder_drop),
                             "service_pct": _r(service), "connections_over": len(over), "feeder": f, "agrees": agrees},
                    targets={"buses": [r[1] for r in over[:200] if r[1] is not None] or [k], "feeder": f, "pin": k,
                             "lines": m.path_lines(k), "trafo": True},
                    impact=best - band, basis="design",
                    thresholds=[_threshold("band (%)", band, p.source["min_vm_pu"]),
                                _heur("info band (pp)", "design_band_info_pp", th)],
                    remedy=("For new versions set MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT to about band − transformer "
                            f"− service drop (about {band - dU_T - 1:.0f} %), or model the tap and MV setpoint as a "
                            "scenario. See also DE-01 and TR-02."),
                    why=("pylovo's feeder (8 %) and service (3 %) design limits are measured from the LV busbar and leave "
                         "out the transformer, which takes 1.6–2.6 pp at these loadings (vk 6 %). A design that meets "
                         "both limits can still leave the DIN EN 50160 band. The design drop uses cable-level "
                         "coincidence, which is more conservative than the snapshot, so this is a warning."))["id"]
        out[f] = {"id": fid, "severity": severity, "bus": k}
    return out


def _rule_da03(m: GridModel, F: Findings, th: dict, pf: dict | None) -> None:
    g = m.g
    status = g.get("power_flow_status")
    if status == "not_converged" or (pf is not None and pf.get("converged") is False):
        F.add("DA-03", "grid", "critical", "Power flow did not converge",
              (f"Generation check (stored when the grid was saved, loads ×1.0): {status}."
               + (f" Power flow now (×{_x(pf.get('load_scaling', 1))}): not converged ({pf.get('error') or 'no solution'})."
                  if pf is not None and pf.get("converged") is False else "")),
              targets={}, impact=100, basis="stored_check",
              remedy="Look at DA-02 (implausible loads) and run the power flow at load scaling 0.5 to locate the problem.",
              why="A power flow that does not converge usually means loads far beyond what the network can carry.")
        return
    if m.pf is None or abs(m.scaling - 1.0) > 1e-9:
        return
    stored = _f(g.get("max_total_drop_pct"))
    vm_lv = m.vm_pf.get(m.root)
    vm_min = _f(m.pf.get("min_vm_pu"))
    if stored is None or vm_lv is None or vm_min is None:
        return
    now = 100 * (vm_lv - vm_min)
    pf_status = "voltage_violation" if (vm_min < m.p.min_vm or (_f(m.pf.get("max_vm_pu"), 0) or 0) > m.p.max_vm) else "converged"
    if abs(now - stored) > th["stored_check_tolerance_pp"] or (status and pf_status != status):
        F.add("DA-03", "grid", "info", "Stored generation check differs from the power flow",
              (f"Generation check (stored when the grid was saved, loads ×1.0): {status}, max drop {stored:.2f} % below "
               f"the LV busbar. Power flow now (×1.0): {pf_status.replace('_', ' ')}, {now:.2f} %. The stored check is "
               "outdated; recompute it to update the statistics."),
              metrics={"stored_pct": stored, "power_flow_pct": _r(now), "stored_status": status, "pf_status": pf_status},
              targets={}, impact=abs(now - stored), basis="power_flow",
              thresholds=[_heur("tolerance (pp)", "stored_check_tolerance_pp", th)],
              remedy="After edits, recompute and store the check (the editing feature does this).",
              why=("The Statistics panel, the status badge and 'Max. drop (stored)' come from the validation power "
                   "flow pylovo runs when it saves the grid. The comparison uses the drop below the LV busbar."))


# =========================================================================== voltage causes
def _voltage_targets(m: GridModel, vt: dict[int, dict], de03: dict[int, dict], th: dict) -> dict[int, dict]:
    """Targets for the voltage causes: VT-01 feeders, plus DE-03 warnings without VT-01."""
    targets: dict[int, dict] = {}
    for f, info in vt.items():
        targets[f] = {"bus": info["bus"], "severity": info["severity"], "explains": [info["id"]]}
        if f in de03 and de03[f]["severity"] != "info":
            targets[f]["explains"].append(de03[f]["id"])
    for f, info in de03.items():
        if f in targets or info["severity"] == "info" or info["bus"] is None:
            continue
        # The attribution explains the snapshot drop, so it targets the feeder's weakest consumer.
        cons = [b for b in m.feeders.get(f, {}).get("consumers", []) if m.vm(b) is not None]
        k = min(cons, key=m.vm) if cons else info["bus"]
        targets[f] = {"bus": k, "severity": info["severity"], "explains": [info["id"]]}
    ranked = sorted(targets.items(), key=lambda kv: m.vm(kv[1]["bus"]) or 9.9)[: th["max_voltage_targets"]]
    return dict(ranked)


def _rule_vt02(m: GridModel, F: Findings, th: dict, targets: dict[int, dict]) -> set[int]:
    p = m.p
    flagged: set[int] = set()
    for f, t in targets.items():
        k = t["bus"]
        lv = m.lv_drop(k)
        if lv <= 0:
            continue
        per: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
        feeder_drop = feeder_len = 0.0
        for lid in m.path_lines(k):
            line = m.lines[lid]
            if line["role"] != "feeder":
                continue
            drop = m.line_drop(lid)
            feeder_drop += drop
            feeder_len += line["length_m"] or 0
            if line.get("feeder_section_id") is not None:
                per[int(line["feeder_section_id"])][0] += drop
                per[int(line["feeder_section_id"])][1] += line["length_m"] or 0
        mean = 100 * feeder_drop / feeder_len if feeder_len else 0.0
        ranked = sorted(per.items(), key=lambda kv: -kv[1][0])
        emitted = 0
        for sid, (drop, length) in ranked:
            share = drop / lv
            density = 100 * drop / length if length else 0.0
            if share < th["section_share"] or density < th["section_density_factor"] * mean or emitted >= 3:
                continue
            emitted += 1
            sec = m.sections[sid]
            nxt = p.next_cable(sec["std_type"])
            cur = p.cable.get(sec["std_type"]) or {}
            z_cur = p.z(cur.get("r", sec["z"]), cur.get("x", 0.0)) if cur else None
            gain_up = drop * (1 - p.z(nxt["r"], nxt["x"]) / z_cur) if nxt and z_cur else None
            gain_par = drop / (sec["parallel"] + 1)
            largest = p.largest_feeder_cable()
            is_largest = bool(largest and sec["std_type"] == largest["name"] and
                              sec["parallel"] == (sec.get("ampacity_parallel") or sec["parallel"]))
            current = sec.get("I_pf") if m.pf else sec["I_s"]
            severity = _share_severity(share, t["severity"], th)
            tilde = "≈" if m.estimated else ""
            message = (f"{m.section_label(sid)} carrying {tilde}{current or 0:.0f} A takes {tilde}{drop:.1f} pp "
                       f"({share:.0%}) of the drop to {m.address(k)}: {density:.2f} %/100 m against a path mean of "
                       f"{mean:.2f}."
                       + (f" {nxt['name']}: −{gain_up:.1f} pp;" if gain_up else "")
                       + f" {'O' if not gain_up else 'o'}ne more parallel cable: −{gain_par:.1f} pp."
                       + (" Already the largest configured conductor." if is_largest else ""))
            F.add("VT-02", f"s{sid}:f{f}", severity, f"S{sid} takes {share:.0%} of the drop on feeder {f}", message,
                  formula=(f"share = dU_S{sid} / dU_LV = {drop:.2f} / {lv:.2f} pp = {share:.0%} (≥ {th['section_share']:.0%}); "
                           f"density {density:.2f} ≥ {th['section_density_factor']:g} × {mean:.2f} %/100 m"),
                  metrics={"section": sid, "dU_pp": _r(drop), "share": _r(share, 3), "density": _r(density, 3),
                           "path_mean": _r(mean, 3), "gain_next_pp": _r(gain_up), "gain_parallel_pp": _r(gain_par),
                           "largest_conductor": is_largest, "current_a": _r(current, 1), "feeder": f},
                  targets={"sections": [sid], "feeder": f, "cabinet_buses": [sec["from_bus"], sec["to_bus"]]},
                  explains=t["explains"], share=share, contribution_pp=drop, impact=drop, estimated=m.estimated,
                  thresholds=[_heur("minimum share", "section_share", th),
                              _heur("density factor", "section_density_factor", th)],
                  remedy=(f"Upsize or add a parallel cable on this section, or cut its current by moving the downstream "
                          f"branch at {m.node_label(sec['to_bus'], True)} to another outlet."
                          + (" It already is the largest conductor: change the topology (TP-01, TP-02, LD-04)." if is_largest else "")),
                  why=("Upsizing one section only helps if it dominates the path drop. The gain uses the same linear "
                       "model as pylovo's feeder design (√3 I L (R cos φ + X sin φ) / n)."))
            flagged.add(sid)
    return flagged


def _rule_vt03_vt04(m: GridModel, F: Findings, th: dict, targets: dict[int, dict], de01: dict[int, dict]) -> dict:
    """VT-03 (dominant load) and VT-04 (spread) per voltage target; returns top-3 cause buses."""
    p = m.p
    top_causes: dict[int, list[str]] = defaultdict(list)
    design_kw = [m.own[b]["design"] for b in m.consumers if b in m.own]
    p90 = _quantile(design_kw, 0.9)
    spread_targets = dict(targets)
    for f, info in de01.items():
        if f is None or info.get("bus") is None:
            continue
        if f in spread_targets:
            spread_targets[f] = dict(spread_targets[f], spread_explains=spread_targets[f]["explains"] + [info["id"]])
        else:
            spread_targets[f] = {"bus": info["bus"], "severity": _cap("warning", info["severity"]),
                                 "explains": [info["id"]]}
    for f, t in spread_targets.items():
        k = t["bus"]
        rows = m.load_contributions(k)
        total = sum(r["c_lin"] for r in rows)
        if total <= 0:
            continue
        kappa = m.kappa_at(k)
        reach = m.feeders.get(f, {}).get("reach_m") or 0.0
        acc, n50 = 0.0, 0
        for r in rows:
            acc += r["c_lin"]
            n50 += 1
            if acc >= 0.5 * total:
                break
        dominant = False
        emitted = 0
        for rank, r in enumerate(rows):
            share = r["c_lin"] / total
            if rank < 3 and share >= 0.10:
                top_causes[r["bus"]].append(t["explains"][0])
            if share < th["load_share"] or f not in targets:
                continue
            dominant = True
            if emitted >= th["max_causes_per_rule"]:
                continue
            emitted += 1
            j = r["bus"]
            own = m.own[j]
            d = m.dist.get(j, 0.0)
            pos = d / reach if reach else None
            at_end = pos is not None and pos >= th["feeder_end_fraction"] and m.bus_feeder.get(j) == f
            large = p90 is not None and own["design"] >= p90
            cats = ", ".join(sorted(c for c, v in own["cats"].items() if v[0] > 0))
            hh = m.households(j)
            fallback = _fallback_flag(m, j)
            c_pp = kappa * r["c_lin"]
            severity = _share_severity(share, t["severity"], th)
            tilde = "≈" if m.estimated else ""
            flags = []
            if at_end:
                flags.append("at the feeder end")
            if large:
                flags.append("one of the largest loads of the grid")
            message = (f"{m.address(j)} ({m.building_type(j) or 'building'}, {hh} households / {cats}; "
                       f"{own['p']:.0f} kW snapshot, {own['installed']:.0f} kW installed; {d:.0f} m"
                       + (f" = {pos:.0%} of the feeder reach" if pos is not None and m.bus_feeder.get(j) == f else "")
                       + f") causes {tilde}{c_pp:.1f} pp ({share:.0%}) of the drop to {m.address(k)}."
                       + (f" It is {' and '.join(flags)}." if flags else "")
                       + f" Half of the drop comes from {n50} load(s)."
                       + (" Its household count matches pylovo's floor-area fallback." if fallback else ""))
            F.add("VT-03", f"b{j}:f{f}", severity,
                  f"{m.address(j)} causes {share:.0%} of the drop on feeder {f}", message,
                  formula=(f"c_j = κ · 100 · (P_j R_c + Q_j X_c) / Vn² = {kappa:.3f} × {r['c_lin']:.2f} pp = {c_pp:.2f} pp; "
                           f"share = {r['c_lin']:.2f} / {total:.2f} = {share:.0%} (≥ {th['load_share']:.0%}); shared path "
                           f"{r['shared_km'] * 1000:.0f} m"),
                  metrics={"bus": j, "share": _r(share, 3), "contribution_pp": _r(c_pp), "snapshot_kw": _r(own["p"], 1),
                           "installed_kw": _r(own["installed"], 1), "design_kw": _r(own["design"], 1), "distance_m": _r(d, 0),
                           "reach_position": _r(pos, 3), "n50": n50, "at_feeder_end": at_end, "large_load": large,
                           "households": hh, "feeder": f, "fallback_households": fallback},
                  targets={"buses": [j], "feeder": f, "pin": j, "lines": m.path_lines(j)}, explains=t["explains"], share=share,
                  contribution_pp=c_pp, impact=c_pp, estimated=m.estimated,
                  thresholds=[_heur("dominant share", "load_share", th), _heur("feeder end", "feeder_end_fraction", th)],
                  remedy=("Check the building data first (households, type, use, floor area). Then consider connecting "
                          "it to a nearer cabinet or another outlet, a dedicated outlet, or an MV connection above "
                          f"MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW ({p.mv_kw:g} kW)."),
                  why=("Load-moment principle: a load's share of the drop is proportional to P × the impedance of the path "
                       "it shares with the weakest consumer (VDE-AR-N 4100 assessment of large connections). In "
                       "pylovo's snapshot, commercial and public loads run at 60–70 % of installed power, households "
                       "at 8–10 %."))
        if dominant or n50 < th["spread_n50"]:
            continue
        # VT-04: the drop is spread over many loads.
        path = m.path_lines(k)
        feeder_path = [lid for lid in path if m.lines[lid]["role"] == "feeder"]
        length_km = sum(m.lines[lid]["length_km"] for lid in feeder_path)
        if length_km <= 0:
            continue
        r_avg = sum(m.lines[lid]["R"] for lid in feeder_path) / length_km
        x_avg = sum(m.lines[lid]["X"] for lid in feeder_path) / length_km
        moment = sum(m.own[r["bus"]]["p"] * r["shared_km"] for r in rows)
        m_crit = 10 * p.feeder_limit * (p.vn / 1000) ** 2 / (r_avg + x_avg * p.tan) if (r_avg + x_avg * p.tan) else None
        feeder_loads = [b for b in m.feeders.get(f, {}).get("consumers", []) if b in m.own]
        p_sum = sum(m.own[b]["p"] for b in feeder_loads) or 1.0
        d_bar = sum(m.own[b]["p"] * m.dist.get(b, 0) for b in feeder_loads) / p_sum
        idx_of = {m.child_of[lid]: i for i, lid in enumerate(path, start=1)}
        best_cab, best_gap = None, None
        for bus, i in idx_of.items():
            if bus not in m.cabinets:
                continue
            behind = sum(m.own[r["bus"]]["p"] * r["shared_km"] for r in rows if r["attach_index"] >= i)
            gap = abs(behind - moment / 2)
            if best_gap is None or gap < best_gap:
                best_cab, best_gap = bus, gap
        mix: dict[str, float] = defaultdict(float)
        for lid in feeder_path:
            line = m.lines[lid]
            mix[line["std_type"] + (f" ×{line['parallel']}" if line["parallel"] > 1 else "")] += line["length_m"] or 0
        mix_text = ", ".join(f"{name} {length:.0f} m" for name, length in sorted(mix.items(), key=lambda kv: -kv[1]))
        top_share = rows[0]["c_lin"] / total if rows else 0
        largest = p.largest_feeder_cable()
        on_largest = bool(largest and all(m.lines[lid]["std_type"] == largest["name"] for lid in feeder_path))
        severity = t["severity"]
        tilde = "≈" if m.estimated else ""
        sec_shares = defaultdict(float)
        lv = m.lv_drop(k) or 1.0
        for lid in feeder_path:
            if m.lines[lid].get("feeder_section_id") is not None:
                sec_shares[m.lines[lid]["feeder_section_id"]] += m.line_drop(lid) / lv
        spread_note = (f" The drop is spread over {len(sec_shares)} sections (largest {max(sec_shares.values()):.0%})."
                       if sec_shares and max(sec_shares.values()) < 0.2 else "")
        message = (f"No single cause on feeder {f}: {n50} loads give half of the drop to {m.address(k)} (largest "
                   f"{top_share:.0%}). Load moment {tilde}{moment:.0f} kW·km over {length_km * 1000:.0f} m of cable ({mix_text}), "
                   f"against about {m_crit:.0f} kW·km allowed for {p.feeder_limit:g} %; centre of load at "
                   f"{d_bar:.0f} of {m.feeders.get(f, {}).get('reach_m', 0):.0f} m. The feeder is too long for its load."
                   + spread_note)
        F.add("VT-04", f"f{f}", severity, f"Feeder {f} is too long for its load ({n50} loads share the drop)", message,
              formula=(f"M = Σ P_j · l_j = {moment:.0f} kW·km; M_crit = 10 · {p.feeder_limit:g} · {p.vn / 1000:g}² / "
                       f"({r_avg:.3f} + {x_avg:.3f} · {p.tan:.3f}) = {m_crit:.0f} kW·km; N50 = {n50} ≥ {th['spread_n50']}, "
                       f"largest share {top_share:.0%} < {th['load_share']:.0%}"),
              metrics={"n50": n50, "moment_kwkm": _r(moment, 1), "moment_crit_kwkm": _r(m_crit, 1),
                       "ratio": _r(moment / m_crit, 3) if m_crit else None, "path_m": _r(length_km * 1000, 0),
                       "centre_of_load_m": _r(d_bar, 0), "largest_share": _r(top_share, 3), "feeder": f,
                       "cabinet": m.cabinets[best_cab]["name"] if best_cab in m.cabinets else None,
                       "largest_conductor": on_largest},
              targets={"lines": feeder_path, "feeder": f, "pin": k, "cabinet_buses": [best_cab] if best_cab else []},
              explains=t.get("spread_explains", t["explains"]), share=1.0, contribution_pp=m.lv_drop(k), impact=m.lv_drop(k),
              estimated=m.estimated,
              thresholds=[_threshold("design limit (%)", p.feeder_limit, p.source["feeder_drop_limit"]),
                          _heur("N50 at least", "spread_n50", th), _heur("no load above", "load_share", th)],
              remedy=((f"Split the feeder at cabinet {m.cabinets[best_cab]['name']} (about half the load moment lies "
                       "behind it) into a second outlet, " if best_cab in m.cabinets else "Split the feeder into a second outlet, ")
                      + "move the station towards the load centre (TP-03), or add a station."
                      + (" Bigger cables no longer help: the path already uses the largest conductor." if on_largest else "")),
              why=("Classical DSO load-moment check: per 8 % at cos φ 0.95 a NAYY 4×150 carries about 55 kW·km and a "
                   "NAYY 4×300 about 101 kW·km at 400 V. When many loads share the drop on the largest conductor, "
                   "only the topology helps."))
    return top_causes


def _fallback_flag(m: GridModel, bus: int) -> bool:
    for b in m.bus_buildings.get(bus, []):
        expected, fixed = _expected_households(m.p, b)
        if expected is not None and not fixed and (b.get("households") or 0) == expected and \
                (b.get("type") in ("AB", "MFH") or not b.get("type")):
            return True
    return False


def _expected_households(p: Params, b: dict) -> tuple[int | None, bool]:
    rfa = _f(b.get("residential_floor_area"), 0.0) or 0.0
    typ = b.get("type")
    fb = p.fallback.get(typ) if typ in p.fallback else p.fallback.get("untyped_residential")
    if not fb:
        return None, False
    if "fixed_households" in fb:
        return int(fb["fixed_households"]), True
    area = _f(fb.get("residential_area_per_household_m2"))
    minimum = int(fb.get("minimum_households") or 1)
    if not area:
        return minimum, False
    return max(minimum, int(round(rfa / area))), False


def _rule_tr02(m: GridModel, F: Findings, th: dict, targets: dict[int, dict]) -> None:
    if not targets:
        return
    p = m.p
    f, t = min(targets.items(), key=lambda kv: m.vm(kv[1]["bus"]) or 9.9)
    k = t["bus"]
    vm_k = m.vm(k)
    if vm_k is None:
        return
    total = 100 * (1 - vm_k)
    share = (m.dU_MV + m.dU_T) / total if total > 0 else 0.0
    severity_all = _worst(*(x["severity"] for x in targets.values()))
    triggered = m.dU_T >= th["trafo_drop_pp"] or share >= th["cause_share_warning"]
    severity = _cap("warning", severity_all) if triggered else "info"
    loading = m.trafo_loading if m.trafo_loading is not None else m.u_T_est
    notes = []
    kva = _f(m.g.get("kva"), 0.0) or 0.0
    if m.vk > 4 and kva <= 630:
        notes.append(f"A 4 % unit (EN 50588-1 reference up to 630 kVA) would take about {m.trafo_drop(loading, 4.0):.1f} pp.")
    tr = m.trafo
    tap_k = None
    vm_new = vm_max_new = None
    if tr.get("tap_pos") is not None and tr.get("tap_neutral") is not None and tr.get("tap_pos") == tr.get("tap_neutral"):
        step = _f(tr.get("tap_step_percent"), 2.5) or 2.5
        side = tr.get("tap_side") or "hv"
        room = abs(int(tr.get("tap_min") if side == "hv" else tr.get("tap_max") or 0) - int(tr.get("tap_neutral") or 0))
        lv_vms = [m.vm(b) for b in m.lv_buses() if m.vm(b) is not None]
        vm_min, vm_max = min(lv_vms), max(lv_vms)
        for kk in range(1, room + 1):
            if vm_min + kk * step / 100 >= p.min_vm + 0.005 and vm_max + kk * step / 100 <= p.max_vm:
                tap_k, vm_new, vm_max_new = kk, vm_min + kk * step / 100, vm_max + kk * step / 100
                break
        if tap_k and vm_min < p.min_vm + th["voltage_margin_pu"]:
            notes.append(f"{tap_k} tap step(s) (+{tap_k * step:g} %) would lift the weakest consumer to about "
                         f"{vm_new:.3f} p.u. (highest bus {vm_max_new:.3f} p.u.).")
        else:
            tap_k = None
    tilde = "≈" if m.estimated else ""
    message = (f"The station takes {tilde}{m.dU_T:.1f} pp ({share:.0%} including the MV setpoint) of the "
               f"{total:.1f} % drop to {m.address(k)}: vk {m.vk:g} %, loading {loading:.0%}, MV {m.vm_hv:.3f} p.u., "
               f"tap {tr.get('tap_pos')}." + ("" if not notes else " " + " ".join(notes)))
    F.add("TR-02", "grid", severity, f"Transformer takes {share:.0%} of the voltage band", message,
          formula=(f"share_T = (dU_MV + dU_T) / (100 · (1 − vm)) = ({m.dU_MV:.2f} + {m.dU_T:.2f}) / {total:.2f} = {share:.0%}; "
                   f"dU_T ≈ u · (vkr cos φ + vkx sin φ) + (u · (vkx cos φ − vkr sin φ))² / 200 with u = {loading:.3f}"
                   if m.estimated else
                   f"share_T = (dU_MV + dU_T) / (100 · (1 − vm)) = ({m.dU_MV:.2f} + {m.dU_T:.2f}) / {total:.2f} = {share:.0%}; "
                   f"dU_T = 100 · (vm_hv − vm_lv) from the power flow"),
          metrics={"dU_T": _r(m.dU_T), "dU_MV": _r(m.dU_MV), "share": _r(share, 3), "vk_percent": m.vk,
                   "vkr_percent": m.vkr, "loading": _r(loading, 3), "tap_pos": tr.get("tap_pos"), "tap_steps": tap_k,
                   "vm_after_tap": _r(vm_new, 4), "vm_max_after_tap": _r(vm_max_new, 4),
                   "dU_T_4pct": _r(m.trafo_drop(loading, 4.0)) if m.vk > 4 and kva <= 630 else None},
          targets={"trafo": True, "pin": k}, explains=[x for tt in targets.values() for x in tt["explains"]],
          share=share, contribution_pp=m.dU_MV + m.dU_T, impact=m.dU_MV + m.dU_T, estimated=m.estimated,
          thresholds=[_heur("transformer drop (pp)", "trafo_drop_pp", th), _heur("share", "cause_share_warning", th)],
          remedy=("Treat this as a modelling assumption first: run a what-if with real transformer data (vk, tap, "
                  "secondary voltage) as a separate scenario. Otherwise reduce the station loading (TR-01, TP-03) or "
                  "split the heavy feeder. Do not use the tap to hide a DE-01 design miss."),
          why=("DSOs split the ±10 % band between the MV setpoint (typically 1.02–1.05 p.u.), the off-load tap of the "
               "MV/LV transformer (±2 × 2.5 %) and the LV network. pylovo's stored net uses vm_pu 1.0, tap 0 and the "
               "pandapower standard types with vk 6 %, so the whole transformer drop comes out of the LV budget."
               + (" Before the power flow the drop is estimated from the coincident load plus the estimated line "
                  "losses at the reduced LV voltage; it reads about 0.1–0.2 pp low (magnetising current)."
                  if m.estimated else "")))


# =========================================================================== loading and design
def _rule_ld02(m: GridModel, F: Findings, th: dict) -> dict[int, str]:
    out: dict[int, str] = {}
    n_out = len(m.feeders)
    factors = {int(k): float(v) for k, v in th["grouping_factors"].items()}
    f_g = factors.get(min(max(n_out, 1), max(factors)), min(factors.values()))
    info_rows: dict[int, list[int]] = defaultdict(list)
    for sid, sec in m.sections.items():
        u = sec["u_d"]
        near = m.dist.get(sec["from_bus"], 99.0) <= th["station_zone_m"]
        grouped = near and n_out >= 2 and u / f_g > 1.0
        if u >= th["reserve_warning"]:
            severity = "warning"
        elif u >= th["reserve_info"] or grouped:
            severity = "info"
        else:
            continue
        out[sid] = severity
        if severity == "info":
            info_rows[sec["feeder"]].append(sid)
            continue
        down = m.down.get(m.child_of[sec["lines"][0]]) or {}
        hh = down.get("res", 0.0)
        nonres = sum(v[1] for c, v in (down.get("cats") or {}).items() if c != "Residential")
        par = f" ×{sec['parallel']}" if sec["parallel"] > 1 else ""
        message = (f"S{sid} ({sec['std_type']}{par}) is sized at {u:.0%} of its {sec['I_max']:.0f} A rating: design "
                   f"current {sec['I_d']:.0f} A for {hh:.0f} households"
                   + (f" and {nonres:.0f} commercial/public loads" if nonres else "")
                   + ". No reserve for growth or switching."
                   + (f" With {n_out} cables leaving the station together, derating ({f_g:.2f}) puts it at {u / f_g:.0%}."
                      if grouped else ""))
        F.add("LD-02", f"s{sid}", severity, f"S{sid} sized at {u:.0%} of its rating", message,
              formula=(f"u_d = I_d / I_max = {sec['I_d']:.0f} / {sec['I_max']:.0f} A = {u:.1%}, I_d = Σ_c P_c (g_c + (1 − g_c) "
                       f"N_c^−3/4) / (√3 Vn cos φ) over the loads behind the section"),
              metrics={"section": sid, "u_d": _r(u, 3), "I_d_a": _r(sec["I_d"], 1), "I_max_a": _r(sec["I_max"], 1),
                       "grouping_factor": f_g if grouped else None, "feeder": sec["feeder"]},
              targets={"sections": [sid], "feeder": sec["feeder"]}, impact=100 * (u - th["reserve_warning"]),
              basis="design",
              thresholds=[_heur("warning from", "reserve_warning", th), _heur("info from", "reserve_info", th)],
              remedy=("Use the next conductor size, or split the feeder. For new versions lower "
                      "FEEDER_SPLIT_MAX_CURRENT_KA. pylovo has no planning reserve factor for cables yet."),
              why=("pylovo takes the cheapest cable whose ampacity covers the design current at nominal voltage, so it "
                   "builds in no reserve. DSOs keep roughly 20–30 % for growth (heat pumps, EV), back-feeding through "
                   "cabinets and derating where cables share a trench (DIN VDE 0276-1000 reduction factors)."))
    for f, sids in info_rows.items():
        sids.sort(key=lambda s: -m.sections[s]["u_d"])
        top = sids[0]
        F.add("LD-02", f"f{f}:info", "info",
              f"{len(sids)} section(s) of feeder {f} at {th['reserve_info']:.0%}–{th['reserve_warning']:.0%} of their rating",
              (f"{', '.join(f'S{s} {m.sections[s]['u_d']:.0%}' for s in sids[:6])}"
               f"{' …' if len(sids) > 6 else ''}: sized close to the rating at pylovo's design current, little "
               "reserve for growth."),
              metrics={"sections": sids, "max_u_d": _r(m.sections[top]["u_d"], 3), "feeder": f},
              targets={"sections": sids, "feeder": f}, impact=100 * (m.sections[top]["u_d"] - th["reserve_warning"]),
              basis="design", thresholds=[_heur("info from", "reserve_info", th)],
              remedy="No action needed on its own; consider the next conductor size where growth is expected.",
              why="Sized without reserve by pylovo's ampacity pass (see LD-02 warnings).")
    return out


def _rule_ld03(m: GridModel, F: Findings, th: dict, context: dict, ld01: dict, ld02: dict, vt02: set[int]) -> None:
    peers = (context or {}).get("sections") or []
    peer_currents = [row[0] for row in peers]
    for sid, sec in m.sections.items():
        u = sec["u_pf"] if m.pf and sec["u_pf"] is not None else sec["u_d"]
        parent = m.sections.get(sec["parent"]) if sec["parent"] is not None else None
        reasons, metrics = [], {"section": sid, "feeder": sec["feeder"], "utilisation": _r(u, 3)}
        par = f" ×{sec['parallel']}" if sec["parallel"] > 1 else ""
        if parent:
            u_p = parent["u_pf"] if m.pf and parent["u_pf"] is not None else parent["u_d"]
            if u >= th["step_down_min"] and u - u_p >= th["step_down_delta"]:
                i_s = (sec.get("I_pf") if m.pf else sec["I_d"]) or 0.0
                i_p = (parent.get("I_pf") if m.pf else parent["I_d"]) or 0.0
                pass_share = i_s / i_p if i_p else None
                p_par = f" ×{parent['parallel']}" if parent["parallel"] > 1 else ""
                reasons.append(
                    f"S{sid} ({sec['std_type']}{par}) runs at {u:.0%} while S{parent['id']} feeding it "
                    f"({parent['std_type']}{p_par}) runs at {u_p:.0%}; at {m.node_label(sec['from_bus'])}, "
                    f"{_pct(pass_share)} of the current continues into the thinner cable"
                    + (" (the step goes from parallel cables to one)." if parent["parallel"] > sec["parallel"] else "."))
                metrics.update(parent=parent["id"], parent_utilisation=_r(u_p, 3), pass_share=_r(pass_share, 3))
        if peer_currents:
            lo = bisect_left(peer_currents, sec["I_d"] * (1 - th["peer_window"]))
            hi = bisect_right(peer_currents, sec["I_d"] * (1 + th["peer_window"]))
            window = peers[lo:hi]
            if len(window) >= th["peer_min"]:
                median_z = statistics.median(row[1] for row in window)
                if median_z and sec["z"] >= th["peer_ratio"] * median_z:
                    types: dict[str, int] = defaultdict(int)
                    for row in window:
                        types[row[2]] += 1
                    typical = max(types, key=types.get)
                    reasons.append(f"S{sid} has {sec['z'] / median_z:.1f}× the impedance of the {len(window)} sections "
                                   f"of this version that carry a similar current (typically {typical}).")
                    metrics.update(peer_ratio=_r(sec["z"] / median_z, 2), peers=len(window), peer_type=typical)
        thicker = [c for c in m.section_children.get(sid, []) if m.sections[c]["I_max"] > sec["I_max"] + 1e-6]
        if thicker:
            reasons.append(f"S{sid} is thinner than the section behind it (S{thicker[0]}, "
                           f"{m.sections[thicker[0]]['std_type']}).")
            metrics.update(thicker_child=thicker[0])
        if not reasons:
            continue
        strong = sid in ld01 and not ld01[sid]["estimated"] or ld02.get(sid) == "warning" or sid in vt02
        severity = "warning" if strong else "info"
        F.add("LD-03", f"s{sid}", severity, f"S{sid} is a bottleneck", " ".join(reasons), metrics=metrics,
              targets={"sections": [sid] + ([parent["id"]] if parent else []), "feeder": sec["feeder"],
                       "cabinet_buses": [sec["from_bus"]]},
              explains=[ld01[sid]["id"]] if sid in ld01 else [], impact=100 * u, basis="power_flow" if m.pf else "design",
              thresholds=[_heur("utilisation at least", "step_down_min", th), _heur("step", "step_down_delta", th),
                          _heur("peer impedance ratio", "peer_ratio", th)],
              remedy=("Continue the upstream cross-section or parallel count to the next cabinet, move the split point, "
                      "or feed part of the downstream branch from another outlet."),
              why=("DSO feeders use one standard cross-section or taper outwards. A section clearly more loaded than the "
                   "one feeding it, or thinner than peers carrying the same current, is the classic weak point."))


def _rule_ld04(m: GridModel, F: Findings, th: dict, ld01: dict, vt: dict[int, dict]) -> dict[int, dict]:
    p = m.p
    out: dict[int, dict] = {}
    kva = _f(m.g.get("kva"), 0.0) or 0.0
    i_rt = kva * 1000 / (SQRT3 * p.vn) if kva else None
    largest = p.largest_feeder_cable()
    for f, info in m.feeders.items():
        if info["head"] is None or info["head_role"] != "feeder":
            continue
        head = m.lines[info["head"]]
        i_head = info["I_head_pf"] if m.pf and info["I_head_pf"] is not None else info["I_head_d"]
        basis = "power flow" if m.pf and info["I_head_pf"] is not None else "design"
        parallel_secs = [s for s, sec in m.sections.items() if sec["feeder"] == f and sec["parallel"] >= 2]
        long_parallel = [s for s in parallel_secs if m.sections[s]["length_m"] > th["station_zone_m"]]
        severity = None
        if i_head > th["outlet_warning_a"] or long_parallel:
            severity = "warning"
        elif i_head > th["outlet_info_a"] or parallel_secs:
            severity = "info"
        if severity is None:
            continue
        len_par = sum(m.sections[s]["length_m"] for s in parallel_secs)
        n_suggested = max(2, math.ceil(i_head / th["outlet_warning_a"])) if severity == "warning" else 1
        i_max1 = (_f(head.get("max_i_ka"), 0.0) or 0.0) * 1000
        par = f"{head['parallel']} × " if head["parallel"] > 1 else ""
        message = (f"Feeder {f} starts with {i_head:.0f} A ({basis}) on {par}{head['std_type']}"
                   + (f"; {len(parallel_secs)} section(s) over {len_par:.0f} m use parallel cables" if parallel_secs else "")
                   + (f". That is more than one outlet fuse ({th['outlet_warning_a']:.0f} A) and one cable "
                      f"({i_max1:.0f} A) can carry" if i_head > th["outlet_warning_a"] else "")
                   + (f", and {i_head / i_rt:.0%} of the transformer's rated current" if i_rt else "")
                   + (f". A DSO would build {n_suggested} outlets" if n_suggested > 1 else "")
                   + f" (FEEDER_SPLIT_MAX_CURRENT_KA = {p.split_ka:g} kA"
                   + (f", largest single cable {largest['max_i_a']:.0f} A" if largest else "") + ").")
        fid = F.add("LD-04", f"f{f}", severity, f"Feeder {f} too heavy for one outlet ({i_head:.0f} A)", message,
                    formula=f"I_head = {i_head:.0f} A ({basis}) against {th['outlet_warning_a']:.0f} A; parallel sections > {th['station_zone_m']:g} m: {len(long_parallel)}",
                    metrics={"feeder": f, "I_head_a": _r(i_head, 1), "basis": basis, "parallel_sections": parallel_secs,
                             "parallel_length_m": _r(len_par, 0), "outlets_suggested": n_suggested,
                             "share_of_rated_current": _r(i_head / i_rt, 3) if i_rt else None,
                             "split_max_ka": p.split_ka},
                    targets={"lines": [info["head"]], "sections": parallel_secs, "feeder": f},
                    # A second outlet halves the current on the shared path, so it also explains the voltage symptom.
                    explains=[ld01[s]["id"] for s in parallel_secs if s in ld01]
                    + ([vt[f]["id"]] if f in vt and severity == "warning" else []), impact=i_head - th["outlet_warning_a"],
                    basis="power_flow" if basis == "power flow" else "design",
                    thresholds=[_heur("outlet fuse (A)", "outlet_warning_a", th), _heur("info from (A)", "outlet_info_a", th),
                                _threshold("FEEDER_SPLIT_MAX_CURRENT_KA", p.split_ka, p.source["split_max_ka"])],
                    remedy=("Split into two or more outlets at the station by re-assigning branches at the first cabinet. "
                            "For new versions lower FEEDER_SPLIT_MAX_CURRENT_KA to about 0.30–0.40 kA, or add a station."),
                    why=("Each LV outlet is one cable behind one NH fuse on the station's fuse strip; NH2 goes up to "
                         "400 A. Parallel street cables on one outlet are not normal practice — a planner opens a second "
                         f"outlet. pylovo grows a branch up to FEEDER_SPLIT_MAX_CURRENT_KA ({p.split_ka:g} kA), twice the "
                         "largest single cable, and then needs parallel cables."))["id"]
        out[f] = {"id": fid, "severity": severity}
    return out


def _rule_ld05(m: GridModel, F: Findings, th: dict) -> None:
    p = m.p
    for lid, line in m.lines.items():
        if line["role"] != "service":
            continue
        j = m.child_of.get(lid)
        if j is None or j not in m.own:
            continue
        design_kw = m.own[j]["design"]
        i_svc = p.design_current_a(design_kw)
        conn = m.parent.get(j)
        up = m.lines.get(m.pline.get(conn)) if conn is not None else None
        sid = up.get("feeder_section_id") if up else None
        sec_imax = m.sections[sid]["I_max"] if sid in m.sections else (up["I_max"] if up else None)
        feeder_sized = bool(sec_imax and line["I_max"] >= sec_imax)
        severity = None
        if design_kw >= p.mv_kw or line["parallel"] >= 2 or feeder_sized:
            severity = "warning"
        elif i_svc > th["service_info_a"]:
            severity = "info"
        if severity is None:
            continue
        par = f" ×{line['parallel']}" if line["parallel"] > 1 else ""
        ratio = line["I_max"] / sec_imax if sec_imax else None
        message = (f"{m.address(j)} ({m.building_type(j) or 'building'}, {m.households(j)} households) needs "
                   f"{i_svc:.0f} A ({design_kw:.0f} kW local design load) on {line['std_type']}{par} over "
                   f"{line['length_m']:.0f} m"
                   + (f", which is {ratio:.0%} of the feeder section it connects to (S{sid}, "
                      f"{m.sections[sid]['std_type']})" if ratio and sid in m.sections else "") + ".")
        F.add("LD-05", f"b{j}", severity, f"Large service connection: {m.address(j)} ({i_svc:.0f} A)", message,
              metrics={"bus": j, "I_a": _r(i_svc, 1), "design_kw": _r(design_kw, 1), "line": lid,
                       "ratio_to_section": _r(ratio, 3), "section": sid},
              targets={"lines": [lid], "buses": [j], "pin": j, "feeder": m.bus_feeder.get(j)},
              impact=i_svc - th["service_info_a"], basis="design",
              thresholds=[_heur("direct metering (A)", "service_info_a", th),
                          _threshold("MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW", p.mv_kw, p.source["mv_threshold_kw"])],
              remedy="Check households and floor area of the building, connect it through a dedicated station outlet, or flag it as MV-direct.",
              why=("Under VDE-AR-N 4100 and the DSO's TAB a house connection box is typically fused at 35–63 A; direct "
                   "metering ends at about 63 A. Larger connections get CT metering, a dedicated outlet or an MV "
                   "connection (VDE-AR-N 4110). pylovo's MV-direct threshold diverts only commercial and public loads."))


def _rule_ld06(m: GridModel, F: Findings, th: dict, context: dict, vt: dict, ld01: dict) -> dict[int, dict]:
    p = m.p
    out: dict[int, dict] = {}
    total_res = sum(f["res_units"] for f in m.feeders.values()) or 1.0
    median = (context or {}).get("density_median_kw_per_km")
    for f, info in m.feeders.items():
        if info["head"] is None or info["head_role"] != "feeder":
            continue
        head = m.lines[info["head"]]
        cable = p.cable.get(head["std_type"]) or {}
        i_max1 = cable.get("max_i_a") or (_f(head.get("max_i_ka"), 0.0) or 0.0) * 1000
        n_th = p.household_capacity(i_max1) if i_max1 else None
        n_res = info["res_units"]
        explains = [ld01[s]["id"] for s, x in ld01.items() if x.get("section") is not None and m.sections.get(s, {}).get("feeder") == f]
        if n_th and n_res >= th["households_info_fraction"] * n_th:
            severity = "warning" if n_res > n_th else "info"
            fid = F.add("LD-06", f"f{f}:hh", severity, f"Feeder {f} supplies {n_res:.0f} households", formula=(
                f"N_th = max N with {p.peak_hh:g} kW · N · ({p.sim.get('Residential', 0.07):g} + "
                f"{1 - p.sim.get('Residential', 0.07):g} · N^−3/4) ≤ √3 · {p.vn:g} V · {i_max1:.0f} A · {p.cos:g} → {n_th}"), message=(
                f"Feeder {f} supplies {n_res:.0f} households ({n_res / total_res:.0%} of the grid); under pylovo's "
                f"coincidence model one {head['std_type']} carries about {n_th}."),
                metrics={"feeder": f, "households": _r(n_res, 0), "capacity": n_th, "head_type": head["std_type"]},
                targets={"lines": [info["head"]], "feeder": f}, explains=explains + ([vt[f]["id"]] if f in vt else []),
                impact=n_res - n_th, basis="design",
                thresholds=[_threshold("households per cable", n_th, "pylovo coincidence model with the version's "
                                       "PEAK_LOAD_HOUSEHOLD and SIM_FACTOR"),
                            _heur("info from", "households_info_fraction", th)],
                remedy="Split the feeder (see LD-04 and TP-02), use shorter or more feeders in dense areas, and check the large loads (DA-02).",
                why=("The cable rating turned into a household count with pylovo's own coincidence model: about 108 "
                     "households for NAYY 4×150, 130 for 185, 152 for 240 and 188 for 300 at the default parameters. "
                     "DSO planners usually put 40–80 dwellings on a 150 mm² feeder to keep reserve."))["id"]
            out[f] = {"id": fid, "severity": severity}
        if median and info["feeder_m"] >= th["density_min_length_m"]:
            rho = info["design_kw"] / (info["feeder_m"] / 1000)
            if rho >= th["density_factor"] * median:
                strong = head["u_d"] >= th["density_head_utilisation"] or f in vt
                n_nonres = sum(1 for b in info["consumers"] if any(c != "Residential" for c in m.own.get(b, {}).get("cats", {})))
                nonres_kw = sum(v[0] for b in info["consumers"] for c, v in m.own.get(b, {}).get("cats", {}).items()
                                if c != "Residential")
                fid = F.add("LD-06", f"f{f}:rho", "warning" if strong else "info",
                            f"Dense demand on feeder {f} ({rho:.0f} kW/km)",
                            (f"Feeder {f}: {rho:.0f} kW/km coincident design demand over {info['feeder_m'] / 1000:.2f} km "
                             f"(version median {median:.0f}); {n_nonres} commercial/public loads ({nonres_kw:.0f} kW installed)."),
                            metrics={"feeder": f, "density_kw_per_km": _r(rho, 1), "median": _r(median, 1)},
                            targets={"lines": [lid for lid in info["lines"] if m.lines[lid]["role"] == "feeder"], "feeder": f},
                            explains=explains, impact=rho / median, basis="design",
                            thresholds=[_heur("density factor", "density_factor", th),
                                        _heur("minimum length (m)", "density_min_length_m", th)],
                            remedy="Use shorter or more feeders in dense areas, and check the large loads (DA-02).",
                            why="Relative outlier test: for a uniform density q the drop grows with q × L² / 2.")["id"]
                out.setdefault(f, {"id": fid, "severity": "warning" if strong else "info"})
    return out


def _rule_de02(m: GridModel, F: Findings, th: dict, de01: dict[int, dict]) -> None:
    p = m.p
    amp = _f(m.g.get("ampacity_feeder_drop_pct"))
    rows: dict[int, list[int]] = defaultdict(list)
    for sid, sec in m.sections.items():
        if sec["basis"] == "end_to_end_voltage":
            rows[sec["feeder"]].append(sid)
    if not rows:
        return
    worst_feeder = max(rows, key=lambda f: sum(m.sections[s]["length_m"] for s in rows[f]))
    for f, sids in rows.items():
        info = m.feeders.get(f) or {}
        l_up = sum(m.sections[s]["length_m"] for s in sids)
        share = l_up / info["feeder_m"] if info.get("feeder_m") else 0.0
        cost = 0.0
        for s in sids:
            sec = m.sections[s]
            sel, base = p.cable.get(sec["std_type"]), p.cable.get(sec.get("ampacity_std_type"))
            if sel and base:
                cost += (sel["cost"] * sec["parallel"] - base["cost"] * (sec.get("ampacity_parallel") or sec["parallel"])) * sec["length_m"]
        big_miss = amp is not None and amp >= th["upsizing_ampacity_factor"] * p.feeder_limit and f == worst_feeder
        severity = "warning" if share >= th["upsizing_share"] or big_miss else "info"
        example = max(sids, key=lambda s: m.sections[s]["length_m"])
        ex = m.sections[example]
        message = (f"{share:.0%} of feeder {f} ({l_up:.0f} m, {len(sids)} sections) was upsized for voltage (e.g. "
                   f"S{example}: {ex.get('ampacity_std_type')} ×{ex.get('ampacity_parallel')} → {ex['std_type']} "
                   f"×{ex['parallel']}; +{cost:,.0f} EUR material)."
                   + (f" The ampacity-only design drop was {amp:.1f} % against the {p.feeder_limit:g} % limit." if amp else ""))
        F.add("DE-02", f"f{f}", severity, f"{len(sids)} section(s) of feeder {f} upsized for voltage", message,
              metrics={"feeder": f, "upsized_m": _r(l_up, 0), "share": _r(share, 3), "sections": sids,
                       "extra_cost_eur": _r(cost, 0), "ampacity_drop_pct": amp},
              targets={"sections": sids, "feeder": f}, impact=share, basis="design",
              explains=[de01[f]["id"]] if f in de01 else [],
              thresholds=[_heur("share", "upsizing_share", th), _heur("ampacity miss factor", "upsizing_ampacity_factor", th)],
              remedy="None needed on its own. If the upsizing is extensive, compare it with an extra outlet or station (DE-01, TP-01).",
              why=("Shows where voltage, not current, set the conductor. When most of a feeder had to be upsized, the "
                   "feeder is too long or too heavily loaded for radial supply, and a topology change is usually cheaper."))


def _rule_de04(m: GridModel, F: Findings, th: dict, context: dict, voltage_buses: set[int]) -> None:
    p = m.p
    median = (context or {}).get("service_length_median_m")
    for lid, line in m.lines.items():
        if line["role"] != "service":
            continue
        j = m.child_of.get(lid)
        reasons, severity = [], None
        if line.get("service_voltage_drop_limit_met") is False:
            reasons.append(f"design drop above the {p.service_limit:g} % service limit")
            severity = "warning"
        if line.get("service_length_review"):
            reasons.append("longer than pylovo's 100 m review threshold")
            severity = _worst(severity, "warning" if j in voltage_buses else "info")
        if line.get("service_sizing_basis") == "service_voltage_drop":
            reasons.append(f"upsized from {line.get('ampacity_std_type')} for voltage")
            severity = _worst(severity, "info")
        if median and (line["length_m"] or 0) > th["service_length_factor"] * median and \
                (line["length_m"] or 0) > th["service_length_min_m"]:
            reasons.append(f"{line['length_m'] / median:.1f}× the version's median service length ({median:.0f} m)")
            severity = _worst(severity, "info")
        if m.pf and j is not None:
            a, b = m.vm_pf.get(m.parent.get(j)), m.vm_pf.get(j)
            if a is not None and b is not None and 100 * (a - b) > th["service_drop_pf_pp"]:
                reasons.append(f"{100 * (a - b):.2f} pp drop in the power flow")
                severity = _worst(severity, "info")
        if severity is None:
            continue
        par = f" ×{line['parallel']}" if line["parallel"] > 1 else ""
        design_kw = m.own.get(j, {}).get("design", 0.0)
        message = (f"Service to {m.address(j)}: {line['length_m']:.0f} m {line['std_type']}{par}, design drop "
                   f"{_n(_f(line.get('service_drop_pct')), 2)} % at {design_kw:.0f} kW (limit {p.service_limit:g} %): "
                   + "; ".join(reasons) + ".")
        F.add("DE-04", f"l{lid}", severity, f"Service cable to {m.address(j)}", message,
              metrics={"line": lid, "length_m": line["length_m"], "service_drop_pct": _f(line.get("service_drop_pct")),
                       "reasons": reasons},
              targets={"lines": [lid], "buses": [j] if j is not None else [], "pin": j, "feeder": m.line_feeder.get(lid)},
              impact=line["length_m"] or 0, basis="design",
              thresholds=[_threshold("service design limit (%)", p.service_limit, p.source["service_drop_limit"]),
                          _threshold("review length (m)", 100, "pylovo constant SERVICE_LENGTH_REVIEW_THRESHOLD_M"),
                          _heur("length factor", "service_length_factor", th)],
              remedy=("Check the building's connection point and assigned street (assigned_way_id, connection_point), add "
                      "a connection point on the nearer street, or use a larger service conductor."),
              why=("House connections are typically 10–30 m; long ones point to a wrong building-to-street assignment "
                   "or a missing street segment. VDE-AR-N 4100 allows 0.5 % from the house connection box to the meter."))


def _rule_de05(m: GridModel, F: Findings, th: dict) -> dict[int, dict]:
    p = m.p
    out: dict[int, dict] = {}
    s_r = m.sr_kva * 1000
    if not s_r:
        return out
    z_t = m.vk / 100 * p.vn ** 2 / s_r
    r_t = m.vkr / 100 * p.vn ** 2 / s_r
    x_t = math.sqrt(max(0.0, z_t ** 2 - r_t ** 2))
    rp: dict[int, tuple[float, float]] = {m.root: (0.0, 0.0)}
    for bus in m.order[1:]:
        line = m.lines[m.pline[bus]]
        pr = rp[m.parent[bus]]
        rp[bus] = (pr[0] + line["R"], pr[1] + line["X"])
    ratings = sorted(int(r) for r in th["fuse_ratings_a"])
    trip = {int(k): float(v) for k, v in th["fuse_trip_5s_a"].items()}
    u0 = p.vn / SQRT3
    for f, info in m.feeders.items():
        if not info["real"]:
            continue
        head = m.lines[info["head"]]
        i_d = info["I_head_d"]
        fuse = next((r for r in ratings if r >= i_d), None)
        no_single = fuse is None
        fuse = fuse or ratings[-1]
        worst = None
        for bus in m.lv_buses(f):
            if m.buses[bus]["role"] != "connection" or bus in m.station:
                continue
            r, x = rp.get(bus, (0.0, 0.0))
            ik = th["fault_c_min"] * u0 / abs(complex(r_t + 2 * th["fault_conductor_factor"] * r, x_t + 2 * x))
            if worst is None or ik < worst[0]:
                worst = (ik, bus)
        if worst is None:
            continue
        ia5 = trip.get(fuse)
        i_z = head["I_max"]
        protection = fuse > i_z + 1e-6
        if ia5 is None or worst[0] >= ia5:
            continue
        ik, bus = worst
        message = (f"Feeder {f}: at {m.node_label(bus)} ({m.dist.get(bus, 0):.0f} m) the minimum single-phase fault "
                   f"current is about {ik:.0f} A, below the {ia5:.0f} A that a {fuse} A outlet fuse needs to trip within "
                   f"5 s. " + (f"No single NH outlet carries the {i_d:.0f} A design current (assessed with {fuse} A)."
                               if no_single else f"This is already the smallest fuse that carries the {i_d:.0f} A design current.")
                   + (f" The fuse is also larger than the head cable's {i_z:.0f} A rating, so overload protection is "
                      "not given." if protection else ""))
        fid = F.add("DE-05", f"f{f}", "warning", f"Fault loop too weak at the end of feeder {f}", message,
                    formula=(f"I_k1,min = c_min · U0 / |Z_T + 2 ({th['fault_conductor_factor']:g} R + j X)| = "
                             f"{th['fault_c_min']:g} · {u0:.0f} V / "
                             f"|({r_t * 1000:.1f} + 2 · {th['fault_conductor_factor']:g} · {rp.get(bus, (0, 0))[0] * 1000:.1f}) + j ({x_t * 1000:.1f} + 2 · "
                             f"{rp.get(bus, (0, 0))[1] * 1000:.1f})| mΩ = {ik:.0f} A < {ia5:.0f} A"),
                    metrics={"feeder": f, "ik1_min_a": _r(ik, 0), "fuse_a": fuse, "trip_5s_a": ia5, "I_d_a": _r(i_d, 1),
                             "no_single_outlet": no_single, "overload_protection_missing": protection,
                             "distance_m": _r(m.dist.get(bus), 0)},
                    targets={"buses": [bus], "feeder": f, "pin": bus, "lines": m.path_lines(bus)},
                    impact=(ia5 - ik) / ia5, basis="design",
                    thresholds=[_heur("c_min", "fault_c_min", th), _heur("conductor factor (80 °C)", "fault_conductor_factor", th),
                                _heur("5 s tripping currents", "fuse_trip_5s_a", th)],
                    remedy=("Shorten the feeder (an extra outlet or station), use a larger cross-section, or protect the "
                            "far branch with a smaller fuse in a cabinet (check selectivity)."),
                    why=("DSO LV planning rules (DIN VDE 0100-410, TN systems, 5 s for distribution circuits; DIN VDE 0102 "
                         "minimum short-circuit currents) require the outlet fuse to clear a single-phase fault at the far "
                         "end. pylovo models no protection, so this is an indicative estimate: it neglects the MV source "
                         "and arc resistance, takes the transformer's Z0 = Z1 (Dyn) and uses approximate fuse values."))["id"]
        out[f] = {"id": fid}
    return out


# =========================================================================== topology
def _rule_tp01(m: GridModel, F: Findings, th: dict, context: dict, linked: dict[int, list[str]]) -> dict[int, str]:
    p = m.p
    out: dict[int, str] = {}
    p90 = (context or {}).get("reach_p90_m")
    losses = (_f(m.pf.get("losses_kw")) / _f(m.pf.get("total_load_kw"), 1.0)) if m.pf and _f(m.pf.get("total_load_kw")) else None
    for f, info in m.feeders.items():
        reach = info["reach_m"]
        if reach <= th["long_feeder_m"] or not info["consumers"]:
            continue
        severity = "warning" if linked.get(f) else "info"
        far = max(info["consumers"], key=lambda b: m.dist.get(b, 0))
        r_path = sum(m.lines[lid]["R"] for lid in m.path_lines(far)) * 1000
        drop = m.lv_drop(far)
        head = m.lines[info["head"]] if info["head"] is not None else None
        z_head = p.z(head["r_ohm_per_km"], head["x_ohm_per_km"]) / head["parallel"] if head else None
        l_crit = (2 * 10 * p.feeder_limit * (p.vn / 1000) ** 2 / (info["p_kw"] * z_head) * 1000
                  if head and info["p_kw"] > 0 and z_head else None)
        tilde = "≈" if m.estimated else ""
        message = (f"Feeder {f} reaches {reach:.0f} m"
                   + (f" (version P90 {p90:.0f} m" if p90 else " (")
                   + (f"; about {l_crit:.0f} m would be the limit for its {info['p_kw']:.0f} kW spread evenly on "
                      f"{head['std_type']}{' ×' + str(head['parallel']) if head['parallel'] > 1 else ''})" if l_crit else ")")
                   + f". Path resistance {r_path:.0f} mΩ; the farthest consumer, {m.address(far)}, is {tilde}{drop:.1f} % "
                   f"below the busbar." + (f" Peak losses {losses * 100:.1f} %." if losses is not None else ""))
        F.add("TP-01", f"f{f}", severity, f"Long feeder {f} ({reach:.0f} m)", message,
              formula=(f"reach {reach:.0f} m > {th['long_feeder_m']:.0f} m"
                       + (f"; L_crit = 2 · 10 · {p.feeder_limit:g} · {p.vn / 1000:g}² / ({info['p_kw']:.0f} kW · {z_head:.3f} Ω/km) "
                          f"= {l_crit:.0f} m" if l_crit else "")),
              metrics={"feeder": f, "reach_m": _r(reach, 0), "p90_m": _r(p90, 0), "critical_length_m": _r(l_crit, 0),
                       "path_resistance_mohm": _r(r_path, 0), "losses_share": _r(losses, 4)},
              targets={"lines": m.path_lines(far), "feeder": f, "pin": far}, explains=linked.get(f, []),
              impact=reach - th["long_feeder_m"], basis="design",
              thresholds=[_heur("long from (m)", "long_feeder_m", th)],
              remedy="Shorten the feeder: a second outlet from a cabinet, an extra or moved station, or an open ring to a neighbouring feeder.",
              why=("German LV street feeders are typically a few hundred metres (about 300–500 m suburban, up to about "
                   "1 km rural with large cross-sections). Length is limited by voltage drop and by the fault-loop "
                   "condition (DE-05)."))
        out[f] = severity
    return out


def _rule_tp02(m: GridModel, F: Findings, th: dict, heavy_symptoms: dict[int, list[str]]) -> None:
    p = m.p
    real = {f: info for f, info in m.feeders.items() if info["real"]}
    if len(real) < 2:
        return
    total = sum(info["p_kw"] for info in real.values()) or 1.0
    f_max, heavy = max(real.items(), key=lambda kv: kv[1]["p_kw"])
    share = heavy["p_kw"] / total
    if share < max(th["imbalance_share"], th["imbalance_even_factor"] / len(real)):
        return
    light = []
    for f, info in real.items():
        if f == f_max:
            continue
        u_head = m.lines[info["head"]]["u_d"]
        vm_min = min((m.vm(b) for b in info["consumers"] if m.vm(b) is not None), default=None)
        if u_head < th["light_feeder_utilisation"] and vm_min is not None and vm_min >= p.min_vm + th["light_feeder_margin_pu"]:
            light.append((f, u_head))
    severity = "warning" if heavy_symptoms.get(f_max) and light else "info"
    candidate = None
    if light:
        light_conn = [(b, m.buses[b]) for f, _ in light for b in m.lv_buses(f) if m.buses[b]["role"] == "connection"]
        for cab, info in sorted(m.cabinets.items(), key=lambda kv: kv[1]["distance_m"]):
            if m.bus_feeder.get(cab) != f_max:
                continue
            sub = [b for b in m.subtree(cab) if m.buses[b]["role"] == "connection"]
            best = None
            for b in sub:
                pos = (m.buses[b].get("lon"), m.buses[b].get("lat"))
                for lb, lbus in light_conn:
                    d = _dist_m(pos, (lbus.get("lon"), lbus.get("lat")))
                    if d is not None and d <= th["transfer_gap_m"] and (best is None or d < best[0]):
                        best = (d, lb)
            if best:
                moved = m.down[cab]["p"] / heavy["p_kw"] if heavy["p_kw"] else 0
                if 0.15 <= moved <= 0.7:
                    candidate = (cab, best[0], m.bus_feeder.get(best[1]), moved)
                    break
    light_text = ", ".join(str(f) for f, _ in light) or "–"
    u_light = max((u for _, u in light), default=None)
    message = (f"Feeder {f_max} carries {share:.0%} of the station load ({heavy['res_units']:.0f} of "
               f"{sum(i['res_units'] for i in m.feeders.values()):.0f} households)"
               + (f", while feeder(s) {light_text} run below {u_light:.0%} at their heads" if light else "") + "."
               + (f" The branch behind cabinet {m.cabinets[candidate[0]]['name']} ({candidate[3]:.0%} of the load) ends "
                  f"{candidate[1]:.0f} m from feeder {candidate[2]}." if candidate else ""))
    F.add("TP-02", "grid", severity, f"Feeder {f_max} carries {share:.0%} of the station load", message,
          metrics={"feeder": f_max, "share": _r(share, 3), "real_feeders": len(real), "light_feeders": [f for f, _ in light],
                   "transfer_cabinet": m.cabinets[candidate[0]]["name"] if candidate else None,
                   "transfer_share": _r(candidate[3], 3) if candidate else None},
          targets={"feeder": f_max, "cabinet_buses": [candidate[0]] if candidate else [],
                   "lines": [m.feeders[f_max]["head"]] + [m.feeders[f]["head"] for f, _ in light]},
          explains=heavy_symptoms.get(f_max, []), impact=share, basis="design",
          thresholds=[_heur("share", "imbalance_share", th), _heur("even-share factor", "imbalance_even_factor", th),
                      _heur("transfer gap (m)", "transfer_gap_m", th)],
          remedy=("Move the branch behind the named cabinet to the lightly loaded outlet, or add an outlet. For a new "
                  "version regenerate with a smaller FEEDER_SPLIT_MAX_CURRENT_KA or a larger MIN_SHARED_PREFIX_LENGTH_M."),
          why=("DSO station layouts balance the outlets so each carries comparable load and can take over part of a "
               "neighbour through cabinets. pylovo grows a branch from the farthest node up to "
               "FEEDER_SPLIT_MAX_CURRENT_KA and reuses shared prefixes, which can put most of a station on one outlet."))


def _rule_tp03(m: GridModel, F: Findings, th: dict, strong: bool, explains: list[str]) -> None:
    cons = [b for b in m.consumers if b in m.own and m.buses[b].get("lon") is not None]
    if len(cons) < th["eccentricity_min_consumers"]:
        return
    station = (m.g.get("lon"), m.g.get("lat"))
    if station[0] is None:
        lv = m.buses.get(m.root) or {}
        station = (lv.get("lon"), lv.get("lat"))
    if station[0] is None:
        return
    lon0, lat0 = station
    pts = [(_local_m(m.buses[b]["lon"], m.buses[b]["lat"], lon0, lat0), m.own[b]["p"]) for b in cons]
    w = sum(pw for _, pw in pts)
    if w <= 0:
        return
    xc = (sum(x * pw for (x, _), pw in pts) / w, sum(y * pw for (_, y), pw in pts) / w)
    r_g = math.sqrt(sum(pw * ((x - xc[0]) ** 2 + (y - xc[1]) ** 2) for (x, y), pw in pts) / w)
    if r_g < th["eccentricity_min_radius_m"]:
        return
    off = math.hypot(*xc)
    e = off / r_g
    if e <= th["eccentricity"]:
        return
    mean_station = sum(pw * math.hypot(x, y) for (x, y), pw in pts) / w
    mean_centre = sum(pw * math.hypot(x - xc[0], y - xc[1]) for (x, y), pw in pts) / w
    gain = 1 - mean_centre / mean_station if mean_station else None
    source = m.g.get("station_source") or "greenfield"
    severity = "warning" if strong else "info"
    tol = m.p.position_tolerance
    message = (f"The station ({source} position) is {off:.0f} m from the load centre, {e:.1f}× the load radius of "
               f"{r_g:.0f} m. A central station ({_compass(*xc)} of the current one) would shorten the load-weighted "
               f"distance by about {gain:.0%}.")
    centre = (lon0 + xc[0] / (111_320.0 * math.cos(math.radians(lat0))), lat0 + xc[1] / 110_540.0)
    F.add("TP-03", "grid", severity, f"Station {off:.0f} m off the load centre", message,
          formula=f"e = offset / load radius = {off:.0f} m / {r_g:.0f} m = {e:.2f} (> {th['eccentricity']:g})",
          metrics={"offset_m": _r(off, 0), "load_radius_m": _r(r_g, 0), "eccentricity": _r(e, 2), "gain": _r(gain, 3),
                   "direction": _compass(*xc), "station_source": source,
                   "centre": [round(centre[0], 6), round(centre[1], 6)]},
          targets={"trafo": True}, explains=explains, impact=e, basis="design",
          thresholds=[_heur("offset / load radius", "eccentricity", th)]
          + ([_threshold("GREENFIELD_TRAFO_POSITION_TOLERANCE", tol, "version parameter")] if tol is not None else []),
          remedy=("Place a manual station position in the Data step and regenerate as a new version, or lower "
                  "GREENFIELD_TRAFO_POSITION_TOLERANCE for a new version. For brownfield, check the assignment and the "
                  "OSM position."),
          why=("DSOs place MV/LV stations near the load centre (Lastschwerpunkt) to minimise load moments. pylovo picks "
               "greenfield positions at random among those costing up to (1 + GREENFIELD_TRAFO_POSITION_TOLERANCE) × "
               "the optimum."))


def _rule_tp04(m: GridModel, F: Findings, th: dict, targets: dict[int, dict]) -> None:
    station = (m.g.get("lon"), m.g.get("lat"))
    if station[0] is None:
        return
    on_path: dict[int, list[str]] = {}
    for f, t in targets.items():
        for lid in m.path_lines(t["bus"]):
            on_path.setdefault(m.child_of[lid], []).extend(t["explains"])
    groups: dict[int, list[tuple]] = defaultdict(list)
    for b in m.lv_buses():
        bus = m.buses[b]
        if bus["role"] not in ("consumer",) and b not in on_path:
            continue
        air = _dist_m((bus.get("lon"), bus.get("lat")), station)
        route = m.dist.get(b)
        if air is None or route is None or air < th["detour_min_air_m"]:
            continue
        ratio = route / air
        extra = route - air
        if b in on_path and ratio >= th["detour_ratio_path"]:
            groups[m.bus_feeder.get(b)].append((ratio, extra, b, route, air, "warning", on_path[b]))
        elif bus["role"] == "consumer" and ratio > th["detour_ratio"] and extra > th["detour_extra_m"]:
            groups[m.bus_feeder.get(b)].append((ratio, extra, b, route, air, "info", []))
    for f, rows in groups.items():
        rows.sort(key=lambda r: (SEV_RANK[r[5]], -r[0]))
        ratio, extra, b, route, air, severity, explains = rows[0]
        cons = [r for r in rows if m.buses[r[2]]["role"] == "consumer"]
        street = next((x.get("street") for x in m.bus_buildings.get(b, []) if x.get("street")), None)
        who = m.address(b) if m.buses[b]["role"] == "consumer" else m.node_label(b)
        message = (f"{len(cons) or len(rows)} connection(s) on feeder {f} are routed {ratio:.1f}× the straight line, "
                   f"e.g. {who}: {route:.0f} m of cable for {air:.0f} m."
                   + (f" Probably a gap in the street graph near {street}." if street else ""))
        F.add("TP-04", f"f{f}", severity, f"Cable route detour on feeder {f} ({ratio:.1f}×)", message,
              metrics={"feeder": f, "ratio": _r(ratio, 2), "extra_m": _r(extra, 0), "buses": len(rows)},
              targets={"buses": [r[2] for r in rows[:50]], "feeder": f, "pin": b, "lines": m.path_lines(b)},
              explains=sorted({x for r in rows for x in r[6]}), impact=ratio, basis="design",
              thresholds=[_heur("detour ratio", "detour_ratio", th), _heur("extra length (m)", "detour_extra_m", th),
                          _heur("ratio on a violated path", "detour_ratio_path", th)],
              remedy=("Inspect the streets around the bus on the map and fix or add ways in the routing data, or supply "
                      "the building from a nearer feeder or station."),
              why=("Street-routed LV cables are typically 1.2–1.6× the straight-line distance. A ratio above 3 with at "
                   "least 150 m of extra cable points to a missing street link, a station on the far side of a barrier "
                   "or cables routed round blocks."))


def _rule_tp05(m: GridModel, F: Findings, th: dict) -> None:
    kva = _f(m.g.get("kva"), 0.0) or 0.0
    n_direct = len(m.topo_direct) + sum(1 for f in m.feeders.values() if f["head_role"] == "service")
    n_out = len(m.feeders) + len(m.topo_direct)  # every circuit at the station takes a fuse way
    n_stub = sum(1 for f in m.feeders.values()
                 if f["n_cons"] <= th["stub_max_consumers"] or f["feeder_m"] < th["stub_max_length_m"])
    limit = th["outlets_warning_large"] if kva >= 630 else th["outlets_warning"]
    big_cab = max(m.cabinets.values(), key=lambda c: c.get("outgoing") or 0, default=None)
    cab_flag = big_cab is not None and (big_cab.get("outgoing") or 0) >= th["cabinet_ways_info"]
    if n_out > limit:
        severity = "warning"
    elif n_out > th["outlets_info"] or cab_flag:
        severity = "info"
    else:
        return
    message = (f"{n_out} circuits leave the {kva:.0f} kVA station ({n_direct} single service cables, {n_stub} stubs with at "
               f"most {th['stub_max_consumers']} connections); pylovo planned {m.g.get('pylovo_branches') or '–'} branches. "
               f"Typical boards have 4–8 ways."
               + (f" Cabinet {big_cab['name']} has {big_cab['outgoing']} outgoing branches." if cab_flag else ""))
    F.add("TP-05", "grid", severity, f"{n_out} outlets at the station" if n_out > th["outlets_info"] else
          f"Cabinet {big_cab['name']} with {big_cab['outgoing']} branches", message,
          metrics={"outlets": n_out, "direct_services": n_direct, "stubs": n_stub, "pylovo_branches": m.g.get("pylovo_branches"),
                   "largest_cabinet": big_cab["name"] if cab_flag else None,
                   "cabinet_ways": big_cab.get("outgoing") if cab_flag else None},
          targets={"lines": [f["head"] for f in m.feeders.values() if f["head"] is not None] + [d["line"] for d in m.topo_direct],
                   "cabinet_buses": [big_cab["bus"]] if cab_flag else [], "trafo": True},
          impact=n_out - limit, basis="design",
          thresholds=[_heur("warning above", "outlets_warning_large" if kva >= 630 else "outlets_warning", th),
                      _heur("info above", "outlets_info", th), _heur("cabinet ways", "cabinet_ways_info", th)],
          remedy=("Bundle the stubs and direct services in a cabinet near the station. For a new version raise "
                  "MIN_SHARED_PREFIX_LENGTH_M or enable AGGREGATE_NEARBY_CONNECTION_POINTS."),
          why=("Compact stations usually have 4–8 LV fuse ways (at most about 10); cable distribution cabinets 4–8. "
               "pylovo counts every line leaving the station as an outlet, so a building at the station or a short "
               "branch takes a way of its own."))


def _rule_tp06(m: GridModel, F: Findings, th: dict, context: dict, strong_feeders: set[int]) -> None:
    stations = [s for s in (context or {}).get("stations") or []
                if s["grid_result_id"] != m.g.get("grid_result_id") and s.get("plz") == m.g.get("plz")]
    own = (m.g.get("lon"), m.g.get("lat"))
    if not stations or own[0] is None:
        return
    groups: dict[int, list[tuple]] = defaultdict(list)
    for b in m.consumers:
        bus = m.buses[b]
        pos = (bus.get("lon"), bus.get("lat"))
        d_own = _dist_m(pos, own)
        if d_own is None or d_own <= th["closer_station_min_m"]:
            continue
        near = min(stations, key=lambda s: _dist_m(pos, (s["lon"], s["lat"])) or 1e12)
        d_other = _dist_m(pos, (near["lon"], near["lat"]))
        if d_other is not None and d_other < th["closer_station_ratio"] * d_own:
            groups[near["grid_result_id"]].append((d_own, d_other, b))
    for gid, rows in groups.items():
        near = next(s for s in stations if s["grid_result_id"] == gid)
        rows.sort(key=lambda r: r[1] - r[0])
        d_own, d_other, b = rows[0]
        feeders = {m.bus_feeder.get(r[2]) for r in rows}
        severity = "warning" if feeders & strong_feeders else "info"
        headroom = (m.p.u_plan * (near.get("kva") or 0) - (near.get("coincident_kw") or 0))
        message = (f"{len(rows)} consumer(s) are much closer to station {near['kcid']}/{near['bcid']} "
                   f"({near['size_label']}, {headroom:.0f} kW planning headroom) than to their own, e.g. {m.address(b)}: "
                   f"{d_own:.0f} m from its own station but only {d_other:.0f} m from the other.")
        F.add("TP-06", f"g{gid}", severity, f"{len(rows)} consumer(s) closer to station {near['kcid']}/{near['bcid']}",
              message, metrics={"other_grid": gid, "consumers": len(rows), "headroom_kw": _r(headroom, 0),
                                "d_own_m": _r(d_own, 0), "d_other_m": _r(d_other, 0)},
              targets={"buses": [r[2] for r in rows], "pin": b}, impact=len(rows), basis="design",
              thresholds=[_heur("distance ratio", "closer_station_ratio", th), _heur("minimum distance (m)", "closer_station_min_m", th)],
              remedy="Reassign the buildings to the neighbouring grid (a future editing action), or review the clustering parameters for a new version.",
              why=("A DSO supplies a building from the nearest suitable station. pylovo's clustering (k-means plus "
                   "capacity) can leave boundary buildings on a distant station."))


# =========================================================================== data
def _rule_da01(m: GridModel, F: Findings, th: dict, top_causes: dict[int, list[str]]) -> None:
    p = m.p
    for bus, buildings in m.bus_buildings.items():
        for b in buildings:
            rfa = _f(b.get("residential_floor_area"), 0.0) or 0.0
            hh = int(b.get("households") or 0)
            if rfa <= 0 or (_f(b.get("residential_peak_load_in_kw"), 0.0) or 0.0) <= 0 or hh <= 0:
                continue
            expected, fixed = _expected_households(p, b)
            a = rfa / hh
            typ = b.get("type")
            reasons, severity = [], None
            if a < th["area_per_household_warning_m2"]:
                reasons.append(f"only {a:.0f} m² per household")
                severity = "warning"
            if expected and hh > th["households_factor"] * expected:
                reasons.append(f"{hh / expected:.1f}× what pylovo's fallback would give")
                severity = "warning"
            if severity is None:
                if a < th["area_per_household_info_m2"]:
                    reasons.append(f"{a:.0f} m² per household")
                    severity = "info"
                if typ in ("SFH", "TH") and hh > 2:
                    reasons.append(f"a {typ} with {hh} households")
                    severity = "info"
                if typ in ("AB", "MFH") and expected and hh < expected / th["households_factor"]:
                    reasons.append("far fewer households than the floor area suggests")
                    severity = "info"
            fa = _f(b.get("floor_area"), 0.0) or 0.0
            floors = b.get("floor_number")
            hall = fa >= th["hall_footprint_m2"] and (floors or 0) <= 2
            if hall:
                reasons.append("the footprint looks like a hall or farm building")
                severity = _worst(severity, "info")
            if 0 < fa < th["shed_footprint_m2"]:
                reasons.append(f"a {fa:.0f} m² footprint (garage or shed?)")
                severity = _worst(severity, "info")
            occ = _f(b.get("occupants"))
            if occ is not None and hh and not (1 <= occ / hh <= 5):
                reasons.append(f"{occ / hh:.1f} occupants per household")
                severity = _worst(severity, "info")
            if severity is None:
                continue
            raised = bool(top_causes.get(bus))
            if raised:
                severity = _worst(severity, "warning")
            estimated = (expected is not None and not fixed and hh == expected and (typ in ("AB", "MFH") or not typ))
            area = (p.fallback.get(typ) or p.fallback.get("untyped_residential") or {}).get("residential_area_per_household_m2")
            forced = bool(estimated and area and rfa / area < (p.fallback.get(typ) or {}).get("minimum_households", 1))
            addr = f"{b.get('street') or ''} {b.get('house_number') or ''}".strip() or str(b["objectid"])
            message = (f"{addr} ({typ or 'untyped'}, {floors or '?'} floors, footprint {fa:.0f} m²): {hh} households on "
                       f"{rfa:.0f} m² residential floor area ({a:.0f} m² each); pylovo's fallback would give "
                       f"{expected if expected is not None else '–'}. Flagged: {'; '.join(reasons)}."
                       + (f" The count itself comes from that fallback{' (forced up to its minimum)' if forced else ''}."
                          if estimated else "")
                       + f" Installed load {_f(b.get('peak_kw'), 0.0):.0f} kW."
                       + (" The building is a top cause of a symptom, so the finding is raised." if raised else ""))
            F.add("DA-01", f"o{b['objectid']}", severity, f"Households of {addr} look implausible", message,
                  metrics={"households": hh, "expected": expected, "area_per_household_m2": _r(a, 1),
                           "residential_floor_area_m2": _r(rfa, 0), "footprint_m2": _r(fa, 0), "floors": floors,
                           "estimated": estimated, "forced": forced, "hall": hall, "reasons": reasons},
                  targets={"buildings": [b["objectid"]], "buses": [bus], "pin": bus, "feeder": m.bus_feeder.get(bus)},
                  explains=top_causes.get(bus, []), impact=hh, basis="data",
                  thresholds=[_heur("m² per household (warning)", "area_per_household_warning_m2", th),
                              _heur("m² per household (info)", "area_per_household_info_m2", th),
                              _heur("factor to the fallback", "households_factor", th),
                              _threshold("HOUSEHOLD_FALLBACK", "per type", "version parameter")],
                  remedy=("Correct the households or the type of the building in the source data (or with the building "
                          "editor); the peak load, snapshot, design loads, power flow and diagnostics are then recomputed."),
                  why=("German dwellings average about 92 m² of living area (Destatis), roughly 110–120 m² gross floor "
                       "area; less than 30 m² gross per household is implausible except for micro-apartments. The "
                       "expected value uses pylovo's own HOUSEHOLD_FALLBACK."))


def _rule_da02(m: GridModel, F: Findings, th: dict, context: dict, top_causes: dict[int, list[str]]) -> None:
    p = m.p
    kva = _f(m.g.get("kva"), 0.0) or 0.0
    median = (context or {}).get("building_peak_median_kw")
    p99 = (context or {}).get("building_peak_p99_kw")
    nonres_one = 0
    for bus, buildings in m.bus_buildings.items():
        own = m.own.get(bus)
        f = m.bus_feeder.get(bus)
        feeder = m.feeders.get(f) or {}
        for b in buildings:
            res_kw = _f(b.get("residential_peak_load_in_kw"), 0.0) or 0.0
            if (b.get("households") or 0) == 1 and res_kw <= 0 and b.get("nonresidential_use"):
                nonres_one += 1
        if not own:
            continue
        design = own["design"]
        reasons, severity = [], None
        share_trafo = design / kva if kva else None
        # Share of the feeder's snapshot load: the snapshot spreads each category's coincident load in
        # proportion to installed power, so a single household is not "40 % of a feeder".
        share_feeder = own["p"] / feeder["p_kw"] if feeder.get("p_kw") else None
        if share_trafo is not None and share_trafo >= th["building_trafo_share"]:
            reasons.append(f"{share_trafo:.0%} of the {kva:.0f} kVA station")
            severity = "warning" if share_trafo >= th["building_trafo_share_warning"] else "info"
        if share_feeder is not None and feeder.get("n_cons", 0) >= 5 and share_feeder >= th["building_feeder_share"]:
            reasons.append(f"{share_feeder:.0%} of feeder {f}'s load")
            severity = _worst(severity, "info")
        for b in buildings:
            peak = _f(b.get("peak_kw"), 0.0) or 0.0
            if median and p99 and peak >= th["building_peak_factor"] * median and peak > p99:
                reasons.append(f"installed {peak:.0f} kW, above the version's P99 ({p99:.0f} kW)")
                severity = _worst(severity, "info")
            nres_area = _f(b.get("nonresidential_floor_area"), 0.0) or 0.0
            if b.get("nonresidential_use") in ("Commercial", "Public") and (b.get("floor_number") or 0) <= 1 and \
                    nres_area >= th["hall_footprint_m2"]:
                reasons.append(f"a single-storey {b['nonresidential_use'].lower()} hall of {nres_area:.0f} m² charged at the full peak load per m²")
                severity = _worst(severity, "info")
            nres_kw = _f(b.get("nonresidential_peak_load_in_kw"), 0.0) or 0.0
            if th["mv_threshold_band"] * p.mv_kw <= nres_kw < p.mv_kw and not b.get("nonresidential_mv_direct"):
                reasons.append(f"{nres_kw:.0f} kW non-residential, just below the {p.mv_kw:g} kW MV-direct threshold")
                severity = _worst(severity, "info")
        if severity is None:
            continue
        raised = bool(top_causes.get(bus))
        if raised:
            severity = _worst(severity, "warning")
        cats = ", ".join(sorted(c for c, v in own["cats"].items() if v[0] > 0))
        message = (f"{m.address(bus)} ({m.building_type(bus) or 'building'}; {cats}): {design:.0f} kW local design load"
                   + (f" = {share_trafo:.0%} of the {kva:.0f} kVA station" if share_trafo is not None else "")
                   + (f" and {share_feeder:.0%} of feeder {f}" if share_feeder is not None else "")
                   + f". Installed {own['installed']:.0f} kW ({m.households(bus)} households). Flagged: {'; '.join(reasons)}."
                   + (" The building is a top cause of a symptom, so the finding is raised." if raised else ""))
        F.add("DA-02", f"b{bus}", severity, f"Load of {m.address(bus)} ({design:.0f} kW)", message,
              metrics={"bus": bus, "design_kw": _r(design, 1), "installed_kw": _r(own["installed"], 1),
                       "share_trafo": _r(share_trafo, 3), "share_feeder": _r(share_feeder, 3), "reasons": reasons},
              targets={"buses": [bus], "pin": bus, "feeder": f}, explains=top_causes.get(bus, []), impact=design,
              basis="data",
              thresholds=[_heur("station share", "building_trafo_share", th), _heur("feeder share", "building_feeder_share", th),
                          _threshold("MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW", p.mv_kw, p.source["mv_threshold_kw"])],
              remedy="Check the building (households, use, floor area, MV-direct flag) and recompute. Otherwise give it a dedicated outlet or an MV connection.",
              why=("One connection using a quarter of a station, or 40 % of a feeder, is unusual in public LV grids; DSOs "
                   "connect such customers on their own outlet or at MV (VDE-AR-N 4110). pylovo charges non-residential "
                   "load as floor area × peak_load_per_m2."))
    if nonres_one:
        F.add("DA-02", "grid:nonres", "info", f"{nonres_one} commercial/public buildings counted as one household",
              (f"{nonres_one} commercial or public buildings carry households = 1 without residential load; the household "
               "KPIs of the inspector and the statistics count them."),
              metrics={"buildings": nonres_one}, targets={}, impact=0, basis="data",
              remedy="Keep this in mind when comparing household counts with other sources.",
              why="pylovo stores households = 1 for every building, including non-residential ones.")


# =========================================================================== entry points
def _checks(m: GridModel, F: Findings) -> list[dict]:
    """One line per symptom for the calm 'no findings' state."""
    p, g = m.p, m.g
    worst = {r: _worst(*(f["severity"] for f in F.of(r))) for r in ("VT-01", "LD-01", "TR-01", "DE-01", "DA-03")}
    lv = [m.vm(b) for b in m.consumers if m.vm(b) is not None]
    vm_min = min(lv) if lv else None
    kva = _f(g.get("kva"))
    u_p = (_f(g.get("coincident_kw"), 0.0) or 0.0) * m.scaling / kva if kva else None
    tilde = "≈" if m.estimated else ""
    max_u = max((ln.get("u_pf") or 0.0 for ln in m.lines.values()), default=0.0) if m.pf else \
        max((ln["u_s"] for ln in m.lines.values()), default=0.0)
    checks = [
        {"rule": "VT-01", "label": "Voltage band", "status": worst["VT-01"] or "ok",
         "value": f"min {tilde}{vm_min:.3f} p.u. (limit {p.min_vm:g})" if vm_min is not None else "–"},
        {"rule": "TR-01", "label": "Transformer loading", "status": worst["TR-01"] if worst["TR-01"] not in (None, "info") else "ok",
         "value": (f"{u_p:.0%} of {g.get('size_label')} in kW (target {p.u_plan:.0%})" if u_p is not None else "–")
         + (f", {m.trafo_loading:.0%} in the power flow" if m.trafo_loading is not None else "")},
        {"rule": "DE-01", "label": "pylovo feeder design limit", "status": worst["DE-01"] or "ok",
         "value": f"{_n(_f(g.get('design_feeder_drop_pct')), 2)} % (limit {p.feeder_limit:g} %)"},
        {"rule": "DE-04", "label": "pylovo service design limit",
         "status": "warning" if g.get("service_limit_met") is False else "ok",
         "value": f"{_n(_f(g.get('design_service_drop_pct')), 2)} % (limit {p.service_limit:g} %)"},
        {"rule": "LD-01", "label": "Cable loading", "status": (worst["LD-01"] or "ok") if m.pf else
         ("estimate" if worst["LD-01"] else "ok"),
         "value": f"max {max_u:.0%} of the ampacity" + ("" if m.pf else " (estimated, run the power flow for exact values)")},
        {"rule": "DA-03", "label": "Generation check", "status": worst["DA-03"] if worst["DA-03"] == "critical" else "ok",
         "value": (g.get("power_flow_status") or "unknown").replace("_", " ")
         + (f", max drop {_n(_f(g.get('max_total_drop_pct')), 2)} % below the busbar" if g.get("max_total_drop_pct") is not None else "")},
    ]
    return checks


def diagnose(inputs: dict, gp: dict | None, pf: dict | None = None, context: dict | None = None,
             thresholds: dict | None = None) -> dict:
    """Run all rules on one grid.

    Args:
        inputs: Compact grid inputs: ``grid`` (the inspector's KPI record plus
            ``ampacity_feeder_drop_pct`` and ``settlement_type``), ``buses``, ``lines``,
            ``loads``, ``trafo``, ``buildings``, ``splits`` and ``vm_ext``.
        gp: The version's stored ``generation_parameters``.
        pf: Result of :func:`pylovo_api.powerflow.run` (optional; a failed run is allowed).
        context: Version statistics (:func:`version_statistics` plus quantiles and stations).
        thresholds: Overrides of :data:`DEFAULT_THRESHOLDS` (heuristics only).

    Returns:
        ``{"source", "findings", "budget", "checks", "counts", "meta"}``. Findings are sorted by
        severity, then impact, then rule order.
    """
    th = effective_thresholds(thresholds)
    params = Params(gp)
    model = GridModel(inputs, params, pf)
    F = Findings(model, th)
    context = context or {}

    vt = _rule_vt01(model, F, th)
    ld01 = _rule_ld01(model, F, th, vt)
    _rule_tr01(model, F, th)
    de01 = _rule_de01(model, F, th, vt)
    de03 = _rule_de03(model, F, th)
    targets = _voltage_targets(model, vt, de03, th)
    vt02 = _rule_vt02(model, F, th, targets)
    top_causes = _rule_vt03_vt04(model, F, th, targets, de01)
    _rule_tr02(model, F, th, targets)
    ld02 = _rule_ld02(model, F, th)
    # LD-01 causes: sizing without reserve and the current share of large buildings.
    for key, info in ld01.items():
        sid = info["section"]
        if sid is not None and ld02.get(sid) == "warning":
            F.items[f"LD-02:s{sid}"]["explains"].append(info["id"])
        line = model.lines[info["line"]]
        child = model.child_of.get(info["line"])
        if child is None or not line["I_s"]:
            continue
        for b in model.subtree(child):
            own = model.own.get(b)
            if not own:
                continue
            i_b = math.hypot(own["p"], own["q"]) * 1000 / (SQRT3 * params.vn)
            share = i_b / line["I_s"]
            if share >= th["load_current_share"] and share < 0.999:
                top_causes[b].append(info["id"])
                F.add("VT-03", f"b{b}:ld{key}", _cap("warning", info["severity"]) if not info["estimated"] else "info",
                      f"{model.address(b)} carries {share:.0%} of the current in "
                      f"{'S' + str(sid) if sid is not None else 'the cable'}",
                      (f"{model.address(b)} ({own['p']:.0f} kW snapshot, {own['installed']:.0f} kW installed) draws "
                       f"{i_b:.0f} A, {share:.0%} of the {line['I_s']:.0f} A snapshot current of "
                       f"{'S' + str(sid) if sid is not None else 'this cable'}."),
                      metrics={"bus": b, "current_share": _r(share, 3), "current_a": _r(i_b, 1)},
                      targets={"buses": [b], "pin": b, "feeder": model.bus_feeder.get(b)}, explains=[info["id"]],
                      share=share, impact=share * 100, estimated=info["estimated"],
                      thresholds=[_heur("current share", "load_current_share", th)],
                      remedy="Check the building data, or connect it through its own outlet.",
                      why="A single building that draws a large part of a section's current dominates its loading.")
    _rule_ld03(model, F, th, context, ld01, ld02, vt02)
    ld04 = _rule_ld04(model, F, th, ld01, vt)
    _rule_ld05(model, F, th)
    ld06 = _rule_ld06(model, F, th, context, vt, ld01)
    de05 = _rule_de05(model, F, th)
    linked_tp01: dict[int, list[str]] = defaultdict(list)
    for f in model.feeders:
        for src in (vt, de01, de03, de05):
            if f in src and (src is not de03 or src[f]["severity"] != "info"):
                linked_tp01[f].append(src[f]["id"])
    tp01 = _rule_tp01(model, F, th, context, linked_tp01)
    heavy: dict[int, list[str]] = defaultdict(list)
    for f in model.feeders:
        if f in vt:
            heavy[f].append(vt[f]["id"])
        heavy[f] += [info["id"] for info in ld01.values() if info.get("section") is not None and not info["estimated"]
                     and model.sections[info["section"]]["feeder"] == f]
        if f in ld04 and ld04[f]["severity"] == "warning":
            heavy[f].append(ld04[f]["id"])
        if f in ld06 and ld06[f]["severity"] == "warning":
            heavy[f].append(ld06[f]["id"])
    _rule_tp02(model, F, th, heavy)
    strong_tp03 = [x["id"] for x in vt.values()] + [x["id"] for x in de01.values()] + \
                  [f"TP-01:f{f}" for f, s in tp01.items() if s == "warning"]
    _rule_tp03(model, F, th, bool(strong_tp03), strong_tp03)
    _rule_tp04(model, F, th, {f: t for f, t in targets.items() if f in vt})
    _rule_tp05(model, F, th)
    strong = set(vt) | {model.sections[i["section"]]["feeder"] for i in ld01.values()
                        if i.get("section") is not None and not i["estimated"]}
    _rule_tp06(model, F, th, context, strong)
    _rule_de02(model, F, th, de01)
    voltage_buses = {t["bus"] for t in targets.values()}
    _rule_de04(model, F, th, context, voltage_buses)
    top3 = {b: ids for b, ids in top_causes.items()}
    _rule_da01(model, F, th, top3)
    _rule_da02(model, F, th, context, top3)
    _rule_da03(model, F, th, pf)

    findings = _finalise(F, th)
    budget = [dict(info["budget"], symptom=info["id"], severity=info["severity"]) for info in
              sorted(vt.values(), key=lambda x: x["vm"])]
    for entry in budget:
        tr02 = F.get("TR-02:grid")
        if tr02 and tr02["metrics"].get("tap_steps"):
            entry["tap"] = {"steps": tr02["metrics"]["tap_steps"], "vm_after": tr02["metrics"]["vm_after_tap"]}
    counts = {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}
    if model.pf:
        source = {"basis": "power_flow", "scaling": model.scaling,
                  "text": f"Power flow ×{_x(model.scaling)}: exact voltages, currents and loadings."}
    else:
        source = {"basis": "stored_check", "scaling": 1.0,
                  "text": ("Symptoms: generation check, pylovo's validation power flow when this grid was saved (loads "
                           "×1.0, identical to Run at ×1.0). Attribution: linearised estimate; cable loadings are "
                           "estimated. Run the power flow for exact values.")}
    return {
        "grid_result_id": model.g.get("grid_result_id"),
        "source": source,
        "findings": findings,
        "budget": budget,
        "checks": _checks(model, F),
        "counts": counts,
        "cabinets": [{"name": c["name"], "bus": c["bus"], "split_bus": c["split_bus"], "outgoing": c["outgoing"],
                      "distance_m": c["distance_m"]} for c in sorted(model.cabinets.values(), key=lambda c: c["distance_m"])],
        "meta": {"thresholds": th, "parameters": params.source, "kappa": _r(model.kappa, 4),
                 "trafo_drop_estimate_pp": _r(model.dU_T_est, 3), "generation_parameters_stored": params.stored,
                 "context_grids": context.get("grids"), "context_sampled": context.get("sampled", False)},
    }


def _finalise(F: Findings, th: dict) -> list[dict]:
    items = list(F.items.values())
    by_id = {f["id"]: f for f in items}
    # Keep explains pointing to existing symptoms only, and cap the causes per rule and symptom.
    for f in items:
        f["explains"] = [x for x in dict.fromkeys(f["explains"]) if x in by_id and x != f["id"]]
    per_key: dict[tuple, list[dict]] = defaultdict(list)
    for f in items:
        for sym in f["explains"]:
            per_key[(f["rule"], sym)].append(f)
    drop: set[str] = set()
    for rows in per_key.values():
        rows.sort(key=lambda x: (SEV_RANK[x["severity"]], -x["impact"]))
        for extra in rows[th["max_causes_per_rule"]:]:
            if all(len(per_key[(extra["rule"], s)]) > th["max_causes_per_rule"] for s in extra["explains"]):
                drop.add(extra["id"])
    items = [f for f in items if f["id"] not in drop]
    for f in items:
        f["causes"] = sorted((c["id"] for c in items if f["id"] in c["explains"]),
                             key=lambda cid: (SEV_RANK[by_id[cid]["severity"]], -(by_id[cid]["contribution_pp"] or 0),
                                              -by_id[cid]["impact"]))
    # Symptoms first (in catalogue order: voltage, overload, transformer, design, check), then the others
    # by impact. Within one severity a voltage violation therefore precedes the design-limit miss it causes.
    items.sort(key=lambda f: (SEV_RANK[f["severity"]], 0 if f["kind"] == "symptom" else 1,
                              RULE_ORDER[f["rule"]] if f["kind"] == "symptom" else 0, -f["impact"],
                              RULE_ORDER[f["rule"]], f["id"]))
    return items


def compare(current: dict, baseline: dict) -> dict:
    """New and resolved findings (warning or critical) of ``current`` against ``baseline``."""
    def key_set(result: dict) -> dict[str, str]:
        return {f["id"]: f["severity"] for f in result.get("findings", []) if f["severity"] != "info"}

    now, before = key_set(current), key_set(baseline)
    new = sorted(fid for fid in now if fid not in before)
    resolved = sorted(fid for fid in before if fid not in now)
    changed = sorted(fid for fid in now if fid in before and now[fid] != before[fid])
    return {"new": new, "resolved": resolved, "changed": changed,
            "resolved_titles": {f["id"]: f["title"] for f in baseline.get("findings", []) if f["id"] in resolved}}


def version_statistics(inputs: list[dict], gp: dict | None) -> dict:
    """Version-wide statistics used as peers: feeder reach, demand density, section table.

    Args:
        inputs: Compact inputs of (a sample of) the version's grids; buildings are not needed.
        gp: The version's generation parameters.

    Returns:
        ``{"reach_p90_m", "density_median_kw_per_km", "sections": [(I_d, z, label), ...]}``.
    """
    params = Params(gp)
    reaches, densities, sections = [], [], []
    for inp in inputs:
        try:
            m = GridModel(inp, params, None)
        except Exception:  # noqa: BLE001 - a broken grid does not spoil the statistics
            continue
        for info in m.feeders.values():
            if info["real"]:
                reaches.append(info["reach_m"])
            if info["head"] is not None and info["head_role"] == "feeder" and info["feeder_m"] >= 150:
                densities.append(info["design_kw"] / (info["feeder_m"] / 1000))
        for sec in m.sections.values():
            label = sec["std_type"] + (f" ×{sec['parallel']}" if sec["parallel"] > 1 else "")
            sections.append((round(sec["I_d"], 2), round(sec["z"], 6), label))
    sections.sort()
    return {"reach_p90_m": _r(_quantile(reaches, 0.9), 0),
            "density_median_kw_per_km": _r(statistics.median(densities), 1) if densities else None,
            "sections": sections}


def grid_summary(result: dict) -> dict:
    """Compact per-grid row for the Statistics panel."""
    findings = result.get("findings", [])
    worst = _worst(*(f["severity"] for f in findings))
    return {"grid_result_id": result.get("grid_result_id"), "counts": result.get("counts"), "worst": worst,
            "top": [{"id": f["id"], "rule": f["rule"], "severity": f["severity"], "title": f["title"]}
                    for f in findings if f["severity"] != "info"][:4],
            "rules": sorted({f["rule"] for f in findings if f["severity"] != "info"}, key=RULE_ORDER.get),
            "rules_info": sorted({f["rule"] for f in findings if f["severity"] == "info"}, key=RULE_ORDER.get)}


def rule_frequency(rows: list[dict]) -> list[dict]:
    """How many grids show each rule (warning or critical, and info) — a calibration aid."""
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        for rule in row.get("rules", []):
            counts.setdefault(rule, {"grids": 0, "info_grids": 0})["grids"] += 1
        for rule in row.get("rules_info", []):
            counts.setdefault(rule, {"grids": 0, "info_grids": 0})["info_grids"] += 1
    return sorted(({"rule": rule, "title": RULES[rule]["title"], "category": RULES[rule]["category"], **c}
                   for rule, c in counts.items()), key=lambda r: (-r["grids"], -r["info_grids"], RULE_ORDER[r["rule"]]))
