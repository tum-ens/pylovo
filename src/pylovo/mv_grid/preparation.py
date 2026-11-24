"""The region step of the MV stage: the input of a variant, written once before its districts are planned.

:func:`region_step` writes in the transaction of the caller: the HV/MV substations of the region (OSM sites matched
to the eGon-data substations, checked for an OSM HV line), the MV nodes (stations with their load statistics, MV
customers, busbars), their attachment to the routing graph, the supply areas of the substations and one pending
``mv_grid`` per supplying substation with its area and its nominal voltage. It is the MV counterpart of the input
step of an LV run (:mod:`pylovo.lv_grid.preparation`). The planning modules it uses (``substations``, ``inputs``,
``districts``, ``graph``) are imported on first use.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid import runtime
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError

#: Node kinds that belong to a district (the busbars belong to their substation).
_KINDS_WITH_GRID = ("station", "customer")


def _optional(dbc: Any, name: str, logger: logging.Logger, func, *args, **kwargs) -> Any:
    """Run an optional read inside a savepoint; on an error log it, roll back to the savepoint and return None."""
    dbc.cur.execute(f"SAVEPOINT {name}")
    try:
        result = func(*args, **kwargs)
        dbc.cur.execute(f"RELEASE SAVEPOINT {name}")
        return result
    except Exception as exc:
        dbc.cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        logger.warning(f"{name} skipped: {runtime.one_line(exc)}")
        return None


def _substation_sites(dbc: Any, region_wkb: bytes, params: MvParameters, logger: logging.Logger) -> pd.DataFrame:
    """The HV/MV substation sites of the region from OSM, matched to the eGon-data substations (eTraGo buses) and
    checked for an OSM HV line that feeds them, as a table for ``insert_substations``."""
    substations = runtime.module("substations")
    egon_ok, egon_why = dbc.egon_available()
    egon = dbc.egon_substations(region_wkb, params.region_buffer_m) if egon_ok else None
    candidates = dbc.osm_substation_candidates(region_wkb, params.region_buffer_m)
    selection = substations.select_osm_sites(candidates, params)
    sites = selection.sites
    logger.info(selection.describe())
    if not egon_ok:
        logger.warning(f"eGon cross-check disabled: {egon_why}")
    matches = substations.match_egon(sites, egon, params.egon_match_m) if egon is not None else None
    lines = _optional(dbc, "mv_osm_hv_lines", logger, dbc.osm_hv_lines, region_wkb,
                      params.region_buffer_m + substations.HV_LINE_END_M)
    fed = substations.hv_line_ends(sites, lines) if lines is not None else None
    frame = substations.sites_frame(sites, region_wkb=region_wkb, matches=matches, hv_lines=fed)
    if matches is not None:
        unmatched = int(frame["egon_bus_id"].isna().sum())
        if unmatched:
            logger.info(f"{unmatched} of {len(frame)} OSM sites have no eGon substation within "
                        f"{params.egon_match_m:g} m")
    if fed is not None and not all(fed):
        logger.info(f"{len(fed) - sum(fed)} of {len(fed)} OSM sites have no 60 or 110 kV line or cable of OSM ending "
                    f"within {substations.HV_LINE_END_M:g} m (flag no_osm_hv_line)")
    return frame.reset_index(drop=True)


def _node_rows(dbc: Any, version_id: str, plz: Sequence[int], sites: pd.DataFrame, params: MvParameters,
               lv: LvVersionParameters, logger: logging.Logger) -> pd.DataFrame:
    """The ``mv_node`` rows of the region: stations, MV customers and busbars."""
    inputs = runtime.module("inputs")
    station_nodes = dbc.station_nodes(version_id, plz)
    stats = dbc.station_load_stats(version_id, plz)
    stations = inputs.station_table(station_nodes, stats, lv)
    check = _optional(dbc, "mv_pandapower_load_check", logger, dbc.pandapower_load_check, version_id, plz)
    if check is not None and len(check) and "p_own_kw" in stations.columns:
        stored = check.set_index(check.columns[0]).iloc[:, 0].astype(float)
        own = stations.set_index("grid_result_id")["p_own_kw"].astype(float)
        both = own.index.intersection(stored.index)
        if len(both):
            rel = ((own.loc[both] - stored.loc[both]).abs() / stored.loc[both].abs().clip(lower=1e-9)).max()
            (logger.warning if rel > 1e-6 else logger.info)(
                f"Station peaks vs pandapower_load: largest relative difference {rel:.2e}")
    customers = inputs.customer_table(dbc.mv_customer_rows(version_id, plz, lv), lv, params)
    stations, customers = inputs.busbar_customers(stations, customers, params)
    stations = inputs.upsize_busbar_stations(stations, lv)
    customers.attrs["n_upsized_stations"] = stations.attrs.get("n_upsized", 0)
    logger.info(inputs.describe_customers(customers.attrs, params))
    rows = inputs.node_rows(stations, customers, sites).reset_index(drop=True)
    if not rows["kind"].eq("busbar").any():          # the busbar rows are part of node_rows; never go without them
        rows = pd.concat([_busbar_rows(sites), rows], ignore_index=True)
    return rows


def _busbar_rows(sites: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"kind": "busbar", "connection_hint": "busbar", "connection": "busbar",
                         "substation_id": sites["substation_id"].astype("int64").to_numpy(),
                         "x": sites["x"].astype(float).to_numpy(), "y": sites["y"].astype(float).to_numpy()})


def _attachment_updates(rows: pd.DataFrame, attach: pd.DataFrame, params: MvParameters,
                        logger: logging.Logger) -> pd.DataFrame:
    """Attachment columns per node from the KNN result.

    Customers without a mapped station move to their street transfer point (offset 0, flag ``transfer_point``);
    the settlement of a node is the one of its attach edge (open field: NULL); ``area_class`` is ``urban`` on an
    urban settlement's edge, else ``rural`` (busbars alike).
    """
    att = attach.set_index("key")
    ids = rows["mv_node_id"].astype("int64")
    att = att.reindex(ids.to_numpy())
    in_settlement = att["in_settlement"].astype(object).map(lambda v: bool(v) if pd.notna(v) else False)
    urban = att["settlement_urban"].astype(object).map(lambda v: bool(v) if pd.notna(v) else False)
    # nullable Int64 columns of attach_points become float64 with NaN (safe for joins and NaN checks)
    settlement = pd.to_numeric(att["settlement_id"], errors="coerce").astype("float64") \
        .where(in_settlement.to_numpy(dtype=bool))
    out = pd.DataFrame({
        "mv_node_id": ids.to_numpy(),
        "attach_edge_id": pd.to_numeric(att["edge_id"], errors="coerce").astype("float64").to_numpy(),
        "attach_fraction": pd.to_numeric(att["fraction"], errors="coerce").to_numpy(),
        "attach_offset_m": pd.to_numeric(att["offset_m"], errors="coerce").to_numpy(),
        "settlement_id": settlement.to_numpy(),
        "area_class": np.where((in_settlement & urban).to_numpy(dtype=bool), "urban", "rural"),
        "x": np.nan, "y": np.nan,
    })
    mapped = rows["mapped_station"].astype(object).map(lambda v: bool(v) if pd.notna(v) else False).to_numpy(bool) \
        if "mapped_station" in rows.columns else np.zeros(len(rows), dtype=bool)
    transfer = (rows["kind"].eq("customer").to_numpy(bool) & ~mapped & att["ax"].notna().to_numpy(bool))
    if transfer.any():
        out.loc[transfer, "x"] = att["ax"].to_numpy(dtype=float)[transfer]
        out.loc[transfer, "y"] = att["ay"].to_numpy(dtype=float)[transfer]
        out.loc[transfer, "attach_offset_m"] = 0.0
    out["flags"] = [["transfer_point"] if t else [] for t in transfer]
    unattached = int(out["attach_edge_id"].isna().sum())
    if unattached:
        logger.warning(f"{unattached} MV nodes could not be attached to the routing graph")
    long = (out["attach_offset_m"] > params.long_connector_m) & ~rows["kind"].eq("busbar").to_numpy(bool)
    if long.any():
        logger.warning(f"{int(long.sum())} MV nodes are more than {params.long_connector_m:g} m from the street graph")
    return out


def _assign_supply_areas(dbc: Any, graph_id: str, nodes: pd.DataFrame, sites: pd.DataFrame,
                         params: MvParameters, lv: LvVersionParameters,
                         logger: logging.Logger) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Site index per node (into ``sites``), bridged flags and the overloaded site indices."""
    districts = runtime.module("districts")
    graph = runtime.module("graph")
    edge_id, source, target, length = dbc.region_graph_arrays(graph_id)
    csr, vertex_index = graph.region_csr(source, target, length)
    edge_len = dict(zip(np.asarray(edge_id).tolist(), np.asarray(length, dtype=float).tolist()))
    ends = pd.DataFrame({"edge_source": source, "edge_target": target, "edge_length_m": length},
                        index=pd.Index(np.asarray(edge_id, dtype=np.int64), name="attach_edge_id"))
    sites_in = sites.join(ends, on="attach_edge_id")
    nodes_in = nodes.join(ends, on="attach_edge_id")
    capacity = districts.site_capacity_mva(sites_in, params)
    result = districts.routed_assignment(csr, vertex_index, edge_len, sites_in, nodes_in, lv.g(), lv.power_factor,
                                         capacity)
    logger.info(f"Supply areas: {len(result.overloaded)} substations above capacity")
    bridged = np.asarray(result.bridged, dtype=bool)
    return np.asarray(result.site_of_node, dtype=np.int64), bridged, [int(i) for i in result.overloaded]


