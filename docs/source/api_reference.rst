API reference
=============

The reference below is generated from the docstrings in ``src/pylovo`` with sphinx-autoapi. It
covers the grid generation, MV stage, database, import, analysis, plotting and command-line
modules; the modules of the classification and validation workflows are left out (the
classification is described in :doc:`user_guide/classification`).

Entry points for most tasks:

* :class:`pylovo.grid_generator.GridGenerator` -- generate grids for one or many postcodes.
* :mod:`pylovo.lv_grid` -- the stages of the generation of one postcode, one class each.
* :class:`pylovo.database.database_client.DatabaseClient` -- database access, e.g.
  :meth:`~pylovo.database.analysis_mixin.AnalysisMixin.read_net_db`.
* :class:`pylovo.database.database_constructor.DatabaseConstructor` -- schema setup.
* :class:`pylovo.cable_installer.CableInstaller` -- construction and sizing of one grid.
* :mod:`pylovo.config_loader` -- all configuration values.
* :mod:`pylovo.electrical_backend` -- backend interface and component specifications.
* :class:`pylovo.analysis.parameter_calculation.ParameterCalculator` -- compute and store the key figures of the
  stored grids; :mod:`pylovo.analysis.net_metrics` and :func:`pylovo.analysis.grid_analysis.key_figures` compute
  them from one pandapower net, without the database.
* :func:`pylovo.mv_grid.stage.run_mv_stage` and :class:`pylovo.mv_grid.parameters.MvParameters` -- the MV
  stage and its parameters.
* :mod:`pylovo.load_editing` -- manual load edits of stored grids.

.. toctree::
   :maxdepth: 2

   api/pylovo/index
