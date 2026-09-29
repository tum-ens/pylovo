"""utils.CoincidentLoads returns exactly what utils.simultaneous_peak_load returns."""

import numpy as np
import pandas as pd
import pytest

from pylovo import utils

CONSUMER_CATEGORIES = pd.DataFrame(
    {"definition": ["Commercial", "Public", "Residential"], "sim_factor": [0.5, 0.6, 0.07]}
)


def _buildings(rng: np.random.Generator, count: int) -> pd.DataFrame:
    residential = rng.uniform(5, 40, count) * rng.integers(1, 12, count)
    households = rng.integers(0, 12, count).astype(float)
    nonresidential = np.where(rng.random(count) < 0.3, rng.uniform(1, 90, count), 0.0)
    df = pd.DataFrame(
        {
            "vertice_id": np.arange(1000, 1000 + count),
            "connection_point": rng.integers(0, count // 3 + 1, count),
            "agg_connection_point": np.where(rng.random(count) < 0.2, np.nan, rng.integers(0, count // 3 + 1, count)),
            "residential_peak_load_in_kw": np.where(rng.random(count) < 0.1, np.nan, residential),
            "households": np.where(rng.random(count) < 0.1, np.nan, households),
            "nonresidential_peak_load_in_kw": nonresidential,
            "nonresidential_use": rng.choice(["Commercial", "Public"], count),
            "nonresidential_mv_direct": rng.choice(np.array([None, True, False], dtype=object), count),
        }
    )
    return df.set_index("vertice_id", drop=False)


@pytest.mark.parametrize("seed", range(5))
def test_coincident_loads_matches_simultaneous_peak_load_bit_for_bit(seed):
    rng = np.random.default_rng(seed)
    buildings = _buildings(rng, 400)
    loads = utils.CoincidentLoads(buildings, CONSUMER_CATEGORIES)
    nodes = sorted(set(utils.planning_nodes(buildings).dropna().astype(int)))
    queries = [[], nodes, nodes[:1], [10**9]]
    queries += [list(rng.choice(nodes, size=int(rng.integers(1, len(nodes))), replace=False)) for _ in range(200)]
    queries += [nodes[:end] for end in range(1, len(nodes), 7)]  # growing prefixes, as in feeder planning
    for query in queries:
        expected = utils.simultaneous_peak_load(buildings, CONSUMER_CATEGORIES, query)
        assert loads.simultaneous_peak_load(query) == expected
        assert type(loads.simultaneous_peak_load(query)) is type(expected)
    assert loads.simultaneous_peak_load(set(nodes)) == utils.simultaneous_peak_load(buildings, CONSUMER_CATEGORIES, nodes)


def test_coincident_loads_raises_invalid_use_only_when_selected():
    buildings = _buildings(np.random.default_rng(7), 30)
    buildings["nonresidential_peak_load_in_kw"] = 10.0
    buildings["nonresidential_mv_direct"] = False
    buildings.iloc[3, buildings.columns.get_loc("nonresidential_use")] = "Industrial"
    bad_node = utils.planning_nodes(buildings).iloc[3]
    other_nodes = sorted(set(utils.planning_nodes(buildings).dropna()) - {bad_node})
    loads = utils.CoincidentLoads(buildings, CONSUMER_CATEGORIES)

    assert loads.simultaneous_peak_load(other_nodes) == utils.simultaneous_peak_load(
        buildings, CONSUMER_CATEGORIES, other_nodes
    )
    with pytest.raises(ValueError, match="invalid nonresidential_use='Industrial'") as reference:
        utils.simultaneous_peak_load(buildings, CONSUMER_CATEGORIES, [bad_node])
    with pytest.raises(ValueError) as raised:
        loads.simultaneous_peak_load([bad_node])
    assert str(raised.value) == str(reference.value)
