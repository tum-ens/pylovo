"""FEEDER_STANDARD_CABLE and MAX_PARALLEL_FEEDER_CABLES: ampacity and voltage options of a feeder section (no database)."""

import logging

import pandas as pd
import pytest

from pylovo import feeder_planning
from pylovo.cable_installer import CableInstaller

# (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur): the shipped feeder catalogue
CABLES = [
    ("NAYY_4_150", 0.208, 0.080, 0.270, 25.0), ("NAYY_4_185", 0.164, 0.080, 0.313, 38.0),
    ("NAYY_4_240", 0.127, 0.080, 0.357, 45.0), ("NAYY_4_300", 0.100, 0.080, 0.425, 58.0),
]


@pytest.fixture
def installer():
    return CableInstaller(None, None, logging.getLogger("standard-cable-test"), CABLES,
                          pd.DataFrame({"name": [name for name, *_ in CABLES]}), pd.DataFrame())


def use(monkeypatch, standard, maximum=4):
    monkeypatch.setattr(feeder_planning, "FEEDER_STANDARD_CABLE", standard)
    monkeypatch.setattr(feeder_planning, "MAX_PARALLEL_FEEDER_CABLES", maximum)
    monkeypatch.setattr(feeder_planning, "FEEDER_PLANNING_UTILIZATION", 1.0)


def choice(installer, current_ka):
    option = feeder_planning._ampacity_option(installer, current_ka)
    return option["cable"], option["parallel"]


@pytest.mark.parametrize("current_ka, expected", [
    (0.20, ("NAYY_4_150", 1)),
    (0.30, ("NAYY_4_185", 1)),   # without a standard cable: a larger cross-section before a parallel cable
    (0.60, ("NAYY_4_185", 2)),   # two cables of at least 0.3 kA each
])
def test_without_a_standard_cable_the_smallest_design_is_chosen(installer, monkeypatch, current_ka, expected):
    use(monkeypatch, None)
    assert choice(installer, current_ka) == expected


@pytest.mark.parametrize("current_ka, expected", [
    (0.20, ("NAYY_4_150", 1)),
    (0.30, ("NAYY_4_150", 2)),
    (0.80, ("NAYY_4_150", 3)),
    (1.08, ("NAYY_4_150", 4)),
    (1.20, ("NAYY_4_300", 3)),   # four standard cables carry 1.08 kA: the fewest larger ones, at most four
    (2.00, ("NAYY_4_300", 5)),   # not even four of the largest: the unbounded rule
])
def test_the_standard_cable_is_laid_in_parallel_before_larger_cables(installer, monkeypatch, current_ka, expected):
    use(monkeypatch, "NAYY_4_150")
    assert choice(installer, current_ka) == expected


def test_the_maximum_parallel_count_is_configurable(installer, monkeypatch):
    use(monkeypatch, "NAYY_4_150", maximum=1)
    assert choice(installer, 0.30) == ("NAYY_4_185", 1)


def options(installer, design):
    stages = feeder_planning._voltage_upgrade_options(installer, design)
    return [sorted((o["cable"], o["parallel"]) for o in stage) for stage in stages]


def design(cable, parallel, current_ka):
    return dict(design_current_ka=current_ka, ampacity=dict(cable=cable, parallel=parallel))


def test_without_a_standard_cable_voltage_upgrades_keep_the_parallel_count(installer, monkeypatch):
    use(monkeypatch, None)
    first, second = options(installer, design("NAYY_4_150", 1, 0.2))
    assert first == [("NAYY_4_150", 1), ("NAYY_4_185", 1), ("NAYY_4_240", 1), ("NAYY_4_300", 1)]
    assert second == []


def test_voltage_upgrades_add_standard_cables_first(installer, monkeypatch):
    use(monkeypatch, "NAYY_4_150")
    first, second = options(installer, design("NAYY_4_150", 1, 0.2))
    assert first == [("NAYY_4_150", 2), ("NAYY_4_150", 3), ("NAYY_4_150", 4)]
    # only when they cannot help: larger cables, at most four in parallel
    assert ("NAYY_4_300", 1) in second and ("NAYY_4_240", 4) in second
    assert all(cable != "NAYY_4_150" and parallel <= 4 for cable, parallel in second)


def test_a_section_on_a_larger_cable_has_no_standard_stage(installer, monkeypatch):
    use(monkeypatch, "NAYY_4_150")
    first, second = options(installer, design("NAYY_4_300", 3, 1.2))
    assert first == [] and ("NAYY_4_300", 4) in second
