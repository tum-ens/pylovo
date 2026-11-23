"""Analysis of the stored LV grids: the statistics of a postcode and the key figures of every grid.

:class:`ParameterCalculator` reads the grids of a postcode from the database and stores

- the statistics of the postcode (``plz_parameters``: counts, cable lengths, per-transformer figures), and
- the key figures of every grid (``clustering_parameters``, :func:`pylovo.analysis.grid_analysis.key_figures`).

The figures themselves are computed from the pandapower net of a grid, without the database, by
:mod:`pylovo.analysis.net_metrics` and :mod:`pylovo.analysis.impedance_metrics`.
"""

from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
import json
from typing import NamedTuple, Tuple

import pandapower.topology as top

import pylovo.database.database_client as dbc
from pylovo.analysis import net_metrics
from pylovo.analysis.grid_analysis import KEY_FIGURES_VERSION, key_figures
from pylovo import utils
from pylovo.config_loader import LOG_LEVEL, N_JOBS, PARALLEL, VERSION_ID


# Tables of a stored net that the per-transformer analysis reads: the loads and transformer, and
# every element pandapower.topology.create_nxgraph turns into graph edges.
TRANSFORMER_ANALYSIS_TABLES = frozenset({
    "bus", "bus_dc", "load", "trafo", "trafo3w", "line", "line_dc", "impedance", "tcsc", "dcline", "vsc", "switch",
})


class GridParameterCounts(NamedTuple):
    """What the per-grid analysis of one PLZ did (:meth:`ParameterCalculator.calculate_grid_parameters`)."""

    #: ``"analysed"``, ``"not_analysed"`` (no PLZ-level parameters, nothing done) or ``"failed"`` (the PLZ could not
    #: be processed, see the log)
    status: str
    #: grids whose parameters were calculated and stored
    calculated: int = 0
    #: grids that already had parameters
    skipped: int = 0
    #: grids whose calculation failed (logged)
    failed: int = 0


