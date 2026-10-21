"""Feeder tree sizing (``feeder_planning.size_feeder_tree``) and the voltage upgrade choice (``_best_voltage_upgrade``).

No database and no pandapower: the installer only holds the cable catalogue. The planning rules under test
are the documented ones: ampacity at ``CABLE_PLANNING_UTILIZATION`` (fewest parallel cables, then the cheapest
cable), one cable per section between hard nodes, and the voltage-drop upgrade with the best reduction per euro.
"""

import logging
import math

import pandas as pd
import pytest

from pylovo import feeder_planning, utils
from pylovo.cable_installer import CableInstaller
from pylovo.feeder_planning import FeederBranch

# (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur): the shipped feeder catalogue.
CABLES = [
    ("NAYY_4_150", 0.208, 0.080, 0.270, 25.0), ("NAYY_4_185", 0.164, 0.080, 0.313, 38.0),
    ("NAYY_4_240", 0.127, 0.080, 0.357, 45.0), ("NAYY_4_300", 0.100, 0.080, 0.425, 58.0),
]
IMPEDANCE = {name: (r, x) for name, r, x, _, _ in CABLES}
LOGGER = logging.getLogger("feeder-sizing-test")
CONSUMERS = pd.DataFrame({"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]})


class KaPerNode:
    """Coincident loads whose "load" of a node set is its design current in kA (sum of the per-node currents)."""

    def __init__(self, currents):
        self.currents = currents

    def simultaneous_peak_load(self, nodes):
        return sum(self.currents[node] for node in nodes)


@pytest.fixture
def installer():
    return CableInstaller(None, None, LOGGER, CABLES, pd.DataFrame({"name": [name for name, *_ in CABLES]}),
                          pd.DataFrame())


@pytest.fixture(autouse=True)
def planning_rules(monkeypatch):
    """The default planning rules, independent of the configuration the tests run with."""
    monkeypatch.setattr(feeder_planning, "CABLE_PLANNING_UTILIZATION", 0.8)
    monkeypatch.setattr(feeder_planning, "FEEDER_STANDARD_CABLE", None)
    monkeypatch.setattr(feeder_planning, "MAX_PARALLEL_FEEDER_CABLES", 4)
    monkeypatch.setattr(feeder_planning, "MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT", 5.0)
    monkeypatch.setattr(feeder_planning, "VN", 400)


def buildings(*connection_points):
    """One building per connection point (its vertex id is 100 + position)."""
    return pd.DataFrame({"vertice_id": [100 + i for i in range(len(connection_points))],
                         "connection_point": list(connection_points)})


def drop_percent(current_ka, length_km, cable, parallel):
    """The documented approximation sqrt(3) I L (R cos(phi) + X sin(phi)) / n / VN, in percent."""
    pf = feeder_planning.DEFAULT_POWER_FACTOR
    r, x = IMPEDANCE[cable]
    return math.sqrt(3) * current_ka * length_km * (r * pf + x * math.sqrt(1 - pf ** 2)) / parallel / 0.4 * 100


def size(installer, monkeypatch, branches, distance, currents, building_nodes, hard_nodes=()):
    monkeypatch.setattr(utils, "CoincidentLoads", lambda buildings_df, consumer_df: KaPerNode(currents))
    monkeypatch.setattr(utils, "design_current_ka", lambda load: load)
    return feeder_planning.size_feeder_tree(installer, branches, 0, distance, buildings(*building_nodes), None,
                                            LOGGER, 80803, hard_nodes=hard_nodes)


# Transformer 0 - 1 (100 m) - 2 (300 m): one branch, edges (0, 1) and (1, 2).
CHAIN = [FeederBranch(index=0, nodes=[2, 1], attachment_node=0)]


def test_a_section_without_a_violation_keeps_its_ampacity_cable(installer, monkeypatch):
    design = size(installer, monkeypatch, CHAIN, {1: 100.0, 2: 300.0}, {1: 0.05, 2: 0.10}, [1, 2])

    # the section carries 0.15 kA (edge (0, 1)): 0.1875 kA planned fits the cheapest cable
    assert design.cable_by_edge == {(0, 1): ("NAYY_4_150", 1), (1, 2): ("NAYY_4_150", 1)}
    assert design.section_by_edge == {(0, 1): 0, (1, 2): 0}
    assert {edge: sizing["feeder_sizing_basis"] for edge, sizing in design.sizing_by_edge.items()} == {
        (0, 1): "ampacity", (1, 2): "ampacity"}
    assert design.downstream_nodes_by_node == {0: [1, 2], 1: [1, 2], 2: [2]}
    first = drop_percent(0.15, 0.1, "NAYY_4_150", 1)
    second = drop_percent(0.10, 0.2, "NAYY_4_150", 1)
    assert design.drop_percent_by_node == pytest.approx({0: 0.0, 1: first, 2: first + second})
    assert design.diagnostics == {
        "ampacity_max_feeder_voltage_drop_percent": pytest.approx(first + second),
        "selected_max_feeder_voltage_drop_percent": pytest.approx(first + second),
        "feeder_voltage_drop_limit_met": True,
    }


@pytest.mark.parametrize("utilization, current_ka, expected", [
    (1.0, 0.25, ("NAYY_4_150", 1)),
    (0.8, 0.25, ("NAYY_4_185", 1)),   # 0.3125 kA planned: the cheapest cable that carries it, no parallel cable
    (0.8, 0.50, ("NAYY_4_185", 2)),   # 0.625 kA: no single cable, so two parallel cables of the cheapest one
])
def test_cables_are_sized_for_the_current_over_the_planning_utilization(
        installer, monkeypatch, utilization, current_ka, expected):
    monkeypatch.setattr(feeder_planning, "CABLE_PLANNING_UTILIZATION", utilization)
    design = size(installer, monkeypatch, [FeederBranch(0, [1], 0)], {1: 100.0}, {1: current_ka}, [1])

    assert design.cable_by_edge == {(0, 1): expected}
    assert design.sizing_by_edge[(0, 1)]["ampacity_std_type"] == expected[0]
    assert design.sizing_by_edge[(0, 1)]["ampacity_parallel"] == expected[1]


def test_a_voltage_violation_upgrades_the_conductor_with_the_best_reduction_per_euro(installer, monkeypatch):
    # 0.15 kA over 500 m: 7.2 % with the ampacity cable. NAYY_4_185 (5.9 %) removes less than the 2.2 % excess for
    # 6,500 EUR, NAYY_4_240 removes all of it for 10,000 EUR, NAYY_4_300 removes no more and costs 16,500 EUR.
    design = size(installer, monkeypatch, [FeederBranch(0, [1], 0)], {1: 500.0}, {1: 0.15}, [1])

    assert drop_percent(0.15, 0.5, "NAYY_4_150", 1) > 5.0 > drop_percent(0.15, 0.5, "NAYY_4_240", 1)
    assert design.cable_by_edge == {(0, 1): ("NAYY_4_240", 1)}
    assert design.sizing_by_edge[(0, 1)] == {
        "feeder_section_id": 0, "feeder_sizing_basis": "end_to_end_voltage",
        "ampacity_std_type": "NAYY_4_150", "ampacity_parallel": 1}
    assert design.diagnostics["ampacity_max_feeder_voltage_drop_percent"] == pytest.approx(
        drop_percent(0.15, 0.5, "NAYY_4_150", 1))
    assert design.diagnostics["selected_max_feeder_voltage_drop_percent"] == pytest.approx(
        drop_percent(0.15, 0.5, "NAYY_4_240", 1))
    assert design.diagnostics["feeder_voltage_drop_limit_met"] is True


def test_an_unreachable_voltage_limit_ends_at_the_lowest_impedance_design_and_warns(installer, monkeypatch, caplog):
    # 0.15 kA over 800 m stays above 5 % even with NAYY_4_300 (6.2 %): no parallel cable is added without a standard cable
    with caplog.at_level(logging.WARNING, logger=LOGGER.name):
        design = size(installer, monkeypatch, [FeederBranch(0, [1], 0)], {1: 800.0}, {1: 0.15}, [1])

    assert design.cable_by_edge == {(0, 1): ("NAYY_4_300", 1)}
    assert design.diagnostics["feeder_voltage_drop_limit_met"] is False
    assert design.diagnostics["selected_max_feeder_voltage_drop_percent"] == pytest.approx(
        drop_percent(0.15, 0.8, "NAYY_4_300", 1))
    assert any("voltage-drop" in message for message in caplog.messages)


def test_with_a_standard_cable_voltage_upgrades_add_parallel_standard_cables_first(installer, monkeypatch):
    monkeypatch.setattr(feeder_planning, "FEEDER_STANDARD_CABLE", "NAYY_4_150")
    design = size(installer, monkeypatch, [FeederBranch(0, [1], 0)], {1: 500.0}, {1: 0.15}, [1])

    assert design.cable_by_edge == {(0, 1): ("NAYY_4_150", 2)}
    assert design.sizing_by_edge[(0, 1)]["feeder_sizing_basis"] == "end_to_end_voltage"
    assert design.sizing_by_edge[(0, 1)]["ampacity_parallel"] == 1
    assert design.diagnostics["selected_max_feeder_voltage_drop_percent"] == pytest.approx(
        drop_percent(0.15, 0.5, "NAYY_4_150", 2))


def test_a_split_point_ends_a_section_and_the_shared_prefix_carries_everything_behind_it(installer, monkeypatch):
    # 0 - 1 (100 m), then 1 - 2 (200 m) and 1 - 3 (250 m)
    branches = [FeederBranch(0, [2, 1], 0), FeederBranch(1, [3], 1)]
    design = size(installer, monkeypatch, branches, {1: 100.0, 2: 200.0, 3: 250.0},
                  {1: 0.05, 2: 0.10, 3: 0.10}, [1, 2, 3])

    assert design.section_by_edge == {(0, 1): 0, (1, 2): 1, (1, 3): 2}
    assert design.downstream_nodes_by_node[0] == [1, 2, 3]
    # prefix: 0.25 kA / 0.8 needs a NAYY_4_185, each branch 0.10 kA / 0.8 only a NAYY_4_150
    assert design.cable_by_edge == {(0, 1): ("NAYY_4_185", 1), (1, 2): ("NAYY_4_150", 1), (1, 3): ("NAYY_4_150", 1)}


def test_hard_nodes_let_the_cable_change_at_a_cabinet(installer, monkeypatch):
    chain = [FeederBranch(0, [3, 2, 1], 0)]
    distance, currents = {1: 100.0, 2: 200.0, 3: 300.0}, {1: 0.05, 2: 0.20, 3: 0.05}

    one_section = size(installer, monkeypatch, chain, distance, currents, [1, 2, 3])
    assert set(one_section.cable_by_edge.values()) == {("NAYY_4_300", 1)}   # 0.30 kA / 0.8 for every edge

    cabinets = size(installer, monkeypatch, chain, distance, currents, [1, 2, 3], hard_nodes=[2])
    assert cabinets.section_by_edge == {(0, 1): 0, (1, 2): 0, (2, 3): 1}
    assert cabinets.cable_by_edge == {(0, 1): ("NAYY_4_300", 1), (1, 2): ("NAYY_4_300", 1), (2, 3): ("NAYY_4_150", 1)}


def test_the_design_follows_the_coincident_load_of_the_buildings(installer, monkeypatch):
    """With the real loads: 150 households behind the first edge need two parallel NAYY_4_150 at 0.8."""
    rows = [(vertex, node) for node in (1, 2, 3) for vertex in range(100 * node, 100 * node + 50)]
    frame = pd.DataFrame({
        "vertice_id": [vertex for vertex, _ in rows], "connection_point": [node for _, node in rows],
        "residential_peak_load_in_kw": 17.0, "households": 1.0, "nonresidential_peak_load_in_kw": 0.0,
        "nonresidential_use": None, "nonresidential_mv_direct": None,
    })
    design = feeder_planning.size_feeder_tree(installer, [FeederBranch(0, [3, 2, 1], 0)], 0,
                                              {1: 100.0, 2: 200.0, 3: 300.0}, frame, CONSUMERS, LOGGER, 80803)

    planned_ka = utils.design_current_ka(utils.simultaneous_peak_load(frame, CONSUMERS, [1, 2, 3])) / 0.8
    assert max(max_i_ka for _, _, _, max_i_ka, _ in CABLES) < planned_ka   # no single cable carries it
    assert set(design.cable_by_edge.values()) == {("NAYY_4_150", 2)}
    assert design.diagnostics["feeder_voltage_drop_limit_met"] is True


def test_a_feeder_node_without_a_routed_distance_is_an_error(installer, monkeypatch):
    with pytest.raises(KeyError, match="Missing routed distance for feeder node 2"):
        size(installer, monkeypatch, CHAIN, {1: 100.0}, {1: 0.05, 2: 0.10}, [1, 2])


def test_a_load_outside_the_feeder_tree_is_an_error(installer, monkeypatch):
    with pytest.raises(ValueError, match="absent from the finalized feeder tree: \\[9\\]"):
        size(installer, monkeypatch, CHAIN, {1: 100.0, 2: 300.0}, {1: 0.05, 2: 0.10}, [1, 2, 9])


def test_a_consumer_in_two_planning_nodes_is_an_error(installer, monkeypatch):
    monkeypatch.setattr(utils, "CoincidentLoads", lambda buildings_df, consumer_df: KaPerNode({1: 0.05, 2: 0.10}))
    monkeypatch.setattr(utils, "design_current_ka", lambda load: load)
    twice = pd.DataFrame({"vertice_id": [100, 100], "connection_point": [1, 2]})
    with pytest.raises(ValueError, match="Consumer vertex 100 maps to multiple planning nodes"):
        feeder_planning.size_feeder_tree(installer, CHAIN, 0, {1: 100.0, 2: 300.0}, twice, None, LOGGER, 80803)


# --- the choice of the voltage upgrade -------------------------------------------------------------------------------
def option(cable, parallel, r, cost, q=150, x=0.08):
    return {"cable": cable, "parallel": parallel, "r_ohm_per_km": r, "x_ohm_per_km": x, "cost_eur_per_m": cost,
            "q_mm2": q}


def section(current, length_km=0.1):
    return {"selected": current, "length_km": length_km}


def best(designs, options, violations, factors):
    return feeder_planning._best_voltage_upgrade(designs, options, violations, factors)


def test_no_upgrade_when_no_option_lowers_the_impedance():
    current = option("A", 1, 0.2, 10)
    designs = {0: section(current)}
    options = {0: [option("A", 1, 0.2, 10), option("B", 1, 0.3, 5)]}   # equal and higher impedance
    assert best(designs, options, {5: 1.0}, {5: {0: 10.0}}) is None


def test_no_upgrade_for_a_section_that_is_on_no_violated_path():
    designs = {0: section(option("A", 1, 0.3, 10)), 1: section(option("A", 1, 0.3, 10))}
    options = {0: [], 1: [option("B", 1, 0.1, 12)]}
    assert best(designs, options, {5: 1.0}, {5: {0: 10.0}}) is None


def test_the_reduction_per_extra_euro_decides_not_the_largest_reduction():
    designs = {0: section(option("A", 1, 0.30, 10))}
    options = {0: [option("B", 1, 0.20, 12), option("C", 1, 0.10, 30)]}
    # B removes 0.95 for 200 EUR, C removes 1.9 for 2,000 EUR
    assert best(designs, options, {5: 5.0}, {5: {0: 10.0}}) == (0, options[0][0])
    # a cheap large upgrade wins: C removes 1.9 for 100 EUR
    options = {0: [option("B", 1, 0.20, 12), option("C", 1, 0.10, 11)]}
    assert best(designs, options, {5: 5.0}, {5: {0: 10.0}}) == (0, options[0][1])


def test_the_reduction_counts_only_up_to_the_excess_drop():
    designs = {0: section(option("A", 1, 0.30, 10))}
    # both options cost the same and remove more than the 0.5 excess: neither is better, the smaller cross-section wins
    options = {0: [option("B", 1, 0.20, 12, q=150), option("C", 1, 0.10, 12, q=185)]}
    assert best(designs, options, {5: 0.5}, {5: {0: 10.0}}) == (0, options[0][0])
    # below the cap the lower impedance does count
    assert best(designs, options, {5: 5.0}, {5: {0: 10.0}}) == (0, options[0][1])


def test_a_section_on_several_violated_paths_benefits_each_of_them():
    shared = option("A", 1, 0.30, 10)
    designs = {0: section(shared), 1: section(dict(shared))}
    options = {0: [option("B", 1, 0.20, 12)], 1: [option("B", 1, 0.20, 12)]}
    factors = {10: {0: 10.0, 1: 10.0}, 11: {0: 10.0}}
    section_id, _ = best(designs, options, {10: 100.0, 11: 100.0}, factors)
    assert section_id == 0


def test_equal_candidates_prefer_fewer_parallel_cables_then_the_smaller_cross_section(monkeypatch):
    monkeypatch.setattr(feeder_planning, "DEFAULT_POWER_FACTOR", 0.5)   # x = 0 below: exact arithmetic
    designs = {0: section(option("A", 1, 0.6, 5, x=0.0))}
    one, two = option("B", 1, 0.2, 10, x=0.0), option("C", 2, 0.4, 5, x=0.0)   # same impedance, same cost per metre
    assert best(designs, {0: [two, one]}, {5: 5.0}, {5: {0: 10.0}}) == (0, one)
    small, large = option("D", 1, 0.2, 10, q=150, x=0.0), option("E", 1, 0.2, 10, q=185, x=0.0)
    assert best(designs, {0: [large, small]}, {5: 5.0}, {5: {0: 10.0}}) == (0, small)


def test_an_option_that_is_not_dearer_costs_next_to_nothing_and_wins():
    designs = {0: section(option("A", 1, 0.30, 10))}
    cheaper = option("B", 1, 0.10, 8)    # lower impedance and cheaper cable: the extra cost is floored at 1e-9
    stronger = option("C", 1, 0.05, 12)
    assert best(designs, {0: [stronger, cheaper]}, {5: 5.0}, {5: {0: 10.0}}) == (0, cheaper)
