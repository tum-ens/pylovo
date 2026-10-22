"""HOUSE_CONNECTION: cabinet - cabinet placement, serving cabinets and house-cable paths (no database)."""

import logging

import pandas as pd

from pylovo import feeder_planning
from pylovo.cable_installer import CableInstaller
from pylovo.utils import design_current_ka
from pylovo.feeder_planning import FeederBranch

# Transformer 0; main street 0-1-2-3-4-5, side street 2-6-7 (distances in m from the station).
DISTANCE = {1: 40.0, 2: 90.0, 3: 150.0, 4: 210.0, 5: 260.0, 6: 130.0, 7: 170.0}
BRANCHES = [FeederBranch(index=0, nodes=[5, 4, 3, 2, 1], attachment_node=0),
            FeederBranch(index=1, nodes=[7, 6], attachment_node=2)]
TWO_EACH = {node: 2 for node in DISTANCE}   # two buildings at every connection node


def test_cabinets_at_splits_branch_ends_and_after_the_spacing():
    plan = feeder_planning.plan_cabinets(BRANCHES, 0, DISTANCE, 100.0, TWO_EACH)
    # 2 splits the feeder, 5 and 7 end it, 4 is the first node 100 m or more beyond the cabinet at 2
    assert plan.kind_by_cabinet == {2: "feeder_split_topology", 4: "added_cabinet",
                                    5: "end_cabinet", 7: "end_cabinet"}
    # each cabinet is attached to the next cabinet towards the station
    assert [(b.nodes, b.attachment_node) for b in plan.branches] == [([2], 0), ([7], 2), ([4], 2), ([5], 4)]


def test_every_node_is_served_from_the_nearer_cabinet_or_the_station():
    plan = feeder_planning.plan_cabinets(BRANCHES, 0, DISTANCE, 100.0, TWO_EACH)
    # node 1: station 40 m, cabinet 2 50 m; node 3: cabinets 2 and 4 both 60 m (tie: the one before);
    # node 6: cabinet 2 40 m, cabinet 7 40 m
    assert plan.cabinet_by_node == {1: 0, 2: 2, 3: 2, 4: 4, 5: 5, 6: 2, 7: 7}


def test_without_spacing_only_splits_and_ends_are_cabinets():
    plan = feeder_planning.plan_cabinets(BRANCHES, 0, DISTANCE, 9999, TWO_EACH)
    assert plan.kind_by_cabinet == {2: "feeder_split_topology", 5: "end_cabinet", 7: "end_cabinet"}
    assert plan.cabinet_by_node[4] == 5 and plan.cabinet_by_node[3] == 2   # 110 vs 60 m from cabinet 2: 3 -> 2


def test_a_single_building_branch_end_hangs_on_the_cabinet_before_it():
    plan = feeder_planning.plan_cabinets(BRANCHES, 0, DISTANCE, 100.0, TWO_EACH | {5: 1})
    # 5 (one building, 50 m beyond 4) gets no feeder: 4 now ends the main street
    assert plan.kind_by_cabinet == {2: "feeder_split_topology", 4: "end_cabinet", 7: "end_cabinet"}
    assert plan.cabinet_by_node[5] == 4 and plan.cabinet_by_node[3] == 2
    assert 5 not in [node for branch in plan.branches for node in branch.nodes]


def test_a_single_building_farther_than_the_spacing_keeps_its_feeder():
    plan = feeder_planning.plan_cabinets(BRANCHES, 0, DISTANCE, 45.0, TWO_EACH | {5: 1, 7: 1})
    # 5 is 50 m beyond 4 and keeps its cabinet; 7 is 40 m beyond 6 and hangs on 6's cabinet
    assert plan.kind_by_cabinet[5] == "end_cabinet" and 7 not in plan.kind_by_cabinet
    assert plan.cabinet_by_node[7] == plan.cabinet_by_node[6] == 6


def test_a_grid_of_single_building_ends_keeps_its_feeders():
    branches = [FeederBranch(index=0, nodes=[1], attachment_node=0), FeederBranch(index=1, nodes=[2], attachment_node=0)]
    plan = feeder_planning.plan_cabinets(branches, 0, {1: 30.0, 2: 50.0}, 100.0, {1: 1, 2: 1})
    assert plan.kind_by_cabinet == {1: "end_cabinet", 2: "end_cabinet"}


def test_buildings_are_planned_at_their_cabinet():
    plan = feeder_planning.plan_cabinets(BRANCHES, 0, DISTANCE, 100.0, TWO_EACH)
    buildings = pd.DataFrame({"vertice_id": [11, 13, 16], "connection_point": [1, 3, 6]})
    planned = feeder_planning.cabinet_buildings(buildings, plan.cabinet_by_node)
    assert planned["agg_connection_point"].tolist() == [0, 2, 2]
    assert "agg_connection_point" not in buildings.columns   # the original rows stay as they are


