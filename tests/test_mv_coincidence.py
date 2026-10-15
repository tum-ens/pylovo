"""MV coincidence (pylovo.mv_grid.coincidence) against pylovo's LV coincidence helpers, without a database."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from pylovo import utils
from pylovo.mv_grid import coincidence
from pylovo.mv_grid.model import CATEGORIES, NODE_COLUMNS

#: Consumer categories with the shipped sim factors (Residential 0.07, Commercial 0.5, Public and Unknown 0.6).
CONSUMER_CATEGORIES = pd.DataFrame({"definition": ["Residential", "Commercial", "Public", "Unknown"],
                                    "sim_factor": [0.07, 0.5, 0.6, 0.6]})
G = CONSUMER_CATEGORIES.set_index("definition").loc[list(CATEGORIES), "sim_factor"].to_numpy()


def _buildings(rng: np.random.Generator, count: int, n_points: int) -> pd.DataFrame:
    """Random building rows as pylovo stores them (NaN values, MV-direct flags, mixed use)."""
    residential = rng.uniform(5, 40, count) * rng.integers(1, 12, count)
    households = rng.integers(0, 12, count).astype(float)
    nonresidential = np.where(rng.random(count) < 0.35, rng.uniform(1, 290, count), 0.0)
    return pd.DataFrame({
        "vertice_id": np.arange(count),
        "connection_point": rng.integers(0, n_points, count),
        "residential_peak_load_in_kw": np.where(rng.random(count) < 0.1, np.nan, residential),
        "households": np.where(rng.random(count) < 0.1, np.nan, households),
        "nonresidential_peak_load_in_kw": nonresidential,
        "nonresidential_use": rng.choice(["Commercial", "Public", "Unknown"], count),
        "nonresidential_mv_direct": rng.choice(np.array([None, True, False], dtype=object), count),
    })


def _station_stats(buildings: pd.DataFrame, station_of_point: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Installed kW and load units per station and category (the sufficient statistics the MV stage reads)."""
    components = utils.build_load_components(buildings)
    point = buildings.set_index("vertice_id").loc[components["consumer_vertex"], "connection_point"].to_numpy()
    components["station"] = station_of_point[point]
    sums = components.groupby(["station", "category"])[["installed_kw", "load_units"]].sum()
    index = pd.MultiIndex.from_product([range(int(station_of_point.max()) + 1), CATEGORIES])
    sums = sums.reindex(index, fill_value=0.0)
    shape = (len(index.levels[0]), len(CATEGORIES))
    return sums["installed_kw"].to_numpy().reshape(shape), sums["load_units"].to_numpy().reshape(shape)


def _reference_kw(installed: np.ndarray, units: np.ndarray, fixed: np.ndarray) -> float:
    """Pooled load of some rows with pylovo's own category formula (independent of the module under test)."""
    installed = np.where(installed > 0, installed, 0.0)
    units = np.where(units > 0, units, 0.0)
    total = sum(utils.category_simultaneous_load(installed[:, c].sum(), units[:, c].sum(), G[c])
                for c in range(len(CATEGORIES)))
    return float(total + np.nansum(fixed))


