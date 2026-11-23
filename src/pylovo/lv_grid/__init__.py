"""The stages of the LV grid generation of one postcode area (PLZ).

:class:`pylovo.grid_generator.GridGenerator` controls a run (lock, temporary tables, storage, worker
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
"""
