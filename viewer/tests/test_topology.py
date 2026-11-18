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
    # station 0: feeder A (0-1, houses 2 and 4: 0.3 km) and the longer feeder B (0-3, houses 5 and 6: 0.6 km);
    # cable 9 is not connected
    edges = [(10, 0, 1, 0.1), (11, 1, 2, 0.1), (13, 1, 4, 0.1), (12, 3, 0, 0.5), (14, 3, 5, 0.05), (15, 3, 6, 0.05),
             (9, 7, 8, 1.0)]
    cables, vertices = feeders(0, edges)
    assert cables == {12: 1, 14: 1, 15: 1, 10: 2, 11: 2, 13: 2}
    assert vertices == {3: 1, 5: 1, 6: 1, 1: 2, 2: 2, 4: 2}


def test_a_house_on_its_own_cable_from_the_station_is_no_feeder():
    # house 9 hangs on its own cable at the station (as in the structural metrics: no feeder)
    edges = [(10, 0, 1, 0.1), (11, 1, 2, 0.1), (12, 1, 3, 0.1), (20, 0, 9, 0.03)]
    cables, vertices = feeders(0, edges)
    assert cables == {10: 1, 11: 1, 12: 1} and 9 not in vertices


def test_the_busbar_link_of_older_grids_belongs_to_the_station():
    # station 0 -- 1 m link -- street node 1, which starts two feeders (not one feeder for the whole grid)
    edges = [(1, 0, 1, 0.001), (10, 1, 2, 0.2), (11, 2, 3, 0.02), (12, 2, 4, 0.02),
             (20, 1, 5, 0.1), (21, 5, 6, 0.02), (22, 5, 7, 0.02)]
    cables, vertices = feeders(0, edges)
    assert cables == {10: 1, 11: 1, 12: 1, 20: 2, 21: 2, 22: 2} and 1 not in vertices


def test_feeders_without_station():
    assert feeders(None, [(1, 0, 1, 0.1)]) == ({}, {})
    assert feeders(5, []) == ({}, {})
