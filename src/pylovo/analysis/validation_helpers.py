"""
Utility helpers for reading validation nets and exporting simple geodata views.
"""
import copy
import csv
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

import geopandas as gpd
import networkx as nx
import pandapower as pp
from tqdm import tqdm
from shapely.geometry import LineString, Point

# Subnets (real or synthetic) with fewer LV buses than this are classified as
# mini grids and excluded from the comparison dataset.
MINI_GRID_BUS_THRESHOLD = 5
NS_LOAD_NAME_PATTERN = re.compile(r"NS[_-]Last", re.IGNORECASE)

def iter_nets_from_json(json_path: Path):
    """
    Load single or multiple pandapower nets from a JSON file.

    Args:
        json_path: Path to JSON file containing network(s)

    Yields:
        Tuple of (index, pandapower_net)
    """
    try:
        net = pp.from_json(str(json_path))
        yield 0, net
        return
    except Exception:
        pass

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        for i, item in enumerate(data):
            tmp = json_path.parent / f"tmp_{i}.json"
            tmp.write_text(json.dumps(item), encoding="utf-8")
            yield i, pp.from_json(str(tmp))
            tmp.unlink(missing_ok=True)
    elif isinstance(data, dict):
        for i, item in enumerate(data.values()):
            tmp = json_path.parent / f"tmp_{i}.json"
            tmp.write_text(json.dumps(item), encoding="utf-8")
            yield i, pp.from_json(str(tmp))
            tmp.unlink(missing_ok=True)


def get_bus_line_geo(net, net_index: int, projection: str):
    """
    Extract bus and line geodata from a pandapower network.

    Args:
        net: Pandapower network
        net_index: Index identifier for the network
        projection: EPSG projection string (e.g., "epsg:25832")

    Returns:
        Tuple of (line_geodataframe, bus_geodataframe)
    """
    pp.plotting.plotly.geo_data_to_latlong(net, projection)

    # Lines
    line_geo_df = net.line_geodata.copy()
    if not line_geo_df.empty:
        lines = [LineString(c) if isinstance(c, (list, tuple)) and len(c) > 1 else None
                 for c in line_geo_df["coords"]]
        gdf_line = gpd.GeoDataFrame(line_geo_df, geometry=lines, crs="EPSG:4326")
        gdf_line["net"] = net_index
        gdf_line = gdf_line.merge(net.line, left_index=True, right_index=True, how="left")
        gdf_line = gdf_line[~gdf_line.geometry.isna()]
    else:
        gdf_line = gpd.GeoDataFrame(columns=["net", "geometry"], crs="EPSG:4326")

    # Buses
    bus_geo_df = net.bus_geodata.copy()
    if not bus_geo_df.empty:
        gdf_bus = gpd.GeoDataFrame(
            bus_geo_df,
            geometry=[Point(xy) for xy in zip(bus_geo_df["x"], bus_geo_df["y"])],
            crs="EPSG:4326"
        )
        gdf_bus["net"] = net_index
        gdf_bus = gdf_bus.merge(net.bus, left_index=True, right_index=True, how="left")
        gdf_bus["consumer_bus"] = gdf_bus.get("name", "").astype(str).str.contains("Consumer Nodebus", na=False)
    else:
        gdf_bus = gpd.GeoDataFrame(columns=["net", "consumer_bus", "geometry"], crs="EPSG:4326")

    return gdf_line, gdf_bus


def extract_mv_grid(net: pp.pandapowerNet, output_dir: Path | str) -> Path | None:
    """Extract and persist the MV part of a SWF validation net.

    The SWF source data encodes MV buses via `chr_name` prefixes. The extracted
    MV net keeps both MV buses and the transformer buses needed to preserve the
    interface to downstream LV grids.
    """
    output_dir = Path(output_dir)
    mv_dir = output_dir / "mv"
    mv_dir.mkdir(parents=True, exist_ok=True)

    if "chr_name" not in net.bus.columns:
        raise ValueError("Cannot extract the MV grid because the bus table has no 'chr_name' column.")

    mv_buses = net.bus[net.bus["chr_name"].fillna("").str.startswith("5", na=False)].index.tolist()
    trafo_buses = list(net.trafo["hv_bus"]) + list(net.trafo["lv_bus"])
    buses_to_keep = sorted(set(mv_buses + trafo_buses))
    if not buses_to_keep:
        return None

    mv_net = pp.select_subnet(net, buses=buses_to_keep, include_results=False)
    mv_net.name = "MV_5001"

    xlsx_path = mv_dir / "MV_5001.xlsx"
    pp.to_excel(mv_net, str(xlsx_path))
    return xlsx_path


