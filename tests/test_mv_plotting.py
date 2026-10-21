"""Tests of the MV grid plots (pylovo.plotting.mv_grid) on synthetic layers (no database, no internet).

Needs the ``plots`` extra: ``uv run --extra plots pytest -q tests/test_mv_plotting.py``; without it the module is
skipped.
"""

import math
import os

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("contextily")
os.environ.setdefault("MPLBACKEND", "Agg")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import geopandas as gpd  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402

from pylovo.analysis import mv_validation  # noqa: E402
from pylovo.plotting import mv_grid as mvplot  # noqa: E402
from pylovo.plotting.mv_grid import maps, statistics  # noqa: E402
from pylovo.plotting.utils import OSM_TILE_HEADERS  # noqa: E402

EPSG = 25832

# mv_node_id: (mv_grid_id, kind, x, y, ring_id, vm_load, vm_n1, tap, lv_busbar, lv_min, lv_feed_in, source, conflict)
_NODES = {
    1: (11, "busbar", 0, 0, None, 1.02, 1.02, None, None, None, None, None, None),
    2: (11, "station", 1000, 0, 1, 1.015, 0.99, 0, 0.975, 0.93, 1.03, 0.98, False),
    3: (11, "station", 2000, 0, 1, 1.010, 0.98, 0, 0.970, 0.925, 1.04, 0.981, False),
    4: (11, "joint", 3000, 0, 1, 1.006, 0.975, None, None, None, None, None, None),
    5: (11, "station", 3000, 1000, 1, 1.003, 0.97, -1, 0.990, 0.94, 1.06, 0.982, False),
    6: (11, "station", 2000, 1000, 1, 1.008, 0.965, -1, 0.995, 0.95, 1.075, 0.983, True),
    7: (11, "station", 1000, 1000, 1, 1.014, 0.96, 0, 0.972, 0.92, 1.03, 0.984, False),
    8: (11, "station", 3500, -500, 1, 1.004, 0.972, 1, 0.962, 0.91, 1.02, 0.985, False),
    9: (11, "customer", -1000, 0, 2, 1.017, 1.017, None, None, None, None, None, None),
    10: (11, "generator", 0, -1500, 3, 1.02, None, None, None, None, None, None, None),
    21: (12, "busbar", 6000, 0, None, 1.02, 1.02, None, None, None, None, None, None),
    22: (12, "station", 7000, 0, 1, 1.012, 0.99, 0, 0.97, 0.93, 1.02, 0.98, False),
    23: (12, "station", 7000, 1000, 1, 1.008, 0.98, 0, 0.968, 0.93, 1.02, 0.979, False),
    24: (12, "station", 6000, 1000, 1, 1.011, 0.985, 0, 0.969, 0.93, 1.02, 0.979, False),
}
# mv_line_id: (mv_grid_id, from, to, role, kind, ring_id, normally_open, open_at_node, loading load / n1 / feed-in)
_LINES = {
    101: (11, 1, 2, "ring", "cable", 1, False, None, 40.0, 80.0, 20.0),
    102: (11, 2, 3, "ring", "cable", 1, False, None, 35.0, 75.0, 18.0),
    103: (11, 3, 4, "ring", "overhead", 1, False, None, 30.0, 70.0, 15.0),
    104: (11, 4, 5, "ring", "cable", 1, False, None, 20.0, 60.0, 12.0),
    105: (11, 5, 6, "ring", "cable", 1, True, 6, 0.0, 50.0, 0.0),
    106: (11, 6, 7, "ring", "cable", 1, False, None, 25.0, 65.0, 14.0),
    107: (11, 7, 1, "ring", "cable", 1, False, None, 45.0, 90.0, 22.0),
    108: (11, 4, 8, "stub", "cable", 1, False, None, 5.0, 5.0, 3.0),
    109: (11, 1, 9, "dedicated", "cable", 2, False, None, 55.0, 55.0, 10.0),
    110: (11, 1, 9, "dedicated", "cable", 2, True, 9, 0.0, 55.0, 0.0),
    111: (11, 1, 10, "generator", "mixed", 3, False, None, 0.0, 0.0, 70.0),
    112: (11, 7, 24, "tie", "cable", None, True, 24, None, None, None),
    121: (12, 21, 22, "ring", "cable", 1, False, None, 30.0, 60.0, 10.0),
    122: (12, 22, 23, "ring", "cable", 1, False, None, 20.0, 50.0, 8.0),
    123: (12, 23, 24, "ring", "cable", 1, True, 24, 0.0, 40.0, 0.0),
    124: (12, 24, 21, "ring", "cable", 1, False, None, 25.0, 55.0, 9.0),
}


