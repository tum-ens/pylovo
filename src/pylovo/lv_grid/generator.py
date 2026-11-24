"""LV grid generation for one or more postcode areas (PLZ).

:class:`GridGenerator` controls a run: it locks the PLZ, creates its temporary tables, runs the
steps of :mod:`pylovo.lv_grid` (:meth:`GridGenerator.generate_grid`), copies the results into the
``*_result`` tables and cleans up; for several PLZ it starts worker processes. The steps are

1. :class:`~pylovo.lv_grid.preparation.InputPreparation`: postcode, buildings with loads, candidate
   transformers and the routable street network,
2. :class:`~pylovo.lv_grid.street_clusters.StreetClustering`: one ``kcid`` per connected street
   component, split with k-means when it has more than ``MAX_BUILDINGS_PER_KCID`` buildings,
3. :class:`~pylovo.lv_grid.station_planning.StationPlanner`: building clusters (``bcid``) around
   existing (brownfield) transformers, the remaining buildings clustered into greenfield transformer
   areas, and the greenfield station positions,
4. :class:`~pylovo.lv_grid.network_builder.NetworkBuilder`: feeders and service cables of every
   grid, a validation power flow, and storage of the network.
"""

import traceback
from enum import StrEnum
from pathlib import Path

import pandas as pd  # type: ignore
from concurrent.futures import ProcessPoolExecutor, as_completed  # lightweight parallel execution

import pylovo.database.database_client as dbc
from pylovo.infdb.infdb_client import InfdbClient
from pylovo.analysis.parameter_calculation import ParameterCalculator
from pylovo import utils
from pylovo.config_loader import (
    CONSUMER_CATEGORIES,
    LOG_LEVEL,
    N_JOBS,
    VERSION_ID,
)

# Import electrical backend components
from pylovo.lv_grid.context import GenerationContext
from pylovo.lv_grid.network_builder import NetworkBuilder
from pylovo.lv_grid.preparation import InputPreparation
from pylovo.lv_grid.station_planning import StationPlanner
from pylovo.lv_grid.street_clusters import StreetClustering

class ResultExistsError(Exception):
    """Raised when the grids of a PLZ already exist for the current ``VERSION_ID``."""


class GenerationOutcome(StrEnum):
    """What :meth:`GridGenerator.generate_grid_for_single_plz` did with a PLZ.

    The log lines that ``pylovo-api`` parses stay unchanged; the outcome makes the same
    information available to callers that must not rely on the log.
    """

    GENERATED = "generated"
    EXISTS = "exists"
    EMPTY = "empty"
    FAILED = "failed"
    #: The grids are stored, but their analysis (``ANALYZE_GRIDS``) failed; ``pylovo-analyze`` repeats it.
    ANALYSIS_FAILED = "analysis_failed"


