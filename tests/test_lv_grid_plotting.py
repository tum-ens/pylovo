"""The LV maps, voltage profiles, statistics, tree drawings and voltage-level figure on fake data.

No database and no internet. Needs the ``plots`` extra; without it the module is skipped.
"""

import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("geopandas")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import geopandas as gpd  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandapower as pp  # noqa: E402
import pandapower.topology as top  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from shapely.geometry import LineString, Point, box  # noqa: E402

from pylovo.plotting import style as st  # noqa: E402
from pylovo.plotting import voltage_levels as vl  # noqa: E402
from pylovo.plotting.generation import networks  # noqa: E402
from pylovo.plotting.lv_grid import electrical, maps, statistics  # noqa: E402

CRS = "EPSG:25832"
PARAMETERS = {
    "power_flow_assessment": {"min_vm_pu": 0.9, "max_vm_pu": 1.1, "lv_reference_voltage_pu": 0.96},
    "cable_dimensioning": {"max_end_to_end_feeder_voltage_drop_percent": 5},
    "transformer_placement": {"rural_max_households": 1.0, "urban_min_households": 2.85,
                              "rural_min_building_distance": 44, "urban_max_building_distance": 29,
                              "transformer_mapping": {"1": [250, 400], "2": [400, 630], "3": [400, 630]}},
}


@pytest.fixture(autouse=True)
def no_show_and_no_leftover_figures(monkeypatch):
    def show(*args, **kwargs):
        raise AssertionError("plt.show() called")

    monkeypatch.setattr(plt, "show", show)
    yield
    plt.close("all")


def _net():
    """A grid like pylovo's: MV bus 1, LV bus 0, two feeders, three consumers; with power-flow results."""
    net = pp.create_empty_network()
    lv = pp.create_bus(net, 0.4, name="LVbus 1", zone="n")
    mv = pp.create_bus(net, 20.0, name="MVbus 1", zone="n")
    pp.create_ext_grid(net, mv)
    pp.create_transformer(net, mv, lv, std_type="0.4 MVA 20/0.4 kV")
    buses = [pp.create_bus(net, 0.4, name=f"Connection Nodebus {i}", zone="n") for i in range(2)]
    consumers = [pp.create_bus(net, 0.4, name=f"Consumer Nodebus {i}", zone="Residential") for i in range(3)]
    pp.create_line(net, lv, buses[0], 0.1, "NAYY 4x150 SE")       # feeder 1
    pp.create_line(net, buses[0], consumers[0], 0.02, "NAYY 4x50 SE")
    pp.create_line(net, buses[0], consumers[1], 0.05, "NAYY 4x50 SE")
    pp.create_line(net, lv, buses[1], 0.2, "NAYY 4x150 SE")       # feeder 2
    pp.create_line(net, buses[1], consumers[2], 0.03, "NAYY 4x50 SE")
    for vertex, bus in enumerate(consumers):
        pp.create_load(net, bus, p_mw=0.03)
    net.load["consumer_vertex"] = [10, 11, 12]
    pp.runpp(net)
    return net


@pytest.fixture(scope="module")
def net():
    return _net()


