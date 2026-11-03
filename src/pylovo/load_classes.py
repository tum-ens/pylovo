"""Load classes of non-residential buildings and the connection level of a building.

The non-residential load model ``load_class`` (``NONRESIDENTIAL_LOAD_MODEL``) gives the non-residential part of a
building the load of its InfDB load class (``basedata.buildings.load_class``) instead of one W/m² per use
(``CONSUMER_CATEGORIES``). Per class, ``LOAD_CLASSES`` of ``config_generation_lv.yaml`` holds the annual electricity
of a federal benchmark per m² of net floor area, the standard load profile and the sources; this module derives
(docs: ``concepts/nonresidential_loads``):

* ``yearly_consumption_per_m2`` in kWh/(m²·a) of gross floor area = benchmark x ``net_floor_area_factor``
  (net floor area per gross floor area);
* ``coincident_load_per_m2`` in W/m² = ``yearly_consumption_per_m2`` / full-load hours of the profile x 1000. A
  standard load profile is the mean of many customers, so this is what one building adds to the peak of many;
* the own peak of one building per m² (pylovo's installed load, factor 1 for one unit) = ``coincident_load_per_m2``
  / g of the consumer category, so that pylovo's coincidence law ``g + (1 - g) n^(-3/4)`` gives the profile peak
  for many buildings.

A benchmark may have a second value for large buildings (``large``): above ``from_m2_net`` of net floor area the
non-residential part takes that value instead.

A Commercial or Public part without a class (NULL: no evidence) keeps the W/m² of its use, or with
``UNCLASSIFIED_LOAD_CLASS`` takes the load of that class; its ``load_class`` stays NULL.

The connection level of a building (``connection_level``) follows eGon-data on the building's own peak:
``lv_feeder`` up to the station-busbar threshold, ``station_busbar`` above it, ``mv`` for a non-residential part
above ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW`` (it leaves the LV grid) and ``hv`` from the HV threshold.

Pure functions without configuration or database access (``config_loader``, the generation and the tests use them).
"""
from __future__ import annotations

import math
from collections.abc import Mapping

import pandas as pd

#: Values of ``NONRESIDENTIAL_LOAD_MODEL``: ``building_use`` is the W/m² of the use (versions before the classes).
NONRESIDENTIAL_LOAD_MODELS = ("building_use", "load_class")
#: The uses whose non-residential part takes the load of its class; an Unknown part keeps its (zero) load.
CLASSIFIED_USES = ("Commercial", "Public")
#: The load classes of the InfDB (``basedata.buildings.load_class``); NULL means no evidence.
INFDB_LOAD_CLASSES = ("office", "retail", "food_retail", "hospitality", "education", "health_care", "sports",
                      "culture_leisure", "worship", "public_administration", "production", "warehouse",
                      "agriculture", "other")
#: Connection levels of a building, from the LV feeder to HV.
CONNECTION_LEVELS = ("lv_feeder", "station_busbar", "mv", "hv")
#: Columns of :func:`load_class_table`.
LOAD_CLASS_COLUMNS = (
    "load_class", "consumer_category", "load_profile", "full_load_hours", "electricity_kwh_per_m2_net",
    "large_from_m2_net", "large_electricity_kwh_per_m2_net", "source",
    "yearly_consumption_per_m2", "coincident_load_per_m2",
    "large_from_m2", "large_yearly_consumption_per_m2", "large_coincident_load_per_m2",
)
HOURS_PER_YEAR = 8784  # a leap year: the most full-load hours a profile can have


def _positive(value, name: str, upper: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"LOAD_CLASSES: {name} must be a number above 0, got {value!r}")
    if upper is not None and value > upper:
        raise ValueError(f"LOAD_CLASSES: {name} must be at most {upper:g}, got {value!r}")
    return float(value)


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"LOAD_CLASSES: {name} must be a text, got {value!r}")
    return value.strip()


