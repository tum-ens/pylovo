"""The kinds of rows of ``buildings_tem`` and their planning node, as SQL fragments.

``buildings_tem`` holds the buildings of the postcode being generated and the transformer candidates
(:meth:`~pylovo.database.preprocessing_mixin.PreprocessingMixin.insert_transformers`). ``peak_load_in_kw``
encodes the kind of a row:

* ``-1``: a transformer candidate (also ``type = 'Transformer'``),
* ``0``: a building outside the LV grids (its load is MV-direct, or it has no LV load),
* ``> 0``: a load building of the LV grids.

The planning node of a building is the street vertex its load is planned at: the connection point of
its group if buildings were merged (``agg_connection_point``), otherwise its own.

Each fragment exists without and with a table alias (``in_lv_grid("b")`` is ``b.peak_load_in_kw != 0``).
"""
from __future__ import annotations


def _column(alias: str, column: str) -> str:
    return f"{alias}.{column}" if alias else column


def in_lv_grid(alias: str = "") -> str:
    """Rows the LV grids are built from: load buildings and transformer candidates."""
    return f"{_column(alias, 'peak_load_in_kw')} != 0"


def lv_load_building(alias: str = "") -> str:
    """Load buildings of the LV grids (no transformer candidates)."""
    return f"{_column(alias, 'type')} != 'Transformer' AND {_column(alias, 'peak_load_in_kw')} != 0"


def outside_lv_grid(alias: str = "") -> str:
    """Buildings outside the LV grids: MV-direct, or without an LV load."""
    return f"{_column(alias, 'peak_load_in_kw')} = 0"


def planning_node(alias: str = "") -> str:
    """The planning node of a row: ``COALESCE(agg_connection_point, connection_point)``."""
    return f"COALESCE({_column(alias, 'agg_connection_point')}, {_column(alias, 'connection_point')})"


IN_LV_GRID = in_lv_grid()
LV_LOAD_BUILDING = lv_load_building()
OUTSIDE_LV_GRID = outside_lv_grid()
PLANNING_NODE = planning_node()