class GridGenerator:
    """
    Generates the synthetic LV grids of postcode areas (PLZ).

    One instance holds a :class:`~pylovo.database.database_client.DatabaseClient`
    and an ``InfdbClient`` and logs to ``log_file``
    (keyword argument, default ``log/log.txt``). Generate grids with
    :meth:`generate_grid_for_single_plz` or :meth:`generate_grid_for_multiple_plz`.
    """

    def __init__(self, plz=999999, **kwargs):
        self.plz = plz
        self.log_file = kwargs.get("log_file", "log/log.txt")
        self.dbc = dbc.DatabaseClient(log_file=self.log_file)
        self.dbc.insert_version_if_not_exists()
        # The loads of the generation come from the configuration of this process: the table
        # pylovo.consumer_categories is rewritten by every run on the database.
        self.consumer_categories = utils.index_consumer_categories(CONSUMER_CATEGORIES)
        self.logger = utils.create_logger(
            name="GridGenerator", log_file=self.log_file, log_level=LOG_LEVEL
        )
        self.inf_dbc = InfdbClient(log_file=self.log_file)

    def __del__(self):
        # Close the database connection. A destructor must not raise: dbc is missing if __init__
        # failed early, and test doubles may not implement close().
        close = getattr(getattr(self, "dbc", None), "close", None)
        if callable(close):
            close()

    def generate_grid_for_single_plz(
        self, plz: int, analyze_grids: bool = False, refresh_views: bool = True
    ) -> GenerationOutcome:
        """Generates the grid for a single PLZ.

        A failing PLZ is logged, rolled back and skipped (the exception is not raised); the returned
        outcome tells callers what happened. If only the analysis fails after the grids were committed, the
        outcome is ``ANALYSIS_FAILED``: the grids stay stored and count as generated. A PLZ without any grid
        (``EMPTY``) stores its ``postcode_result`` row, its building exclusions and its MV customers, so a rerun
        reports it as ``EMPTY`` again.

        :param plz: Postal code for which the grid should be generated.
        :type plz: int
        :param analyze_grids: Option to analyze the results after grid generation, defaults to False.
        :type analyze_grids: bool
        :param refresh_views: Refresh materialized views after processing, defaults to True.
        :type refresh_views: bool
        :return: The outcome of the PLZ.
        :rtype: GenerationOutcome
        """
        self.plz = plz
        self.dbc.ensure_grid_persistence_schema()
        self.dbc.commit_changes()
        print('-------------------- start', self.plz, '---------------------------')
        self.dbc.acquire_plz_lock(plz)
        interrupted = False
        outcome = GenerationOutcome.GENERATED
        try:
            self.dbc.create_temp_tables(plz)
            self.generate_grid()
            if self.dbc.get_list_from_plz(plz):
                self.dbc.save_tables(plz=self.plz)
            else:
                # No grid, but the PLZ is finished: keep its postcode_result row, why each building is in
                # no grid and its MV customers. A FAILED PLZ is rolled back instead (its steps did not finish).
                outcome = GenerationOutcome.EMPTY
                self.logger.warning(f"No grid_result rows were generated for PLZ {plz}; "
                                    "only its building exclusions and MV customers are stored.")
                self.dbc.save_building_exclusions(plz)
            self.dbc.commit_changes()
            if analyze_grids and outcome == GenerationOutcome.GENERATED:
                # The grids are committed: a failing analysis must not roll them back or report the PLZ as FAILED.
                try:
                    pc = ParameterCalculator()
                    pc.analyze_parameters_for_plz(plz)
                    self.dbc.commit_changes()  # commit the changes to the database
                except Exception as e:
                    outcome = GenerationOutcome.ANALYSIS_FAILED
                    self.logger.error(f"The grids of PLZ {plz} are stored, but their analysis failed: {e}. "
                                      f"'pylovo-analyze --plz {plz}' repeats the analysis.")
                    traceback.print_exc()
        except ResultExistsError:
            if self.dbc.get_list_from_plz(plz):
                outcome = GenerationOutcome.EXISTS
                self.dbc.logger.info(f"Grid for the postcode area {plz} has already been generated.")
            else:
                # Stored before without grids: it stays empty (no grids to analyse or to pass to the MV stage).
                outcome = GenerationOutcome.EMPTY
                self.logger.warning(f"No grid_result rows were generated for PLZ {plz} (stored before without grids).")
        except KeyboardInterrupt:
            interrupted = True
            self.logger.warning(f"Grid generation interrupted by user for PLZ {self.plz}.")
            self.dbc.rollback_changes()
        except Exception as e:
            outcome = GenerationOutcome.FAILED
            self.logger.error(f"Error during grid generation for PLZ {self.plz}: {e}")
            self.logger.info(f"Skipped PLZ {self.plz} due to generation error.")
            self.dbc.rollback_changes()
            traceback.print_exc()
        finally:
            # Always clean up temporary tables, even if there was an error.
            # Roll back first so cleanup can run after SQL errors/interrupts.
            self.dbc.rollback_changes()

            try:
                self.dbc.drop_temp_tables(plz)
                # Commit cleanup so dropped tables don't reappear after interruption.
                self.dbc.commit_changes()
            except Exception as cleanup_error:
                self.logger.error(
                    f"Failed to clean up PLZ-specific temporary tables for PLZ {plz}: {cleanup_error}"
                )
                self.dbc.rollback_changes()
            finally:
                self.dbc.release_plz_lock(plz)
                self.dbc.commit_changes()

        if interrupted:
            raise KeyboardInterrupt("Grid generation interrupted by user")

        if refresh_views:
            # update the materialized views to reflect changes in their base tables
            self.dbc.ensure_connection()
            self.dbc.refresh_materialized_views()
            self.dbc.commit_changes()
        else:
            self.dbc.commit_changes()  # commit the changes to the database
        print('-------------------- end', self.plz, '-----------------------------')
        return outcome

    def generate_grid_for_multiple_plz(
        self, df_plz: pd.DataFrame, analyze_grids: bool = False, parallel: bool = True
    ) -> dict[int, GenerationOutcome]:
        """Generate the grids of several PLZ, in parallel worker processes if possible.

        Workers are used when ``parallel`` is set, there is more than one PLZ and
        ``N_JOBS`` (``N_JOBS_PERCENT`` of the CPU cores) is above one. Each worker
        logs to ``log/log_<plz>.txt``; a failing PLZ is logged and skipped. The
        materialized views are refreshed once at the end.

        Args:
            df_plz: Table with a ``plz`` column.
            analyze_grids: Run the parameter analysis after each PLZ.
            parallel: Allow parallel workers.

        Returns:
            The outcome of every PLZ; a worker that raised counts as ``failed``.

        Raises:
            KeyboardInterrupt: If the run is interrupted; pending PLZ are cancelled.
        """
        self.dbc.ensure_grid_persistence_schema()
        self.dbc.commit_changes()
        plz_list = [int(plz) for plz in df_plz["plz"]]

        # Parallel workers only help with several PLZ and more than one allowed core.
        should_use_parallel = parallel and len(plz_list) > 1 and N_JOBS > 1
        self.logger.info(
            f"Generating {len(plz_list)} PLZ: parallel={should_use_parallel} "
            f"(requested={parallel}, N_JOBS={N_JOBS})"
        )
        outcomes: dict[int, GenerationOutcome] = {}

        if should_use_parallel:
            max_workers = min(N_JOBS, len(plz_list))
            self.logger.info(f"Using {max_workers} worker processes for {len(plz_list)} PLZ")

            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(GridGenerator._worker, plz, analyze_grids): plz
                    for plz in plz_list
                }
                completed_count = 0
                total_count = len(plz_list)

                try:
                    for future in as_completed(futures):
                        plz = futures[future]
                        completed_count += 1
                        try:
                            # Raises the worker's exception, if any.
                            outcome = GenerationOutcome(future.result())
                            outcomes[plz] = outcome
                            incomplete = outcome in (GenerationOutcome.FAILED, GenerationOutcome.ANALYSIS_FAILED)
                            log = self.logger.warning if incomplete else self.logger.info
                            log(f"Completed PLZ {plz} ({completed_count}/{total_count}): {outcome.value}")
                        except Exception as exc:
                            # Record the failure and continue with the other PLZ.
                            self.logger.exception(
                                f"PLZ {plz} generated an exception ({completed_count}/{total_count}): {exc}"
                            )
                            outcomes[plz] = GenerationOutcome.FAILED

                except KeyboardInterrupt:
                    self.logger.warning(
                        f"KeyboardInterrupt received after {completed_count}/{total_count} PLZ; "
                        "cancelling pending PLZ and waiting for running workers."
                    )
                    for future in futures:
                        future.cancel()
                    # Leaving the with block waits for the running workers.
                    raise KeyboardInterrupt("Grid generation interrupted by user") from None

                except Exception as e:
                    self.logger.error(
                        f"Error during parallel processing after {completed_count}/{total_count} PLZ: {e}"
                    )
                    for future in futures:
                        future.cancel()
                    raise
        else:
            for plz in plz_list:
                # defer materialized view refresh until all PLZ are processed
                outcomes[plz] = self.generate_grid_for_single_plz(
                    plz=plz, analyze_grids=analyze_grids, refresh_views=False
                )

        # refresh materialized views once after all grids have been generated
        try:
            self.dbc.ensure_connection()
            self.dbc.refresh_materialized_views()
            self.dbc.commit_changes()
        except Exception as e:
            self.logger.error(f"Error refreshing materialized views: {e}")
            # Don't re-raise here as individual PLZ processing might have succeeded

        # A PLZ that failed inside its worker is returned as FAILED, not raised: count the outcomes.
        failed_plz = sorted(plz for plz, outcome in outcomes.items() if outcome == GenerationOutcome.FAILED)
        run = "Parallel grid generation" if should_use_parallel else "Grid generation"
        if failed_plz:
            failed_plz_str = ", ".join(str(plz) for plz in failed_plz)
            self.logger.warning(f"{run} finished with {len(failed_plz)} failed PLZ: {failed_plz_str}")
        else:
            self.logger.info(f"{run} finished with no failed PLZ.")
        unanalysed_plz = sorted(plz for plz, outcome in outcomes.items() if outcome == GenerationOutcome.ANALYSIS_FAILED)
        if unanalysed_plz:
            unanalysed_plz_str = ", ".join(str(plz) for plz in unanalysed_plz)
            self.logger.warning(f"{run} stored the grids of {len(unanalysed_plz)} PLZ whose analysis failed: "
                                f"{unanalysed_plz_str}. 'pylovo-analyze --plz <plz>' repeats the analysis.")
        counts = {outcome.value: sum(1 for value in outcomes.values() if value == outcome)
                  for outcome in GenerationOutcome}
        self.logger.info("Generation outcomes: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        return outcomes

    @staticmethod
    def _worker(plz: int, analyze_grids: bool) -> GenerationOutcome:
        """Generate the grid of one PLZ in a worker process and return its outcome.

        Each worker builds its own :class:`GridGenerator` (own database connection)
        that logs to ``log/log_<plz>.txt``. The connection is closed afterwards;
        exceptions are passed on to the parent process.
        """
        log_file = Path("log") / f"log_{plz}.txt"
        if log_file.exists():
            log_file.unlink()  # Overwrite log file if it exists

        gg = None
        try:
            gg = GridGenerator(log_file=log_file)
            return gg.generate_grid_for_single_plz(
                plz=plz, analyze_grids=analyze_grids, refresh_views=False
            )
        except Exception:
            if gg is not None:
                gg.logger.exception(f"Worker failed for PLZ {plz}")
                gg.dbc.rollback_changes()
            raise
        finally:
            if gg is not None:
                gg.dbc.close()

    def context(self) -> GenerationContext:
        """The context of the postcode being generated, for the steps of :mod:`pylovo.lv_grid`."""
        return GenerationContext(plz=self.plz, dbc=self.dbc, logger=self.logger,
                                 consumer_categories=self.consumer_categories, inf_dbc=self.inf_dbc)

    def generate_grid(self):
        """Run all generation steps for ``self.plz`` inside the PLZ temporary tables.

        Raises:
            ResultExistsError: If the PLZ already has grids for ``VERSION_ID``.
        """
        if self.dbc.is_grid_generated(self.plz):
            raise ResultExistsError(
                f"The grids for the postcode area {self.plz} is already generated "
                f"for the version {VERSION_ID}."
            )
        context = self.context()
        InputPreparation(context).run()
        StreetClustering(context).run()
        StationPlanner(context).run()
        NetworkBuilder(context).run()