class ParameterCalculator:
    """Compute and store the analysis of the grids of a postcode (the database side of the analysis).

    1) Postcode level (:meth:`analyze_parameters_for_plz`): aggregate statistics across all grids of a PLZ.
    2) Grid level (:meth:`analyze_grid_parameters_for_plz`): the key figures of every grid.

    Attributes:
        plz (int): Postcode area ID
        bcid (int): Building cluster ID (negative for brownfield grids around an existing transformer)
        kcid (int): K-means cluster ID of the grid
        version_id (str): Analysis version taken from configuration
        dbc (DatabaseClient): Database client used for I/O of pandapower nets and parameters
    """

    def __init__(self):
        self.dbc = dbc.DatabaseClient()
        self.version_id = VERSION_ID
        self.plz = None
        self.kcid = None
        self.bcid = None

    def _require_plz_context(self, method_name: str) -> int:
        """Require an active PLZ context for stateful workflows and lookups."""
        if self.plz is None:
            raise ValueError(f"{method_name} requires calculator.plz to be set before use.")
        return self.plz

    def _require_grid_context(self, method_name: str) -> Tuple[int, int]:
        """Require an active grid context for grid-scoped stateful methods."""
        if self.kcid is None or self.bcid is None:
            raise ValueError(
                f"{method_name} requires calculator.kcid and calculator.bcid to be set before use."
            )
        return self.kcid, self.bcid

    def analyze_parameters_for_plz(self, plz: int | None = None) -> str:
        """Compute and store PLZ-wide parameters.

        Args:
            plz (int): Postcode area ID

        Returns:
            ``"no_grids"`` if the PLZ has no grids for the ``VERSION_ID`` (nothing is done),
            ``"already_analysed"`` if its parameters exist, else ``"analysed"``.

        Side effects:
            - Reads all nets of the PLZ from the database.
            - Writes aggregated per-PLZ results and sets analysis flags.
            - Skips PLZs already analyzed.
        """
        if plz is None:
            raise ValueError("analyze_parameters_for_plz requires an explicit PLZ value.")

        self.plz = plz
        grid_generated = self.dbc.is_grid_generated(self.plz)
        if not grid_generated:
            self.dbc.logger.info(f"Grid for the postcode area {self.plz} is not generated, yet. Generate it first.")
            return "no_grids"
        grid_analysed = self.dbc.is_grid_analyzed(self.plz)
        if grid_analysed:
            self.dbc.logger.info(f"Grid for the postcode area {self.plz} has already been analyzed.")
            return "already_analysed"

        try:
            self.dbc.logger.info(f"PLZ {self.plz}: start basic result analysis")
            self.analyze_basic_parameters_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: start cable counting")
            self.analyze_cable_lengths_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: start per-trafo analysis")
            self.analyze_transformer_parameters_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: result analysis finished")
            self.dbc.conn.commit()
            return "analysed"
        except Exception as e:
            self.dbc.logger.error(f"Error during analysis for PLZ {self.plz}: {e}")
            self.dbc.logger.info(f"Skipped PLZ {self.plz} due to analysis error.")
            raise

    def analyze_grid_parameters_for_plz(self, plz: int | None = None) -> str:
        """Compute and store per-grid parameters for all grids of an analyzed PLZ.

        Prints the number of grids, one line per calculated grid and a summary.
        :func:`analyze_grid_parameters_for_multiple_plz` analyses several PLZ, in worker processes if possible.

        Args:
            plz (int): Postcode area ID

        Returns:
            ``"not_analysed"`` if the PLZ-level parameters are missing (nothing is done), else ``"analysed"``.

        Note:
            Runs only after the PLZ-level analysis (:meth:`analyze_parameters_for_plz`).
        """
        if plz is None:
            raise ValueError("analyze_grid_parameters_for_plz requires an explicit PLZ value.")
        return self.calculate_grid_parameters(plz, progress=True).status

    def calculate_grid_parameters(self, plz: int, progress: bool = False) -> "GridParameterCounts":
        """Compute and store the per-grid parameters of every grid of ``plz`` that has none of the current definitions.

        Grids that already have a row of ``KEY_FIGURES_VERSION`` in ``clustering_parameters`` are skipped; a row of
        older definitions is computed again and replaced. A grid whose calculation fails is logged, its open
        transaction is rolled back and the next grid is calculated.

        Args:
            plz: Postcode area; its PLZ-level parameters (:meth:`analyze_parameters_for_plz`) must exist.
            progress: Print the number of grids, one line per calculated grid and a summary.

        Returns:
            The counts. Status ``"not_analysed"`` (nothing is done) if the PLZ-level parameters are missing.
        """
        self.plz = plz
        grid_analysed = self.dbc.is_grid_analyzed(self.plz)
        if not grid_analysed:
            self.dbc.logger.info(
                f"PLZ parameters for the postcode area {self.plz} missing. Please run analyze_parameters_for_plz() first.")
            return GridParameterCounts("not_analysed")

        cluster_list = self.dbc.get_list_from_plz(self.plz)
        if progress:
            print(f"Checking {len(cluster_list)} grids for PLZ {self.plz}...")

        skipped = 0
        calculated = 0
        failed = 0

        for kcid, bcid in cluster_list:
            # Check if this specific grid already has parameters
            try:
                if self.dbc.has_clustering_parameters(self.plz, kcid, bcid, KEY_FIGURES_VERSION):
                    skipped += 1
                    continue

                if progress:
                    print(f"Calculating parameters for grid kcid={kcid}, bcid={bcid}")
                self.analyze_single_grid(bcid, kcid)
                calculated += 1

            except Exception as e:
                failed += 1
                self.dbc.logger.error(
                    f"Failed to calculate/insert parameters for grid {kcid},{bcid} in PLZ {self.plz}: {e}"
                )
                # a failed statement aborts the transaction: without the rollback every later grid would fail too
                self.dbc.rollback_changes()

        if progress:
            print(f"Finished PLZ {self.plz}. Calculated new: {calculated}, Skipped existing: {skipped}.")
        return GridParameterCounts("analysed", calculated, skipped, failed)

    def analyze_single_grid(self, bcid: int, kcid: int) -> None:
        """Compute clustering parameters for a single local grid and persist them.

        Args:
            bcid (int): Building cluster ID
            kcid (int): Grid cluster ID

        Writes a record to the clustering-parameters table including metadata
        (plz, bcid, kcid, version, osm_trafo flag, ``KEY_FIGURES_VERSION``), replacing a stored one.
        """
        self._require_plz_context("analyze_single_grid")
        self.bcid = bcid
        self.kcid = kcid
        osm_trafo = self.has_osm_trafo()

        net = self.dbc.read_net_db(self.plz, self.kcid, self.bcid)
        params = key_figures(net)
        params.update({
            "version_id": self.version_id,
            "plz": self.plz,
            "bcid": self.bcid,
            "kcid": self.kcid,
            "osm_trafo": bool(osm_trafo),
            "key_figures_version": KEY_FIGURES_VERSION,
        })

        self.dbc.insert_clustering_parameters(params)

    def has_osm_trafo(self) -> bool:
        """True if the grid's transformer originates from OSM data (bcid < 0)."""
        self._require_grid_context("has_osm_trafo")
        return self.bcid < 0

    # -------------------------------------------------------------------------
    # PLZ-Level Aggregation Workflows
    # -------------------------------------------------------------------------

    def analyze_basic_parameters_for_plz(self, plz: int):
        """Aggregate basic counts per transformer size across all grids of a PLZ.

        The lists per transformer size follow the grid key (``kcid``, ``bcid``), so they do not
        depend on the physical row order of the database.
        """
        load_count_dict = {}
        bus_count_dict = {}
        trafo_dict = {}

        query = """
            WITH grid_scope AS (
                SELECT grid_result_id, kcid, bcid
                FROM pylovo.grid_result
                WHERE version_id = %(v)s
                  AND plz = %(p)s
            ),
            -- Aggregate only the grids of the PLZ: the joins below read no other grid, and
            -- PostgreSQL does not push a join condition into a GROUP BY subquery.
            load_counts AS (
                SELECT grid_result_id,
                       COUNT(DISTINCT COALESCE(consumer_vertex, pp_index::bigint))::integer AS load_count
                FROM pylovo.pandapower_load
                WHERE grid_result_id IN (SELECT grid_result_id FROM grid_scope)
                GROUP BY grid_result_id
            ),
            bus_counts AS (
                SELECT grid_result_id, COUNT(*)::integer AS bus_count
                FROM pylovo.pandapower_bus
                WHERE grid_result_id IN (SELECT grid_result_id FROM grid_scope)
                GROUP BY grid_result_id
            )
            SELECT
                ROUND(pt.sn_mva * COALESCE(pt.parallel, 1) * 1000.0)::integer AS capacity,  -- station, not unit
                COALESCE(lc.load_count, 0) AS load_count,
                COALESCE(bc.bus_count, 0) AS bus_count
            FROM grid_scope gs
            JOIN pylovo.pandapower_trafo pt
              ON pt.grid_result_id = gs.grid_result_id
            LEFT JOIN load_counts lc
              ON lc.grid_result_id = gs.grid_result_id
            LEFT JOIN bus_counts bc
              ON bc.grid_result_id = gs.grid_result_id
            WHERE pt.sn_mva IS NOT NULL
            ORDER BY capacity, gs.kcid, gs.bcid
        """
        self.dbc.cur.execute(query, {"v": VERSION_ID, "p": plz})
        rows = self.dbc.cur.fetchall()
        count = len(rows)
        processed = 0
        percent = 0

        for capacity, load_count, bus_count in rows:
            if capacity not in trafo_dict:
                trafo_dict[capacity] = 0
                load_count_dict[capacity] = []
                bus_count_dict[capacity] = []

            trafo_dict[capacity] += 1
            load_count_dict[capacity].append(load_count)
            bus_count_dict[capacity].append(bus_count)

            processed += 1
            if count > 0 and processed / count >= 0.1:
                percent += 10
                self.dbc.logger.info(f"{percent} percent finished")
                processed = 0

        self.dbc.insert_plz_parameters(
            plz,
            json.dumps(trafo_dict),
            json.dumps(load_count_dict),
            json.dumps(bus_count_dict)
        )

    def analyze_cable_lengths_for_plz(self, plz: int):
        """Sum cable lengths per standard type across all grids of a PLZ.

        The sum runs in the order of the grid key and line index: a float sum in another order
        differs in the last digit between databases.
        """
        query = """
            SELECT
                pl.std_type,
                COALESCE(SUM(COALESCE(pl.parallel, 1) * COALESCE(pl.length_km, 0.0)
                             ORDER BY gr.kcid, gr.bcid, pl.pp_index), 0.0) AS cable_length
            FROM pylovo.pandapower_line pl
            JOIN pylovo.grid_result gr
              ON gr.grid_result_id = pl.grid_result_id
            WHERE gr.version_id = %(v)s
              AND gr.plz = %(p)s
              AND COALESCE(pl.in_service, TRUE)
              AND pl.std_type IS NOT NULL
            GROUP BY pl.std_type
            ORDER BY pl.std_type
        """
        self.dbc.cur.execute(query, {"v": VERSION_ID, "p": plz})
        cable_length_dict = {std_type: float(length) for std_type, length in self.dbc.cur.fetchall()}

        self.dbc.insert_cable_length(plz, json.dumps(cable_length_dict))

    def analyze_transformer_parameters_for_plz(self, plz: int):
        """Collect per-transformer simultaneous peak loads and distance summaries for a PLZ.

        For each grid, compute:
        - sim_peak_load (kW) using category simultaneity factors
        - average and maximum LV-bus-to-load-bus distances (m) using topology weights
        Group by transformer size (kVA) and store lists per size for later lookup.
        """
        cluster_list = self.dbc.get_list_from_plz(plz)
        total_grids = len(cluster_list)
        processed = 0
        percent = 0

        trafo_load_dict = {}
        trafo_max_distance_dict = {}
        trafo_avg_distance_dict = {}

        for kcid, bcid in cluster_list:
            try:
                net = self.dbc.read_net_db(plz, kcid, bcid, tables=TRANSFORMER_ANALYSIS_TABLES)
            except Exception as e:
                self.dbc.logger.warning(f"Skipping local network {kcid},{bcid} in PLZ {plz}: {e}")
                continue

            if net.trafo.empty:
                self.dbc.logger.warning(f"Grid {kcid},{bcid} has no transformer. Skipping in trafo analysis.")
                continue

            if net.load.empty or "bus" not in net.load.columns:
                continue

            load_bus = net.load["bus"].unique().tolist()
            if not load_bus:
                continue

            lv_bus = net.trafo["lv_bus"].iloc[0]

            try:
                # Distances are derived from the active topology rather than Euclidean
                # bus geometry so the lookup table reflects actual feeder paths.
                graph = top.create_nxgraph(net, respect_switches=True)
                dists = top.calc_distance_to_bus(net, lv_bus, weight="weight", respect_switches=True, g=graph)
                trafo_distance_to_buses_km = dists.loc[load_bus].tolist()
            except Exception:
                continue

            sim_peak_load = net_metrics.simultaneous_peak_load(net)

            if trafo_distance_to_buses_km:
                avg_distance_m = (sum(trafo_distance_to_buses_km) / len(trafo_distance_to_buses_km)) * 1000.0
                max_distance_m = max(trafo_distance_to_buses_km) * 1000.0
            else:
                avg_distance_m = 0.0
                max_distance_m = 0.0

            trafo_size_kva = round(net_metrics.station_mva(net) * 1000.0)

            if trafo_size_kva not in trafo_load_dict:
                trafo_load_dict[trafo_size_kva] = []
                trafo_max_distance_dict[trafo_size_kva] = []
                trafo_avg_distance_dict[trafo_size_kva] = []

            trafo_load_dict[trafo_size_kva].append(sim_peak_load)
            trafo_max_distance_dict[trafo_size_kva].append(max_distance_m)
            trafo_avg_distance_dict[trafo_size_kva].append(avg_distance_m)

            processed += 1
            if total_grids > 0 and processed / total_grids >= 0.1:
                percent += 10
                self.dbc.logger.info(f"{percent} % processed")
                processed = 0

        self.dbc.logger.info("Transformer-parameter aggregation finished.")

        self.dbc.insert_trafo_parameters(
            plz,
            json.dumps(trafo_load_dict),
            json.dumps(trafo_max_distance_dict),
            json.dumps(trafo_avg_distance_dict)
        )


