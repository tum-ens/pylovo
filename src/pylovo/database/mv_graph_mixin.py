"""Queries of the MV routing graph (``pylovo.mv_graph``, ``mv_graph_vertex``, ``mv_graph_edge``, ``mv_graph_settlement``).

The graph is built server-side from the regional street graph of InfDB, with per-edge settlement
flags and settlement components. It is unversioned reference data, content-addressed by ``graph_id``.
This mixin builds or reuses it, attaches MV terminals to it, reads the subgraph of a supply district and
reports QA figures. Like the other mixins its methods run in the current transaction and do not commit
unless their docstring says so.
"""

from pylovo.database.base_mixin import BaseMixin


class MvGraphMixin(BaseMixin):
    """Build, attach to and read the persistent MV routing graph."""
