"""Tests of the colour slots and feeder numbers (no database)."""
from __future__ import annotations

import itertools

import numpy as np
from pylovo_viewer.topology import colour_slots, feeders


def test_neighbours_never_share_a_slot():
    xy = np.array([[x * 100.0, y * 100.0] for x, y in itertools.product(range(12), range(12))])
    keys = list(range(len(xy)))
    slots = colour_slots(keys, xy)
    assert set(slots.values()) <= set(range(8))
    for i, j in itertools.combinations(keys, 2):
        if np.hypot(*(xy[i] - xy[j])) == 100.0:
            assert slots[i] != slots[j]


def test_slots_of_tiny_inputs():
    assert colour_slots([], np.zeros((0, 2))) == {}
    assert colour_slots(["a"], np.array([[0.0, 0.0]])) == {"a": 0}


def test_feeders_numbered_by_length():
    # station 0: feeder A (0-1-2, 0.3 km) and the longer feeder B (0-3, 0.5 km); cable 9 is not connected
    edges = [(10, 0, 1, 0.1), (11, 1, 2, 0.2), (12, 3, 0, 0.5), (9, 7, 8, 1.0)]
    cables, vertices = feeders(0, edges)
    assert cables == {12: 1, 10: 2, 11: 2}
    assert vertices == {3: 1, 1: 2, 2: 2}


def test_feeders_without_station():
    assert feeders(None, [(1, 0, 1, 0.1)]) == ({}, {})
    assert feeders(5, []) == ({}, {})
