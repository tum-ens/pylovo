"""Generic parameter-calculation toolbox for LV distribution grids.

This module contains the `ParameterCalculator` class, which groups the shared
algorithms used across clustering, PLZ-level analysis, and comparison-oriented
parameter calculations for synthetic PyLoVo grids.

Core ideas
- Treat the LV transformer bus as the root of a predominantly radial LV graph.
- Respect the operational topology by building graphs with `respect_switches=True`.
- Reuse the same topology, distance, simultaneity, and impedance routines across
    multiple higher-level parameter compositions.

The toolbox exposes:
- PLZ-level aggregation workflows
- per-grid analysis workflows
- shared counting, lookup, topology, distance, and impedance routines

Several methods assume radial structure or a unique upstream path. Geographic and
projected coordinates are auto-detected where spatial distance calculations need it.
"""

import json
import re
from typing import Tuple, Dict, Any, List, Optional

import networkx as nx
import numpy as np
import pandas as pd
import pandapower as pp
import pandapower.topology as top
from sklearn.metrics.pairwise import haversine_distances

import pylovo.database.database_client as dbc
from pylovo.analysis.grid_analysis import compute_clustering_metrics
from pylovo import utils
from pylovo.config_loader import *


# ---------------------------------------------------------------------------
# Bus type classification configs for feeder counting
# ---------------------------------------------------------------------------
# Each config maps bus name patterns to semantic roles used by the unified
# feeder counter.  Adapt ``name_column`` and the regex patterns when working
# with a different DSO data export.

SWF_BUS_TYPE_CONFIG: Dict[str, str] = {
    "name_column": "name",
    "house_connection_pattern": r"NS_HaAn",
    "kvs_pattern": r"NS_KVS",
}

PYLOVO_BUS_TYPE_CONFIG: Dict[str, str] = {
    "name_column": "name",
    "house_connection_pattern": r"Consumer Nodebus",
    "kvs_pattern": r"NS_KVS",
}


def classify_bus_types(
    net: pp.pandapowerNet,
    config: Dict[str, str],
) -> Dict[int, str]:
    """Build a mapping ``{bus_index: bus_type}`` from naming patterns.

    Recognised bus types:
    - ``"house_connection"`` – leaf consumer buses (e.g. NS_HaAn, Consumer Nodebus)
    - ``"kvs"`` – cable distribution stations treated as transparent splitters
    - ``"backbone"`` – everything else (cable nodes, connection buses, …)

    Parameters
    ----------
    net : pp.pandapowerNet
        Pandapower network whose ``bus`` table is inspected.
    config : dict
        Must contain ``name_column``, ``house_connection_pattern``, and
        ``kvs_pattern`` keys.  Patterns are compiled as case-insensitive
        regexes and matched with ``re.search``.
    """
    col = config["name_column"]
    if col not in net.bus.columns:
        return {}

    hc_re = re.compile(config["house_connection_pattern"], re.IGNORECASE)
    kvs_re = re.compile(config["kvs_pattern"], re.IGNORECASE)

    bus_types: Dict[int, str] = {}
    for idx, name in net.bus[col].fillna("").items():
        name_str = str(name)
        if kvs_re.search(name_str):
            bus_types[idx] = "kvs"
        elif hc_re.search(name_str):
            bus_types[idx] = "house_connection"
        else:
            bus_types[idx] = "backbone"
    return bus_types


