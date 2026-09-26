"""Cable distribution cabinets (KVS): pylovo's feeder split points, named K1…Kn.

One definition for the Grid inspector, the map and the grid diagnostics, so all of them show the
same names. A split point (``pylovo.split_points``, the routing vertex where a feeder branches) is a
cabinet if its vertex belongs to a connection bus of the pandapower net that is not part of the
station. Cabinets are numbered by cable distance from the station, ties by bus index.
"""
from __future__ import annotations

from typing import Any


def vertex_of(name: str | None) -> int | None:
    """Routing vertex in a pandapower bus name (``'Connection Nodebus 371'`` -> 371)."""
    digits = ""
    for ch in reversed(name or ""):
        if not ch.isdigit():
            break
        digits = ch + digits
    return int(digits) if digits else None


def connection_vertices(buses: list[dict]) -> dict[int, int]:
    """Vertex -> ``pp_index`` of the connection buses (``role == 'connection'``)."""
    out = {}
    for bus in buses:
        vertex = vertex_of(bus.get("name"))
        if vertex is not None and bus.get("role") == "connection":
            out[vertex] = bus["pp_index"]
    return out


def number_cabinets(splits: list[dict], vertex_bus: dict[int, int], station: set[int],
                    distance_m: dict[int, float]) -> list[dict[str, Any]]:
    """Cabinets of one grid in K order.

    Args:
        splits: ``split_points`` rows (``split_bus``, ``outgoing_count``).
        vertex_bus: Routing vertex -> bus ``pp_index`` of the connection buses.
        station: Bus indices of the station (busbar and its link nodes).
        distance_m: Cable distance of each bus from the station in metres.

    Returns:
        ``[{"name": "K1", "bus", "split_bus", "outgoing", "distance_m"}, …]``.
    """
    found = []
    for split in splits:
        bus = vertex_bus.get(int(split["split_bus"])) if split.get("split_bus") is not None else None
        if bus is None or bus in station:
            continue
        found.append((distance_m.get(bus, 0.0), bus, split))
    found.sort(key=lambda t: (t[0], t[1]))
    return [{"name": f"K{i}", "bus": bus, "split_bus": split["split_bus"], "outgoing": split.get("outgoing_count"),
             "distance_m": round(dist, 1)} for i, (dist, bus, split) in enumerate(found, start=1)]
