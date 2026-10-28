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