class ParameterCalculator:
    """Provide workflows and shared calculation routines for LV grid parameters.

    Scope
    1) PLZ level: aggregate statistics across all local grids inside a PLZ.
    2) Grid level: detailed parameters per local grid for clustering and related analyses.
    3) Shared toolbox: reusable helpers for topology traversal, consumer counting,
       distance calculations, simultaneity aggregation, and impedance proxies.

    Attributes:
        plz (int): Postcode area ID
        bcid (int): Building cluster ID (negative bcid implies an OSM-only transformer)
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

    def analyze_parameters_for_plz(self, plz: int = None):
        """Compute and store PLZ-wide parameters.

        Args:
            plz (int): Postcode area ID

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
            return
        grid_analysed = self.dbc.is_grid_analyzed(self.plz)
        if grid_analysed:
            self.dbc.logger.info(f"Grid for the postcode area {self.plz} has already been analyzed.")
            return

        try:
            self.dbc.logger.info(f"PLZ {self.plz}: start basic result analysis")
            self.analyze_basic_parameters_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: start cable counting")
            self.analyze_cable_lengths_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: start per-trafo analysis")
            self.analyze_transformer_parameters_for_plz(self.plz)
            self.dbc.logger.info(f"PLZ {self.plz}: result analysis finished")
            self.dbc.conn.commit()
        except Exception as e:
            self.dbc.logger.error(f"Error during analysis for PLZ {self.plz}: {e}")
            self.dbc.logger.info(f"Skipped PLZ {self.plz} due to analysis error.")
            self.dbc.delete_plz_from_sample_set_table(str(CLASSIFICATION_VERSION), self.plz)
            raise e

    def analyze_grid_parameters_for_plz(self, plz: int = None):
        """Compute and store per-grid parameters for all grids of an analyzed PLZ.

        Args:
            plz (int): Postcode area ID

        Note:
            Ensures PLZ-level metrics exist first (used for per-PLZ lookups).
        """
        if plz is None:
            raise ValueError("analyze_grid_parameters_for_plz requires an explicit PLZ value.")

        self.plz = plz
        grid_analysed = self.dbc.is_grid_analyzed(self.plz)
        if not grid_analysed:
            self.dbc.logger.info(
                f"PLZ parameters for the postcode area {self.plz} missing. Please run analyze_parameters_for_plz() first.")
            return

        cluster_list = self.dbc.get_list_from_plz(self.plz)
        total_grids = len(cluster_list)
        print(f"Checking {total_grids} grids for PLZ {self.plz}...")

        skipped = 0
        calculated = 0

        for kcid, bcid in cluster_list:
            # Check if this specific grid already has parameters
            try:
                if self.dbc.has_clustering_parameters(self.plz, kcid, bcid):
                    skipped += 1
                    continue

                print(f"Calculating parameters for grid {bcid}, {kcid}")
                self.analyze_single_grid(bcid, kcid)
                calculated += 1

            except Exception as e:
                self.dbc.logger.error(
                    f"Failed to calculate/insert parameters for grid {kcid},{bcid} in PLZ {self.plz}: {e}"
                )

        print(f"Finished PLZ {self.plz}. Calculated new: {calculated}, Skipped existing: {skipped}.")

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

    def count_consumers(self, consumer_buses: List[int]) -> int:
        """Count unique consumer connection points from the resolved consumer buses."""
        return len(consumer_buses)

    def count_feeders(
        self,
        net: pp.pandapowerNet,
        graph: nx.Graph,
        root_idx: int,
        uses_synthetic_naming: bool,
        bus_type_config: Optional[Dict[str, str]] = None,
        expand_kvs: bool = True,
        recursive_expansion: bool = False,
    ) -> int:
        """Count feeders using a unified topology-aware algorithm.

        The method walks past degree-1 / degree-2 source stubs to find the
        first real branching node, then classifies each downstream neighbor:

        * **house_connection leaf** (degree 1): skipped – not a feeder.
        * **KVS (cable distribution station)**: expanded – each non-house-
          connection child counts as a separate feeder.  If *all* children
          are house connections the KVS itself counts as **one** feeder.
        * **backbone / anything else**: counted as one feeder.

        Parameters
        ----------
        net : pp.pandapowerNet
            The grid model (used for bus names and trafo table).
        graph : nx.Graph
            Switch-aware topology graph.
        root_idx : int
            LV root bus index (transformer LV side or LVbus).
        uses_synthetic_naming : bool
            Legacy flag – only used when *bus_type_config* is ``None`` so
            that ``PYLOVO_BUS_TYPE_CONFIG`` is chosen automatically.
        bus_type_config : dict, optional
            Naming-pattern dictionary (see :data:`SWF_BUS_TYPE_CONFIG`).
            When ``None``, the config is inferred from *uses_synthetic_naming*.
        expand_kvs : bool, default True
            When ``True``, explicit KVS neighbors at the first branching point
            are expanded into multiple feeders. When ``False``, every first-hop
            downstream backbone neighbor counts as one feeder, regardless of
            whether it is an explicit KVS bus.
        recursive_expansion : bool, default False
            When ``True``, count terminal feeder branches recursively in the
            downstream backbone tree. This treats implicit synthetic split
            points and explicit real KVS split points symmetrically.
        """
        if bus_type_config is None:
            bus_type_config = (
                PYLOVO_BUS_TYPE_CONFIG if uses_synthetic_naming else SWF_BUS_TYPE_CONFIG
            )
        bus_types = classify_bus_types(net, bus_type_config)

        return self._count_feeders_unified(
            net,
            graph,
            root_idx,
            bus_types,
            expand_kvs=expand_kvs,
            recursive_expansion=recursive_expansion,
            uses_synthetic_naming=uses_synthetic_naming,
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

        if root_idx not in graph_for_count:
            return 0

        # --- walk past source stubs ----------------------------------------
        previous = None
        current = root_idx

        while graph_for_count.degree[current] == 1:
            neighbors = list(graph_for_count.neighbors(current))
            if not neighbors:
                return 0
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

    def get_parameters_as_dataframe(self, net: pp.pandapowerNet) -> pd.DataFrame:
        """Return clustering parameters as a one-row DataFrame."""
        params = compute_clustering_metrics(self, net)
        return pd.DataFrame([params], columns=CLUSTERING_PARAMETERS)

    def lookup_simultaneous_peak_load(self, transformer_mva: float, max_trafo_dis: float) -> float:
        """Lookup coincident peak load for a transformer size and max path distance.

        Args:
            transformer_mva (float): Transformer rating in MVA.
            max_trafo_dis (float): Maximum distance from transformer to any load in km.

        Returns:
            float: Simultaneous peak load in MW.
        """
        if self.plz is None:
            self.dbc.logger.warning(
                "Skipping simultaneous peak-load lookup because calculator.plz is unset. "
                f"Returning 0.0 for {self._calculator_context()}."
            )
            return 0.0

        data_list, _, _ = self.dbc.read_per_trafo_dict(self.plz)
        transformer_type_str = str(int(transformer_mva * 1000))
        max_trafo_distance_list = data_list[3].get(transformer_type_str, [])

        if not max_trafo_distance_list:
            self.dbc.logger.debug(
                "No transformer-distance lookup bucket found for transformer %s kVA in PLZ %s.",
                transformer_type_str,
                self.plz,
            )
            return 0.0

        target_dist = max_trafo_dis * 1000
        if target_dist in max_trafo_distance_list:
            sim_load_index = max_trafo_distance_list.index(target_dist)
            simultaneous_peak_load_mw = data_list[2][transformer_type_str][sim_load_index] / 1000
            return simultaneous_peak_load_mw

        self.dbc.logger.debug(
            "No simultaneous peak-load match for transformer %s kVA at %.2f m in PLZ %s.",
            transformer_type_str,
            target_dist,
            self.plz,
        )
        return 0.0

    def get_transformer_power(self, pandapower_net: pp.pandapowerNet) -> float:
        """Return the transformer rating in MVA for a grid with exactly one LV transformer."""
        if pandapower_net.trafo.empty or "sn_mva" not in pandapower_net.trafo.columns:
            raise ValueError(f"No transformer found for PLZ {self.plz}, kcid {self.kcid}, bcid {self.bcid}.")
        return pandapower_net.trafo["sn_mva"].iloc[0]

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
        """Count load elements, used here as a proxy for the number of households."""
        return len(pandapower_net.load)

    def count_buses_by_keyword(self, pandapower_net: pp.pandapowerNet, bus_description: str) -> int:
        """Count buses whose name contains a given description (substring match)."""
        if "name" not in pandapower_net.bus.columns:
            return 0
        return pandapower_net.bus["name"].fillna("").str.contains(bus_description, na=False).sum()

    def calculate_cable_length(self, pandapower_net: pp.pandapowerNet, only_in_service: bool = False) -> float:
        """Total circuit length in km across line elements.

        Args:
            pandapower_net (pp.pandapowerNet): The grid model.
            only_in_service (bool): Restrict the sum to active lines.
        """
        line_df = pandapower_net.line
        if line_df.empty or "length_km" not in line_df.columns:
            return 0.0
        if only_in_service and "in_service" in line_df.columns:
            line_df = line_df[line_df["in_service"]]
        return line_df["length_km"].sum()

    def calculate_graph_length(self, pandapower_net: pp.pandapowerNet, only_in_service: bool = False) -> float:
        """Total topology length in km across unique line segments.

        This metric intentionally ignores the ``parallel`` circuit multiplier and
        therefore reflects the routed graph length rather than installed circuit
        length. It is the appropriate length basis for real-vs-synthetic grid
        comparisons.
        """
        line_df = pandapower_net.line
        if line_df.empty or "length_km" not in line_df.columns:
            return 0.0
        if only_in_service and "in_service" in line_df.columns:
            line_df = line_df[line_df["in_service"]]
        return float(line_df["length_km"].fillna(0.0).sum())

    def calculate_graph_resistance(self, pandapower_net: pp.pandapowerNet, only_in_service: bool = False) -> float:
        """Return the aggregate line-resistance proxy in ohms.

        The proxy sums the per-segment equivalent resistance
        ``length_km * r_ohm_per_km / parallel`` over active line segments and
        uses the same impedance-ready line-table validation as the path-based
        impedance routines. Unlike :meth:`calculate_graph_length`, this metric
        treats parallel conductors electrically, so grids that use many smaller
        parallel cables do not collapse onto the same resistance proxy as a
        single-conductor layout with the same routed length.
        """
        line_table = self._build_impedance_line_table(pandapower_net)
        if line_table.empty:
            return 0.0
        if only_in_service and "in_service" in line_table.columns:
            line_table = line_table[line_table["in_service"]]
        if line_table.empty:
            return 0.0
        parallel = (
            line_table["parallel"].fillna(1.0).replace(0, 1.0)
            if "parallel" in line_table.columns
            else 1.0
        )
        resistance = (
            line_table["length_km"].fillna(0.0)
            * line_table["r_ohm_per_km"].fillna(0.0)
            / parallel
        )
        return float(resistance.sum())

    def calculate_average_house_distance(self, pandapower_net: pp.pandapowerNet) -> float:
        """Calculate a spatial neighborhood distance proxy for consumer locations.

        The returned value is the median of the mean distance to the four nearest
        neighboring points. The method auto-detects whether the available geometry
        coordinates are geographic or projected.
        """
        bus = pandapower_net.bus

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
            coords_rad = np.radians(coords)
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

    def calculate_trafo_distances_for_synthetic_grid(self, pandapower_net: pp.pandapowerNet, networkx_graph: nx.Graph, root_idx: int = None, leaves: List[int] = None) -> Tuple[float, float]:
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

    def _resolve_branch_from_path(self, pandapower_net: pp.pandapowerNet, path: List[int]) -> Tuple[int, int] | None:
        """Resolve the feeder branch label associated with one source-to-consumer path."""
        if len(path) < 2:
            return None

        first_hop_name = ""
        if "name" in pandapower_net.bus.columns:
            first_hop_name = str(pandapower_net.bus.at[path[1], "name"])

        if "NS_KVS" in first_hop_name and len(path) >= 3:
            return tuple(sorted((path[1], path[2])))

        return tuple(sorted((path[0], path[1])))

    def _calculate_path_impedance_summary(
        self,
        pandapower_net: pp.pandapowerNet,
        networkx_graph: nx.Graph,
        bus_weights: pd.DataFrame,
        line_table: pd.DataFrame,
        apply_simultaneity: bool,
    ) -> Dict[str, float]:
        """Run the shared source-to-consumer path impedance aggregation.

        This helper is the common base for both the historical VSW metric and
        the household-count path proxy. The caller provides the bus weights and
        decides whether cumulated simultaneity should affect line resistance.
        """
        if bus_weights.empty or line_table.empty:
            return {
                "max_branch_weight": 0.0,
                "total_resistance": 0.0,
                "total_reactance": 0.0,
                "max_branch_resistance": 0.0,
                "total_weight": 0.0,
            }

        try:
            root_bus = self._resolve_impedance_root_bus(pandapower_net, networkx_graph)
        except ValueError:
            return {
                "max_branch_weight": 0.0,
                "total_resistance": 0.0,
                "total_reactance": 0.0,
                "max_branch_resistance": 0.0,
                "total_weight": 0.0,
            }

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

            branch = self._resolve_branch_from_path(pandapower_net, path)
            path_resistance = 0.0
            path_reactance = 0.0

            for from_bus, to_bus in zip(path, path[1:]):
                line_data = line_lookup.get(tuple(sorted((from_bus, to_bus))))
                if line_data is None:
                    missing_line_edges += 1
                    continue

                length_km = float(line_data.get("length_km", 0.0) or 0.0)
                resistance_per_km = float(line_data.get("r_ohm_per_km", 0.0) or 0.0)
                reactance_per_km = float(line_data.get("x_ohm_per_km", 0.0) or 0.0)
                sim_factor = float(line_data.get("sim_factor_cumulated", 1.0) or 1.0) if apply_simultaneity else 1.0

                path_resistance += length_km * resistance_per_km * sim_factor
                path_reactance += length_km * reactance_per_km

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
            return {
                "max_branch_weight": 0.0,
                "total_resistance": 0.0,
                "total_reactance": 0.0,
                "max_branch_resistance": 0.0,
                "total_weight": 0.0,
            }

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

    def calculate_impedance_metrics(self, pandapower_net: pp.pandapowerNet, networkx_graph: nx.Graph) -> Tuple[float, float, float, float, float]:
        """Calculate the legacy VSW-style impedance metrics used by clustering.

        Returns maximum household equivalents per branch, total resistance, total
        reactance, the resistance-to-reactance ratio, and the maximum branch
        resistance proxy.
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

    def calculate_household_path_impedance_proxy(
        self,
        pandapower_net: pp.pandapowerNet,
        networkx_graph: nx.Graph,
        load_table: pd.DataFrame | None = None,
    ) -> Dict[str, float]:
        """Calculate the HH-only weighted mean path impedance proxy.

        The caller may pass a pre-filtered load table to define which rows count
        as households. This keeps dataset-specific HH filtering outside of the
        shared ParameterCalculator path logic.
        """
        effective_load_table = pandapower_net.load if load_table is None else load_table
        if effective_load_table.empty or "bus" not in effective_load_table.columns:
            bus_weights = pd.DataFrame(columns=["consumer_bus", "path_weight"])
        else:
            # For the household path proxy, the path weight is simply the number
            # of household load rows attached to each consumer bus.
            bus_weights = effective_load_table.groupby("bus").size().reset_index(name="path_weight")
            bus_weights["path_weight"] = bus_weights["path_weight"].astype(float)
            bus_weights = bus_weights.rename(columns={"bus": "consumer_bus"})
        line_table = self._build_impedance_line_table(pandapower_net)
        result = self._calculate_path_impedance_summary(
            pandapower_net,
            networkx_graph,
            bus_weights,
            line_table,
            apply_simultaneity=False,
        )
        total_reactance = result["total_reactance"]
        total_weight = result["total_weight"]
        resistance = result["total_resistance"] / total_weight if total_weight > 0 else 0.0
        reactance = total_reactance / total_weight if total_weight > 0 else 0.0
        ratio = result["total_resistance"] / total_reactance if total_reactance > 0 else 0.0
        return {
            "household_count": float(total_weight),
            "max_households_of_a_branch": float(result["max_branch_weight"]),
            "resistance": float(resistance),
            "reactance": float(reactance),
            "ratio": float(ratio),
            "max_branch_resistance": float(result["max_branch_resistance"]),
        }

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

        if "zone" in net.bus.columns:
            loads = loads.merge(net.bus[["zone"]], left_on="bus", right_index=True, how="left")
        else:
            loads["zone"] = "Residential"
            self.dbc.logger.debug("Bus table has no zone column; defaulting load categories to Residential.")

        loads["zone"] = loads["zone"].fillna("Residential")
        loads["zone"] = loads["zone"].replace(["MFH", "SFH", "AB", "TH"], "Residential")
        # Map any zone not recognised by SIM_FACTOR (e.g. DSO zone codes like "SWF") to
        # "Residential" so the category-based upstream propagation produces valid non-zero
        # sim_factor_cumulated values for real grid exports.
        _known_zones = set(SIM_FACTOR.keys())
        loads.loc[~loads["zone"].isin(_known_zones), "zone"] = "Residential"

        load_name_column = "name" if "name" in loads.columns else "bus"

        bus_zone_stats = loads.groupby(["bus", "zone"]).agg(
            count=(load_name_column, "count"),
            max_p_mw=("max_p_mw", "sum")
        ).reset_index()

        if bus_zone_stats.empty:
            return df_line

        bus_zone_stats = bus_zone_stats.merge(sim_defs, left_on="zone", right_on="description", how="left")
        bus_zone_stats["sim_factor"] = bus_zone_stats["sim_factor"].fillna(1.0)

        bus_zone_stats["sim_load"] = bus_zone_stats.apply(
            lambda row: utils.oneSimultaneousLoad(1, row["count"], row["sim_factor"]) * row["max_p_mw"],
            axis=1,
        )

        bus_zone_stats["sim_factor_level1"] = bus_zone_stats.apply(
            lambda row: utils.oneSimultaneousLoad(1, row["count"], row["sim_factor"]),
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
                    total_sim_load += utils.oneSimultaneousLoad(category_peak_load, category_count, sim_factor)
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
        """Aggregate basic counts per transformer size across all grids of a PLZ."""
        load_count_dict = {}
        bus_count_dict = {}
        cable_length_dict = {}
        trafo_dict = {}

        query = """
            WITH grid_scope AS (
                SELECT grid_result_id
                FROM pylovo.grid_result
                WHERE version_id = %(v)s
                  AND plz = %(p)s
            ),
            load_counts AS (
                SELECT grid_result_id, COUNT(*)::integer AS load_count
                FROM pylovo.pandapower_load
                GROUP BY grid_result_id
            ),
            bus_counts AS (
                SELECT grid_result_id, COUNT(*)::integer AS bus_count
                FROM pylovo.pandapower_bus
                GROUP BY grid_result_id
            ),
            line_lengths AS (
                SELECT grid_result_id, COALESCE(SUM(length_km), 0.0) AS cable_length
                FROM pylovo.pandapower_line
                GROUP BY grid_result_id
            )
            SELECT
                ROUND(pt.sn_mva * 1000.0)::integer AS capacity,
                COALESCE(lc.load_count, 0) AS load_count,
                COALESCE(bc.bus_count, 0) AS bus_count,
                COALESCE(ll.cable_length, 0.0) AS cable_length
            FROM grid_scope gs
            JOIN pylovo.pandapower_trafo pt
              ON pt.grid_result_id = gs.grid_result_id
            LEFT JOIN load_counts lc
              ON lc.grid_result_id = gs.grid_result_id
            LEFT JOIN bus_counts bc
              ON bc.grid_result_id = gs.grid_result_id
            LEFT JOIN line_lengths ll
              ON ll.grid_result_id = gs.grid_result_id
            WHERE pt.sn_mva IS NOT NULL
            ORDER BY capacity
        """
        self.dbc.cur.execute(query, {"v": VERSION_ID, "p": plz})
        rows = self.dbc.cur.fetchall()
        count = len(rows)
        time = 0
        percent = 0

        for capacity, load_count, bus_count, cable_length in rows:
            if capacity not in trafo_dict:
                trafo_dict[capacity] = 0
                load_count_dict[capacity] = []
                bus_count_dict[capacity] = []
                cable_length_dict[capacity] = []

            trafo_dict[capacity] += 1
            load_count_dict[capacity].append(load_count)
            bus_count_dict[capacity].append(bus_count)
            cable_length_dict[capacity].append(cable_length)

            time += 1
            if count > 0 and time / count >= 0.1:
                percent += 10
                self.dbc.logger.info(f"{percent} percent finished")
                time = 0

        self.dbc.insert_plz_parameters(
            plz,
            json.dumps(trafo_dict),
            json.dumps(load_count_dict),
            json.dumps(bus_count_dict)
        )

    def analyze_cable_lengths_for_plz(self, plz: int):
        """Sum cable lengths per standard type across all grids of a PLZ."""
        query = """
            SELECT
                pl.std_type,
                COALESCE(SUM(COALESCE(pl.parallel, 1) * COALESCE(pl.length_km, 0.0)), 0.0) AS cable_length
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
        time = 0
        percent = 0

        trafo_load_dict = {}
        trafo_max_distance_dict = {}
        trafo_avg_distance_dict = {}

        for kcid, bcid in cluster_list:
            try:
                net = self.dbc.read_net_db(plz, kcid, bcid)
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
                g = top.create_nxgraph(net, respect_switches=True)
                dists = top.calc_distance_to_bus(net, lv_bus, weight="weight", respect_switches=True)
                trafo_distance_to_buses_km = dists.loc[load_bus].tolist()
            except Exception:
                continue

            # If zone labels are missing, treat all loads as Residential so the PLZ-
            # level lookup remains usable for imperfect historic datasets.
            def get_cat_stats(mask):
                relevant_buses = net.bus[mask].index
                load_subset = net.load[net.load["bus"].isin(relevant_buses)]
                count = len(load_subset)
                sum_load_kw = load_subset["max_p_mw"].sum() * 1000.0
                return count, sum_load_kw

            if "zone" in net.bus.columns:
                res_mask = ~net.bus["zone"].isin(["Commercial", "Public"])
                com_mask = net.bus["zone"] == "Commercial"
                pub_mask = net.bus["zone"] == "Public"
            else:
                res_mask = pd.Series(True, index=net.bus.index)
                com_mask = pd.Series(False, index=net.bus.index)
                pub_mask = pd.Series(False, index=net.bus.index)

            stats = {
                "Residential": get_cat_stats(res_mask),
                "Commercial": get_cat_stats(com_mask),
                "Public": get_cat_stats(pub_mask)
            }

            sim_peak_load = 0.0
            for cat, (category_count, sum_load) in stats.items():
                if category_count > 0:
                    sim_peak_load += utils.oneSimultaneousLoad(
                        installed_power=sum_load,
                        load_count=category_count,
                        sim_factor=SIM_FACTOR.get(cat, 1.0)
                    )

            if trafo_distance_to_buses_km:
                avg_distance_m = (sum(trafo_distance_to_buses_km) / len(trafo_distance_to_buses_km)) * 1000.0
                max_distance_m = max(trafo_distance_to_buses_km) * 1000.0
            else:
                avg_distance_m = 0.0
                max_distance_m = 0.0

            trafo_size_kva = round(net.trafo["sn_mva"].iloc[0] * 1000.0)

            if trafo_size_kva not in trafo_load_dict:
                trafo_load_dict[trafo_size_kva] = []
                trafo_max_distance_dict[trafo_size_kva] = []
                trafo_avg_distance_dict[trafo_size_kva] = []

            trafo_load_dict[trafo_size_kva].append(sim_peak_load)
            trafo_max_distance_dict[trafo_size_kva].append(max_distance_m)
            trafo_avg_distance_dict[trafo_size_kva].append(avg_distance_m)

            time += 1
            if total_grids > 0 and time / total_grids >= 0.1:
                percent += 10
                self.dbc.logger.info(f"{percent} % processed")
                time = 0

        self.dbc.logger.info("Transformer-parameter aggregation finished.")

        self.dbc.insert_trafo_parameters(
            plz,
            json.dumps(trafo_load_dict),
            json.dumps(trafo_max_distance_dict),
            json.dumps(trafo_avg_distance_dict)
        )
