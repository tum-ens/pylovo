"""Plots of the classification package that need no database."""
import pandas as pd
import pytest

pytest.importorskip("matplotlib")  # the package imports the whole plotting stack (extra "plots")
pytest.importorskip("seaborn")
plotly = pytest.importorskip("plotly")

from plotly import graph_objects as go  # noqa: E402

from pylovo.plotting.classification import plot_samples_on_map  # noqa: E402


def test_sample_map_uses_the_current_plotly_map_api_without_a_token(monkeypatch):
    shown = []
    monkeypatch.setattr(go.Figure, "show", lambda self, *args, **kwargs: shown.append(self))
    samples = pd.DataFrame({"lat": [48.1, 48.2], "lon": [11.5, 11.6], "regio7": [71, 73]})
    plot_samples_on_map(samples)
    (fig,) = shown
    assert fig.data[0].type == "scattermap", "scatter_mapbox is deprecated"
    assert fig.layout.map.style == "carto-positron"