def _set_district_voltages(dbc: Any, version_id: str, variant_id: str, rows: pd.DataFrame,
                           grid_of_node: dict[int, int | None], params: MvParameters,
                           logger: logging.Logger, graph_id: str | None = None, region_wkb: bytes | None = None,
                           attach_edge: pd.Series | None = None) -> pd.DataFrame:
    """The nominal voltage of every district from its installed density (``MV_NOMINAL_VOLTAGE_KV``).

    Runs after the district areas are written: ``districts.installed_density`` of the station ratings and MV
    customer peaks of each district over its area, ``districts.district_voltages`` by the variant's rule; sets
    ``mv_grid.vn_kv`` and ``mv_grid.metrics['voltage_level']`` (caller commits). With ``auto`` and the routing graph
    (``graph_id``, ``region_wkb``, ``attach_edge``), each node takes the municipality of its street edge, and a
    district gets 10 kV when at least half of its installed capacity lies in municipalities of at least the threshold
    density (``districts.municipal_density``, ``districts.dense_share``).

    Args:
        rows: The node rows of the region with ``mv_node_id`` (``kind``, ``connection_hint``, ``sn_kva``,
            ``installed_kw``).
        grid_of_node: ``mv_grid_id`` per ``mv_node_id`` (None: not assigned).

    Returns:
        The table of ``districts.district_voltages`` (index ``mv_grid_id``).
    """
    districts = runtime.module("districts")
    nodes = pd.DataFrame({column: rows[column] if column in rows.columns else np.nan
                          for column in ("mv_node_id", "kind", "connection_hint", "sn_kva", "installed_kw")})
    nodes["mv_grid_id"] = [grid_of_node.get(int(k)) for k in nodes["mv_node_id"]]
    nodes["mv_grid_id"] = pd.to_numeric(nodes["mv_grid_id"], errors="coerce")
    share = None
    if params.auto_voltage and graph_id is not None and region_wkb is not None and attach_edge is not None:
        ags_of_edge = dbc.edge_municipalities(graph_id)
        nodes["ags"] = nodes["mv_node_id"].map(attach_edge).map(ags_of_edge)
        ags = sorted({str(a) for a in nodes["ags"].dropna()})
        municipalities = districts.municipal_density(nodes, dbc.municipality_areas(ags, region_wkb))
        share = districts.dense_share(nodes, municipalities, params.mv_10kv_min_density_mva_km2)
        above = municipalities["density_mva_km2"] >= params.mv_10kv_min_density_mva_km2
        dense = ", ".join(f"{a} ({d:.2f})" for a, d in municipalities.loc[above, "density_mva_km2"].items())
        below = municipalities.loc[~above, "density_mva_km2"].max()
        logger.info(f"Municipalities at or above {params.mv_10kv_min_density_mva_km2:g} MVA/km² installed: "
                    f"{dense or 'none'}; highest below: {below:.2f} MVA/km²")
    table = districts.district_voltages(districts.installed_density(nodes, dbc.grid_areas(version_id, variant_id)),
                                        params, share)
    dbc.set_grid_voltages(version_id, variant_id, table,
                          {int(g): districts.voltage_record(row, params) for g, row in table.iterrows()})
    logger.info(districts.describe_voltages(table, params))
    return table