def _layers(id_shift: int = 0, ring_position: bool = False) -> dict:
    """Two MV grids: grid 11 with a ring (joint, stub, NOP), a 2-leg customer feeder, a generator feeder and a
    tie to grid 12, which has one ring. ``id_shift`` shifts every id (another variant of the same districts)."""
    s = id_shift
    node_rows = []
    for nid, (gid, kind, x, y, ring, vm, vm_n1, tap, lv, lv_min, lv_feed, src, conflict) in _NODES.items():
        node_rows.append({
            "mv_node_id": nid + s, "mv_grid_id": gid + s, "kind": kind, "ring_id": ring, "vm_load_pu": vm,
            "vm_n1_pu": vm_n1, "mv_lv_tap_pos": tap, "lv_busbar_vm_pu": lv, "lv_min_vm_pu": lv_min,
            "lv_busbar_vm_feed_in_pu": lv_feed, "lv_design_source_vm_pu": src, "band_conflict": conflict,
            "area_class": "rural", "geom": Point(x, y),
        })
    nodes = gpd.GeoDataFrame(pd.DataFrame(node_rows), geometry="geom", crs=EPSG)
    if ring_position:
        nodes["ring_position"] = nodes["mv_node_id"].map({2 + s: 1, 3 + s: 2, 5 + s: 3, 6 + s: 4, 7 + s: 5})
    xy = {nid + s: (x, y) for nid, (_, _, x, y, *_) in _NODES.items()}
    line_rows = []
    for lid, (gid, a, b, role, kind, ring, is_open, at, l_load, l_n1, l_feed) in _LINES.items():
        geom = LineString([xy[a + s], xy[b + s]])
        line_rows.append({
            "mv_line_id": lid + s, "mv_grid_id": gid + s, "from_node": a + s, "to_node": b + s, "role": role,
            "kind": kind, "ring_id": ring, "normally_open": is_open, "open_at_node": None if at is None else at + s,
            "length_km": geom.length / 1000, "parallel": 1, "loading_load_percent": l_load,
            "loading_n1_percent": l_n1, "loading_feed_in_percent": l_feed, "geom": geom,
        })
    lines = gpd.GeoDataFrame(pd.DataFrame(line_rows), geometry="geom", crs=EPSG)
    substations = gpd.GeoDataFrame({"substation_id": [101 + s, 102 + s], "name": ["A", "B"],
                                    "geom": [Point(0, 0), Point(6000, 0)]}, geometry="geom", crs=EPSG)
    grids = gpd.GeoDataFrame({"mv_grid_id": [11 + s, 12 + s], "substation_id": [101 + s, 102 + s],
                              "complete": [True, False], "status": ["done", "done"],
                              "geom": [box(-2000, -2000, 4000, 2000), box(4000, -2000, 9000, 2000)]},
                             geometry="geom", crs=EPSG)
    settlements = gpd.GeoDataFrame({"settlement_id": [1, 2], "urban": [False, True],
                                    "geom": [box(500, -300, 2500, 1300), box(5500, -500, 7500, 1500)]},
                                   geometry="geom", crs=EPSG)
    return {"nodes": nodes, "lines": lines, "substations": substations, "grids": grids, "settlements": settlements}


@pytest.fixture(autouse=True)
def _headless(monkeypatch):
    """No plt.show() and no basemap download in any test; figures are closed afterwards."""
    def no_show(*args, **kwargs):
        raise AssertionError("plt.show() must not be called")

    def no_tiles(*args, **kwargs):
        raise AssertionError("no basemap download in tests")

    monkeypatch.setattr(plt, "show", no_show)
    monkeypatch.setattr(maps.cx, "add_basemap", no_tiles)
    yield
    plt.close("all")


def _legend_labels(ax):
    legend = ax.get_legend()
    return [t.get_text() for t in legend.get_texts()] if legend is not None else []


