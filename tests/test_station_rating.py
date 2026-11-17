"""The one rule for the rating of a station: the smallest catalogue rating strictly above the planned load."""
import numpy as np
import pytest

from pylovo import utils


@pytest.mark.parametrize("planned, expected", [(100.0, 250), (249.9, 250), (250.0, 400), (399.0, 400), (630.0, None)])
def test_the_smallest_rating_strictly_above_the_planned_load(planned, expected):
    assert utils.smallest_rating_above(np.array([250, 400, 630]), planned) == expected
    assert utils.smallest_rating_above([250, 400, 630], planned) == expected


def test_the_planned_load_is_the_coincident_load_over_the_utilization(monkeypatch):
    monkeypatch.setattr(utils, "TRANSFORMER_PLANNING_UTILIZATION", 0.8)
    assert utils.planned_station_load(320.0) == pytest.approx(400.0)
    assert utils.smallest_rating_above([250, 400, 630], utils.planned_station_load(320.0)) == 630
