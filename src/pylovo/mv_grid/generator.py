"""The run control of the MV stage: lock, variant, district workers and summary.

:func:`run_mv_stage` runs the MV stage for one MV variant of a stored LV version:

0. **Safety:** prints the database target, checks that no migration is pending and takes the variant
   lock (advisory lock 907362) for the whole run.
1. **PLZ:** the requested PLZ with LV grids of the version; requested PLZ without grids are try-locked (907361): a
   PLZ that is being generated right now stops the run before any write, the others are reported as missing.
2. **Variant:** the parameter snapshot is checked first (no write), then the routing graph is built or reused
   (own commit), then the variant is registered once (``new``, ``resume`` or ``grow``, union semantics of the PLZ
   set).
3. **Region** (one transaction): HV/MV substations, MV nodes (stations with their load statistics, MV customers,
   busbars), their attachment to the routing graph, the supply areas and
   one pending ``mv_grid`` per supplying substation with its Voronoi-cell polygon and its nominal voltage (with
   ``MV_NOMINAL_VOLTAGE_KV`` auto 10 kV from an installed density of ``mv_10kv_min_density_mva_km2``, else
   20 kV). Skipped when a run resumes.
4. **Districts:** one task per ``pending`` or ``failed`` grid, largest first, in a ``fork`` process pool (or in
   process): each worker opens its own database connection, reads its district, plans it with the pure
   :func:`~pylovo.mv_grid.planner.plan_district` and stores it in one transaction. A failed district is marked
   ``failed`` with its error; the others continue. A broken pool (e.g. out of memory) leaves the remaining grids
   ``pending``; a rerun with fewer ``jobs`` resumes them.
5. **Post:** normally-open ties between districts and links inside them, then the summary (``mv_variant.summary``)
   and the final status ``done``, ``partial`` or ``failed`` of the districts. A failed tie or link step (out of
   memory included) keeps the districts and becomes a warning of the summary; a rerun of the variant plans it again.

This module is the MV counterpart of :mod:`pylovo.lv_grid.generator`: it controls the run and keeps the worker
pool. The steps are modules of their own: the region step :mod:`~pylovo.mv_grid.preparation`, the input of one
district :mod:`~pylovo.mv_grid.district_input`, the steps after the districts :mod:`~pylovo.mv_grid.post_steps`;
the helpers they share are in :mod:`~pylovo.mv_grid.runtime`. The planning modules (``graph``, ``substations``,
``inputs``, ``districts``, ...) are imported when they are used, so this module imports quickly.
"""
from __future__ import annotations

import logging
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from typing import Any, Sequence

from pylovo.mv_grid import district_input, post_steps, preparation, runtime
from pylovo.mv_grid.model import CASES, GridOutcome, MvRunSummary
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters, MvStageError


# ---------------------------------------------------------------------------------------------- district workers
def _grid_worker(version_id: str, variant_id: str, mv_grid_id: int, params_snapshot: dict, comment: str,
                 lv_json: dict, graph_id: str, log_file: str) -> GridOutcome:
    """Plan and store one district (runs in a worker process, or in process).

    Only picklable arguments; the worker opens its own database connection and closes it at the end, and never
    uses a connection inherited from the parent. It logs to ``log_mv_<version>_<variant>_<mv_grid_id>.txt`` next to
    ``log_file``. Every exception is caught: the grid is marked ``failed`` with the error text.
    """
    start = time.time()
    worker_log = str(Path(log_file).parent / f"log_mv_{version_id}_{variant_id}_{mv_grid_id}.txt")
    logger = runtime.create_logger(f"MvGrid.{version_id}.{variant_id}.{mv_grid_id}", worker_log)
    dbc = None
    try:
        dbc = runtime.database_client(log_file)
        params = MvParameters.from_snapshot(variant_id, comment, params_snapshot)
        lv = LvVersionParameters.from_json(lv_json)
        inp, requirements = district_input.district_input(dbc, version_id, variant_id, mv_grid_id, graph_id, params, lv)
        dbc.rollback_changes()                     # no open transaction while the district is planned
        logger.info(f"MV grid {mv_grid_id}: {len(inp.nodes)} nodes, {len(inp.edges.edge_id)} edges")
        plan = runtime.module("planner").plan_district(inp)
        runtime_s = time.time() - start
        dbc.save_district(version_id, variant_id, mv_grid_id, plan, runtime_s, requirements=requirements)
        logger.info(f"MV grid {mv_grid_id} stored: {plan.power_flow_status}, {len(plan.topology.rings)} rings, "
                    f"{len(plan.repairs)} repairs, {runtime_s:.1f} s")
        return GridOutcome(int(mv_grid_id), "done", None, runtime_s)
    except Exception as exc:
        runtime_s = time.time() - start
        error = runtime.one_line(exc)
        logger.exception(f"MV grid {mv_grid_id} failed: {error}")
        if dbc is not None:
            try:
                dbc.rollback_changes()
                dbc.mark_grid_failed(mv_grid_id, error)
            except Exception as mark_exc:   # the connection may be gone
                logger.error(f"MV grid {mv_grid_id}: could not mark the grid failed: {runtime.one_line(mark_exc)}")
        return GridOutcome(int(mv_grid_id), "failed", error, runtime_s)
    finally:
        if dbc is not None:
            dbc.close()
        runtime.close_logger(logger)


