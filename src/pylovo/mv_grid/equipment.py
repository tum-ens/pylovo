"""MV equipment: line types, mixed cable and overhead lines, HV/MV transformer types and their sizing.

The types are pandapower standard types, resolved once per MV variant into the frozen records
:class:`~pylovo.mv_grid.model.LineType` and :class:`~pylovo.mv_grid.model.HvMvType` (and stored in the variant
snapshot by :class:`~pylovo.mv_grid.parameters.MvParameters`). pandapower is imported on the first lookup only.

* Lines: one standard cable and one overhead line type per nominal voltage, by default the most used types in
  German MV grids (:data:`DEFAULT_LINE_TYPES`; ``MV_STANDARD_CABLE`` and ``MV_OVERHEAD_LINE`` choose others; with
  ``MV_NOMINAL_VOLTAGE_KV`` auto the 10 kV districts take the same conductor for 10 kV,
  :func:`line_type_name_at`). A line that is partly cable (inside settlements) and partly overhead line gets
  length-weighted parameters (:func:`mixed_parameters`).
* HV/MV transformers: the pandapower types ``25/40/63 MVA 110/20 kV`` (``110/10 kV`` at 10 kV). pandapower has no
  31.5 MVA type; it is interpolated between 25 and 40 MVA with rounded data-sheet values
  (:data:`INTERPOLATED_HV_MV`) and flagged ``interpolated``. Other sizes are rejected.
* Sizing (:func:`size_hv_mv`): two units by default, the smallest size that carries the district peak with one unit
  out of service; known OSM ratings win.

The module is pure apart from the pandapower type tables: it imports no database or configuration module.
"""
from __future__ import annotations

import dataclasses
import difflib
import functools
import math
import re
from typing import Any, Sequence

from pylovo.mv_grid.model import LINE_KINDS, HvMvType, LineType

#: Starting (cable, overhead line) pandapower types per nominal voltage. Older, narrower conductors remain
#: selectable through ``MV_STANDARD_CABLE`` and ``MV_OVERHEAD_LINE`` and stored snapshots retain their types.
DEFAULT_LINE_TYPES: dict[float, tuple[str, str]] = {
    20.0: ("NA2XS2Y 1x240 RM/25 12/20 kV", "122-AL1/20-ST1A 20.0"),
    10.0: ("NA2XS2Y 1x240 RM/25 6/10 kV", "122-AL1/20-ST1A 10.0"),
}

#: pandapower line ``type`` per line kind of the MV stage.
PANDAPOWER_LINE_KIND: dict[str, str] = {"cs": "cable", "ol": "overhead"}

#: Insulation voltage ``U0/U`` of the MV cables per nominal voltage, as in the pandapower type names.
CABLE_INSULATION: dict[float, str] = {20.0: "12/20", 10.0: "6/10"}

#: HV side of the HV/MV transformers in kV.
HV_KV: float = 110.0

#: Interpolated HV/MV transformer data (rounded data-sheet values between the pandapower types of 25 and 40 MVA:
#: linear interpolation gives vk 13.8 %, vkr 0.380 %, pfe 15.7 kW, i0 0.061 %); the other values are those of the
#: next larger pandapower type.
INTERPOLATED_HV_MV: dict[float, dict[str, float]] = {
    31.5: {"vk_percent": 14.0, "vkr_percent": 0.38, "pfe_kw": 16.0, "i0_percent": 0.06},
}

