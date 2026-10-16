"""The MV stage driver: region steps, district workers, summary and the export of stored nets.

:func:`run_mv_stage` runs the MV stage for one MV variant of a stored LV version (DESIGN §1.2):

0. **Safety:** prints the database target, checks the schema (migration ``0009_mv_grids``) and takes the variant
   lock (advisory lock 907362) for the whole run.
1. **PLZ:** the requested PLZ with LV grids of the version; requested PLZ without grids are try-locked (907361): a
   PLZ that is being generated right now stops the run before any write, the others are reported as missing.
2. **Variant:** the parameter snapshot is checked first (no write), then the routing graph is built or reused
   (own commit), then the variant is registered once (``new``, ``resume`` or ``grow``, union semantics of the PLZ
   set, DESIGN §2.5).
3. **Region** (one transaction): HV/MV substations, MV nodes (stations with their load statistics and LV-side
   generation, MV customers, MV generators, busbars), their attachment to the routing graph, the supply areas and
   one pending ``mv_grid`` per supplying substation with its Voronoi-cell polygon and its nominal voltage (with
   ``MV_NOMINAL_VOLTAGE_KV`` auto 10 kV from an installed density of ``mv_10kv_min_density_mva_km2``, else
   20 kV). Skipped when a run resumes.
4. **Districts:** one task per ``pending`` or ``failed`` grid, largest first, in a ``fork`` process pool (or in
   process): each worker opens its own database connection, reads its district, plans it with the pure
   :func:`~pylovo.mv_grid.planner.plan_district` and stores it in one transaction. A failed district is marked
   ``failed`` with its error; the others continue. A broken pool (e.g. out of memory) leaves the remaining grids
   ``pending``; a rerun with fewer ``jobs`` resumes them.
5. **Post:** ties between districts (``MV_INTERCONNECTION: ties``), then the summary (``mv_variant.summary``) and
   the final status ``done``, ``partial`` or ``failed``.

The planning modules (``graph``, ``substations``, ``inputs``, ``districts``, ...) are imported when they are used,
so this module imports quickly and a missing optional module (ties, coupling) only disables its step.
"""
from __future__ import annotations

import importlib
import logging
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from types import ModuleType
from typing import Any, Sequence

import numpy as np
import pandas as pd

from pylovo.mv_grid.model import CASES, DistrictInput, GridOutcome, MvRunSummary, demand_line
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError

#: Memory of one district worker after its imports plus a large district (GB), measured ~0.3 + district data.
WORKER_GB: float = 0.6
#: Memory kept free for the parent, PostgreSQL and the user's other processes (GB).
RESERVED_GB: float = 2.0
#: Default upper bound of the district workers (``min(4, N_JOBS)``).
DEFAULT_MAX_JOBS: int = 4

_KINDS_WITH_GRID = ("station", "customer", "generator")


def _module(name: str) -> ModuleType:
    """``pylovo.mv_grid.<name>``, imported on first use."""
    return importlib.import_module(f"pylovo.mv_grid.{name}")


def _config() -> ModuleType:
    return importlib.import_module("pylovo.config_loader")


def _database_client(log_file: str):
    from pylovo.database.database_client import DatabaseClient

    return DatabaseClient(log_file=log_file)


def _logger(name: str, log_file: str) -> logging.Logger:
    from pylovo import utils

    return utils.create_logger(name, log_file, _config().LOG_LEVEL)


def _close_logger(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        handler.close()
    logger.handlers.clear()


def _one_line(exc: BaseException) -> str:
    """``Type: message`` on one line, at most 2,000 characters."""
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:2000]


