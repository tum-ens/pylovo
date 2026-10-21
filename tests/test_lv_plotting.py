"""Plots of the LV generation and validation packages on fake data (no database, no internet).

Needs the ``plots`` extra; without it the module is skipped.
"""
import types

import networkx as nx
import pandas as pd
import pytest

pytest.importorskip("matplotlib")
pytest.importorskip("contextily")
pytest.importorskip("plotly")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

import pandapower as pp  # noqa: E402

from pylovo.plotting.generation import networks  # noqa: E402
from pylovo.plotting.validation import geo_validation, metric_validation, powerflow_validation  # noqa: E402


@pytest.fixture(autouse=True)
def no_show_and_no_leftover_figures(monkeypatch):
    """The plot functions return their figure: a ``plt.show()`` would block a script or hide the figure in a loop."""

    def show(*args, **kwargs):
        raise AssertionError("plt.show() called")

    monkeypatch.setattr(plt, "show", show)
    yield
    plt.close("all")


class FakeClient:
    """The reads of the PLZ plots."""

    def __init__(self, *args, **kwargs):
        self.cur = types.SimpleNamespace(execute=lambda *a, **k: None,
                                         fetchall=lambda: [("NAYY 4x150 SE", 1.5), ("NAYY 4x35 SE", 0.5)])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read_per_trafo_dict(self, plz):
        per_size = {400: [3.0, 5.0], 630: [4.0]}
        return [per_size, per_size], ["load_count", "bus_count"], {400: 2, 630: 1}

    def read_cable_dict(self, plz):
        return {"NAYY 4x150 SE": 1.5, "NAYY 4x35 SE": 0.5}


@pytest.mark.parametrize("name", ["plot_pie_of_trafo_cables", "plot_hist_trafos", "plot_boxplot_plz",
                                  "plot_cable_length_of_types"])
def test_the_plz_plots_return_an_open_figure_and_do_not_show_it(monkeypatch, name):
    monkeypatch.setattr(metric_validation, "DatabaseClient", FakeClient)
    fig = getattr(metric_validation, name)(85653)
    assert isinstance(fig, Figure)
    assert plt.get_fignums() == [fig.number]


def _tree():
    return nx.Graph([(1, 2), (2, 3), (2, 4), (1, 5)])


@pytest.mark.parametrize("draw", [networks.draw_tree_network, networks.draw_tree_network_spacing,
                                  networks.draw_radial_network])
def test_the_tree_drawings_return_their_figure_and_do_not_show_it(draw):
    fig = draw(_tree())
    assert isinstance(fig, Figure)
    assert plt.get_fignums() == [fig.number]


def test_the_tree_drawing_of_a_stored_grid_returns_the_figure(monkeypatch):
    monkeypatch.setattr(networks, "read_net_with_grid_generator", lambda plz, kcid, bcid: object())
    monkeypatch.setattr(networks, "create_nxgraph", lambda net: _tree())
    fig = networks.draw_tree_network_with_spacing_from_grid_id(85653, 1, 2)
    assert isinstance(fig, Figure)


def _net_with_results():
    return types.SimpleNamespace(res_load=pd.DataFrame({"p_mw": [0.004, 0.006, 0.0075, 0.005]}),
                                 res_bus=pd.DataFrame({"vm_pu": [1.0, 0.99, 0.98, 0.97, 0.965]}))


def test_the_load_and_voltage_histograms_go_into_their_own_axes():
    fig, axes = plt.subplots(1, 2)
    drawn = powerflow_validation.plot_load_and_voltage_distribution(_net_with_results(), bins=10, ax=axes)
    assert drawn is fig
    assert axes[0].get_title() == "Load Distribution" and axes[1].get_title() == "Voltage Distribution"
    assert len(axes[0].patches) == len(axes[1].patches) == 10


def test_the_load_and_voltage_histograms_refuse_one_axes():
    _, ax = plt.subplots()
    with pytest.raises(ValueError, match="two axes"):
        powerflow_validation.plot_load_and_voltage_distribution(_net_with_results(), ax=ax)


def _station_net(parallel=2):
    """A stored grid with one station of ``parallel`` 400 kVA units at its LV bus."""
    net = pp.create_empty_network()
    mv, lv = pp.create_bus(net, 20.0, geodata=(11.5, 48.1)), pp.create_bus(net, 0.4, geodata=(11.5, 48.1))
    pp.create_transformer(net, mv, lv, std_type="0.4 MVA 20/0.4 kV", parallel=parallel)
    return net


class GridClient(FakeClient):
    """One stored grid per PLZ."""
    net = None

    def get_list_from_plz(self, plz):
        return [(1, 1)]

    def read_net_db(self, plz, kcid, bcid, **kwargs):
        return self.net


def test_the_transformer_map_colours_a_station_by_its_rating_like_the_charts(monkeypatch):
    GridClient.net = _station_net(parallel=2)
    drawn = {}
    monkeypatch.setattr(geo_validation, "DatabaseClient", GridClient)
    monkeypatch.setattr(geo_validation, "vlevel_plotly", lambda net_plot, **kwargs: drawn.update(net=net_plot, **kwargs))

    geo_validation.plot_trafo_on_map(85653)

    assert drawn["net"].bus["vn_kv"].tolist() == [800]                              # 2 x 400 kVA
    assert drawn["net"].bus["name"].iloc[0].endswith("transformer: 800_kVA")
    assert drawn["colors_dict"] is geo_validation.PLOT_COLOR_DICT                  # 800 kVA has a configured colour
