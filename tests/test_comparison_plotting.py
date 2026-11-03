"""Smoke checks of the comparison figures: each draws its panels from small metric tables."""
import numpy as np
import pandas as pd
import pytest

pytest.importorskip("matplotlib")

import matplotlib  # noqa: E402

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402

from pylovo.analysis import comparison  # noqa: E402
from pylovo.plotting import comparison as plots  # noqa: E402


@pytest.fixture
def groups():
    rng = np.random.default_rng(3)
    tables = {}
    for name, low in (("reference", 10), ("candidate", 14), ("other", 30)):
        tables[name] = pd.DataFrame({m.key: rng.uniform(low, low + 10, 25) for m in comparison.METRICS})
    yield tables
    plt.close("all")


def test_metric_figures_have_one_panel_per_metric(groups):
    pair = {name: groups[name] for name in ("reference", "candidate")}
    colors = plots.group_colors(list(groups), "reference")
    assert colors["reference"] == plots.REFERENCE_COLOR
    scores = comparison.compare(*pair.values())
    for fig in (plots.plot_boxplots(groups, colors), plots.plot_histograms(groups, colors),
                plots.plot_distributions(pair, colors, scores)):
        assert len(fig.axes) == len(comparison.METRICS)
    with pytest.raises(ValueError):
        plots.plot_distributions(groups, colors, scores)


def test_correlation_cable_and_score_figures(groups):
    pair = {name: groups[name] for name in ("reference", "candidate")}
    assert len(plots.plot_correlations(pair).axes) == 4                       # three matrices and the colour bar
    cables = pd.DataFrame({"kind": ["feeders", "house services"], "cross_section": [150, 50], "km": [1., .5],
                           "parallel": [False, False]})
    assert len(plots.plot_cable_shares({"reference": cables, "candidate": cables},
                                       plots.group_colors(["reference", "candidate"], "reference")).axes) == 2
    scores = {name: comparison.compare(groups["reference"], groups[name]) for name in ("candidate", "other")}
    fig = plots.plot_score_matrix(scores)
    assert [t.get_text() for t in fig.axes[0].get_xticklabels()] == ["candidate", "other"]
