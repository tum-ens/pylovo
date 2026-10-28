"""utils.allocate_consumer_simultaneous_loads: service cables carry the coincident load behind them (no database)."""

import pandas as pd
import pytest

from pylovo import utils

CONSUMER_CATEGORIES = pd.DataFrame(
    {"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]}
)


def test_a_service_cable_carries_the_coincidence_of_its_own_households():
    """12 of 60 households of a grid: P_sim = P_installed * (g + (1 - g) * n ** (-3/4)), g = 0.07.

    The service cable of the 12 households carries 201.9 kW * (0.07 + 0.93 * 12 ** -0.75) = 43.26 kW; their share of
    the coincident load of all 60 (the power-flow snapshot) is only 22.84 kW (fd035ce1 sized services from the share).
    """
    buildings = pd.DataFrame({
        "vertice_id": [1, 2], "households": [12.0, 48.0], "residential_peak_load_in_kw": [12 * 16.825, 48 * 16.825],
        "nonresidential_peak_load_in_kw": [0.0, 0.0], "nonresidential_use": [None, None],
        "nonresidential_mv_direct": [None, None]})
    service, snapshot = utils.allocate_consumer_simultaneous_loads([1, 2], buildings, CONSUMER_CATEGORIES)
    assert service[1] == pytest.approx(43.256, abs=1e-3)
    assert snapshot[1][0]["simultaneous_kw"] == pytest.approx(22.843, abs=1e-3)
    assert snapshot[1][0]["service_design_kw"] == service[1]