def _run_districts(dbc: Any, version_id: str, variant_id: str, pending: Sequence[tuple[int, int]],
                   params: MvParameters, lv: LvVersionParameters, graph_id: str, *, parallel: bool,
                   jobs: int | None, log_file: str, logger: logging.Logger, warnings: list[str]) -> list[GridOutcome]:
    """Plan the pending districts (largest first) in a fork pool or in process; one outcome per finished grid."""
    if not pending:
        return []
    n = runtime.worker_count(jobs, len(pending)) if parallel else 1
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
                    message = (f"the MV worker pool broke ({runtime.one_line(exc)}; out of memory?): the remaining "
                               "grids stay pending, rerun with fewer --jobs")
                    logger.error(message)
                    warnings.append(message)
                    for other in futures:
                        other.cancel()
                    break
                except Exception as exc:     # an error outside the worker's own handling (e.g. pickling)
                    error = runtime.one_line(exc)
                    try:
                        dbc.rollback_changes()
                        dbc.mark_grid_failed(mv_grid_id, error)
                    except Exception as mark_exc:
                        logger.error(f"MV grid {mv_grid_id}: could not mark the grid failed: "
                                     f"{runtime.one_line(mark_exc)}")
                    outcome = GridOutcome(int(mv_grid_id), "failed", error, 0.0)
                report(outcome)
        except KeyboardInterrupt:
            logger.warning("Interrupted: cancelling the pending MV grids (they stay pending)")
            for future in futures:
                future.cancel()
            raise
    return outcomes


# ---------------------------------------------------------------------------------------------- the stage
def _status(totals: dict) -> str:
    if totals["grids_total"] == 0 or totals["grids_done"] == 0:
        return "failed"
    return "done" if totals["grids_done"] == totals["grids_total"] else "partial"


def run_mv_stage(version_id: str, plz_list: Sequence[int] | None, params: MvParameters | None = None, *,
                 parallel: bool = True, jobs: int | None = None, replace: bool = False,
                 log_file: str = "log/log.txt", osm_coverage: bool | None = None) -> MvRunSummary:
    """Run the MV stage for one MV variant of a stored LV version (see the module docstring).

    Args:
        version_id: The LV version (never the configured ``VERSION_ID`` implicitly).
        plz_list: Requested PLZ; None means every PLZ with LV grids of the version (``--all``).
        params: The MV variant (default :meth:`MvParameters.from_config`).
        parallel: Plan the districts in worker processes.
        jobs: Number of workers (default ``min(4, N_JOBS)``, capped by the free memory).
        replace: Delete the variant first and build it from scratch.
        log_file: Log file of the run (workers log next to it).
        osm_coverage: Add the OSM coverage report to the summary (default ``MV_OSM_COVERAGE``).

    Returns:
        The run summary (also stored in ``mv_variant.summary``); ``status`` is ``done``, ``partial`` or ``failed``.

    Raises:
        MvStageError: The run cannot start (another run of the variant is active, a requested PLZ is being
            generated, no PLZ has LV grids, the LV snapshot is incomplete, no substation, ...).
        ValueError: The parameters differ from the stored snapshot of the variant.
    """
    params = params if params is not None else MvParameters.from_config()
    config = runtime.config()
    variant_id = params.variant_id
    start = time.time()
    logger = runtime.create_logger("MvGrid", log_file)
    print(f"✓ MV stage on {config.DBNAME}@{config.HOST}:{config.PORT}, version {version_id}, variant {variant_id}")
    dbc = runtime.database_client(log_file)
    locked = False
    try:
        dbc.ensure_grid_persistence_schema()
        for message in dbc.check_mv_sources(auto_voltage=params.auto_voltage):
            logger.warning(message)
        logger.info(f"InfDB source data read from {dbc.source_reader().where}")
        if not dbc.try_lock_variant(version_id, variant_id):
            raise MvStageError(f"another MV run of variant {variant_id} of version {version_id} is active")
        locked = True
        dbc.commit_changes()
        return _run_locked(dbc, version_id, params, plz_list, parallel=parallel, jobs=jobs, replace=replace,
                           log_file=log_file, logger=logger, start=start,
                           osm_coverage=config.MV_OSM_COVERAGE if osm_coverage is None else bool(osm_coverage))
    finally:
        if locked:
            try:
                dbc.rollback_changes()
                dbc.unlock_variant(version_id, variant_id)
                dbc.commit_changes()
            except Exception as exc:   # the lock ends with the session anyway
                logger.warning(f"Could not release the lock of MV variant {variant_id}: {runtime.one_line(exc)}")
        dbc.close()