_CABLE_VOLTAGE = re.compile(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*kV\s*$")
_OVERHEAD_VOLTAGE = re.compile(r"\s(\d+(?:\.\d+)?)\s*$")
_LINE_PARAMETERS: tuple[str, ...] = ("r_ohm_per_km", "x_ohm_per_km", "c_nf_per_km", "max_i_ka")


def _apparent_mva(value: float | None) -> float:
    """A non-negative apparent power; None, NaN and negative values count as 0."""
    if value is None:
        return 0.0
    value = float(value)
    return value if math.isfinite(value) and value > 0 else 0.0


@functools.lru_cache(maxsize=1)
def _std_types() -> dict[str, dict[str, Any]]:
    """The standard type tables of pandapower (the types of every new pandapower net)."""
    import pandapower as pp

    return pp.create_empty_network().std_types


# ---------------------------------------------------------------------------------------------- lines
def rated_kv(name: str) -> float | None:
    """The rated voltage in kV in the name of a pandapower line type, or None.

    Cables end with ``U0/U kV`` (``NA2XS2Y 1x240 RM/25 12/20 kV``: 20), overhead lines with the voltage
    (``122-AL1/20-ST1A 20.0``: 20).
    """
    match = _CABLE_VOLTAGE.search(name)
    if match:
        return float(match.group(2))
    match = _OVERHEAD_VOLTAGE.search(name)
    if match:
        return float(match.group(1))
    return None


@functools.lru_cache(maxsize=None)
def line_type(name: str) -> LineType:
    """The pandapower line type ``name`` as a :class:`~pylovo.mv_grid.model.LineType`.

    The kind follows the pandapower ``type`` (``cs`` cable, ``ol`` overhead line), the voltage the name
    (:func:`rated_kv`).

    Raises:
        ValueError: If pandapower has no line type of this name (with the closest names), or if the type is
            neither a cable nor an overhead line or names no rated voltage.
    """
    catalogue = _std_types()["line"]
    data = catalogue.get(name) if isinstance(name, str) else None
    if data is None:
        close = difflib.get_close_matches(str(name), list(catalogue), n=3)
        hint = f" (closest: {', '.join(close)})" if close else ""
        raise ValueError(f"{name!r} is not a pandapower line type{hint}")
    kind = PANDAPOWER_LINE_KIND.get(data.get("type"))
    if kind is None:
        raise ValueError(f"pandapower line type {name!r} has type {data.get('type')!r}, neither a cable ('cs') nor "
                         "an overhead line ('ol')")
    vn_kv = rated_kv(name)
    if vn_kv is None:
        raise ValueError(f"pandapower line type {name!r} names no rated voltage (cables end with 'U0/U kV', "
                         "overhead lines with the voltage)")
    return LineType(name=name, kind=kind, vn_kv=vn_kv, **{key: float(data[key]) for key in _LINE_PARAMETERS})


def line_type_name_at(name: str, vn_kv: float) -> str:
    """The pandapower name of the same conductor for another nominal voltage (``MV_NOMINAL_VOLTAGE_KV`` auto).

    A name rated for ``vn_kv`` is returned as it is. A cable takes the insulation of the voltage
    (:data:`CABLE_INSULATION`: ``NA2XS2Y 1x150 RM/25 12/20 kV`` -> ``NA2XS2Y 1x150 RM/25 6/10 kV``), an overhead
    line the voltage at the end of its name (``48-AL1/8-ST1A 20.0`` -> ``48-AL1/8-ST1A 10.0``).

    Raises:
        ValueError: If the name rates no voltage, a cable has no insulation for ``vn_kv``, or pandapower has no
            type of that name.
    """
    vn = float(vn_kv)
    rated = rated_kv(name)
    if rated is None:
        raise ValueError(f"{name!r} names no rated voltage (cables end with 'U0/U kV', overhead lines with the "
                         "voltage)")
    if math.isclose(rated, vn):
        return name
    cable = _CABLE_VOLTAGE.search(name)
    if cable is not None:
        if vn not in CABLE_INSULATION:
            raise ValueError(f"there is no cable insulation for {vn:g} kV (known: "
                             f"{', '.join(f'{v:g}' for v in CABLE_INSULATION)} kV)")
        other = f"{name[:cable.start()]}{CABLE_INSULATION[vn]} kV"
    else:
        other = f"{name[:_OVERHEAD_VOLTAGE.search(name).start()]} {vn:.1f}"
    if other not in _std_types()["line"]:
        raise ValueError(f"pandapower has no {vn:g} kV type of the conductor of {name!r} ({other!r})")
    return other


@functools.lru_cache(maxsize=None)
def upgrade_ladder(kind: str, vn_kv: float) -> tuple[LineType, ...]:
    """The pandapower conductors of one kind (``cable`` or ``overhead``) rated for ``vn_kv``, by ampacity: the steps
    of the conductor upgrade in the repair loop (Ding0's reinforcement takes the next larger type)."""
    found = []
    for name, data in _std_types()["line"].items():
        rated = rated_kv(name)
        if PANDAPOWER_LINE_KIND.get(data.get("type")) == kind and rated is not None and math.isclose(rated, vn_kv):
            found.append(line_type(name))
    return tuple(sorted(found, key=lambda lt: (lt.max_i_ka, lt.name)))


def upgraded_line_type(name: str, vn_kv: float, min_i_ka: float | None = None) -> str | None:
    """The next larger conductor of the same kind as the pandapower type ``name`` at ``vn_kv`` (with ``min_i_ka``:
    the smallest one that carries this current, else the largest); None if there is no larger one."""
    current = line_type(name)
    larger = [lt for lt in upgrade_ladder(current.kind, float(vn_kv)) if lt.max_i_ka > current.max_i_ka + 1e-9]
    if not larger:
        return None
    if min_i_ka is not None:
        return next((lt.name for lt in larger if lt.max_i_ka >= float(min_i_ka) - 1e-9), larger[-1].name)
    return larger[0].name


def type_rank(name: str) -> tuple[float, ...]:
    """The ampacities of the parts of a line type name (``<cable> + <overhead>`` for a mixed line), to compare
    conductors of the same kind."""
    return tuple(line_type(part.strip()).max_i_ka for part in str(name).split(" + "))


def default_line_types(vn_kv: float) -> tuple[str, str]:
    """The default (cable, overhead line) type names of a nominal voltage (20 or 10 kV).

    Raises:
        ValueError: For another voltage.
    """
    try:
        return DEFAULT_LINE_TYPES[float(vn_kv)]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"there are default MV line types for {' and '.join(f'{v:g}' for v in DEFAULT_LINE_TYPES)} "
                         f"kV only, got {vn_kv!r}") from None


