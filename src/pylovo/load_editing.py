"""Manual load edits of generated grids.

A user can correct the load inputs of one building of a stored grid (number of households,
residential and non-residential floor area, non-residential use). The edit recomputes what
generation derived from these inputs, with the parameters stored for the grid's version in
``pylovo.version.generation_parameters`` (never today's config):

1. the component peaks of the building (``set_building_peak_load`` and
   ``update_too_large_consumers_to_zero`` of :class:`~pylovo.database.preprocessing_mixin.PreprocessingMixin`),
2. the power-flow snapshot loads of the whole grid: simultaneity is grouped per category over the
   grid (:func:`pylovo.utils.allocate_consumer_simultaneous_loads`), so one household more changes
   every residential load of the grid,
3. the validation power flow at the transformer-coincident operating point
   (``GridGenerator.save_net``).

Cables, the transformer and the design diagnostics of ``grid_result`` stay as generated: an edit
re-validates the grid, it does not re-plan it. Regenerating the PLZ as a new version does that.

Two parts of generation are replicated here instead of being refactored, because
``grid_generator.py`` and ``cable_installer.py`` are shared with other development branches:

* :func:`build_load_specs` repeats the ``LoadSpec`` arithmetic of
  ``CableInstaller.create_consumer_bus_and_load`` character for character (the power factor is
  passed in instead of read from the config),
* :func:`validate_operating_point` repeats the classification and the voltage-drop diagnostics of
  ``GridGenerator.save_net`` (the voltage band is passed in).

The guards make this safe: an edit is refused unless the stored loads of the grid are reproduced
from ``buildings_result`` and the version snapshot (:func:`compare_loads`), and unless the network
JSON agrees with the ``pandapower_*`` SQL tables (:func:`check_storage_consistency`). The tests in
``api/tests/test_load_editing_*.py`` check both replications against generation.

:class:`LoadEditor` combines these functions with the queries of
:class:`~pylovo.database.load_edit_mixin.LoadEditMixin`: context, preview, apply (one short write
transaction with an audit row in ``pylovo.load_edit``), exact undo and undo of all edits of a grid.
"""
from __future__ import annotations

import contextlib
import copy
import getpass
import json
import logging
import math
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from pylovo import utils
from pylovo.electrical_backend.core.specs import LoadSpec

logger = logging.getLogger(__name__)

EDITABLE_FIELDS = ("households", "residential_floor_area", "nonresidential_floor_area", "nonresidential_use")
NONRESIDENTIAL_USES = ("Commercial", "Public")
OPERATING_POINT_BASIS = "synthetic_transformer_coincident_proportional"

MAX_HOUSEHOLDS = 5000
MAX_FLOOR_AREA_M2 = 1e6
MAX_AREA_FACTOR = 10.0          # an area may be at most 10 x the gross floor area
AREA_DECIMALS = 2               # user input is rounded to 0.01 m2
PEAK_REL_TOL = 1e-12            # reproduction: component peaks and loads
LOAD_ABS_TOL_MW = 1e-15
STORAGE_TOL_MW = 1e-12          # JSON (15 decimals) versus SQL
PF_BASELINE_TOL_PU = 1e-9       # recomputed versus stored validation power flow
CHANGED_LOAD_TOL_KW = 1e-9      # loads that moved less are not listed as changed

_BUILDING_INPUTS = EDITABLE_FIELDS
_BUILDING_PEAKS = ("residential_peak_load_in_kw", "nonresidential_peak_load_in_kw", "nonresidential_mv_direct",
                   "peak_load_in_kw")
_QUIET = logging.getLogger("pylovo.load_editing.backend")
_QUIET.setLevel(logging.CRITICAL)


# --------------------------------------------------------------------------- errors
class LoadEditError(Exception):
    """Base class of load-edit errors; ``status`` is the matching HTTP status code.

    Args:
        code: Machine-readable reason, e.g. ``reproduction_drift``.
        message: Text for the user.
        **detail: Additional JSON-serialisable details.
    """

    status = 400

    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.detail}


class LoadEditNotFound(LoadEditError):
    """The grid, building or audit row does not exist."""

    status = 404


class LoadEditValidationError(LoadEditError):
    """The requested change is invalid (``detail['errors']`` lists the field errors)."""

    status = 422


class LoadEditConflict(LoadEditError):
    """The edit cannot be done now or at all (locked, stale etag, not editable, guard failed, ...)."""

    status = 409


# --------------------------------------------------------------------------- parameters
@dataclass(frozen=True)
class LoadParameters:
    """Load parameters of one version, taken from its ``generation_parameters`` snapshot.

    Attributes:
        version_id: Version the parameters belong to.
        residential_peak_kw: kW per household, from the ``Residential`` consumer category record
            (what ``set_building_peak_load`` multiplies with).
        peak_load_household_kw: ``PEAK_LOAD_HOUSEHOLD`` of the snapshot (for display).
        peak_load_per_m2_w: W/m2 of the non-residential uses that have a value.
        sim_factor: Simultaneity factor per consumer category.
        power_factor: ``DEFAULT_POWER_FACTOR``.
        mv_direct_threshold_kw: Non-residential components above this are MV-direct.
        residential_only: ``RESIDENTIAL_ONLY_GENERATION`` of the version.
        vn_v: Nominal LV voltage in V.
        max_service_drop_percent: Service design voltage-drop limit (warnings only).
        min_vm_pu: Lower voltage limit of the validation power flow (``None``: from the net).
        max_vm_pu: Upper voltage limit (``None``: from the net).
        planning_utilization: ``TRANSFORMER_PLANNING_UTILIZATION``.
        notes: Remarks about how the values were derived.
    """

    version_id: str
    residential_peak_kw: float
    peak_load_household_kw: float | None
    peak_load_per_m2_w: dict[str, float]
    sim_factor: dict[str, float]
    power_factor: float
    mv_direct_threshold_kw: float
    residential_only: bool
    vn_v: float
    max_service_drop_percent: float | None
    min_vm_pu: float | None
    max_vm_pu: float | None
    planning_utilization: float
    notes: tuple[str, ...] = ()

    @classmethod
    def from_generation_parameters(cls, version_id: str, gp: dict | None) -> LoadParameters:
        """Read the parameters from a version snapshot.

        Args:
            version_id: Version ID.
            gp: ``pylovo.version.generation_parameters`` (parsed JSON).

        Returns:
            The parameters.

        Raises:
            LoadEditConflict: ``not_editable`` if the snapshot is missing required values or the
                version does not use the pandapower backend.
        """
        reasons: list[str] = []
        notes: list[str] = []
        if not gp:
            raise LoadEditConflict("not_editable", f"Version {version_id} has no stored generation parameters.",
                                   reasons=["generation_parameters is NULL"])
        backend = gp.get("electrical_backend")
        if backend not in (None, "pandapower"):
            reasons.append(f"electrical backend {backend!r}: only pandapower grids can be edited")
        lc = gp.get("load_calculation") or {}
        cd = gp.get("cable_dimensioning") or {}
        pfa = gp.get("power_flow_assessment") or {}
        records = {r.get("definition"): r for r in (lc.get("consumer_categories") or []) if isinstance(r, dict)}
        residential = records.get("Residential") or {}
        p_res = residential.get("peak_load")
        household = lc.get("peak_load_household")
        if p_res is None:
            reasons.append("the Residential consumer category has no peak_load")
            p_res = float("nan")
        elif household is not None and abs(float(p_res) - float(household)) <= 1e-10 * abs(float(p_res)):
            # The records went through pandas to_json (10 decimals); the scalar is exact.
            p_res = float(household)
        elif household is not None:
            notes.append(f"kW per household {p_res} comes from the Residential category record "
                         f"(PEAK_LOAD_HOUSEHOLD is {household})")
        per_m2 = {use: float(records[use]["peak_load_per_m2"]) for use in NONRESIDENTIAL_USES
                  if use in records and records[use].get("peak_load_per_m2") is not None}
        sim_factor = {str(k): float(v) for k, v in (lc.get("sim_factor") or {}).items()}
        for name, record in records.items():
            if name not in sim_factor and record.get("sim_factor") is not None:
                sim_factor[name] = float(record["sim_factor"])
        for name in ("Residential", *per_m2):
            if name not in sim_factor:
                reasons.append(f"no simultaneity factor for {name}")
        power_factor = lc.get("default_power_factor")
        if power_factor is None:
            reasons.append("default_power_factor is missing")
        threshold = cd.get("mv_direct_connection_load_threshold_kw")
        if threshold is None:
            reasons.append("mv_direct_connection_load_threshold_kw is missing")
        vn = cd.get("vn")
        if vn is None:
            reasons.append("cable_dimensioning.vn is missing")
        if "residential_only_generation" not in gp:
            notes.append("residential_only_generation is not in the snapshot; assumed false")
        if reasons:
            raise LoadEditConflict("not_editable", f"Grids of version {version_id} cannot be edited: " + "; ".join(reasons),
                                   reasons=reasons)
        placement = gp.get("transformer_placement") or {}
        return cls(
            version_id=str(version_id),
            residential_peak_kw=float(p_res),
            peak_load_household_kw=None if household is None else float(household),
            peak_load_per_m2_w=per_m2,
            sim_factor=sim_factor,
            power_factor=float(power_factor),
            mv_direct_threshold_kw=float(threshold),
            residential_only=bool(gp.get("residential_only_generation", False)),
            vn_v=float(vn),
            max_service_drop_percent=cd.get("max_service_design_voltage_drop_percent"),
            min_vm_pu=pfa.get("min_vm_pu"),
            max_vm_pu=pfa.get("max_vm_pu"),
            planning_utilization=float(placement.get("transformer_planning_utilization") or 1.0),
            notes=tuple(notes),
        )

    @property
    def include_nonresidential(self) -> bool:
        """Whether non-residential components count in ``peak_load_in_kw`` (not residential-only)."""
        return not self.residential_only

    def consumer_categories_df(self) -> pd.DataFrame:
        """The consumer categories shaped like ``UtilsMixin.get_consumer_categories``."""
        names = sorted(set(self.sim_factor) | {"Residential", *self.peak_load_per_m2_w})
        rows = [{"definition": name,
                 "peak_load": self.residential_peak_kw if name == "Residential" else None,
                 "peak_load_per_m2": self.peak_load_per_m2_w.get(name),
                 "sim_factor": self.sim_factor.get(name)} for name in names]
        df = pd.DataFrame(rows)
        df.set_index("definition", drop=False, inplace=True)
        df.sort_index(inplace=True)
        return df

    def as_json(self) -> dict[str, Any]:
        """JSON-serialisable form (stored with every audit row)."""
        data = asdict(self)
        data["notes"] = list(self.notes)
        return data


