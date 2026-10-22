"""OSM MV overhead lines as corridors of the MV routing graph (pure: no database or configuration import).

The MV overhead lines mapped in OpenStreetMap become edges of the routing graph
(:class:`~pylovo.database.mv_graph_mixin.MvGraphMixin`), and an MV line is an overhead line exactly where its route
runs on them. :func:`is_mv_overhead_line` decides which ways of the OSM power layer count. The rules come from a
review of the OSM power lines of the test regions, where untagged ``minor_line`` ways were 98 % MV
by length, MV cables were hardly mapped and LV lines were rare:

* the way is an overhead line (``power=line`` or ``power=minor_line``) in service: no lifecycle prefix on the power
  key (``disused:power`` ...), no substation internals (``line=bay|busbar|internal``), not underground, underwater,
  indoor or on a roof, and no railway current (16.7 Hz without 50 Hz);
* a ``voltage`` tag has an MV value (above 1 kV and below 60 kV, or ``medium``), read by :func:`parse_voltages` as
  in the HV/MV substation filter (``20000/400``, ``20_kv`` and a bare ``20`` are 20 kV); shared towers of 110 kV
  and MV (``110000;20000``) count as an MV corridor;
* without a voltage tag, a ``minor_line`` counts as MV unless it has four conductors (``cables=4``, an LV line);
  a ``line`` without a voltage is left out (often a high-voltage line).
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping

#: OSM subtypes (``power`` values) of overhead lines.
OVERHEAD_SUBTYPES: tuple[str, ...] = ("line", "minor_line")
#: Lifecycle prefixes of the power key that mark a line out of service.
LIFECYCLE_PREFIXES: tuple[str, ...] = ("abandoned", "construction", "demolished", "destroyed", "disused",
                                       "planned", "proposed", "razed", "removed")
#: ``line`` values of substation internals.
INTERNAL_LINES: tuple[str, ...] = ("bay", "busbar", "internal")
#: ``location`` values of lines that are no MV overhead corridor.
EXCLUDED_LOCATIONS: tuple[str, ...] = ("underground", "underwater", "indoor", "roof")
#: MV voltage range in V (exclusive bounds).
MV_MIN_V: float = 1000.0
MV_MAX_V: float = 60000.0

_NUMBER = r"(\d+(?:\.\d+)?)"
_VOLTAGE_PART = re.compile(rf"^{_NUMBER}\s*(kv|v)?$")
_SEPARATORS = re.compile(r"[;/|]")
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d{1,2}(?!\d))")


def _tag_parts(text: Any) -> list[str]:
    """The lower-case parts of a multi-valued OSM tag: split at ``;``, ``/``, ``|`` and a comma between values
    (``10,5`` is a decimal comma); ``_`` reads as a space (``20_kv``)."""
    if text is None or (isinstance(text, float) and math.isnan(text)):
        return []
    parts = []
    for chunk in _SEPARATORS.split(str(text).strip().lower().replace("_", " ")):
        parts.extend(part.strip() for part in _DECIMAL_COMMA.sub(".", chunk).split(","))
    return [part for part in parts if part]


def parse_voltages(text: Any) -> set[int]:
    """The voltages (V) of an OSM ``voltage`` tag, for the MV lines and the HV/MV substation filter alike.

    Each part of the tag (:func:`_tag_parts`) is a number with an optional unit ``V`` or ``kV`` (``110 kV``,
    ``110kV``, ``20_kv``). A number without unit is in volts if it is at least 1,000. A smaller number is read as kV
    (``110`` is 110 kV, ``20`` is 20 kV, ``0.4`` is 400 V), except an integer next to a value of at least 1,000 V in
    the same tag, which is an LV value in volts (``20000;400`` and ``20000/400`` are 20 kV and 400 V). Parts that are
    no number (``medium``, ``unknown``) are skipped (:func:`is_medium_voltage`).

    Returns:
        The distinct voltages in volts (empty if the tag is missing or holds no number).
    """
    parts = [(float(m.group(1)), m.group(2)) for m in map(_VOLTAGE_PART.match, _tag_parts(text)) if m]
    volts_context = any(unit is None and value >= 1000 for value, unit in parts)
    voltages: set[int] = set()
    for value, unit in parts:
        if unit == "kv" or (unit is None and value < 1000 and not (volts_context and value.is_integer())):
            value *= 1000.0
        if value >= 1.0:
            voltages.add(int(round(value)))
    return voltages


def is_medium_voltage(text: Any) -> bool:
    """Whether an OSM ``voltage`` tag names the level ``medium`` instead of a number."""
    return "medium" in _tag_parts(text)


def is_railway_current(frequency: Any) -> bool:
    """Whether an OSM ``frequency`` tag is railway current (16.7 Hz without 50 Hz)."""
    if frequency is None:
        return False
    parts = [p.strip() for p in str(frequency).split(";")]
    return any(p.startswith(("16.7", "16,7")) for p in parts) and "50" not in parts


def is_mv_overhead_line(subtype: Any, tags: Mapping[str, Any] | None) -> bool:
    """Whether an OSM power way is an MV overhead line in service (the rules of the module docstring).

    Args:
        subtype: The power value of the way (``osm_subtype`` of the InfDB OSM layer).
        tags: Its OSM tags (missing: none).
    """
    if subtype not in OVERHEAD_SUBTYPES:
        return False
    tags = tags or {}
    if any(str(key).partition(":")[0] in LIFECYCLE_PREFIXES and str(key).endswith(":power") for key in tags):
        return False
    if tags.get("line") in INTERNAL_LINES or tags.get("location") in EXCLUDED_LOCATIONS:
        return False
    if is_railway_current(tags.get("frequency")):
        return False
    voltages, medium = parse_voltages(tags.get("voltage")), is_medium_voltage(tags.get("voltage"))
    if voltages or medium:
        return medium or any(MV_MIN_V < v < MV_MAX_V for v in voltages)
    return subtype == "minor_line" and str(tags.get("cables", "")).split(";")[0].strip() != "4"
