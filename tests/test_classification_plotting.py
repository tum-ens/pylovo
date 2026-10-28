"""Plots of the classification package that need no database."""
import pandas as pd
import pytest

pytest.importorskip("matplotlib")  # the package imports the whole plotting stack (extra "plots")
pytest.importorskip("seaborn")
pytest.importorskip("contextily")
plotly = pytest.importorskip("plotly")

from plotly import graph_objects as go  # noqa: E402
from psycopg2.extensions import adapt  # noqa: E402

from pylovo.plotting.classification import plot_samples_on_map  # noqa: E402
from pylovo.plotting.generation.networks import get_network_info_for_plotting  # noqa: E402


def test_sample_map_uses_the_current_plotly_map_api_without_a_token(monkeypatch):
    shown = []
    monkeypatch.setattr(go.Figure, "show", lambda self, *args, **kwargs: shown.append(self))
    samples = pd.DataFrame({"lat": [48.1, 48.2], "lon": [11.5, 11.6], "regio7": [71, 73]})
    plot_samples_on_map(samples)
    (fig,) = shown
    assert fig.data[0].type == "scattermap", "scatter_mapbox is deprecated"
    assert fig.layout.map.style == "carto-positron"


def test_network_info_of_a_dataframe_row_gives_plain_ints_that_psycopg2_can_adapt():
    """The representative-grid plots of the classification notebooks pass a DataFrame row (numpy.int64 values)."""
    row = pd.DataFrame({"plz": [91054], "kcid": [3], "bcid": [-2], "no_branches": [4]}).iloc[0]
    info = get_network_info_for_plotting(row)
    assert info == (91054, 3, -2) and all(type(value) is int for value in info)
    for value in info:
        adapt(value).getquoted()  # raises "can't adapt type 'numpy.int64'" for the numpy scalars of the row


# ------------------------------------------------------------------------------------- figures of the notebooks
import json  # noqa: E402
import re  # noqa: E402
import warnings  # noqa: E402
from pathlib import Path  # noqa: E402

import matplotlib  # noqa: E402
import numpy as np  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandapower as pp  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from classification_helpers import synthetic_clustering_parameters  # noqa: E402
from pylovo.classification.clustering.clustering_algorithms import kmeans_clustering  # noqa: E402
from pylovo.config_loader import TUMPalette, TUMPalette1  # noqa: E402
from pylovo.plotting.classification import (  # noqa: E402
    REGIOSTAR7_NAMES,
    cluster_color,
    plot_bar_distribution_of_clusters_per_regio_5,
    plot_ch_index_for_clustering_algos,
    plot_clusters_in_parameter_space,
    plot_clusters_per_regiostar_class,
    plot_db_index_for_clustering_algos,
    plot_eigendecomposition,
    plot_factor_analysis,
    plot_key_figure_distributions,
    plot_percentage_of_clusters,
    plot_radar_graph,
    plot_representative_grids,
    plot_sample_map,
    plot_samples_per_regiostarclass,
    plot_stacked_distribution_of_clusters_per_regio_5,
    regiostar7_color,
)

PARAMS = ["no_house_connections", "vsw_per_branch", "avg_trafo_dis"]
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _close_figures():
    yield
    plt.close("all")