# --------------------------------------------------------------------------- building peaks
@dataclass(frozen=True)
class BuildingInputs:
    """The editable load inputs of a building (``buildings_result`` columns)."""

    households: int | None
    residential_floor_area: float | None
    nonresidential_floor_area: float | None
    nonresidential_use: str | None

    @classmethod
    def from_row(cls, row: dict) -> BuildingInputs:
        values = {k: _py(row.get(k)) for k in _BUILDING_INPUTS}
        if isinstance(values["households"], float) and values["households"].is_integer():
            values["households"] = int(values["households"])  # pandas stores integer columns with NULLs as float
        for key in ("residential_floor_area", "nonresidential_floor_area"):
            if values[key] is not None:
                values[key] = float(values[key])
        return cls(**values)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ComponentPeaks:
    """Peak loads derived from :class:`BuildingInputs` (``buildings_result`` columns)."""

    residential_peak_load_in_kw: float
    nonresidential_peak_load_in_kw: float
    nonresidential_mv_direct: bool
    peak_load_in_kw: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def component_peaks(inputs: BuildingInputs, params: LoadParameters) -> ComponentPeaks:
    """Peak loads of one building, as ``set_building_peak_load`` and ``update_too_large_consumers_to_zero``.

    The float operations are the ones of the SQL: ``households * kW`` and
    ``(area * W/m2) / 1000``. Non-residential kW is computed in residential-only versions too
    (it then counts in the LV loads, but not in ``peak_load_in_kw``).

    Args:
        inputs: Building inputs.
        params: Version parameters.

    Returns:
        The component peaks; ``peak_load_in_kw == 0`` means the building is in no LV grid.
    """
    res_fa = inputs.residential_floor_area or 0
    nonres_fa = inputs.nonresidential_floor_area or 0
    residential_kw = 0.0
    if res_fa > 0:
        if inputs.households is None:
            raise ValueError("households are required when the residential floor area is > 0")
        residential_kw = inputs.households * params.residential_peak_kw
    nonresidential_kw = 0.0
    if nonres_fa > 0:
        if inputs.nonresidential_use not in params.peak_load_per_m2_w:
            raise ValueError(f"no W/m2 for non-residential use {inputs.nonresidential_use!r}")
        nonresidential_kw = nonres_fa * params.peak_load_per_m2_w[inputs.nonresidential_use] / 1000
    include = params.include_nonresidential
    threshold = params.mv_direct_threshold_kw
    mv_direct = include and nonresidential_kw > threshold
    peak = residential_kw + (nonresidential_kw if include and nonresidential_kw <= threshold else 0)
    return ComponentPeaks(float(residential_kw), float(nonresidential_kw), bool(mv_direct), float(peak))


def _stored_peaks(building: dict) -> ComponentPeaks:
    return ComponentPeaks(float(_py(building.get("residential_peak_load_in_kw")) or 0.0),
                          float(_py(building.get("nonresidential_peak_load_in_kw")) or 0.0),
                          bool(_py(building.get("nonresidential_mv_direct"))),
                          float(_py(building.get("peak_load_in_kw")) or 0.0))


def gross_floor_area(building: dict) -> float | None:
    """``floor_area * COALESCE(floor_number, 1)`` of a building row."""
    area = building.get("floor_area")
    if area is None:
        return None
    return float(area) * (building.get("floor_number") or 1)


# --------------------------------------------------------------------------- validation
@dataclass
class ChangeCheck:
    """Result of :func:`validate_changes`."""

    inputs: BuildingInputs
    peaks: ComponentPeaks | None
    changes: dict[str, list]                      # field -> [old, new]
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


def _issue(code: str, message: str, field_name: str | None = None, severity: str = "warning", **detail) -> dict:
    out = {"code": code, "message": message, "severity": severity}
    if field_name:
        out["field"] = field_name
    if detail:
        out["detail"] = detail
    return out


def validate_changes(building: dict, changes: dict, params: LoadParameters,
                     original: BuildingInputs | None = None) -> ChangeCheck:
    """Check requested changes of one building and compute its new peaks.

    The server is authoritative for these rules (the UI mirrors them for instant feedback).
    Areas that are sent are rounded to 0.01 m2; unchanged stored values are never re-rounded.

    Args:
        building: ``buildings_result`` row (inputs, peaks, ``type``, ``floor_area``, ``floor_number``).
        changes: Subset of :data:`EDITABLE_FIELDS` with the new values.
        params: Version parameters.
        original: Generated inputs (for plausibility warnings), if the building was edited before.

    Returns:
        The new inputs and peaks, the effective changes, errors and warnings.
    """
    current = BuildingInputs.from_row(building)
    values = current.as_dict()
    errors: list[dict] = []
    warnings: list[dict] = []
    unknown = sorted(set(changes) - set(EDITABLE_FIELDS))
    for name in unknown:
        errors.append(_issue("unknown_field", f"{name} cannot be edited", name, "error"))
    gross = gross_floor_area(building)
    area_limit = min(MAX_FLOOR_AREA_M2, MAX_AREA_FACTOR * gross) if gross else MAX_FLOOR_AREA_M2
    for name in ("residential_floor_area", "nonresidential_floor_area"):
        if name not in changes:
            continue
        value = changes[name]
        if value is None:
            value = 0.0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            errors.append(_issue("invalid_number", "Enter a finite number", name, "error"))
            continue
        value = round(float(value), AREA_DECIMALS)
        if value < 0:
            errors.append(_issue("negative_area", "The area cannot be negative", name, "error"))
        elif value > area_limit:
            errors.append(_issue("area_too_large", f"At most {area_limit:,.0f} m² (10 × the gross floor area)", name, "error"))
        if values[name] is not None and round(float(values[name]), AREA_DECIMALS) == value:
            continue  # unchanged within the input precision: keep the stored value exactly
        values[name] = value
    if "nonresidential_use" in changes:
        values["nonresidential_use"] = changes["nonresidential_use"] or None
    res_fa = values["residential_floor_area"] or 0
    nonres_fa = values["nonresidential_floor_area"] or 0
    if nonres_fa <= 0:
        values["nonresidential_use"] = None
    elif values["nonresidential_use"] not in params.peak_load_per_m2_w:
        allowed = ", ".join(params.peak_load_per_m2_w) or "none"
        errors.append(_issue("invalid_use", f"Choose the non-residential use ({allowed})", "nonresidential_use", "error"))
    if "households" in changes:
        households = changes["households"]
        if res_fa <= 0:
            if households != current.households:
                errors.append(_issue("households_without_area", "Households need a residential floor area > 0",
                                     "households", "error"))
        elif isinstance(households, bool) or not isinstance(households, int):
            errors.append(_issue("invalid_households", "Households must be a whole number", "households", "error"))
        elif not 1 <= households <= MAX_HOUSEHOLDS:
            errors.append(_issue("households_range", f"Households must be between 1 and {MAX_HOUSEHOLDS}",
                                 "households", "error"))
        else:
            values["households"] = int(households)
    if res_fa > 0 and not values["households"]:
        errors.append(_issue("households_required", "Households are required when the residential floor area is > 0",
                             "households", "error"))
    new = BuildingInputs(**values)
    effective = {k: [getattr(current, k), getattr(new, k)] for k in EDITABLE_FIELDS
                 if getattr(current, k) != getattr(new, k)}
    peaks = None
    if not errors:
        peaks = component_peaks(new, params)
        if not effective:
            errors.append(_issue("no_change", "Nothing changed", None, "error"))
        elif peaks.peak_load_in_kw == 0:
            errors.append(_issue("no_lv_load", "The building would leave the LV grid (no LV load left). "
                                 "Regenerate the PLZ as a new version instead.", None, "error"))
    if peaks is not None and not errors:
        old_peaks = _stored_peaks(building)
        if gross is not None and (abs(res_fa + nonres_fa - gross) > 0.01 or nonres_fa > gross):
            warnings.append(_issue("split_mismatch", f"Residential + non-residential area ({res_fa + nonres_fa:,.2f} m²) differs "
                                   f"from the gross floor area ({gross:,.2f} m²). pylovo's input check would reject this split "
                                   "when the PLZ is regenerated."))
        if params.include_nonresidential and peaks.nonresidential_mv_direct != bool(old_peaks.nonresidential_mv_direct):
            warnings.append(_issue("mv_direct_flip", "The non-residential part is now "
                                   + ("above" if peaks.nonresidential_mv_direct else "below")
                                   + f" the MV-direct threshold of {params.mv_direct_threshold_kw:g} kW, so it "
                                   + ("leaves" if peaks.nonresidential_mv_direct else "joins") + " the LV grid."))
        if params.residential_only and peaks.nonresidential_peak_load_in_kw > 0 and (
                "nonresidential_floor_area" in effective or "nonresidential_use" in effective):
            warnings.append(_issue("residential_only_nonres", "Residential-only version: the non-residential part still enters "
                                   "the LV loads (as in generation) but not peak_load_in_kw."))
        households = new.households or 0
        if res_fa > 0 and households:
            per_household = res_fa / households
            if per_household < 25 or per_household > 400:
                warnings.append(_issue("plausibility", f"{per_household:,.0f} m² per household is unusual (25–400 m²)."))
        if building.get("type") in ("SFH", "TH") and households > 2:
            warnings.append(_issue("plausibility", f"{households} households in a {building.get('type')} building."))
        base = (original or current).households
        if base and households > 3 * base:
            warnings.append(_issue("plausibility", f"More than 3 × the generated {base} households."))
    return ChangeCheck(new, peaks, effective, errors, warnings)