# ---------------------------------------------------------------------------------------------- workers
def mem_available_gb() -> float | None:
    """``MemAvailable`` of ``/proc/meminfo`` in GB, or None where it cannot be read."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def worker_count(jobs: int | None, n_pending: int, per_worker_gb: float = WORKER_GB) -> int:
    """Number of district workers: ``min(jobs, n_pending)`` capped by the free memory.

    Args:
        jobs: Requested workers; None means ``min(4, N_JOBS)``.
        n_pending: Number of districts to plan.
        per_worker_gb: Memory of one worker (GB).

    Returns:
        ``n = min(jobs, n_pending)``, then at most ``max(1, int((MemAvailable_GB - 2.0) / per_worker_gb))``;
        0 when nothing is pending.
    """
    if n_pending <= 0:
        return 0
    if jobs is None:
        jobs = min(DEFAULT_MAX_JOBS, int(_config().N_JOBS))
    n = max(1, min(int(jobs), int(n_pending)))
    available = mem_available_gb()
    if available is not None and per_worker_gb > 0:
        n = min(n, max(1, int((available - RESERVED_GB) / per_worker_gb)))
    return n


def attachment_points(nodes: pd.DataFrame, geometries: dict[int, Any]) -> tuple[np.ndarray, np.ndarray]:
    """The street point of every node: its attach edge's geometry at ``attach_fraction`` (NaN without one).

    ``graph.build_run_graph`` places the split vertex of an attach edge there; without these coordinates it falls
    back to the straight chord between the edge ends, which lies off a curved street, and the line geometries then
    zigzag between the chord point and the street.
    """
    edge = pd.to_numeric(nodes["attach_edge_id"], errors="coerce").to_numpy(dtype=float)
    fraction = pd.to_numeric(nodes["attach_fraction"], errors="coerce").to_numpy(dtype=float)
    ax = np.full(len(nodes), np.nan)
    ay = np.full(len(nodes), np.nan)
    for i, (e, f) in enumerate(zip(edge, fraction)):
        geom = geometries.get(int(e)) if np.isfinite(e) and np.isfinite(f) else None
        if geom is not None and not geom.is_empty:
            point = geom.interpolate(min(max(float(f), 0.0), 1.0), normalized=True)
            ax[i], ay[i] = point.x, point.y
    return ax, ay


def district_input(dbc: Any, version_id: str, variant_id: str, mv_grid_id: int, graph_id: str,
                   params: MvParameters, lv: LvVersionParameters) -> tuple[DistrictInput, dict[int, float]]:
    """Read one district for the planner (worker side).

    The nodes come from :meth:`MvResultMixin.load_district_nodes`; the stored LV requirement (``ext_grid.vm_pu``
    of each station's LV net) is read for the district's stations only; the subgraph is the part of the routing
    graph within ``max(subgraph_buffer_m, largest attach offset + 100 m)`` of the convex hull of the node points,
    plus every attach edge. Edge geometries are read lazily (once per edge) through ``edge_geometry``; the attach
    edges are read at once for the street points ``ax``, ``ay`` of the nodes (:func:`attachment_points`).

    Returns:
        ``(district input, {grid_result_id: requirement})``.
    """
    import shapely

    nodes, grid = dbc.load_district_nodes(mv_grid_id)
    if grid["version_id"] != version_id or grid["mv_variant_id"] != variant_id:
        raise ValueError(f"MV grid {mv_grid_id} belongs to variant {grid['mv_variant_id']} of version "
                         f"{grid['version_id']}, not to {variant_id} of {version_id}")
    if not len(nodes) or nodes.iloc[0]["kind"] != "busbar":
        raise MvStageError(f"MV grid {mv_grid_id} has no busbar node")
    stations = nodes["kind"].eq("station") & nodes["grid_result_id"].notna()
    station_ids = sorted({int(g) for g in nodes.loc[stations, "grid_result_id"]})
    requirements = {int(k): float(v) for k, v in (dbc.lv_design_source_vm(version_id, station_ids)
                                                  if station_ids else {}).items() if v is not None}
    nodes = nodes.copy()
    nodes["lv_design_source_vm_pu"] = [requirements.get(int(g), np.nan) if pd.notna(g) else np.nan
                                       for g in nodes["grid_result_id"]]
    xy = nodes[["x", "y"]].to_numpy(dtype=float)
    hull = shapely.MultiPoint(xy).convex_hull
    offsets = pd.to_numeric(nodes["attach_offset_m"], errors="coerce")
    buffer_m = max(float(params.subgraph_buffer_m), (float(offsets.max()) if offsets.notna().any() else 0.0) + 100.0)
    must = sorted({int(e) for e in pd.to_numeric(nodes["attach_edge_id"], errors="coerce").dropna()})
    edges = dbc.subgraph_edges(graph_id, shapely.to_wkb(hull), buffer_m, must)
    ratings = sorted(float(r) for r in (grid.get("osm_ratings_mva") or []) if r is not None and float(r) > 0)
    known_units = tuple(ratings) if len(ratings) >= 2 else ()
    cache: dict[int, Any] = {}

    def edge_geometry(edge_ids: Sequence[int]) -> dict[int, Any]:
        wanted = [int(e) for e in edge_ids]
        missing = sorted({e for e in wanted if e not in cache})
        if missing:
            cache.update({int(k): v for k, v in dbc.edge_geometries(graph_id, missing).items()})
        return {e: cache[e] for e in wanted if e in cache}

    nodes["ax"], nodes["ay"] = attachment_points(nodes, edge_geometry(must) if must else {})
    hv_bus = grid.get("egon_bus_id")
    inp = DistrictInput(mv_grid_id=int(mv_grid_id), substation_id=int(grid["substation_id"]),
                        vn_kv=float(grid["vn_kv"]), nodes=nodes, edges=edges, params=params, lv=lv,
                        known_units_mva=known_units, edge_geometry=edge_geometry, settlement_geoms={},
                        hv_bus_id=int(hv_bus) if hv_bus is not None and not pd.isna(hv_bus) else None)
    return inp, requirements


def _grid_worker(version_id: str, variant_id: str, mv_grid_id: int, params_snapshot: dict, comment: str,
                 lv_json: dict, graph_id: str, log_file: str) -> GridOutcome:
    """Plan and store one district (runs in a worker process, or in process).

    Only picklable arguments; the worker opens its own database connection and closes it at the end, and never
    uses a connection inherited from the parent. It logs to ``log_mv_<version>_<variant>_<mv_grid_id>.txt`` next to
    ``log_file``. Every exception is caught: the grid is marked ``failed`` with the error text.
    """
    start = time.time()
    worker_log = str(Path(log_file).parent / f"log_mv_{version_id}_{variant_id}_{mv_grid_id}.txt")
    logger = _logger(f"MvGrid.{version_id}.{variant_id}.{mv_grid_id}", worker_log)
    dbc = None
    try:
        dbc = _database_client(log_file)
        params = MvParameters.from_snapshot(variant_id, comment, params_snapshot)
        lv = LvVersionParameters.from_json(lv_json)
        inp, requirements = district_input(dbc, version_id, variant_id, mv_grid_id, graph_id, params, lv)
        dbc.rollback_changes()                     # no open transaction while the district is planned
        logger.info(f"MV grid {mv_grid_id}: {len(inp.nodes)} nodes, {len(inp.edges.edge_id)} edges")
        plan = _module("planner").plan_district(inp)
        runtime = time.time() - start
        dbc.save_district(version_id, variant_id, mv_grid_id, plan, runtime, requirements=requirements)
        logger.info(f"MV grid {mv_grid_id} stored: {plan.power_flow_status}, {len(plan.topology.rings)} rings, "
                    f"{len(plan.repairs)} repairs, {runtime:.1f} s")
        return GridOutcome(int(mv_grid_id), "done", None, runtime)
    except Exception as exc:
        runtime = time.time() - start
        error = _one_line(exc)
        logger.exception(f"MV grid {mv_grid_id} failed: {error}")
        if dbc is not None:
            try:
                dbc.rollback_changes()
                dbc.mark_grid_failed(mv_grid_id, error)
            except Exception as mark_exc:   # the connection may be gone
                logger.error(f"MV grid {mv_grid_id}: could not mark the grid failed: {_one_line(mark_exc)}")
        return GridOutcome(int(mv_grid_id), "failed", error, runtime)
    finally:
        if dbc is not None:
            dbc.close()
        _close_logger(logger)


def _run_districts(dbc: Any, version_id: str, variant_id: str, pending: Sequence[tuple[int, int]],
                   params: MvParameters, lv: LvVersionParameters, graph_id: str, *, parallel: bool,
                   jobs: int | None, log_file: str, logger: logging.Logger, warnings: list[str]) -> list[GridOutcome]:
    """Plan the pending districts (largest first) in a fork pool or in process; one outcome per finished grid."""
    if not pending:
        return []
    n = worker_count(jobs, len(pending)) if parallel else 1
    total = len(pending)
    logger.info(f"Planning {total} MV grids with {n} worker{'s' if n != 1 else ''}"
                + ("" if parallel else " (in process)"))
    snapshot = params.to_snapshot()
    lv_json = lv.as_json()
    outcomes: list[GridOutcome] = []

    def report(outcome: GridOutcome) -> None:
        outcomes.append(outcome)
        if outcome.status == "done":
            logger.info(f"MV grid {outcome.mv_grid_id} done in {outcome.runtime_s:.0f} s ({len(outcomes)}/{total})")
        else:
            logger.warning(f"MV grid {outcome.mv_grid_id} failed after {outcome.runtime_s:.0f} s "
                           f"({len(outcomes)}/{total}): {outcome.error}")

    if n <= 1 or total == 1:
        for mv_grid_id, _ in pending:
            report(_grid_worker(version_id, variant_id, mv_grid_id, snapshot, params.comment, lv_json, graph_id,
                                log_file))
        return outcomes

    context = multiprocessing.get_context("fork")
    with ProcessPoolExecutor(max_workers=n, mp_context=context) as executor:
        futures = {executor.submit(_grid_worker, version_id, variant_id, mv_grid_id, snapshot, params.comment,
                                   lv_json, graph_id, log_file): mv_grid_id
                   for mv_grid_id, _ in pending}
        try:
            for future in as_completed(futures):
                mv_grid_id = futures[future]
                try:
                    outcome = future.result()
                except BrokenProcessPool as exc:
                    message = (f"the MV worker pool broke ({_one_line(exc)}; out of memory?): the remaining grids "
                               "stay pending, rerun with fewer --jobs")
                    logger.error(message)
                    warnings.append(message)
                    for other in futures:
                        other.cancel()
                    break
                except Exception as exc:     # an error outside the worker's own handling (e.g. pickling)
                    error = _one_line(exc)
                    try:
                        dbc.rollback_changes()
                        dbc.mark_grid_failed(mv_grid_id, error)
                    except Exception as mark_exc:
                        logger.error(f"MV grid {mv_grid_id}: could not mark the grid failed: {_one_line(mark_exc)}")
                    outcome = GridOutcome(int(mv_grid_id), "failed", error, 0.0)
                report(outcome)
        except KeyboardInterrupt:
            logger.warning("Interrupted: cancelling the pending MV grids (they stay pending)")
            for future in futures:
                future.cancel()
            raise
    return outcomes


# ---------------------------------------------------------------------------------------------- region steps
def _optional(dbc: Any, name: str, logger: logging.Logger, func, *args, **kwargs) -> Any:
    """Run an optional read inside a savepoint; on an error log it, roll back to the savepoint and return None."""
    dbc.cur.execute(f"SAVEPOINT {name}")
    try:
        result = func(*args, **kwargs)
        dbc.cur.execute(f"RELEASE SAVEPOINT {name}")
        return result
    except Exception as exc:
        dbc.cur.execute(f"ROLLBACK TO SAVEPOINT {name}")
        logger.warning(f"{name} skipped: {_one_line(exc)}")
        return None


def _substation_sites(dbc: Any, region_wkb: bytes, params: MvParameters, logger: logging.Logger,
                      warnings: list[str]) -> pd.DataFrame:
    """The HV/MV substation sites of the region (OSM or eGon) as a table for ``insert_substations``."""
    substations = _module("substations")
    egon_ok, egon_why = dbc.egon_available()
    egon = dbc.egon_substations(region_wkb, params.region_buffer_m) if egon_ok else None
    if params.substations == "egon":
        if not egon_ok:
            raise MvStageError(f"MV_SUBSTATIONS egon needs the eGon-data tables: {egon_why}")
        sites = substations.egon_sites(egon)
        if not sites:
            raise MvStageError(f"no eGon HV/MV substations within {params.region_buffer_m / 1000:g} km of the region")
        logger.info(f"eGon substations: {len(sites)} sites")
    else:
        candidates = dbc.osm_substation_candidates(region_wkb, params.region_buffer_m)
        selection = substations.select_osm_sites(candidates, params)
        sites = selection.sites
        logger.info(selection.describe())
        if not egon_ok:
            logger.warning(f"eGon cross-check disabled: {egon_why}")
    matches = substations.match_egon(sites, egon, params.egon_match_m) if egon is not None else None
    frame = substations.sites_frame(sites, region_wkb=region_wkb, matches=matches)
    if matches is not None and params.substations == "osm":
        unmatched = int(frame["egon_bus_id"].isna().sum())
        if unmatched:
            logger.info(f"{unmatched} of {len(frame)} OSM sites have no eGon substation within "
                        f"{params.egon_match_m:g} m")
    return frame.reset_index(drop=True)


def _generator_split(locations: pd.DataFrame, params: MvParameters) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Masks of the MaStR connection points: own MV node, LV side, HV (excluded); DESIGN §3.6.

    The ``treatment`` of ``inputs.aggregate_locations`` (``node``, ``lv_side``, ``excluded``) decides; without it
    the ``voltage_class`` does (``hv``, ``hvmv``, ``mv``, ``mvlv``, ``lv``): an ``mvlv`` point above
    ``generator_lv_side_kw`` is an own MV node, else it is LV side.
    """
    if "treatment" in locations.columns:
        treatment = locations["treatment"].astype(object)
        return treatment.eq("node"), treatment.eq("lv_side"), treatment.eq("excluded")
    cls = locations["voltage_class"].astype(object) if "voltage_class" in locations.columns \
        else pd.Series("mv", index=locations.index)
    kw = sum(pd.to_numeric(locations[c], errors="coerce").fillna(0.0)
             for c in ("gen_solar_kw", "gen_wind_kw", "gen_other_kw") if c in locations.columns)
    if isinstance(kw, int):          # no power columns
        kw = pd.Series(0.0, index=locations.index)
    node = cls.isin(["mv", "hvmv"]) | (cls.eq("mvlv") & (kw > params.generator_lv_side_kw))
    lv_side = cls.eq("lv") | (cls.eq("mvlv") & ~(kw > params.generator_lv_side_kw))
    return node, lv_side, cls.eq("hv")


def _node_rows(dbc: Any, version_id: str, plz: Sequence[int], region_wkb: bytes, sites: pd.DataFrame,
               params: MvParameters, lv: LvVersionParameters, extra_stations: pd.DataFrame | None,
               logger: logging.Logger, warnings: list[str]) -> pd.DataFrame:
    """The ``mv_node`` rows of the region: stations, MV customers, MV generators and busbars (DESIGN §3.4–§3.6)."""
    inputs = _module("inputs")
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
    # MaStR is only required for a variant that actually plans generation.
    generators = pd.DataFrame()
    if params.include_generation:
        units = dbc.mastr_units_in_region(region_wkb)
        locations = inputs.aggregate_locations(units, params).reset_index(drop=True)
        node_mask, lv_mask, hv_mask = _generator_split(locations, params)
        if hv_mask.any():
            logger.info(f"{int(hv_mask.sum())} MaStR connection points at HV level are not modelled")
        lv_points = locations[lv_mask].copy()
        lv_points["key"] = np.arange(len(lv_points), dtype=np.int64)
        targets = dbc.lv_side_targets(version_id, lv_points)
        aggregates = dbc.mastr_postcode_aggregates(plz)
        counts = dbc.station_building_counts(version_id, plz)
        generation = inputs.distribute_lv_generation(stations, targets, aggregates, counts)
        if generation is not None and len(generation):
            if "grid_result_id" not in generation.columns:
                generation = generation.rename_axis("grid_result_id").reset_index()
            gen = generation.groupby("grid_result_id")[[c for c in ("gen_solar_kw", "gen_wind_kw", "gen_other_kw")
                                                        if c in generation.columns]].sum()
            stations = stations.drop(columns=[c for c in gen.columns if c in stations.columns])
            stations = stations.merge(gen, left_on="grid_result_id", right_index=True, how="left")
        generators = locations[node_mask].reset_index(drop=True)
    for column in ("gen_solar_kw", "gen_wind_kw", "gen_other_kw"):
        stations[column] = (pd.to_numeric(stations[column], errors="coerce").fillna(0.0)
                            if params.include_generation and column in stations.columns else 0.0)
    customers = inputs.customer_table(dbc.mv_customer_rows(version_id, plz, lv), lv, params)
    stations, customers = inputs.busbar_customers(stations, customers, params)
    stations = inputs.upsize_busbar_stations(stations, lv)
    customers.attrs["n_upsized_stations"] = stations.attrs.get("n_upsized", 0)
    logger.info(inputs.describe_customers(customers.attrs, params))
    rows = inputs.node_rows(stations, customers, generators, sites, params).reset_index(drop=True)
    if not rows["kind"].eq("busbar").any():          # the busbar rows are part of node_rows; never go without them
        rows = pd.concat([_busbar_rows(sites), rows], ignore_index=True)
    if extra_stations is not None and len(extra_stations):
        rows = pd.concat([rows, _external_station_rows(extra_stations)], ignore_index=True)
    if not params.include_generation:
        for column in ("gen_solar_kw", "gen_wind_kw", "gen_other_kw"):
            rows[column] = 0.0
    return rows


def _busbar_rows(sites: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"kind": "busbar", "connection_hint": "busbar", "connection": "busbar",
                         "substation_id": sites["substation_id"].astype("int64").to_numpy(),
                         "x": sites["x"].astype(float).to_numpy(), "y": sites["y"].astype(float).to_numpy()})