def _grid_parameters_of_plz(plz: int) -> GridParameterCounts:
    """Compute and store the per-grid parameters of one PLZ in a worker process, with its own database connection."""
    calculator = ParameterCalculator()
    try:
        return calculator.calculate_grid_parameters(plz)
    finally:
        calculator.dbc.close()


def analyze_grid_parameters_for_multiple_plz(plz_list: Iterable[int],
                                             parallel: bool | None = None) -> dict[int, GridParameterCounts]:
    """Compute and store the per-grid parameters of every grid of several PLZ, in worker processes if possible.

    Workers are used as for the grid generation: when ``parallel`` is set, there is more than one PLZ and ``N_JOBS``
    (``N_JOBS_PERCENT`` of the CPU cores) is above one. Every worker computes whole PLZ with its own database
    connection. The rows are those of the serial path: one per grid without parameters, computed by
    :meth:`ParameterCalculator.calculate_grid_parameters` and written in the grid order of their PLZ. Only the order
    in which the PLZ finish differs; the table has no column that depends on it. One line per finished PLZ is
    printed and errors go to the log. A PLZ that cannot be processed (for example a lost connection) is logged and
    counted as ``"failed"``, the other PLZ go on.

    Args:
        plz_list: The PLZ (analysed once each, in the given order without workers).
        parallel: Allow worker processes. None takes ``PARALLEL`` of ``config_generation_lv.yaml``.

    Returns:
        The counts of every PLZ, in the order of ``plz_list``.

    Raises:
        KeyboardInterrupt: If the run is interrupted; the PLZ not started yet are cancelled.
    """
    plz_list = list(dict.fromkeys(int(plz) for plz in plz_list))
    parallel = PARALLEL if parallel is None else parallel
    workers = min(N_JOBS, len(plz_list)) if parallel and len(plz_list) > 1 and N_JOBS > 1 else 1
    logger = utils.create_logger("ParameterCalculator", log_file="log/log.txt", log_level=LOG_LEVEL)
    print(f"Per-grid parameters of {len(plz_list)} PLZ in "
          + (f"{workers} worker processes" if workers > 1 else "one process"))
    counts: dict[int, GridParameterCounts] = {}

    def finished(plz: int, result: GridParameterCounts) -> None:
        counts[plz] = result
        line = (f"Per-grid parameters of PLZ {plz} ({len(counts)}/{len(plz_list)}): {result.calculated} calculated, "
                f"{result.skipped} existing")
        if result.failed:
            line += f", {result.failed} failed"
        if result.status != "analysed":
            line += f" ({result.status.replace('_', ' ')})"
        print(line)

    def failure(plz: int) -> GridParameterCounts:
        logger.exception(f"Per-grid parameters of PLZ {plz} could not be computed")
        return GridParameterCounts("failed")

    if workers == 1:
        calculator = ParameterCalculator()
        try:
            for plz in plz_list:
                try:
                    result = calculator.calculate_grid_parameters(plz)
                except Exception:
                    result = failure(plz)
                    calculator.dbc.rollback_changes()
                finished(plz, result)
        finally:
            calculator.dbc.close()
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_grid_parameters_of_plz, plz): plz for plz in plz_list}
            try:
                for future in as_completed(futures):
                    plz = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        result = failure(plz)
                    finished(plz, result)
            except KeyboardInterrupt:
                logger.warning(f"Interrupted after {len(counts)}/{len(plz_list)} PLZ; cancelling the others.")
                for future in futures:
                    future.cancel()
                raise

    totals = [sum(getattr(result, field) for result in counts.values()) for field in ("calculated", "skipped", "failed")]
    print(f"Per-grid parameters of {len(plz_list)} PLZ: {totals[0]} calculated, {totals[1]} existing, "
          f"{totals[2]} failed")
    return {plz: counts[plz] for plz in plz_list}