def load_class_table(config: Mapping, sim_factor: Mapping[str, float]) -> pd.DataFrame:
    """Check ``LOAD_CLASSES`` and derive the annual and coincident loads per m² of gross floor area.

    Args:
        config: ``LOAD_CLASSES`` of ``config_generation_lv.yaml``: ``net_floor_area_factor``, ``load_profiles``
            (profile -> full-load hours) and ``classes``, a list of ``{load_class, consumer_category,
            electricity_kwh_per_m2_net, load_profile, source}`` with an optional ``large: {from_m2_net,
            electricity_kwh_per_m2_net, source}``. ``consumer_category`` (Commercial or Public) is the category the
            class's loads count in; ``null`` keeps the use of the building.
        sim_factor: g per consumer category (``SIM_FACTOR``).

    Returns:
        One row per class with :data:`LOAD_CLASS_COLUMNS`, in the configured order; the ``large_*`` columns are NaN
        without a value for large buildings.

    Raises:
        ValueError: If a value is missing, not positive, or names an unknown profile or category, or a class
            occurs twice.
    """
    if not isinstance(config, Mapping):
        raise ValueError(f"LOAD_CLASSES must be a mapping, got {config!r}")
    factor = _positive(config.get("net_floor_area_factor"), "net_floor_area_factor", upper=1.0)
    profiles = config.get("load_profiles")
    if not isinstance(profiles, Mapping) or not profiles:
        raise ValueError("LOAD_CLASSES: load_profiles must map each standard load profile to its full-load hours")
    hours = {str(name): _positive(value, f"full-load hours of {name}", upper=HOURS_PER_YEAR)
             for name, value in profiles.items()}
    classes = config.get("classes")
    if not isinstance(classes, list) or not classes:
        raise ValueError("LOAD_CLASSES: classes must be a list of load classes")
    rows = []
    for entry in classes:
        if not isinstance(entry, Mapping):
            raise ValueError(f"LOAD_CLASSES: a class must be a mapping, got {entry!r}")
        name = _text(entry.get("load_class"), "load_class")
        if len(name) > 30:
            raise ValueError(f"LOAD_CLASSES: load_class {name!r} is longer than 30 characters")
        category = entry.get("consumer_category")
        if category is not None and (category not in CLASSIFIED_USES or category not in sim_factor):
            raise ValueError(f"LOAD_CLASSES: consumer_category of {name} must be one of {list(CLASSIFIED_USES)} "
                             f"with a sim_factor in CONSUMER_CATEGORIES, or null, got {category!r}")
        profile = _text(entry.get("load_profile"), f"load_profile of {name}")
        if profile not in hours:
            raise ValueError(f"LOAD_CLASSES: load_profile {profile!r} of {name} is not in load_profiles")
        electricity = _positive(entry.get("electricity_kwh_per_m2_net"), f"electricity_kwh_per_m2_net of {name}")
        source = _text(entry.get("source"), f"source of {name}")
        large = entry.get("large")
        large_from = large_electricity = math.nan
        if large is not None:
            if not isinstance(large, Mapping):
                raise ValueError(f"LOAD_CLASSES: large of {name} must be a mapping, got {large!r}")
            large_from = _positive(large.get("from_m2_net"), f"large.from_m2_net of {name}")
            large_electricity = _positive(large.get("electricity_kwh_per_m2_net"),
                                          f"large.electricity_kwh_per_m2_net of {name}")
            source = f"{source}; large: {_text(large.get('source'), f'large.source of {name}')}"
        rows.append({
            "load_class": name, "consumer_category": category, "load_profile": profile,
            "full_load_hours": hours[profile], "electricity_kwh_per_m2_net": electricity,
            "large_from_m2_net": large_from, "large_electricity_kwh_per_m2_net": large_electricity, "source": source,
        })
    table = pd.DataFrame(rows)
    duplicated = table.loc[table["load_class"].duplicated(), "load_class"].tolist()
    if duplicated:
        raise ValueError(f"LOAD_CLASSES: classes listed twice: {duplicated}")
    table["yearly_consumption_per_m2"] = table["electricity_kwh_per_m2_net"] * factor
    table["coincident_load_per_m2"] = table["yearly_consumption_per_m2"] / table["full_load_hours"] * 1000.0
    table["large_from_m2"] = table["large_from_m2_net"] / factor
    table["large_yearly_consumption_per_m2"] = table["large_electricity_kwh_per_m2_net"] * factor
    table["large_coincident_load_per_m2"] = (table["large_yearly_consumption_per_m2"] / table["full_load_hours"]
                                             * 1000.0)
    return table[list(LOAD_CLASS_COLUMNS)]


def own_peak_per_m2(coincident_load_per_m2: float, sim_factor: float) -> float:
    """The own peak of one building per m² (W/m²) whose coincident load in a large group is ``coincident_load_per_m2``.

    pylovo's law ``g + (1 - g) n^(-3/4)`` tends to ``g`` for many units, so the own peak is the coincident load over g.
    """
    if not 0 < sim_factor <= 1:
        raise ValueError(f"sim_factor must be in (0, 1], got {sim_factor!r}")
    return coincident_load_per_m2 / sim_factor


def class_load_kw(row: Mapping, floor_area_m2: float, category: str, sim_factor: Mapping[str, float]) -> float:
    """The own peak (kW) of a non-residential part of ``floor_area_m2`` gross floor area of the class ``row``.

    ``category`` is the use of the building, which counts where the class has no ``consumer_category``. The
    generation computes the same in SQL (``PreprocessingMixin.set_building_peak_load``).
    """
    category = row["consumer_category"] or category
    large_from = row["large_from_m2"]
    large = large_from is not None and not math.isnan(large_from) and floor_area_m2 > large_from
    coincident = row["large_coincident_load_per_m2"] if large else row["coincident_load_per_m2"]
    return floor_area_m2 * own_peak_per_m2(coincident, sim_factor[category]) / 1000.0


