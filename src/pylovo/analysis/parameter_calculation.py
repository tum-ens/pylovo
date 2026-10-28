"""Generic parameter-calculation toolbox for LV distribution grids.

This module contains the `ParameterCalculator` class, which groups the shared
algorithms used across clustering, PLZ-level analysis, and comparison-oriented
parameter calculations for synthetic PyLoVo grids.

Core ideas:

- Treat the LV transformer bus as the root of a predominantly radial LV graph.
- Respect the operational topology by building graphs with ``respect_switches=True``.
- Reuse the same topology, distance, simultaneity, and impedance routines across
  multiple higher-level parameter compositions.

The toolbox exposes:

- PLZ-level aggregation workflows
- per-grid analysis workflows
- shared counting, lookup, topology, distance, and impedance routines

Several methods assume radial structure or a unique upstream path. Geographic and
projected coordinates are auto-detected where spatial distance calculations need it.
"""

from collections import deque
from collections.abc import Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import pairwise
import json
import re
from typing import NamedTuple, Tuple, Dict, Any, List, Optional

import networkx as nx
import numpy as np
import pandas as pd
import pandapower as pp
import pandapower.topology as top
from sklearn.metrics.pairwise import haversine_distances

import pylovo.database.database_client as dbc
from pylovo.analysis.grid_analysis import compute_clustering_metrics
from pylovo import utils
from pylovo.config_loader import LOG_LEVEL, N_JOBS, PARALLEL, PEAK_LOAD_HOUSEHOLD, SIM_FACTOR, VERSION_ID


# ---------------------------------------------------------------------------
# Bus type classification configs for feeder counting
# ---------------------------------------------------------------------------
# Each config maps bus and line name patterns to the roles used by the unified feeder counter
# (see classify_bus_types); a pattern that is None matches nothing.
#
# Grids without the pylovo bus names, e.g. exports of real grids, use GENERIC_BUS_TYPE_CONFIG: no name
# patterns, so the load buses are the house connections and every other bus is backbone. To mark service
# buses, cable distribution cabinets and service cables by the names of an export, pass a config with
# its patterns as ``bus_type_config``.

GENERIC_BUS_TYPE_CONFIG: Dict[str, Optional[str]] = {
    "name_column": "name",
    "house_connection_pattern": None,
    "kvs_pattern": None,
    "service_line_pattern": None,
}

PYLOVO_BUS_TYPE_CONFIG: Dict[str, Optional[str]] = {
    "name_column": "name",
    "house_connection_pattern": r"Consumer Nodebus",
    "kvs_pattern": None,
}


# Tables of a stored net that the per-transformer analysis reads: the loads and transformer, and
# every element pandapower.topology.create_nxgraph turns into graph edges.
TRANSFORMER_ANALYSIS_TABLES = frozenset({
    "bus", "bus_dc", "load", "trafo", "trafo3w", "line", "line_dc", "impedance", "tcsc", "dcline", "vsc", "switch",
})


def station_mva(net: pp.pandapowerNet) -> float:
    """Rating in MVA of the (first) transformer station of a net: ``sn_mva x parallel``.

    pandapower's ``sn_mva`` is the rating of one unit; pylovo builds some stations from two
    parallel units (800 kVA as 2 x 400 kVA), so the station size is the product.
    """
    row = net.trafo.iloc[0]
    parallel = row["parallel"] if "parallel" in net.trafo.columns and pd.notna(row["parallel"]) else 1
    return float(row["sn_mva"]) * max(1, int(parallel))


def classify_bus_types(
    net: pp.pandapowerNet,
    config: Dict[str, str],
) -> Dict[int, str]:
    """Build a mapping ``{bus_index: bus_type}`` from naming patterns.

    Recognised bus types:

    - ``"house_connection"``: service/attachment buses that only count when
      topology shows they continue or split the backbone.
    - ``"kvs"``: cable distribution stations treated as transparent splitters.
    - ``"backbone"``: everything else (cable nodes, connection buses, ...).

    Args:
        net: Network whose ``bus`` table is inspected.
        config: Bus-type config with ``name_column`` and optionally ``house_connection_pattern``
            and ``kvs_pattern`` (see :data:`GENERIC_BUS_TYPE_CONFIG`). Patterns are
            case-insensitive regexes matched with ``re.search``; a missing or None pattern
            matches nothing.

    Returns:
        Bus type per bus index; empty if the name column is missing.
    """
    col = config["name_column"]
    if col not in net.bus.columns:
        return {}

    patterns = {
        bus_type: re.compile(config[key], re.IGNORECASE)
        for bus_type, key in (("kvs", "kvs_pattern"), ("house_connection", "house_connection_pattern"))
        if config.get(key)
    }
    return {
        idx: next((bus_type for bus_type, regex in patterns.items() if regex.search(str(name))), "backbone")
        for idx, name in net.bus[col].fillna("").items()
    }


def _classify_bus_types_with_consumers(
    net: pp.pandapowerNet,
    config: Dict[str, str],
    root_idx: int,
    additional_house_connection_buses: Optional[List[int]] = None,
) -> Dict[int, str]:
    """Classify buses by name and mark the given consumer buses (except the root) as house connections."""
    bus_types = classify_bus_types(net, config)
    for bus in additional_house_connection_buses or []:
        bus_idx = int(bus)
        if bus_idx != root_idx:
            bus_types[bus_idx] = "house_connection"
    return bus_types


def _empty_impedance_summary() -> Dict[str, float]:
    """Return the all-zero result of the path impedance aggregation."""
    return {
        "max_branch_weight": 0.0,
        "total_resistance": 0.0,
        "total_reactance": 0.0,
        "max_branch_resistance": 0.0,
        "total_weight": 0.0,
    }


class GridParameterCounts(NamedTuple):
    """What the per-grid analysis of one PLZ did (:meth:`ParameterCalculator.calculate_grid_parameters`)."""

    #: ``"analysed"``, ``"not_analysed"`` (no PLZ-level parameters, nothing done) or ``"failed"`` (the PLZ could not
    #: be processed, see the log)
    status: str
    #: grids whose parameters were calculated and stored
    calculated: int = 0
    #: grids that already had parameters
    skipped: int = 0
    #: grids whose calculation failed (logged)
    failed: int = 0


