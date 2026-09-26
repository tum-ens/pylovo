"""Plotting functions of pylovo (needs the ``plots`` extra: ``uv sync --extra plots``).

Subpackages by workflow:

- ``generation``: plots of single generated grids (maps, plotly, tree layouts).
- ``classification``: classification and clustering plots.
- ``validation``: metric, geographic and power-flow plots for grid comparisons.
- ``gis_preparation``: export of grid geometries for QGIS (used by ``pylovo-export``).
- ``utils``: shared axis, colour and legend helpers.

Import the functions from their submodules, e.g.
``from pylovo.plotting.generation.networks import plot_simple_grid``.
"""
