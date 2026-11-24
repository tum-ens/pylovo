"""The steps of the MV stage after all districts: ties, links, display geometry and the reports of the summary.

:func:`plan_ties` and :func:`plan_links` plan the normally-open ties between the districts and the links inside
them (:mod:`~pylovo.mv_grid.interconnection`); :func:`run_post_step` keeps the done districts when one of them fails.
:func:`display_geometry` stores the lanes of the map, :func:`demand_anchor` and :func:`osm_coverage_report` add the
regional demand anchor and the OSM completeness to the summary. Every step reads and writes the variant in the
database; the planning modules it uses are imported on first use.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence

import pandas as pd

from pylovo.mv_grid import runtime
from pylovo.mv_grid.model import demand_line
from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters


def run_post_step(dbc: Any, name: str, logger: logging.Logger, warnings: list[str], func, *args) -> dict | None:
    """Run a post step of the districts (ties, links); an error (out of memory included) is logged, rolled back and
    kept as a warning of the summary, so the done districts stay and the variant keeps the status of its districts.
    Ctrl-C still stops the run."""
    try:
        return func(*args)
    except Exception as exc:
        message = (f"{name} failed: {runtime.one_line(exc)}; the districts are kept, rerun pylovo-mv generate of the "
                   "variant to plan them again")
        logger.error(message)
        warnings.append(message)
        dbc.rollback_changes()
        return None


def plan_ties(dbc: Any, version_id: str, variant_id: str, graph_id: str, params: MvParameters,
              logger: logging.Logger) -> dict:
    """Plan and store the normally-open ties between the districts (post step).

    The ties of the variant are always planned anew from its done districts (a resumed run replaces them).

    Returns:
        The tie summary of :func:`pylovo.mv_grid.interconnection.tie_summary` (stored as
        ``mv_variant.summary['ties']``).
    """
    interconnection = runtime.module("interconnection")
    logger.info("Planning the ties between the districts")
    summary = interconnection.plan_and_save_ties(dbc, version_id, variant_id, graph_id, params, logger=logger)
    logger.info(interconnection.describe_ties(summary))
    return summary


def plan_links(dbc: Any, version_id: str, variant_id: str, graph_id: str, params: MvParameters,
               logger: logging.Logger) -> dict:
    """Plan and store the normally-open links between the rings of each district (post step after the ties).

    Returns:
        The link summary of :func:`pylovo.mv_grid.interconnection.link_summary` (stored as
        ``mv_variant.summary['links']``).
    """
    interconnection = runtime.module("interconnection")
    logger.info("Planning the links inside the districts")
    summary = interconnection.plan_and_save_links(dbc, version_id, variant_id, graph_id, params, logger=logger)
    logger.info(interconnection.describe_links(summary))
    return summary


def display_geometry(dbc: Any, version_id: str, variant_id: str,
                     logger: logging.Logger) -> tuple[int, int] | None:
    """Store the display geometry of a variant's lines and busbar circles (``display.layout``); logged.

    For maps only: an error is logged, rolled back and leaves the display geometry empty (readers then draw
    ``geom``). Returns the number of lines with a display geometry (lines that share a route) and of busbar
    circles, or None after an error.
    """
    try:
        logger.info("Display geometry of the lines")
        lines, busbars = dbc.display_input(version_id, variant_id)
        result = runtime.module("display").layout(lines, busbars)
        dbc.save_display_geometry(version_id, variant_id, result.lines, result.busbar_polygons())
    except Exception as exc:
        logger.warning(f"Display geometry skipped: {runtime.one_line(exc)}")
        dbc.rollback_changes()
        return None
    logger.info(f"Display geometry: {len(result.lines)} of {len(lines)} lines in lanes, "
                f"{len(result.busbars)} busbar circles")
    return len(result.lines), len(result.busbars)


def demand_anchor(dbc: Any, version_id: str, variant_id: str, plz: Sequence[int], lv: LvVersionParameters,
                  logger: logging.Logger) -> dict:
    """The regional demand anchor of a variant for its summary (``inputs.demand_anchor``), logged.

    A report only: an error is logged, the read is rolled back and the anchor stays empty.
    """
    try:
        anchor = runtime.module("inputs").demand_anchor(dbc.variant_node_table(version_id, variant_id),
                                                 dbc.plz_population(plz), lv)
    except Exception as exc:
        logger.warning(f"Demand anchor skipped: {runtime.one_line(exc)}")
        dbc.rollback_changes()
        return {}
    logger.info(demand_line(anchor)[2:])
    return anchor


def osm_coverage_report(dbc: Any, version_id: str, variant_id: str, region_wkb: bytes, graph_id: str,
                        logger: logging.Logger) -> dict:
    """The OSM completeness per land-use class of a variant for its summary (``osm_coverage.summarise``), logged.

    A report only: without an OSM schema, or on any error, it is logged, the read is rolled back and the record
    stays empty.
    """
    try:
        import shapely

        osm_coverage = runtime.module("osm_coverage")
        region = shapely.from_wkb(bytes(region_wkb))

        def shapes(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
            out = frame[list(columns)].copy()
            out["geometry"] = [shapely.from_wkb(g) if g is not None else None for g in frame["geom_wkb"]]
            return out

        settlements = shapes(dbc.settlement_polygons(graph_id, region_wkb), ("urban",))
        landuse = shapes(dbc.osm_landuse(region_wkb), ("osm_type",))
        nodes = dbc.variant_node_table(version_id, variant_id)
        stations = nodes[nodes["kind"] == "station"][["x", "y"]].to_numpy(dtype=float)
        lines = [shapely.from_wkb(g) for g in dbc.osm_line_geometries(graph_id, region_wkb)]
        by_settlement, by_land_use, record = osm_coverage.summarise(region, dbc.osm_power_objects(region_wkb),
                                                                     stations, settlements, landuse, lines)
        dbc.rollback_changes()
    except Exception as exc:
        logger.warning(f"OSM coverage skipped: {runtime.one_line(exc)}")
        dbc.rollback_changes()
        return {}
    logger.info("OSM coverage per settlement class:\n" + osm_coverage.describe_table(by_settlement))
    logger.info("OSM coverage per land-use class:\n" + osm_coverage.describe_table(by_land_use))
    return record