# ---------------------------------------------------------------------------------------------------------------
# ring_paths
# ---------------------------------------------------------------------------------------------------------------


def test_ring_paths_halves_main_path_order_and_distances():
    layers = _layers()
    nodes, lines = maps.ring_paths(layers["nodes"], layers["lines"], 11, 1)
    assert sorted(nodes.index) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert nodes.loc[[2, 3, 4, 5, 8], "half"].tolist() == ["a"] * 5
    assert nodes.loc[[6, 7], "half"].tolist() == ["b", "b"]
    assert nodes.at[1, "half"] is None
    assert nodes.loc[[1, 2, 3, 4, 5, 6, 7], "main"].all() and not nodes.at[8, "main"]
    assert nodes["order"].dropna().astype(int).to_dict() == {2: 1, 3: 2, 5: 3, 6: 4, 7: 5}
    d = math.sqrt(2)
    expected = {1: 0.0, 2: 1.0, 3: 2.0, 4: 3.0, 5: 4.0, 6: 1 + d, 7: d, 8: 3 + math.sqrt(0.5)}
    assert nodes["dist_km"].to_dict() == pytest.approx(expected)
    # (n-1): the longer feeding path of both exits out (with the open point closed)
    expected_n1 = {1: 0.0, 2: 5 + d, 3: 4 + d, 4: 3 + d, 5: 4.0, 6: 5.0, 7: 6.0, 8: 3 + d + math.sqrt(0.5)}
    assert nodes["dist_n1_km"].to_dict() == pytest.approx(expected_n1)
    assert lines["half"].to_dict() == {101: "a", 102: "a", 103: "a", 104: "a", 105: "nop", 106: "b", 107: "b",
                                       108: "a"}
    assert lines["exit"].dropna().to_dict() == {101: "a", 107: "b"}
    assert not lines.at[108, "main"] and lines.at[105, "main"]


def test_ring_paths_uses_stored_ring_positions_and_handles_feeders():
    layers = _layers(ring_position=True)
    layers["nodes"].loc[layers["nodes"]["mv_node_id"] == 7, "ring_position"] = 9
    nodes, _ = maps.ring_paths(layers["nodes"], layers["lines"], 11, 1)
    assert nodes.at[7, "order"] == 9 and nodes.at[2, "order"] == 1
    # two-leg customer feeder: both legs reach the customer, the open leg counts for (n-1)
    nodes, lines = maps.ring_paths(layers["nodes"], layers["lines"], 11, 2)
    assert nodes.at[9, "half"] == "a" and nodes.at[9, "main"] and nodes.at[9, "dist_n1_km"] == pytest.approx(1.0)
    assert lines.at[110, "half"] == "nop"
    # generator feeder: one exit, no (n-1) distance
    nodes, _ = maps.ring_paths(layers["nodes"], layers["lines"], 11, 3)
    assert nodes.at[10, "dist_km"] == pytest.approx(1.5) and nodes["dist_n1_km"].isna().all()
    with pytest.raises(ValueError, match="no lines of ring 7"):
        maps.ring_paths(layers["nodes"], layers["lines"], 11, 7)


def test_ring_paths_accepts_plain_frames_without_geometry():
    layers = _layers()
    nodes = pd.DataFrame(layers["nodes"].drop(columns="geom"))
    nodes["x"] = layers["nodes"].geometry.x
    nodes["y"] = layers["nodes"].geometry.y
    lines = pd.DataFrame(layers["lines"].drop(columns="geom"))
    table, _ = maps.ring_paths(nodes, lines, 12, 1)
    assert table.loc[[22, 23], "half"].tolist() == ["a", "a"] and table.at[24, "half"] == "b"
    fig = maps.plot_mv_grid_map({"nodes": nodes, "lines": lines}, 12)
    assert isinstance(fig, Figure)


# ---------------------------------------------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------------------------------------------


def test_overview_draws_districts_lines_substations_and_nops():
    fig = mvplot.plot_mv_overview(_layers(), basemap=False)
    ax = fig.axes[0]
    labels = _legend_labels(ax)
    assert {"HV/MV substation", "normally-open point", "incomplete district", "tie", "overhead line"} <= set(labels)
    assert "2 supply districts" in ax.get_title()
    assert ax.get_xticks().size == 0