def _is_false(value) -> bool:
    """Return True for common boolean false encodings found in DSO exports."""
    if isinstance(value, bool):
        return not value
    if value is None:
        return False
    return str(value).strip().lower() in {"false", "falsch", "0", "no", "nein"}


def _is_in_service(row) -> bool:
    return not _is_false(row.get("in_service", True))


def _lv_subnet_id_from_chr_name(value) -> str | None:
    value = str(value)
    if len(value) > 4 and value.startswith("7"):
        return value[1:4]
    return None


def _active_line_switch_elements(net: pp.pandapowerNet, open_only: bool = True) -> set[int]:
    if not hasattr(net, "switch") or net.switch.empty:
        return set()

    line_elements: set[int] = set()
    for _, sw_row in net.switch.iterrows():
        if str(sw_row.get("et", "")).lower() != "l":
            continue
        if open_only and not _is_false(sw_row.get("closed", True)):
            continue
        try:
            line_elements.add(int(sw_row["element"]))
        except (TypeError, ValueError):
            continue
    return line_elements


def _build_lv_topology(net: pp.pandapowerNet):
    """Build a switch-aware LV-only graph and return (graph, lv_bus_set).

    Only in-service lines are included. Lines with an open switch (``et='l'``,
    ``closed=False`` or equivalent text encodings) are excluded so that normally
    open ring connections between transformer service areas are severed.
    """
    bus_df = net.bus
    lv_buses = set(
        bus_df[
            bus_df["chr_name"].apply(lambda c: _lv_subnet_id_from_chr_name(c) is not None)
        ].index
    )

    open_switch_lines = _active_line_switch_elements(net, open_only=True)

    G = nx.Graph()
    G.add_nodes_from(lv_buses)
    for line_idx, line_row in net.line.iterrows():
        if not _is_in_service(line_row):
            continue
        if int(line_idx) in open_switch_lines:
            continue
        fb, tb = line_row["from_bus"], line_row["to_bus"]
        if fb in lv_buses and tb in lv_buses:
            G.add_edge(fb, tb)

    return G, lv_buses


def _assign_buses_to_trafos(
    net: pp.pandapowerNet,
    G: nx.Graph,
    lv_buses: set[int],
) -> dict[str, list[int]]:
    """Assign every LV bus to a transformer subnet using graph topology.

    For connected components with a single in-service trafo the assignment is
    trivial.  In multi-trafo components each bus is assigned to the nearest
    trafo (shortest unweighted path in the LV graph).  The subnet is named
    ``LV_XXX`` where *XXX* comes from the trafo's ``lv_bus`` ``chr_name[1:4]``.

    Returns a dict mapping subnet name (e.g. ``"LV_041"``) to a list of bus
    indices.
    """
    bus_df = net.bus

    # Map every in-service LV trafo to its sub_id.
    trafo_info: list[tuple[int, str, int]] = []  # (trafo_idx, sub_id, lv_bus)
    for tidx, trow in net.trafo.iterrows():
        if trow.get("in_service", True) is False:
            continue
        lv_bus = trow["lv_bus"]
        if lv_bus not in lv_buses:
            continue
        chr_name = str(bus_df.at[lv_bus, "chr_name"])
        if len(chr_name) > 4 and chr_name.startswith("7"):
            sub_id = chr_name[1:4]
            trafo_info.append((tidx, sub_id, lv_bus))

    trafo_lv_set = {lv_bus for _, _, lv_bus in trafo_info}
    sub_id_by_lv_bus = {lv_bus: sub_id for _, sub_id, lv_bus in trafo_info}

    components = list(nx.connected_components(G))

    subnet_buses: dict[str, list[int]] = defaultdict(list)

    for comp in components:
        comp_trafos = [(sid, lb) for _, sid, lb in trafo_info if lb in comp]

        if not comp_trafos:
            # Orphan component (no trafo) – skip.
            continue

        if len(comp_trafos) == 1:
            sid, _ = comp_trafos[0]
            subnet_buses[f"LV_{sid}"].extend(comp)
            continue

        # Multi-trafo component: assign each bus to the nearest trafo by
        # shortest unweighted path.
        subgraph = G.subgraph(comp)
        trafo_buses_in_comp = [lb for _, lb in comp_trafos]

        # BFS from every trafo simultaneously: for each bus keep the trafo
        # that reaches it first (= shortest path).
        bus_owner: dict[int, str] = {}
        for sid, lb in comp_trafos:
            for target, dist in nx.single_source_shortest_path_length(subgraph, lb).items():
                if target not in bus_owner or dist < bus_owner[target][1]:
                    bus_owner[target] = (sid, dist)

        for bus, (sid, _) in bus_owner.items():
            subnet_buses[f"LV_{sid}"].append(bus)

    return dict(subnet_buses)


