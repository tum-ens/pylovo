"""Queries of the LV results that the MV stage builds on.

Reads, for an explicit ``version_id``, the generated PLZ and the planning region, the LV version
snapshot, the stations of the LV grids with their load statistics, the MV customers of
``pylovo.mv_customers``, the LV-side generation targets, the stored LV voltage requirement and the
stored LV pandapower nets. Read-only; the methods never commit.
"""

from pylovo.database.base_mixin import BaseMixin


class MvInputMixin(BaseMixin):
    """Read the LV results of a version as inputs of the MV stage."""