@pytest.fixture(scope="module")
def layers():
    """Two grids of a postcode area 12345 in local coordinates (metres)."""
    stations = gpd.GeoDataFrame({
        "plz": [12345, 12345], "kcid": [1, 1], "bcid": [1, 2], "transformer_rated_power": [400, 630],
        "power_flow_status": ["converged", "voltage_violation"], "buildings": [3, 2], "households": [4, 6],
        "lv_bus": [0, 0], "ampacity_max_feeder_voltage_drop_percent": [6.5, 3.0],
        "selected_max_feeder_voltage_drop_percent": [4.8, 3.0]},
        geometry=[Point(0, 0), Point(400, 0)], crs=CRS)
    buildings = gpd.GeoDataFrame({
        "kcid": [1] * 5, "bcid": [1, 1, 1, 2, 2], "building_use": ["Residential", "Residential", "Commercial",
                                                                   "Residential", "Public"],
        "peak_load_in_kw": [20.0, 35.0, 80.0, 25.0, 40.0], "vertice_id": [10, 11, 12, 20, 21]},
        geometry=[box(x, 20, x + 10, 30) for x in (10, 60, 110, 380, 430)], crs=CRS)
    lines = gpd.GeoDataFrame({
        "kcid": [1] * 7, "bcid": [1, 1, 1, 1, 1, 2, 2], "pp_index": [0, 1, 2, 3, 4, 0, 1],
        "from_bus": [0, 2, 2, 0, 3, 0, 2], "to_bus": [2, 4, 5, 3, 6, 2, 4],
        "std_type": ["NAYY_4_150", "NAYY_4_50", "NAYY_4_50", "NAYY_4_150", "NAYY_4_50", "NAYY_4_240", "NAYY_4_50"],
        "parallel": [1, 1, 1, 2, 1, 1, 1], "length_km": [0.1, 0.02, 0.05, 0.2, 0.03, 0.05, 0.02],
        "is_service": [False, True, True, False, True, False, True],
        "feeder_sizing_basis": ["ampacity", None, None, "end_to_end_voltage", None, "ampacity", None]},
        geometry=[LineString([(0, 0), (100, 0)]), LineString([(100, 0), (15, 25)]), LineString([(100, 0), (65, 25)]),
                  LineString([(0, 0), (0, -150)]), LineString([(0, -150), (115, 25)]),
                  LineString([(400, 0), (450, 0)]), LineString([(450, 0), (435, 25)])], crs=CRS)
    return {
        "postcode": gpd.GeoDataFrame({"plz": [12345], "settlement_type": [2], "house_distance": [35.0],
                                      "avg_households_per_building": [1.6]}, geometry=[box(-50, -200, 500, 80)],
                                     crs=CRS),
        "grids": stations, "buildings": buildings, "lines": lines,
        "streets": gpd.GeoDataFrame({"clazz": [41]}, geometry=[LineString([(-50, 0), (500, 0)])], crs=CRS),
        "connections": gpd.GeoDataFrame({"source": [10], "target": [99]},
                                        geometry=[LineString([(15, 25), (15, 0)])], crs=CRS),
        "split_points": gpd.GeoDataFrame({"kcid": [1], "bcid": [1]}, geometry=[Point(100, 0)], crs=CRS),
        "mv_customers": gpd.GeoDataFrame({"objectid": ["x"]}, geometry=[box(200, 40, 240, 70)], crs=CRS),
        "exclusions": pd.DataFrame({"objectid": ["a", "b", "c"], "reason": ["unloaded", "unloaded", "mv_direct"]}),
    }


# --------------------------------------------------------------------------- style
def test_cable_colors_follow_the_cross_section():
    colors = st.cable_colors(["NAYY_4_240", "NAYY_4_50", "NAYY_4_150"])
    assert list(colors) == ["NAYY_4_50", "NAYY_4_150", "NAYY_4_240"]
    assert len({tuple(c) for c in colors.values()}) == 3
    assert st.cross_section("NAYY 4x150 SE") == 150


def test_neighbouring_features_get_different_colours():
    points = {i: (float(i), 0.0) for i in range(12)}
    colors = st.neighbour_colors(points, k=2)
    assert all(colors[i] != colors[i + 1] for i in range(11))


def test_scale_bar_lengths_are_round():
    assert st.nice_length(1000) == 200
    assert st.nice_length(4300) == 500
    assert st.nice_length(37) == 5


def test_the_style_changes_no_global_setting():
    before = plt.rcParams["axes.titlelocation"]
    with st.style():
        assert plt.rcParams["axes.titlelocation"] == "left"
    assert plt.rcParams["axes.titlelocation"] == before


# --------------------------------------------------------------------------- maps
@pytest.mark.parametrize("color_by", ["grid", "rating", "cable", "status"])
def test_overview_draws_every_mode(layers, color_by):
    assert isinstance(maps.plot_lv_overview(layers, color_by=color_by), Figure)


def test_overview_of_the_core_extent(layers):
    assert isinstance(maps.plot_lv_overview(layers, extent="core"), Figure)
    assert isinstance(maps.plot_lv_overview(layers, extent=(0, -10, 100, 50)), Figure)