def _safe_filename_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_")


def _grid_category(bus_count: int) -> str:
    return "mini" if bus_count < MINI_GRID_BUS_THRESHOLD else "regular"


def _has_ns_load_name(net: pp.pandapowerNet) -> bool:
    if not hasattr(net, "load") or net.load.empty or "name" not in net.load.columns:
        return False
    return net.load["name"].fillna("").astype(str).str.contains(NS_LOAD_NAME_PATTERN).any()


def _has_ns_load_file(net: pp.pandapowerNet) -> bool:
    if not hasattr(net, "load") or net.load.empty or "file" not in net.load.columns:
        return False
    return net.load["file"].fillna("").astype(str).str.contains(NS_LOAD_NAME_PATTERN).any()


def _has_household_load(net: pp.pandapowerNet) -> bool:
    if not hasattr(net, "load") or net.load.empty or "type" not in net.load.columns:
        return False
    return (net.load["type"].fillna("").astype(str) == "HH").any()


def _has_lv_comparison_load(net: pp.pandapowerNet) -> bool:
    return _has_ns_load_name(net) or _has_ns_load_file(net) or _has_household_load(net)


def _load_status(net: pp.pandapowerNet) -> str:
    return "lvload" if _has_lv_comparison_load(net) else "no_lvload"


def _active_lv_trafos(net: pp.pandapowerNet) -> list[tuple[int, str, int]]:
    """Return active LV trafos as (trafo_idx, sub_id, lv_bus)."""
    bus_df = net.bus
    trafo_info: list[tuple[int, str, int]] = []
    for tidx, trow in net.trafo.iterrows():
        if not _is_in_service(trow):
            continue
        lv_bus = trow["lv_bus"]
        if lv_bus not in bus_df.index:
            continue
        sub_id = _lv_subnet_id_from_chr_name(bus_df.at[lv_bus, "chr_name"])
        if sub_id is not None:
            trafo_info.append((int(tidx), sub_id, int(lv_bus)))
    return trafo_info


def _logical_lv_bus_groups(net: pp.pandapowerNet) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for bus_idx, row in net.bus.iterrows():
        sub_id = _lv_subnet_id_from_chr_name(row.get("chr_name"))
        if sub_id is not None:
            groups[sub_id].append(int(bus_idx))
    return dict(groups)


