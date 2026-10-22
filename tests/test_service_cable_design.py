"""CableInstaller.select_service_cable_design picks the same design as the former pandas implementation."""

import logging

import numpy as np
import pandas as pd

import pytest

from pylovo import cable_installer
from pylovo.cable_installer import CableInstaller
from pylovo.config_loader import MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT

# (name, r_ohm_per_km, x_ohm_per_km, max_i_ka, cost_eur); two cables share cost and cross-section.
CABLES = [
    ("NAYY_4_50", 0.642, 0.083, 0.142, 11.0), ("NAYY_4_70", 0.443, 0.082, 0.173, 16.0),
    ("NAYY_4_95", 0.320, 0.082, 0.215, 18.0), ("NFA2X_4_95", 0.320, 0.0, 0.245, 18.0),
    ("NAYY_4_150", 0.208, 0.080, 0.270, 25.0), ("NAYY_4_240", 0.127, 0.080, 0.357, 45.0),
]


def _reference_design(installer: CableInstaller, design_current_ka, length_km, available_cables):
    """The implementation before the pandas-free rewrite."""
    cable_df = installer._cable_df
    line_df = cable_df.loc[cable_df.index.isin(available_cables)]
    parallel = 1
    while True:
        ampacity_options = line_df.loc[
            line_df["max_i_ka"] >= design_current_ka / cable_installer.CABLE_PLANNING_UTILIZATION / parallel]
        if not ampacity_options.empty:
            break
        parallel += 1
    ampacity_cable = ampacity_options.sort_values(by=["cost_eur", "q_mm2"]).index[0]
    drops = {c: installer._service_voltage_drop_percent(design_current_ka, length_km, c, parallel)
             for c in ampacity_options.index}
    voltage_options = ampacity_options.loc[
        [c for c, d in drops.items() if d <= MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT + 1e-9]
    ]
    if voltage_options.empty:
        selected = min(ampacity_options.index, key=lambda c: (
            drops[c], float(ampacity_options.at[c, "cost_eur"]), int(ampacity_options.at[c, "q_mm2"])))
    else:
        selected = voltage_options.sort_values(by=["cost_eur", "q_mm2"]).index[0]
    return selected, parallel, ampacity_cable, drops[ampacity_cable], drops[selected]


@pytest.mark.parametrize("utilization", [1.0, 0.8])
def test_service_cable_design_matches_the_pandas_implementation(monkeypatch, utilization):
    monkeypatch.setattr(cable_installer, "CABLE_PLANNING_UTILIZATION", utilization)
    installer = CableInstaller(None, None, logging.getLogger("service-cable-test"), CABLES,
                               pd.DataFrame(), pd.DataFrame())
    names = [name for name, *_ in CABLES]
    rng = np.random.default_rng(3)
    for _ in range(2000):
        available = list(rng.choice(names, size=int(rng.integers(1, len(names) + 1)), replace=False))
        current = float(rng.uniform(0.001, 1.2))
        length = float(rng.choice([rng.uniform(0.001, 0.05), rng.uniform(0.05, 0.6)]))
        design = installer.select_service_cable_design(current, length, available)
        selected, parallel, ampacity_cable, ampacity_drop, selected_drop = _reference_design(
            installer, current, length, available)
        assert (design["cable"], design["parallel"], design["ampacity_cable"]) == (selected, parallel, ampacity_cable)
        assert design["ampacity_drop_percent"] == ampacity_drop
        assert design["selected_drop_percent"] == selected_drop


@pytest.mark.parametrize("utilization, expected", [(1.0, "NAYY_4_50"), (0.8, "NAYY_4_70")])
def test_service_cable_keeps_the_planned_reserve(monkeypatch, utilization, expected):
    """0.12 kA fits a 142 A cable, but not at 80 %; the drop stays computed at 0.12 kA."""
    monkeypatch.setattr(cable_installer, "CABLE_PLANNING_UTILIZATION", utilization)
    installer = CableInstaller(None, None, logging.getLogger("service-reserve-test"), CABLES,
                               pd.DataFrame(), pd.DataFrame())
    design = installer.select_service_cable_design(0.12, 0.01, ["NAYY_4_50", "NAYY_4_70"])
    assert (design["cable"], design["parallel"], design["sizing_basis"]) == (expected, 1, "ampacity")
    assert design["selected_drop_percent"] == installer._service_voltage_drop_percent(0.12, 0.01, expected, 1)
