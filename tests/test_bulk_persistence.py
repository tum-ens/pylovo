import logging

import pandas as pd
import pytest

import pylovo.database.grid_mixin as grid_mixin
from pylovo.cable_installer import CableInstaller


def test_cable_installer_uses_bulk_coordinates_without_scalar_fallback():
    class Backend:
        def get_bus_coordinates(self, _name):
            return None

    class Database:
        def get_node_geom(self, _node_id):
            raise AssertionError("scalar coordinate lookup should not be used")

    installer = CableInstaller(
        Backend(),
        Database(),
        logging.getLogger("bulk-installer-test"),
        [("NAYY_4_50", 0.642, 0.083, 0.142, 11)],
        pd.DataFrame(),
        pd.DataFrame(),
        node_coordinates={7: (11.1, 49.7)},
        context=(91301, 1, 2),
    )
    assert installer._get_line_node_coordinates(7) == (11.1, 49.7)
    with pytest.raises(ValueError, match="node_id=8.*plz=91301.*kcid=1.*bcid=2"):
        installer._get_line_node_coordinates(8)


def test_line_batch_passes_all_rows_and_page_size(monkeypatch):
    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchone(self):
            return (42,)

    class Client(grid_mixin.GridMixin):
        get_connection = lambda self: None
        get_logger = lambda self: logging.getLogger("grid-batch-test")
        get_sqla_engine = lambda self: None
        get_grid_result_id = lambda self, **kwargs: 42

    client = object.__new__(Client)
    client.cur = Cursor()
    calls = []

    def fake_execute_values(cur, query, values, **kwargs):
        calls.append((cur, query, values, kwargs))

    monkeypatch.setattr(grid_mixin, "execute_values", fake_execute_values)
    records = [
        {"geom": [(0, 0), (1, 1)], "line_name": f"L{i}", "std_type": "NAYY_4_50",
         "from_bus": i, "to_bus": i + 1, "length_km": 0.1, "parallel": 1,
         "feeder_section_id": i}
        for i in range(3)
    ]
    client.insert_lines_batch(records, plz=91301, kcid=1, bcid=2, page_size=2)
    assert len(calls) == 1
    assert len(calls[0][2]) == 3
    assert calls[0][3]["page_size"] == 2
    assert calls[0][2][0][4:6] == (0, 1)