def test_basemap_uses_the_tile_headers_and_attribution(monkeypatch):
    calls = []
    monkeypatch.setattr(maps.cx, "add_basemap", lambda ax, **kw: calls.append(kw))
    mvplot.plot_mv_overview(_layers(), basemap=True)
    assert calls and calls[0]["headers"] == OSM_TILE_HEADERS
    assert calls[0]["attribution"] == maps.OSM_ATTRIBUTION and "25832" in str(calls[0]["crs"])


def test_a_basemap_failure_is_a_warning(monkeypatch):
    def offline(*args, **kwargs):
        raise ConnectionError("no internet")

    monkeypatch.setattr(maps.cx, "add_basemap", offline)
    with pytest.warns(UserWarning, match="basemap not drawn"):
        fig = mvplot.plot_mv_grid_map(_layers(), 11, basemap=True)
    assert isinstance(fig, Figure)


@pytest.mark.parametrize("color_by", ["ring", "kind", "loading", "voltage", "tap"])
def test_grid_map_colour_modes(color_by):
    fig, ax = plt.subplots()
    out = mvplot.plot_mv_grid_map(_layers(), 11, color_by=color_by, ax=ax)
    assert out is fig
    assert "MV grid 11: 3 rings and feeders" in ax.get_title()
    labels = _legend_labels(ax)
    assert {"cable line", "overhead line", "mixed line", "stub line", "tie (normally open)",
            "normally-open point"} <= set(labels)
    if color_by == "tap":
        assert "band conflict" in labels
    if color_by in ("loading", "voltage", "tap"):
        assert len(fig.axes) == 2          # colour bar


def test_grid_map_errors():
    with pytest.raises(ValueError, match="color_by"):
        mvplot.plot_mv_grid_map(_layers(), 11, color_by="age")
    with pytest.raises(ValueError, match="MV grid 99"):
        mvplot.plot_mv_grid_map(_layers(), 99)


def test_ring_map_numbers_the_members():
    fig = mvplot.plot_mv_ring(_layers(), 11, 1)
    ax = fig.axes[0]
    assert ax.get_title() == "Ring 1 of MV grid 11: 5 members, 7.4 km (+0.7 km stubs)"
    assert sorted(t.get_text() for t in ax.texts) == ["1", "2", "3", "4", "5"]
    assert {"half A (exit A)", "half B (exit B)", "normally-open section", "stub line", "stub node", "joint",
            "normally-open point"} <= set(_legend_labels(ax))


def test_ring_map_renumbers_members_sequentially_when_stored_positions_have_gaps():
    layers = _layers(ring_position=True)
    for node, position in {2: 1, 3: 4, 5: 9, 6: 13, 7: 17}.items():
        layers["nodes"].loc[layers["nodes"]["mv_node_id"] == node, "ring_position"] = position
    fig = mvplot.plot_mv_ring(layers, 11, 1)
    assert sorted(t.get_text() for t in fig.axes[0].texts) == ["1", "2", "3", "4", "5"]


def test_lv_busbar_voltage_map_marks_band_conflicts():
    fig = mvplot.plot_lv_busbar_voltage_map(_layers())
    ax = fig.axes[0]
    assert "band conflict" in _legend_labels(ax)
    assert ax.get_title().startswith("lv_busbar_vm_pu: 9 stations, 1 band conflicts")
    fig = mvplot.plot_lv_busbar_voltage_map(_layers(), column="lv_min_vm_pu", mv_grid_id=12)
    assert "3 stations, 0 band conflicts" in fig.axes[0].get_title()
    with pytest.raises(ValueError, match="no column"):
        mvplot.plot_lv_busbar_voltage_map(_layers(), column="coupled_lv_busbar_vm_pu")


def test_variants_side_by_side_match_districts_by_busbar_position():
    layers = {"d1": _layers(), "d0": _layers(id_shift=1000)}
    fig = mvplot.plot_variants_side_by_side(layers, 1011)
    a, b = fig.axes[0], fig.axes[1]
    assert a.get_title().startswith("d1: grid 11,") and b.get_title().startswith("d0: grid 1011,")
    assert a.get_xlim() == b.get_xlim() and a.get_ylim() == b.get_ylim()
    fig = mvplot.plot_variants_side_by_side(layers, {"d1": 12, "d0": 1012}, color_by="loading")
    assert fig.axes[0].get_title().startswith("d1: grid 12,")
    with pytest.raises(ValueError, match="none of the variants"):
        mvplot.plot_variants_side_by_side(layers, 5)


