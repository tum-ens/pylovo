"""Pure-Python topology helpers for one radial LV grid (no pandapower import needed).

pylovo grids are radial: every cable leaving the station busbar (``LVbus``) towards at least two houses starts one
outgoing feeder, as in the structural metrics (a house on its own cable from the station is not a feeder). Grids generated
before pylovo merged the transformer's street node into the busbar link that node to ``LVbus``
with a 1 m cable; both buses then count as *station buses*. These
helpers label buses and lines with that feeder and compute the cable distance of every bus
from the station, which the UI uses for "colour by feeder" and the voltage profile.
"""
from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Iterable

LINK_MAX_KM = 0.0011  # the busbar link of older pylovo grids is exactly 1 m long


def bus_role(name: str | None) -> str:
    """Role of a pylovo bus from its name."""
    name = name or ""
    if name.startswith("LVbus"):
        return "lv_busbar"
    if name.startswith("MVbus"):
        return "mv_bus"
    if name.startswith("Consumer"):
        return "consumer"
    if name.startswith("Connection"):
        return "connection"
    return "other"


def line_role(line: dict) -> str:
    """``feeder`` (street cable), ``service`` (house connection) or ``link`` (busbar link)."""
    if line.get("service_sizing_basis"):
        return "service"
    if line.get("feeder_section_id") is None and (line.get("length_km") or 0) <= LINK_MAX_KM:
        return "link"
    return "feeder"


def analyse(buses: Iterable[dict], lines: Iterable[dict]) -> dict:
    """Label every bus and line with its outgoing feeder and distance from the station.

    Args:
        buses: Rows with ``pp_index`` and ``name``.
        lines: Rows with ``pp_index``, ``from_bus``, ``to_bus``, ``length_km`` and the
            sizing columns used by :func:`line_role`.

    Returns:
        ``{"station": [...], "bus_feeder": {bus: n}, "line_feeder": {line: n},
        "distance_km": {bus: km}, "feeders": [{"feeder": n, "buses": .., "lines": ..,
        "length_km": ..}], "direct": [{"line": .., "bus": ..}]}`` with feeders numbered
        1..n by descending cable length. ``direct`` lists the cables from a station bus towards fewer than two
        consumer buses (a house on its own cable); their lines and buses carry no feeder number.
    """
    buses = list(buses)
    lines = list(lines)
    roots = [b["pp_index"] for b in buses if bus_role(b.get("name")) == "lv_busbar"]
    adjacency: dict[int, list[tuple[int, int, float]]] = defaultdict(list)
    for line in lines:
        length = float(line.get("length_km") or 0.0)
        adjacency[line["from_bus"]].append((line["to_bus"], line["pp_index"], length))
        adjacency[line["to_bus"]].append((line["from_bus"], line["pp_index"], length))

    station = set(roots)
    for line in lines:
        if line_role(line) == "link" and (line["from_bus"] in station or line["to_bus"] in station):
            station.update((line["from_bus"], line["to_bus"]))

    # Dijkstra from the station over cable length.
    distance: dict[int, float] = {b: 0.0 for b in station}
    heap = [(0.0, b) for b in station]
    heapq.heapify(heap)
    while heap:
        dist, bus = heapq.heappop(heap)
        if dist > distance.get(bus, float("inf")):
            continue
        for other, _, length in adjacency[bus]:
            new = dist + length
            if new < distance.get(other, float("inf")):
                distance[other] = new
                heapq.heappush(heap, (new, other))

    # Every cable leaving a station bus starts a branch; flood-fill away from the station. As in the structural
    # metrics (pylovo.analysis.comparison.feeder_structure), a branch is a feeder only if it supplies at least two
    # consumer buses. A house on its own cable from the station is listed in "direct" and gets no feeder number.
    consumers = {b["pp_index"] for b in buses if bus_role(b.get("name")) == "consumer"}
    bus_feeder: dict[int, int] = {}
    line_feeder: dict[int, int] = {}
    direct: list[dict] = []
    taken: set[int] = set()
    provisional = 0
    for bus in sorted(station):
        for other, line_id, _ in adjacency[bus]:
            if other in station or line_id in taken:
                continue
            branch_lines, branch_buses, stack = {line_id}, {other}, [other]
            while stack:
                current = stack.pop()
                for nxt, lid, _ in adjacency[current]:
                    if lid in branch_lines or lid in taken or nxt in station:
                        continue
                    branch_lines.add(lid)
                    if nxt not in branch_buses:
                        branch_buses.add(nxt)
                        stack.append(nxt)
            taken |= branch_lines
            if len(branch_buses & consumers) < 2:
                direct.append({"line": line_id, "bus": other})
                continue
            provisional += 1
            line_feeder.update(dict.fromkeys(branch_lines, provisional))
            for b in branch_buses:
                bus_feeder.setdefault(b, provisional)

    lengths: dict[int, float] = defaultdict(float)
    counts_lines: dict[int, int] = defaultdict(int)
    for line in lines:
        feeder = line_feeder.get(line["pp_index"])
        if feeder:
            lengths[feeder] += float(line.get("length_km") or 0.0)
            counts_lines[feeder] += 1
    order = sorted(lengths, key=lambda f: (-lengths[f], f))
    renumber = {old: new for new, old in enumerate(order, start=1)}
    bus_feeder = {b: renumber[f] for b, f in bus_feeder.items() if f in renumber}
    line_feeder = {lid: renumber[f] for lid, f in line_feeder.items() if f in renumber}
    counts_buses: dict[int, int] = defaultdict(int)
    for feeder in bus_feeder.values():
        counts_buses[feeder] += 1
    feeders = [{"feeder": renumber[f], "lines": counts_lines[f], "buses": counts_buses[renumber[f]],
                "length_km": round(lengths[f], 4)} for f in order]
    return {"station": sorted(station), "bus_feeder": bus_feeder, "line_feeder": line_feeder,
            "distance_km": distance, "feeders": feeders, "direct": direct}
