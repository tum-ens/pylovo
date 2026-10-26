"""Normally-open ties between the rings of neighbouring MV districts, without a database: tie selection on
synthetic street graphs, the takeover check on small pandapower nets, the summaries, the persistence with a fake
cursor, the post step of the stage and the tie handling of the metrics."""
from __future__ import annotations

import dataclasses

import json
import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pandapower as pp
import pytest
import shapely

import pylovo.database.mv_result_mixin as mixin
from pylovo.mv_grid import interconnection as ic
from pylovo.mv_grid import metrics, stage
from pylovo.mv_grid.model import EdgeSet, links_line
from pylovo.mv_grid.parameters import MvParameters

#: Ties along the routing graph, overhead where they run on OSM MV lines (test_osm_ties_are_overhead_on_osm_lines).
PARAMS = MvParameters(variant_id="y0")


# ---------------------------------------------------------------------------------------------- synthetic inputs
def street(xs, y=0.0, first_vertex=1, first_edge=1):
    """A straight street along x through the given vertex positions: (vertex ids, vertex xy, edges)."""
    xs = np.asarray(xs, dtype=float)
    vid = np.arange(first_vertex, first_vertex + len(xs))
    eid = np.arange(first_edge, first_edge + len(xs) - 1)
    return vid, np.column_stack([xs, np.full(len(xs), y)]), (eid, vid[:-1], vid[1:], np.diff(xs))


def edge_set(*streets, links=()) -> EdgeSet:
    """Edge set of streets plus extra links ``(edge_id, source, target, length_m)``."""
    vid = np.concatenate([s[0] for s in streets])
    xy = np.vstack([s[1] for s in streets])
    parts = [s[2] for s in streets] + [tuple(np.array([v]) for v in link) for link in links]
    eid, src, dst, length = (np.concatenate([np.asarray(p[k]) for p in parts]) for k in range(4))
    n = len(eid)
    return EdgeSet(edge_id=eid.astype(np.int64), source=src.astype(np.int64), target=dst.astype(np.int64),
                   length_m=length.astype(float), in_settlement=np.zeros(n, bool), settlement_id=np.full(n, -1),
                   vertex_id=vid.astype(np.int64), vertex_xy=xy)


LINE = edge_set(street(np.arange(0.0, 6001.0, 500.0)))      # one street, 0 .. 6 km, edges 1..12


