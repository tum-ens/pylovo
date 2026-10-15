"""Queries of the MV results (``pylovo.mv_variant``, ``mv_substation``, ``mv_grid``, ``mv_node``, ``mv_line``, ``mv_ring``).

Registers MV variants and their locks, writes the region steps and the districts of a variant, reads
the results back for plots and exports, and lists and deletes variants. Every method takes
``version_id`` (and ``mv_variant_id`` where relevant) explicitly and never uses the configured
``VERSION_ID``. Like the other mixins its methods run in the current transaction and do not commit
unless their docstring says so.
"""

from pylovo.database.base_mixin import BaseMixin


class MvResultMixin(BaseMixin):
    """Write, read, list and delete the MV results of a version."""
