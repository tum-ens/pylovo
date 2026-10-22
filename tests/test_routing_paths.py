from pylovo.cable_installer import CableInstaller
from pylovo.database.grid_mixin import GridMixin


def test_routing_rows_are_grouped_by_target_and_path_sequence():
    rows = [
        (20, 2, 30, 4.0),
        (10, 3, 99, 7.0),
        (10, 1, 10, 0.0),
        (20, 1, 20, 0.0),
        (10, 2, 30, 3.0),
        (20, 3, 99, 8.0),
    ]

    costs, paths = GridMixin._routing_results_from_rows(rows)

    assert costs == {10: 7.0, 20: 8.0}
    assert paths == {
        10: (10, 30, 99),
        20: (20, 30, 99),
    }


def test_routing_rows_handle_target_equal_to_transformer():
    costs, paths = GridMixin._routing_results_from_rows([(99, 1, 99, 0.0)])

    assert costs == {99: 0.0}
    assert paths == {99: (99,)}


def test_cable_installer_slices_cached_path_without_mutating_it():
    class Database:
        def get_path_to_bus(self, _start, _end):
            raise AssertionError("scalar routing fallback should not be used")

    installer = object.__new__(CableInstaller)
    installer.dbc = Database()
    installer._paths_to_transformer = {10: (10, 20, 30, 99)}

    path = installer._get_path_to_bus(10, 30)
    path.pop()

    assert path == [10, 20]
    assert installer._paths_to_transformer[10] == (10, 20, 30, 99)
    assert installer._get_path_to_bus(10, 99) == [10, 20, 30, 99]


def test_cable_installer_uses_scalar_fallback_for_uncached_pair():
    class Database:
        def __init__(self):
            self.calls = []

        def get_path_to_bus(self, start, end):
            self.calls.append((start, end))
            return [start, 77, end]

    database = Database()
    installer = object.__new__(CableInstaller)
    installer.dbc = database
    installer._paths_to_transformer = {10: (10, 20, 99)}

    assert installer._get_path_to_bus(10, 55) == [10, 77, 55]
    assert database.calls == [(10, 55)]