def test_plot_mv_grid_reads_the_layers_read_only(monkeypatch):
    calls = []

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read_mv_layers(self, version_id, variant_id, mv_grid_id=None):
            calls.append((version_id, variant_id, mv_grid_id))
            return _layers()

    monkeypatch.setattr(maps, "DatabaseClient", FakeClient)
    assert isinstance(mvplot.plot_mv_grid("mv1", "d1", 11, color_by="kind"), Figure)
    assert calls == [("mv1", "d1", 11)]


def test_interactive_mv_net_writes_no_file(tmp_path, monkeypatch):
    pytest.importorskip("plotly")
    import pandapower as pp

    monkeypatch.chdir(tmp_path)
    net = pp.create_empty_network()
    xy = [(11.50, 48.50), (11.51, 48.50), (11.51, 48.51)]
    buses = [pp.create_bus(net, vn_kv=20.0, geodata=p) for p in xy]
    hv = pp.create_bus(net, vn_kv=110.0, geodata=xy[0], name="HV busbar 1")      # as the stored nets
    pp.create_ext_grid(net, hv, vm_pu=1.0)
    pp.create_transformer(net, hv, buses[0], "25 MVA 110/20 kV")
    for i in range(2):
        pp.create_line(net, buses[i], buses[i + 1], 1.0, "NA2XS2Y 1x240 RM/25 12/20 kV", geodata=[xy[i], xy[i + 1]])
    pp.create_load(net, buses[2], p_mw=0.4)
    fig = mvplot.interactive_mv_net(net)
    assert fig.layout.template.layout.plot_bgcolor == "white"
    assert any(trace.type == "scattermap" for trace in fig.data)
    assert list(tmp_path.iterdir()) == []
    fig = mvplot.interactive_mv_net(net, on_map=False)
    assert len(fig.data) > 0 and list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------------------------------------------


def test_metric_distribution_with_literature_ranges():
    df = pd.DataFrame({"variant": ["d0"] * 4 + ["d1"] * 3, "ring_members": [8, 12, 15, 20, 5, 9, 11]})
    fig = statistics.plot_metric_distribution(df, "ring_members", ranges="auto", area_class="rural")
    ax = fig.axes[0]
    labels = _legend_labels(ax)
    assert any("Ding0 planning limit" in label and "at most 20" in label for label in labels)
    assert any("textbook open ring" in label and "5 to 10" in label for label in labels)
    assert [t.get_text() for t in ax.get_xticklabels()] == ["d0\n(n=4)", "d1\n(n=3)"]
    custom = [mv_validation.LiteratureRange("ring_members", None, 5, 30, 15, "city", "x")]
    fig = statistics.plot_metric_distribution(df, "ring_members", ranges=custom, order=["d1", "d0"])
    assert {"city: 5 to 30", "city: typical 15"} <= set(_legend_labels(fig.axes[0]))
    fig = statistics.plot_metric_distribution(df, "ring_members")
    assert fig.axes[0].get_legend() is None
    with pytest.raises(ValueError, match="no column"):
        statistics.plot_metric_distribution(df, "feeder_km")


def test_variant_summary_small_multiples():
    stats = pd.DataFrame({"version_id": ["mv1", "mv1", "mv1b"], "mv_variant_id": ["d0", "d1", "d1"],
                          "circuit_km": [410.0, 455.5, 470.2], "cable_share": [1.0, 0.66, 0.64],
                          "n_rings": [40, 42, 45], "runtime_s": [60.0, 75.0, np.nan]})
    fig = statistics.plot_variant_summary(stats, ["circuit_km", "cable_share", "n_rings", "runtime_s"], ncols=2)
    visible = [ax for ax in fig.axes if ax.get_visible()]
    assert len(visible) == 4
    assert [t.get_text() for t in visible[0].get_xticklabels()] == ["mv1/d0", "mv1/d1", "mv1b/d1"]
    assert visible[1].get_title() == "Cable share of the circuit length"
    with pytest.raises(ValueError, match="trench_km"):
        statistics.plot_variant_summary(stats, ["trench_km"])


