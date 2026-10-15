"""Maps and statistics plots of the MV grids of the MV stage (:mod:`pylovo.mv_grid`).

- :mod:`pylovo.plotting.mv_grid.maps`: maps of a variant, one grid, one ring, LV busbar voltages, variants side by
  side, interactive maps of stored MV nets, and a loader that reads one grid from the database.
- :mod:`pylovo.plotting.mv_grid.statistics`: metric distributions with literature ranges, variant summaries,
  voltage profiles, line loadings, voltage budgets, MV/LV taps, plausibility traffic lights and solver diagnostics.

The drawing functions take data frames (the layers of ``MvResultMixin.read_mv_layers`` or the tables of
:mod:`pylovo.mv_grid.metrics`) and return figures; only :func:`~pylovo.plotting.mv_grid.maps.plot_mv_grid` reads
the database. Needs the ``plots`` extra; the MV stage itself never imports this package.
"""

from pylovo.plotting.mv_grid.maps import (
    interactive_mv_net,
    plot_lv_busbar_voltage_map,
    plot_mv_grid,
    plot_mv_grid_map,
    plot_mv_overview,
    plot_mv_ring,
    plot_variants_side_by_side,
    ring_paths,
)
from pylovo.plotting.mv_grid.statistics import (
    metric_label,
    plot_loading_histogram,
    plot_metric_distribution,
    plot_plausibility,
    plot_solver_convergence,
    plot_tap_distribution,
    plot_variant_summary,
    plot_voltage_budget,
    plot_voltage_profile,
)

__all__ = [
    "interactive_mv_net",
    "metric_label",
    "plot_loading_histogram",
    "plot_lv_busbar_voltage_map",
    "plot_metric_distribution",
    "plot_mv_grid",
    "plot_mv_grid_map",
    "plot_mv_overview",
    "plot_mv_ring",
    "plot_plausibility",
    "plot_solver_convergence",
    "plot_tap_distribution",
    "plot_variant_summary",
    "plot_variants_side_by_side",
    "plot_voltage_budget",
    "plot_voltage_profile",
    "ring_paths",
]