def _random_rows(rng: np.random.Generator, k: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    present = rng.random((k, 4)) < 0.6
    installed = np.where(present, rng.uniform(1, 800, (k, 4)), 0.0)
    units = np.where(present, rng.integers(1, 120, (k, 4)), 0).astype(float)
    units[:, 1:] = np.where(present[:, 1:], rng.integers(1, 6, (k, 3)), 0)   # few non-residential units
    joints = rng.random(k) < 0.15                                            # joints carry nothing
    installed[joints], units[joints] = 0.0, 0.0
    fixed = np.where(rng.random(k) < 0.2, rng.uniform(100, 3000, k), 0.0)
    return installed, units, fixed


# ---------------------------------------------------------------------------------------------- category law
@pytest.mark.parametrize("seed", range(5))
def test_coincident_kw_equals_pooled_simultaneous_peak_load(seed):
    """The law on summed station statistics equals pylovo's pooled coincident load of all their buildings."""
    rng = np.random.default_rng(seed)
    n_points, n_stations = 160, 12
    buildings = _buildings(rng, 600, n_points)
    station_of_point = rng.integers(0, n_stations, n_points)
    station_of_point[:n_stations] = np.arange(n_stations)                 # every station has a point
    installed, units = _station_stats(buildings, station_of_point)
    subsets = [[s] for s in range(n_stations)] + [list(range(n_stations))]
    subsets += [sorted(rng.choice(n_stations, size=int(rng.integers(2, n_stations)), replace=False))
                for _ in range(40)]
    worst = 0.0
    for subset in subsets:
        nodes = np.flatnonzero(np.isin(station_of_point, subset))
        expected = utils.simultaneous_peak_load(buildings, CONSUMER_CATEGORIES, nodes)
        result = coincidence.coincident_kw(installed[subset].sum(axis=0), units[subset].sum(axis=0), G)
        assert result == pytest.approx(expected, rel=1e-12, abs=1e-9)
        if expected:
            worst = max(worst, abs(result - expected) / expected)
    assert worst <= 1e-12
    # one station: its design load is the station peak pylovo sized it with
    single = coincidence.coincident_kw(installed, units, G)
    assert single.shape == (n_stations,)
    for s in range(n_stations):
        nodes = np.flatnonzero(station_of_point == s)
        assert single[s] == pytest.approx(utils.simultaneous_peak_load(buildings, CONSUMER_CATEGORIES, nodes),
                                          rel=1e-12, abs=1e-9)


def test_reference_values_of_the_law():
    residential = coincidence.coincident_kw([16.825 * 100, 0, 0, 0], [100, 0, 0, 0], G)
    assert residential == pytest.approx(167.256, abs=1e-3)              # 1.673 kW per household
    commercial = coincidence.category_kw([0, 2.0, 0, 0], [0, 2, 0, 0], G)
    assert commercial[1] / 2.0 == pytest.approx(0.5 + 0.5 * 2 ** -0.75)   # g(2) = 0.797
    assert commercial[[0, 2, 3]].tolist() == [0.0, 0.0, 0.0]
    # station peaks are sub-additive: 20 stations of 100 households carry 0.735 of their summed peaks
    pooled = coincidence.coincident_kw([16.825 * 2000, 0, 0, 0], [2000, 0, 0, 0], G)
    assert pooled == pytest.approx(2460.14, abs=0.01)
    assert pooled / (20 * residential) == pytest.approx(0.735, abs=1e-3)


def test_terms_without_load_or_units_are_zero():
    installed = np.array([[100.0, -5.0, np.nan, 50.0], [0.0, 10.0, 10.0, 10.0]])
    units = np.array([[0.0, 1.0, 1.0, np.nan], [3.0, 0.0, -1.0, 2.0]])
    kw = coincidence.category_kw(installed, units, G)
    assert kw[0].tolist() == [0.0, 0.0, 0.0, 0.0]
    assert kw[1, :3].tolist() == [0.0, 0.0, 0.0]
    assert kw[1, 3] == pytest.approx(10.0 * (0.6 + 0.4 * 2 ** -0.75))
    assert coincidence.coincident_kw(np.zeros(4), np.zeros(4), G) == 0.0


def test_coincident_kw_broadcasts_over_leading_axes():
    rng = np.random.default_rng(3)
    installed, units, _ = _random_rows(rng, 15)
    stacked_i, stacked_n = installed.reshape(3, 5, 4), units.reshape(3, 5, 4)
    result = coincidence.coincident_kw(stacked_i, stacked_n, G)
    assert result.shape == (3, 5)
    for i in range(3):
        for j in range(5):
            assert result[i, j] == pytest.approx(_reference_kw(stacked_i[i, j][None], stacked_n[i, j][None],
                                                               np.zeros(1)), rel=1e-12, abs=1e-12)


def test_pooled_kw_adds_fixed_loads_once():
    rng = np.random.default_rng(4)
    installed, units, fixed = _random_rows(rng, 25)
    assert coincidence.pooled_kw(installed, units, fixed, G) == pytest.approx(_reference_kw(installed, units, fixed),
                                                                              rel=1e-12)
    assert coincidence.pooled_kw(np.zeros((0, 4)), np.zeros((0, 4)), np.zeros(0), G) == 0.0
    fixed[0] = np.nan                                                    # a missing fixed load counts as 0
    assert math.isfinite(coincidence.pooled_kw(installed, units, fixed, G))


def test_shapes_are_checked():
    with pytest.raises(ValueError, match="same shape"):
        coincidence.coincident_kw(np.zeros((2, 3)), np.zeros((2, 3)), G)
    with pytest.raises(ValueError, match="same shape"):
        coincidence.coincident_kw(np.zeros((2, 4)), np.zeros((3, 4)), G)
    with pytest.raises(ValueError, match="one sim factor per category"):
        coincidence.coincident_kw(np.zeros(4), np.zeros(4), G[:3])
    with pytest.raises(ValueError, match="fixed_kw"):
        coincidence.segment_loads(np.zeros((3, 4)), np.zeros((3, 4)), np.zeros(2), G)
    with pytest.raises(ValueError, match=r"\(k, 4\)"):
        coincidence.segment_loads(np.zeros(4), np.zeros(4), np.zeros(1), G)


# ---------------------------------------------------------------------------------------------- segments
@pytest.mark.parametrize("seed", range(5))
def test_segment_loads_equal_direct_sums(seed):
    rng = np.random.default_rng(100 + seed)
    for k in (1, 2, 3, 7, 20, 31):
        installed, units, fixed = _random_rows(rng, k)
        loads = coincidence.segment_loads(installed, units, fixed, G)
        assert loads.shape == (k, k)
        for a in range(k):
            for b in range(k):
                if b < a:
                    assert np.isnan(loads[a, b])
                else:
                    expected = _reference_kw(installed[a:b + 1], units[a:b + 1], fixed[a:b + 1])
                    assert loads[a, b] == pytest.approx(expected, rel=1e-10, abs=1e-9)


def test_segment_loads_treat_missing_values_as_zero():
    rng = np.random.default_rng(7)
    installed, units, fixed = _random_rows(rng, 8)
    dirty_i, dirty_n, dirty_f = installed.copy(), units.copy(), fixed.copy()
    dirty_i[2, 1], dirty_n[2, 1] = np.nan, np.nan
    dirty_i[5, 0] = -50.0
    dirty_f[3] = np.nan
    clean_i, clean_n, clean_f = installed.copy(), units.copy(), fixed.copy()
    clean_i[2, 1] = clean_n[2, 1] = clean_i[5, 0] = clean_f[3] = 0.0
    np.testing.assert_allclose(coincidence.segment_loads(dirty_i, dirty_n, dirty_f, G),
                               coincidence.segment_loads(clean_i, clean_n, clean_f, G), rtol=1e-12, atol=1e-9)
    assert coincidence.segment_loads(np.zeros((0, 4)), np.zeros((0, 4)), np.zeros(0), G).shape == (0, 0)


def test_segment_loads_are_not_inferred_from_neighbours():
    """A small non-residential load lowers the factor of a large one: a longer range can carry less load."""
    installed = np.array([[0, 1000.0, 0, 0], [0, 1.0, 0, 0]])
    units = np.array([[0, 1.0, 0, 0], [0, 1.0, 0, 0]])
    loads = coincidence.segment_loads(installed, units, np.zeros(2), G)
    assert loads[0, 1] < loads[0, 0]
    assert loads[0, 1] == pytest.approx(1001.0 * (0.5 + 0.5 * 2 ** -0.75))


# ---------------------------------------------------------------------------------------------- feeders
def test_feeder_scaling_reproduces_the_category_loads():
    installed_f = np.array([1683.0, 500.0, 0.0, 20.0])
    units_f = np.array([100.0, 4.0, 0.0, 0.0])                            # Unknown without units: no component
    u = coincidence.feeder_scaling(installed_f, units_f, G)
    np.testing.assert_allclose(u * installed_f, coincidence.category_kw(installed_f, units_f, G), rtol=1e-14)
    assert u[2] == 0.0 and u[3] == 0.0
    several = coincidence.feeder_scaling(np.stack([installed_f, 2 * installed_f]), np.stack([units_f, units_f]), G)
    assert several.shape == (2, 4)
    np.testing.assert_allclose(several[1], u)                            # the scaling does not depend on I alone


@pytest.mark.parametrize("seed", range(5))
def test_allocate_sums_to_the_coincident_load_of_every_feeder(seed):
    rng = np.random.default_rng(200 + seed)
    n = 60
    installed, units, fixed = _random_rows(rng, n)
    feeder_of = rng.integers(-1, 6, n)
    loads = coincidence.allocate(installed, units, fixed, feeder_of, G)
    assert loads.shape == (n,)
    for feeder in range(6):
        rows = feeder_of == feeder
        assert loads[rows].sum() == pytest.approx(_reference_kw(installed[rows], units[rows], fixed[rows]),
                                                  rel=1e-12, abs=1e-9)
        # one scaling per category and feeder: the loads of two nodes of a feeder are proportional per category
        u = coincidence.feeder_scaling(installed[rows].sum(axis=0), units[rows].sum(axis=0), G)
        np.testing.assert_allclose(loads[rows], installed[rows] @ u + fixed[rows], rtol=1e-12, atol=1e-9)
    for row in np.flatnonzero(feeder_of < 0):                             # own set: own coincident load
        assert loads[row] == pytest.approx(_reference_kw(installed[[row]], units[[row]], fixed[[row]]),
                                           rel=1e-12, abs=1e-9)
    one_feeder = coincidence.allocate(installed, units, fixed, np.zeros(n, dtype=int), G)
    assert one_feeder.sum() == pytest.approx(coincidence.pooled_kw(installed, units, fixed, G), rel=1e-12)
    assert (one_feeder <= coincidence.allocate(installed, units, fixed, np.arange(n), G) + 1e-9).all()


def test_allocate_checks_its_labels():
    installed, units, fixed = np.ones((3, 4)), np.ones((3, 4)), np.zeros(3)
    np.testing.assert_allclose(coincidence.allocate(installed, units, fixed, np.array([0.0, 0.0, 1.0]), G),
                               coincidence.allocate(installed, units, fixed, [0, 0, 1], G))
    with pytest.raises(ValueError, match="integer labels"):
        coincidence.allocate(installed, units, fixed, [0, 0.5, 1], G)
    with pytest.raises(ValueError, match="integer labels"):
        coincidence.allocate(installed, units, fixed, np.array([0, None, 1], dtype=object), G)
    with pytest.raises(ValueError, match="one label per node"):
        coincidence.allocate(installed, units, fixed, [0, 1], G)


# ---------------------------------------------------------------------------------------------- helpers
def test_node_stats_reads_a_node_table():
    rows = []
    for key in (1, 2, 3):
        row = dict.fromkeys(NODE_COLUMNS, 0.0)
        row.update(kind="station", connection_hint="ring", inst_res=16.825 * key, units_res=key, inst_com=10.0 * key,
                   units_com=1.0, inst_unk=np.nan, p_fixed_kw=None if key == 3 else 5.0 * key)
        rows.append(row)
    nodes = pd.DataFrame(rows, index=[11, 12, 13])
    installed, units, fixed = coincidence.node_stats(nodes)
    assert installed.shape == units.shape == (3, 4) and fixed.shape == (3,)
    np.testing.assert_allclose(installed[:, 0], [16.825, 33.65, 50.475])
    np.testing.assert_allclose(installed[:, 3], 0.0)
    np.testing.assert_allclose(units[:, 1], 1.0)
    np.testing.assert_allclose(fixed, [5.0, 10.0, 0.0])
    with pytest.raises(KeyError):
        coincidence.node_stats(nodes.drop(columns="units_pub"))


def test_tan_phi():
    assert coincidence.tan_phi(0.95) == pytest.approx(0.32868, abs=1e-5)
    assert coincidence.tan_phi(1.0) == 0.0
    with pytest.raises(ValueError, match="power factor"):
        coincidence.tan_phi(0.0)