class ParameterCalculator:
    """Provide workflows and shared calculation routines for LV grid parameters.

    Scope:

    1) PLZ level: aggregate statistics across all local grids inside a PLZ.
    2) Grid level: detailed parameters per local grid for clustering and related analyses.
    3) Shared toolbox: reusable helpers for topology traversal, consumer counting,
       distance calculations, simultaneity aggregation, and impedance proxies.

    Attributes:
        plz (int): Postcode area ID
        bcid (int): Building cluster ID (negative for brownfield grids around an existing transformer)
        kcid (int): K-means cluster ID of the grid
        version_id (str): Analysis version taken from configuration
        dbc (DatabaseClient): Database client used for I/O of pandapower nets and parameters
        lvbus_keyword (str): substring to identify the LV root bus
        consumer_bus_keyword (str): substring to identify consumer buses
        connection_bus_keyword (str): substring to identify internal connection buses
    """

    def __init__(self, keyword_lvbus: str = "LVbus", keyword_consumer_bus: str = "Consumer Nodebus",
                 keyword_connection_bus: str = "Connection Nodebus"):
        self.dbc = dbc.DatabaseClient()
        self.version_id = VERSION_ID
        # Configurable keywords for bus identification across different datasets
        self.lvbus_keyword = keyword_lvbus
        self.consumer_bus_keyword = keyword_consumer_bus
        self.connection_bus_keyword = keyword_connection_bus
        self.plz = None
        self.kcid = None
        self.bcid = None

    def _calculator_context(self) -> str:
        """Return a compact calculator-context string for diagnostics."""
        return f"plz={self.plz}, kcid={self.kcid}, bcid={self.bcid}"

    def _require_plz_context(self, method_name: str) -> int:
        """Require an active PLZ context for stateful workflows and lookups."""
        if self.plz is None:
            raise ValueError(f"{method_name} requires calculator.plz to be set before use.")
        return self.plz

    def _require_grid_context(self, method_name: str) -> Tuple[int, int]:
        """Require an active grid context for grid-scoped stateful methods."""
        if self.kcid is None or self.bcid is None:
            raise ValueError(
                f"{method_name} requires calculator.kcid and calculator.bcid to be set before use."
            )
        return self.kcid, self.bcid

    def _resolve_default_root_bus(self, pandapower_net: pp.pandapowerNet) -> int:
        """Resolve a generic root/source bus from transformer, ext-grid, or bus table."""
        if not pandapower_net.trafo.empty and "lv_bus" in pandapower_net.trafo.columns:
            return pandapower_net.trafo["lv_bus"].iloc[0]
        if not pandapower_net.ext_grid.empty and "bus" in pandapower_net.ext_grid.columns:
            return pandapower_net.ext_grid["bus"].iloc[0]
        if pandapower_net.bus.empty:
            raise ValueError("Cannot resolve a root bus because the bus table is empty.")
        return pandapower_net.bus.index[0]

    def _resolve_synthetic_consumer_buses(self, pandapower_net: pp.pandapowerNet) -> List[int]:
        """Resolve consumer buses in synthetic grids from the configured naming convention."""
        if "name" not in pandapower_net.bus.columns:
            return []
        consumer_mask = pandapower_net.bus["name"].fillna("").str.contains(self.consumer_bus_keyword, na=False)
        return pandapower_net.bus[consumer_mask].index.tolist()

    def analyze_parameters_for_plz(self, plz: int | None = None) -> str:
        """Compute and store PLZ-wide parameters.

        Args:
            plz (int): Postcode area ID

        Returns:
            ``"no_grids"`` if the PLZ has no grids for the ``VERSION_ID`` (nothing is done),
            ``"already_analysed"`` if its parameters exist, else ``"analysed"``.

        Side effects:
            - Reads all nets of the PLZ from the database.
            - Writes aggregated per-PLZ results and sets analysis flags.
            - Skips PLZs already analyzed.
        """
        if plz is None:
            raise ValueError("analyze_parameters_for_plz requires an explicit PLZ value.")

        self.plz = plz
        grid_generated = self.dbc.is_grid_generated(self.plz)
        if not grid_generated:
            self.dbc.logger.info(f"Grid for the postcode area {self.plz} is not generated, yet. Generate it first.")
            return "no_grids"
        grid_analysed = self.dbc.is_grid_analyzed(self.plz)
        if grid_analysed:
            self.dbc.logger.info(f"Grid for the postcode area {self.plz} has already been analyzed.")
            return "already_analysed"

        try:
            self.dbc.logger.info(f"PLZ {self.plz}: start basic result analysis")
            self.analyze_basic_parameters_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: start cable counting")
            self.analyze_cable_lengths_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: start per-trafo analysis")
            self.analyze_transformer_parameters_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: result analysis finished")
            self.dbc.conn.commit()
            return "analysed"
        except Exception as e:
            self.dbc.logger.error(f"Error during analysis for PLZ {self.plz}: {e}")
            self.dbc.logger.info(f"Skipped PLZ {self.plz} due to analysis error.")
            raise

    def analyze_grid_parameters_for_plz(self, plz: int | None = None) -> str:
        """Compute and store per-grid parameters for all grids of an analyzed PLZ.

        Prints the number of grids, one line per calculated grid and a summary.
        :func:`analyze_grid_parameters_for_multiple_plz` analyses several PLZ, in worker processes if possible.

        Args:
            plz (int): Postcode area ID

        Returns:
            ``"not_analysed"`` if the PLZ-level parameters are missing (nothing is done), else ``"analysed"``.

        Note:
            Runs only after the PLZ-level analysis (:meth:`analyze_parameters_for_plz`).
        """
        if plz is None:
            raise ValueError("analyze_grid_parameters_for_plz requires an explicit PLZ value.")
        return self.calculate_grid_parameters(plz, progress=True).status

    def calculate_grid_parameters(self, plz: int, progress: bool = False) -> "GridParameterCounts":
        """Compute and store the per-grid parameters of every grid of ``plz`` that has none yet.

        Grids that already have a row in ``clustering_parameters`` are skipped. A grid whose calculation fails is
        logged, its open transaction is rolled back and the next grid is calculated.

        Args:
            plz: Postcode area; its PLZ-level parameters (:meth:`analyze_parameters_for_plz`) must exist.
            progress: Print the number of grids, one line per calculated grid and a summary.

        Returns:
            The counts. Status ``"not_analysed"`` (nothing is done) if the PLZ-level parameters are missing.
        """
        self.plz = plz
        grid_analysed = self.dbc.is_grid_analyzed(self.plz)
        if not grid_analysed:
            self.dbc.logger.info(
                f"PLZ parameters for the postcode area {self.plz} missing. Please run analyze_parameters_for_plz() first.")
            return GridParameterCounts("not_analysed")

        cluster_list = self.dbc.get_list_from_plz(self.plz)
        if progress:
            print(f"Checking {len(cluster_list)} grids for PLZ {self.plz}...")

        skipped = 0
        calculated = 0
        failed = 0

        for kcid, bcid in cluster_list:
            # Check if this specific grid already has parameters
            try:
                if self.dbc.has_clustering_parameters(self.plz, kcid, bcid):
                    skipped += 1
                    continue

                if progress:
                    print(f"Calculating parameters for grid kcid={kcid}, bcid={bcid}")
                self.analyze_single_grid(bcid, kcid)
                calculated += 1

            except Exception as e:
                failed += 1
                self.dbc.logger.error(
                    f"Failed to calculate/insert parameters for grid {kcid},{bcid} in PLZ {self.plz}: {e}"
                )
                # a failed statement aborts the transaction: without the rollback every later grid would fail too
                self.dbc.rollback_changes()

        if progress:
            print(f"Finished PLZ {self.plz}. Calculated new: {calculated}, Skipped existing: {skipped}.")
        return GridParameterCounts("analysed", calculated, skipped, failed)

    # -------------------------------------------------------------------------
    # Shared Comparison-Parameter Helpers
    # -------------------------------------------------------------------------

    def uses_synthetic_bus_naming(self, net: pp.pandapowerNet) -> bool:
        """Return whether the grid follows the PyLoVo synthetic bus naming convention."""
        return "name" in net.bus.columns and net.bus["name"].fillna("").str.contains(self.lvbus_keyword).any()

    def resolve_root_bus(self, net: pp.pandapowerNet, uses_synthetic_naming: bool) -> int:
        """Resolve the source bus used for feeder and distance calculations."""
        if uses_synthetic_naming:
            return self.resolve_synthetic_root_bus(net)
        return self._resolve_default_root_bus(net)

    def resolve_consumer_buses(self, net: pp.pandapowerNet, uses_synthetic_naming: bool) -> List[int]:
        """Resolve the consumer bus indices used by the structural metric set."""
        if uses_synthetic_naming:
            return self._resolve_synthetic_consumer_buses(net)

        if net.load.empty or "bus" not in net.load.columns:
            return []
        return net.load["bus"].unique().tolist()

    @staticmethod
    def classification_branches(graph: nx.Graph, root_idx: int, consumer_buses: List[int]) -> Dict[int, int]:
        """Assign every consumer bus to its branch, the outgoing circuit of the LV busbar that supplies it.

        A branch is a cable that leaves the busbar ``root_idx``; it is named by the bus at its far end. A building
        connected directly at the busbar is a branch of its own, and a single-building grid has one branch. This is
        the branch of the key figures of the classification (``no_branches`` and the per-branch figures). The
        structural comparison counts feeders with :meth:`count_feeders`, which skips the buildings at the busbar.

        Args:
            graph: Topology graph of a radial net.
            root_idx: The LV busbar.
            consumer_buses: Consumer buses of the net.

        Returns:
            ``{consumer bus: first bus after the busbar on its path}``; a consumer that the busbar does not reach
            has no entry.
        """
        if root_idx not in graph:
            return {}
        paths = nx.single_source_shortest_path(graph, root_idx)
        return {int(bus): int(paths[bus][1]) for bus in consumer_buses if len(paths.get(bus, ())) > 1}

    def count_feeders(
        self,
        net: pp.pandapowerNet,
        graph: nx.Graph,
        root_idx: int,
        uses_synthetic_naming: bool,
        bus_type_config: Optional[Dict[str, str]] = None,
        expand_kvs: bool = True,
        recursive_expansion: bool = False,
        additional_house_connection_buses: Optional[List[int]] = None,
        collapse_service_connection_leaves: bool = False,
    ) -> int:
        """Count feeders using a unified topology-aware algorithm.

        The method walks past degree-1 / degree-2 source stubs to find the
        first real branching node, then classifies each downstream neighbor:

        * **house_connection leaf** (degree 1): skipped – not a feeder.
        * **KVS (cable distribution station)**: expanded – each non-house-
          connection child counts as a separate feeder.  If *all* children
          are house connections the KVS itself counts as **one** feeder.
        * **backbone / anything else**: counted as one feeder.

        Args:
            net: The grid model (used for bus names and the trafo table).
            graph: Switch-aware topology graph.
            root_idx: LV root bus index (transformer LV side or LVbus).
            uses_synthetic_naming: Selects ``PYLOVO_BUS_TYPE_CONFIG`` (True) or
                ``GENERIC_BUS_TYPE_CONFIG`` when ``bus_type_config`` is None; also passed on to the
                recursive counter.
            bus_type_config: Naming-pattern dictionary (see :func:`classify_bus_types`).
            expand_kvs: Expand explicit KVS neighbours at the first branching point into several
                feeders. If False, every first-hop downstream backbone neighbour is one feeder.
            recursive_expansion: Count terminal feeder branches recursively in the downstream
                backbone tree, treating implicit synthetic and explicit KVS split points alike.
            additional_house_connection_buses: Consumer buses that count as terminal house
                connections even if their names do not match the configured pattern.
            collapse_service_connection_leaves: When counting recursively, also collapse terminal
                non-KVS service stubs that only became leaves after the consumer endpoints were
                pruned, so that house-connection attachment nodes are not counted as feeders.

        Returns:
            Number of feeders; at least 1 if the root has downstream buses.
        """
        if bus_type_config is None:
            bus_type_config = (
                PYLOVO_BUS_TYPE_CONFIG if uses_synthetic_naming else GENERIC_BUS_TYPE_CONFIG
            )
        bus_types = _classify_bus_types_with_consumers(
            net, bus_type_config, root_idx, additional_house_connection_buses
        )

        return self._count_feeders_unified(
            net,
            graph,
            root_idx,
            bus_types,
            expand_kvs=expand_kvs,
            recursive_expansion=recursive_expansion,
            uses_synthetic_naming=uses_synthetic_naming,
            collapse_service_connection_leaves=collapse_service_connection_leaves,
            service_line_pattern=bus_type_config.get("service_line_pattern"),
        )

    def _count_feeders_unified(
        self,
        net: pp.pandapowerNet,
        graph: nx.Graph,
        root_idx: int,
        bus_types: Dict[int, str],
        expand_kvs: bool = True,
        recursive_expansion: bool = False,
        uses_synthetic_naming: bool = False,
        collapse_service_connection_leaves: bool = False,
        service_line_pattern: Optional[str] = None,
    ) -> int:
        """Core feeder counting logic shared by all grid representations.

        1. Walk from *root_idx* past degree-1 and degree-2 stubs to the first
           branching node.
        2. Enumerate downstream neighbors (excluding the incoming edge and any
           trafo edges).
        3. Classify each neighbor using *bus_types* and apply the counting rules
           documented in :meth:`count_feeders`.
        """
        graph_for_count = self._build_rooted_tree(graph, root_idx) if recursive_expansion else graph
        if recursive_expansion and collapse_service_connection_leaves:
            graph_for_count = self._remove_service_line_edges(
                graph_for_count,
                net,
                bus_types,
                root_idx,
                service_line_pattern=service_line_pattern if not uses_synthetic_naming else None,
            )
            graph_for_count = self._remove_terminal_service_attachments(
                graph_for_count,
                bus_types,
                root_idx,
            )
            graph_for_count = self._collapse_service_connection_leaves(
                graph_for_count,
                bus_types,
                root_idx,
            )

        if root_idx not in graph_for_count:
            return 0

        # --- walk past source stubs ----------------------------------------
        previous = None
        current = root_idx

        while graph_for_count.degree[current] == 1:
            neighbors = [n for n in graph_for_count.neighbors(current) if n != previous]
            if not neighbors:
                break
            previous, current = current, neighbors[0]

        while previous is not None and graph_for_count.degree[current] == 2:
            next_nodes = [n for n in graph_for_count.neighbors(current) if n != previous]
            if not next_nodes:
                break
            previous, current = current, next_nodes[0]

        branch_point = current

        # --- determine downstream neighbors --------------------------------
        if previous is not None:
            downstream = [n for n in graph_for_count.neighbors(branch_point) if n != previous]
        else:
            # Root itself is the branch point (e.g. synthetic LVbus).
            # Exclude trafo edges explicitly.
            downstream = [
                n for n in graph_for_count.neighbors(branch_point)
                if not self._is_trafo_edge(net, branch_point, n)
            ]

        if not downstream and graph_for_count.number_of_edges() > 0:
            return 1

        if recursive_expansion:
            feeders = sum(
                self._count_terminal_feeders(
                    graph_for_count,
                    neighbor,
                    branch_point,
                    bus_types,
                    uses_synthetic_naming=uses_synthetic_naming,
                    visited={branch_point},
                )
                for neighbor in downstream
                if not self._is_house_connection_leaf(graph_for_count, neighbor, branch_point, bus_types)
            )
            if downstream and feeders == 0:
                feeders = 1
            return feeders

        # --- classify and count --------------------------------------------
        feeders = 0
        for neighbor in downstream:
            btype = bus_types.get(neighbor, "backbone")
            # A node is a leaf if it has no unique neighbor other than the
            # branch point (parallel edges in a MultiGraph inflate degree).
            neighbor_children = [c for c in graph.neighbors(neighbor) if c != branch_point]

            if btype == "house_connection" and not neighbor_children:
                # Leaf house connection directly at the branch point – not a feeder.
                continue

            if btype == "kvs" and expand_kvs:
                # KVS = transparent splitter.  Count non-house-connection
                # children as individual feeders; if *all* children are
                # house connections the KVS itself is one feeder.
                kvs_children = [c for c in graph.neighbors(neighbor) if c != branch_point]
                backbone_children = [
                    c for c in kvs_children
                    if not (
                        bus_types.get(c, "backbone") == "house_connection"
                        and not [x for x in graph.neighbors(c) if x != neighbor]
                    )
                ]
                feeders += max(len(backbone_children), 1)
            else:
                feeders += 1

        # A grid with downstream buses must have at least one feeder even if
        # all neighbors are leaf house connections (degenerate star topology).
        if downstream and feeders == 0:
            feeders = 1

        return feeders

    def _count_terminal_feeders(
        self,
        graph: nx.Graph,
        node: int,
        parent: int,
        bus_types: Dict[int, str],
        uses_synthetic_naming: bool,
        visited: set[int],
    ) -> int:
        """Count terminal backbone branches in the downstream radial tree."""
        if node in visited:
            return 0

        visited = set(visited)
        visited.add(node)

        if self._is_house_connection_leaf(graph, node, parent, bus_types):
            return 0

        backbone_children = [
            child
            for child in graph.neighbors(node)
            if child != parent
            and child not in visited
            and not self._is_house_connection_leaf(graph, child, node, bus_types)
        ]
        if not backbone_children:
            return 1

        if not uses_synthetic_naming and bus_types.get(node, "backbone") != "kvs" and len(backbone_children) > 1:
            return 1

        return sum(
            self._count_terminal_feeders(
                graph,
                child,
                node,
                bus_types,
                uses_synthetic_naming,
                visited,
            )
            for child in backbone_children
        )


    @staticmethod
    def _remove_service_line_edges(
        graph: nx.Graph,
        net: pp.pandapowerNet,
        bus_types: Dict[int, str],
        root_idx: int,
        service_line_pattern: Optional[str] = None,
    ) -> nx.Graph:
        """Remove terminal service-connection line rows from feeder topology.

        Line names that match ``service_line_pattern`` (the service cables of an
        export) are the clearest signal for house/service connection cables. A small
        endpoint guard keeps rare structural-looking edges with the same line-name
        family, e.g. KVS to KVS, in the backbone metric.
        """
        if not service_line_pattern or not hasattr(net, "line") or net.line.empty:
            return graph
        if "name" not in net.line.columns:
            return graph

        service_line_re = re.compile(service_line_pattern, re.IGNORECASE)
        pruned = graph.copy()

        for _, line in net.line.iterrows():
            name = str(line.get("name", ""))
            if not service_line_re.search(name):
                continue
            if "in_service" in line and not bool(line.get("in_service", True)):
                continue

            from_bus = int(line["from_bus"])
            to_bus = int(line["to_bus"])
            if from_bus == root_idx or to_bus == root_idx:
                continue
            if from_bus not in pruned or to_bus not in pruned:
                continue
            if not pruned.has_edge(from_bus, to_bus):
                continue

            if (
                bus_types.get(from_bus, "backbone") == "house_connection"
                or bus_types.get(to_bus, "backbone") == "house_connection"
            ):
                pruned.remove_edge(from_bus, to_bus)

        return pruned


    @staticmethod
    def _remove_terminal_service_attachments(
        graph: nx.Graph,
        bus_types: Dict[int, str],
        root_idx: int,
    ) -> nx.Graph:
        """Remove terminal multi-consumer service attachments from the backbone.

        A terminal service attachment has exactly one non-consumer neighbor and
        one or more terminal consumer/load neighbors. It is pruned together with
        those consumer leaves, because it represents the service side of the
        model rather than a downstream feeder continuation. Explicit KVS nodes
        and nodes with more than one non-consumer neighbor stay visible as
        topology-relevant backbone/split nodes.
        """
        pruned = graph.copy()
        removed: set[int] = set()

        for node in list(graph.nodes):
            if node == root_idx or bus_types.get(node, "backbone") in {"house_connection", "kvs"}:
                continue

            neighbors = list(graph.neighbors(node))
            consumer_leaf_neighbors = [
                neighbor
                for neighbor in neighbors
                if bus_types.get(neighbor, "backbone") == "house_connection"
                and not [other for other in graph.neighbors(neighbor) if other != node]
            ]
            non_consumer_neighbors = [
                neighbor
                for neighbor in neighbors
                if bus_types.get(neighbor, "backbone") != "house_connection"
            ]

            if consumer_leaf_neighbors and len(non_consumer_neighbors) == 1:
                removed.add(node)
                removed.update(consumer_leaf_neighbors)

        pruned.remove_nodes_from(removed)
        return pruned


    @staticmethod
    def _collapse_service_connection_leaves(
        graph: nx.Graph,
        bus_types: Dict[int, str],
        root_idx: int,
    ) -> nx.Graph:
        """Collapse terminal service stubs before counting feeder terminals.

        The recursive feeder metric should describe backbone topology, not the
        number of individual consumer attachment stubs.  After known
        house/consumer leaves are removed, a non-KVS leaf with no original
        topology split is treated as a service-parent artefact and removed as
        well.  KVS leaves stay visible because they are meaningful
        split/end points for this benchmark.
        """
        degree = dict(graph.degree)
        removed: set[int] = set()
        original_degree = dict(graph.degree)
        original_has_house_neighbor = {
            node: any(
                bus_types.get(neighbor, "backbone") == "house_connection"
                for neighbor in graph.neighbors(node)
            )
            for node in graph.nodes
        }
        queue = deque(node for node, deg in degree.items() if node != root_idx and deg <= 1)

        def is_removable_leaf(node: int) -> bool:
            if node == root_idx or degree.get(node, 0) > 1:
                return False
            bus_type = bus_types.get(node, "backbone")
            if bus_type == "house_connection":
                return True
            return (
                bus_type != "kvs"
                and original_has_house_neighbor.get(node, False)
                and original_degree.get(node, 0) <= 2
            )

        while queue:
            node = queue.popleft()
            if node in removed or not is_removable_leaf(node):
                continue
            removed.add(node)
            for neighbor in graph.neighbors(node):
                if neighbor in removed:
                    continue
                degree[neighbor] -= 1
                if degree[neighbor] <= 1:
                    queue.append(neighbor)

        pruned = graph.copy()
        pruned.remove_nodes_from(removed)
        return pruned

    @staticmethod
    def _build_rooted_tree(graph: nx.Graph, root_idx: int) -> nx.Graph:
        """Project a graph to a rooted tree for path-based feeder counting."""
        undirected_graph = nx.Graph(graph)
        return nx.Graph(nx.bfs_tree(undirected_graph, root_idx))

    @staticmethod
    def _is_house_connection_leaf(
        graph: nx.Graph,
        node: int,
        parent: int,
        bus_types: Dict[int, str],
    ) -> bool:
        """Return whether a node is a terminal house-connection leaf."""
        if bus_types.get(node, "backbone") != "house_connection":
            return False
        return not [neighbor for neighbor in graph.neighbors(node) if neighbor != parent]

    @staticmethod
    def _is_trafo_edge(net: pp.pandapowerNet, u: int, v: int) -> bool:
        """Return True if (u, v) corresponds to a transformer winding."""
        if net.trafo.empty:
            return False
        mask = (
            ((net.trafo["hv_bus"] == u) & (net.trafo["lv_bus"] == v))
            | ((net.trafo["hv_bus"] == v) & (net.trafo["lv_bus"] == u))
        )
        return mask.any()

    def calculate_trafo_distances(
        self,
        graph: nx.Graph,
        root_idx: int,
        consumer_buses: List[int],
    ) -> Tuple[float, float]:
        """Calculate average and maximum weighted source-to-consumer path lengths."""
        valid_consumers = [bus_idx for bus_idx in consumer_buses if bus_idx in graph]
        return self._calculate_path_lengths(graph, root_idx, valid_consumers)

    def calculate_feeder_terminal_distances(
        self,
        graph: nx.Graph,
        root_idx: int,
    ) -> Tuple[float, float]:
        """Calculate weighted root distances to terminal feeder/backbone nodes."""
        if root_idx not in graph:
            return 0.0, 0.0
        terminal_nodes = [
            node
            for node, degree in graph.degree
            if node != root_idx and degree <= 1
        ]
        return self._calculate_path_lengths(graph, root_idx, terminal_nodes)

    def analyze_single_grid(self, bcid: int, kcid: int) -> None:
        """Compute clustering parameters for a single local grid and persist them.

        Args:
            bcid (int): Building cluster ID
            kcid (int): Grid cluster ID

        Writes a record to the clustering-parameters table including metadata
        (plz, bcid, kcid, version, osm_trafo flag).
        """
        self._require_plz_context("analyze_single_grid")
        self.bcid = bcid
        self.kcid = kcid
        osm_trafo = self.has_osm_trafo()

        net = self.dbc.read_net_db(self.plz, self.kcid, self.bcid)
        params = compute_clustering_metrics(self, net)
        params.update({
            "version_id": self.version_id,
            "plz": self.plz,
            "bcid": self.bcid,
            "kcid": self.kcid,
            "osm_trafo": bool(osm_trafo)
        })

        self.dbc.insert_clustering_parameters(params)

    # -------------------------------------------------------------------------
    # Shared Lookups And Single-Grid Accessors
    # -------------------------------------------------------------------------

    def calculate_simultaneous_peak_load(self, pandapower_net: pp.pandapowerNet) -> float:
        """Coincident peak load of a grid in kW: simultaneity per consumer category, then summed.

        Per category (Residential, Commercial, Public) ``utils.category_simultaneous_load`` of the
        installed power (``max_p_mw``) and the number of units (``load_units``: households of
        residential loads, one per non-residential load) with the ``SIM_FACTOR`` of the category.
        """
        loads = self._categorised_loads(pandapower_net)
        sim_peak_load = 0.0
        for cat in ("Residential", "Commercial", "Public"):
            load_subset = loads[loads["analysis_category"] == cat]
            category_count = load_subset["load_units"].sum()
            if category_count > 0:
                sim_peak_load += utils.category_simultaneous_load(
                    installed_power=load_subset["max_p_mw"].sum() * 1000.0,
                    load_count=category_count,
                    sim_factor=SIM_FACTOR.get(cat, 1.0)
                )
        return sim_peak_load

    def get_transformer_power(self, pandapower_net: pp.pandapowerNet) -> float:
        """Return the station rating in MVA for a grid with exactly one LV transformer element.

        pandapower's ``sn_mva`` is the rating of one unit; a station of two parallel units
        (``parallel = 2``, e.g. 800 kVA as 2 x 400 kVA) is rated ``sn_mva x parallel``.
        """
        if pandapower_net.trafo.empty or "sn_mva" not in pandapower_net.trafo.columns:
            raise ValueError(f"No transformer found for PLZ {self.plz}, kcid {self.kcid}, bcid {self.bcid}.")
        return station_mva(pandapower_net)

    def has_osm_trafo(self) -> bool:
        """True if the grid's transformer originates from OSM data (bcid < 0)."""
        self._require_grid_context("has_osm_trafo")
        return self.bcid < 0

    def calculate_total_installed_power(self, pandapower_net: pp.pandapowerNet) -> float:
        """Sum of installed maximum active power (MW) over all loads."""
        if pandapower_net.load.empty or "max_p_mw" not in pandapower_net.load.columns:
            return 0.0
        return pandapower_net.load["max_p_mw"].sum()

    def count_households(self, pandapower_net: pp.pandapowerNet) -> int:
        """Number of households: the sum of ``load_units`` of the residential loads.

        ``load_units`` is the number of households of a residential load and 1 for a non-residential
        load, which is no household. A load without ``load_units`` counts as one household.
        """
        loads = self._categorised_loads(pandapower_net)
        return int(round(loads.loc[loads["analysis_category"] == "Residential", "load_units"].sum()))

    @staticmethod
    def _categorised_loads(pandapower_net: pp.pandapowerNet) -> pd.DataFrame:
        """Copy of the load table with the consumer category (``analysis_category``) and numeric ``load_units``.

        The category is the load's ``category``, else the ``zone`` of its bus, else Residential; the
        building types of older nets (MFH, SFH, AB, TH, Mixed) count as Residential. Missing
        ``load_units`` count as 1.
        """
        loads = pandapower_net.load.copy()
        if "category" in loads.columns:
            loads["analysis_category"] = loads["category"]
        elif "zone" in pandapower_net.bus.columns:
            loads = loads.merge(pandapower_net.bus[["zone"]], left_on="bus", right_index=True, how="left")
            loads["analysis_category"] = loads["zone"]
        else:
            loads["analysis_category"] = "Residential"
        loads["analysis_category"] = loads["analysis_category"].fillna("Residential")
        loads["analysis_category"] = loads["analysis_category"].replace(
            ["MFH", "SFH", "AB", "TH", "Mixed"], "Residential"
        )
        if "load_units" not in loads.columns:
            loads["load_units"] = 1.0
        loads["load_units"] = pd.to_numeric(loads["load_units"], errors="coerce").fillna(1.0)
        return loads

    def count_buses_by_keyword(self, pandapower_net: pp.pandapowerNet, bus_description: str) -> int:
        """Count buses whose name contains a given description (substring match)."""
        if "name" not in pandapower_net.bus.columns:
            return 0
        return pandapower_net.bus["name"].fillna("").str.contains(bus_description, na=False).sum()

    def build_service_pruned_graph(
        self,
        pandapower_net: pp.pandapowerNet,
        additional_house_connection_buses: Optional[List[int]] = None,
        bus_type_config: Optional[Dict[str, str]] = None,
    ) -> nx.Graph:
        """Return a topology graph with terminal service connections removed."""
        uses_synthetic_naming = self.uses_synthetic_bus_naming(pandapower_net)
        root_bus = self.resolve_root_bus(pandapower_net, uses_synthetic_naming)
        graph = top.create_nxgraph(
            pandapower_net,
            include_lines=True,
            include_trafos=False,
            respect_switches=True,
        )
        bus_type_config = bus_type_config or (
            PYLOVO_BUS_TYPE_CONFIG if uses_synthetic_naming else GENERIC_BUS_TYPE_CONFIG
        )
        bus_types = _classify_bus_types_with_consumers(
            pandapower_net, bus_type_config, root_bus, additional_house_connection_buses
        )

        graph = self._remove_service_line_edges(
            graph,
            pandapower_net,
            bus_types,
            root_bus,
            service_line_pattern=bus_type_config.get("service_line_pattern"),
        )
        graph = self._remove_terminal_service_attachments(graph, bus_types, root_bus)
        return self._collapse_service_connection_leaves(graph, bus_types, root_bus)

    def _line_indices_after_service_pruning(
        self,
        pandapower_net: pp.pandapowerNet,
        only_in_service: bool = False,
        additional_house_connection_buses: Optional[List[int]] = None,
        bus_type_config: Optional[Dict[str, str]] = None,
    ) -> set[int]:
        """Return line indices after removing terminal consumer service stubs.

        The comparison graph length should describe the LV backbone, not the
        number or length of individual house/consumer service connections.  This
        mirrors the service-pruning assumption used by the terminal feeder-count
        metric.
        """
        line_df = pandapower_net.line
        if line_df.empty:
            return set()

        graph = self.build_service_pruned_graph(
            pandapower_net,
            additional_house_connection_buses=additional_house_connection_buses,
            bus_type_config=bus_type_config,
        )

        kept_indices: set[int] = set()
        for line_idx, line in line_df.iterrows():
            if only_in_service and "in_service" in line and not bool(line.get("in_service", True)):
                continue
            from_bus = int(line["from_bus"])
            to_bus = int(line["to_bus"])
            if graph.has_edge(from_bus, to_bus):
                kept_indices.add(line_idx)
        return kept_indices

    def calculate_cable_length(self, pandapower_net: pp.pandapowerNet, only_in_service: bool = False) -> float:
        """Installed cable length in km: ``length_km x parallel`` summed over the lines.

        A line of two parallel cables counts twice its route length, as in ``plz_parameters.cable_length``.

        Args:
            pandapower_net (pp.pandapowerNet): The grid model.
            only_in_service (bool): Restrict the sum to active lines.
        """
        line_df = pandapower_net.line
        if line_df.empty or "length_km" not in line_df.columns:
            return 0.0
        if only_in_service and "in_service" in line_df.columns:
            line_df = line_df[line_df["in_service"]]
        parallel = (
            pd.to_numeric(line_df["parallel"], errors="coerce").fillna(1.0).replace(0, 1.0)
            if "parallel" in line_df.columns
            else 1.0
        )
        return float((line_df["length_km"] * parallel).sum())

    def calculate_graph_length(
        self,
        pandapower_net: pp.pandapowerNet,
        only_in_service: bool = False,
        with_service_lines: bool = False,
        additional_house_connection_buses: Optional[List[int]] = None,
        bus_type_config: Optional[Dict[str, str]] = None,
    ) -> float:
        """Topology length in km across unique line segments.

        With the default ``with_service_lines=False``, terminal house/consumer
        service-connection stubs are excluded so the metric describes the LV
        feeder backbone. Set ``with_service_lines=True`` to reproduce the total
        routed line-length metric used before this validation assumption.
        """
        line_df = pandapower_net.line
        if line_df.empty or "length_km" not in line_df.columns:
            return 0.0
        if only_in_service and "in_service" in line_df.columns:
            line_df = line_df[line_df["in_service"]]
        if with_service_lines:
            return float(line_df["length_km"].fillna(0.0).sum())
        kept_indices = self._line_indices_after_service_pruning(
            pandapower_net,
            only_in_service=only_in_service,
            additional_house_connection_buses=additional_house_connection_buses,
            bus_type_config=bus_type_config,
        )
        if not kept_indices:
            return 0.0
        line_df = line_df.loc[list(kept_indices)]
        return self._calculate_unique_edge_length(line_df)

    def calculate_graph_resistance(
        self,
        pandapower_net: pp.pandapowerNet,
        only_in_service: bool = False,
        with_service_lines: bool = False,
        additional_house_connection_buses: Optional[List[int]] = None,
        bus_type_config: Optional[Dict[str, str]] = None,
    ) -> float:
        """Return the aggregate line-resistance proxy in ohms.

        The proxy sums the per-segment equivalent resistance
        ``length_km * r_ohm_per_km / parallel`` over active line segments and
        uses the same impedance-ready line-table validation as the path-based
        impedance routines. With the default ``with_service_lines=False``,
        terminal service connections are excluded consistently with the active
        backbone length and distance metrics.
        """
        line_table = self._build_impedance_line_table(pandapower_net)
        if line_table.empty:
            return 0.0
        if only_in_service and "in_service" in line_table.columns:
            line_table = line_table[line_table["in_service"]]
        if not with_service_lines:
            kept_indices = self._line_indices_after_service_pruning(
                pandapower_net,
                only_in_service=only_in_service,
                additional_house_connection_buses=additional_house_connection_buses,
                bus_type_config=bus_type_config,
            )
            line_table = line_table.loc[line_table.index.intersection(kept_indices)]
        if line_table.empty:
            return 0.0
        return self._calculate_unique_edge_equivalent_resistance(line_table)

    @staticmethod
    def _add_undirected_bus_pair(line_table: pd.DataFrame) -> pd.DataFrame:
        """Return a copy with an unordered bus-pair key for parallel line rows."""
        df = line_table.copy()
        from_bus = pd.to_numeric(df["from_bus"], errors="coerce").astype("Int64")
        to_bus = pd.to_numeric(df["to_bus"], errors="coerce").astype("Int64")
        df["_bus_a"] = np.minimum(from_bus, to_bus)
        df["_bus_b"] = np.maximum(from_bus, to_bus)
        return df.dropna(subset=["_bus_a", "_bus_b"])

    def _calculate_unique_edge_length(self, line_table: pd.DataFrame) -> float:
        """Return topology length after collapsing parallel rows by bus pair."""
        if line_table.empty:
            return 0.0

        df = self._add_undirected_bus_pair(line_table)
        if df.empty:
            return 0.0

        df["_length_km"] = pd.to_numeric(df["length_km"], errors="coerce").abs().fillna(0.0)
        edge_lengths = df.groupby(["_bus_a", "_bus_b"], observed=True)["_length_km"].min()
        return float(edge_lengths.sum())

    def _calculate_unique_edge_equivalent_resistance(self, line_table: pd.DataFrame) -> float:
        """Return aggregate equivalent resistance after collapsing parallel rows.

        Synthetic parallel cables are represented by one line row with
        ``parallel > 1``. Real radialized grids may contain several physical line
        rows between the same two buses. For comparison metrics, both cases are
        reduced to one equivalent bus-to-bus edge.
        """
        if line_table.empty:
            return 0.0

        df = self._add_undirected_bus_pair(line_table)
        if df.empty:
            return 0.0

        parallel = (
            pd.to_numeric(df["parallel"], errors="coerce").fillna(1.0).replace(0, 1.0)
            if "parallel" in df.columns
            else 1.0
        )
        length_km = pd.to_numeric(df["length_km"], errors="coerce").abs().fillna(0.0)
        resistance_per_km = pd.to_numeric(df["r_ohm_per_km"], errors="coerce").fillna(0.0)
        row_resistance = length_km * resistance_per_km / parallel
        conductance = pd.Series(0.0, index=df.index)
        positive = row_resistance > 0
        conductance.loc[positive] = 1.0 / row_resistance.loc[positive]
        conductance.loc[(row_resistance == 0) & (length_km == 0)] = np.inf
        df["_conductance"] = conductance

        edge_conductance = df.groupby(["_bus_a", "_bus_b"], observed=True)["_conductance"].sum()
        edge_resistance = pd.Series(0.0, index=edge_conductance.index)
        finite = np.isfinite(edge_conductance) & (edge_conductance > 0)
        edge_resistance.loc[finite] = 1.0 / edge_conductance.loc[finite]
        return float(edge_resistance.sum())

    def calculate_average_house_distance(self, pandapower_net: pp.pandapowerNet) -> float:
        """Calculate a spatial neighborhood distance proxy for consumer locations.

        The returned value (km) is the median over the consumer buses of the mean distance
        to their four nearest neighbouring consumer buses. Consumer buses are the
        ``Consumer Nodebus`` buses of pylovo grids and the load buses of other nets
        (:meth:`resolve_consumer_buses`). The method auto-detects whether the available
        geometry coordinates are geographic or projected.
        """
        consumer_buses = self.resolve_consumer_buses(pandapower_net, self.uses_synthetic_bus_naming(pandapower_net))
        bus = pandapower_net.bus.loc[pandapower_net.bus.index.intersection(consumer_buses)]

        if "geo" not in bus.columns:
            self.dbc.logger.debug("Bus table has no geo column; returning 0.0 for house-distance proxy.")
            return 0.0

        if bus["geo"].isna().all():
            self.dbc.logger.debug("Bus table has no populated geo values; returning 0.0 for house-distance proxy.")
            return 0.0

        geometries = []
        invalid_geometry_count = 0
        for geo_str in bus["geo"].dropna():
            try:
                geo_dict = json.loads(geo_str)
                geometries.append(geo_dict["coordinates"])
            except (json.JSONDecodeError, KeyError, TypeError):
                invalid_geometry_count += 1
                continue

        if len(geometries) < 2:
            if invalid_geometry_count:
                self.dbc.logger.debug(
                    "Skipping house-distance proxy because only %s valid bus geometries remained after parsing %s invalid entries.",
                    len(geometries),
                    invalid_geometry_count,
                )
            return 0.0

        coords = np.array(geometries)

        is_geographic = (coords[:, 0].min() >= -180 and coords[:, 0].max() <= 180 and
                         coords[:, 1].min() >= -90 and coords[:, 1].max() <= 90)

        if is_geographic:
            coords_rad = np.radians(coords[:, ::-1])   # GeoJSON order (lon, lat); haversine_distances needs (lat, lon)
            dis_mat = haversine_distances(coords_rad, coords_rad) * 6371.0
        else:
            from scipy.spatial.distance import cdist
            dis_mat = cdist(coords, coords, metric="euclidean") / 1000.0

        k = min(len(dis_mat) - 1, 4)
        if k == 0:
            return 0.0

        dis_mat.sort(axis=1)
        avg_dists = dis_mat[:, 1:k+1].mean(axis=1)

        return float(np.median(avg_dists))

    def resolve_synthetic_root_bus(self, pandapower_net: pp.pandapowerNet) -> int:
        """Resolve the LV root bus index for synthetic PyLoVo grids."""
        if "name" in pandapower_net.bus.columns:
            root = pandapower_net.bus[
                pandapower_net.bus["name"].fillna("").str.contains(self.lvbus_keyword, na=False)
            ]
            if not root.empty:
                return root.index[0]

        return self._resolve_default_root_bus(pandapower_net)

    def calculate_trafo_distances_for_synthetic_grid(self, pandapower_net: pp.pandapowerNet, networkx_graph: nx.Graph, root_idx: int | None = None, leaves: List[int] | None = None) -> Tuple[float, float]:
        """Calculate average and maximum weighted distances from the root to consumer buses.

        The preferred path is the synthetic consumer-bus naming convention. When
        explicit leaves are not provided and no consumer buses can be resolved, the
        method returns `(0.0, 0.0)` instead of failing.
        """
        if root_idx is None:
            root = self.resolve_synthetic_root_bus(pandapower_net)
        else:
            root = root_idx

        if leaves is None:
            leaves = self._resolve_synthetic_consumer_buses(pandapower_net)

        if len(leaves) == 0:
            return 0.0, 0.0

        return self._calculate_path_lengths(networkx_graph, root, leaves)

    def _calculate_path_lengths(self, graph: nx.Graph, source: int, targets: List[int]) -> Tuple[float, float]:
        """Calculate average and maximum weighted path lengths from one source to multiple targets."""
        if source not in graph:
            self.dbc.logger.debug(
                "Path-length calculation skipped because source bus %s is absent from the topology graph.",
                source,
            )
            return 0.0, 0.0

        path_lengths = []
        skipped_targets = 0
        for target in targets:
            if target not in graph:
                skipped_targets += 1
                continue
            try:
                length = nx.dijkstra_path_length(graph, source, target, weight="weight")
                path_lengths.append(length)
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                skipped_targets += 1
                continue

        if not path_lengths:
            if targets:
                self.dbc.logger.debug(
                    "No valid weighted paths found from source %s to %s targets; %s targets were skipped.",
                    source,
                    len(targets),
                    skipped_targets,
                )
            return 0.0, 0.0

        return sum(path_lengths) / len(path_lengths), max(path_lengths)

    def _resolve_impedance_root_bus(self, pandapower_net: pp.pandapowerNet, networkx_graph: nx.Graph) -> int:
        """Resolve the root bus used by the shared impedance engine."""
        root_bus = self.resolve_root_bus(
            pandapower_net,
            self.uses_synthetic_bus_naming(pandapower_net),
        )
        if root_bus not in networkx_graph:
            raise ValueError(
                f"Resolved root bus {root_bus} is absent from the topology graph for {self._calculator_context()}."
            )
        return root_bus

    def _prepare_legacy_impedance_loads(self, pandapower_net: pp.pandapowerNet) -> pd.DataFrame:
        """Return the legacy VSW load basis without mutating ``net.load``."""
        if pandapower_net.load.empty or "bus" not in pandapower_net.load.columns:
            return pd.DataFrame(columns=["bus", "max_p_mw"])

        load = pandapower_net.load.copy()
        if "max_p_mw" in load.columns:
            return load

        unique_buses = pd.Index(load["bus"].dropna().unique())
        if unique_buses.empty:
            return pd.DataFrame(columns=["bus", "max_p_mw"])

        # Real exports often carry many sub-load rows per connection bus but no
        # max_p_mw column. The legacy VSW metric uses one household equivalent per
        # unique consumer bus to stay comparable with synthetic grids.
        return pd.DataFrame({
            "bus": unique_buses.to_numpy(),
            "max_p_mw": PEAK_LOAD_HOUSEHOLD / 1000.0,
        })

    def _build_impedance_line_table(self, pandapower_net: pp.pandapowerNet) -> pd.DataFrame:
        """Return the line table when the impedance columns needed by the engine exist."""
        df_line = pandapower_net.line.copy()
        if df_line.empty:
            return df_line

        required_columns = {"from_bus", "to_bus", "length_km", "r_ohm_per_km", "x_ohm_per_km"}
        missing_columns = sorted(required_columns - set(df_line.columns))
        if missing_columns:
            self.dbc.logger.warning(
                "Cannot calculate impedance metrics because the line table is missing columns %s.",
                missing_columns,
            )
            return pd.DataFrame(columns=list(required_columns))

        return df_line

    def _build_line_lookup(self, df_line: pd.DataFrame) -> Dict[Tuple[int, int], Dict[str, Any]]:
        """Build a stable edge-to-line lookup for path aggregation."""
        line_lookup = {}
        duplicate_edges = 0
        if df_line.empty:
            return line_lookup

        for _, row in df_line.iterrows():
            key = tuple(sorted((int(row["from_bus"]), int(row["to_bus"]))))
            if key in line_lookup:
                duplicate_edges += 1
                continue
            line_lookup[key] = row.to_dict()

        if duplicate_edges:
            self.dbc.logger.debug(
                "Collapsed %s duplicate line rows onto existing bus-to-bus edges while building the impedance lookup. "
                "This can indicate parallel lines or incomplete line segmentation in %s.",
                duplicate_edges,
                self._calculator_context(),
            )

        return line_lookup

    @staticmethod
    def _resolve_branch_from_path(path: List[int], kvs_buses: set) -> Tuple[int, int] | None:
        """Resolve the feeder branch of one source-to-consumer path: its first edge, or the edge
        behind a cable distribution cabinet (``kvs_buses``) at the first hop."""
        if len(path) < 2:
            return None

        if path[1] in kvs_buses and len(path) >= 3:
            return tuple(sorted((path[1], path[2])))

        return tuple(sorted((path[0], path[1])))

    def _calculate_path_impedance_summary(
        self,
        pandapower_net: pp.pandapowerNet,
        networkx_graph: nx.Graph,
        bus_weights: pd.DataFrame,
        line_table: pd.DataFrame,
        apply_simultaneity: bool,
        bus_type_config: Optional[Dict[str, Optional[str]]] = None,
        branch_of_bus: Optional[Dict[int, int]] = None,
    ) -> Dict[str, float]:
        """Run the source-to-consumer path impedance aggregation of the VSW metric.

        The caller provides the bus weights and decides whether cumulated
        simultaneity should affect line resistance.
        Paths are grouped by the branch of their consumer in ``branch_of_bus``. Without it, by feeder branch: a
        first-hop bus of type ``kvs`` in ``bus_type_config`` (default: by the bus naming) splits its feeders.
        """
        if bus_weights.empty or line_table.empty:
            return _empty_impedance_summary()

        try:
            root_bus = self._resolve_impedance_root_bus(pandapower_net, networkx_graph)
        except ValueError:
            return _empty_impedance_summary()

        bus_type_config = bus_type_config or (
            PYLOVO_BUS_TYPE_CONFIG if self.uses_synthetic_bus_naming(pandapower_net) else GENERIC_BUS_TYPE_CONFIG
        )
        kvs_buses = {bus for bus, bus_type in classify_bus_types(pandapower_net, bus_type_config).items()
                     if bus_type == "kvs"}
        line_lookup = self._build_line_lookup(line_table)
        records = []
        missing_line_edges = 0

        for row in bus_weights.itertuples(index=False):
            consumer_bus = int(row.consumer_bus)
            path_weight = float(row.path_weight)
            if path_weight <= 0:
                continue

            try:
                path = nx.shortest_path(networkx_graph, source=root_bus, target=consumer_bus, weight="weight")
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                continue

            if branch_of_bus is None:
                branch = self._resolve_branch_from_path(path, kvs_buses)
            else:
                branch = branch_of_bus.get(consumer_bus)
            path_resistance = 0.0
            path_reactance = 0.0

            for from_bus, to_bus in pairwise(path):
                line_data = line_lookup.get(tuple(sorted((from_bus, to_bus))))
                if line_data is None:
                    missing_line_edges += 1
                    continue

                length_km = float(line_data.get("length_km", 0.0) or 0.0)
                resistance_per_km = float(line_data.get("r_ohm_per_km", 0.0) or 0.0)
                reactance_per_km = float(line_data.get("x_ohm_per_km", 0.0) or 0.0)
                sim_factor = float(line_data.get("sim_factor_cumulated", 1.0) or 1.0) if apply_simultaneity else 1.0
                # parallel cables of one line divide its impedance (as in calculate_graph_resistance)
                parallel = float(line_data.get("parallel", 1) or 1)
                parallel = parallel if parallel > 0 else 1.0        # NaN or 0: one cable

                path_resistance += length_km * resistance_per_km / parallel * sim_factor
                path_reactance += length_km * reactance_per_km / parallel

            records.append({
                "branch": branch,
                "path_weight": path_weight,
                "weighted_resistance": path_weight * path_resistance,
                "weighted_reactance": path_weight * path_reactance,
            })

        if missing_line_edges:
            self.dbc.logger.debug(
                "Impedance path collection skipped %s path edges because no matching line row was found. "
                "This can indicate missing line segmentation in %s.",
                missing_line_edges,
                self._calculator_context(),
            )

        if not records:
            return _empty_impedance_summary()

        df_records = pd.DataFrame(records)
        branch_weights = df_records.groupby("branch", dropna=False)["path_weight"].sum()
        branch_resistance = df_records.groupby("branch", dropna=False)["weighted_resistance"].sum()
        return {
            "max_branch_weight": float(branch_weights.max()) if not branch_weights.empty else 0.0,
            "total_resistance": float(df_records["weighted_resistance"].sum()),
            "total_reactance": float(df_records["weighted_reactance"].sum()),
            "max_branch_resistance": float(branch_resistance.max()) if not branch_resistance.empty else 0.0,
            "total_weight": float(df_records["path_weight"].sum()),
        }

    def calculate_impedance_metrics(
        self,
        pandapower_net: pp.pandapowerNet,
        networkx_graph: nx.Graph,
        branch_of_bus: Optional[Dict[int, int]] = None,
    ) -> Tuple[float, float, float, float, float]:
        """Calculate the legacy VSW-style impedance metrics used by clustering.

        Returns maximum household equivalents per branch, total resistance, total
        reactance, the resistance-to-reactance ratio, and the maximum branch
        resistance proxy. The resistance and reactance of a line are divided by its
        number of parallel cables. The branches are those of ``branch_of_bus``
        (:meth:`classification_branches`), else the feeder branches of
        :meth:`_calculate_path_impedance_summary`.
        """
        load_table = self._prepare_legacy_impedance_loads(pandapower_net)
        if load_table.empty or "max_p_mw" not in load_table.columns:
            bus_weights = pd.DataFrame(columns=["consumer_bus", "path_weight"])
        else:
            bus_weights = (
                load_table.groupby("bus")["max_p_mw"].sum() * 1000.0 / PEAK_LOAD_HOUSEHOLD
            ).reset_index(name="path_weight").rename(columns={"bus": "consumer_bus"})
        line_table = self._augment_line_table_with_simultaneity(
            pandapower_net,
            networkx_graph,
            load_table=load_table,
        )
        result = self._calculate_path_impedance_summary(
            pandapower_net,
            networkx_graph,
            bus_weights,
            line_table,
            apply_simultaneity=True,
            branch_of_bus=branch_of_bus,
        )
        total_reactance = result["total_reactance"]
        ratio = result["total_resistance"] / total_reactance if total_reactance > 0 else 0.0
        return (
            result["max_branch_weight"],
            result["total_resistance"],
            total_reactance,
            ratio,
            result["max_branch_resistance"],
        )

    def _augment_line_table_with_simultaneity(
        self,
        net: pp.pandapowerNet,
        graph: nx.Graph,
        load_table: pd.DataFrame | None = None,
        root_bus: int | None = None,
    ) -> pd.DataFrame:
        """Augment the line table with per-line simultaneity and aggregated category metadata.

        The routine assumes a predominantly radial synthetic LV grid. It seeds line-
        level category counts at house-connection buses and then walks upstream toward
        the root to accumulate counts, loads, and cumulated simultaneity factors.
        Missing columns are handled conservatively so higher-level metric computation
        can continue on imperfect inputs.
        """
        df_line = self._build_impedance_line_table(net)
        if df_line.empty:
            return df_line

        df_line["sim_load"] = 0.0
        df_line["sim_factor_cumulated"] = 1.0

        effective_load_table = net.load.copy() if load_table is None else load_table.copy()
        if effective_load_table.empty:
            return df_line

        if "bus" not in effective_load_table.columns or "max_p_mw" not in effective_load_table.columns:
            self.dbc.logger.warning(
                "Cannot augment line simultaneity because the load table is missing required columns."
            )
            return df_line

        sim_defs = pd.DataFrame.from_dict(SIM_FACTOR, orient="index", columns=["sim_factor"])
        sim_defs.index.name = "description"
        sim_defs.reset_index(inplace=True)

        loads = effective_load_table.copy()

        if "category" in loads.columns:
            loads["zone"] = loads["category"]
        elif "zone" in net.bus.columns:
            loads = loads.merge(net.bus[["zone"]], left_on="bus", right_index=True, how="left")
        else:
            loads["zone"] = "Residential"
            self.dbc.logger.debug("Bus table has no zone column; defaulting load categories to Residential.")

        loads["zone"] = loads["zone"].fillna("Residential")
        loads["zone"] = loads["zone"].replace(["MFH", "SFH", "AB", "TH"], "Residential")
        # Map any zone not recognised by SIM_FACTOR (e.g. the zone codes of a DSO export) to
        # "Residential" so the category-based upstream propagation produces valid non-zero
        # sim_factor_cumulated values for real grid exports.
        _known_zones = set(SIM_FACTOR.keys())
        loads.loc[~loads["zone"].isin(_known_zones), "zone"] = "Residential"

        if "load_units" not in loads.columns:
            loads["load_units"] = 1.0
        loads["load_units"] = pd.to_numeric(loads["load_units"], errors="coerce").fillna(1.0)

        bus_zone_stats = loads.groupby(["bus", "zone"]).agg(
            count=("load_units", "sum"),
            max_p_mw=("max_p_mw", "sum"),
        ).reset_index()

        if bus_zone_stats.empty:
            return df_line

        bus_zone_stats = bus_zone_stats.merge(sim_defs, left_on="zone", right_on="description", how="left")
        bus_zone_stats["sim_factor"] = bus_zone_stats["sim_factor"].fillna(1.0)

        bus_zone_stats["sim_load"] = bus_zone_stats.apply(
            lambda row: utils.category_simultaneous_load(1, row["count"], row["sim_factor"]) * row["max_p_mw"],
            axis=1,
        )

        bus_zone_stats["sim_factor_level1"] = bus_zone_stats.apply(
            lambda row: utils.category_simultaneous_load(1, row["count"], row["sim_factor"]),
            axis=1,
        )

        line_stats = {}

        for _, row in bus_zone_stats.iterrows():
            bus_idx = row["bus"]
            incident_lines = df_line[(df_line["from_bus"] == bus_idx) | (df_line["to_bus"] == bus_idx)]
            if incident_lines.empty:
                continue

            # House-connection buses are expected to attach to a single upstream line.
            line_idx = incident_lines.index[0]

            if line_idx not in line_stats:
                line_stats[line_idx] = {"sim_load": 0.0, "peak_load": 0.0}

            line_stats[line_idx]["sim_load"] += row["sim_load"]
            line_stats[line_idx]["peak_load"] += row["max_p_mw"]

        for idx, stats in line_stats.items():
            df_line.at[idx, "sim_load"] = stats["sim_load"]
            peak_load = stats["peak_load"]
            if peak_load > 0:
                df_line.at[idx, "sim_factor_cumulated"] = stats["sim_load"] / peak_load
            else:
                df_line.at[idx, "sim_factor_cumulated"] = 0.0

        try:
            resolved_root_bus = self.resolve_synthetic_root_bus(net) if root_bus is None else root_bus
        except ValueError:
            self.dbc.logger.debug("Could not resolve a synthetic root bus; returning seeded line simultaneity only.")
            return df_line

        if resolved_root_bus not in graph:
            self.dbc.logger.debug(
                "Synthetic root bus %s is not present in the topology graph; returning seeded line simultaneity only.",
                resolved_root_bus,
            )
            return df_line

        graph_distances = nx.shortest_path_length(graph, source=resolved_root_bus)
        # Process buses from the leaves upward so child-line totals are available
        # before their parent line is recomputed.
        buses_by_reverse_distance = sorted(
            graph_distances.keys(),
            key=lambda bus_idx: graph_distances[bus_idx],
            reverse=True,
        )

        # Category-specific totals are kept separately because simultaneity factors
        # differ between Residential, Commercial, and Public loads.
        category_names = ["Commercial", "Public", "Residential"]
        for category_name in category_names:
            df_line[f"no_{category_name}"] = 0
            df_line[f"load_{category_name}_mw"] = 0.0

        for _, row in bus_zone_stats.iterrows():
            bus_idx = row["bus"]
            category_name = row["zone"]
            if category_name not in category_names:
                continue

            incident_lines = df_line[(df_line["from_bus"] == bus_idx) | (df_line["to_bus"] == bus_idx)]
            if incident_lines.empty:
                continue

            line_idx = incident_lines.index[0]
            df_line.at[line_idx, f"no_{category_name}"] += row["count"]
            df_line.at[line_idx, f"load_{category_name}_mw"] += row["max_p_mw"]

        for bus_idx in buses_by_reverse_distance:
            if bus_idx == resolved_root_bus:
                continue

            parent_bus = None
            child_buses = []
            for neighbor in graph.neighbors(bus_idx):
                if graph_distances[neighbor] < graph_distances[bus_idx]:
                    parent_bus = neighbor
                else:
                    child_buses.append(neighbor)

            if parent_bus is None:
                continue

            upstream_line_mask = ((df_line["from_bus"] == parent_bus) & (df_line["to_bus"] == bus_idx)) | \
                                 ((df_line["from_bus"] == bus_idx) & (df_line["to_bus"] == parent_bus))
            if upstream_line_mask.sum() == 0:
                continue

            upstream_line_idx = upstream_line_mask.idxmax()

            for child_bus in child_buses:
                downstream_line_mask = ((df_line["from_bus"] == bus_idx) & (df_line["to_bus"] == child_bus)) | \
                                       ((df_line["from_bus"] == child_bus) & (df_line["to_bus"] == bus_idx))
                if downstream_line_mask.sum() == 0:
                    continue

                downstream_line_idx = downstream_line_mask.idxmax()
                for category_name in category_names:
                    df_line.at[upstream_line_idx, f"no_{category_name}"] += df_line.at[
                        downstream_line_idx, f"no_{category_name}"
                    ]
                    df_line.at[upstream_line_idx, f"load_{category_name}_mw"] += df_line.at[
                        downstream_line_idx, f"load_{category_name}_mw"
                    ]

            total_sim_load = 0.0
            total_peak_load = 0.0
            for category_name in category_names:
                category_count = df_line.at[upstream_line_idx, f"no_{category_name}"]
                category_peak_load = df_line.at[upstream_line_idx, f"load_{category_name}_mw"]
                sim_factor = SIM_FACTOR.get(category_name, 1.0)

                if category_count > 0 and category_peak_load > 0:
                    total_sim_load += utils.category_simultaneous_load(category_peak_load, category_count, sim_factor)
                total_peak_load += category_peak_load

            df_line.at[upstream_line_idx, "sim_load"] = total_sim_load
            if total_peak_load > 0:
                df_line.at[upstream_line_idx, "sim_factor_cumulated"] = total_sim_load / total_peak_load
            else:
                df_line.at[upstream_line_idx, "sim_factor_cumulated"] = 0.0

        return df_line

    # -------------------------------------------------------------------------
    # PLZ-Level Aggregation Workflows
    # -------------------------------------------------------------------------

    def analyze_basic_parameters_for_plz(self, plz: int):
        """Aggregate basic counts per transformer size across all grids of a PLZ.

        The lists per transformer size follow the grid key (``kcid``, ``bcid``), so they do not
        depend on the physical row order of the database.
        """
        load_count_dict = {}
        bus_count_dict = {}
        trafo_dict = {}

        query = """
            WITH grid_scope AS (
                SELECT grid_result_id, kcid, bcid
                FROM pylovo.grid_result
                WHERE version_id = %(v)s
                  AND plz = %(p)s
            ),
            -- Aggregate only the grids of the PLZ: the joins below read no other grid, and
            -- PostgreSQL does not push a join condition into a GROUP BY subquery.
            load_counts AS (
                SELECT grid_result_id,
                       COUNT(DISTINCT COALESCE(consumer_vertex, pp_index::bigint))::integer AS load_count
                FROM pylovo.pandapower_load
                WHERE grid_result_id IN (SELECT grid_result_id FROM grid_scope)
                GROUP BY grid_result_id
            ),
            bus_counts AS (
                SELECT grid_result_id, COUNT(*)::integer AS bus_count
                FROM pylovo.pandapower_bus
                WHERE grid_result_id IN (SELECT grid_result_id FROM grid_scope)
                GROUP BY grid_result_id
            )
            SELECT
                ROUND(pt.sn_mva * COALESCE(pt.parallel, 1) * 1000.0)::integer AS capacity,  -- station, not unit
                COALESCE(lc.load_count, 0) AS load_count,
                COALESCE(bc.bus_count, 0) AS bus_count
            FROM grid_scope gs
            JOIN pylovo.pandapower_trafo pt
              ON pt.grid_result_id = gs.grid_result_id
            LEFT JOIN load_counts lc
              ON lc.grid_result_id = gs.grid_result_id
            LEFT JOIN bus_counts bc
              ON bc.grid_result_id = gs.grid_result_id
            WHERE pt.sn_mva IS NOT NULL
            ORDER BY capacity, gs.kcid, gs.bcid
        """
        self.dbc.cur.execute(query, {"v": VERSION_ID, "p": plz})
        rows = self.dbc.cur.fetchall()
        count = len(rows)
        processed = 0
        percent = 0

        for capacity, load_count, bus_count in rows:
            if capacity not in trafo_dict:
                trafo_dict[capacity] = 0
                load_count_dict[capacity] = []
                bus_count_dict[capacity] = []

            trafo_dict[capacity] += 1
            load_count_dict[capacity].append(load_count)
            bus_count_dict[capacity].append(bus_count)

            processed += 1
            if count > 0 and processed / count >= 0.1:
                percent += 10
                self.dbc.logger.info(f"{percent} percent finished")
                processed = 0

        self.dbc.insert_plz_parameters(
            plz,
            json.dumps(trafo_dict),
            json.dumps(load_count_dict),
            json.dumps(bus_count_dict)
        )

    def analyze_cable_lengths_for_plz(self, plz: int):
        """Sum cable lengths per standard type across all grids of a PLZ.

        The sum runs in the order of the grid key and line index: a float sum in another order
        differs in the last digit between databases.
        """
        query = """
            SELECT
                pl.std_type,
                COALESCE(SUM(COALESCE(pl.parallel, 1) * COALESCE(pl.length_km, 0.0)
                             ORDER BY gr.kcid, gr.bcid, pl.pp_index), 0.0) AS cable_length
            FROM pylovo.pandapower_line pl
            JOIN pylovo.grid_result gr
              ON gr.grid_result_id = pl.grid_result_id
            WHERE gr.version_id = %(v)s
              AND gr.plz = %(p)s
              AND COALESCE(pl.in_service, TRUE)
              AND pl.std_type IS NOT NULL
            GROUP BY pl.std_type
            ORDER BY pl.std_type
        """
        self.dbc.cur.execute(query, {"v": VERSION_ID, "p": plz})
        cable_length_dict = {std_type: float(length) for std_type, length in self.dbc.cur.fetchall()}

        self.dbc.insert_cable_length(plz, json.dumps(cable_length_dict))

    def analyze_transformer_parameters_for_plz(self, plz: int):
        """Collect per-transformer simultaneous peak loads and distance summaries for a PLZ.

        For each grid, compute:
        - sim_peak_load (kW) using category simultaneity factors
        - average and maximum LV-bus-to-load-bus distances (m) using topology weights
        Group by transformer size (kVA) and store lists per size for later lookup.
        """
        cluster_list = self.dbc.get_list_from_plz(plz)
        total_grids = len(cluster_list)
        processed = 0
        percent = 0

        trafo_load_dict = {}
        trafo_max_distance_dict = {}
        trafo_avg_distance_dict = {}

        for kcid, bcid in cluster_list:
            try:
                net = self.dbc.read_net_db(plz, kcid, bcid, tables=TRANSFORMER_ANALYSIS_TABLES)
            except Exception as e:
                self.dbc.logger.warning(f"Skipping local network {kcid},{bcid} in PLZ {plz}: {e}")
                continue

            if net.trafo.empty:
                self.dbc.logger.warning(f"Grid {kcid},{bcid} has no transformer. Skipping in trafo analysis.")
                continue

            if net.load.empty or "bus" not in net.load.columns:
                continue

            load_bus = net.load["bus"].unique().tolist()
            if not load_bus:
                continue

            lv_bus = net.trafo["lv_bus"].iloc[0]

            try:
                # Distances are derived from the active topology rather than Euclidean
                # bus geometry so the lookup table reflects actual feeder paths.
                graph = top.create_nxgraph(net, respect_switches=True)
                dists = top.calc_distance_to_bus(net, lv_bus, weight="weight", respect_switches=True, g=graph)
                trafo_distance_to_buses_km = dists.loc[load_bus].tolist()
            except Exception:
                continue

            sim_peak_load = self.calculate_simultaneous_peak_load(net)

            if trafo_distance_to_buses_km:
                avg_distance_m = (sum(trafo_distance_to_buses_km) / len(trafo_distance_to_buses_km)) * 1000.0
                max_distance_m = max(trafo_distance_to_buses_km) * 1000.0
            else:
                avg_distance_m = 0.0
                max_distance_m = 0.0

            trafo_size_kva = round(station_mva(net) * 1000.0)

            if trafo_size_kva not in trafo_load_dict:
                trafo_load_dict[trafo_size_kva] = []
                trafo_max_distance_dict[trafo_size_kva] = []
                trafo_avg_distance_dict[trafo_size_kva] = []

            trafo_load_dict[trafo_size_kva].append(sim_peak_load)
            trafo_max_distance_dict[trafo_size_kva].append(max_distance_m)
            trafo_avg_distance_dict[trafo_size_kva].append(avg_distance_m)

            processed += 1
            if total_grids > 0 and processed / total_grids >= 0.1:
                percent += 10
                self.dbc.logger.info(f"{percent} % processed")
                processed = 0

        self.dbc.logger.info("Transformer-parameter aggregation finished.")

        self.dbc.insert_trafo_parameters(
            plz,
            json.dumps(trafo_load_dict),
            json.dumps(trafo_max_distance_dict),
            json.dumps(trafo_avg_distance_dict)
        )


