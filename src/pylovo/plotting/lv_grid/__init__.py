"""Maps, voltage profiles and statistics of the generated LV grids.

- :mod:`pylovo.plotting.lv_grid.maps`: all grids of a postcode area, one grid by cable type, feeder, sizing basis,
  loading or voltage, the buildings by use, the k-means and building clusters, the connection lines.
- :mod:`pylovo.plotting.lv_grid.electrical`: feeders, cable distances and the voltage profile along the feeders.
- :mod:`pylovo.plotting.lv_grid.statistics`: settlement type, station ratings, cable inventory, the voltage-drop
  check of the design, distributions of key figures per grid, building exclusions.

The functions take the layers of ``DatabaseClient.read_lv_layers`` (or of the example data,
:class:`pylovo.examples.TutorialData`), a pandapower net or the table of ``read_grid_summary``, and return a
matplotlib figure; they never read the database and never call ``plt.show()``. All of them use the shared style of
:mod:`pylovo.plotting.style`. Needs the ``plots`` extra.
"""
from pylovo.plotting.lv_grid.electrical import bus_distances, feeder_ids, plot_feeder_voltage_profile, voltage_band
from pylovo.plotting.lv_grid.maps import (
    core_bounds,
    grid_colors,
    plot_building_uses,
    plot_connections,
    plot_lv_grid,
    plot_lv_overview,
    plot_partition,
)
from pylovo.plotting.lv_grid.statistics import (
    SETTLEMENT_NAMES,
    plot_cable_inventory,
    plot_exclusions,
    plot_grid_distributions,
    plot_rating_counts,
    plot_settlement_type,
    plot_voltage_drop_check,
    settlement_score,
)

__all__ = [
    "SETTLEMENT_NAMES",
    "bus_distances",
    "core_bounds",
    "feeder_ids",
    "grid_colors",
    "plot_building_uses",
    "plot_cable_inventory",
    "plot_connections",
    "plot_exclusions",
    "plot_feeder_voltage_profile",
    "plot_grid_distributions",
    "plot_lv_grid",
    "plot_lv_overview",
    "plot_partition",
    "plot_rating_counts",
    "plot_settlement_type",
    "plot_voltage_drop_check",
    "settlement_score",
    "voltage_band",
]
