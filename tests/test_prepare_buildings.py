"""Order and fallbacks of GridGenerator.prepare_buildings (no database)."""

import logging

import pytest

from pylovo import grid_generator
from pylovo.grid_generator import GridGenerator


class Db:
    """Records every database call; ``raises`` names the calls that raise a ValueError."""

    def __init__(self, raises=()):
        self.calls, self.raises = [], set(raises)

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            if name in self.raises:
                raise ValueError(f"{name} cannot be calculated.")
            return 1
        return call


def prepared(monkeypatch, raises=()):
    monkeypatch.setattr(grid_generator, "USE_INFDB", False)
    monkeypatch.setattr(grid_generator, "RESIDENTIAL_ONLY_GENERATION", False)
    gg = GridGenerator.__new__(GridGenerator)
    gg.plz, gg.dbc, gg.logger = 80331, Db(raises), logging.getLogger("prepare-test")
    gg.prepare_buildings()
    return gg.dbc.calls


@pytest.mark.parametrize("missing", ["calculate_avg_households_per_building", "calculate_house_distance_metric"])
def test_a_metric_that_cannot_be_calculated_does_not_skip_the_settlement_type(monkeypatch, missing):
    calls = prepared(monkeypatch, raises=[missing])
    assert "set_settlement_type_per_plz" in calls