def check_line_type(lt: LineType, vn_kv: float, kind: str) -> None:
    """Check that ``lt`` is a line type of the given kind (``cable`` or ``overhead``) for the nominal voltage.

    Raises:
        ValueError: On a kind or voltage mismatch.
    """
    if kind not in ("cable", "overhead"):
        raise ValueError(f"the kind of a line type must be cable or overhead, got {kind!r}")
    if lt.kind != kind:
        raise ValueError(f"{lt.name} is a pandapower {lt.kind} type, not a {kind} type")
    if not math.isclose(lt.vn_kv, float(vn_kv)):
        raise ValueError(f"{lt.name} is rated for {lt.vn_kv:g} kV, not for {float(vn_kv):g} kV")


def mixed_parameters(cable: LineType, overhead: LineType, cable_m: float, overhead_m: float) -> dict[str, float]:
    """Electrical parameters of a line that is ``cable_m`` cable and ``overhead_m`` overhead line.

    ``r``, ``x`` and ``c`` per km are length-weighted; the ampacity is the smaller one of the parts with a
    positive length.

    Returns:
        ``r_ohm_per_km``, ``x_ohm_per_km``, ``c_nf_per_km`` and ``max_i_ka``.

    Raises:
        ValueError: If a length is negative or not finite, or both are 0.
    """
    lengths = (float(cable_m), float(overhead_m))
    if not all(math.isfinite(m) and m >= 0 for m in lengths) or sum(lengths) <= 0:
        raise ValueError(f"a mixed line needs non-negative lengths with a positive sum, got cable {cable_m!r} m and "
                         f"overhead {overhead_m!r} m")
    total = sum(lengths)
    parts = tuple(zip((cable, overhead), (m / total for m in lengths)))
    values = {key: sum(share * getattr(lt, key) for lt, share in parts) for key in _LINE_PARAMETERS[:3]}
    values["max_i_ka"] = min(lt.max_i_ka for lt, share in parts if share > 0)
    return values


def line_parameters(kind: str, cable: LineType, overhead: LineType, cable_m: float = 0.0,
                    overhead_m: float = 0.0) -> dict[str, float]:
    """Electrical parameters of a line of the given kind (``cable``, ``overhead`` or ``mixed``).

    Returns:
        ``r_ohm_per_km``, ``x_ohm_per_km``, ``c_nf_per_km`` and ``max_i_ka`` of the cable or the overhead type,
        or :func:`mixed_parameters` of the two lengths for a mixed line.

    Raises:
        ValueError: For another kind, or invalid lengths of a mixed line.
    """
    if kind == "cable":
        return {key: getattr(cable, key) for key in _LINE_PARAMETERS}
    if kind == "overhead":
        return {key: getattr(overhead, key) for key in _LINE_PARAMETERS}
    if kind == "mixed":
        return mixed_parameters(cable, overhead, cable_m, overhead_m)
    raise ValueError(f"the kind of a line must be one of {', '.join(LINE_KINDS)}, got {kind!r}")


def line_std_type_name(kind: str, cable: LineType, overhead: LineType) -> str:
    """The type name of a line of the given kind: the cable or overhead type, ``'<cable> + <overhead>'`` if mixed.

    Raises:
        ValueError: For another kind.
    """
    if kind == "cable":
        return cable.name
    if kind == "overhead":
        return overhead.name
    if kind == "mixed":
        return f"{cable.name} + {overhead.name}"
    raise ValueError(f"the kind of a line must be one of {', '.join(LINE_KINDS)}, got {kind!r}")