@pytest.mark.parametrize("color_by", ["cable", "feeder", "sizing", "loading", "voltage"])
def test_one_grid_in_every_mode(layers, net, color_by):
    assert isinstance(maps.plot_lv_grid(layers, 1, 1, color_by=color_by, net=net), Figure)


@pytest.mark.parametrize("buildings", ["peak_load", "use", None])
def test_buildings_of_one_grid(layers, buildings):
    assert isinstance(maps.plot_lv_grid(layers, 1, 1, buildings=buildings), Figure)


def test_feeders_from_the_layer_topology(layers):
    fig = maps.plot_lv_grid(layers, 1, 1, color_by="feeder")        # without a net: from from_bus/to_bus
    labels = [t.get_text() for t in fig.axes[0].get_legend().get_texts()]
    assert "Feeder 1" in labels and "Feeder 2" in labels


def test_results_need_the_net(layers):
    with pytest.raises(ValueError, match="needs the pandapower net"):
        maps.plot_lv_grid(layers, 1, 1, color_by="loading")
    with pytest.raises(ValueError, match="no cables"):
        maps.plot_lv_grid(layers, 9, 9)
    with pytest.raises(ValueError, match="color_by"):
        maps.plot_lv_overview(layers, color_by="voltage")


@pytest.mark.parametrize("plot", [
    lambda layers: maps.plot_building_uses(layers),
    lambda layers: maps.plot_partition(layers, "bcid"),
    lambda layers: maps.plot_partition(layers, "kcid"),
    lambda layers: maps.plot_connections(layers),
])
def test_pipeline_step_maps(layers, plot):
    assert isinstance(plot(layers), Figure)


def test_maps_draw_into_given_axes(layers):
    fig, ax = plt.subplots()
    assert maps.plot_lv_overview(layers, ax=ax) is fig


# --------------------------------------------------------------------------- electrical
def test_feeders_and_distances(net):
    feeders = electrical.feeder_ids(net)
    assert feeders.to_dict() == {0: 1, 1: 1, 2: 1, 3: 2, 4: 2}
    distance = electrical.bus_distances(net)
    assert distance[0] == 0
    assert distance[5] == pytest.approx(150.0)      # 0.1 + 0.05 km on feeder 1
    assert distance[6] == pytest.approx(230.0)      # 0.2 + 0.03 km on feeder 2


def test_voltage_band_from_the_version_parameters():
    assert electrical.voltage_band(PARAMETERS) == {"lv_reference_pu": 0.96, "min_vm_pu": 0.9, "max_vm_pu": 1.1}


def test_voltage_profile(net):
    fig = electrical.plot_feeder_voltage_profile(net, band=electrical.voltage_band(PARAMETERS))
    assert isinstance(fig, Figure)
    assert fig.axes[0].get_title(loc="left").startswith("Voltage along the feeders")


def test_voltage_profile_needs_results():
    net = _net()
    net.res_bus = net.res_bus.iloc[0:0]
    with pytest.raises(ValueError, match="no power-flow results"):
        electrical.plot_feeder_voltage_profile(net)


# --------------------------------------------------------------------------- statistics
@pytest.mark.parametrize("households, distance, expected", [
    (1.0, 50.0, 1),     # both rural
    (2.85, 29.0, 3),    # both urban
    (1.925, 36.5, 2),   # both in the middle
    (None, 29.0, 2),    # a missing metric counts as rural: at most semi-urban
    (None, None, 1),
])
def test_settlement_score_like_the_generation(households, distance, expected):
    assert statistics.settlement_score(households, distance, PARAMETERS["transformer_placement"])["type"] == expected


