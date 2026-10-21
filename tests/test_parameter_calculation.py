"""DB-free checks of the PLZ-level analysis queries of pylovo.analysis.parameter_calculation."""
import json
import logging
import re
from types import SimpleNamespace

from pylovo.analysis.parameter_calculation import ParameterCalculator


def test_basic_parameters_aggregate_only_the_grids_of_the_plz():
    """Unscoped GROUP BY subqueries aggregated whole pandapower tables (all versions) for every PLZ."""
    queries, stored = [], []

    class Cursor:
        def execute(self, query, params=None):
            queries.append(" ".join(query.split()))

        def fetchall(self):  # capacity, load_count, bus_count per grid
            return [(400, 12, 30), (400, 8, 20), (630, 30, 60)]

    calculator = object.__new__(ParameterCalculator)
    calculator.dbc = SimpleNamespace(cur=Cursor(), logger=logging.getLogger("parameter-calculation-test"),
                                     insert_plz_parameters=lambda *args: stored.append(args))
    calculator.analyze_basic_parameters_for_plz(80803)

    scope = " WHERE grid_result_id IN (SELECT grid_result_id FROM grid_scope)"
    aggregates = re.findall(r"FROM pylovo\.(pandapower_\w+)( WHERE [^)]*\))? GROUP BY grid_result_id", queries[0])
    assert aggregates == [(table, scope) for table in ("pandapower_load", "pandapower_bus")]
    assert stored == [(80803, json.dumps({400: 2, 630: 1}), json.dumps({400: [12, 8], 630: [30]}),
                       json.dumps({400: [30, 20], 630: [60]}))]