# --------------------------------------------------------------------------- snapshot loads
BUILDING_FRAME_COLUMNS = ("objectid", "vertice_id", "households", "residential_floor_area", "nonresidential_floor_area",
                          "nonresidential_use", "residential_peak_load_in_kw", "nonresidential_peak_load_in_kw",
                          "nonresidential_mv_direct", "peak_load_in_kw")


def buildings_frame(buildings: Iterable[dict]) -> pd.DataFrame:
    """Building rows as generation sees them in ``install_cables`` (indexed by ``vertice_id``).

    ``get_buildings_from_bcid`` reads the rows without ORDER BY and sorts them with pandas'
    unstable default sort, so buildings that share a vertex come in arbitrary order. The rows
    are sorted stably by ``(vertice_id, objectid)`` here; :func:`compare_loads` accepts the
    floating-point differences that the order can cause.
    """
    df = pd.DataFrame(list(buildings))
    if df.empty:
        df = pd.DataFrame(columns=list(BUILDING_FRAME_COLUMNS))
    if df["vertice_id"].isna().any():
        raise ValueError("buildings without vertice_id cannot be part of a grid")
    df["vertice_id"] = df["vertice_id"].astype("int64")
    df = df.sort_values(["vertice_id", "objectid"], kind="stable")
    df.set_index("vertice_id", drop=False, inplace=True)
    return df


@dataclass
class LoadTable:
    """Power-flow snapshot loads of a grid (the ``net.load`` generation creates)."""

    specs: list[LoadSpec]
    zones: dict[int, str]                           # consumer vertex -> bus zone
    categories: dict[str, dict[str, float]]         # category -> units, installed_kw, simultaneous_kw, utilisation

    @property
    def rows(self) -> list[dict]:
        """The loads as ``pandapower_load`` values (the arithmetic of ``PandapowerBackend._create_load``)."""
        return [{"name": s.name, "bus_name": s.bus, "p_mw": s.kw / 1000.0, "q_mvar": s.kvar / 1000.0,
                 "max_p_mw": s.max_p_mw, "service_design_p_mw": s.service_design_p_mw, "category": s.category,
                 "load_units": s.load_units, "consumer_vertex": s.consumer_vertex} for s in self.specs]


def build_load_specs(consumer_list: list, powerflow_snapshot_components: dict,
                     power_factor: float) -> tuple[list[LoadSpec], dict[int, str]]:
    """Load specs and bus zones of the consumer vertices, as ``CableInstaller.create_consumer_bus_and_load``.

    The expressions are the ones of the installer; only ``DEFAULT_POWER_FACTOR`` is replaced by the
    version's ``power_factor``.

    Args:
        consumer_list: Consumer vertices in generation order.
        powerflow_snapshot_components: Output of :func:`pylovo.utils.allocate_consumer_simultaneous_loads`.
        power_factor: Power factor of the version.

    Returns:
        ``(specs, zones)``: the load specs in creation order and the zone of each consumer bus.

    Raises:
        ValueError: If a consumer has no LV load component.
    """
    specs: list[LoadSpec] = []
    zones: dict[int, str] = {}
    for consumer in consumer_list:
        components = powerflow_snapshot_components.get(consumer, [])
        if not components:
            raise ValueError(f"Consumer vertex {consumer} has no LV load components.")
        categories = [component["category"] for component in components]
        load_type = categories[0] if len(categories) == 1 else "Mixed"
        zones[int(consumer)] = load_type
        for component in components:
            simultaneous_load_kw = float(component["simultaneous_kw"])
            phi = np.arccos(power_factor)
            kvar = simultaneous_load_kw * np.tan(phi)
            load_spec = LoadSpec(
                name=f"Load {consumer} {component['category']}",
                bus=f"Consumer Nodebus {consumer}",
                kw=simultaneous_load_kw,
                kvar=kvar,
                max_p_mw=float(component["installed_kw"]) * 1e-3,
                service_design_p_mw=float(component["service_design_kw"]) * 1e-3,
                operating_point_basis=OPERATING_POINT_BASIS,
                category=component["category"],
                load_units=float(component["load_units"]),
                consumer_vertex=int(consumer),
            )
            specs.append(load_spec)
    return specs, zones


def snapshot_loads(buildings: pd.DataFrame, params: LoadParameters) -> LoadTable:
    """Recompute the power-flow snapshot loads of a grid from its buildings.

    Args:
        buildings: Output of :func:`buildings_frame`.
        params: Version parameters.

    Returns:
        The loads, the consumer bus zones and per category the simultaneity numbers.
    """
    consumer_list = list(dict.fromkeys(buildings["vertice_id"].to_list()))
    consumer_df = params.consumer_categories_df()
    _, components = utils.allocate_consumer_simultaneous_loads(consumer_list, buildings, consumer_df)
    specs, zones = build_load_specs(consumer_list, components, params.power_factor)
    categories: dict[str, dict[str, float]] = {}
    for comps in components.values():
        for c in comps:
            entry = categories.setdefault(c["category"], {"units": 0.0, "installed_kw": 0.0, "simultaneous_kw": 0.0})
            entry["units"] += float(c["load_units"])
            entry["installed_kw"] += float(c["installed_kw"])
            entry["simultaneous_kw"] += float(c["simultaneous_kw"])
    for name, entry in categories.items():
        entry["loads"] = sum(1 for s in specs if s.category == name)
        entry["utilisation"] = entry["simultaneous_kw"] / entry["installed_kw"] if entry["installed_kw"] else 0.0
        entry["sim_factor"] = params.sim_factor.get(name)
    return LoadTable(specs, zones, categories)


# --------------------------------------------------------------------------- guards
@dataclass
class Reproduction:
    """Outcome of :func:`compare_loads` / :func:`check_building_peaks`."""

    tier: str                                       # exact | tolerance | drift
    max_rel_diff: float = 0.0
    max_abs_diff_mw: float = 0.0
    loads_checked: int = 0
    buildings_checked: int = 0
    load_mismatches: list[str] = field(default_factory=list)
    peak_mismatches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "load_mismatches": self.load_mismatches[:10], "peak_mismatches": self.peak_mismatches[:10]}


def _rel(a: float, b: float) -> float:
    if a == b:
        return 0.0
    return abs(a - b) / max(abs(a), abs(b))


def check_building_peaks(buildings: pd.DataFrame, params: LoadParameters) -> tuple[list[str], float]:
    """Recompute the component peaks of every building and compare them with the stored ones.

    Returns:
        ``(mismatches, max_rel_diff)``.
    """
    mismatches: list[str] = []
    max_rel = 0.0
    for row in buildings.to_dict("records"):
        try:
            peaks = component_peaks(BuildingInputs.from_row(row), params)
        except ValueError as exc:
            mismatches.append(f"{row['objectid']}: {exc}")
            continue
        if peaks.nonresidential_mv_direct != bool(row.get("nonresidential_mv_direct")):
            mismatches.append(f"{row['objectid']}: MV-direct flag differs")
        for key in ("residential_peak_load_in_kw", "nonresidential_peak_load_in_kw", "peak_load_in_kw"):
            stored = float(row.get(key) or 0.0)
            diff = _rel(getattr(peaks, key), stored)
            max_rel = max(max_rel, diff)
            if diff > PEAK_REL_TOL:
                mismatches.append(f"{row['objectid']}: {key} {getattr(peaks, key)!r} != stored {stored!r}")
    return mismatches, max_rel


