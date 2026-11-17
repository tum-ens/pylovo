"""The generation takes its load parameters from the configuration, never from ``pylovo.consumer_categories``.

That table is rewritten by every run on the database, so a table read would mix the parameters of runs with
different ``VERSION_ID`` (no database).
"""
import numpy as np
import pandas as pd
import pytest

from pylovo import config_loader, grid_generator, utils
from pylovo.database.database_client import DatabaseClient
from pylovo.grid_generator import GridGenerator


class FakeDb:
    """The only database calls of ``GridGenerator.__init__``; any other call fails the test."""

    def __init__(self, *args, **kwargs):
        pass

    def insert_version_if_not_exists(self):
        pass

    def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError(f"GridGenerator.__init__ called dbc.{name}")


@pytest.fixture()
def generator(monkeypatch, tmp_path):
    monkeypatch.setattr(grid_generator.dbc, "DatabaseClient", FakeDb)
    monkeypatch.setattr(grid_generator, "InfdbClient", lambda **kwargs: None)
    return GridGenerator(plz=80803, log_file=str(tmp_path / "log.txt"))


def table_frame():
    """The frame ``get_consumer_categories`` returned: the table rows, float columns, indexed by definition."""
    config = config_loader.CONSUMER_CATEGORIES
    expected = config.astype({"consumer_category_id": "int64", **{
        column: "float64" for column in ("peak_load", "yearly_consumption", "peak_load_per_m2",
                                         "yearly_consumption_per_m2", "sim_factor")}})
    return expected.set_index("definition", drop=False).sort_index()


def test_the_generator_frame_has_the_shape_of_the_former_table_read(generator):
    frame = generator.consumer_categories
    pd.testing.assert_frame_equal(frame, table_frame())
    assert list(frame.index) == sorted(frame["definition"]) and frame.index.name == "definition"
    assert frame.loc["Residential", "peak_load"] == config_loader.PEAK_LOAD_HOUSEHOLD
    assert np.isnan(frame.loc["Commercial", "peak_load"])


def test_the_load_functions_get_the_same_numbers_as_from_the_table(generator):
    buildings = pd.DataFrame({
        "vertice_id": [1, 2, 3], "connection_point": [1, 2, 3], "households": [20.0, 1.0, 1.0],
        "residential_peak_load_in_kw": [336.5, 16.825, 0.0], "nonresidential_peak_load_in_kw": [0.0, 20.0, 50.0],
        "nonresidential_use": [None, "Commercial", "Public"], "nonresidential_mv_direct": [None, None, None],
    })
    for nodes in ([1], [1, 2], [1, 2, 3]):
        assert (utils.simultaneous_peak_load(buildings, generator.consumer_categories, nodes)
                == utils.simultaneous_peak_load(buildings, table_frame(), nodes))


def test_the_generation_has_no_table_reader_for_the_categories():
    assert not hasattr(DatabaseClient, "get_consumer_categories")
    source = open(grid_generator.__file__, encoding="utf-8").read()
    assert "get_consumer_categories" not in source