def test_statistics_charts(layers):
    summary = pd.DataFrame({"settlement_type": [1, 1, 2, 3, 3], "buildings": [5, 20, 40, 30, 60],
                            "households": [6, 25, 60, 120, 300], "feeder_km": [0.3, 1.2, 1.4, 0.6, 0.9],
                            "transformer_rated_power": [250, 400, 400, 630, 630]})
    figures = [
        statistics.plot_settlement_type(layers["postcode"], PARAMETERS, context=summary.assign(
            avg_households_per_building=1.5, house_distance=40.0)),
        statistics.plot_rating_counts(layers["grids"]),
        statistics.plot_cable_inventory(layers["lines"]),
        statistics.plot_voltage_drop_check(layers["grids"], limit_percent=5),
        statistics.plot_grid_distributions(summary),
        statistics.plot_grid_distributions(summary, by=None, columns=("buildings",)),
        statistics.plot_exclusions(layers["exclusions"], supplied=5),
    ]
    assert all(isinstance(fig, Figure) for fig in figures)


def test_voltage_drop_check_counts_the_upsized_grids(layers):
    fig = statistics.plot_voltage_drop_check(layers["grids"], limit_percent=5)
    assert fig.axes[0].get_title(loc="left") == "1 of 2 grids needed thicker feeders for the voltage drop"


# --------------------------------------------------------------------------- trees
def test_tidy_tree_layout_spaces_the_leaves_and_centres_the_parents(net):
    graph = top.create_nxgraph(net)
    pos = networks.tidy_tree_pos(graph, 1)
    leaves = sorted(x for node, (x, _) in pos.items() if graph.degree(node) == 1 and node != 1)
    assert leaves == [0.0, 1.0, 2.0]
    assert pos[2][0] == pytest.approx(0.5)                        # above its two consumers
    assert pos[0][0] == pytest.approx((pos[2][0] + pos[3][0]) / 2)  # LV bus between its two feeders
    assert pos[1][1] == 0 and pos[0][1] == -1
    distance = networks.tidy_tree_pos(graph, 1, y="distance")
    assert distance[5][1] == pytest.approx(-150.0)


@pytest.mark.parametrize("draw", [
    lambda graph, net: networks.draw_tree_network(graph),
    lambda graph, net: networks.draw_tree_network(graph, net=net, edge_color_by="cable", y="distance"),
    lambda graph, net: networks.draw_radial_network(graph, net=net, radius="distance"),
])
def test_tree_drawings(net, draw):
    assert isinstance(draw(top.create_nxgraph(net), net), Figure)


def test_a_radial_drawing_in_a_given_axes_leaves_the_other_axes_alone(net):
    graph = top.create_nxgraph(net)
    fig, axes = plt.subplots(1, 2)
    networks.draw_tree_network(graph, net=net, y="distance", ax=axes[0])
    before = axes[0].get_position().bounds
    networks.draw_radial_network(graph, net=net, ax=axes[1])
    assert axes[0].get_position().bounds == before and axes[0].get_aspect() == "auto" and axes[0].axison
    assert axes[1].get_aspect() == 1.0 and not axes[1].axison


def test_core_bounds_can_be_widened_to_an_aspect(layers):
    plain = maps.core_bounds(layers)
    square = maps.core_bounds(layers, aspect=1.0)
    assert square[2] - square[0] == pytest.approx(square[3] - square[1])
    assert square[0] <= plain[0] and square[1] <= plain[1] and square[2] >= plain[2] and square[3] >= plain[3]


def test_the_style_context_restores_only_its_own_settings():
    import matplotlib

    before = (matplotlib.is_interactive(), plt.rcParams["figure.dpi"], plt.rcParams["axes.titleweight"])
    with st.style():
        assert plt.rcParams["axes.titleweight"] == "bold"
        matplotlib.interactive(not before[0])          # what a notebook backend does with the first figure
        plt.rcParams["figure.dpi"] = 150
    try:
        assert matplotlib.is_interactive() is (not before[0])   # kept: rc_context would undo it
        assert plt.rcParams["figure.dpi"] == 150                 # the resolution belongs to the session
        assert plt.rcParams["axes.titleweight"] == before[2]
    finally:
        matplotlib.interactive(before[0])
        plt.rcParams["figure.dpi"] = before[1]


