API reference
=============

The reference below is generated from the docstrings in ``src/pylovo`` with sphinx-autoapi. It
covers the grid generation, database, import, analysis, plotting and command-line modules; the
modules of the classification and validation workflows are left out.

Entry points for most tasks:

* :class:`pylovo.grid_generator.GridGenerator` -- generate grids for one or many postcodes.
* :class:`pylovo.database.database_client.DatabaseClient` -- database access, e.g.
  :meth:`~pylovo.database.analysis_mixin.AnalysisMixin.read_net_db`.
* :class:`pylovo.database.database_constructor.DatabaseConstructor` -- schema setup.
* :class:`pylovo.cable_installer.CableInstaller` -- construction and sizing of one grid.
* :mod:`pylovo.config_loader` -- all configuration values.
* :mod:`pylovo.electrical_backend` -- backend interface and component specifications.
* :class:`pylovo.analysis.parameter_calculation.ParameterCalculator` -- key figures.

.. toctree::
   :maxdepth: 2

   api/pylovo/index