def compare_loads(expected: LoadTable, stored: list[dict], bus_index: dict[str, int] | None = None) -> Reproduction:
    """Compare recomputed loads with the stored ``pandapower_load`` rows (ordered by ``pp_index``).

    The structure (count, order, names, categories, vertices, household units, buses) must be
    identical. The numbers are ``exact``, within ``tolerance`` (rel 1e-12 or abs 1e-15 MW, the
    float differences that the building order can cause) or ``drift``.

    Args:
        expected: Output of :func:`snapshot_loads`.
        stored: ``pandapower_load`` rows.
        bus_index: Optional consumer bus name -> pp_index map to check the ``bus`` column.
    """
    rows = expected.rows
    rep = Reproduction("exact", loads_checked=len(stored))
    if len(rows) != len(stored):
        rep.load_mismatches.append(f"{len(stored)} stored loads, {len(rows)} recomputed")
    for i, (new, old) in enumerate(zip(rows, stored)):
        if old.get("pp_index") is not None and int(old["pp_index"]) != i:
            rep.load_mismatches.append(f"pp_index {old['pp_index']} at position {i}")
        for key in ("name", "category"):
            if new[key] != old.get(key):
                rep.load_mismatches.append(f"load {i}: {key} {old.get(key)!r} != {new[key]!r}")
        if old.get("consumer_vertex") is None or int(old["consumer_vertex"]) != new["consumer_vertex"]:
            rep.load_mismatches.append(f"load {i}: consumer_vertex {old.get('consumer_vertex')!r} != {new['consumer_vertex']}")
        if old.get("load_units") is None or float(old["load_units"]) != new["load_units"]:
            rep.load_mismatches.append(f"load {i} ({new['name']}): load_units {old.get('load_units')!r} != {new['load_units']!r}")
        if bus_index is not None and bus_index.get(new["bus_name"]) != old.get("bus"):
            rep.load_mismatches.append(f"load {i} ({new['name']}): bus {old.get('bus')!r} is not {new['bus_name']}")
        for key in ("p_mw", "q_mvar", "max_p_mw", "service_design_p_mw"):
            a, b = float(new[key]), float(old.get(key) if old.get(key) is not None else math.nan)
            if a == b:
                continue
            diff_abs = abs(a - b)
            diff_rel = _rel(a, b) if math.isfinite(diff_abs) else math.inf
            rep.max_rel_diff = max(rep.max_rel_diff, diff_rel)
            rep.max_abs_diff_mw = max(rep.max_abs_diff_mw, diff_abs)
            if diff_rel <= PEAK_REL_TOL or diff_abs <= LOAD_ABS_TOL_MW:
                if rep.tier == "exact":
                    rep.tier = "tolerance"
            else:
                rep.load_mismatches.append(f"load {i} ({new['name']}): {key} {b!r} stored, {a!r} recomputed")
    if rep.load_mismatches:
        rep.tier = "drift"
    return rep


def check_storage_consistency(net, sql_loads: list[dict], sql_buses: list[dict],
                              sql_counts: dict[str, int] | None = None) -> list[str]:
    """Compare the loads and consumer buses of the network JSON with the ``pandapower_*`` SQL tables.

    ``save_net`` writes the SQL tables best-effort after the JSON, and notebooks may change the
    JSON, so both must agree before one of them is rewritten.

    Args:
        net: pandapower network parsed from ``grid_result.grid``.
        sql_loads: ``pandapower_load`` rows ordered by ``pp_index``.
        sql_buses: ``pandapower_bus`` rows (at least the consumer buses) with ``pp_index`` and ``name``.
        sql_counts: Optional row counts of ``bus``, ``line`` and ``trafo``.

    Returns:
        Mismatches (empty if consistent).
    """
    out: list[str] = []
    load = net.load
    if len(load) != len(sql_loads):
        out.append(f"{len(load)} loads in the JSON, {len(sql_loads)} in pandapower_load")
    if list(load.index) != list(range(len(load))):
        out.append("the JSON load index is not 0..n-1")
    for (idx, row), sql in zip(load.iterrows(), sql_loads):
        if int(sql["pp_index"]) != int(idx):
            out.append(f"load {idx}: pp_index {sql['pp_index']}")
        for key in ("name", "category"):
            if _py(row.get(key)) != sql.get(key):
                out.append(f"load {idx}: {key} {row.get(key)!r} (JSON) != {sql.get(key)!r} (SQL)")
        for key in ("consumer_vertex", "bus"):
            a, b = _py(row.get(key)), sql.get(key)
            if a is None or b is None or int(a) != int(b):
                out.append(f"load {idx}: {key} {a!r} (JSON) != {b!r} (SQL)")
        if _py(row.get("load_units")) != (None if sql.get("load_units") is None else float(sql["load_units"])):
            out.append(f"load {idx}: load_units differ")
        for key in ("p_mw", "q_mvar", "max_p_mw", "service_design_p_mw"):
            a, b = _py(row.get(key)), sql.get(key)
            if a is None or b is None or not abs(float(a) - float(b)) <= STORAGE_TOL_MW:
                out.append(f"load {idx}: {key} {a!r} (JSON) != {b!r} (SQL)")
    sql_by_index = {int(b["pp_index"]): b.get("name") for b in sql_buses}
    for idx, name in net.bus["name"].items():
        if isinstance(name, str) and name.startswith("Consumer Nodebus ") and sql_by_index.get(int(idx)) != name:
                out.append(f"bus {idx} {name!r}: SQL has {sql_by_index.get(int(idx))!r}")
    if sql_counts:
        for element in ("bus", "line", "trafo"):
            if element in sql_counts and int(sql_counts[element]) != len(net[element]):
                out.append(f"{len(net[element])} {element} rows in the JSON, {sql_counts[element]} in pandapower_{element}")
    return out


# --------------------------------------------------------------------------- power flow
@dataclass
class Validation:
    """Outcome of the validation power flow (the ``grid_result`` columns written by ``save_net``)."""

    status: str
    min_voltage_pu: float | None = None
    max_voltage_pu: float | None = None
    max_feeder_voltage_drop_pu: float | None = None
    max_service_voltage_drop_pu: float | None = None
    max_total_lv_voltage_drop_pu: float | None = None
    error: str | None = None

    @property
    def drops(self) -> dict[str, float | None]:
        return {"max_feeder_voltage_drop_pu": self.max_feeder_voltage_drop_pu,
                "max_service_voltage_drop_pu": self.max_service_voltage_drop_pu,
                "max_total_lv_voltage_drop_pu": self.max_total_lv_voltage_drop_pu}


def validate_operating_point(backend, min_vm_pu: float, max_vm_pu: float) -> Validation:
    """Run and classify the validation power flow, as ``GridGenerator.save_net``.

    Status ``converged``, ``voltage_violation`` (a bus outside ``[min_vm_pu, max_vm_pu]``) or
    ``not_converged`` (also when the solver raises). The voltage drops are measured over the
    service lines (to ``Consumer Nodebus`` buses) relative to ``LVbus 1``.

    Args:
        backend: :class:`~pylovo.electrical_backend.pandapower.backend.PandapowerBackend` holding the net.
        min_vm_pu: Lower voltage limit (``POWER_FLOW_MIN_VM_PU`` of the version).
        max_vm_pu: Upper voltage limit.
    """
    result = Validation("not_converged")
    try:
        converged = backend.solve_power_flow()
        if converged:
            metrics = backend.get_circuit_metrics()
            min_voltage_pu = metrics.get("min_voltage_pu")
            max_voltage_pu = metrics.get("max_voltage_pu")
            result.min_voltage_pu, result.max_voltage_pu = min_voltage_pu, max_voltage_pu

            if getattr(backend, "net", None) is not None:
                net = backend.net
                lv_buses = net.bus.index[net.bus.name == "LVbus 1"]
                consumer_buses = set(
                    net.bus.index[
                        net.bus.name.fillna("").str.startswith("Consumer Nodebus ")
                    ]
                )
                service_lines = net.line.loc[net.line.to_bus.isin(consumer_buses)]
                if len(lv_buses) == 1 and not service_lines.empty:
                    lv_voltage_pu = float(net.res_bus.at[int(lv_buses[0]), "vm_pu"])
                    feeder_drops = []
                    service_drops = []
                    total_drops = []
                    for line in service_lines.itertuples():
                        connection_voltage_pu = float(net.res_bus.at[line.from_bus, "vm_pu"])
                        consumer_voltage_pu = float(net.res_bus.at[line.to_bus, "vm_pu"])
                        feeder_drops.append(lv_voltage_pu - connection_voltage_pu)
                        service_drops.append(connection_voltage_pu - consumer_voltage_pu)
                        total_drops.append(lv_voltage_pu - consumer_voltage_pu)
                    result.max_feeder_voltage_drop_pu = max(feeder_drops)
                    result.max_service_voltage_drop_pu = max(service_drops)
                    result.max_total_lv_voltage_drop_pu = max(total_drops)

            voltage_out_of_band = False
            if min_voltage_pu is not None and min_voltage_pu < min_vm_pu:
                voltage_out_of_band = True
            if max_voltage_pu is not None and max_voltage_pu > max_vm_pu:
                voltage_out_of_band = True
            result.status = "voltage_violation" if voltage_out_of_band else "converged"
    except Exception as exc:  # noqa: BLE001 - save_net stores such grids as not_converged
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def _backend_for(net):
    from pylovo.electrical_backend.pandapower.backend import PandapowerBackend

    backend = PandapowerBackend(logger=_QUIET)
    backend.net = net
    return backend


_EMPTY_LOAD: pd.DataFrame | None = None


