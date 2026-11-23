"""Low-voltage (LV) grid generation of pylovo: the LV grids of one postcode area (PLZ).

The MV grids on a stored LV version are the job of :mod:`pylovo.mv_grid`. It reads the stored LV results from the
database, and the two packages do not import each other.

:class:`pylovo.lv_grid.generator.GridGenerator` controls a run (lock, temporary tables, storage, worker
processes) and calls the stages in this order, each on the :class:`~pylovo.lv_grid.context.GenerationContext`
of the postcode:

1. :class:`~pylovo.lv_grid.preparation.InputPreparation`: postcode, buildings with loads, candidate
   transformers and the routable street network,
2. :class:`~pylovo.lv_grid.street_clusters.StreetClustering`: one ``kcid`` per connected street component,
3. :class:`~pylovo.lv_grid.station_planning.StationPlanner`: the supply areas (``bcid``) of existing and
   new stations (:class:`~pylovo.lv_grid.greenfield_areas.GreenfieldAreas`) and the station positions,
4. :class:`~pylovo.lv_grid.network_builder.NetworkBuilder`: the electrical network of every supply area,
   with the voltage repair (:class:`~pylovo.lv_grid.voltage_repair.VoltageRepair`) and the validation and
   storage of each grid (:class:`~pylovo.lv_grid.grid_storage.GridStorage`).

The rules the stages apply are modules of their own: :mod:`~pylovo.lv_grid.postcode_border` (streets of the
neighbouring postcodes), :mod:`~pylovo.lv_grid.supply_areas` (supply areas in one piece),
:mod:`~pylovo.lv_grid.feeder_planning` (feeder topology and cable sizing) and
:mod:`~pylovo.lv_grid.cable_installer` (the components of one grid in the electrical backend).

This package module imports nothing, so a rule module can be imported without the stages.
"""
