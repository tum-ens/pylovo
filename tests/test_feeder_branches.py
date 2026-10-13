"""Feeder branch planning (``feeder_planning.plan_feeder_branches``): every branch, the last one too, joins an existing feeder."""

import logging

import pytest

from pylovo import feeder_planning, utils

# Transformer 0; a street 0-1-2-3 and a side street 1-4.
DISTANCE = {0: 0.0, 1: 100.0, 2: 200.0, 3: 300.0, 4: 150.0}
PARENT = {1: 0, 2: 1, 3: 2, 4: 1}


def path_to_transformer(node):
    if node == 0:
        return []   # the transformer's own vertex has no routed path
    path = [node]
    while path[-1] != 0:
        path.append(PARENT[path[-1]])
    return path


class CurrentPerNode:
    def __init__(self, buildings, consumers):
        pass

    def simultaneous_peak_load(self, nodes):
        return 0.01 * len(nodes)   # 10 A per node


def plan(monkeypatch, nodes, prefix_m, split_ka=1.0):
    monkeypatch.setattr(feeder_planning, "FEEDER_SPLIT_MAX_CURRENT_KA", split_ka)
    monkeypatch.setattr(feeder_planning, "MIN_SHARED_PREFIX_LENGTH_M", prefix_m)
    monkeypatch.setattr(utils, "CoincidentLoads", CurrentPerNode)
    monkeypatch.setattr(utils, "design_current_ka", lambda load: load)
    branches = feeder_planning.plan_feeder_branches(
        nodes, DISTANCE, 0, path_to_transformer, None, None, logging.getLogger("feeder-branches-test"))
    return [(b.nodes, b.attachment_node) for b in branches]


def test_the_last_remaining_node_joins_the_existing_feeder_like_any_branch(monkeypatch):
    # the main street is planned first; the side-street node 4 is the last one left and splits off at node 1
    assert plan(monkeypatch, [1, 2, 3, 4], prefix_m=50) == [([3, 2, 1], 0), ([4], 1)]


def test_within_the_shared_prefix_the_last_node_gets_its_own_feeder(monkeypatch):
    assert plan(monkeypatch, [1, 2, 3, 4], prefix_m=120) == [([3, 2, 1], 0), ([4], 0)]


@pytest.mark.parametrize("nodes", [[3], [1]])
def test_a_grid_with_one_connection_node_gets_one_feeder(monkeypatch, nodes):
    assert plan(monkeypatch, nodes, prefix_m=50) == [(nodes, 0)]


def test_a_connection_node_at_the_transformer_vertex_is_a_branch_at_the_station(monkeypatch):
    # buildings connected at the station's own vertex: with a low split current no branch reaches it,
    # so it is left last (distance 0) and has no routed path of its own
    assert plan(monkeypatch, [0, 1], prefix_m=50, split_ka=0.015) == [([1], 0), ([0], 0)]
