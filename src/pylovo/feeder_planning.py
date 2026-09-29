"""Feeder topology planning and feeder cable sizing for one LV grid.

One LV grid is the street network behind one station (one ``kcid``/``bcid``
cluster). ``GridGenerator.install_cables`` plans its feeders in two passes, so
that feeder segments shared by several branches are sized for everything that
is connected behind them:

1. **Topology** (:func:`plan_feeder_branches`). Repeatedly take the remaining
   connection node that is furthest from the transformer and walk along its
   routed path towards the transformer, adding the remaining connection nodes
   until the coincident current of the branch reaches
   ``FEEDER_SPLIT_MAX_CURRENT_KA`` (the node that reaches it starts a later
   branch). The branch is then attached to the nearest node of an earlier branch
   on its path to the transformer (a split point) if that node is at least
   ``MIN_SHARED_PREFIX_LENGTH_M`` from the transformer, otherwise directly to the
   transformer. The last remaining node always gets its own feeder from the
   transformer.
2. **Sizing** (:func:`size_feeder_tree`). The branches form a tree rooted at the
   transformer. It is cut into sections between hard nodes (the transformer and
   every node with more than one child) and each section gets one conductor:

   a. *ampacity*: the smallest design (fewest parallel cables, then the smallest
      cross-section) that carries the largest coincident current of the section;
   b. *voltage drop*: while the approximate drop from the transformer to some load
      node exceeds ``MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT``, apply the
      conductor upgrade (same parallel count) with the largest reduction of the
      excess drops per euro of extra cable cost.

Coincident loads come from :func:`pylovo.utils.simultaneous_peak_load` over all
planning nodes downstream of an edge. Nothing here creates backend components or
database rows; the only database access is the optional path lookup passed in
by the caller.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pylovo import utils
from pylovo.cable_installer import CableInstaller
from pylovo.config_loader import (
    DEFAULT_POWER_FACTOR,
    FEEDER_SPLIT_MAX_CURRENT_KA,
    MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT,
    MIN_SHARED_PREFIX_LENGTH_M,
    VN,
)

#: Directed feeder edge ``(parent, child)``; the parent is closer to the transformer.
Edge = tuple[int, int]
#: Returns the routed node path from a vertex to the transformer (vertex first).
PathLookup = Callable[[int], Sequence[int]]

# Safety stop for the voltage-drop upgrade loop.
_MAX_VOLTAGE_UPGRADES = 10000


@dataclass
class FeederBranch:
    """One planned feeder branch.

    Attributes:
        index: Planning order (0 = first branch).
        nodes: Connection nodes of the branch, ordered from its far end towards
            the transformer; ``nodes[-1]`` is the branch start.
        attachment_node: Node the branch start is connected to: the transformer
            vertex or a node of an earlier branch (split point).
    """

    index: int
    nodes: list[int]
    attachment_node: int


@dataclass
class FeederDesign:
    """Cable design of the feeder tree of one grid (result of :func:`size_feeder_tree`).

    Attributes:
        cable_by_edge: ``(cable name, parallel count)`` per feeder edge.
        sizing_by_edge: Per edge ``feeder_section_id``, ``feeder_sizing_basis``
            (``"ampacity"`` or ``"end_to_end_voltage"``), ``ampacity_std_type`` and
            ``ampacity_parallel``.
        section_by_edge: Section id per edge.
        downstream_nodes_by_node: Tree nodes behind each node (the node itself
            included, except for the transformer).
        diagnostics: ``ampacity_max_feeder_voltage_drop_percent``,
            ``selected_max_feeder_voltage_drop_percent`` and
            ``feeder_voltage_drop_limit_met``.
        drop_percent_by_node: Planned feeder voltage drop in percent from the
            transformer to every tree node.
    """

    cable_by_edge: dict[Edge, tuple[str, int]]
    sizing_by_edge: dict[Edge, dict]
    section_by_edge: dict[Edge, int]
    downstream_nodes_by_node: dict[int, list[int]]
    diagnostics: dict[str, float | bool]
    drop_percent_by_node: dict[int, float]


# =============================================================================
# 1. Topology
# =============================================================================
def plan_feeder_branches(
    connection_nodes: list[int],
    distance_m_by_node: dict[int, float],
    transformer_node: int,
    path_to_transformer: PathLookup,
    buildings_df: pd.DataFrame,
    consumer_df: pd.DataFrame,
    logger: logging.Logger,
) -> list[FeederBranch]:
    """Split the connection nodes of one grid into feeder branches.

    See the module docstring for the rules.

    Args:
        connection_nodes: Street-side connection nodes of the grid.
        distance_m_by_node: Routed distance from the transformer in m
            (``vertices_dict`` of ``GridGenerator``).
        transformer_node: Transformer vertex.
        path_to_transformer: Routed path lookup, see :data:`PathLookup`.
        buildings_df: Buildings of the grid, for the coincident branch load.
        consumer_df: Consumer categories with simultaneity factors.
        logger: Logger for debug messages.

    Returns:
        The branches in planning order; every connection node is in exactly one.
    """
    branches: list[FeederBranch] = []
    remaining = list(connection_nodes)
    planned_nodes: set[int] = set()
    loads = utils.CoincidentLoads(buildings_df, consumer_df)

    while remaining:
        if len(remaining) == 1:
            branch_nodes = [remaining[0]]
            attachment_node = transformer_node
            logger.debug(f"Final remaining connection node {remaining[0]}; preserving direct branch.")
        else:
            path = _path_of_furthest_node(remaining, distance_m_by_node, path_to_transformer)
            branch_nodes, current_ka = _cut_branch_at_current_limit(path, loads)
            logger.debug(
                f"Selected branch {len(branches)} (nodes={len(branch_nodes)}, first={branch_nodes[0]}, "
                f"last={branch_nodes[-1]}, Imax={current_ka:.3f} kA)"
            )
            attachment_node = _attachment_node(
                branch_nodes[-1], transformer_node, distance_m_by_node, planned_nodes, path_to_transformer
            )

        branches.append(
            FeederBranch(index=len(branches), nodes=list(branch_nodes), attachment_node=attachment_node)
        )
        for node in branch_nodes:
            remaining.remove(node)
        planned_nodes.update(branch_nodes)

    return branches


def _path_of_furthest_node(
    remaining: list[int],
    distance_m_by_node: dict[int, float],
    path_to_transformer: PathLookup,
) -> list[int]:
    """Return the remaining nodes on the path from the furthest remaining node to the transformer."""
    furthest_node = max(remaining, key=distance_m_by_node.__getitem__)  # first one on ties
    remaining_set = set(remaining)
    return [node for node in path_to_transformer(furthest_node) if node in remaining_set]


def _cut_branch_at_current_limit(path: list[int], loads: utils.CoincidentLoads) -> tuple[list[int], float]:
    """Take nodes from the far end of ``path`` while the branch stays below the current limit.

    A single node that alone reaches ``FEEDER_SPLIT_MAX_CURRENT_KA`` still forms a
    branch (cables are sized later, possibly in parallel).

    Returns:
        ``(branch nodes, coincident branch current in kA)``.
    """
    branch: list[int] = []
    current_ka = 0.0
    for node in path:
        branch.append(node)
        node_current_ka = utils.design_current_ka(loads.simultaneous_peak_load(branch))
        if node_current_ka >= FEEDER_SPLIT_MAX_CURRENT_KA:
            if len(branch) > 1:
                branch.pop()  # this node starts a later branch
            else:
                current_ka = node_current_ka
            break
        current_ka = node_current_ka
    return branch, current_ka


def _attachment_node(
    branch_start_node: int,
    transformer_node: int,
    distance_m_by_node: dict[int, float],
    planned_nodes: set[int],
    path_to_transformer: PathLookup,
) -> int:
    """Return the node a new branch is connected to.

    That is the first already planned node on the path from the branch start to
    the transformer, so later branches split off existing feeders at street
    corners instead of all starting at the transformer. If that node is closer to
    the transformer than ``MIN_SHARED_PREFIX_LENGTH_M``, the branch starts at the
    transformer instead.
    """
    for node in list(path_to_transformer(branch_start_node))[1:]:
        if node in planned_nodes:
            if distance_m_by_node.get(node, 0.0) < MIN_SHARED_PREFIX_LENGTH_M:
                return transformer_node
            return node
    return transformer_node


def feeder_tree_edges(branches: list[FeederBranch], transformer_node: int) -> list[Edge]:
    """Return the directed feeder edges of the planned branches.

    Per branch: the edges between consecutive branch nodes, then the edge from
    the attachment node to the branch start (unless the branch starts at the
    transformer vertex itself).
    """
    edges: list[Edge] = []
    for branch in branches:
        branch_nodes = [int(node) for node in branch.nodes]
        for index in range(len(branch_nodes) - 1):
            edges.append((int(branch_nodes[index + 1]), int(branch_nodes[index])))

        branch_start_node = int(branch_nodes[-1])
        if branch_start_node != transformer_node:
            edges.append((int(branch.attachment_node), branch_start_node))
    return edges


def split_visualization_edges(branches: list[FeederBranch], transformer_node: int) -> list[dict[str, int]]:
    """Return the feeder edges that get laterally shifted helper lines in the GIS views.

    At a node with several children, the child with the smallest id keeps the
    centre line; the others get offset ranks +1, -1, +2, -2, ...

    Returns:
        Dicts with ``from_bus``, ``to_bus`` and ``offset_rank``.
    """
    children_by_parent: dict[int, set[int]] = {}
    for parent, child in feeder_tree_edges(branches, transformer_node):
        children_by_parent.setdefault(parent, set()).add(child)

    split_edges = []
    for parent, children in children_by_parent.items():
        ordered_children = sorted(children)
        if len(ordered_children) <= 1:
            continue

        for child_index, child in enumerate(ordered_children[1:], start=1):
            sign = 1 if child_index % 2 else -1
            magnitude = (child_index + 1) // 2
            split_edges.append(
                {
                    "from_bus": int(parent),
                    "to_bus": int(child),
                    "offset_rank": int(sign * magnitude),
                }
            )

    return split_edges


# =============================================================================
# 2. Sizing
# =============================================================================
def size_feeder_tree(
    installer: CableInstaller,
    branches: list[FeederBranch],
    transformer_node: int,
    distance_m_by_node: dict[int, float],
    buildings_df: pd.DataFrame,
    consumer_df: pd.DataFrame,
    logger: logging.Logger,
    plz: int,
) -> FeederDesign:
    """Choose the feeder cable of every edge of the planned feeder tree.

    See the module docstring for the method. The voltage drop of an edge is
    approximated as ``sqrt(3) * I * L * (R cos(phi) + X sin(phi)) / n / VN``
    with the coincident current ``I`` of everything downstream of the edge.

    Args:
        installer: Cable installer of the grid (feeder cable catalogue).
        branches: Result of :func:`plan_feeder_branches`.
        transformer_node: Transformer vertex.
        distance_m_by_node: Routed distance from the transformer in m.
        buildings_df: Buildings of the grid.
        consumer_df: Consumer categories with simultaneity factors.
        logger: Logger for the sizing summary and warnings.
        plz: Postcode, only used in log messages.

    Returns:
        The feeder design.

    Raises:
        KeyError: If a feeder node has no routed distance.
        ValueError: If a consumer maps to two planning nodes or a load-bearing
            planning node is not in the feeder tree.
        RuntimeError: If the voltage upgrade loop does not terminate.
    """
    children_by_node: dict[int, list[int]] = {}
    for parent, child in feeder_tree_edges(branches, transformer_node):
        children_by_node.setdefault(parent, []).append(child)

    downstream_nodes_by_node = _downstream_nodes(children_by_node, transformer_node)
    sections_by_key = group_edges_into_sections(children_by_node, transformer_node)
    section_by_edge = {
        edge: int(section_id)
        for section_id, section_edges in sections_by_key.items()
        for edge in section_edges
    }

    def _distance_from_transformer(node: int) -> float:
        if node == transformer_node:
            return 0.0
        try:
            return float(distance_m_by_node[node])
        except KeyError as exc:
            raise KeyError(
                f"Missing routed distance for feeder node {node} while sizing feeder sections."
            ) from exc

    # --- a. ampacity ---------------------------------------------------------
    section_designs, edge_current_ka = _ampacity_designs(
        installer, sections_by_key, downstream_nodes_by_node, buildings_df, consumer_df,
        _distance_from_transformer,
    )

    # --- b. end-to-end voltage drop -------------------------------------------
    edge_length_km = {
        (parent, child): (_distance_from_transformer(child) - _distance_from_transformer(parent))
        * 1e-3
        for parent, child in edge_current_ka
    }
    path_by_node = _edge_paths_from_root(children_by_node, transformer_node)

    load_planning_nodes = _load_planning_nodes(buildings_df)
    assessment_nodes = sorted(node for node in load_planning_nodes if node in path_by_node)
    unreachable_load_nodes = sorted(
        node for node in load_planning_nodes if node not in path_by_node
    )
    if unreachable_load_nodes:
        raise ValueError(
            "Load-bearing planning nodes are absent from the finalized feeder tree: "
            f"{unreachable_load_nodes[:10]}"
        )

    # Drop in percent of an edge = edge_factor * effective impedance (ohm/km) of its cable.
    nominal_voltage_kv = VN * 1e-3
    edge_factor = {
        edge: np.sqrt(3) * current_ka * edge_length_km[edge]
        / nominal_voltage_kv
        * 100
        for edge, current_ka in edge_current_ka.items()
    }

    def _path_drop_percent(path) -> float:
        return sum(
            edge_factor[edge]
            * _effective_voltage_impedance(section_designs[section_by_edge[edge]]["selected"])
            for edge in path
        )

    def _path_drops_percent() -> dict[int, float]:
        return {node: _path_drop_percent(path_by_node[node]) for node in assessment_nodes}

    # Sum of the edge factors of each section on the path to each assessed node.
    section_factors_by_node = {
        node: _section_factors(path_by_node[node], section_by_edge, edge_factor)
        for node in assessment_nodes
    }
    upgrade_options_by_section = {
        section_id: [
            option
            for option in installer.get_feeder_cable_options(
                design["design_current_ka"], design["ampacity"]["parallel"]
            )
            if option["parallel"] == design["ampacity"]["parallel"]
        ]
        for section_id, design in section_designs.items()
    }

    initial_path_drops = _path_drops_percent()
    voltage_upgrade_iterations = 0
    while True:
        path_drops = _path_drops_percent()
        violations = {
            node: voltage_drop - MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT
            for node, voltage_drop in path_drops.items()
            if voltage_drop > MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT + 1e-9
        }
        if not violations:
            break

        upgrade = _best_voltage_upgrade(
            section_designs, upgrade_options_by_section, violations, section_factors_by_node
        )
        if upgrade is None:
            logger.warning(
                "Could not satisfy the end-to-end feeder voltage-drop envelope because no "
                "lower-impedance feeder design remained."
            )
            break

        section_id, option = upgrade
        section_designs[section_id]["selected"] = option
        voltage_upgrade_iterations += 1
        if voltage_upgrade_iterations > _MAX_VOLTAGE_UPGRADES:
            raise RuntimeError(
                f"End-to-end feeder voltage sizing exceeded {_MAX_VOLTAGE_UPGRADES} upgrades."
            )

    # --- result and diagnostics -------------------------------------------------
    initial_max_drop_percent = float(max(initial_path_drops.values(), default=0.0))
    final_max_drop_percent = float(max(_path_drops_percent().values(), default=0.0))
    feeder_voltage_drop_limit_met = bool(
        final_max_drop_percent <= MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT + 1e-9
    )
    voltage_upgraded_sections = sum(
        _is_voltage_upgraded(design) for design in section_designs.values()
    )
    logger.info(
        f"End-to-end feeder voltage sizing finished for plz={plz}: "
        f"assessment_nodes={len(assessment_nodes)}, sections={len(section_designs)}, "
        f"voltage_upgraded_sections={voltage_upgraded_sections}, "
        f"initial_max_drop_percent={initial_max_drop_percent:.3f}, "
        f"final_max_drop_percent={final_max_drop_percent:.3f}, "
        f"limit_percent={MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT:.3f}."
    )
    if not feeder_voltage_drop_limit_met:
        logger.warning(
            f"Feeder planning voltage-drop envelope remains violated for plz={plz}: "
            f"selected_max_drop_percent={final_max_drop_percent:.3f}, "
            f"limit_percent={MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT:.3f}. "
            "Every remaining useful section is already at the largest configured conductor "
            "for its ampacity-required parallel count; topology or transformer placement "
            "would have to change."
        )

    cable_by_edge: dict[Edge, tuple[str, int]] = {}
    sizing_by_edge: dict[Edge, dict] = {}
    for section_id, design in section_designs.items():
        selected = design["selected"]
        ampacity = design["ampacity"]
        for edge in design["edges"]:
            cable_by_edge[edge] = (selected["cable"], selected["parallel"])
            sizing_by_edge[edge] = {
                "feeder_section_id": section_id,
                "feeder_sizing_basis": "end_to_end_voltage" if _is_voltage_upgraded(design) else "ampacity",
                "ampacity_std_type": ampacity["cable"],
                "ampacity_parallel": ampacity["parallel"],
            }

    return FeederDesign(
        cable_by_edge=cable_by_edge,
        sizing_by_edge=sizing_by_edge,
        section_by_edge=section_by_edge,
        downstream_nodes_by_node=downstream_nodes_by_node,
        diagnostics={
            "ampacity_max_feeder_voltage_drop_percent": initial_max_drop_percent,
            "selected_max_feeder_voltage_drop_percent": final_max_drop_percent,
            "feeder_voltage_drop_limit_met": feeder_voltage_drop_limit_met,
        },
        drop_percent_by_node={
            node: float(_path_drop_percent(path)) for node, path in path_by_node.items()
        },
    )


def group_edges_into_sections(
    children_by_node: dict[int, list[int]],
    transformer_node: int,
) -> dict[int, list[Edge]]:
    """Group the directed feeder edges into sections between hard nodes.

    Hard nodes are the transformer and every node with more than one child. A
    section starts at a hard node and follows single-child nodes until it reaches
    a hard node or a leaf. Sections are numbered by ascending hard node id.

    Returns:
        ``{section id: edges from the hard node outwards}``.
    """
    hard_nodes = {transformer_node}
    hard_nodes.update(
        parent for parent, children in children_by_node.items() if len(children) > 1
    )
    sections_by_key: dict[int, list[Edge]] = {}
    section_index = 0

    for hard_node in sorted(hard_nodes):
        for first_child in children_by_node.get(hard_node, []):
            section_edges = []
            parent = hard_node
            child = int(first_child)

            while True:
                section_edges.append((parent, child))
                child_children = children_by_node.get(child, [])
                if child in hard_nodes or len(child_children) != 1:
                    break

                parent = child
                child = int(child_children[0])

            sections_by_key[section_index] = section_edges
            section_index += 1

    return sections_by_key


def _downstream_nodes(children_by_node: dict[int, list[int]], root: int) -> dict[int, list[int]]:
    """Return, per node reachable from ``root``, the node itself plus all nodes behind it.

    The root itself is not part of its own list. Children are visited in the order
    of ``children_by_node`` (iterative, so deep feeders do not hit the recursion limit).
    """
    preorder = []
    stack = [root]
    while stack:
        node = stack.pop()
        preorder.append(node)
        stack.extend(children_by_node.get(node, []))

    downstream_nodes_by_node: dict[int, list[int]] = {}
    for node in reversed(preorder):  # children before their parents
        downstream_nodes = [] if node == root else [node]
        for child in children_by_node.get(node, []):
            downstream_nodes.extend(downstream_nodes_by_node[child])
        downstream_nodes_by_node[node] = downstream_nodes
    return downstream_nodes_by_node


def _edge_paths_from_root(children_by_node: dict[int, list[int]], root: int) -> dict[int, tuple[Edge, ...]]:
    """Return the edges on the path from ``root`` to every reachable node."""
    path_by_node: dict[int, tuple[Edge, ...]] = {root: ()}
    stack = [root]
    while stack:
        parent = stack.pop()
        for child in children_by_node.get(parent, []):
            edge = (parent, child)
            path_by_node[child] = path_by_node[parent] + (edge,)
            stack.append(child)
    return path_by_node


def _ampacity_designs(
    installer: CableInstaller,
    sections_by_key: dict[int, list[Edge]],
    downstream_nodes_by_node: dict[int, list[int]],
    buildings_df: pd.DataFrame,
    consumer_df: pd.DataFrame,
    distance_from_transformer: Callable[[int], float],
) -> tuple[dict[int, dict], dict[Edge, float]]:
    """Size every section for the largest coincident current of its edges.

    Returns:
        ``(section_designs, edge_current_ka)``. A section design holds ``edges``,
        ``length_km``, ``design_current_ka``, the ampacity choice ``ampacity`` and
        the current choice ``selected`` (both feeder cable option dicts, see
        ``CableInstaller.get_feeder_cable_options``).
    """
    section_designs: dict[int, dict] = {}
    edge_current_ka: dict[Edge, float] = {}
    loads = utils.CoincidentLoads(buildings_df, consumer_df)

    for section_id, section_edges in sections_by_key.items():
        section_Imax = 0.0
        section_distance = 0.0

        for parent, child in section_edges:
            sim_load = loads.simultaneous_peak_load(downstream_nodes_by_node[child])
            edge_Imax = utils.design_current_ka(sim_load)
            edge_distance = distance_from_transformer(child) - distance_from_transformer(parent)
            section_Imax = max(section_Imax, edge_Imax)
            section_distance += edge_distance
            edge_current_ka[(parent, child)] = edge_Imax

        cable, count = installer.find_minimal_available_cable(section_Imax)
        matching_option = next(
            option
            for option in installer.get_feeder_cable_options(section_Imax, count)
            if option["cable"] == cable and option["parallel"] == count
        )
        section_designs[int(section_id)] = {
            "edges": list(section_edges),
            "length_km": section_distance * 1e-3,
            "design_current_ka": section_Imax,
            "ampacity": dict(matching_option),
            "selected": dict(matching_option),
        }

    return section_designs, edge_current_ka


def _load_planning_nodes(buildings_df: pd.DataFrame) -> set[int]:
    """Return the planning nodes that carry consumers.

    Raises:
        ValueError: If one consumer vertex maps to two planning nodes.
    """
    consumer_to_planning_node = {}
    for consumer_vertex, planning_node in zip(
        buildings_df["vertice_id"], utils.planning_nodes(buildings_df), strict=True
    ):
        consumer_vertex = int(consumer_vertex)
        planning_node = int(planning_node)
        existing_node = consumer_to_planning_node.setdefault(consumer_vertex, planning_node)
        if existing_node != planning_node:
            raise ValueError(
                f"Consumer vertex {consumer_vertex} maps to multiple planning nodes: "
                f"{existing_node} and {planning_node}."
            )
    return set(consumer_to_planning_node.values())


_SIN_PHI = np.sqrt(1 - DEFAULT_POWER_FACTOR ** 2)


def _effective_voltage_impedance(option: dict) -> float:
    """Return ``(R cos(phi) + X sin(phi)) / parallel`` of a cable option in ohm/km."""
    return (
        option["r_ohm_per_km"] * DEFAULT_POWER_FACTOR
        + option["x_ohm_per_km"] * _SIN_PHI
    ) / option["parallel"]


def _is_voltage_upgraded(design: dict) -> bool:
    """Return whether the voltage-drop pass replaced the ampacity choice of a section."""
    return (
        design["selected"]["cable"] != design["ampacity"]["cable"]
        or design["selected"]["parallel"] != design["ampacity"]["parallel"]
    )


def _section_factors(
    path: tuple[Edge, ...], section_by_edge: dict[Edge, int], edge_factor: dict[Edge, float]
) -> dict[int, float]:
    """Return the sum of the edge factors per section along ``path``."""
    edges_by_section: dict[int, list[Edge]] = {}
    for edge in path:
        edges_by_section.setdefault(section_by_edge[edge], []).append(edge)
    return {
        section_id: sum(edge_factor[edge] for edge in edges)
        for section_id, edges in edges_by_section.items()
    }


def _best_voltage_upgrade(
    section_designs: dict[int, dict],
    upgrade_options_by_section: dict[int, list[dict]],
    violations: dict[int, float],
    section_factors_by_node: dict[int, dict[int, float]],
) -> tuple[int, dict] | None:
    """Return the most cost-effective conductor upgrade, or None if none helps.

    A candidate is a lower-impedance option of a section with the parallel count
    fixed by ampacity. Its benefit is the sum over violated nodes of the drop
    reduction on their path, each capped at the node's excess drop; it is divided
    by the extra cable cost of the section (at least 1e-9 EUR). Ties prefer the
    larger benefit, the lower cost, fewer parallel cables and the smaller
    cross-section.

    Returns:
        ``(section id, selected option)``.
    """
    best_candidate = None
    for section_id, design in section_designs.items():
        current_option = design["selected"]
        current_impedance = _effective_voltage_impedance(current_option)
        current_cost_per_m = (
            current_option["cost_eur_per_m"] * current_option["parallel"]
        )
        for option in upgrade_options_by_section[section_id]:
            option_impedance = _effective_voltage_impedance(option)
            if option_impedance >= current_impedance - 1e-12:
                continue

            aggregate_reduction = 0.0
            for node, excess_drop in violations.items():
                section_factor = section_factors_by_node[node].get(section_id)
                if section_factor is None:
                    continue  # the section is not on this node's path
                reduction = section_factor * (current_impedance - option_impedance)
                aggregate_reduction += min(excess_drop, reduction)

            if aggregate_reduction <= 0:
                continue
            incremental_cost = max(
                (
                    option["cost_eur_per_m"] * option["parallel"]
                    - current_cost_per_m
                )
                * design["length_km"]
                * 1000,
                1e-9,
            )
            score = aggregate_reduction / incremental_cost
            candidate_key = (
                score,
                aggregate_reduction,
                -incremental_cost,
                -option["parallel"],
                -option["q_mm2"],
            )
            if best_candidate is None or candidate_key > best_candidate[0]:
                best_candidate = (candidate_key, section_id, dict(option))

    if best_candidate is None:
        return None
    _, section_id, option = best_candidate
    return section_id, option
