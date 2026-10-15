"""Parameters of one MV variant and the values the MV stage reads from the snapshot of the LV version.

:class:`MvParameters` holds every MV parameter: the ``MV_*`` keys of ``config_generation.yaml``
(:data:`~pylovo.mv_grid.options.YAML_KEYS`) and the constants (:data:`~pylovo.mv_grid.options.CONSTANTS`), one field
each. The stage code receives this object and never reads ``MV_*`` constants itself; only
:meth:`MvParameters.from_config` reads :mod:`pylovo.config_loader`. Its snapshot (:meth:`MvParameters.to_snapshot`)
is stored once per MV variant in ``pylovo.mv_variant.mv_parameters`` and contains the resolved line and HV/MV
transformer types, so a stored variant stays reproducible when the pandapower type tables change.

:class:`LvVersionParameters` holds the LV values the MV stage needs (sim factors, power factor, ...). They are read
from the stored snapshot of the LV version (``pylovo.version.generation_parameters``), never from today's config.

The line and transformer types come from :mod:`pylovo.mv_grid.equipment` (pandapower), imported only when a type is
resolved.
"""
from __future__ import annotations

import copy
import dataclasses
import importlib
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np

from pylovo.mv_grid.model import CATEGORIES, HvMvType, LineType
from pylovo.mv_grid.options import (
    AUTO_VOLTAGE,
    CONSTANTS,
    MV_STAGE_VERSION,
    YAML_KEYS,
    Spec,
    check_mv_combination,
    coerce_mv_value,
    default_value,
    spec_of,
)
from pylovo.version_snapshot import MV_ADDED_LATER, UNKNOWN

logger = logging.getLogger(__name__)

#: Layout version of the MV variant snapshot.
SNAPSHOT_SCHEMA: int = 1

#: Rules of this code version that are recorded in the snapshot but are no parameters (a change of one of them
#: changes results, so it also bumps :data:`~pylovo.mv_grid.options.MV_STAGE_VERSION`).
FIXED_RULES: dict[tuple[str, ...], Any] = {
    ("loads", "customer_peak"): "sim_factor_times_installed",
    ("generation", "include_storage"): False,
    ("taps", "rule"): "centre_load_feed_in",
    ("coupling", "merged_scaling"): "feeder_category",
    ("coupling", "lv_pv_at"): "LVbus 1",
}

#: Fields whose snapshot path holds the resolved type data instead of the field value.
RESOLVED_FIELDS: tuple[str, ...] = ("cable_type", "overhead_type", "hv_mv_sizes_mva")
#: ``MV_NOMINAL_VOLTAGE_KV`` auto: the voltage of the districts below ``mv_10kv_min_density_mva_km2`` (the base
#: voltage of the variant, whose resolved types are the unsuffixed equipment records of the snapshot) ...
AUTO_BASE_KV: float = 20.0
#: ... and the voltage of the dense districts (snapshot records ``cable_10kv``, ``overhead_10kv``,
#: ``hv_mv_types_10kv``).
AUTO_DENSE_KV: float = 10.0
#: Suffix of the equipment records of :data:`AUTO_DENSE_KV` in the snapshot of an auto variant.
DENSE_SUFFIX: str = "_10kv"
#: Fields the resolved types depend on (a change drops the cached types).
_TYPE_FIELDS = frozenset({"vn_kv", *RESOLVED_FIELDS})
#: The non-residential consumer categories (the MV customers): their specific peak load per m² is read.
NONRESIDENTIAL_CATEGORIES: tuple[str, ...] = tuple(c for c in CATEGORIES if c != "Residential")
#: Station ratings of pylovo's default ``TRANSFORMER_MAPPING`` (kVA), for LV snapshots without a catalogue.
DEFAULT_STATION_RATINGS_KVA: tuple[float, ...] = (250.0, 400.0, 630.0)


class MvStageError(RuntimeError):
    """The MV stage cannot run on its inputs (for example an incomplete snapshot of the LV version)."""


def _y(name: str) -> Any:
    return default_value(name)


def _c(name: str) -> Any:
    return dataclasses.field(default_factory=lambda: default_value(name))