def replace_net_loads(net, table: LoadTable) -> list[tuple[int, str | None, str]]:
    """Replace all loads of ``net`` by ``table`` and update the consumer bus zones.

    The load table is reset to pandapower's empty load table and every spec is created through
    ``PandapowerBackend.create_component``, exactly like generation. For an unchanged grid the
    exported JSON is byte-identical to the stored one.

    Returns:
        ``[(bus pp_index, old zone, new zone)]`` of the consumer buses whose zone changed.
    """
    import pandapower as pp

    global _EMPTY_LOAD
    if _EMPTY_LOAD is None:
        _EMPTY_LOAD = pp.create_empty_network().load
    backend = _backend_for(net)
    net.load = _EMPTY_LOAD.copy()
    for spec in table.specs:
        backend.create_component(spec)
    changes = []
    names = net.bus["name"]
    for vertex, zone in table.zones.items():
        idx = names.index[names == f"Consumer Nodebus {vertex}"]
        if len(idx) != 1:
            raise ValueError(f"Consumer bus of vertex {vertex} not found in the net")
        old = _py(net.bus.at[idx[0], "zone"])
        if old != zone:
            net.bus.at[idx[0], "zone"] = zone
            changes.append((int(idx[0]), old, zone))
    return changes


def run_validation(net, params: LoadParameters) -> Validation:
    """:func:`validate_operating_point` with the version's voltage band (fallback: the bus limits)."""
    min_vm = params.min_vm_pu if params.min_vm_pu is not None else float(net.bus.min_vm_pu.min())
    max_vm = params.max_vm_pu if params.max_vm_pu is not None else float(net.bus.max_vm_pu.max())
    return validate_operating_point(_backend_for(net), min_vm, max_vm)


def power_flow_summary(net, validation: Validation) -> dict[str, Any]:
    """Key numbers of a solved net (``None`` values when the power flow did not converge)."""
    out: dict[str, Any] = {"status": validation.status, "error": validation.error,
                           "coincident_kw": float(net.load.p_mw.sum() * 1000) if len(net.load) else 0.0,
                           "coincident_kvar": float(net.load.q_mvar.sum() * 1000) if len(net.load) else 0.0}
    keys = ("min_vm_pu", "max_vm_pu", "max_line_loading_percent", "max_line_loading_index", "overloaded_lines",
            "trafo_loading_percent", "trafo_p_kw", "losses_kw")
    if validation.status == "not_converged" or net.res_bus.empty:
        out.update({k: None for k in keys})
        out.update(validation.drops)
        return out
    lv = net.bus.index[net.bus.vn_kv < 1.0]
    vm = net.res_bus.vm_pu.loc[lv]
    loading = net.res_line.loading_percent
    out.update({
        "min_vm_pu": float(vm.min()), "max_vm_pu": float(vm.max()),
        "max_line_loading_percent": float(loading.max()) if len(loading) else None,
        "max_line_loading_index": int(loading.idxmax()) if len(loading) else None,
        "overloaded_lines": int((loading > 100).sum()),
        "trafo_loading_percent": float(net.res_trafo.loading_percent.max()) if len(net.res_trafo) else None,
        "trafo_p_kw": float(net.res_trafo.p_hv_mw.sum() * 1000) if len(net.res_trafo) else None,
        "losses_kw": float((net.res_line.pl_mw.sum() + net.res_trafo.pl_mw.sum()) * 1000),
        **validation.drops,
    })
    return out


def service_design_check(net, vertex: int, params: LoadParameters) -> dict[str, Any] | None:
    """Service cable of a consumer vertex against its design load (the version's VN and power factor)."""
    buses = net.bus.index[net.bus.name == f"Consumer Nodebus {vertex}"]
    if len(buses) != 1:
        return None
    lines = net.line[net.line.to_bus == buses[0]]
    if lines.empty:
        return None
    line = lines.iloc[0]
    design_kw = float(net.load.loc[net.load.bus == buses[0], "service_design_p_mw"].sum() * 1000)
    current_ka = design_kw / (params.vn_v * params.power_factor * np.sqrt(3))
    parallel = int(line.get("parallel", 1) or 1)
    capacity_ka = float(line.max_i_ka) * parallel
    sin_phi = np.sqrt(1 - params.power_factor ** 2)
    impedance = (float(line.r_ohm_per_km) * params.power_factor + float(line.x_ohm_per_km) * sin_phi) / parallel
    drop = float(np.sqrt(3) * current_ka * float(line.length_km) * impedance / (params.vn_v * 1e-3) * 100)
    return {"line_index": int(lines.index[0]), "std_type": _py(line.get("std_type")), "parallel": parallel,
            "length_m": float(line.length_km) * 1000, "design_kw": design_kw, "design_current_a": current_ka * 1000,
            "capacity_a": capacity_ka * 1000, "design_drop_percent": drop}