def test_voltage_profile_of_a_ring():
    layers = _layers()
    fig = statistics.plot_voltage_profile(layers["nodes"], layers["lines"], 11, 1)
    ax = fig.axes[0]
    by_label = {line.get_label(): line for line in ax.get_lines()}
    half_a = by_label["load case, half A"]
    assert list(half_a.get_xdata()) == pytest.approx([0.0, 1.0, 2.0, 3.0, 4.0])
    assert list(half_a.get_ydata()) == pytest.approx([1.02, 1.015, 1.010, 1.006, 1.003])
    assert list(by_label["load case, half B"].get_xdata()) == pytest.approx([0.0, math.sqrt(2), 1 + math.sqrt(2)])
    d = math.sqrt(2)
    n1_a = by_label["(n-1), half A fed from exit B"]       # from the open point back to exit A
    assert list(n1_a.get_xdata()) == pytest.approx([4.0, 3 + d, 4 + d, 5 + d])
    assert list(n1_a.get_ydata()) == pytest.approx([0.97, 0.975, 0.98, 0.99])
    n1_b = by_label["(n-1), half B fed from exit A"]
    assert list(n1_b.get_xdata()) == pytest.approx([5.0, 6.0])
    assert list(n1_b.get_ydata()) == pytest.approx([0.965, 0.96])
    labels = _legend_labels(ax)
    assert {"normal limit (5 % drop)", "(n-1) limit (10 % drop)", "normally-open point"} <= set(labels)
    assert by_label["normal limit (5 % drop)"].get_ydata()[0] == pytest.approx(0.97)
    assert by_label["(n-1) limit (10 % drop)"].get_ydata()[0] == pytest.approx(0.92)


@pytest.mark.parametrize("case, column", [("load", "loading_load_percent"), ("n1a", "loading_n1_percent")])
def test_loading_histogram_cases(case, column):
    lines = _layers()["lines"]
    fig = statistics.plot_loading_histogram(lines, case)
    ax = fig.axes[0]
    box_text = ax.texts[0].get_text()
    n_expected = 15 if column == "loading_n1_percent" else 12     # ties never, normally-open lines only in (n-1)
    assert box_text.startswith(f"lines: {n_expected}\n")
    assert {"planning limit, normal operation (60 %)", "(n-1) limit (100 %)"} <= set(_legend_labels(ax))
    with pytest.raises(ValueError, match="no column 'feed_in'"):                 # no feed-in cases any more
        statistics.plot_loading_histogram(lines.drop(columns="loading_feed_in_percent"), "feed_in")


def test_loading_histogram_by_variant_and_errors():
    a, b = _layers()["lines"].assign(variant="d0"), _layers()["lines"].assign(variant="d1")
    fig = statistics.plot_loading_histogram(pd.concat([a, b]), "load", by="variant", utilization=0.5)
    assert {"d0 (12)", "d1 (12)", "planning limit, normal operation (50 %)"} <= set(_legend_labels(fig.axes[0]))
    with pytest.raises(ValueError, match="loading_max_percent"):
        statistics.plot_loading_histogram(a, "loading_max_percent")


def test_voltage_budget_chain():
    fig = statistics.plot_voltage_budget(_layers()["nodes"])
    ax = fig.axes[0]
    assert ax.get_title() == "Voltage budget of 9 MV/LV stations, 1 band conflicts"
    labels = _legend_labels(ax)
    assert {"tap -1", "tap 0", "tap +1", "band conflict", "requirement of the LV net (MV side)"} <= set(labels)
    # the stage values of one station: busbar, band edge, MV node at the band edge, after the tap, LV busbar
    normal = [c for c in ax.collections if c.__class__.__name__ == "LineCollection"][0]
    first = normal.get_segments()[0]
    assert first[:, 1][:5] == pytest.approx([1.02, 1.005, 1.0, 1.0, 0.975])
    with pytest.raises(ValueError, match="lv_busbar_vm_pu"):
        statistics.plot_voltage_budget(_layers()["nodes"].drop(columns="lv_busbar_vm_pu"))