@dataclass(frozen=True)
class MvParameters:
    """All parameters of one MV variant; every value is checked and coerced on construction.

    The first fields are the ``MV_*`` keys of ``config_generation.yaml`` (YAML key in the comment), the others the
    constants of :data:`~pylovo.mv_grid.options.CONSTANTS` under their own names. Create it with
    :meth:`from_config`, :meth:`from_raw` (YAML keys and constant names), :meth:`from_snapshot` or the
    constructor (field names), and derive variants with :meth:`replace` or :meth:`replace_raw`.

    Raises:
        ValueError: On construction, if a value does not fit its key (the message names the YAML key or the
            constant) or if ``MV_SUPPLY_AREAS`` egon is combined with ``MV_SUBSTATIONS`` osm.
    """

    variant_id: str = _y("MV_VARIANT_ID")                   # MV_VARIANT_ID
    comment: str = _y("MV_VARIANT_COMMENT")                 # MV_VARIANT_COMMENT
    vn_kv: float | str = _y("MV_NOMINAL_VOLTAGE_KV")        # MV_NOMINAL_VOLTAGE_KV: 20.0, 10.0 or "auto"
    substations: str = _y("MV_SUBSTATIONS")                 # MV_SUBSTATIONS
    supply_areas: str = _y("MV_SUPPLY_AREAS")               # MV_SUPPLY_AREAS
    routing: str = _y("MV_ROUTING")                         # MV_ROUTING
    solver: str = _y("MV_RING_SOLVER")                      # MV_RING_SOLVER
    max_stations_per_ring: int = _y("MV_MAX_STATIONS_PER_RING")   # MV_MAX_STATIONS_PER_RING
    stubs: str = _y("MV_STUBS")                             # MV_STUBS
    overhead_lines: str = _y("MV_OVERHEAD_LINES")           # MV_OVERHEAD_LINES
    interconnection: str = _y("MV_INTERCONNECTION")         # MV_INTERCONNECTION
    cable_type: str | None = _y("MV_STANDARD_CABLE")        # MV_STANDARD_CABLE
    overhead_type: str | None = _y("MV_OVERHEAD_LINE")      # MV_OVERHEAD_LINE
    line_utilization: float = _y("MV_LINE_PLANNING_UTILIZATION")  # MV_LINE_PLANNING_UTILIZATION
    max_drop_percent: float = _y("MV_MAX_VOLTAGE_DROP_PERCENT")   # MV_MAX_VOLTAGE_DROP_PERCENT
    # voltage
    busbar_vm_pu: float = _y("busbar_vm_pu")
    busbar_band_pu: float = _y("busbar_band_pu")
    max_drop_n1_percent: float = _y("max_drop_n1_percent")
    max_rise_percent: float = _y("max_rise_percent")
    mv_10kv_min_density_mva_km2: float = _y("mv_10kv_min_density_mva_km2")
    # loading
    n1_utilization: float = _y("n1_utilization")
    hv_mv_n1_utilization: float = _y("hv_mv_n1_utilization")
    # topology
    max_half_ring_length_m: float = _y("max_half_ring_length_m")
    stub_max_kva: float = _y("stub_max_kva")
    stub_max_length_m: float = _y("stub_max_length_m")
    stub_min_offset_m: float = _y("stub_min_offset_m")
    loop_in_max_offset_m: float = _y("loop_in_max_offset_m")
    generator_max_tee_m: float = _y("generator_max_tee_m")
    tie_max_length_m: float = _y("tie_max_length_m")
    local_search_rounds: int = _y("local_search_rounds")
    neighbours: int = _y("neighbours")
    ls_time_guard_s: float = _y("ls_time_guard_s")
    repair_rounds: int = _y("repair_rounds")
    # loads
    dedicated_feeder_kw: float = _y("dedicated_feeder_kw")
    hv_customer_kw: float = _y("hv_customer_kw")
    customer_hall_min_footprint_m2: float | None = _y("customer_hall_min_footprint_m2")   # None: no hall rule
    customer_min_mv_kw: float = _y("customer_min_mv_kw")
    busbar_customer_max_m: float | None = _y("busbar_customer_max_m")                     # None: no limit
    # generation
    generator_lv_side_kw: float = _y("generator_lv_side_kw")
    generator_fallback_kw: dict[str, float] = _c("generator_fallback_kw")
    mv_gen_cos_phi: float = _y("mv_gen_cos_phi")
    lv_pv_cos_phi: float = _y("lv_pv_cos_phi")
    # cases
    feed_in_cases: dict[str, dict[str, float]] = _c("feed_in_cases")
    # equipment
    hv_mv_sizes_mva: tuple[float, ...] = _y("hv_mv_sizes_mva")
    hv_mv_units: int = _y("hv_mv_units")
    max_parallel_cables: int = _y("max_parallel_cables")
    # routing and graph
    crowfly_detour: float = _y("crowfly_detour")
    overhead_detour: float = _y("overhead_detour")
    bridge_detour: float = _y("bridge_detour")
    long_connector_m: float = _y("long_connector_m")
    graph_excluded_klasse: tuple[str, ...] = _y("graph_excluded_klasse")
    graph_snap_tolerance_m: float = _y("graph_snap_tolerance_m")
    region_buffer_m: float = _y("region_buffer_m")
    subgraph_buffer_m: float = _y("subgraph_buffer_m")
    # settlements
    settlement_buffer_m: float = _y("settlement_buffer_m")
    settlement_spacing_m: float = _y("settlement_spacing_m")
    settlement_min_buildings: int = _y("settlement_min_buildings")
    urban_min_buildings: int = _y("urban_min_buildings")
    settlement_polygon_buffer_m: float = _y("settlement_polygon_buffer_m")
    # substations and supply areas
    substation_merge_m: float = _y("substation_merge_m")
    substation_drop_m: float = _y("substation_drop_m")
    ehv_partner_m: float = _y("ehv_partner_m")
    substation_max_attach_m: float = _y("substation_max_attach_m")
    egon_match_m: float = _y("egon_match_m")
    rebalance_step_m: float = _y("rebalance_step_m")
    rebalance_max_iter: int = _y("rebalance_max_iter")
    # MV/LV taps
    mvlv_tap_step_percent: float = _y("mvlv_tap_step_percent")
    mvlv_tap_min: int = _y("mvlv_tap_min")
    mvlv_tap_max: int = _y("mvlv_tap_max")
    lv_design_tolerance_pu: float = _y("lv_design_tolerance_pu")
    lv_feed_in_max_pu: float = _y("lv_feed_in_max_pu")
    # resolved types {(cable | overhead | hv_mv_types, vn_kv): ...}, filled lazily or from a snapshot; no parameter
    _types: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for spec in _FIELD_SPECS.values():
            object.__setattr__(self, spec.field, coerce_mv_value(spec.name, getattr(self, spec.field)))
        check_mv_combination({"MV_SUBSTATIONS": self.substations, "MV_SUPPLY_AREAS": self.supply_areas})

    def __hash__(self) -> int:
        # the generated hash would fail on the dict fields (generator_fallback_kw, feed_in_cases)
        return hash(json.dumps({name: _json_value(getattr(self, name)) for name in _FIELD_SPECS}, sort_keys=True))

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(cls) -> "MvParameters":
        """The parameters of the configuration (``MV_*`` of :mod:`pylovo.config_loader`; constants at default).

        This is the only reader of the ``MV_*`` constants. The line and HV/MV transformer types are resolved at
        once (pandapower), so a type name that does not fit fails here.

        Raises:
            ValueError: If a configured type does not exist or does not fit the nominal voltage (names the key).
        """
        from pylovo import config_loader

        params = cls.from_raw({name: getattr(config_loader, name) for name, spec in YAML_KEYS.items() if spec.field})
        params.check_types()
        return params

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "MvParameters":
        """Parameters from YAML keys and constant names (missing ones at their defaults)."""
        return cls(**_fields_from_raw(raw))

    @classmethod
    def from_snapshot(cls, variant_id: str, comment: str, snapshot: Mapping[str, Any]) -> "MvParameters":
        """The parameters of a stored MV variant snapshot (:meth:`to_snapshot`), with its resolved types.

        The types are taken from the snapshot, not from pandapower. A value missing from an older snapshot takes
        its legacy value from :data:`~pylovo.version_snapshot.MV_ADDED_LATER`, else its current default.

        Raises:
            MvStageError: If the snapshot has another layout or no resolved equipment.
        """
        if snapshot.get("schema") != SNAPSHOT_SCHEMA:
            raise MvStageError(f"MV snapshot schema {snapshot.get('schema')!r} is not supported "
                               f"(this code writes schema {SNAPSHOT_SCHEMA})")
        values: dict[str, Any] = {"variant_id": variant_id, "comment": comment}
        for spec in _SNAPSHOT_SPECS:
            found, value = _lookup(snapshot, spec.path)
            if not found:
                legacy = MV_ADDED_LATER.get(spec.path, UNKNOWN)
                value = default_value(spec.name) if legacy is UNKNOWN else copy.deepcopy(legacy)
            values[spec.field] = value
        vn_kv = coerce_mv_value("MV_NOMINAL_VOLTAGE_KV", values["vn_kv"])
        base = AUTO_BASE_KV if vn_kv == AUTO_VOLTAGE else float(vn_kv)
        suffixes = {base: ""}
        if vn_kv == AUTO_VOLTAGE:
            suffixes[AUTO_DENSE_KV] = DENSE_SUFFIX
        types: dict = {}
        try:
            equipment = snapshot["equipment"]
            for vn, suffix in suffixes.items():
                types[("cable", vn)] = LineType(**equipment["cable" + suffix])
                types[("overhead", vn)] = LineType(**equipment["overhead" + suffix])
                types[("hv_mv_types", vn)] = tuple(HvMvType(**item) for item in equipment["hv_mv_types" + suffix])
        except (KeyError, TypeError) as exc:
            raise MvStageError(f"the MV snapshot has no complete resolved equipment ({exc!r})") from exc
        cable, overhead = types[("cable", base)], types[("overhead", base)]
        values["hv_mv_sizes_mva"] = [t.sn_mva for t in types[("hv_mv_types", base)]]
        default_cable, default_overhead = _equipment().default_line_types(base)
        values["cable_type"] = None if cable.name == default_cable else cable.name
        values["overhead_type"] = None if overhead.name == default_overhead else overhead.name
        params = cls(**values)
        params._types.update(types)
        return params

    def replace(self, **changes: Any) -> "MvParameters":
        """A copy with some fields changed (field names; the values are checked like on construction)."""
        unknown = sorted(set(changes) - set(_FIELD_SPECS))
        if unknown:
            raise ValueError(f"unknown MV parameter field(s) {', '.join(unknown)}")
        new = dataclasses.replace(self, **changes)
        if not _TYPE_FIELDS & set(changes):
            new._types.update(self._types)
        return new

    def replace_raw(self, raw: Mapping[str, Any]) -> "MvParameters":
        """A copy with some parameters changed by YAML key or constant name (the names of ``--set``)."""
        return self.replace(**_fields_from_raw(raw))

    # ------------------------------------------------------------------ nominal voltage
    @property
    def auto_voltage(self) -> bool:
        """``MV_NOMINAL_VOLTAGE_KV`` is ``auto``: each district gets 10 or 20 kV by its installed density."""
        return self.vn_kv == AUTO_VOLTAGE

    @property
    def base_kv(self) -> float:
        """The fixed nominal voltage, or with ``auto`` the 20 kV of the districts below the density threshold."""
        return AUTO_BASE_KV if self.auto_voltage else float(self.vn_kv)

    @property
    def voltages(self) -> tuple[float, ...]:
        """The nominal voltages the districts of the variant can have: ``(20.0, 10.0)`` with ``auto``, else one."""
        return (AUTO_BASE_KV, AUTO_DENSE_KV) if self.auto_voltage else (float(self.vn_kv),)

    def district_kv(self, density_mva_km2: float | None) -> float:
        """The nominal voltage of a district with the given installed density in MVA/km² (DESIGN §4.6).

        With ``auto``: 10 kV when the density is at least ``mv_10kv_min_density_mva_km2``, else 20 kV (also without
        a finite density, e.g. a district without area); otherwise ``MV_NOMINAL_VOLTAGE_KV``.
        """
        if not self.auto_voltage:
            return float(self.vn_kv)
        if density_mva_km2 is not None and math.isfinite(float(density_mva_km2)) \
                and float(density_mva_km2) >= float(self.mv_10kv_min_density_mva_km2):
            return AUTO_DENSE_KV
        return AUTO_BASE_KV

    def _voltage(self, vn_kv: float | None) -> float:
        """``vn_kv`` (None: the base voltage) as one of :attr:`voltages`.

        Raises:
            ValueError: The variant has no districts of that voltage.
        """
        vn = self.base_kv if vn_kv is None else float(vn_kv)
        if vn not in self.voltages:
            raise ValueError(f"MV variant {self.variant_id} has no {vn:g} kV districts (MV_NOMINAL_VOLTAGE_KV "
                             f"{self.vn_kv})")
        return vn

    # ------------------------------------------------------------------ resolved types
    def cable_at(self, vn_kv: float | None = None) -> LineType:
        """The standard MV cable of the districts of nominal voltage ``vn_kv`` (None: :attr:`base_kv`).

        ``MV_STANDARD_CABLE``, or the default cable of the voltage. With ``auto`` a configured type serves both
        voltages: the other voltage takes the same conductor with its insulation for that voltage
        (``equipment.line_type_name_at``).
        """
        vn = self._voltage(vn_kv)
        if ("cable", vn) not in self._types:
            self._types[("cable", vn)] = _line_type(self.cable_type, 0, "cable", "MV_STANDARD_CABLE", vn,
                                                    any_voltage=self.auto_voltage)
        return self._types[("cable", vn)]

    def overhead_at(self, vn_kv: float | None = None) -> LineType:
        """The overhead line type of the districts of nominal voltage ``vn_kv`` (None: :attr:`base_kv`):
        ``MV_OVERHEAD_LINE``, or the default overhead line of the voltage (with ``auto`` as :meth:`cable_at`)."""
        vn = self._voltage(vn_kv)
        if ("overhead", vn) not in self._types:
            self._types[("overhead", vn)] = _line_type(self.overhead_type, 1, "overhead", "MV_OVERHEAD_LINE", vn,
                                                       any_voltage=self.auto_voltage)
        return self._types[("overhead", vn)]

    def hv_mv_types_at(self, vn_kv: float | None = None) -> tuple[HvMvType, ...]:
        """The HV/MV transformer types of ``hv_mv_sizes_mva`` for the districts of nominal voltage ``vn_kv`` (None:
        :attr:`base_kv`; ``110/20 kV`` or ``110/10 kV``, 31.5 MVA interpolated)."""
        vn = self._voltage(vn_kv)
        if ("hv_mv_types", vn) not in self._types:
            try:
                types = tuple(_equipment().hv_mv_types(vn, self.hv_mv_sizes_mva))
            except (LookupError, ValueError, UserWarning) as exc:
                raise ValueError(f"hv_mv_sizes_mva {list(self.hv_mv_sizes_mva)} has no HV/MV transformer types "
                                 f"for {vn:g} kV: {exc}") from exc
            self._types[("hv_mv_types", vn)] = types
        return self._types[("hv_mv_types", vn)]

    @property
    def cable(self) -> LineType:
        """The standard MV cable of the base voltage (:meth:`cable_at`); districts use ``cable_at(vn_kv)``."""
        return self.cable_at()

    @property
    def overhead(self) -> LineType:
        """The overhead line type of the base voltage (:meth:`overhead_at`)."""
        return self.overhead_at()

    @property
    def hv_mv_types(self) -> tuple[HvMvType, ...]:
        """The HV/MV transformer types of the base voltage (:meth:`hv_mv_types_at`)."""
        return self.hv_mv_types_at()

    def check_types(self) -> None:
        """Resolve the cable, overhead line and HV/MV types of every voltage of the variant now.

        Raises:
            ValueError: If a type does not exist or does not fit the nominal voltage (the message names the key).
        """
        for vn in self.voltages:
            _ = self.cable_at(vn), self.overhead_at(vn), self.hv_mv_types_at(vn)

    # ------------------------------------------------------------------ snapshot
    def to_snapshot(self) -> dict:
        """The MV variant snapshot: JSON-native, lower_snake_case keys, resolved types, no NaN.

        Layout: ``schema``, ``mv_stage_version`` and the groups ``voltage``, ``loading``, ``sources``,
        ``routing``, ``settlements``, ``topology``, ``equipment``, ``loads``, ``generation``, ``cases``, ``taps``
        and ``coupling`` (see the ``group`` and ``key`` of each :class:`~pylovo.mv_grid.options.Spec`). The
        variant id and comment are not part of it (they are columns of ``pylovo.mv_variant``). ``equipment``
        holds the resolved types of the base voltage (``cable``, ``overhead``, ``hv_mv_types``) and, with
        ``MV_NOMINAL_VOLTAGE_KV`` auto, those of the 10 kV districts (``cable_10kv``, ``overhead_10kv``,
        ``hv_mv_types_10kv``).
        """
        snapshot: dict[str, Any] = {"schema": SNAPSHOT_SCHEMA, "mv_stage_version": MV_STAGE_VERSION}
        for spec in _SNAPSHOT_SPECS:
            _place(snapshot, spec.path, _json_value(getattr(self, spec.field)))
        for vn in self.voltages:
            suffix = "" if vn == self.base_kv else DENSE_SUFFIX
            for name, value in (("cable_type", _record(self.cable_at(vn))),
                                ("overhead_type", _record(self.overhead_at(vn))),
                                ("hv_mv_sizes_mva", [_record(t) for t in self.hv_mv_types_at(vn)])):
                path = _FIELD_SPECS[name].path
                _place(snapshot, (*path[:-1], path[-1] + suffix), value)
        for path, value in FIXED_RULES.items():
            _place(snapshot, path, value)
        return snapshot