def _run_locked(dbc: Any, version_id: str, params: MvParameters, plz_list: Sequence[int] | None, *, parallel: bool,
                jobs: int | None, replace: bool, log_file: str, logger: logging.Logger,
                start: float, osm_coverage: bool = True) -> MvRunSummary:
    variant_id = params.variant_id
    warnings: list[str] = []
    logger.info(f"MV planning cases: {', '.join(CASES)} (load-only)")
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
    elif mode == "resume" and row.get("lv_fingerprint") not in (None, fingerprint):
        stale = (f"The LV results of the PLZ of MV variant {variant_id} changed since it was built (load edits, "
                 "deleted or regenerated LV grids): the resumed districts read the new LV results, the stored ones "
                 "keep the old. Plan it again with --replace or under a new id")
        logger.warning(stale)
        warnings.append(stale)
    try:
        # 3. region
        if mode in ("new", "grow") or not dbc.variant_region_built(version_id, variant_id):
            try:
                dbc.reset_variant_region(version_id, variant_id)
                preparation.region_step(dbc, version_id, variant_id, union, region_wkb, graph_id, params, lv, logger,
                                        warnings)
                dbc.commit_changes()
            except Exception:
                dbc.rollback_changes()
                raise
        else:
            logger.info(f"Resuming MV variant {variant_id} of version {version_id}: region steps skipped")
        # 4. districts
        pending = dbc.pending_grids(version_id, variant_id)
        dbc.commit_changes()                       # no open transaction is inherited by the workers
        dbc.source_reader().close()                # nor the InfDB connection (opened again when needed)
        _run_districts(dbc, version_id, variant_id, pending, params, lv, graph_id, parallel=parallel, jobs=jobs,
                       log_file=log_file, logger=logger, warnings=warnings)
        # 5. post: a failure here keeps the done districts; a rerun of the variant plans the step again
        ties = post_steps.run_post_step(dbc, "Ties", logger, warnings, post_steps.plan_ties, dbc, version_id,
                                        variant_id, graph_id, params, logger)
        links = post_steps.run_post_step(dbc, "Links", logger, warnings, post_steps.plan_links, dbc, version_id,
                                         variant_id, graph_id, params, logger)
        post_steps.display_geometry(dbc, version_id, variant_id, logger)
        totals = dbc.variant_totals(version_id, variant_id)
        dbc.commit_changes()
        demand = post_steps.demand_anchor(dbc, version_id, variant_id, union, lv, logger)
        if osm_coverage:
            coverage = post_steps.osm_coverage_report(dbc, version_id, variant_id, region_wkb, graph_id, logger)
        else:
            coverage = {}
            logger.info("OSM coverage report skipped (MV_OSM_COVERAGE: False)")
    except BaseException as exc:                   # also Ctrl-C: the variant must not stay 'running'
        totals = None
        try:
            dbc.rollback_changes()
            totals = dbc.variant_totals(version_id, variant_id)     # the districts stored before the failure
        except Exception as totals_exc:
            logger.error(f"Could not count the grids of MV variant {variant_id}: {runtime.one_line(totals_exc)}")
        summary = _summary(version_id, variant_id, "failed", requested, used, missing, totals,
                           warnings + [runtime.one_line(exc)], start, params=params)
        try:
            dbc.finish_variant(version_id, variant_id, summary)
        except Exception as finish_exc:
            logger.error(f"Could not store the failed status of MV variant {variant_id}: "
                         f"{runtime.one_line(finish_exc)}")
        raise
    summary = _summary(version_id, variant_id, _status(totals), requested, used, missing, totals, warnings, start,
                       demand, ties=ties, params=params, osm_coverage=coverage, links=links)
    dbc.finish_variant(version_id, variant_id, summary)                 # summary['ties'] holds the tie summary
    logger.info(summary.describe().splitlines()[0])
    return summary


def _summary(version_id: str, variant_id: str, status: str, requested: list[int] | None, used: list[int],
             missing: list[int], totals: dict | None, warnings: list[str], start: float,
             demand: dict | None = None, *, ties: dict | None = None,
             params: MvParameters | None = None, osm_coverage: dict | None = None,
             links: dict | None = None) -> MvRunSummary:
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
        n_customers=int(totals.get("n_customers", 0)),
        circuit_km=float(totals.get("circuit_km", 0.0)), cable_share=float(totals.get("cable_share", 0.0)),
        n_violation_grids=int(totals.get("n_violation_grids", 0)),
        n_band_conflicts=int(totals.get("n_band_conflicts", 0)), warnings=list(warnings),
        elapsed_s=time.time() - start, demand=dict(demand or {}), ties=dict(ties or {}), voltages=voltages,
        osm_coverage=dict(osm_coverage or {}), links=dict(links or {}))