def region_step(dbc: Any, version_id: str, variant_id: str, plz: Sequence[int], region_wkb: bytes, graph_id: str,
                params: MvParameters, lv: LvVersionParameters, logger: logging.Logger,
                warnings: list[str]) -> dict:
    """Write the substations, nodes, attachments, supply areas and grids of a variant (caller commits)."""
    # substations
    sites = _substation_sites(dbc, region_wkb, params, logger)
    sites["substation_id"] = dbc.insert_substations(version_id, variant_id, sites)
    # nodes
    rows = _node_rows(dbc, version_id, plz, sites, params, lv, logger)
    rows["mv_node_id"] = dbc.insert_nodes(version_id, variant_id, rows)
    # attachment to the routing graph (one KNN query for all nodes and busbars)
    logger.info(f"Region: attaching {len(rows)} nodes to the routing graph")
    attach = dbc.attach_points(graph_id, pd.DataFrame({"key": rows["mv_node_id"].to_numpy(),
                                                       "x": rows["x"].astype(float).to_numpy(),
                                                       "y": rows["y"].astype(float).to_numpy()}))
    updates = _attachment_updates(rows, attach, params, logger)
    dbc.update_node_attachments(updates)
    busbar = rows["kind"].eq("busbar")
    busbar_offset = pd.Series(updates.loc[busbar.to_numpy(), "attach_offset_m"].to_numpy(),
                              index=rows.loc[busbar, "substation_id"].astype("int64").to_numpy())
    attach_columns = ["attach_edge_id", "attach_fraction", "attach_offset_m"]
    sites = sites.drop(columns=attach_columns, errors="ignore").join(
        updates.loc[busbar.to_numpy(), attach_columns].set_index(
            rows.loc[busbar, "substation_id"].astype("int64").to_numpy()), on="substation_id")
    long_attach = [int(s) for s, off in busbar_offset.items()
                   if not pd.notna(off) or off > params.substation_max_attach_m]
    dbc.flag_substations(long_attach, "long_attach")
    usable = sites[~sites["substation_id"].isin(long_attach)].reset_index(drop=True)
    if long_attach:
        # a warning of the summary: usually the InfDB lacks the streets around these substations (a region imported
        # by postcode), and their nodes go to substations farther away
        message = (f"{len(long_attach)} of {len(sites)} HV/MV substations are more than "
                   f"{params.substation_max_attach_m:g} m from the street graph and supply nothing (long_attach), "
                   f"so the supply areas go to the other {len(usable)}; check that the InfDB has the streets of the "
                   f"municipalities within {params.region_buffer_m / 1000:g} km of the region")
        logger.warning(message)
        warnings.append(message)
    if not len(usable):
        raise MvStageError("no HV/MV substation is within reach of the street graph of the region")
    # supply areas
    nodes = dbc.variant_node_table(version_id, variant_id)
    members = nodes[nodes["kind"].isin(_KINDS_WITH_GRID)]
    # every node gets a district; excluded nodes (HV customers) do not count for the capacity
    if len(members):
        logger.info(f"Region: supply areas of {len(members)} nodes and {len(usable)} substations")
        owner, bridged, overloaded = _assign_supply_areas(dbc, graph_id, members, usable, params, lv, logger)
    else:
        owner, bridged, overloaded = np.zeros(0, dtype=np.int64), np.zeros(0, dtype=bool), []
    owner_sub = np.where(owner >= 0, usable["substation_id"].to_numpy(dtype=np.int64)[np.clip(owner, 0, None)], -1)
    used = sorted({int(s) for s in owner_sub if s >= 0})
    grids = dbc.create_grids(version_id, variant_id, used, params.base_kv)
    grid_of_node = [grids.get(int(s)) if s >= 0 else None for s in owner_sub]
    dbc.set_node_grids(pd.DataFrame({
        "mv_node_id": members.index.to_numpy(dtype=np.int64),
        "mv_grid_id": grid_of_node,
        "bridged": bridged,
    }))
    dbc.flag_substations([int(usable["substation_id"].iloc[i]) for i in overloaded], "over_capacity")
    dbc.flag_substations([int(s) for s in sites["substation_id"] if int(s) not in grids and int(s) not in long_attach],
                         "no_nodes")
    unassigned = int((owner_sub < 0).sum())
    if unassigned:
        warnings.append(f"{unassigned} MV nodes were assigned to no HV/MV substation")
    logger.info(f"Region: areas of {len(grids)} districts")
    dbc.write_grid_areas(version_id, variant_id, plz)
    _set_district_voltages(dbc, version_id, variant_id, rows,
                           dict(zip(members.index.to_numpy(dtype=np.int64).tolist(), grid_of_node)), params, logger,
                           graph_id, region_wkb, pd.to_numeric(members["attach_edge_id"], errors="coerce"))
    kinds = members["kind"].value_counts().to_dict()
    logger.info(f"Region: {len(sites)} substations ({len(grids)} supplying), "
                f"{kinds.get('station', 0)} stations, {kinds.get('customer', 0)} customers")
    return {"n_sites": len(sites), "n_grids": len(grids), "kinds": kinds}