def sql_parameters(table: pd.DataFrame) -> dict:
    """The class table as arrays for ``unnest`` in SQL (``classes``, ``categories``, ``coincident``,
    ``large_from``, ``large_coincident``); NaN becomes NULL."""
    def floats(column):
        return [None if pd.isna(v) else float(v) for v in table[column]]

    return {
        "classes": [str(v) for v in table["load_class"]],
        "categories": [None if v is None or pd.isna(v) else str(v) for v in table["consumer_category"]],
        "coincident": floats("coincident_load_per_m2"),
        "large_from": floats("large_from_m2"),
        "large_coincident": floats("large_coincident_load_per_m2"),
    }


def snapshot(config: Mapping, table: pd.DataFrame, sim_factor: Mapping[str, float]) -> dict:
    """The load classes for the version snapshot: the inputs and, per class, the derived values and the own peak
    per m² in each category the class can count in."""
    records = []
    for row in table.to_dict(orient="records"):
        categories = [row["consumer_category"]] if row["consumer_category"] else list(CLASSIFIED_USES)
        record = {key: (None if isinstance(value, float) and math.isnan(value) else value)
                  for key, value in row.items()}
        record["peak_load_per_m2"] = {c: own_peak_per_m2(row["coincident_load_per_m2"], sim_factor[c])
                                      for c in categories}
        if record["large_from_m2"] is not None:
            record["large_peak_load_per_m2"] = {c: own_peak_per_m2(row["large_coincident_load_per_m2"], sim_factor[c])
                                                for c in categories}
        records.append(record)
    return {
        "net_floor_area_factor": float(config["net_floor_area_factor"]),
        "load_profiles": {str(k): float(v) for k, v in config["load_profiles"].items()},
        "classes": records,
    }


def unclassified_load_class(value, table: pd.DataFrame | None) -> str | None:
    """Check ``UNCLASSIFIED_LOAD_CLASS`` (``None``: a part without a load class keeps the W/m² of its use).

    Raises:
        ValueError: Unless the value is ``None`` or a class of ``LOAD_CLASSES``.
    """
    if value is None:
        return None
    classes = [] if table is None else list(table["load_class"])
    if value not in classes:
        raise ValueError(f"UNCLASSIFIED_LOAD_CLASS must be null or a class of LOAD_CLASSES {classes}, got {value!r}")
    return value


def connection_thresholds(config, mv_threshold_kw: float) -> dict | None:
    """Check ``CONNECTION_LEVEL_THRESHOLDS_KW`` (``None``: no connection levels).

    Returns:
        ``{"station_busbar": kW, "mv": kW, "hv": kW}``: the MV threshold is ``MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW``.

    Raises:
        ValueError: Unless the thresholds are numbers with station_busbar < mv < hv.
    """
    if config is None:
        return None
    if not isinstance(config, Mapping) or set(config) != {"station_busbar", "hv"}:
        raise ValueError("CONNECTION_LEVEL_THRESHOLDS_KW must be null or {station_busbar: kW, hv: kW}, "
                         f"got {config!r}")
    busbar = _connection_kw(config["station_busbar"], "station_busbar")
    hv = _connection_kw(config["hv"], "hv")
    if not busbar < mv_threshold_kw < hv:
        raise ValueError(f"CONNECTION_LEVEL_THRESHOLDS_KW: station_busbar ({busbar:g} kW) < "
                         f"MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW ({mv_threshold_kw:g} kW) < hv ({hv:g} kW) is required")
    return {"station_busbar": busbar, "mv": float(mv_threshold_kw), "hv": hv}


def _connection_kw(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"CONNECTION_LEVEL_THRESHOLDS_KW: {name} must be a number above 0 (kW), got {value!r}")
    return float(value)


def connection_level(nonresidential_kw: float, mv_direct: bool, lv_own_peak_kw: float, thresholds: Mapping) -> str:
    """The connection level of one building (:data:`CONNECTION_LEVELS`).

    Args:
        nonresidential_kw: Own peak of the non-residential part (installed load, factor 1).
        mv_direct: Whether that part is MV-direct (above the MV threshold); it then leaves the LV grid.
        lv_own_peak_kw: Own peak of what stays in the LV grid: the residential households with pylovo's law plus a
            non-residential part that is not MV-direct.
        thresholds: :func:`connection_thresholds`.
    """
    if mv_direct:
        return "hv" if nonresidential_kw >= thresholds["hv"] else "mv"
    return "station_busbar" if lv_own_peak_kw > thresholds["station_busbar"] else "lv_feeder"