# --------------------------------------------------------------------------- helpers
def _py(value: Any) -> Any:
    """numpy/pandas scalars to Python, NaN/NA to ``None``."""
    if value is None:
        return None
    if hasattr(value, "item") and not isinstance(value, (list, dict, str)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _pair(before: Any, after: Any) -> list:
    return [before, after]


def default_client_label(app: str = "pylovo") -> str:
    """``'<app> <os-user>@<host>'`` for the audit rows."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "?"
    return f"{app} {user}@{socket.gethostname()}"


class NetCache:
    """Small thread-safe LRU cache of parsed grids and their baseline power flow, keyed by ``(grid_result_id, md5)``."""

    def __init__(self, size: int = 6):
        self.size = size
        self._data: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key, value) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


# --------------------------------------------------------------------------- orchestrator
@dataclass
class GridState:
    """Everything read and derived for one grid (the parsed ``net`` is shared with the cache: never mutate it)."""

    head: dict
    etag: str
    params: LoadParameters
    buildings: pd.DataFrame
    loads: list[dict]
    consumer_buses: dict[str, int]
    net: Any
    storage: list[str]
    reproduction: Reproduction
    table: LoadTable
    baseline: Validation
    baseline_summary: dict
    pf_baseline: dict


class LoadEditor:
    """Context, preview, apply and undo of load edits for one database connection.

    Args:
        db: Object with the :class:`~pylovo.database.load_edit_mixin.LoadEditMixin` and
            ``AnalysisMixin`` query methods and ``conn``/``cur`` (e.g. a ``DatabaseClient``).
        pf_lock: Context manager that serialises power flows (the UI shares one with its
            on-demand power flow).
        cache: :class:`NetCache` shared between editor instances.
        client: Text stored as ``client`` in the audit rows.
    """

    def __init__(self, db, *, pf_lock: AbstractContextManager | None = None, cache: NetCache | None = None,
                 client: str | None = None):
        self.db = db
        self.pf_lock = pf_lock if pf_lock is not None else contextlib.nullcontext()
        self.cache = cache if cache is not None else NetCache(2)
        self.client = client or default_client_label()

    # ------------------------------------------------------------------ reading
    def _end_read(self) -> None:
        with contextlib.suppress(Exception):
            self.db.conn.rollback()

    def state(self, grid_result_id: int) -> GridState:
        """Read the grid, its buildings and loads, run the guards and the baseline power flow.

        The buildings and loads are read on every call (a few hundred rows); the parsed network
        and its baseline power flow are cached per ``(grid_result_id, md5 of the network JSON)``.

        Raises:
            LoadEditNotFound: If the grid does not exist.
            LoadEditConflict: ``not_editable`` if parameters or stored data are missing.
        """
        try:
            head = self.db.fetch_load_edit_grid(grid_result_id, with_net=False)
            if head is None:
                raise LoadEditNotFound("grid_not_found", f"Grid {grid_result_id} not found")
            etag = f"{head['grid_md5']}-{head['last_edit_id'] or 0}"
            params = LoadParameters.from_generation_parameters(head["version_id"], head["generation_parameters"])
            reasons = []
            if head["grid_md5"] is None:
                reasons.append("the grid has no stored pandapower network (grid_result.grid is NULL)")
            loads = self.db.fetch_pandapower_loads(grid_result_id)
            if not loads:
                reasons.append("the grid has no pandapower_load rows")
            elif any(row.get(k) is None for row in loads for k in ("category", "consumer_vertex", "service_design_p_mw")):
                reasons.append("pandapower_load lacks category, consumer_vertex or service_design_p_mw (older pylovo)")
            if reasons:
                raise LoadEditConflict("not_editable", "This grid cannot be edited: " + "; ".join(reasons), reasons=reasons)
            buildings = buildings_frame(self.db.fetch_load_edit_buildings(head["version_id"], grid_result_id))
            buses = self.db.fetch_pandapower_consumer_buses(grid_result_id)
            counts = self.db.count_pandapower_elements(grid_result_id)
            key = (grid_result_id, head["grid_md5"])
            cached = self.cache.get(key)
            grid_text = None if cached else self.db.fetch_load_edit_grid(grid_result_id, with_net=True)["grid_text"]
        finally:
            self._end_read()
        if cached is None:
            import pandapower as pp

            net = pp.from_json_string(grid_text)
            work = copy.deepcopy(net)
            with self.pf_lock:
                baseline = run_validation(work, params)
            cached = (net, baseline, power_flow_summary(work, baseline))
            self.cache.put(key, cached)
        net, baseline, baseline_summary = cached
        storage = check_storage_consistency(net, loads, buses, counts)
        consumer_buses = {b["name"]: int(b["pp_index"]) for b in buses}
        peak_mismatches, peak_rel = check_building_peaks(buildings, params)
        try:
            table = snapshot_loads(buildings, params)
            reproduction = compare_loads(table, loads, consumer_buses)
        except (ValueError, KeyError) as exc:
            table = LoadTable([], {}, {})
            reproduction = Reproduction("drift", load_mismatches=[str(exc)])
        reproduction.buildings_checked = len(buildings)
        reproduction.peak_mismatches = peak_mismatches
        reproduction.max_rel_diff = max(reproduction.max_rel_diff, peak_rel)
        if peak_mismatches:
            reproduction.tier = "drift"
        drop_diff = 0.0
        for name, value in baseline.drops.items():
            stored = head.get(name)
            if (value is None) != (stored is None):
                drop_diff = math.inf
            elif value is not None:
                drop_diff = max(drop_diff, abs(value - stored))
        pf_baseline = {"status_stored": head.get("power_flow_status"), "status_recomputed": baseline.status,
                       "max_drop_diff_pu": None if math.isinf(drop_diff) else drop_diff,
                       "matches_stored": baseline.status == head.get("power_flow_status") and drop_diff <= PF_BASELINE_TOL_PU}
        return GridState(head, etag, params, buildings, loads, consumer_buses, net, storage, reproduction, table,
                         baseline, baseline_summary, pf_baseline)

    @staticmethod
    def require_editable(state: GridState) -> None:
        """Raise unless the storage check passed and the loads were reproduced (no override)."""
        if state.storage:
            raise LoadEditConflict("inconsistent_storage", "The stored network JSON and the pandapower SQL tables of this "
                                   "grid disagree, so it cannot be edited.", mismatches=state.storage[:20])
        rep = state.reproduction
        if rep.tier == "drift":
            raise LoadEditConflict(
                "reproduction_drift",
                f"This grid cannot be edited: its stored loads are not reproducible from its buildings and the "
                f"v{state.params.version_id} parameters. Regenerate the PLZ as a new version to edit it.",
                max_rel_diff=rep.max_rel_diff, mismatches=(rep.peak_mismatches + rep.load_mismatches)[:20],
                likely_causes=["grid generated by an older pylovo formula",
                               "version snapshot backfilled from a later configuration",
                               "concurrent generation of another version re-synced consumer_categories",
                               "a duplicated objectid dropped by save_tables"])

    def _building(self, state: GridState, objectid: str) -> dict:
        rows = state.buildings[state.buildings["objectid"] == objectid]
        if rows.empty:
            raise LoadEditNotFound("building_not_found", f"Building {objectid} is not part of grid "
                                   f"{state.head['grid_result_id']}")
        out = {k: _py(v) for k, v in rows.iloc[0].to_dict().items()}
        for key in ("households", "vertice_id", "floor_number"):  # integer columns with NULLs come back as float
            if isinstance(out.get(key), float) and out[key].is_integer():
                out[key] = int(out[key])
        return out

    def _original_inputs(self, version_id: str, objectid: str) -> BuildingInputs | None:
        try:
            row = self.db.first_load_edit_of_building(version_id, objectid)
        finally:
            self._end_read()
        return BuildingInputs.from_row(row["before_building"]) if row else None

    # ------------------------------------------------------------------ context
    def context(self, grid_result_id: int, objectid: str) -> dict[str, Any]:
        """The building, its generated inputs, the parameters, the guard results and the edit history."""
        state = self.state(grid_result_id)
        building = self._building(state, objectid)
        original = self._original_inputs(state.params.version_id, objectid)
        vertex = int(building["vertice_id"])
        loads = [{"name": r["name"], "category": r["category"], "p_kw": r["p_mw"] * 1000,
                  "installed_kw": r["max_p_mw"] * 1000, "design_kw": r["service_design_p_mw"] * 1000,
                  "units": r["load_units"]}
                 for r in state.loads if int(r["consumer_vertex"]) == vertex]
        editable_reasons = []
        if state.storage:
            editable_reasons.append("stored JSON and SQL tables disagree")
        if state.reproduction.tier == "drift":
            editable_reasons.append("stored loads are not reproducible")
        same_vertex = state.buildings[(state.buildings["vertice_id"] == vertex) & (state.buildings["objectid"] != objectid)]
        try:
            history = self.db.load_edit_history(grid_result_id=grid_result_id)
            first_version_edit = not self.db.version_has_load_edits(state.params.version_id)
        finally:
            self._end_read()
        return {
            "grid_result_id": grid_result_id,
            "version_id": state.params.version_id, "plz": state.head["plz"],
            "kcid": state.head["kcid"], "bcid": state.head["bcid"],
            "building": {**building, "gross_floor_area": gross_floor_area(building),
                         "shares_vertex_with": same_vertex["objectid"].tolist()},
            "original": original.as_dict() if original else None,
            "editable": {"ok": not editable_reasons, "reasons": editable_reasons,
                         "uses": list(state.params.peak_load_per_m2_w)},
            "parameters": state.params.as_json(),
            "loads": loads,
            "service": service_design_check(state.net, vertex, state.params),
            "checks": {"storage": {"consistent": not state.storage, "mismatches": state.storage[:20]},
                       "reproduction": state.reproduction.as_dict(), "pf_baseline": state.pf_baseline},
            "baseline": _clean(state.baseline_summary),
            "first_version_edit": first_version_edit,
            "history": [h for h in history if h["objectid"] == objectid],
            "etag": state.etag,
        }

    def check(self, grid_result_id: int) -> dict[str, Any]:
        """Guard results of one grid (read-only diagnostic)."""
        state = self.state(grid_result_id)
        reasons = []
        if state.storage:
            reasons.append("inconsistent_storage")
        if state.reproduction.tier == "drift":
            reasons.append("reproduction_drift")
        return {"grid_result_id": grid_result_id, "etag": state.etag,
                "storage": {"consistent": not state.storage, "mismatches": state.storage[:20]},
                "reproduction": state.reproduction.as_dict(), "pf_baseline": state.pf_baseline,
                "parameters": state.params.as_json(), "editable": not reasons, "reasons": reasons}

    # ------------------------------------------------------------------ preview
    def _compute(self, state: GridState, objectid: str, changes: dict) -> dict[str, Any]:
        building = self._building(state, objectid)
        original = self._original_inputs(state.params.version_id, objectid)
        check = validate_changes(building, changes, state.params, original)
        result: dict[str, Any] = {"valid": check.valid, "errors": check.errors, "warnings": list(check.warnings),
                                  "changes": check.changes, "etag": state.etag, "objectid": objectid}
        before_peaks = _stored_peaks(building)
        result["building"] = {"before": {"inputs": BuildingInputs.from_row(building).as_dict(), "peaks": before_peaks.as_dict()},
                              "after": {"inputs": check.inputs.as_dict(),
                                        "peaks": check.peaks.as_dict() if check.peaks else None}}
        if not check.valid:
            return result
        edited = state.buildings.copy()
        mask = (edited["objectid"] == objectid).to_numpy()
        for key, value in {**check.inputs.as_dict(), **check.peaks.as_dict()}.items():
            if edited[key].dtype != object and value is None:
                edited[key] = edited[key].astype(object)
            edited.loc[mask, key] = value
        table = snapshot_loads(edited, state.params)
        net = copy.deepcopy(state.net)
        zone_changes = replace_net_loads(net, table)
        with self.pf_lock:
            validation = run_validation(net, state.params)
        if validation.status == "not_converged":
            from pandapower.results import reset_results

            reset_results(net)
        summary = power_flow_summary(net, validation)
        result.update(self._impact(state, building, table, net, zone_changes, validation, summary, check))
        result["_net"] = net
        result["_table"] = table
        result["_validation"] = validation
        result["_zone_changes"] = zone_changes
        result["_check"] = check
        return result

    def _impact(self, state: GridState, building: dict, table: LoadTable, net, zone_changes, validation: Validation,
                summary: dict, check: ChangeCheck) -> dict[str, Any]:
        before_rows, after_rows = state.table.rows, table.rows
        before_by_name = {r["name"]: r for r in before_rows}
        after_by_name = {r["name"]: r for r in after_rows}
        changed = [n for n, r in after_by_name.items() if n in before_by_name
                   and abs(r["p_mw"] - before_by_name[n]["p_mw"]) * 1000 > CHANGED_LOAD_TOL_KW]
        vertex = int(building["vertice_id"])
        edited_loads = [{"name": n,
                         "p_kw": _pair(before_by_name[n]["p_mw"] * 1000 if n in before_by_name else None,
                                       after_by_name[n]["p_mw"] * 1000 if n in after_by_name else None),
                         "design_kw": _pair(before_by_name[n]["service_design_p_mw"] * 1000 if n in before_by_name else None,
                                            after_by_name[n]["service_design_p_mw"] * 1000 if n in after_by_name else None)}
                        for n in dict.fromkeys([*(r["name"] for r in before_rows if r["consumer_vertex"] == vertex),
                                                *(r["name"] for r in after_rows if r["consumer_vertex"] == vertex)])]
        categories = []
        for name in sorted(set(state.table.categories) | set(table.categories)):
            b, a = state.table.categories.get(name, {}), table.categories.get(name, {})
            categories.append({"category": name, "sim_factor": state.params.sim_factor.get(name),
                               **{k: _pair(b.get(k), a.get(k)) for k in ("units", "installed_kw", "simultaneous_kw",
                                                                          "utilisation", "loads")},
                               "changed_loads": sum(1 for n in changed if after_by_name[n]["category"] == name)})
        rated = state.head.get("transformer_rated_power")
        base = state.baseline_summary
        warnings = list(check.warnings)
        other = [n for n in changed if after_by_name[n]["consumer_vertex"] != vertex]
        if other:
            warnings.append(_issue("other_loads_change", f"{len(other)} other loads of this grid change, because simultaneity is "
                                   "grouped per category over the whole grid."))
        service = service_design_check(net, vertex, state.params)
        if service and service["design_current_a"] > service["capacity_a"]:
            warnings.append(_issue("service_ampacity", f"The service cable ({service['std_type']}, {service['parallel']} ×) carries "
                                   f"{service['capacity_a']:.0f} A; the new design load needs {service['design_current_a']:.0f} A."))
        limit = state.params.max_service_drop_percent
        if service and limit is not None and service["design_drop_percent"] > float(limit):
            warnings.append(_issue("service_drop", f"Service design voltage drop {service['design_drop_percent']:.2f} % "
                                   f"exceeds the limit of {float(limit):g} %."))
        planning = [x / state.params.planning_utilization for x in (base["coincident_kw"], summary["coincident_kw"])]
        if rated and planning[1] > rated >= planning[0]:
            warnings.append(_issue("transformer_planning", f"Coincident load / planning utilisation = {planning[1]:.0f} kW now exceeds "
                                   f"the rated {rated} kVA (the rule pylovo applies when it sizes greenfield stations; "
                                   "brownfield ratings come from data)."))
        if (summary.get("trafo_loading_percent") or 0) > 100:
            warnings.append(_issue("transformer_overload", f"Transformer loading {summary['trafo_loading_percent']:.0f} %."))
        if summary.get("overloaded_lines"):
            new = summary["overloaded_lines"] - (base.get("overloaded_lines") or 0)
            warnings.append(_issue("line_overload", f"{summary['overloaded_lines']} cable(s) above 100 % loading"
                                   + (f" ({new} more than before)" if new > 0 else " (pre-existing)")))
        if validation.status == "voltage_violation":
            warnings.append(_issue("voltage_band", "Bus voltages outside the band "
                                   + ("(pre-existing)." if state.baseline.status == "voltage_violation" else "(new).")))
        if validation.status == "not_converged":
            warnings.append(_issue("not_converged", "The validation power flow does not converge with this load. Applying "
                                   "needs an explicit acknowledgement.", severity="error"))
        if not state.pf_baseline["matches_stored"]:
            warnings.append(_issue("pf_baseline_differs", "The power flow recomputed on the unchanged grid differs from the stored "
                                   "result (another pandapower version?). Before/after values use the recomputed baseline."))
        if state.reproduction.tier == "tolerance":
            warnings.append(_issue("reproduction_tolerance", "Stored loads reproduced within floating-point tolerance "
                                   f"(max rel. {state.reproduction.max_rel_diff:.1e}).", severity="info"))
        pf = {k: _pair(base.get(k), summary.get(k)) for k in (
            "status", "min_vm_pu", "max_vm_pu", "max_line_loading_percent", "overloaded_lines", "trafo_loading_percent",
            "max_total_lv_voltage_drop_pu", "max_feeder_voltage_drop_pu", "max_service_voltage_drop_pu", "losses_kw")}
        coincident_kva = [math.hypot(x["coincident_kw"], x["coincident_kvar"]) for x in (base, summary)]
        return {
            "warnings": warnings,
            "categories": categories,
            "loads": {"total": len(after_rows), "changed": len(changed),
                      "added": [n for n in after_by_name if n not in before_by_name],
                      "removed": [n for n in before_by_name if n not in after_by_name],
                      "zone_changes": [list(z) for z in zone_changes], "edited": edited_loads},
            "grid": {"coincident_kw": _pair(base["coincident_kw"], summary["coincident_kw"]),
                     "coincident_kva": coincident_kva, "rated_kva": rated,
                     "utilisation": [x / rated for x in coincident_kva] if rated else None,
                     "planning_check_kw_over_util": planning, "planning_utilization": state.params.planning_utilization},
            "power_flow": {"baseline_source": "recomputed", **pf, "converged_after": validation.status != "not_converged",
                           "min_vm_limit": state.params.min_vm_pu},
            "service": {"before": service_design_check(state.net, vertex, state.params), "after": service},
            "reproduction": {"tier": state.reproduction.tier, "max_rel_diff": state.reproduction.max_rel_diff},
        }

    def preview(self, grid_result_id: int, objectid: str, changes: dict) -> dict[str, Any]:
        """Compute the effect of ``changes`` without writing anything.

        Returns:
            ``valid``, field ``errors``, ``warnings`` and the before/after numbers of the building,
            the categories, the loads, the grid and the validation power flow.
        """
        started = time.time()
        state = self.state(grid_result_id)
        self.require_editable(state)
        result = self._compute(state, objectid, changes)
        try:
            result["analysis_to_remove"] = self.db.load_dependent_analysis_rows(
                state.params.version_id, state.head["plz"], grid_result_id)
        finally:
            self._end_read()
        result["took_s"] = round(time.time() - started, 3)
        return _public(result)

    # ------------------------------------------------------------------ writing
    @contextlib.contextmanager
    def _write(self, lock_timeout: str = "2s"):
        cur = self.db.cur
        self._end_read()
        try:
            cur.execute("SET LOCAL lock_timeout = %s", (lock_timeout,))
            cur.execute("SET LOCAL statement_timeout = '60s'")
            yield cur
            self.db.conn.commit()
        except Exception:
            self.db.conn.rollback()
            raise

    def _ensure_schema(self) -> None:
        import psycopg2

        try:
            exists = self.db.load_edit_table_exists()
        finally:
            self._end_read()
        if exists:
            return
        try:
            self.db.ensure_load_edit_schema()
        except psycopg2.errors.InsufficientPrivilege as exc:
            raise LoadEditConflict("schema_missing", "The audit table pylovo.load_edit is missing and this database user may "
                                   "not create it. Run pylovo-setup or grant CREATE on schema pylovo.") from exc
        except psycopg2.errors.LockNotAvailable as exc:
            raise LoadEditConflict("locked", "The audit table pylovo.load_edit could not be created because grid_result is "
                                   "locked (a generation is running?). Try again later.") from exc

    def _lock(self, grid_result_id: int) -> dict:
        import psycopg2

        try:
            row = self.db.lock_load_edit_grid(grid_result_id)
        except psycopg2.errors.LockNotAvailable as exc:
            raise LoadEditConflict("locked", "Another edit or a pylovo job is writing this grid right now. "
                                   "Try again in a moment.") from exc
        if row is None:
            raise LoadEditNotFound("grid_not_found", f"Grid {grid_result_id} not found")
        return row

    def apply(self, grid_result_id: int, objectid: str, changes: dict, *, if_match: str, reason: str | None = None,
              acknowledge: Iterable[str] = (), first_version_edit_confirm: str | None = None,
              write_guard: AbstractContextManager | None = None, action: str = "edit") -> dict[str, Any]:
        """Write an edit: building row, pandapower loads, bus zones, network JSON, validation columns, audit row.

        Everything is computed before the write transaction; the transaction locks the grid row
        (``FOR NO KEY UPDATE NOWAIT``), re-checks the etag and the building row and takes about
        0.1-0.3 s.

        Args:
            grid_result_id: Grid.
            objectid: Building.
            changes: New input values.
            if_match: Etag the preview was computed at.
            reason: Optional free text (max. 500 characters).
            acknowledge: ``["non_convergence"]`` to store a grid whose power flow does not converge.
            first_version_edit_confirm: The version ID, required for the first edit of a version.
            write_guard: Context manager held during the write transaction (the UI's job lease).
            action: ``edit`` or ``revert``.

        Returns:
            The new etag, the audit row id, the impact and the removed analysis rows.

        Raises:
            LoadEditValidationError: Invalid or empty change.
            LoadEditConflict: ``etag_mismatch``, ``locked``, ``ack_required`` or a failed guard.
        """
        state = self.state(grid_result_id)
        self.require_editable(state)
        if if_match != state.etag:
            raise LoadEditConflict("etag_mismatch", "The grid changed since the preview. Preview again.", etag=state.etag)
        result = self._compute(state, objectid, changes)
        if not result["valid"]:
            raise LoadEditValidationError("invalid_change", "; ".join(e["message"] for e in result["errors"]),
                                          errors=result["errors"])
        validation: Validation = result["_validation"]
        acknowledge = set(acknowledge or ())
        version_id = state.params.version_id
        try:
            first_edit = not self.db.version_has_load_edits(version_id)
        finally:
            self._end_read()
        missing = []
        if validation.status == "not_converged" and "non_convergence" not in acknowledge:
            missing.append("non_convergence")
        if first_edit and str(first_version_edit_confirm or "").strip() != version_id:
            missing.append("first_version_edit")
        if missing:
            raise LoadEditConflict("ack_required", "Confirmation required: " + ", ".join(missing), acknowledge=missing,
                                   first_version_edit=first_edit)
        import pandapower as pp

        net = result["_net"]
        json_text = pp.to_json(net)
        check: ChangeCheck = result["_check"]
        building_before = self._building(state, objectid)
        impact = _public({k: result[k] for k in ("warnings", "categories", "loads", "grid", "power_flow", "service")})
        with (write_guard or contextlib.nullcontext()):
            self._ensure_schema()
            with self._write():
                locked = self._lock(grid_result_id)
                if f"{locked['grid_md5']}-{locked['last_edit_id'] or 0}" != state.etag:
                    raise LoadEditConflict("etag_mismatch", "The grid changed since the preview. Preview again.")
                current = self.db.fetch_load_edit_building(version_id, grid_result_id, objectid, lock=True)
                if current is None or any(_py(current.get(k)) != building_before.get(k)
                                          for k in (*_BUILDING_INPUTS, *_BUILDING_PEAKS)):
                    raise LoadEditConflict("etag_mismatch", "The building changed since the preview. Preview again.")
                before_loads = self.db.snapshot_pandapower_loads(grid_result_id)
                removed = self.db.capture_load_dependent_analysis(version_id, state.head["plz"], grid_result_id,
                                                                  grid_token=locked["grid_md5"])
                edit_id = self.db.insert_load_edit({
                    "version_id": version_id, "grid_result_id": grid_result_id, "plz": state.head["plz"],
                    "objectid": objectid, "action": action, "changes": _clean(check.changes), "reason": reason or None,
                    "before_building": {k: building_before.get(k) for k in (*_BUILDING_INPUTS, *_BUILDING_PEAKS)},
                    "after_building": {**check.inputs.as_dict(), **check.peaks.as_dict()},
                    "before_grid": {"power_flow_status": state.head.get("power_flow_status"),
                                    **{k: state.head.get(k) for k in validation.drops}},
                    "after_grid": {"power_flow_status": validation.status, **validation.drops},
                    "before_loads": before_loads,
                    "before_bus_zones": [[pp_index, old] for pp_index, old, _ in result["_zone_changes"]],
                    "before_net_md5": locked["grid_md5"],
                    "removed_analysis": removed, "impact": impact, "parameters": state.params.as_json(),
                    "reproduction": {"tier": state.reproduction.tier, "max_rel_diff": state.reproduction.max_rel_diff,
                                     "max_abs_diff_mw": state.reproduction.max_abs_diff_mw},
                    "client": self.client,
                })
                self.db.update_building_load_inputs(version_id, grid_result_id, objectid,
                                                    {**check.inputs.as_dict(), **check.peaks.as_dict()})
                self.db.replace_pandapower_load_rows(grid_result_id, net.load)
                self.db.update_pandapower_bus_zones(grid_result_id, [(i, new) for i, _, new in result["_zone_changes"]])
                after_md5 = self.db.save_revalidated_grid(grid_result_id, json_text, validation.status, validation.drops)
                self.db.set_load_edit_after_md5(edit_id, after_md5)
        return {"load_edit_id": edit_id, "action": action, "etag": f"{after_md5}-{edit_id}",
                "status": _pair(state.head.get("power_flow_status"), validation.status),
                "impact": impact, "removed_analysis": {k: removed.get(k) is not None for k in
                                                       ("plz_parameters", "clustering_parameters", "grid_parameters")}}

    def revert(self, grid_result_id: int, objectid: str, **kwargs) -> dict[str, Any]:
        """Apply the generated inputs of a building again (a new audit row with ``action='revert'``).

        Raises:
            LoadEditConflict: ``not_edited`` if the building has no edits or already has its generated inputs.
        """
        state = self.state(grid_result_id)
        original = self._original_inputs(state.params.version_id, objectid)
        building = self._building(state, objectid)
        if original is None or original == BuildingInputs.from_row(building):
            raise LoadEditConflict("not_edited", "The building has its generated inputs.")
        return self.apply(grid_result_id, objectid, original.as_dict(), action="revert", **kwargs)

    def _undo_one(self, edit: dict, undone_by: str) -> dict[str, Any]:
        """Undo one edit inside the current write transaction (checks already done by the caller)."""
        gid = edit["grid_result_id"]
        restored_md5 = self.db.restore_grid_from_load_edit(edit["load_edit_id"])
        if restored_md5 != edit["before_net_md5"]:
            raise LoadEditConflict("restore_failed", "The restored network does not match its stored checksum.")
        before = edit["before_building"]
        self.db.update_building_load_inputs(edit["version_id"], gid, edit["objectid"],
                                            {k: before.get(k) for k in (*_BUILDING_INPUTS, *_BUILDING_PEAKS)})
        self.db.restore_pandapower_load_rows(gid, edit["before_loads"])
        self.db.update_pandapower_bus_zones(gid, [(int(i), z) for i, z in edit["before_bus_zones"]])
        self.db.mark_load_edit_undone(edit["load_edit_id"], undone_by)
        return {"load_edit_id": edit["load_edit_id"], "loads": len(edit["before_loads"])}

    def undo(self, load_edit_id: int, *, if_match: str | None = None, write_guard: AbstractContextManager | None = None,
             undone_by: str | None = None) -> dict[str, Any]:
        """Undo the latest active edit of a grid exactly (network JSON, loads, building, zones, analysis rows).

        Raises:
            LoadEditNotFound: Unknown edit.
            LoadEditConflict: ``already_undone``, ``not_latest``, ``etag_mismatch`` or ``locked``.
        """
        try:
            edit = self.db.fetch_load_edit(load_edit_id)
        finally:
            self._end_read()
        if edit is None:
            raise LoadEditNotFound("edit_not_found", f"Load edit {load_edit_id} not found")
        gid = edit["grid_result_id"]
        with (write_guard or contextlib.nullcontext()), self._write():
            locked = self._lock(gid)
            edit = self.db.fetch_load_edit(load_edit_id)
            self._check_undoable(edit, locked, if_match)
            out = self._undo_one(edit, undone_by or self.client)
            analysis = self.db.restore_load_dependent_analysis(edit["version_id"], edit["plz"], gid)
            head = self.db.lock_load_edit_grid(gid)
        return {**out, "etag": f"{head['grid_md5']}-{head['last_edit_id'] or 0}", "net_md5_matches": True,
                "analysis_restored": analysis}

    def _check_undoable(self, edit: dict, locked: dict, if_match: str | None) -> None:
        if edit["undone_at"] is not None:
            raise LoadEditConflict("already_undone", f"Edit {edit['load_edit_id']} was already undone.")
        latest = self.db.latest_active_load_edit(edit["grid_result_id"])
        if latest and latest["load_edit_id"] != edit["load_edit_id"]:
            raise LoadEditConflict("not_latest", f"Undo edit {latest['load_edit_id']} of this grid first.",
                                   latest_id=latest["load_edit_id"])
        if if_match is not None and if_match != f"{locked['grid_md5']}-{locked['last_edit_id'] or 0}":
            raise LoadEditConflict("etag_mismatch", "The grid changed. Reload it and try again.")
        if locked["grid_md5"] != edit["after_net_md5"]:
            raise LoadEditConflict("etag_mismatch", "The stored network was changed after this edit.")
        current = self.db.fetch_load_edit_building(edit["version_id"], edit["grid_result_id"], edit["objectid"], lock=True)
        after = edit["after_building"]
        if current is None or any(_py(current.get(k)) != after.get(k) for k in after):
            raise LoadEditConflict("etag_mismatch", "The building was changed after this edit.")

    def undo_all(self, grid_result_id: int, *, if_match: str | None = None, write_guard: AbstractContextManager | None = None,
                 undone_by: str | None = None) -> dict[str, Any]:
        """Undo every active edit of a grid, newest first, in one transaction (exact)."""
        undone = []
        with (write_guard or contextlib.nullcontext()), self._write():
            locked = self._lock(grid_result_id)
            if if_match is not None and if_match != f"{locked['grid_md5']}-{locked['last_edit_id'] or 0}":
                raise LoadEditConflict("etag_mismatch", "The grid changed. Reload it and try again.")
            edit = self.db.latest_active_load_edit(grid_result_id)
            if edit is None:
                raise LoadEditConflict("nothing_to_undo", "This grid has no active load edits.")
            analysis = {}
            while edit is not None:
                edit = self.db.fetch_load_edit(edit["load_edit_id"])
                self._check_undoable(edit, locked, None)
                undone.append(self._undo_one(edit, undone_by or self.client)["load_edit_id"])
                analysis = self.db.restore_load_dependent_analysis(edit["version_id"], edit["plz"], grid_result_id)
                locked = self.db.lock_load_edit_grid(grid_result_id)
                edit = self.db.latest_active_load_edit(grid_result_id)
            generated_md5 = self.db.generated_net_md5(grid_result_id)
        return {"undone": undone, "etag": f"{locked['grid_md5']}-{locked['last_edit_id'] or 0}",
                "net_equals_generated": generated_md5 == locked["grid_md5"], "analysis_restored": analysis}

    def history(self, grid_result_id: int | None = None, *, version_id: str | None = None,
                objectid: str | None = None) -> list[dict]:
        """Audit rows (newest first) with ``undoable`` and ``undo_blocked_by``."""
        try:
            rows = self.db.load_edit_history(grid_result_id=grid_result_id, version_id=version_id)
        finally:
            self._end_read()
        if objectid:
            rows = [r for r in rows if r["objectid"] == objectid]
        return rows


def _clean(value: Any) -> Any:
    """Make numbers JSON-safe (NaN/inf -> ``None``, numpy -> Python)."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    value = _py(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _public(result: dict) -> dict:
    """Drop the private ``_*`` entries and clean the numbers."""
    return _clean({k: v for k, v in result.items() if not k.startswith("_")})


def dumps(value: Any) -> str:
    """JSON text for the audit table (exact floats, no NaN)."""
    return json.dumps(_clean(value), allow_nan=False)


__all__ = [
    "EDITABLE_FIELDS",
    "BuildingInputs",
    "ComponentPeaks",
    "LoadEditConflict",
    "LoadEditError",
    "LoadEditNotFound",
    "LoadEditValidationError",
    "LoadEditor",
    "LoadParameters",
    "NetCache",
    "build_load_specs",
    "buildings_frame",
    "check_building_peaks",
    "check_storage_consistency",
    "compare_loads",
    "component_peaks",
    "power_flow_summary",
    "replace_net_loads",
    "run_validation",
    "snapshot_loads",
    "validate_changes",
    "validate_operating_point",
]