def thermal_mva(lt: LineType) -> float:
    """Thermal rating of one circuit in MVA: ``sqrt(3) * vn_kv * max_i_ka`` (11.1 MVA for the default 1x150 cable
    and 14.6 MVA for 1x240 at 20 kV)."""
    return math.sqrt(3.0) * lt.vn_kv * lt.max_i_ka


def parallel_count(s_mva: float, lt: LineType, utilization: float, max_parallel: int) -> tuple[int, bool]:
    """Parallel circuits of type ``lt`` needed for ``s_mva`` at the given utilization of the thermal rating.

    ``n = ceil(s_mva / (utilization * thermal_mva(lt)))``, at least 1, capped at ``max_parallel``.

    Returns:
        ``(n, overloaded)``; ``overloaded`` is True when more than ``max_parallel`` circuits would be needed.

    Raises:
        ValueError: If ``utilization`` or ``max_parallel`` is not positive.
    """
    if not utilization > 0 or max_parallel < 1:
        raise ValueError(f"utilization and max_parallel must be positive, got {utilization!r} and {max_parallel!r}")
    needed = max(1, math.ceil(_apparent_mva(s_mva) / (utilization * thermal_mva(lt)) - 1e-9))
    return min(needed, int(max_parallel)), needed > max_parallel


# ---------------------------------------------------------------------------------------------- HV/MV transformers
def _hv_mv_name(sn_mva: float, vn_lv_kv: float) -> str:
    return f"{sn_mva:g} MVA {HV_KV:g}/{vn_lv_kv:g} kV"


def _hv_mv_record(name: str, data: dict[str, Any]) -> HvMvType:
    return HvMvType(
        name=name,
        sn_mva=float(data["sn_mva"]),
        vn_hv_kv=float(data["vn_hv_kv"]),
        vn_lv_kv=float(data["vn_lv_kv"]),
        vk_percent=float(data["vk_percent"]),
        vkr_percent=float(data["vkr_percent"]),
        pfe_kw=float(data["pfe_kw"]),
        i0_percent=float(data["i0_percent"]),
        shift_degree=float(data.get("shift_degree", 0.0)),
        vector_group=str(data.get("vector_group", "")),
        tap_side=str(data.get("tap_side", "hv")),
        tap_neutral=int(data.get("tap_neutral", 0)),
        tap_min=int(data.get("tap_min", 0)),
        tap_max=int(data.get("tap_max", 0)),
        tap_step_percent=float(data.get("tap_step_percent", 0.0)),
    )


def _catalogue_hv_mv(vn_lv_kv: float) -> dict[float, HvMvType]:
    """The pandapower HV/MV types (110 kV to ``vn_lv_kv``) by rating."""
    catalogue: dict[float, HvMvType] = {}
    for name, data in _std_types()["trafo"].items():
        if math.isclose(float(data["vn_hv_kv"]), HV_KV) and math.isclose(float(data["vn_lv_kv"]), vn_lv_kv):
            catalogue[float(data["sn_mva"])] = _hv_mv_record(name, data)
    return dict(sorted(catalogue.items()))


def _interpolated_hv_mv(sn_mva: float, vn_lv_kv: float, catalogue: dict[float, HvMvType]) -> HvMvType:
    upper = next((s for s in catalogue if s > sn_mva), None)
    if sn_mva not in INTERPOLATED_HV_MV or upper is None:
        available = sorted({*catalogue, *(s for s in INTERPOLATED_HV_MV if any(c > s for c in catalogue))})
        raise ValueError(f"there is no HV/MV transformer type of {sn_mva:g} MVA for {HV_KV:g}/{vn_lv_kv:g} kV "
                         f"(available: {', '.join(f'{s:g}' for s in available) or 'none'} MVA)")
    return dataclasses.replace(catalogue[upper], **INTERPOLATED_HV_MV[sn_mva], name=_hv_mv_name(sn_mva, vn_lv_kv),
                               sn_mva=float(sn_mva), interpolated=True)


def hv_mv_types(vn_lv_kv: float, sizes_mva: Sequence[float]) -> tuple[HvMvType, ...]:
    """The HV/MV transformer types (110 kV to ``vn_lv_kv``) of the given ratings, in the given order.

    pandapower types where they exist (25, 40, 63 MVA) and 31.5 MVA with the values of
    :data:`INTERPOLATED_HV_MV` (``interpolated = True``).

    Raises:
        ValueError: For another rating, or a voltage without pandapower HV/MV types.
    """
    vn = float(vn_lv_kv)
    catalogue = _catalogue_hv_mv(vn)
    if not catalogue:
        raise ValueError(f"pandapower has no HV/MV transformer types for {HV_KV:g}/{vn:g} kV")
    types = []
    for size in sizes_mva:
        sn = float(size)
        types.append(catalogue[sn] if sn in catalogue else _interpolated_hv_mv(sn, vn, catalogue))
    return tuple(types)