def _add_feeder_metadata(
    net: pp.pandapowerNet,
    source_net: pp.pandapowerNet,
    trafo_row,
    add_ext_grid: bool = True,
) -> None:
    """Add feeder metadata to an LV subnet."""
    lv_bus = int(trafo_row["lv_bus"])
    if lv_bus not in net.bus.index:
        return

    if add_ext_grid and (net.ext_grid.empty or lv_bus not in set(net.ext_grid["bus"].tolist())):
        pp.create_ext_grid(net, bus=lv_bus, name=f"Feed_from_{trafo_row['name']}")

    hv_bus_orig = trafo_row["hv_bus"]
    vn_hv_kv = (
        float(source_net.bus.at[hv_bus_orig, "vn_kv"])
        if hv_bus_orig in source_net.bus.index
        else float(trafo_row.get("vn_hv_kv", 20.0))
    )
    hv_dummy = pp.create_bus(
        net,
        vn_kv=vn_hv_kv,
        name=f"HV_{trafo_row['name']}",
        type="b",
        in_service=False,
    )
    new_trafo_idx = pp.create_transformer_from_parameters(
        net,
        hv_bus=hv_dummy,
        lv_bus=lv_bus,
        sn_mva=float(trafo_row["sn_mva"]),
        vn_hv_kv=vn_hv_kv,
        vn_lv_kv=float(trafo_row.get("vn_lv_kv", 0.4)),
        vk_percent=float(trafo_row.get("vk_percent", 4.0)),
        vkr_percent=float(trafo_row.get("vkr_percent", 1.0)),
        pfe_kw=float(trafo_row.get("pfe_kw", 0.0)),
        i0_percent=float(trafo_row.get("i0_percent", 0.0)),
        name=str(trafo_row.get("name", "")),
        in_service=False,
    )
    for col, val in trafo_row.items():
        if col in {"hv_bus", "in_service"} or str(col).startswith("Unnamed"):
            continue
        if col not in net.trafo.columns:
            net.trafo[col] = None
        net.trafo.at[new_trafo_idx, col] = val


def _build_logical_lv_net(
    source_net: pp.pandapowerNet,
    sub_id: str,
    core_buses: list[int],
    trafo_rows,
) -> pp.pandapowerNet:
    lv_net = pp.select_subnet(source_net, buses=core_buses, include_results=False)
    lv_net.name = f"LV_{sub_id}"
    for _, trafo_row in trafo_rows.iterrows():
        _add_feeder_metadata(lv_net, source_net, trafo_row)
    return lv_net


def _mark_open_switch_lines_out_of_service(net: pp.pandapowerNet) -> int:
    open_lines = _active_line_switch_elements(net, open_only=True)
    if not open_lines or net.line.empty:
        return 0
    if "split_removed_reason" not in net.line.columns:
        net.line["split_removed_reason"] = None
    removed = 0
    for line_idx in sorted(open_lines):
        if line_idx in net.line.index and _is_in_service(net.line.loc[line_idx]):
            net.line.at[line_idx, "in_service"] = False
            net.line.at[line_idx, "split_removed_reason"] = "open_switch"
            removed += 1
    return removed


