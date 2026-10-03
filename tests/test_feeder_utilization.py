"""Feeder cable ampacity sizing with FEEDER_PLANNING_UTILIZATION (``feeder_planning._ampacity_designs``)."""

import logging

import pandas as pd
import pytest

from pylovo import feeder_planning, utils
from pylovo.cable_installer import CableInstaller

# (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur): the shipped feeder catalogue.
CABLES = [
    ("NAYY_4_150", 0.208, 0.080, 0.270, 25.0), ("NAYY_4_185", 0.164, 0.080, 0.313, 38.0),
    ("NAYY_4_240", 0.127, 0.080, 0.357, 45.0), ("NAYY_4_300", 0.100, 0.080, 0.425, 58.0),
]


class CurrentPerNode:
    """Coincident loads whose "load" of a node set is its design current in kA."""

    def __init__(self, currents):
        self.currents = currents

    def simultaneous_peak_load(self, nodes):
        return sum(self.currents[node] for node in nodes)


@pytest.mark.parametrize("utilization, current_ka, expected", [
    (1.0, 0.25, ("NAYY_4_150", 1)),
    (0.8, 0.25, ("NAYY_4_185", 1)),   # 0.3125 kA planned
    (0.8, 0.20, ("NAYY_4_150", 1)),   # 0.25 kA planned
    (0.7, 0.24, ("NAYY_4_240", 1)),   # 0.343 kA planned
    (0.7, 0.25, ("NAYY_4_300", 1)),   # 0.3571 kA planned, just above 4x240 (0.357)
    (0.5, 0.25, ("NAYY_4_150", 2)),   # 0.5 kA: no single cable, two in parallel
])
def test_section_is_sized_for_the_current_over_the_utilization(monkeypatch, utilization, current_ka, expected):
    monkeypatch.setattr(feeder_planning, "FEEDER_PLANNING_UTILIZATION", utilization)
    monkeypatch.setattr(utils, "CoincidentLoads", lambda buildings, consumers: CurrentPerNode({1: current_ka}))
    monkeypatch.setattr(utils, "design_current_ka", lambda load: load)
    installer = CableInstaller(None, None, logging.getLogger("feeder-utilization-test"), CABLES,
                               pd.DataFrame({"name": [name for name, *_ in CABLES]}), pd.DataFrame())

    designs, edge_current = feeder_planning._ampacity_designs(
        installer, {0: [(0, 1)]}, {1: [1]}, None, None, lambda node: 100.0 * node)

    design = designs[0]
    assert (design["ampacity"]["cable"], design["ampacity"]["parallel"]) == expected
    assert design["selected"] == design["ampacity"]
    assert design["design_current_ka"] == current_ka == edge_current[(0, 1)]   # voltage drops use the current itself
