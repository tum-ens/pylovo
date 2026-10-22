"""Medium-voltage (MV) grid stage of pylovo.

The MV stage connects the LV stations of a stored pylovo version (one MV node per LV grid) and the MV customers to
HV/MV substations with open rings. It never changes the LV grids; its results and parameters
are stored per MV variant of the LV version (``pylovo.mv_variant``).

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