def _radialize_lv_net(net: pp.pandapowerNet, root_bus: int | None) -> tuple[pp.pandapowerNet, dict[str, int | str]]:
    """Return a radialized copy by keeping a length-weighted spanning tree."""
    radial_net = copy.deepcopy(net)
    open_switch_removed = _mark_open_switch_lines_out_of_service(radial_net)

    if radial_net.line.empty:
        return radial_net, {
            "open_switch_lines_removed": open_switch_removed,
            "cycle_lines_removed": 0,
            "components_after_radialization": len(radial_net.bus),
            "cycles_after_radialization": 0,
            "radialization_status": "no_lines",
        }

    if "split_removed_reason" not in radial_net.line.columns:
        radial_net.line["split_removed_reason"] = None

    graph = nx.Graph()
    graph.add_nodes_from(radial_net.bus.index.tolist())
    for line_idx, line_row in radial_net.line.iterrows():
        if not _is_in_service(line_row):
            continue
        fb = int(line_row["from_bus"])
        tb = int(line_row["to_bus"])
        weight = float(line_row.get("length_km", 1.0) or 1.0)
        if graph.has_edge(fb, tb):
            existing = graph[fb][tb]
            if weight < existing.get("weight", 1.0):
                existing.update(weight=weight, line_idx=int(line_idx))
        else:
            graph.add_edge(fb, tb, weight=weight, line_idx=int(line_idx))

    if root_bus is not None and root_bus in graph:
        reachable = set(nx.node_connected_component(graph, root_bus))
    else:
        reachable = set(max(nx.connected_components(graph), key=len)) if graph.nodes else set()

    disconnected_bus_count = len(set(graph.nodes) - reachable)
    if disconnected_bus_count:
        trafo_rows = radial_net.trafo.copy()
        radial_net = pp.select_subnet(radial_net, buses=sorted(reachable), include_results=False)
        if radial_net.ext_grid.empty and root_bus in radial_net.bus.index:
            pp.create_ext_grid(radial_net, bus=root_bus, name="Feed_from_radialized_root")
        if radial_net.trafo.empty and not trafo_rows.empty and root_bus in radial_net.bus.index:
            _add_feeder_metadata(radial_net, radial_net, trafo_rows.iloc[0], add_ext_grid=False)
        if "split_removed_reason" not in radial_net.line.columns:
            radial_net.line["split_removed_reason"] = None

    active_graph = graph.subgraph(reachable).copy()
    keep_line_ids: set[int] = set()
    if active_graph.nodes:
        tree = nx.minimum_spanning_tree(active_graph, weight="weight")
        keep_line_ids = {int(data["line_idx"]) for _, _, data in tree.edges(data=True)}

    cycle_removed = 0
    for line_idx, line_row in radial_net.line.iterrows():
        if not _is_in_service(line_row):
            continue
        if int(line_idx) not in keep_line_ids:
            radial_net.line.at[line_idx, "in_service"] = False
            radial_net.line.at[line_idx, "split_removed_reason"] = "cycle_radialization"
            cycle_removed += 1

    check_graph = pp.topology.create_nxgraph(radial_net, respect_switches=True)
    components = nx.number_connected_components(check_graph) if len(check_graph) else 0
    cycles = len(check_graph.edges) - len(check_graph.nodes) + components if len(check_graph) else 0
    return radial_net, {
        "open_switch_lines_removed": open_switch_removed,
        "cycle_lines_removed": cycle_removed,
        "disconnected_buses_removed": disconnected_bus_count if 'disconnected_bus_count' in locals() else 0,
        "components_after_radialization": components,
        "cycles_after_radialization": cycles,
        "radialization_status": "ok" if cycles == 0 else "cycles_remaining",
    }


def _write_net(net: pp.pandapowerNet, path: Path) -> None:
    pp.to_excel(net, str(path))


def _manifest_record(
    *,
    sub_id: str,
    variant: str,
    category: str,
    load_status: str,
    path: Path | None,
    net: pp.pandapowerNet | None,
    reason: str = "exported",
    extra: dict | None = None,
) -> dict:
    record = {
        "lv_id": sub_id,
        "variant": variant,
        "category": category,
        "load_status": load_status,
        "file": str(path) if path is not None else "",
        "status": reason,
        "bus_count": len(net.bus) if net is not None else 0,
        "line_count": len(net.line) if net is not None else 0,
        "active_line_count": int(sum(_is_in_service(row) for _, row in net.line.iterrows())) if net is not None else 0,
        "load_count": len(net.load) if net is not None else 0,
        "sgen_count": len(net.sgen) if net is not None else 0,
        "trafo_count": len(net.trafo) if net is not None else 0,
        "ext_grid_count": len(net.ext_grid) if net is not None else 0,
        "has_ns_load_name": _has_ns_load_name(net) if net is not None else False,
        "has_ns_load_file": _has_ns_load_file(net) if net is not None else False,
        "has_hh_load": _has_household_load(net) if net is not None else False,
    }
    if extra:
        record.update(extra)
    return record


def _write_manifest(output_dir: Path, records: list[dict]) -> Path:
    manifest_path = output_dir / "split_manifest.csv"
    if not records:
        manifest_path.write_text("", encoding="utf-8")
        return manifest_path
    fields = sorted({key for record in records for key in record})
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    return manifest_path