def _external_station_rows(extra: pd.DataFrame) -> pd.DataFrame:
    """Zero-load station nodes for real stations without LV load (``extra_stations``, DESIGN §7.4)."""
    extra = pd.DataFrame(extra).reset_index(drop=True)
    source = extra["source_id"].astype(str) if "source_id" in extra.columns else pd.Series([""] * len(extra))
    return pd.DataFrame({
        "kind": "station", "connection_hint": "ring", "station_source": "external",
        "x": extra["x"].astype(float), "y": extra["y"].astype(float),
        "sn_kva": pd.to_numeric(extra["sn_kva"], errors="coerce") if "sn_kva" in extra.columns else np.nan,
        "load_stats": [{} for _ in range(len(extra))], "installed_kw": 0.0, "p_own_kw": 0.0, "households": 0.0,
        "flags": [["external"] + ([f"source:{s}"] if s else []) for s in source],
    })


def _attachment_updates(rows: pd.DataFrame, attach: pd.DataFrame, params: MvParameters,
                        logger: logging.Logger) -> pd.DataFrame:
    """Attachment columns per node from the KNN result (DESIGN §3.3).

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
                         params: MvParameters, lv: LvVersionParameters, region_wkb: bytes,
                         logger: logging.Logger) -> tuple[np.ndarray, np.ndarray, list[int]]:
    """Site index per node (into ``sites``), bridged flags and the overloaded site indices (DESIGN §4.1)."""
    districts = _module("districts")
    if params.supply_areas == "egon":
        polygons = dbc.egon_districts(region_wkb)
        bus_ids = [int(b) if pd.notna(b) else -1 for b in sites["egon_bus_id"]]
        owner = np.asarray(districts.egon_assignment(nodes[["x", "y"]].to_numpy(dtype=float), polygons, bus_ids),
                           dtype=np.int64)
        return owner, np.zeros(len(nodes), dtype=bool), []
    graph = _module("graph")
    edge_id, source, target, length = dbc.region_graph_arrays(graph_id)
    csr, vertex_index = graph.region_csr(source, target, length)
    edge_len = dict(zip(np.asarray(edge_id).tolist(), np.asarray(length, dtype=float).tolist()))
    ends = pd.DataFrame({"edge_source": source, "edge_target": target, "edge_length_m": length},
                        index=pd.Index(np.asarray(edge_id, dtype=np.int64), name="attach_edge_id"))
    sites_in = sites.join(ends, on="attach_edge_id")
    nodes_in = nodes.join(ends, on="attach_edge_id")
    capacity = districts.site_capacity_mva(sites_in, params)
    result = districts.routed_assignment(csr, vertex_index, edge_len, sites_in, nodes_in, lv.g(), lv.power_factor,
                                         capacity, params)
    logger.info(f"Supply areas: {result.iterations} rebalancing iterations, "
                f"{len(result.overloaded)} substations above capacity")
    bridged = np.asarray(result.bridged, dtype=bool) if result.bridged is not None \
        else np.zeros(len(nodes), dtype=bool)
    return np.asarray(result.site_of_node, dtype=np.int64), bridged, [int(i) for i in result.overloaded]


def _set_district_voltages(dbc: Any, version_id: str, variant_id: str, rows: pd.DataFrame,
                           grid_of_node: dict[int, int | None], params: MvParameters,
                           logger: logging.Logger) -> pd.DataFrame:
    """The nominal voltage of every district from its installed density (``MV_NOMINAL_VOLTAGE_KV``, DESIGN §4.6).

    Runs after the district areas are written: ``districts.installed_density`` of the station ratings and MV
    customer peaks of each district over its area, ``districts.district_voltages`` by the variant's rule; sets
    ``mv_grid.vn_kv`` and ``mv_grid.metrics['voltage_level']`` (caller commits).

    Args:
        rows: The node rows of the region with ``mv_node_id`` (``kind``, ``connection_hint``, ``sn_kva``,
            ``installed_kw``).
        grid_of_node: ``mv_grid_id`` per ``mv_node_id`` (None: not assigned).

    Returns:
        The table of ``districts.district_voltages`` (index ``mv_grid_id``).
    """
    districts = _module("districts")
    nodes = pd.DataFrame({column: rows[column] if column in rows.columns else np.nan
                          for column in ("mv_node_id", "kind", "connection_hint", "sn_kva", "installed_kw")})
    nodes["mv_grid_id"] = [grid_of_node.get(int(k)) for k in nodes["mv_node_id"]]
    nodes["mv_grid_id"] = pd.to_numeric(nodes["mv_grid_id"], errors="coerce")
    table = districts.district_voltages(districts.installed_density(nodes, dbc.grid_areas(version_id, variant_id)),
                                        params)
    dbc.set_grid_voltages(version_id, variant_id, table,
                          {int(g): districts.voltage_record(row, params) for g, row in table.iterrows()})
    logger.info(districts.describe_voltages(table, params))
    return table


def _region_step(dbc: Any, version_id: str, variant_id: str, plz: Sequence[int], region_wkb: bytes, graph_id: str,
                 params: MvParameters, lv: LvVersionParameters, extra_stations: pd.DataFrame | None,
                 logger: logging.Logger, warnings: list[str]) -> dict:
    """Write the substations, nodes, attachments, supply areas and grids of a variant (caller commits)."""
    # substations
    sites = _substation_sites(dbc, region_wkb, params, logger, warnings)
    sites["substation_id"] = dbc.insert_substations(version_id, variant_id, sites)
    # nodes
    rows = _node_rows(dbc, version_id, plz, region_wkb, sites, params, lv, extra_stations, logger, warnings)
    rows["mv_node_id"] = dbc.insert_nodes(version_id, variant_id, rows)
    # attachment to the routing graph (one KNN query for all nodes and busbars)
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
    if long_attach:
        logger.info(f"{len(long_attach)} substations are more than {params.substation_max_attach_m:g} m from the "
                    "street graph (long_attach): not used as supply points")
    usable = sites[~sites["substation_id"].isin(long_attach)].reset_index(drop=True)
    if not len(usable):
        raise MvStageError("no HV/MV substation is within reach of the street graph of the region")
    # supply areas
    nodes = dbc.variant_node_table(version_id, variant_id)
    members = nodes[nodes["kind"].isin(_KINDS_WITH_GRID)]
    # every node gets a district; generators and excluded nodes (HV customers) do not count for the capacity
    if len(members):
        owner, bridged, overloaded = _assign_supply_areas(dbc, graph_id, members, usable, params, lv, region_wkb,
                                                          logger)
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
    dbc.write_grid_areas(version_id, variant_id, plz)
    _set_district_voltages(dbc, version_id, variant_id, rows,
                           dict(zip(members.index.to_numpy(dtype=np.int64).tolist(), grid_of_node)), params, logger)
    kinds = members["kind"].value_counts().to_dict()
    logger.info(f"Region: {len(sites)} substations ({len(grids)} supplying), "
                f"{kinds.get('station', 0)} stations, {kinds.get('customer', 0)} customers, "
                f"{kinds.get('generator', 0)} generators")
    return {"n_sites": len(sites), "n_grids": len(grids), "kinds": kinds}


# ---------------------------------------------------------------------------------------------- the stage
def _status(totals: dict) -> str:
    if totals["grids_total"] == 0 or totals["grids_done"] == 0:
        return "failed"
    return "done" if totals["grids_done"] == totals["grids_total"] else "partial"


def run_mv_stage(version_id: str, plz_list: Sequence[int] | None, params: MvParameters | None = None, *,
                 parallel: bool = True, jobs: int | None = None, replace: bool = False,
                 extra_stations: pd.DataFrame | None = None, log_file: str = "log/log.txt") -> MvRunSummary:
    """Run the MV stage for one MV variant of a stored LV version (see the module docstring).

    Args:
        version_id: The LV version (never the configured ``VERSION_ID`` implicitly).
        plz_list: Requested PLZ; None means every PLZ with LV grids of the version (``--all``).
        params: The MV variant (default :meth:`MvParameters.from_config`).
        parallel: Plan the districts in worker processes.
        jobs: Number of workers (default ``min(4, N_JOBS)``, capped by the free memory).
        replace: Delete the variant first and build it from scratch.
        extra_stations: Real stations without LV load (``x``, ``y``, ``sn_kva``, ``source_id``): zero-load
            ``station`` nodes with ``station_source = 'external'`` (Python API only; used by new or rebuilt
            variants, not on resume).
        log_file: Log file of the run (workers log next to it).

    Returns:
        The run summary (also stored in ``mv_variant.summary``); ``status`` is ``done``, ``partial`` or ``failed``.

    Raises:
        MvStageError: The run cannot start (another run of the variant is active, a requested PLZ is being
            generated, no PLZ has LV grids, the LV snapshot is incomplete, no substation, ...).
        ValueError: The parameters differ from the stored snapshot of the variant.
    """
    params = params if params is not None else MvParameters.from_config()
    config = _config()
    variant_id = params.variant_id
    start = time.time()
    logger = _logger("MvGrid", log_file)
    print(f"✓ MV stage on {config.DBNAME}@{config.HOST}:{config.PORT}, version {version_id}, variant {variant_id}")
    dbc = _database_client(log_file)
    locked = False
    try:
        dbc.ensure_grid_persistence_schema()
        if not dbc.try_lock_variant(version_id, variant_id):
            raise MvStageError(f"another MV run of variant {variant_id} of version {version_id} is active")
        locked = True
        dbc.commit_changes()
        return _run_locked(dbc, version_id, params, plz_list, parallel=parallel, jobs=jobs, replace=replace,
                           extra_stations=extra_stations, log_file=log_file, logger=logger, start=start)
    finally:
        if locked:
            try:
                dbc.rollback_changes()
                dbc.unlock_variant(version_id, variant_id)
                dbc.commit_changes()
            except Exception as exc:   # the lock ends with the session anyway
                logger.warning(f"Could not release the lock of MV variant {variant_id}: {_one_line(exc)}")
        dbc.close()


def _run_locked(dbc: Any, version_id: str, params: MvParameters, plz_list: Sequence[int] | None, *, parallel: bool,
                jobs: int | None, replace: bool, extra_stations: pd.DataFrame | None, log_file: str,
                logger: logging.Logger, start: float) -> MvRunSummary:
    variant_id = params.variant_id
    warnings: list[str] = []
    logger.info(f"MV planning cases: {', '.join(params.active_cases)}"
                + (" (generation included)" if params.include_generation else " (load-only)"))
    # 1. PLZ
    requested = None if plz_list is None else sorted({int(p) for p in plz_list})
    used = [int(p) for p in dbc.generated_plz(version_id, requested)]
    missing = [] if requested is None else sorted(set(requested) - set(used))
    if missing:
        busy = [int(p) for p in dbc.busy_plz(missing)]
        if busy:
            raise MvStageError(f"PLZ {', '.join(map(str, busy))} of version {version_id} are being generated right "
                               "now; run the MV stage when the LV generation has finished")
        logger.warning(f"{len(missing)} requested PLZ have no LV grids in version {version_id}: "
                       + ", ".join(map(str, missing)))
    if not used:
        raise MvStageError(f"none of the requested PLZ has LV grids in version {version_id}")
    dbc.commit_changes()
    # 2. variant: snapshot check, graph, registration
    if replace and dbc.delete_variants(version_id, [variant_id]):
        logger.info(f"--replace: deleted MV variant {variant_id} of version {version_id}")
    row = dbc.variant_row(version_id, variant_id)
    if row is not None:
        dbc.check_variant_parameters(version_id, params)
    union = sorted(set(used) | set(row["plz_list"] if row is not None else []))
    lv = LvVersionParameters.from_generation_parameters(version_id, dbc.lv_snapshot(version_id))
    region_wkb = dbc.planning_region(union)
    dbc.commit_changes()
    graph_id = dbc.ensure_graph(region_wkb, params)
    fingerprint = dbc.lv_fingerprint(version_id, union)
    mode = dbc.register_variant(version_id, params, union, graph_id, lv, fingerprint)
    if mode == "grow" and len(union) > len(row["plz_list"]):
        logger.info(f"PLZ set grew from {len(row['plz_list'])} to {len(union)}: rebuilding all districts")
    elif mode == "grow":
        logger.info(f"routing graph changed from {row.get('graph_id')} to {graph_id}: rebuilding all districts")
    try:
        # 3. region
        if mode in ("new", "grow") or not dbc.variant_region_built(version_id, variant_id):
            try:
                dbc.reset_variant_region(version_id, variant_id)
                _region_step(dbc, version_id, variant_id, union, region_wkb, graph_id, params, lv, extra_stations,
                             logger, warnings)
                dbc.commit_changes()
            except Exception:
                dbc.rollback_changes()
                raise
        else:
            logger.info(f"Resuming MV variant {variant_id} of version {version_id}: region steps skipped")
        # 4. districts
        pending = dbc.pending_grids(version_id, variant_id)
        dbc.commit_changes()                       # no open transaction is inherited by the workers
        _run_districts(dbc, version_id, variant_id, pending, params, lv, graph_id, parallel=parallel, jobs=jobs,
                       log_file=log_file, logger=logger, warnings=warnings)
        # 5. post
        ties = None
        if params.interconnection == "ties":
            ties = _plan_ties(dbc, version_id, variant_id, graph_id, params, logger, warnings)
        totals = dbc.variant_totals(version_id, variant_id)
        dbc.commit_changes()
        demand = _demand_anchor(dbc, version_id, variant_id, union, lv, logger)
    except Exception as exc:
        dbc.rollback_changes()
        summary = _summary(version_id, variant_id, "failed", requested, used, missing, None,
                           warnings + [_one_line(exc)], start, params=params)
        try:
            dbc.finish_variant(version_id, variant_id, summary)
        except Exception as finish_exc:
            logger.error(f"Could not store the failed status of MV variant {variant_id}: {_one_line(finish_exc)}")
        raise
    summary = _summary(version_id, variant_id, _status(totals), requested, used, missing, totals, warnings, start,
                       demand, ties=ties, params=params)
    dbc.finish_variant(version_id, variant_id, summary)                 # summary['ties'] holds the tie summary
    logger.info(summary.describe().splitlines()[0])
    return summary


def _plan_ties(dbc: Any, version_id: str, variant_id: str, graph_id: str, params: MvParameters,
               logger: logging.Logger, warnings: list[str]) -> dict | None:
    """Plan and store the normally-open ties between the districts (post step, DESIGN §4.5).

    The ties of the variant are always planned anew from its done districts (a resumed run replaces them).

    Returns:
        The tie summary of :func:`pylovo.mv_grid.interconnection.tie_summary` (stored as
        ``mv_variant.summary['ties']``), or None when the module is missing.
    """
    try:
        interconnection = _module("interconnection")
    except ImportError:
        message = "ties not implemented in this build: MV_INTERCONNECTION ties was ignored"
        logger.warning(message)
        warnings.append(message)
        return None
    summary = interconnection.plan_and_save_ties(dbc, version_id, variant_id, graph_id, params, logger=logger)
    logger.info(interconnection.describe_ties(summary))
    return summary


def _demand_anchor(dbc: Any, version_id: str, variant_id: str, plz: Sequence[int], lv: LvVersionParameters,
                   logger: logging.Logger) -> dict:
    """The regional demand anchor of a variant for its summary (``inputs.demand_anchor``), logged.

    A report only: an error is logged, the read is rolled back and the anchor stays empty.
    """
    try:
        anchor = _module("inputs").demand_anchor(dbc.variant_node_table(version_id, variant_id),
                                                 dbc.plz_population(plz), lv)
    except Exception as exc:
        logger.warning(f"Demand anchor skipped: {_one_line(exc)}")
        dbc.rollback_changes()
        return {}
    logger.info(demand_line(anchor)[2:])
    return anchor


def _summary(version_id: str, variant_id: str, status: str, requested: list[int] | None, used: list[int],
             missing: list[int], totals: dict | None, warnings: list[str], start: float,
             demand: dict | None = None, *, ties: dict | None = None,
             params: MvParameters | None = None) -> MvRunSummary:
    totals = totals or {}
    voltages = {}
    if params is not None:
        voltages = {"rule": "auto" if params.auto_voltage else "fixed",
                    "threshold_mva_km2": float(params.mv_10kv_min_density_mva_km2),
                    "grids": dict(totals.get("grids_by_kv") or {})}
    return MvRunSummary(
        version_id=version_id, variant_id=variant_id, status=status,
        plz_requested=list(requested if requested is not None else used), plz_used=list(used),
        plz_missing=list(missing), grids_total=int(totals.get("grids_total", 0)),
        grids_done=int(totals.get("grids_done", 0)), grids_failed=int(totals.get("grids_failed", 0)),
        grids_pending=int(totals.get("grids_pending", 0)), n_stations=int(totals.get("n_stations", 0)),
        n_customers=int(totals.get("n_customers", 0)), n_generators=int(totals.get("n_generators", 0)),
        circuit_km=float(totals.get("circuit_km", 0.0)), cable_share=float(totals.get("cable_share", 0.0)),
        n_violation_grids=int(totals.get("n_violation_grids", 0)),
        n_band_conflicts=int(totals.get("n_band_conflicts", 0)), warnings=list(warnings),
        elapsed_s=time.time() - start, demand=dict(demand or {}), ties=dict(ties or {}), voltages=voltages,
        active_cases=params.active_cases if params is not None else CASES)


# ---------------------------------------------------------------------------------------------- export
def export_net(version_id: str, variant_id: str, mv_grid_id: int, output: Path, case: str = "load") -> Path:
    """Write the stored pandapower net of one MV grid to a JSON file (``pylovo-mv export``).

    Args:
        case: ``load`` (the stored net with its load-case results), or another case of
            :data:`~pylovo.mv_grid.model.CASES`, applied with ``electrical.case_net`` and solved before writing.

    Returns:
        The written path.

    Raises:
        ValueError: Unknown case.
        LookupError: The variant or grid does not exist, or the grid has no stored net.
    """
    import pandapower as pp

    if case not in CASES:
        raise ValueError(f"case must be one of {', '.join(CASES)}, got {case!r}")
    output = Path(output)
    with _database_client("log/log.txt") as dbc:
        row = dbc.variant_row(version_id, variant_id)
        if row is None:
            raise LookupError(f"MV variant {variant_id} of version {version_id} does not exist")
        params = MvParameters.from_snapshot(variant_id, row.get("mv_variant_comment") or "", row["mv_parameters"])
        if case not in params.active_cases:
            raise ValueError(f"case {case!r} was not requested for MV variant {variant_id}; "
                             f"available cases: {', '.join(params.active_cases)}")
        _, grid = dbc.load_district_nodes(mv_grid_id)
        if grid["version_id"] != version_id or grid["mv_variant_id"] != variant_id:
            raise LookupError(f"MV grid {mv_grid_id} does not belong to variant {variant_id} of version {version_id}")
        net = dbc.read_mv_grid_net(mv_grid_id)
    if case != "load":
        net = _module("electrical").case_net(net, case, params)
        try:
            pp.runpp(net, algorithm="nr", init="auto")
        except Exception as exc:   # the net is written without results
            logging.getLogger(__name__).warning(f"Case {case} of MV grid {mv_grid_id} did not converge: {exc}")
    output.parent.mkdir(parents=True, exist_ok=True)
    pp.to_json(net, str(output))
    return output