def test_cabinets_end_feeder_sections():
    children = {0: [1], 1: [2], 2: [3]}
    assert feeder_planning.group_edges_into_sections(children, 0) == {0: [(0, 1), (1, 2), (2, 3)]}
    assert feeder_planning.group_edges_into_sections(children, 0, [2]) == {0: [(0, 1), (1, 2)], 1: [(2, 3)]}


def test_a_house_cable_runs_from_its_cabinet_along_the_feeder():
    installer = CableInstaller.__new__(CableInstaller)
    installer._paths_to_transformer = {3: (3, 2, 1, 0), 4: (4, 3, 2, 1, 0), 2: (2, 1, 0)}
    installer.dbc = None
    assert installer._cabinet_path(4, 3) == [4, 3]     # cabinet behind the node
    assert installer._cabinet_path(2, 3) == [2, 3]     # cabinet before the node
    assert installer._cabinet_path(3, 3) == [3]        # the cabinet's own node


class _RecordingBackend:
    """Collects the created components; knows no bus coordinates."""

    def __init__(self):
        self.components = []

    def create_component(self, spec):
        self.components.append(spec)

    def get_bus_coordinates(self, name):
        return None


def _shared_cable_installer():
    # Transformer 0, cabinet 2 (50 m), connection node 3 (80 m) with buildings 31 and 32 about 11 m off the street.
    cables = [("NAYY_4_50", 0.642, 0.083, 0.142, 11.0), ("NAYY_4_70", 0.443, 0.082, 0.173, 16.0),
              ("NAYY_4_150", 0.208, 0.080, 0.270, 25.0)]
    coordinates = {0: (11.5, 48.5), 2: (11.5, 48.50045), 3: (11.5, 48.00072 + 0.5),
                   31: (11.5001, 48.50072), 32: (11.4999, 48.50072)}
    installer = CableInstaller(_RecordingBackend(), None, logging.getLogger("shared-house-cable-test"), cables,
                               pd.DataFrame({"name": ["NAYY_4_150"]}), pd.DataFrame({"name": ["NAYY_4_50", "NAYY_4_70"]}),
                               node_coordinates=coordinates, consumer_connection_mapping={3: [31, 32]},
                               paths_to_transformer={3: (3, 2, 0), 2: (2, 0)})
    installer._station_vertex = 0
    return installer


def _install(installer, shared_loads):
    material = {"NAYY_4_50": 0.0, "NAYY_4_70": 0.0, "NAYY_4_150": 0.0}
    _, diagnostics = installer.install_cabinet_service_cables(
        1, 1, 1, {2: 2, 3: 2}, 0, {2: 50.0, 3: 80.0, 31: 91.0, 32: 91.0}, {31: 10.0, 32: 10.0}, material,
        {2: 1.0}, shared_loads)
    lines = {spec.name: spec for spec in installer.backend.components if spec.component_type == "line"}
    return lines, diagnostics, material


def test_buildings_at_one_node_share_the_house_cable_from_their_cabinet():
    lines, diagnostics, material = _install(_shared_cable_installer(), {3: 18.0})
    shared = lines["Line to joint 3"]
    assert (shared.bus1, shared.bus2) == ("Connection Nodebus 2", "Connection Nodebus 3")
    assert abs(shared.length_km - 0.030) < 1e-9          # cabinet at 50 m to the joint at 80 m
    for consumer in (31, 32):
        line = lines[f"Line to {consumer}"]
        assert line.bus1 == "Connection Nodebus 3" and line.length_km < 0.012   # own cable from the joint only
    # the drop limit and the diagnostics cover the shared cable as well; the feeder drop is the cabinet's
    assert shared.service_sizing_basis is None    # service annotations mark the cable of one building
    shared_drop = _shared_cable_installer()._service_voltage_drop_percent(
        design_current_ka(18.0), shared.length_km, shared.cable_name, shared.parallel)
    assert shared_drop > 0
    for row in diagnostics:
        assert row["selected_drop_percent"] > shared_drop
        assert abs(row["total_design_drop_percent"] - (1.0 + row["selected_drop_percent"])) < 1e-9
    assert abs(sum(material.values()) - (0.030 + sum(lines[f"Line to {c}"].length_km for c in (31, 32)))) < 1e-9


def test_without_shared_loads_every_building_keeps_its_own_cable_from_the_cabinet():
    lines, diagnostics, _ = _install(_shared_cable_installer(), None)
    assert "Line to joint 3" not in lines
    assert {lines[f"Line to {c}"].bus1 for c in (31, 32)} == {"Connection Nodebus 2"}
    assert all(lines[f"Line to {c}"].length_km > 0.030 for c in (31, 32))