def extract_lv_grids(net: pp.pandapowerNet, output_dir: Path | str) -> dict[str, list[Path] | Path]:
    """Extract logical and radialized LV subnet variants from a SWF validation net.

    ``logical/`` preserves DSO LV subnet IDs from ``chr_name``. ``radialized/``
    writes the comparison projection: open-switch lines and cycle-closing lines
    are marked out of service so the active topology is a tree. File names carry
    the variant, regular/mini category, and whether an LV comparison load is present. The manifest records exported and skipped comparison candidates.
    """
    output_dir = Path(output_dir)
    logical_dir = output_dir / "logical"
    radialized_dir = output_dir / "radialized"
    logical_dir.mkdir(parents=True, exist_ok=True)
    radialized_dir.mkdir(parents=True, exist_ok=True)

    if "chr_name" not in net.bus.columns:
        raise ValueError("Cannot extract LV grids because the bus table has no 'chr_name' column.")

    bus_groups = _logical_lv_bus_groups(net)
    active_trafos = _active_lv_trafos(net)
    trafo_by_sub_id: dict[str, list[int]] = defaultdict(list)
    lv_bus_by_sub_id: dict[str, int] = {}
    for trafo_idx, sub_id, lv_bus in active_trafos:
        trafo_by_sub_id[sub_id].append(trafo_idx)
        lv_bus_by_sub_id[sub_id] = lv_bus

    logical_paths: list[Path] = []
    radialized_paths: list[Path] = []
    records: list[dict] = []

    for sub_id, core_buses in tqdm(sorted(bus_groups.items())):
        trafo_indices = trafo_by_sub_id.get(sub_id, [])
        trafo_rows = net.trafo.loc[trafo_indices] if trafo_indices else net.trafo.iloc[0:0]
        category = _grid_category(len(core_buses))

        if len(trafo_indices) != 1:
            load_status = "unknown"
            records.append(_manifest_record(
                sub_id=sub_id,
                variant="logical",
                category=category,
                load_status=load_status,
                path=None,
                net=None,
                reason="skipped_no_active_lv_trafo" if not trafo_indices else "skipped_multiple_active_lv_trafos",
                extra={"active_lv_trafo_count": len(trafo_indices)},
            ))
            continue

        logical_net = _build_logical_lv_net(net, sub_id, core_buses, trafo_rows)
        load_status = _load_status(logical_net)
        logical_name = f"LV_{sub_id}__logical__{category}__{load_status}.xlsx"
        logical_path = logical_dir / logical_name
        _write_net(logical_net, logical_path)
        logical_paths.append(logical_path)
        records.append(_manifest_record(
            sub_id=sub_id,
            variant="logical",
            category=category,
            load_status=load_status,
            path=logical_path,
            net=logical_net,
            extra={"active_lv_trafo_count": len(trafo_indices)},
        ))

        if load_status != "lvload":
            records.append(_manifest_record(
                sub_id=sub_id,
                variant="radialized",
                category=category,
                load_status=load_status,
                path=None,
                net=logical_net,
                reason="skipped_no_lv_comparison_load",
                extra={"active_lv_trafo_count": len(trafo_indices)},
            ))
            continue

        radial_net, radial_info = _radialize_lv_net(logical_net, lv_bus_by_sub_id.get(sub_id))
        radial_name = f"LV_{sub_id}__radialized__{category}__{load_status}.xlsx"
        radial_path = radialized_dir / radial_name
        _write_net(radial_net, radial_path)
        radialized_paths.append(radial_path)
        records.append(_manifest_record(
            sub_id=sub_id,
            variant="radialized",
            category=category,
            load_status=load_status,
            path=radial_path,
            net=radial_net,
            extra={"active_lv_trafo_count": len(trafo_indices), **radial_info},
        ))

    manifest_path = _write_manifest(output_dir, records)
    return {
        "logical_grids": logical_paths,
        "radialized_grids": radialized_paths,
        "manifest": manifest_path,
    }


def split_to_subgrids(
    input_file: Path | str,
    output_dir: Path | str,
    clear_output_dir: bool = True,
) -> dict[str, list[Path] | Path | None]:
    """Split a source validation net into one MV net and multiple LV subnets."""
    input_file = Path(input_file)
    output_dir = Path(output_dir)

    if clear_output_dir and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    net = pp.from_json(str(input_file))
    mv_path = extract_mv_grid(net, output_dir)
    lv_results = extract_lv_grids(net, output_dir)
    return {"mv_grid": mv_path, **lv_results}