#: The specs of the MvParameters fields by field name.
_FIELD_SPECS: dict[str, Spec] = {spec.field: spec for spec in (*YAML_KEYS.values(), *CONSTANTS.values())
                                 if spec.field}
#: The specs whose field value is written to the snapshot as it is.
_SNAPSHOT_SPECS: tuple[Spec, ...] = tuple(spec for spec in _FIELD_SPECS.values()
                                          if spec.path and spec.field not in RESOLVED_FIELDS)


def _fields_from_raw(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, value in raw.items():
        spec = spec_of(name)
        if not spec.field:
            raise ValueError(f"{name} is an execution flag of pylovo-generate, not an MV parameter")
        fields[spec.field] = value
    return fields


def _equipment():
    """The equipment module (imported on first use: it loads pandapower)."""
    return importlib.import_module("pylovo.mv_grid.equipment")


def _line_type(name: str | None, index: int, kind: str, key: str, vn_kv: float, *,
               any_voltage: bool = False) -> LineType:
    """The resolved line type of a key for a voltage; with ``any_voltage`` (``MV_NOMINAL_VOLTAGE_KV`` auto) a
    configured name of the other voltage stands for its conductor at ``vn_kv`` (``equipment.line_type_name_at``)."""
    equipment = _equipment()
    if name is None:
        name = equipment.default_line_types(vn_kv)[index]
    try:
        line_type = equipment.line_type(equipment.line_type_name_at(name, vn_kv) if any_voltage else name)
        equipment.check_line_type(line_type, vn_kv, kind)
    except (LookupError, ValueError, UserWarning) as exc:   # pandapower raises UserWarning for unknown types
        raise ValueError(f"{key} must name a pandapower {kind} line type for {vn_kv:g} kV, got {name!r}: {exc}"
                         ) from exc
    return line_type


def _record(item: LineType | HvMvType) -> dict[str, Any]:
    """JSON-native record of a resolved type (floats as float, so stored and expected snapshots compare)."""
    record: dict[str, Any] = {}
    for spec_field in dataclasses.fields(item):
        value = getattr(item, spec_field.name)
        if isinstance(value, (bool, str)) or value is None:
            record[spec_field.name] = value
        elif spec_field.type in ("int", int):
            record[spec_field.name] = int(value)
        else:
            record[spec_field.name] = float(value)
    return record


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def _place(snapshot: dict, path: tuple[str, ...], value: Any) -> None:
    node = snapshot
    for key in path[:-1]:
        node = node.setdefault(key, {})
    if path[-1] in node and isinstance(node[path[-1]], dict) and isinstance(value, dict):
        node[path[-1]].update(value)
    else:
        node[path[-1]] = value


def _lookup(snapshot: Mapping[str, Any], path: tuple[str, ...]) -> tuple[bool, Any]:
    node: Any = snapshot
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return False, None
        node = node[key]
    return True, node


# ---------------------------------------------------------------------------------------------- LV version
def _number(value: Any, what: str, version_id: str, *, low: float, high: float | None = None,
            low_open: bool = False) -> float:
    ok = not isinstance(value, bool) and isinstance(value, (int, float)) and np.isfinite(value)
    if ok:
        ok = (value > low if low_open else value >= low) and (high is None or value <= high)
    if not ok:
        raise MvStageError(f"the snapshot of LV version {version_id} has no valid {what}, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class LvVersionParameters:
    """The values of an LV version that the MV stage uses, read from its stored snapshot.

    ``LvVersionParameters(**lv.as_json())`` reproduces ``lv`` (the stage passes ``as_json()`` to its workers).
    """

    version_id: str
    sim_factor: dict[str, float]           # coincidence factor g per consumer category, all of CATEGORIES
    power_factor: float                    # cos phi of the loads
    mv_direct_threshold_kw: float          # MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW of the LV version
    residential_only: bool
    lv_reference_voltage_pu: float | None  # None: no station voltage budgets (flag no_lv_reference)
    use_dso: bool
    use_open: bool
    use_manual: bool
    electrical_backend: str
    # W/m² of the gross floor area per non-residential category (consumer_categories.peak_load_per_m2); empty in
    # the lv_parameters of variants stored before it was read
    peak_load_per_m2: dict[str, float] = field(default_factory=dict)
    # TRANSFORMER_PLANNING_UTILIZATION and TRANSFORMER_MAPPING (settlement type as text -> station ratings in kVA)
    # of the LV version: the capacity check of the stations with station-busbar customers (1.0 and empty in the
    # lv_parameters of variants stored before they were read)
    transformer_planning_utilization: float = 1.0
    transformer_mapping: dict[str, list[float]] = field(default_factory=dict)

    @classmethod
    def from_generation_parameters(cls, version_id: str, gp: dict | str | None) -> "LvVersionParameters":
        """Read the LV values from ``pylovo.version.generation_parameters`` of ``version_id``.

        Raises:
            MvStageError: If the snapshot is missing, has no sim factor for one of the four consumer categories,
                no positive peak load per m² for one of the non-residential categories, no valid power factor or
                MV-direct threshold, or another electrical backend than pandapower.
        """
        if gp is None:
            raise MvStageError(f"LV version {version_id} has no generation parameter snapshot "
                               "(pylovo.version.generation_parameters is empty)")
        if isinstance(gp, str):
            gp = json.loads(gp)
        load = gp.get("load_calculation") or {}
        sim = {str(rec.get("definition")): rec.get("sim_factor") for rec in load.get("consumer_categories") or []
               if isinstance(rec, Mapping)}
        if isinstance(load.get("sim_factor"), Mapping):
            sim.update({str(k): v for k, v in load["sim_factor"].items()})
        missing = [c for c in CATEGORIES if c not in sim]
        if missing:
            raise MvStageError(f"the snapshot of LV version {version_id} has no sim factor for "
                               f"{', '.join(missing)} (load_calculation.sim_factor)")
        sim_factor = {c: _number(sim[c], f"sim factor of {c}", version_id, low=0.0, high=1.0) for c in CATEGORIES}
        per_m2 = {str(rec.get("definition")): rec.get("peak_load_per_m2")
                  for rec in load.get("consumer_categories") or [] if isinstance(rec, Mapping)}
        missing = [c for c in NONRESIDENTIAL_CATEGORIES if per_m2.get(c) is None]
        if missing:
            raise MvStageError(f"the snapshot of LV version {version_id} has no peak load per m² for "
                               f"{', '.join(missing)} (load_calculation.consumer_categories)")
        peak_load_per_m2 = {c: _number(per_m2[c], f"peak load per m² of {c}", version_id, low=0.0, low_open=True)
                            for c in NONRESIDENTIAL_CATEGORIES}
        power_factor = _number(load.get("default_power_factor"), "default power factor", version_id,
                               low=0.0, high=1.0, low_open=True)
        threshold = _number((gp.get("cable_dimensioning") or {}).get("mv_direct_connection_load_threshold_kw"),
                            "MV-direct connection threshold", version_id, low=0.0)
        backend = gp.get("electrical_backend", "pandapower")
        if backend != "pandapower":
            raise MvStageError(f"the MV stage needs LV grids of the pandapower backend; LV version {version_id} "
                               f"uses {backend!r}")
        residential_only = bool(gp.get("residential_only_generation", False))
        if residential_only:
            logger.warning(f"LV version {version_id} was generated from residential buildings only: "
                           "it has no MV customers")
        reference = (gp.get("power_flow_assessment") or {}).get("lv_reference_voltage_pu")
        if reference is not None:
            reference = _number(reference, "LV reference voltage", version_id, low=0.8, high=1.2)
        placement = gp.get("transformer_placement") or {}
        utilization = placement.get("transformer_planning_utilization")
        utilization = 1.0 if utilization is None else _number(utilization, "transformer planning utilization",
                                                                version_id, low=0.0, low_open=True)
        mapping = {str(k): sorted(float(v) for v in (values or []) if isinstance(v, (int, float)) and v > 0)
                   for k, values in (placement.get("transformer_mapping") or {}).items()}
        return cls(
            version_id=str(version_id),
            sim_factor=sim_factor,
            power_factor=power_factor,
            mv_direct_threshold_kw=threshold,
            residential_only=residential_only,
            lv_reference_voltage_pu=reference,
            use_dso=bool(placement.get("use_dso_transformer_positions", False)),
            use_open=bool(placement.get("use_open_transformer_positions", False)),
            use_manual=bool(placement.get("use_manual_transformer_positions", False)),
            electrical_backend=str(backend),
            peak_load_per_m2=peak_load_per_m2,
            transformer_planning_utilization=utilization,
            transformer_mapping={k: v for k, v in mapping.items() if v},
        )

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "LvVersionParameters":
        """The parameters of :meth:`as_json` (as passed to the district workers)."""
        values = dict(data)
        values["sim_factor"] = dict(values["sim_factor"])
        values["peak_load_per_m2"] = dict(values.get("peak_load_per_m2") or {})
        values["transformer_mapping"] = {str(k): [float(x) for x in v]
                                         for k, v in (values.get("transformer_mapping") or {}).items()}
        return cls(**values)

    def station_ratings(self, settlement_type: Any = None) -> list[float]:
        """The catalogue ratings (kVA, ascending) of the LV version for a station of a PLZ settlement type
        (``TRANSFORMER_MAPPING``); an unknown type takes every rating of the catalogue, a version without one
        pylovo's default ratings (:data:`DEFAULT_STATION_RATINGS_KVA`)."""
        key = None
        try:
            if settlement_type is not None and not (isinstance(settlement_type, float) and np.isnan(settlement_type)):
                key = str(int(settlement_type))
        except (TypeError, ValueError):
            key = None
        if key in self.transformer_mapping:
            return sorted(self.transformer_mapping[key])
        every = sorted({float(v) for values in self.transformer_mapping.values() for v in values})
        return every or list(DEFAULT_STATION_RATINGS_KVA)

    def g(self) -> np.ndarray:
        """The sim factors in :data:`~pylovo.mv_grid.model.CATEGORIES` order."""
        return np.array([self.sim_factor[c] for c in CATEGORIES], dtype=float)

    def as_json(self) -> dict:
        """JSON-native dictionary of all fields (stored informationally in ``pylovo.mv_variant.lv_parameters``)."""
        values = dataclasses.asdict(self)
        values["sim_factor"] = {c: float(self.sim_factor[c]) for c in CATEGORIES}
        values["peak_load_per_m2"] = {str(c): float(v) for c, v in self.peak_load_per_m2.items()}
        values["transformer_planning_utilization"] = float(self.transformer_planning_utilization)
        values["transformer_mapping"] = {str(k): [float(x) for x in v] for k, v in self.transformer_mapping.items()}
        return values