def hv_mv_std_type(t: HvMvType) -> dict[str, Any]:
    """The pandapower standard type data of ``t`` (for ``pandapower.create_std_type(net, data, t.name, "trafo")``).

    The tap changer is a ratio tap changer (``tap_changer_type = "Ratio"``); without it pandapower 3 ignores the
    tap position.
    """
    return {
        "sn_mva": t.sn_mva, "vn_hv_kv": t.vn_hv_kv, "vn_lv_kv": t.vn_lv_kv, "vk_percent": t.vk_percent,
        "vkr_percent": t.vkr_percent, "pfe_kw": t.pfe_kw, "i0_percent": t.i0_percent,
        "shift_degree": t.shift_degree, "vector_group": t.vector_group, "tap_side": t.tap_side,
        "tap_neutral": t.tap_neutral, "tap_min": t.tap_min, "tap_max": t.tap_max,
        "tap_step_percent": t.tap_step_percent, "tap_step_degree": 0.0, "tap_changer_type": "Ratio",
    }


def add_hv_mv_std_type(net, t: HvMvType) -> str:
    """Register ``t`` (resolved values, e.g. from a variant snapshot) as a transformer type of ``net``.

    An existing type of the same name is replaced, so the net uses exactly the resolved values.

    Returns:
        The type name, for ``pandapower.create_transformer(..., std_type=name)``.
    """
    import pandapower as pp

    pp.create_std_type(net, hv_mv_std_type(t), t.name, element="trafo", overwrite=True)
    return t.name


def size_hv_mv(s_load_mva: float, s_feed_mva: float, sizes_mva: Sequence[float], units: int,
               n1_utilization: float, known_units_mva: Sequence[float] = ()) -> tuple[int, float, str]:
    """Number and rating of the HV/MV transformers of a substation (DESIGN section 4.6).

    * Two or more known ratings (OSM): ``n = len(known)``, ``sn = min(known)``, sizing ``osm_rating``.
    * Otherwise ``n = units`` and ``sn`` = the smallest size whose ``n - 1`` units carry the peak
      ``max(s_load_mva, s_feed_mva)`` at ``n1_utilization`` (one unit out of service; with ``units = 1`` there is
      no reserve and the single unit carries the peak), sizing ``sized``.
    * If no size fits: ``sn`` = the largest size and as many units as the peak needs plus the reserve unit,
      sizing ``large_substation`` (70 MVA: 3 x 63 MVA).

    Args:
        s_load_mva: Coincident apparent load of all district nodes in MVA.
        s_feed_mva: Largest apparent power of the feed-in cases at the busbar in MVA (NaN or None: not known).
        sizes_mva: Available ratings in MVA (``hv_mv_sizes_mva``).
        units: Number of units of a sized substation (``hv_mv_units``).
        n1_utilization: Allowed loading of the remaining units with one unit out (``hv_mv_n1_utilization``).
        known_units_mva: Ratings of the known units of the substation (OSM tags), or empty.

    Returns:
        ``(n_units, unit_mva, sizing)`` with sizing ``osm_rating``, ``sized`` or ``large_substation``.

    Raises:
        ValueError: Without sizes, with fewer than one unit or a non-positive utilization.
    """
    known = sorted(_apparent_mva(s) for s in known_units_mva)
    known = [s for s in known if s > 0]
    if len(known) >= 2:
        return len(known), known[0], "osm_rating"
    sizes = sorted(float(s) for s in sizes_mva if _apparent_mva(s) > 0)
    if not sizes:
        raise ValueError(f"size_hv_mv needs at least one positive transformer size, got {list(sizes_mva)!r}")
    units = int(units)
    if units < 1 or not n1_utilization > 0:
        raise ValueError(f"size_hv_mv needs units >= 1 and a positive utilization, got {units!r} and "
                         f"{n1_utilization!r}")
    peak = max(_apparent_mva(s_load_mva), _apparent_mva(s_feed_mva))
    reserve = 1 if units > 1 else 0
    for size in sizes:
        if (units - reserve) * size * n1_utilization >= peak:
            return units, size, "sized"
    largest = sizes[-1]
    return math.ceil(peak / (largest * n1_utilization) - 1e-9) + reserve, largest, "large_substation"
