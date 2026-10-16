"""OSM MV overhead lines as corridors of the MV routing graph (pure: no database or configuration import).

With ``MV_OVERHEAD_LINES: osm`` the MV overhead lines mapped in OpenStreetMap become edges of the routing graph
(:class:`~pylovo.database.mv_graph_mixin.MvGraphMixin`), and an MV line is an overhead line exactly where its route
runs on them. :func:`is_mv_overhead_line` decides which ways of the OSM power layer count. The rules come from a
review of the OSM power lines of the test regions (2026-10-16), where untagged ``minor_line`` ways were 98 % MV
by length, MV cables were hardly mapped and LV lines were rare:

* the way is an overhead line (``power=line`` or ``power=minor_line``) in service: no lifecycle prefix on the power
  key (``disused:power`` ...), no substation internals (``line=bay|busbar|internal``), not underground, underwater,
  indoor or on a roof, and no railway current (16.7 Hz without 50 Hz);
* a ``voltage`` tag has an MV value (above 1 kV and below 60 kV, or ``medium``); shared towers of 110 kV and MV
  (``110000;20000``) count as an MV corridor;
* without a voltage tag, a ``minor_line`` counts as MV unless it has four conductors (``cables=4``, an LV line);
  a ``line`` without a voltage is left out (often a high-voltage line).
"""
from __future__ import annotations

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

_NUMBER = re.compile(r"(\d+(?:\.\d+)?)(kv|v)?")


def parse_voltages(text: Any) -> list[float]:
    """Voltages in V of an OSM ``voltage`` tag (``;`` or ``,`` separated, ``kV`` allowed); ``medium`` gives -1."""
    if text is None:
        return []
    out = []
    for part in re.split(r"[;,]", str(text)):
        value = part.strip().lower().replace(" ", "")
        if value == "medium":
            out.append(-1.0)
            continue
        match = _NUMBER.fullmatch(value)
        if match:
            volts = float(match.group(1)) * (1000.0 if match.group(2) == "kv" else 1.0)
            if volts > 0:
                out.append(volts)
    return out


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
    voltages = parse_voltages(tags.get("voltage"))
    if voltages:
        return any(v == -1.0 or MV_MIN_V < v < MV_MAX_V for v in voltages)
    return subtype == "minor_line" and str(tags.get("cables", "")).split(";")[0].strip() != "4"