def _resolve_convert_geodata_to_geojson():
    """Resolve the pandapower geojson conversion helper across version differences."""
    try:
        from pandapower.plotting.geo import convert_geodata_to_geojson
        return convert_geodata_to_geojson
    except ImportError:
        try:
            from pandapower.plotting import convert_geodata_to_geojson
            return convert_geodata_to_geojson
        except ImportError as exc:
            raise ImportError(
                "convert_geodata_to_geojson not found in pandapower.plotting or pandapower.plotting.geo"
            ) from exc


def fix_subnet_geos(base_dir: Path | str) -> tuple[int, int]:
    """Convert bus and line geodata of exported subnet JSON files into GeoJSON-ready form."""
    base_dir = Path(base_dir)
    convert_geodata_to_geojson = _resolve_convert_geodata_to_geojson()
    files = sorted(base_dir.glob("**/*.json"))

    success_count = 0
    fail_count = 0
    for file_path in tqdm(files):
        try:
            net = pp.from_json(str(file_path))
            convert_geodata_to_geojson(net, delete=False)
            pp.to_json(net, str(file_path))

            excel_path = file_path.with_suffix(".xlsx")
            if excel_path.exists():
                pp.to_excel(net, str(excel_path))

            success_count += 1
        except Exception:
            fail_count += 1

    return success_count, fail_count


def export_synthetic_grids_to_excel(
    output_dir: Path | str,
    plz: int | None = None,
    kcid: int | None = None,
    bcid: int | None = None,
) -> list[Path]:
    """Export synthetic grids from the database as Excel files using ``pp.to_excel``.

    Fetches one specific grid when *plz*, *kcid*, and *bcid* are all provided.
    Fetches every grid stored for the current ``VERSION_ID`` when all three are
    ``None``.  Mixed partial arguments raise a ``ValueError``.

    Args:
        output_dir: Directory where the ``.xlsx`` files will be written.
        plz: Postal code.  Must be given together with *kcid* and *bcid*.
        kcid: K-means cluster ID.  Must be given together with *plz* and *bcid*.
        bcid: Building cluster ID.  Must be given together with *plz* and *kcid*.

    Returns:
        List of :class:`~pathlib.Path` objects for every file that was written.

    Raises:
        ValueError: If only some of *plz*/*kcid*/*bcid* are provided, or if a
            requested grid does not exist in the database.
    """
    from pylovo.config_loader import VERSION_ID
    from pylovo.database.database_client import DatabaseClient

    identifiers = (plz, kcid, bcid)
    n_given = sum(v is not None for v in identifiers)
    if n_given not in (0, 3):
        raise ValueError(
            "Provide either all three of plz/kcid/bcid (single grid) "
            "or none of them (all grids)."
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    with DatabaseClient() as dbc:
        if n_given == 3:
            rows = [(int(plz), int(kcid), int(bcid))]
        else:
            dbc.cur.execute(
                f"SELECT plz, kcid, bcid FROM pylovo.grid_result "
                "WHERE version_id = %s AND grid IS NOT NULL",
                (VERSION_ID,),
            )
            rows = dbc.cur.fetchall()

        for row_plz, row_kcid, row_bcid in tqdm(rows, desc="Exporting grids"):
            try:
                net = dbc.read_net_db(int(row_plz), int(row_kcid), int(row_bcid))
            except ValueError:
                continue

            xlsx_path = output_dir / f"grid_{row_plz}_{row_kcid}_{row_bcid}.xlsx"
            pp.to_excel(net, str(xlsx_path))
            written.append(xlsx_path)

    return written


__all__ = [
    "export_synthetic_grids_to_excel",
    "extract_lv_grids",
    "extract_mv_grid",
    "fix_subnet_geos",
    "get_bus_line_geo",
    "iter_nets_from_json",
    "MINI_GRID_BUS_THRESHOLD",
    "split_to_subgrids",
]