# --------------------------------------------------------------------------- voltage levels
def test_voltage_level_figure_and_chain(layers):
    chain = {"plz": 12345, "kcid": 1, "bcid": 1, "transformer_rated_power": 400, "ring_id": 1, "ring_position": 3,
             "vm_load_pu": 1.0, "mv_lv_tap_pos": 0, "lv_busbar_vm_pu": 0.97, "mv_grid_id": 7, "vn_kv": 20.0,
             "n_stations": 2, "n_rings": 1, "circuit_km": 3.0, "peak_load_mw": 0.5, "hv_mv_transformers": 2,
             "hv_mv_transformer_mva": 40.0, "substation_id": 3, "substation_name": "Test", "egon_bus_id": 42}
    district = gpd.GeoDataFrame({"mv_grid_id": [7]}, geometry=[box(-3000, -3000, 3000, 3000)], crs=CRS)
    mv_grid = {
        "grids": district,
        "substations": gpd.GeoDataFrame({"substation_id": [3]}, geometry=[Point(-2000, 0)], crs=CRS),
        "nodes": gpd.GeoDataFrame({"kind": ["busbar", "station", "station", "customer"]},
                                  geometry=[Point(-2000, 0), Point(0, 0), Point(400, 0), Point(800, 500)], crs=CRS),
        "lines": gpd.GeoDataFrame({"ring_id": [1, 1, None], "role": ["ring", "ring", "tie"],
                                   "kind": ["cable", "overhead", "cable"], "normally_open": [False, False, True]},
                                  geometry=[LineString([(-2000, 0), (0, 0)]), LineString([(0, 0), (400, 0)]),
                                            LineString([(400, 0), (2500, 2500)])], crs=CRS),
    }
    hv = {"lines": gpd.GeoDataFrame({"v_nom": [110, 380], "bus0": [41, 50], "bus1": [42, 51]},
                                    geometry=[LineString([(-20000, 0), (-2000, 0)]),
                                              LineString([(-20000, 5000), (20000, 5000)])], crs=CRS),
          "buses": gpd.GeoDataFrame({"fid": [42], "v_nom": [110]}, geometry=[Point(-2000, 0)], crs=CRS)}
    context = {"grids": district, "substations": mv_grid["substations"]}
    fig = vl.plot_voltage_levels(chain, hv, context, mv_grid, layers)
    assert isinstance(fig, Figure) and len(fig.axes) == 3
    legend = [text.get_text() for text in fig.axes[0].get_legend().get_texts()]
    assert "110 kV at the bus of the substation" in legend
    assert [s.get_edgecolor() for s in fig.axes[1].spines.values()][0][:3] == pytest.approx(
        matplotlib.colors.to_rgb(st.LEVEL_COLORS["MV"]))            # the next map is framed in its level colour
    table = vl.chain_table(chain)
    assert list(table["level"]) == ["HV grid", "HV/MV substation", "MV supply area", "MV ring", "MV/LV station",
                                    "LV grid"]
    assert table.loc[0, "key figures"] == "bus 42"

    unmatched = vl.plot_voltage_levels({**chain, "egon_bus_id": None}, hv, context, mv_grid, layers)
    texts = [text.get_text() for text in unmatched.axes[0].texts]
    assert "Not in the eTraGo HV grid" in texts
    assert any("not in the eTraGo grid" in text.get_text() for text in unmatched.texts)


def test_hv_region_keeps_the_supply_area_at_a_quarter_of_the_map():
    assert vl.hv_region((0, 0, 10_000, 10_000)) == pytest.approx((-15_000, -15_000, 25_000, 25_000))  # 4 x 10 km
    assert vl.hv_region((0, 0, 1_000, 1_000))[2] - vl.hv_region((0, 0, 1_000, 1_000))[0] == pytest.approx(25_000)
    assert vl.hv_region((0, 0, 40_000, 40_000))[2] - vl.hv_region((0, 0, 40_000, 40_000))[0] == \
        pytest.approx(70_000)                                                     # 25 to 70 km wide
    wide = vl.hv_region((0, 0, 10_000, 10_000), aspect=2.0)
    assert (wide[2] - wide[0]) / (wide[3] - wide[1]) == pytest.approx(2.0)


def test_hv_grid_needs_the_download(tmp_path):
    with pytest.raises(FileNotFoundError, match="pylovo-viewer fetch etrago"):
        vl.read_hv_grid(tmp_path)