def node(nid, grid, ring, x, *, feeder=None, kind="station", connection="ring", ring_kind="ring", offset=0.0,
         area="rural", settlement=np.nan, y=None, edge=None, fraction=None, degree=None, settlement_type=None):
    """A node row on LINE (or on a given edge) in the form of ``MvResultMixin.tie_candidate_nodes`` (``degree``:
    2 for ring members, 1 for stub nodes, unless given; no ``settlement_type``: the urban link reach of 500 m)."""
    if edge is None:
        edge = int(min(x // 500.0, 11)) + 1
        fraction = (x - 500.0 * (edge - 1)) / 500.0
    return {"mv_node_id": nid, "mv_grid_id": grid, "kind": kind, "connection": connection, "ring_id": ring,
            "ring_kind": ring_kind, "feeder_id": 2 * ring if feeder is None else feeder, "x": float(x),
            "y": float(offset if y is None else y), "attach_edge_id": edge, "attach_fraction": fraction,
            "attach_offset_m": float(offset), "settlement_id": settlement, "area_class": area,
            "degree": (1 if connection == "stub" else 2) if degree is None else degree,
            "settlement_type": settlement_type}


def nodes_frame(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows)).set_index("mv_node_id")


def grids_frame(*rows) -> pd.DataFrame:
    """Rows ``(mv_grid_id, vn_kv[, status, complete, in_region])``."""
    out = []
    for row in rows:
        g, vn, status, complete, in_region = (tuple(row) + ("done", True, True))[:5]
        out.append({"mv_grid_id": g, "vn_kv": vn, "status": status, "complete": complete, "in_region": in_region,
                    "n_stations": 10, "n_customers": 2, "n_rings": 3, "circuit_km": 20.0})
    return pd.DataFrame(out).set_index("mv_grid_id")


def pairs_of(ties: pd.DataFrame) -> list[tuple[int, int]]:
    return list(zip(ties["from_node"].astype(int), ties["to_node"].astype(int)))


# ---------------------------------------------------------------------------------------------- selection
def test_the_limit_applies_to_the_street_distance():
    grids = grids_frame((1, 20.0), (2, 20.0))
    near = ic.plan_ties(nodes_frame(node(11, 1, 0, 1000.0), node(21, 2, 0, 2900.0)), grids, LINE, PARAMS)
    assert pairs_of(near) == [(11, 21)] and near.loc[0, "street_m"] == pytest.approx(1900.0)
    far = ic.plan_ties(nodes_frame(node(11, 1, 0, 1000.0), node(21, 2, 0, 3100.0)), grids, LINE, PARAMS)
    assert far.empty and list(far.columns) == list(ic.TIE_COLUMNS)
    # connectors count: 1,900 m along the street plus 150 m off it is beyond 2 km
    off = ic.plan_ties(nodes_frame(node(11, 1, 0, 1000.0), node(21, 2, 0, 2900.0, offset=150.0)), grids, LINE,
                       PARAMS)
    assert off.empty
    # 300 m apart in a straight line, but 4.3 km along the streets (two parallel streets joined at x = 0)
    upper = street([0.0, 1000.0, 2000.0], y=300.0, first_vertex=101, first_edge=101)
    lower = street([0.0, 1000.0, 2000.0], y=0.0, first_vertex=201, first_edge=201)
    u_turn = edge_set(upper, lower, links=[(301, 101, 201, 300.0)])
    rows = nodes_frame(node(11, 1, 0, 2000.0, y=300.0, edge=102, fraction=1.0),
                       node(21, 2, 0, 2000.0, y=0.0, edge=202, fraction=1.0))
    assert ic.plan_ties(rows, grids, u_turn, PARAMS).empty
    assert pairs_of(ic.plan_ties(rows, grids, u_turn, PARAMS.replace(tie_max_length_m=5000.0))) == [(11, 21)]


def test_at_most_one_tie_per_ring_greedy_by_length():
    grids = grids_frame((1, 20.0), (2, 20.0), (3, 20.0))
    rows = nodes_frame(
        node(11, 1, 0, 1000.0), node(12, 1, 0, 1200.0),          # ring (1, 0): two members
        node(21, 2, 0, 2000.0),                                   # 800 m from node 12
        node(22, 2, 1, 2100.0),                                   # ring (2, 1): next to ring (2, 0)
        node(31, 3, 0, 3500.0),                                   # 1.4 km from 22, 1.5 km from 21
    )
    ties = ic.plan_ties(rows, grids, LINE, PARAMS)
    assert pairs_of(ties) == [(12, 21), (22, 31)]
    rings = list(zip(ties["from_grid"], ties["from_ring"])) + list(zip(ties["to_grid"], ties["to_ring"]))
    assert len(rings) == len(set(rings))                         # every ring at most once
    assert ties["length_m"].tolist() == pytest.approx([800.0, 1400.0])


def test_only_switchgear_ring_members_of_done_open_rings_hold_ties():
    grids = grids_frame((1, 20.0), (2, 20.0), (4, 20.0, "failed"))
    closer = [                                                    # each of them 100 m from node 21
        node(12, 1, 0, 1900.0, kind="joint", connection="joint"),
        node(13, 1, 0, 1900.0, kind="generator", connection="loop_in"),
        node(14, 1, 0, 1900.0, connection="stub"),
        node(15, 1, 1, 1900.0, kind="customer", connection="dedicated", ring_kind="dedicated"),
        node(16, 1, 2, 1900.0, ring_kind="radial"),
        node(41, 4, 0, 1900.0),                                   # failed district
    ]
    rows = nodes_frame(node(11, 1, 0, 1000.0, kind="customer"), *closer, node(21, 2, 0, 2000.0))
    ties = ic.plan_ties(rows, grids, LINE, PARAMS)
    assert pairs_of(ties) == [(11, 21)]
    kinds = rows.loc[[11, 21], "kind"].tolist()
    assert set(kinds) <= set(ic.TIE_NODE_KINDS)
    assert ic.tie_candidates(rows, grids).index.tolist() == [11, 21]


# ---------------------------------------------------------------------------------------------- links
def test_links_join_rings_of_one_district_one_per_member_two_per_ring_pair():
    grids = grids_frame((1, 20.0), (2, 20.0))
    rows = nodes_frame(
        node(11, 1, 0, 1000.0), node(12, 1, 0, 2000.0), node(13, 1, 0, 3000.0),     # ring 0
        node(21, 1, 1, 1100.0), node(22, 1, 1, 2150.0), node(23, 1, 1, 3200.0),     # ring 1, 100-200 m away
        node(31, 1, 2, 1020.0),                                                     # ring 2, 20 m from node 11
        node(41, 2, 0, 3250.0),                                                     # another district
    )
    links = ic.plan_links(rows, grids, LINE, PARAMS)
    # shortest first: 11-31 (20 m); 11 is then taken, so ring 0 meets ring 1 at 12-22 and 13-23 (two links)
    assert pairs_of(links) == [(11, 31), (12, 22), (13, 23)]
    assert links["length_m"].tolist() == pytest.approx([20.0, 150.0, 200.0])
    assert (links["from_grid"] == links["to_grid"]).all() and links["flags"].map(len).eq(0).all()
    # 21 is free but ring 0 and ring 1 already have two links; node 41 is in another district (a tie, not a link)
    assert 21 not in set(links["to_node"]) | set(links["from_node"]) and 41 not in set(links["to_node"])
    short = ic.plan_links(rows, grids, LINE, PARAMS.replace(link_max_length_m=100.0))
    assert pairs_of(short) == [(11, 31)]
    summary = ic.link_summary(links, grids, PARAMS)
    assert summary["n_links"] == 3 and summary["link_km"] == pytest.approx(0.37)
    assert summary["n_ring_pairs"] == 2 and summary["n_ring_pairs_closed"] == 1
    assert summary["n_rings_with_link"] == 3 and summary["n_nodes_with_link"] == 6
    assert summary["node_share_with_link"] == pytest.approx(6 / 24)                 # 2 districts x 12 supply points
    json.dumps(summary)
    assert "3 normally-open links between rings" in ic.describe_links(summary)
    assert ic.plan_links(nodes_frame(node(11, 1, 0, 1000.0), node(12, 1, 0, 1100.0)), grids, LINE, PARAMS).empty


def stub_rows():
    """One district: ring 0 (feeders 0 and 1) and ring 1 (feeders 2 and 3) with ring members and stub nodes."""
    return nodes_frame(
        node(11, 1, 0, 1000.0), node(12, 1, 0, 1500.0),                             # ring 0, feeder 0
        node(41, 1, 0, 1300.0, feeder=1),                                            # ring 0, feeder 1
        node(31, 1, 1, 1050.0),                                                      # ring 1, feeder 2
        node(13, 1, 0, 1100.0, connection="stub"),                                   # stub end on feeder 0
        node(14, 1, 1, 1600.0, connection="stub", degree=2),                         # stub node with a child
        node(15, 1, 1, 3000.0, feeder=3, connection="stub"),                         # stub end on feeder 3
        node(16, 1, 0, 3100.0, feeder=1, connection="stub", kind="customer"),        # stub end on feeder 1
    )


def test_links_from_stub_ends_take_the_nearest_free_node_of_another_feeder():
    grids = grids_frame((1, 20.0))
    rows = stub_rows()
    cand = ic.stub_link_candidates(rows, grids)
    assert cand.index.tolist() == [11, 12, 13, 14, 15, 16, 31, 41]
    assert cand.index[cand["stub_end"]].tolist() == [13, 15, 16]
    assert ic.tie_candidates(rows, grids).index.tolist() == [11, 12, 31, 41]       # ties keep the ring members
    # off: the ring members 11 and 31 (50 m) take the only link between rings
    off = PARAMS.replace(link_stub_ends=False)
    assert pairs_of(ic.plan_links(rows, grids, LINE, off)) == [(11, 31)]
    assert PARAMS.link_stub_ends                                                    # on by default
    links = ic.plan_links(rows, grids, LINE, PARAMS)
    # stub end 13: 31 (50 m) holds its link already and 11, 12 are on its own feeder, so 41 on the other half of
    # ring 0 (200 m); the stub ends 15 and 16 of different feeders join each other (100 m); node 14 has a child
    # and no link
    assert pairs_of(links) == [(11, 31), (13, 41), (16, 15)]
    assert links["length_m"].tolist() == pytest.approx([50.0, 200.0, 100.0])
    assert (links["from_grid"] == links["to_grid"]).all()
    summary = ic.link_summary(links, grids, PARAMS, stub_ends=[13, 15, 16])
    assert summary["link_stub_ends"] is True and summary["n_stub_ends"] == 3
    assert summary["n_stub_end_links"] == 2 and summary["n_stub_ends_with_link"] == 3
    assert summary["stub_end_share_with_link"] == pytest.approx(1.0)
    assert summary["n_ring_pairs"] == 1 and summary["n_ring_pairs_closed"] == 1     # 13-41 joins one ring
    json.dumps(summary)
    text = ic.describe_links(summary)
    assert text.startswith("3 normally-open links inside the districts") and "with a link" in text
    assert text.endswith("2 of them from stub ends (3 of 3 stub ends with a second infeed)")
    assert links_line(summary).endswith("2 of them from stub ends (3 of 3 stub ends with a second infeed)")
    plain = ic.link_summary(links.iloc[:1], grids, off)
    assert "n_stub_ends" not in plain and plain["link_stub_ends"] is False
    assert "third cable" in links_line(plain) and "stub ends" not in links_line(plain)


def test_the_link_reach_follows_the_settlement_type_of_both_ends():
    grids = grids_frame((1, 20.0))
    types = nodes_frame(*(node(n, 1, 0, 0.0, settlement_type=t) for n, t in ((1, 1), (2, 2), (3, 3), (4, None))))
    assert ic.link_reach(types, PARAMS).tolist() == [1000.0, 750.0, 500.0, 500.0]   # no type: the urban reach
    assert ic.largest_reach(PARAMS) == 1000.0
    rows = nodes_frame(node(11, 1, 0, 1000.0, settlement_type=1), node(21, 1, 1, 1800.0, settlement_type=1),
                       node(12, 1, 0, 3000.0, settlement_type=1), node(22, 1, 1, 3800.0, settlement_type=3))
    # 800 m each: rural to rural is within 1 km, rural to urban takes the urban 500 m
    assert pairs_of(ic.plan_links(rows, grids, LINE, PARAMS)) == [(11, 21)]
    one = PARAMS.replace(link_max_length_m=900.0)                                    # one number for every type
    assert pairs_of(ic.plan_links(rows, grids, LINE, one)) == [(11, 21), (12, 22)]
    summary = ic.link_summary(ic.empty_ties(), grids, PARAMS)
    assert summary["link_max_length_m"] == {"rural": 1000.0, "semi_urban": 750.0, "urban": 500.0}
    assert ic.describe_links(summary).endswith("within 1,000/750/500 m (rural/semi-urban/urban)")
    assert links_line({"n_links": 0, "link_max_length_m": 500.0}).endswith("within 500 m)")         # older summary


def test_links_from_stub_ends_stay_within_the_limit_and_the_district():
    grids = grids_frame((1, 20.0), (2, 20.0))
    on = PARAMS
    rows = nodes_frame(node(13, 1, 0, 1000.0, connection="stub"), node(21, 1, 1, 1600.0),     # 600 m
                       node(22, 2, 0, 1100.0))                                                # another district
    assert ic.plan_links(rows, grids, LINE, on).empty
    assert pairs_of(ic.plan_links(rows, grids, LINE, on.replace(link_max_length_m=700.0))) == [(13, 21)]
    # two stub nodes with children on different feeders do not take a link
    rows = nodes_frame(node(13, 1, 0, 1000.0, connection="stub", degree=2),
                       node(14, 1, 1, 1100.0, connection="stub", degree=3))
    assert ic.plan_links(rows, grids, LINE, on).empty


def test_no_ties_within_a_district_or_between_voltages():
    rows = nodes_frame(node(11, 1, 0, 1000.0), node(12, 1, 1, 1100.0), node(31, 3, 0, 1200.0))
    assert ic.plan_ties(rows, grids_frame((1, 20.0), (3, 10.0)), LINE, PARAMS).empty
    assert pairs_of(ic.plan_ties(rows, grids_frame((1, 20.0), (3, 20.0)), LINE, PARAMS)) == [(12, 31)]


def test_ties_are_deterministic():
    grids = grids_frame((1, 20.0), (2, 20.0), (3, 20.0))
    rows = nodes_frame(node(11, 1, 0, 1000.0), node(12, 1, 1, 1500.0), node(21, 2, 0, 2000.0),
                       node(22, 2, 1, 2000.0), node(31, 3, 0, 2900.0), node(32, 3, 1, 2500.0))
    first = ic.plan_ties(rows, grids, LINE, PARAMS)
    shuffled = ic.plan_ties(rows.sample(frac=1.0, random_state=3), grids, LINE, PARAMS)
    again = ic.plan_ties(rows, grids, LINE, PARAMS)
    for other in (shuffled, again):
        pd.testing.assert_frame_equal(first.drop(columns="geometry"), other.drop(columns="geometry"))
        assert [g.wkb for g in first["geometry"]] == [g.wkb for g in other["geometry"]]
    # equal lengths are broken by the node ids: 21 and 22 lie on the same point, 500 m from 12; 21 wins
    assert pairs_of(first) == [(11, 31), (12, 21), (22, 32)]


def test_ties_follow_the_streets_as_cables_without_osm_lines():
    grids = grids_frame((1, 20.0), (2, 20.0))
    # a detour street: up 400 m, along 1,000 m, down 400 m (1.8 km) between points 1 km apart
    vid = np.array([1, 2, 3, 4])
    xy = np.array([[0.0, 0.0], [0.0, 400.0], [1000.0, 400.0], [1000.0, 0.0]])
    detour = EdgeSet(edge_id=np.array([1, 2, 3]), source=np.array([1, 2, 3]), target=np.array([2, 3, 4]),
                     length_m=np.array([400.0, 1000.0, 400.0]), in_settlement=np.zeros(3, bool),
                     settlement_id=np.full(3, -1), vertex_id=vid, vertex_xy=xy)
    a = node(11, 1, 0, 0.0, y=0.0, edge=1, fraction=0.0)
    b = node(21, 2, 0, 1000.0, y=0.0, edge=3, fraction=1.0)
    tie = ic.plan_ties(nodes_frame(a, b), grids, detour, PARAMS).iloc[0]
    assert tie["kind"] == "cable" and tie["length_m"] == pytest.approx(1800.0)
    assert tie["std_type"] == PARAMS.cable.name and tie["geometry"].length == pytest.approx(1800.0)
    assert tie["straight_m"] == pytest.approx(1000.0) and tie["overhead_m"] == 0.0


def test_osm_ties_are_overhead_on_osm_lines():
    grids = grids_frame((1, 20.0), (2, 20.0))
    vid = np.array([1, 2, 3, 4])
    xy = np.array([[0.0, 0.0], [0.0, 400.0], [1000.0, 400.0], [1000.0, 0.0]])
    detour = EdgeSet(edge_id=np.array([1, 2, 3]), source=np.array([1, 2, 3]), target=np.array([2, 3, 4]),
                     length_m=np.array([400.0, 1000.0, 400.0]), in_settlement=np.zeros(3, bool),
                     settlement_id=np.full(3, -1), vertex_id=vid, vertex_xy=xy,
                     overhead=np.array([False, True, False]))                 # the middle edge is an OSM MV line
    a = node(11, 1, 0, 0.0, y=0.0, edge=1, fraction=0.0)
    b = node(21, 2, 0, 1000.0, y=0.0, edge=3, fraction=1.0)
    osm = PARAMS
    tie = ic.plan_ties(nodes_frame(a, b), grids, detour, osm).iloc[0]
    assert tie["kind"] == "mixed" and tie["length_m"] == pytest.approx(1800.0)
    assert tie["overhead_m"] == pytest.approx(1000.0) and tie["cable_m"] == pytest.approx(800.0)
    assert tie["std_type"] == f"{osm.cable.name} + {osm.overhead.name}"
    streets = dataclasses.replace(detour, overhead=None)
    assert ic.plan_ties(nodes_frame(a, b), grids, streets, osm).iloc[0]["kind"] == "cable"


def test_the_limit_applies_to_the_true_length_on_osm_lines():
    grids = grids_frame((1, 20.0), (2, 20.0))
    vid = np.array([1, 2, 3, 4])
    xy = np.array([[0.0, 0.0], [0.0, 400.0], [1600.0, 400.0], [1600.0, 0.0]])
    corridor = EdgeSet(edge_id=np.array([1, 2, 3]), source=np.array([1, 2, 3]), target=np.array([2, 3, 4]),
                       length_m=np.array([400.0, 1600.0, 400.0]), in_settlement=np.zeros(3, bool),
                       settlement_id=np.full(3, -1), vertex_id=vid, vertex_xy=xy,
                       overhead=np.array([False, True, False]))               # the middle edge is an OSM MV line
    a = node(11, 1, 0, 0.0, y=0.0, edge=1, fraction=0.0)
    b = node(21, 2, 0, 1600.0, y=0.0, edge=3, fraction=1.0)
    osm = PARAMS
    # 2,400 m long, but only 1,920 m in costs at the OSM factor of 0.7: beyond the 2 km limit
    assert ic.plan_ties(nodes_frame(a, b), grids, corridor, osm).empty
    tie = ic.plan_ties(nodes_frame(a, b), grids, corridor, osm.replace(tie_max_length_m=2500.0)).iloc[0]
    assert tie["length_m"] == pytest.approx(2400.0) and tie["street_m"] == pytest.approx(2400.0)


def _corridor_and_street(osm_m: float, street_m: float) -> EdgeSet:
    """A detour of 2 x (osm_m / 6) street and 2/3 osm_m on an OSM MV line between vertices 1 and 4, and a direct
    street of ``street_m`` that costs more than the OSM detour at the OSM factor of 0.7."""
    leg, top = osm_m / 6.0, 2.0 * osm_m / 3.0
    xy = np.array([[0.0, 0.0], [0.0, leg], [top, leg], [top, 0.0]])
    return EdgeSet(edge_id=np.array([1, 2, 3, 4]), source=np.array([1, 2, 3, 1]), target=np.array([2, 3, 4, 4]),
                   length_m=np.array([leg, top, leg, street_m]), in_settlement=np.zeros(4, bool),
                   settlement_id=np.full(4, -1), vertex_id=np.array([1, 2, 3, 4]), vertex_xy=xy,
                   overhead=np.array([False, True, False, False]))


def test_a_pair_beyond_the_reach_on_the_cheapest_route_takes_the_shortest_route_within_it():
    """The cheapest route runs on the OSM MV line, but its true length is beyond the reach; the direct street costs
    more, but lies within the reach: the tie or link takes the street. Within the reach, the cheapest route stays."""
    grids = grids_frame((1, 20.0), (2, 20.0))
    both = _corridor_and_street(2400.0, 1950.0)               # OSM route: 2,400 m (1,920 m of cost)
    a = node(11, 1, 0, 0.0, y=0.0, edge=1, fraction=0.0)
    b = node(21, 2, 0, 1600.0, y=0.0, edge=3, fraction=1.0)
    tie = ic.plan_ties(nodes_frame(a, b), grids, both, PARAMS).iloc[0]
    assert tie["kind"] == "cable" and tie["length_m"] == pytest.approx(1950.0) and tie["overhead_m"] == 0.0
    assert tie["street_m"] == pytest.approx(1950.0)
    tie = ic.plan_ties(nodes_frame(a, b), grids, both, PARAMS.replace(tie_max_length_m=2500.0)).iloc[0]
    assert tie["kind"] == "mixed" and tie["length_m"] == pytest.approx(2400.0)
    # a link between two rural rings (reach 1 km): OSM route 1,200 m (960 m of cost), street 980 m
    both = _corridor_and_street(1200.0, 980.0)
    rows = nodes_frame(node(11, 1, 0, 0.0, y=0.0, edge=1, fraction=0.0, settlement_type=1),
                       node(21, 1, 1, 800.0, y=0.0, edge=3, fraction=1.0, settlement_type=1))
    link = ic.plan_links(rows, grids_frame((1, 20.0)), both, PARAMS).iloc[0]
    assert (link["from_node"], link["to_node"]) == (11, 21) and link["length_m"] == pytest.approx(980.0)
    assert link["kind"] == "cable"


def test_orientation_geometry_and_flags():
    grids = grids_frame((5, 20.0, "done", False, True), (2, 20.0, "done", True, False))
    rows = nodes_frame(node(51, 5, 0, 1000.0, offset=30.0), node(21, 2, 0, 2200.0, offset=20.0))
    tie = ic.plan_ties(rows, grids, LINE, PARAMS).iloc[0]
    assert (tie["from_grid"], tie["from_node"], tie["to_grid"], tie["to_node"]) == (2, 21, 5, 51)   # lower grid id
    assert tie["street_m"] == pytest.approx(1200.0 + 30.0 + 20.0)
    coords = list(tie["geometry"].coords)
    assert coords[0] == (2200.0, 20.0) and coords[-1] == (1000.0, 30.0)       # starts at from_node, ends at to_node
    assert tie["geometry"].length == pytest.approx(tie["length_m"])
    assert tie["flags"] == ["incomplete_district", "substation_outside_region"]
    clean = ic.plan_ties(rows, grids_frame((5, 20.0), (2, 20.0)), LINE, PARAMS).iloc[0]
    assert clean["flags"] == []


def test_edge_geometries_are_used_for_street_ties():
    grids = grids_frame((1, 20.0), (2, 20.0))
    rows = nodes_frame(node(11, 1, 0, 1000.0), node(21, 2, 0, 1900.0))
    asked = []

    def bent(ids):                     # every edge bends 10 m to the north at its middle
        asked.extend(ids)
        out = {}
        for e in ids:
            x0 = 500.0 * (e - 1)
            out[e] = shapely.LineString([(x0, 0.0), (x0 + 250.0, 10.0), (x0 + 500.0, 0.0)])
        return out

    tie = ic.plan_ties(rows, grids, LINE, PARAMS, edge_geometry=bent).iloc[0]
    assert sorted(set(asked)) == [3, 4]
    assert max(y for _, y in tie["geometry"].coords) == pytest.approx(10.0)
    assert tie["geometry"].coords[0] == (1000.0, 0.0) and tie["geometry"].coords[-1] == (1900.0, 0.0)


# ---------------------------------------------------------------------------------------------- takeover
def ring_net(grid: int, base: int, load_mw: float, length_km: float = 1.0, vn_kv: float = 20.0) -> pp.pandapowerNet:
    """A stored-net-like district: busbar ``base`` and one open ring busbar - base+1 - base+2 | base+3 - busbar
    (feeder 0: base+1, base+2 from exit a; feeder 1: base+3 from exit b; the NOP open at base+3)."""
    net = pp.create_empty_network()
    buses = [pp.create_bus(net, vn_kv, name=f"MV node {base + i}") for i in range(4)]
    net.bus["kind"] = ["busbar", "station", "station", "station"]
    net.bus["mv_node_id"] = [float(base + i) for i in range(4)]
    net.bus["node_key"] = [base + i for i in range(4)]
    pp.create_ext_grid(net, buses[0], vm_pu=1.0)
    sections = [(0, 1, 0, "a"), (1, 2, 0, None), (2, 3, 1, None), (3, 0, 1, "b")]
    for a, b, feeder, exit_ in sections:
        pp.create_line_from_parameters(net, buses[a], buses[b], length_km, r_ohm_per_km=0.122, x_ohm_per_km=0.112,
                                       c_nf_per_km=304.0, max_i_ka=0.421)
    net.line["ring_id"] = 0.0
    net.line["feeder_id"] = [float(s[2]) for s in sections]
    net.line["exit"] = [s[3] for s in sections]
    net.line["role"] = "ring"
    pp.create_switch(net, buses[3], 2, et="l", closed=False)
    net.switch["normally_open"] = True
    for b in buses[1:]:
        pp.create_load(net, b, p_mw=load_mw, q_mvar=0.33 * load_mw)
    return net


def tie_row(**changes) -> dict:
    row = {"from_node": 102, "to_node": 202, "from_grid": 1, "to_grid": 2, "from_ring": 0, "to_ring": 0,
           "from_feeder": 0, "to_feeder": 0, "kind": "cable", "length_m": 800.0,
           "street_m": 800.0, "straight_m": 700.0, "cable_m": 800.0, "overhead_m": 0.0, "parallel": 1,
           "std_type": PARAMS.cable.name, "flags": [], "geometry": shapely.LineString([(0, 0), (700, 0)])}
    row.update(changes)
    return row


def test_takeover_of_the_half_ring_through_the_tie():
    ties = pd.DataFrame([tie_row()], columns=list(ic.TIE_COLUMNS))
    light = ic.takeover_checks(ties, {1: ring_net(1, 100, 0.5), 2: ring_net(2, 200, 0.5)}, PARAMS)
    assert light[["side", "mv_grid_id", "node", "partner_grid_id"]].values.tolist() == [
        ["from", 1, 102, 2], ["to", 2, 202, 1]]
    assert light["converged"].all() and light["ok"].all() and light["error"].isna().all()
    # half A (2 x 0.5 MW) through B's exit a (1 km), B's 101-102 section ... about 4.6 km from busbar B
    assert 0.0 < light.loc[0, "drop_percent"] < 1.0 and 0.0 < light.loc[0, "loading_percent"] < 30.0
    heavy = ic.takeover_checks(ties, {1: ring_net(1, 100, 4.0), 2: ring_net(2, 200, 4.0)}, PARAMS)
    assert heavy["converged"].all() and not heavy["ok"].any()
    assert (heavy["loading_percent"] > 100.0).all()                # 16 MW through one 1x240 exit
    missing = ic.takeover_checks(ties, {1: ring_net(1, 100, 0.5)}, PARAMS)
    assert not missing["ok"].any() and missing["error"].str.contains("no stored net").all()
    unknown = ic.takeover_checks(pd.DataFrame([tie_row(from_feeder=7)], columns=list(ic.TIE_COLUMNS)),
                                 {1: ring_net(1, 100, 0.5), 2: ring_net(2, 200, 0.5)}, PARAMS)
    assert unknown.loc[0, "error"].startswith("LookupError") and unknown.loc[1, "ok"]


def test_ties_take_the_line_types_of_their_districts_voltage(monkeypatch):
    """MV_NOMINAL_VOLTAGE_KV auto: a tie between two 10 kV districts is a 10 kV cable, and its takeover check uses
    that type."""
    assert PARAMS.auto_voltage
    grids = grids_frame((1, 10.0), (2, 10.0))
    ten = ic.plan_ties(nodes_frame(node(11, 1, 0, 1000.0), node(21, 2, 0, 1800.0)), grids, LINE, PARAMS)
    assert ten.loc[0, "std_type"] == PARAMS.cable_at(10.0).name == "NA2XS2Y 1x240 RM/25 6/10 kV"
    twenty = ic.plan_ties(nodes_frame(node(11, 1, 0, 1000.0, area="urban"), node(21, 2, 0, 1800.0)),
                          grids_frame((1, 20.0), (2, 20.0)), LINE, PARAMS)
    assert twenty.loc[0, "std_type"] == PARAMS.cable_at(20.0).name
    used = []
    real = PARAMS.cable_at
    monkeypatch.setattr(type(PARAMS), "cable_at", lambda self, vn_kv=None: used.append(vn_kv) or real(vn_kv))
    ties = pd.DataFrame([tie_row(std_type=PARAMS.cable_at(10.0).name)], columns=list(ic.TIE_COLUMNS))
    used.clear()
    check = ic.takeover_checks(ties, {1: ring_net(1, 100, 0.2, vn_kv=10.0), 2: ring_net(2, 200, 0.2, vn_kv=10.0)},
                               PARAMS)
    assert used == [10.0] and check["converged"].all() and check["error"].isna().all()


def test_the_takeover_does_not_change_the_normal_flow():
    nets = {1: ring_net(1, 100, 0.5), 2: ring_net(2, 200, 0.5)}
    before = {g: net.line.copy() for g, net in nets.items()}
    ic.takeover_checks(pd.DataFrame([tie_row()], columns=list(ic.TIE_COLUMNS)), nets, PARAMS)
    for g, net in nets.items():                                    # the input nets are not changed
        pd.testing.assert_frame_equal(net.line, before[g])
        assert not net.switch["closed"].any()


# ---------------------------------------------------------------------------------------------- reporting
def test_grid_metrics_and_summary():
    ties = pd.DataFrame([tie_row(), tie_row(from_node=103, to_node=301, to_grid=3, from_ring=1, to_ring=0,
                                            kind="overhead", cable_m=0.0, overhead_m=1200.0, length_m=1200.0,
                                            flags=["incomplete_district"])], columns=list(ic.TIE_COLUMNS))
    takeover = pd.DataFrame([
        {"tie": 0, "side": "from", "mv_grid_id": 1, "ring_id": 0, "node": 102, "partner_grid_id": 2,
         "converged": True, "drop_percent": 3.0, "loading_percent": 40.0, "ok": True, "error": None},
        {"tie": 0, "side": "to", "mv_grid_id": 2, "ring_id": 0, "node": 202, "partner_grid_id": 1,
         "converged": True, "drop_percent": 12.0, "loading_percent": 50.0, "ok": False, "error": None},
        {"tie": 1, "side": "from", "mv_grid_id": 1, "ring_id": 1, "node": 103, "partner_grid_id": 3,
         "converged": False, "drop_percent": None, "loading_percent": None, "ok": False, "error": None},
        {"tie": 1, "side": "to", "mv_grid_id": 3, "ring_id": 0, "node": 301, "partner_grid_id": 1,
         "converged": False, "drop_percent": None, "loading_percent": None, "ok": False,
         "error": "LookupError: MV grid 3 has no stored net"},
    ], columns=list(ic.TAKEOVER_COLUMNS))
    per_grid = ic.grid_tie_metrics(ties, takeover)
    assert sorted(per_grid) == [1, 2, 3]
    assert per_grid[1]["n_ties"] == 2 and per_grid[1]["n_rings_with_tie"] == 2
    assert per_grid[1]["tie_km"] == pytest.approx(2.0)
    first = per_grid[2]["ties"][0]
    assert first["owner"] is False and first["open_here"] is True and first["partner_node"] == 102
    assert first["takeover"]["ok"] is False and first["takeover"]["drop_percent"] == pytest.approx(12.0)
    json.dumps(per_grid, allow_nan=False)
    grids = grids_frame((1, 20.0), (2, 20.0), (3, 20.0), (4, 20.0, "failed"))
    summary = ic.tie_summary(ties, grids, PARAMS, takeover, runtime_s=1.234)
    assert summary["n_ties"] == 2 and summary["tie_km"] == pytest.approx(2.0)
    assert summary["tie_cable_km"] == pytest.approx(0.8) and summary["tie_overhead_km"] == pytest.approx(1.2)
    assert summary["circuit_km"] == pytest.approx(60.0)
    assert summary["tie_share_of_circuit_km"] == pytest.approx(2 / 60, rel=1e-4)
    assert summary["n_rings"] == 9 and summary["n_rings_with_tie"] == 4
    assert summary["ring_share_with_tie"] == pytest.approx(4 / 9, rel=1e-4)
    assert summary["km_per_station"] == pytest.approx(2.0)
    assert summary["km_per_station_with_ties"] == pytest.approx(62.0 / 30, abs=1e-4)      # rounded to 4 digits
    assert summary["n_incomplete_district"] == 1 and summary["n_district_pairs"] == 2
    assert (summary["takeover_checked"], summary["takeover_ok"], summary["takeover_not_converged"],
            summary["takeover_errors"]) == (3, 1, 1, 1)
    assert summary["takeover_max_drop_percent"] == pytest.approx(12.0)
    json.dumps(summary, allow_nan=False)
    line = ic.describe_ties(summary)
    assert line.startswith("2 normally-open ties between districts: 2.0 km (3.3 % of 60 km MV circuits)")
    assert "4 of 9 rings (44 %) with a tie" in line and "1 of 3 tie ends" in line
    empty = ic.tie_summary(ic.empty_ties(), grids, PARAMS)
    assert empty["n_ties"] == 0 and empty["ring_share_with_tie"] == 0.0 and empty["takeover_checked"] == 0
    assert ic.describe_ties(empty).startswith("no normally-open ties")


def test_tie_inventory_counts_installed_parallel_circuits():
    ties = pd.DataFrame([tie_row(parallel=2)], columns=list(ic.TIE_COLUMNS))
    per_grid = ic.grid_tie_metrics(ties)
    assert per_grid[1]["tie_km"] == per_grid[2]["tie_km"] == pytest.approx(1.6)
    assert per_grid[1]["ties"][0]["length_km"] == pytest.approx(0.8)
    summary = ic.tie_summary(ties, grids_frame((1, 20.0), (2, 20.0)), PARAMS)
    assert summary["tie_km"] == pytest.approx(1.6)
    assert summary["tie_cable_km"] == pytest.approx(1.6)
    assert summary["tie_share_of_circuit_km"] == pytest.approx(1.6 / 40)


# ---------------------------------------------------------------------------------------------- persistence
class FakeCursor:
    def __init__(self):
        self.statements: list[tuple[str, object]] = []
        self.next_id = 500
        self._rows: list[tuple] = []
        self.rowcount = 1

    def execute(self, query, params=None):
        text = " ".join(str(query).split())
        self.statements.append((text, params))
        self._rows = []
        if "nextval(pg_get_serial_sequence" in text:
            self._rows = [(self.next_id + i,) for i in range(params[0])]
            self.next_id += params[0]

    def fetchall(self):
        return list(self._rows)


class FakeConnection:
    def __init__(self):
        self.commits = self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class Client(mixin.MvResultMixin):
    pass


def make_client(monkeypatch, fail_on=None):
    client = object.__new__(Client)
    client.cur, client.conn = FakeCursor(), FakeConnection()
    client.logger = logging.getLogger("mv-tie-test")
    calls = []

    def fake_execute_values(cur, query, values, template=None, page_size=100, fetch=False):
        text = " ".join(str(query).split())
        if fail_on and fail_on in text:
            raise RuntimeError("insert failed")
        cur.statements.append((text, "execute_values"))
        calls.append((text, list(values), template))

    monkeypatch.setattr(mixin, "execute_values", fake_execute_values)
    return client, calls


def test_save_ties_replaces_the_ties_of_the_variant(monkeypatch):
    client, calls = make_client(monkeypatch)
    ties = pd.DataFrame([tie_row(), tie_row(from_node=103, to_node=301, to_grid=3, from_ring=1)],
                        columns=list(ic.TIE_COLUMNS))
    metrics_by_grid = ic.grid_tie_metrics(ties)
    assert client.save_ties("mv1", "y1", ties, metrics_by_grid) == 2
    texts = [s for s, _ in client.cur.statements]
    assert texts[0].startswith("DELETE FROM pylovo.mv_line") and "role = %s" in texts[0]
    assert "SET has_tie = false" in texts[1] and "metrics - %s" in texts[2]
    params = [p for _, p in client.cur.statements[:3]]                               # the variant only
    assert params == [("mv1", "y1", "tie"), ("mv1", "y1"), ("ties", "mv1", "y1", "ties")]
    insert, values, template = calls[0]
    assert insert.startswith("INSERT INTO pylovo.mv_line") and "ST_GeomFromWKB" in template
    first = values[0]
    assert first[:11] == (500, "mv1", "y1", 1, 102, 202, "tie", "cable", PARAMS.cable.name, 1, 0.8)
    assert first[14:17] == (0.0, True, 202)                         # bridge_m, normally open, open at to_node
    assert [v[0] for v in values] == [500, 501]
    assert "SET has_tie = true" in calls[1][0] and calls[1][1] == [(1, 0), (1, 1), (2, 0), (3, 0)]
    assert "jsonb_build_object('ties'" in calls[2][0] and [g for g, _ in calls[2][1]] == [1, 2, 3]
    assert json.loads(calls[2][1][0][1])["n_ties"] == 2
    assert client.conn.commits == 1 and client.conn.rollbacks == 0
    # no ties: the old ones are still removed
    client, calls = make_client(monkeypatch)
    assert client.save_ties("mv1", "y1", ic.empty_ties(), {}) == 0
    assert len(client.cur.statements) == 3 and calls == [] and client.conn.commits == 1
    # an error rolls the whole replacement back
    client, _ = make_client(monkeypatch, fail_on="INSERT INTO pylovo.mv_line")
    with pytest.raises(RuntimeError):
        client.save_ties("mv1", "y1", ties, metrics_by_grid)
    assert client.conn.rollbacks == 1 and client.conn.commits == 0


def test_save_links_replaces_only_the_links_and_keeps_has_tie(monkeypatch):
    client, calls = make_client(monkeypatch)
    links = pd.DataFrame([tie_row(to_grid=1, to_ring=1)], columns=list(ic.TIE_COLUMNS))
    assert client.save_ties("mv1", "y1", links, ic.grid_link_metrics(links), role="link") == 1
    texts = [s for s, _ in client.cur.statements]
    assert not any("has_tie" in t for t in texts)                                # ties keep their ring flags
    assert [p for _, p in client.cur.statements[:2]] == [("mv1", "y1", "link"), ("links", "mv1", "y1", "links")]
    insert, values, _ = calls[0]
    assert values[0][6] == "link" and values[0][15] is True                     # role, normally open
    assert "jsonb_build_object('links'" in calls[1][0]
    entry = json.loads(calls[1][1][0][1])
    assert entry["n_links"] == 1 and entry["n_rings_with_link"] == 2 and entry["n_nodes_with_link"] == 2
    with pytest.raises(ValueError, match="role must be tie or link"):
        client.save_ties("mv1", "y1", links, {}, role="ring")


class TieDb:
    """The reads and the write of ``plan_and_save_ties`` on synthetic data."""

    def __init__(self, nodes, grids, edges, nets=None):
        self.nodes, self.grids, self.edges, self.nets = nodes, grids, edges, nets or {}
        self.calls: list[tuple] = []
        self.saved = None

    def tie_grids(self, version_id, variant_id):
        return self.grids

    def tie_candidate_nodes(self, version_id, variant_id):
        return self.nodes

    def subgraph_edges(self, graph_id, hull_wkb, buffer_m, must):
        self.calls.append(("subgraph", graph_id, len(shapely.from_wkb(hull_wkb).geoms), buffer_m, list(must)))
        return self.edges

    def edge_geometries(self, graph_id, ids):
        self.calls.append(("geometries", list(ids)))
        return {}

    def read_mv_grid_net(self, mv_grid_id):
        if mv_grid_id not in self.nets:
            raise LookupError(f"MV grid {mv_grid_id} has no stored net")
        return self.nets[mv_grid_id]

    def save_ties(self, version_id, variant_id, ties, grid_metrics):
        self.saved = (version_id, variant_id, ties, grid_metrics)
        return len(ties)


def test_plan_and_save_ties_reads_plans_checks_and_writes():
    rows = nodes_frame(node(102, 1, 0, 1000.0, feeder=0), node(101, 1, 0, 400.0, feeder=0),
                       node(202, 2, 0, 1800.0, feeder=0), node(301, 3, 0, 5800.0))
    db = TieDb(rows, grids_frame((1, 20.0), (2, 20.0), (3, 20.0)), LINE,
               nets={1: ring_net(1, 100, 0.5), 2: ring_net(2, 200, 0.5)})
    summary = ic.plan_and_save_ties(db, "mv1", "y1", "g1", PARAMS)
    _, _, ties, per_grid = db.saved
    assert pairs_of(ties) == [(102, 202)] and summary["n_ties"] == 1
    subgraph = db.calls[0]
    assert subgraph[:4] == ("subgraph", "g1", 3, PARAMS.tie_max_length_m + ic.SUBGRAPH_MARGIN_M)   # 301 is far
    assert summary["takeover_checked"] == 2 and summary["takeover_ok"] == 2
    assert per_grid[1]["ties"][0]["takeover"]["ok"] is True
    # nothing in reach: the old ties are still replaced (by none)
    db = TieDb(nodes_frame(node(11, 1, 0, 0.0), node(31, 3, 0, 6000.0)), grids_frame((1, 20.0), (3, 20.0)), LINE)
    summary = ic.plan_and_save_ties(db, "mv1", "y1", "g1", PARAMS)
    assert db.saved[2].empty and db.saved[3] == {} and summary["n_ties"] == 0 and db.calls == []


class LinkDb(TieDb):
    """The reads and the write of ``plan_and_save_links`` on synthetic data."""

    def save_ties(self, version_id, variant_id, ties, grid_metrics, role="tie"):
        self.saved = (version_id, variant_id, ties, grid_metrics, role)
        return len(ties)


def test_plan_and_save_links_reads_the_stub_nodes_with_the_option():
    db = LinkDb(stub_rows(), grids_frame((1, 20.0)), LINE)
    summary = ic.plan_and_save_links(db, "mv1", "y1", "g1", PARAMS.replace(link_stub_ends=False))
    assert pairs_of(db.saved[2]) == [(11, 31)] and db.saved[4] == "link" and "n_stub_ends" not in summary
    assert db.calls[0][2] == 4                                       # the subgraph around the ring members
    assert db.calls[0][3] == 1000.0 + ic.SUBGRAPH_MARGIN_M           # the longest reach
    db = LinkDb(stub_rows(), grids_frame((1, 20.0)), LINE)
    summary = ic.plan_and_save_links(db, "mv1", "y1", "g1", PARAMS)
    assert pairs_of(db.saved[2]) == [(11, 31), (13, 41), (16, 15)]
    assert db.calls[0][2] == 8                                       # ... and the stub nodes in reach
    assert summary["n_stub_ends"] == 3 and summary["n_stub_end_links"] == 2
    assert db.saved[3][1]["n_links"] == 3


# ---------------------------------------------------------------------------------------------- stage
class LvStub:
    """Stands in for ``LvVersionParameters`` (the stage only passes it on to the patched steps)."""

    @classmethod
    def from_generation_parameters(cls, version_id, snapshot):
        return cls()

    def as_json(self):
        return {}


class StageDb:
    """The calls of ``run_mv_stage`` up to the post step (every other method records its call)."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.cur = SimpleNamespace(execute=lambda *args, **kwargs: None)   # savepoints of optional reads

    def __getattr__(self, name):
        def method(*args, **kwargs):
            self.calls.append((name, args, kwargs))
        return method

    def try_lock_variant(self, version_id, variant_id):
        return True

    def check_mv_sources(self):
        return []

    def source_reader(self):
        return SimpleNamespace(where="the pylovo database", close=lambda: None)

    def generated_plz(self, version_id, plz):
        return [1]

    def variant_row(self, version_id, variant_id):
        return None

    def lv_snapshot(self, version_id):
        return {}

    def ensure_graph(self, region, params):
        return "g1"

    def register_variant(self, *args):
        return "new"

    def pending_grids(self, version_id, variant_id):
        return []

    def variant_totals(self, version_id, variant_id):
        return {"grids_total": 1, "grids_done": 1, "grids_failed": 0, "grids_pending": 0, "n_stations": 3,
                "n_customers": 0, "n_generators": 0, "circuit_km": 5.0, "cable_share": 1.0,
                "n_violation_grids": 0, "n_band_conflicts": 0}


def test_the_stage_stores_the_tie_summary(monkeypatch, tmp_path):
    db = StageDb()
    seen = []

    def fake_ties(dbc, version_id, variant_id, graph_id, params, logger=None):
        seen.append((version_id, variant_id, graph_id))
        return {"n_ties": 3, "tie_km": 2.5, "tie_max_length_m": 2000.0}

    monkeypatch.setattr(stage, "_database_client", lambda log_file: db)
    monkeypatch.setattr(stage, "LvVersionParameters", LvStub)              # no LV snapshot rules in this test
    monkeypatch.setattr(stage, "_region_step", lambda *a, **k: {})
    monkeypatch.setattr(stage, "_run_districts", lambda *a, **k: [])
    monkeypatch.setattr(ic, "plan_and_save_ties", fake_ties)
    monkeypatch.setattr(ic, "plan_and_save_links", lambda dbc, version_id, variant_id, graph_id, params, logger=None: {
        "n_links": 4, "link_km": 1.2, "n_rings": 6, "n_rings_with_link": 4, "node_share_with_link": 0.1,
        "n_ring_pairs_closed": 1, "link_max_length_m": 500.0})
    summary = stage.run_mv_stage("mv1", [1], PARAMS, log_file=str(tmp_path / "log.txt"))
    assert seen == [("mv1", "y0", "g1")] and summary.status == "done"
    assert summary.to_dict()["links"]["n_links"] == 4
    assert ("✓ Links: 4 normally-open links between rings, 1.2 km, 4 of 6 rings, 10 % of the supply points with a "
            "third cable, 1 ring pairs joined twice") in summary.describe()
    finish = [c for c in db.calls if c[0] == "finish_variant"]
    assert len(finish) == 1 and finish[0][1][2] is summary                      # mv_variant.summary['ties']
    assert summary.to_dict()["ties"] == {"n_ties": 3, "tie_km": 2.5, "tie_max_length_m": 2000.0}
    assert "✓ Ties: 3 normally-open ties, 2.5 km" in summary.describe()


# ---------------------------------------------------------------------------------------------- metrics
def test_metrics_count_a_district_tie_to_a_neighbour():
    nodes = pd.DataFrame({"node_id": [1, 2, 3, 4], "kind": ["busbar", "station", "station", "station"],
                          "x": [0.0, 1000.0, 2000.0, 1000.0], "y": [0.0, 0.0, 0.0, 1000.0]})
    lines = pd.DataFrame({
        "line_id": [1, 2, 3, 4, 5], "from_node": [1, 2, 3, 4, 3], "to_node": [2, 3, 4, 1, 99],
        "length_km": [1.0, 1.0, 1.5, 1.0, 0.8], "kind": "cable", "normally_open": [False, False, True, False, True],
        "open_at_node": [None, None, 4, None, 99], "role": ["ring", "ring", "ring", "ring", "tie"]})
    rings = metrics.ring_table(nodes, lines)
    assert rings["kind"].tolist() == ["ring"] and rings["has_tie"].tolist() == [True]
    tm = metrics.topology_metrics(nodes, lines)
    assert tm["n_ties"] == 1 and tm["tie_km"] == pytest.approx(0.8) and tm["circuit_km"] == pytest.approx(4.5)
    assert tm["n_rings_with_tie"] == 1 and tm["tie_ring_share"] == 1.0 and tm["n_nops"] == 1
    without = metrics.topology_metrics(nodes, lines.iloc[:4])
    assert without["n_rings_with_tie"] == 0 and without["tie_ring_share"] == 0.0
