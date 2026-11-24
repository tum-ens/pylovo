"""Medium-voltage (MV) grid stage of pylovo.

The MV stage connects the LV stations of a stored pylovo version (one MV node per LV grid) and the MV customers to
HV/MV substations with open rings. It never changes the LV grids; its results and parameters
are stored per MV variant of the LV version (``pylovo.mv_variant``). It reads the LV results from the database,
and :mod:`pylovo.lv_grid` and this package do not import each other.

:func:`pylovo.mv_grid.generator.run_mv_stage` controls a run (lock, variant, worker processes, summary), as
:mod:`pylovo.lv_grid.generator` does for an LV run, and calls these steps:

1. :mod:`~pylovo.mv_grid.preparation`: the region step, the input of a variant (HV/MV substations, MV nodes, their
   attachment to the routing graph, supply areas and districts),
2. per district in a worker process: :mod:`~pylovo.mv_grid.district_input` reads the district and the pure
   :func:`~pylovo.mv_grid.planner.plan_district` plans it,
3. :mod:`~pylovo.mv_grid.post_steps`: ties and links between the rings (:mod:`~pylovo.mv_grid.interconnection`),
   display geometry (:mod:`~pylovo.mv_grid.display`) and the reports of the summary.

The helpers these modules share (database client, loggers, worker count) are in :mod:`~pylovo.mv_grid.runtime`;
:mod:`~pylovo.mv_grid.export` writes a stored net and :mod:`~pylovo.mv_grid.coupling` couples a done variant with
its LV grids.

The planner composes the planning modules: :mod:`~pylovo.mv_grid.graph` (run graph),
:mod:`~pylovo.mv_grid.topology` with :mod:`~pylovo.mv_grid.routing` and :mod:`~pylovo.mv_grid.network` (rings,
stubs, dedicated feeders, open points and geometries), :mod:`~pylovo.mv_grid.rings` and
:mod:`~pylovo.mv_grid.solvers` (ring check and ring solver), :mod:`~pylovo.mv_grid.electrical` (pandapower cases
and criteria, HV/MV sizing, station budgets), :mod:`~pylovo.mv_grid.repair` (the repair loop) and
:mod:`~pylovo.mv_grid.metrics`. The region step uses :mod:`~pylovo.mv_grid.substations`,
:mod:`~pylovo.mv_grid.inputs`, :mod:`~pylovo.mv_grid.districts` and the region graph of :mod:`~pylovo.mv_grid.graph`.

Configuration and shared contracts:

* :mod:`pylovo.mv_grid.options`: the ``MV_*`` YAML keys and the constants of the stage with their coercion rules
  (imported by :mod:`pylovo.config_loader`, so it imports only the standard library and ``yaml``).
* :mod:`pylovo.mv_grid.parameters`: :class:`~pylovo.mv_grid.parameters.MvParameters`, the parameters of one MV
  variant and their snapshot, and :class:`~pylovo.mv_grid.parameters.LvVersionParameters`, the values the stage
  reads from the snapshot of the LV version.
* :mod:`pylovo.mv_grid.model`: data classes and protocols shared by the modules of the stage.

The planning modules of the stage are pure: they import no database, configuration or plotting module and receive
the parameter objects above. This package module imports nothing.
"""