@pytest.fixture()
def no_warnings():
    """Fail on any warning: the notebooks show them, and they filter none."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        yield


@pytest.fixture()
def clustered():
    frame = synthetic_clustering_parameters()
    frame["regio7"] = np.resize([71.0, 72.0, 73.0, 74.0, 75.0, 76.0, 77.0], len(frame))
    grids, representatives = kmeans_clustering(frame.copy(), PARAMS, 4)
    return grids, representatives


def texts_of(fig: Figure) -> str:
    """All labels, titles, tick labels and legend texts of a figure, for the language checks."""
    parts = [fig._suptitle.get_text()] if fig._suptitle else []
    for ax in fig.axes:
        parts += [ax.get_xlabel(), ax.get_ylabel(), ax.get_title()]
        parts += [label.get_text() for label in ax.get_xticklabels() + ax.get_yticklabels()]
        legend = ax.get_legend()
        if legend is not None:
            parts += [legend.get_title().get_text()] + [text.get_text() for text in legend.get_texts()]
    for legend in fig.legends:
        parts += [legend.get_title().get_text()] + [text.get_text() for text in legend.get_texts()]
    return " ".join(parts)


GERMAN = re.compile(r"Anzahl|Klasse|Stichprobe|Anteil|Verteilung|Erkl[äa]rt|Kumulativ|Index des|Algorithmus|Gemeindetyp")


def test_no_german_label_is_left_in_the_classification_plots():
    for path in (REPO / "src/pylovo/plotting/classification").glob("*.py"):
        assert not GERMAN.search(path.read_text(encoding="utf-8")), path.name


def test_samples_per_class_has_english_labels_and_a_fixed_colour_per_class(no_warnings):
    samples = pd.DataFrame({"regio7": [71.0, 71.0, 74.0, 77.0, 77.0, 77.0]})
    fig = plot_samples_per_regiostarclass(samples)
    ax = fig.axes[0]
    assert (ax.get_xlabel(), ax.get_ylabel()) == ("RegioStaR 7 class", "Sampled PLZ")
    assert [label.get_text() for label in ax.get_xticklabels()] == ["71", "74", "77"]
    assert [patch.get_height() for patch in ax.patches] == [2, 1, 3]
    colors = [matplotlib.colors.to_hex(patch.get_facecolor()) for patch in ax.patches]
    assert colors == [TUMPalette1[0].lower(), TUMPalette1[3].lower(), TUMPalette1[6].lower()]
    assert [text.get_text() for text in ax.texts] == ["2", "1", "3"], "every bar is labelled"


def test_sample_map_draws_pool_and_classes_offline_into_the_given_axes(no_warnings):
    samples = pd.DataFrame({"lat": [48.1, 48.2, 49.5], "lon": [11.5, 11.6, 11.0], "regio7": [71.0, 71.0, 77.0]})
    pool = pd.DataFrame({"lat": np.linspace(47.5, 50.5, 20), "lon": np.linspace(9, 13.5, 20)})
    fig, ax = plt.subplots()
    assert plot_sample_map(samples, pool, ax=ax) is fig
    assert len(ax.collections) == 3, "pool and one layer per class"
    assert [len(collection.get_offsets()) for collection in ax.collections] == [20, 2, 1]
    labels = [text.get_text() for text in ax.get_legend().get_texts()]
    assert labels == ["PLZ of the region (20)", f"71 {REGIOSTAR7_NAMES[71]} (2)", f"77 {REGIOSTAR7_NAMES[77]} (1)"]
    assert ax.get_aspect() == pytest.approx(1 / np.cos(np.radians(pool["lat"].mean())))
    assert matplotlib.colors.to_hex(ax.collections[2].get_facecolor()[0]) == regiostar7_color(77).lower()
    assert ax.get_images() == [], "no tiles"


def test_key_figure_distributions_one_panel_per_column_and_the_cut_tail_is_named(no_warnings):
    frame = synthetic_clustering_parameters()
    fig = plot_key_figure_distributions(frame, ["no_branches", "avg_trafo_dis", "cable_length_km"], ncols=2)
    visible = [ax for ax in fig.axes if ax.get_visible()]
    assert [ax.get_title() for ax in visible] == ["no_branches", "avg_trafo_dis", "cable_length_km"]
    assert len(fig.axes) == 4 and not fig.axes[3].get_visible()
    note = visible[1].texts[0].get_text()
    hidden = int((frame["avg_trafo_dis"] > frame["avg_trafo_dis"].quantile(0.99)).sum())
    assert note.startswith("median") and f"{hidden} above" in note
    default = plot_key_figure_distributions(frame)
    titles = [ax.get_title() for ax in default.axes if ax.get_visible()]
    assert not {"plz", "bcid", "kcid", "version_id"} & set(titles) and "no_households" in titles


def test_eigendecomposition_marks_the_eigenvalue_one_in_english(no_warnings):
    frame = synthetic_clustering_parameters()[["no_branches", "no_households", "avg_trafo_dis", "cable_length_km"]]
    fig, ax = plt.subplots()
    assert plot_eigendecomposition(frame, ax=ax) is fig
    assert (ax.get_xlabel(), ax.get_ylabel()) == ("Principal component", "Share of the explained variance")
    assert sum(patch.get_height() for patch in ax.patches) == pytest.approx(1)
    assert [patch.get_x() + patch.get_width() / 2 for patch in ax.patches] == [1, 2, 3, 4], "components count from 1"
    kaiser = [line for line in ax.lines if line.get_linestyle() == "--"]
    assert len(kaiser) == 1 and kaiser[0].get_ydata()[0] == pytest.approx(1 / 4)
    eigenvalues = np.linalg.eigvalsh(frame.corr().to_numpy())
    above = [patch.get_height() > 1 / 4 for patch in ax.patches]
    assert sum(above) == int((eigenvalues > 1).sum()), "the line separates the eigenvalues above 1"


def test_factor_analysis_numbers_the_factors_from_one_with_a_colour_bar(no_warnings):
    frame = synthetic_clustering_parameters()[["no_branches", "no_households", "avg_trafo_dis", "cable_length_km"]]
    fig = plot_factor_analysis(frame, n_comps=2)
    panels = [ax for ax in fig.axes if ax.get_title()]
    assert [ax.get_title() for ax in panels] == ["PCA", "Unrotated FA", "Varimax FA"]
    assert all([label.get_text() for label in ax.get_xticklabels()] == ["1", "2"] for ax in panels)
    assert any(ax.get_ylabel() == "Loading" for ax in fig.axes), "colour bar"


@pytest.mark.parametrize("plot, ylabel", [(plot_ch_index_for_clustering_algos, "Calinski-Harabasz index"),
                                          (plot_db_index_for_clustering_algos, "Davies-Bouldin index")])
def test_index_plots_draw_into_the_given_axes_with_english_labels(plot, ylabel, no_warnings):
    frame = synthetic_clustering_parameters()
    fig, (left, right) = plt.subplots(1, 2)
    table = plot(frame, [3, 4], PARAMS, ax=left, legend=False)
    plot(frame, [3, 4], PARAMS, ax=right)
    assert len(table) == 5 and not right.get_title()
    assert left.get_xlabel() == "Number of clusters" and left.get_ylabel().startswith(ylabel)
    assert left.get_legend() is None
    legend = right.get_legend()
    assert legend.get_title().get_text() == "Clustering algorithm"
    assert [text.get_text() for text in legend.get_texts()] == ["k-means", "GMM full", "GMM diagonal", "GMM tied",
                                                                 "GMM spherical"]
    assert sum(len(line.get_xdata()) == 2 for line in left.lines) == 5, "one line per algorithm"
    assert plt.get_fignums() == [fig.number], "no extra figure"


def test_index_plot_without_axes_opens_its_own_figure():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        first = plot_ch_index_for_clustering_algos(synthetic_clustering_parameters(), [3, 4], PARAMS)
        plot_ch_index_for_clustering_algos(synthetic_clustering_parameters(), [3, 4], PARAMS)
    assert len(plt.get_fignums()) == 2 and list(first.columns) == ["algorithm", "no_clusters", "ch_index"]


def test_cluster_shares_add_up_to_100_percent(clustered, no_warnings):
    grids, _ = clustered
    fig, ax = plt.subplots()
    assert plot_percentage_of_clusters(grids, ax=ax) is fig
    assert (ax.get_xlabel(), ax.get_ylabel()) == ("Cluster", "Share of the grids (%)")
    assert sum(patch.get_height() for patch in ax.patches) == pytest.approx(100)


def test_clusters_per_regiostar_class_stack_to_one_in_the_cluster_colours(clustered, no_warnings):
    grids, _ = clustered
    fig = plot_clusters_per_regiostar_class(grids)
    ax = fig.axes[0]
    assert [label.get_text() for label in ax.get_xticklabels()] == ["71", "72", "73", "74", "75", "76", "77"]
    heights = pd.DataFrame([(patch.get_x(), patch.get_height()) for patch in ax.patches], columns=["x", "h"])
    assert heights.groupby("x")["h"].sum().to_numpy() == pytest.approx(np.ones(7))
    assert ax.get_legend().get_title().get_text() == "Cluster"
    first_bars = [matplotlib.colors.to_hex(container.patches[0].get_facecolor()) for container in ax.containers]
    assert first_bars == [cluster_color(cluster).lower() for cluster in sorted(grids["clusters"].unique())]
    gem5 = plot_clusters_per_regiostar_class(grids, level="regio5").axes[0]
    assert [label.get_text() for label in gem5.get_xticklabels()] == ["51", "52", "53", "54", "55"]
    assert gem5.get_xlabel() == "RegioStaR Gem 5 class"
    with pytest.raises(ValueError, match="level"):
        plot_clusters_per_regiostar_class(grids, level="regio9")


def test_old_regio_5_plots_are_english_and_return_their_figure(clustered):
    grids, _ = clustered
    stacked = plot_stacked_distribution_of_clusters_per_regio_5(grids)
    assert stacked.axes[0].get_title() == "Clusters per RegioStaR Gem 5 class"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # seaborn's own palette handling of a numeric hue
        bars = plot_bar_distribution_of_clusters_per_regio_5(grids)
    assert isinstance(bars, Figure), "the figure is returned instead of None"
    for fig in (stacked, bars):
        assert not GERMAN.search(texts_of(fig))
    assert bars.axes[0].get_xlabel() == "RegioStaR 7 class"


def test_radar_panels_are_titled_with_their_cluster_in_its_colour(clustered, no_warnings):
    _, representatives = clustered
    fig = plot_radar_graph(representatives, PARAMS)
    titled = [ax for ax in fig.axes if ax.get_title()]
    assert [ax.get_title() for ax in titled] == ["Cluster 0", "Cluster 1", "Cluster 2", "Cluster 3"]
    assert [matplotlib.colors.to_hex(ax.title.get_color()) for ax in titled] == [
        cluster_color(cluster).lower() for cluster in range(4)]
    labels = [label.get_text() for label in titled[0].get_xticklabels()]
    assert labels == ["no_house_\nconnections", "vsw_per_branch", "avg_trafo_dis"], "long names wrap at underscores"


def test_parameter_space_is_a_corner_plot_with_the_representatives_as_stars(clustered, no_warnings):
    grids, representatives = clustered
    fig = plot_clusters_in_parameter_space(grids, PARAMS, representatives, max_points=60)
    assert fig._suptitle.get_text() == f"60 of {len(grids)} grids, stars: representative grids"
    lower = [ax for ax in fig.axes if any(collection.get_offsets().shape[0] == len(representatives)
                                          and collection.get_sizes()[0] == 260 for collection in ax.collections)]
    assert len(lower) == 3, "stars in the 3 panels below the diagonal, none above it (corner plot)"
    all_points = plot_clusters_in_parameter_space(grids, PARAMS, max_points=None)
    assert all_points._suptitle.get_text() == f"{len(grids)} grids"


def fake_net(houses: int, spacing_deg: float, with_line_geo: bool = False, lon0: float = 11.5, lat0: float = 48.0):
    """A feeder along the latitude ``lat0`` with one service cable and load per house (pylovo's bus 0 is the LV bus)."""
    net = pp.create_empty_network()
    lv = pp.create_bus(net, 0.4, geodata=(lon0, lat0))
    mv = pp.create_bus(net, 20, geodata=(lon0, lat0 + 1e-4))
    pp.create_transformer(net, mv, lv, "0.4 MVA 20/0.4 kV")
    previous = lv
    for i in range(1, houses + 1):
        node = pp.create_bus(net, 0.4, geodata=(lon0 + i * spacing_deg, lat0))
        pp.create_line(net, previous, node, 0.05, "NAYY 4x150 SE")
        house = pp.create_bus(net, 0.4, geodata=(lon0 + i * spacing_deg, lat0 + spacing_deg / 2))
        pp.create_line(net, node, house, 0.02, "NAYY 4x50 SE")
        pp.create_load(net, house, 0.002)
        previous = node
    if with_line_geo:
        geo = net.bus["geo"].map(lambda text: json.loads(text)["coordinates"])
        net.line["geo"] = [json.dumps({"type": "LineString", "coordinates": [geo[f], geo[t]]})
                           for f, t in zip(net.line["from_bus"], net.line["to_bus"])]
    return net


def test_representative_grids_one_panel_per_net_in_metres_from_the_transformer(no_warnings):
    nets = [fake_net(3, 0.001), fake_net(5, 0.002, with_line_geo=True)]
    fig = plot_representative_grids(nets, titles=["Cluster 0", "Cluster 1"], ncols=3)
    panels = [ax for ax in fig.axes if ax.get_visible()]
    assert [ax.get_title() for ax in panels] == ["Cluster 0", "Cluster 1"] and len(fig.axes) == 3
    first = panels[0]
    feeders = [line for line in first.lines if line.get_linewidth() == 1.6]
    services = [line for line in first.lines if line.get_linewidth() == 0.6]
    assert (len(feeders), len(services)) == (3, 3), "the cables to a load are service cables"
    end = feeders[-1].get_xydata()[-1]
    assert end[0] == pytest.approx(3 * 0.001 * 111_320 * np.cos(np.radians(48.0)), rel=0.01)
    assert end[1] == pytest.approx(0, abs=0.5)
    station = first.collections[-1].get_offsets()[0]
    assert tuple(station) == (0, 0)
    assert len(first.collections[0].get_offsets()) == 3, "one dot per consumer"
    assert [matplotlib.colors.to_hex(line.get_color()) for line in panels[1].lines][:1] == [TUMPalette[1].lower()]
    assert [text.get_text() for text in fig.legends[0].get_texts()] == ["Feeder cable", "Service cable", "Consumer",
                                                                        "Transformer"]


def test_representative_grids_on_the_same_scale_share_the_extent():
    nets = [fake_net(2, 0.001), fake_net(8, 0.002)]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        fig = plot_representative_grids(nets, same_scale=True)
    fig.canvas.draw()
    widths = [np.diff(ax.get_xlim())[0] for ax in fig.axes if ax.get_visible()]
    assert widths[0] == pytest.approx(widths[1])
    assert widths[0] >= 8 * 0.002 * 111_320 * np.cos(np.radians(48.0))


def test_the_classification_maps_credit_their_data_unless_switched_off():
    from pylovo.plotting import style as st
    from pylovo.plotting.classification.features import PLZ_ATTRIBUTION

    grids = plot_representative_grids([fake_net(2, 0.001)])
    assert [text.get_text() for text in grids.texts] == [st.DATA_ATTRIBUTION]
    assert plot_representative_grids([fake_net(2, 0.001)], attribution="").texts == []
    samples = pd.DataFrame({"lat": [48.1], "lon": [11.5], "regio7": [71.0]})
    fig, ax = plt.subplots()
    plot_sample_map(samples, ax=ax)
    assert [text.get_text() for text in ax.texts] == [PLZ_ATTRIBUTION]


def test_representative_grids_need_a_net_with_geodata():
    with pytest.raises(ValueError, match="No net"):
        plot_representative_grids([])
    net = pp.create_empty_network()
    pp.create_bus(net, 0.4)
    with pytest.raises(ValueError, match="geodata"):
        plot_representative_grids([net])