def test_tap_distribution_per_variant():
    nodes = _layers()["nodes"]
    other = nodes.assign(mv_lv_tap_pos=nodes["mv_lv_tap_pos"].where(nodes["mv_lv_tap_pos"].isna(), -2))
    fig = statistics.plot_tap_distribution({"d1": nodes, "sq": other})
    ax = fig.axes[0]
    assert [t.get_text() for t in ax.get_xticklabels()] == ["-2", "-1", "0", "+1", "+2"]
    labels = _legend_labels(ax)
    assert "d1: 9 stations, 1 band conflicts (11.1 %)" in labels and "band conflict" in labels
    heights = [p.get_height() for p in ax.patches[:5]]
    assert heights == pytest.approx([0.0, 200 / 9, 600 / 9, 100 / 9, 0.0])
    fig = statistics.plot_tap_distribution(pd.concat([nodes.assign(variant="a"), other.assign(variant="b")]),
                                           share=False)
    assert fig.axes[0].get_ylabel() == "Stations"


def test_plausibility_traffic_lights():
    values = pd.DataFrame({"ring_members": [12, 25], "cable_share": [0.6, 0.9], "area_class": ["rural", "rural"]},
                          index=pd.Index(["g1", "g2"], name="mv_grid_id"))
    table = mv_validation.compare_to_literature(values)
    fig = statistics.plot_plausibility(table)
    ax = fig.axes[0]
    keys = table[["metric", "label", "area_class"]].astype(str).drop_duplicates()
    assert len(ax.get_yticklabels()) == len(keys)
    assert ax.get_xscale() == "log"
    labels = _legend_labels(ax)
    assert any(label.startswith("green (") for label in labels) and any(label.startswith("red (") for label in
                                                                            labels)
    with pytest.raises(ValueError, match="status"):
        statistics.plot_plausibility(table.drop(columns="status"))


def test_solver_convergence_panels():
    diagnostics = pd.DataFrame({"variant": ["d1", "d1", "sw"], "length_savings_m": [52000.0, 31000.0, 60000.0],
                                "length_final_m": [48000.0, 30500.0, 51000.0], "runtime_s": [99.0, 99.0, 99.0],
                                "solver_runtime_s": [4.2, 0.2, 60.0]})
    fig = statistics.plot_solver_convergence(diagnostics)
    assert len(fig.axes) == 2 and fig.axes[1].get_xscale() == "log"      # solver_runtime_s, not runtime_s
    assert "median gain: 7.7 %" in fig.axes[0].texts[0].get_text()
    fig, ax = plt.subplots()
    assert statistics.plot_solver_convergence(diagnostics.drop(columns=["runtime_s", "solver_runtime_s"]),
                                              ax=ax) is fig
    with pytest.raises(ValueError, match="length_savings_m"):
        statistics.plot_solver_convergence(diagnostics.drop(columns="length_savings_m"))


def test_package_exports():
    for name in mvplot.__all__:
        assert callable(getattr(mvplot, name))
    assert statistics.metric_label("km_per_km2") == "Circuit km per km²"
    assert statistics.metric_label("my_metric") == "my metric"


@pytest.mark.parametrize('ring_id', [1, 2, 3])
def test_maps_preserve_string_node_ids(ring_id):
    layers = _layers()
    expected_nodes, expected_lines = maps.ring_paths(layers['nodes'], layers['lines'], 11, ring_id)
    ids = {n: f'node-{n}' for n in layers['nodes']['mv_node_id']}
    layers['nodes']['mv_node_id'] = layers['nodes']['mv_node_id'].map(ids)
    for column in ('from_node', 'to_node', 'open_at_node'):
        layers['lines'][column] = layers['lines'][column].map(ids)
    nodes, lines = maps.ring_paths(layers['nodes'], layers['lines'], 11, ring_id)
    expected_nodes.index = expected_nodes.index.map(ids)
    pd.testing.assert_frame_equal(nodes.sort_index(), expected_nodes.sort_index())
    assert lines['half'].to_dict() == expected_lines['half'].to_dict()
    points = maps._nop_points(maps._line_table(layers['lines']), maps._node_table(layers['nodes']))
    assert set(points['node']) == {'node-6', 'node-9', 'node-24'}
    assert isinstance(mvplot.plot_mv_grid_map(layers, 11), Figure)
    assert isinstance(mvplot.plot_mv_ring(layers, 11, ring_id), Figure)