def _grid_parameters_of_plz(plz: int) -> GridParameterCounts:
    """Compute and store the per-grid parameters of one PLZ in a worker process, with its own database connection."""
    calculator = ParameterCalculator()
    try:
        return calculator.calculate_grid_parameters(plz)
    finally:
        calculator.dbc.close()


def analyze_grid_parameters_for_multiple_plz(plz_list: Iterable[int],
                                             parallel: bool | None = None) -> dict[int, GridParameterCounts]:
    """Compute and store the per-grid parameters of every grid of several PLZ, in worker processes if possible.

    Workers are used as for the grid generation: when ``parallel`` is set, there is more than one PLZ and ``N_JOBS``
    (``N_JOBS_PERCENT`` of the CPU cores) is above one. Every worker computes whole PLZ with its own database
    connection. The rows are those of the serial path: one per grid without parameters, computed by
    :meth:`ParameterCalculator.calculate_grid_parameters` and written in the grid order of their PLZ. Only the order
    in which the PLZ finish differs; the table has no column that depends on it. One line per finished PLZ is
    printed and errors go to the log. A PLZ that cannot be processed (for example a lost connection) is logged and
    counted as ``"failed"``, the other PLZ go on.

    Args:
        plz_list: The PLZ (analysed once each, in the given order without workers).
        parallel: Allow worker processes. None takes ``PARALLEL`` of ``config_generation_lv.yaml``.

    Returns:
        The counts of every PLZ, in the order of ``plz_list``.

    Raises:
        KeyboardInterrupt: If the run is interrupted; the PLZ not started yet are cancelled.
    """
    plz_list = list(dict.fromkeys(int(plz) for plz in plz_list))
    parallel = PARALLEL if parallel is None else parallel
    workers = min(N_JOBS, len(plz_list)) if parallel and len(plz_list) > 1 and N_JOBS > 1 else 1
    logger = utils.create_logger("ParameterCalculator", log_file="log/log.txt", log_level=LOG_LEVEL)
    print(f"Per-grid parameters of {len(plz_list)} PLZ in "
          + (f"{workers} worker processes" if workers > 1 else "one process"))
    counts: dict[int, GridParameterCounts] = {}

    def finished(plz: int, result: GridParameterCounts) -> None:
        counts[plz] = result
        line = (f"Per-grid parameters of PLZ {plz} ({len(counts)}/{len(plz_list)}): {result.calculated} calculated, "
                f"{result.skipped} existing")
        if result.failed:
            line += f", {result.failed} failed"
        if result.status != "analysed":
            line += f" ({result.status.replace('_', ' ')})"
        print(line)

    def failure(plz: int) -> GridParameterCounts:
        logger.exception(f"Per-grid parameters of PLZ {plz} could not be computed")
        return GridParameterCounts("failed")

    if workers == 1:
        calculator = ParameterCalculator()
        try:
            for plz in plz_list:
                try:
                    result = calculator.calculate_grid_parameters(plz)
                except Exception:
                    result = failure(plz)
                    calculator.dbc.rollback_changes()
                finished(plz, result)
        finally:
            calculator.dbc.close()
    else:
        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_grid_parameters_of_plz, plz): plz for plz in plz_list}
            try:
                for future in as_completed(futures):
                    plz = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        result = failure(plz)
                    finished(plz, result)
            except KeyboardInterrupt:
                logger.warning(f"Interrupted after {len(counts)}/{len(plz_list)} PLZ; cancelling the others.")
                for future in futures:
                    future.cancel()
                raise

    totals = [sum(getattr(result, field) for result in counts.values()) for field in ("calculated", "skipped", "failed")]
    print(f"Per-grid parameters of {len(plz_list)} PLZ: {totals[0]} calculated, {totals[1]} existing, "
          f"{totals[2]} failed")
    return {plz: counts[plz] for plz in plz_list}
