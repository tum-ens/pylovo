"""The PLZ-wide parameter queries fix the order of the per-transformer lists and of the cable length sums (no database)."""

import logging

from pylovo.analysis.parameter_calculation import ParameterCalculator


class Cursor:
    def __init__(self, rows):
        self.rows, self.statements = rows, []

    def execute(self, query, params=None):
        self.statements.append(" ".join(query.split()))

    def fetchall(self):
        return self.rows


class Dbc:
    def __init__(self, rows):
        self.cur, self.logger, self.stored = Cursor(rows), logging.getLogger("plz-parameters-order-test"), {}

    def insert_plz_parameters(self, plz, trafo, load_count, bus_count):
        self.stored["basic"] = (trafo, load_count, bus_count)

    def insert_cable_length(self, plz, cable_length):
        self.stored["cable"] = cable_length


def calculator(rows) -> ParameterCalculator:
    instance = ParameterCalculator.__new__(ParameterCalculator)
    instance.dbc = Dbc(rows)
    return instance


def test_lists_per_transformer_size_follow_the_grid_key():
    instance = calculator([(250, 40, 80, 1.5), (250, 12, 30, 0.7), (400, 55, 99, 2.0)])
    instance.analyze_basic_parameters_for_plz(91301)
    query = instance.dbc.cur.statements[0]
    # grids of one size would otherwise come in the physical row order of the database
    assert "SELECT grid_result_id, kcid, bcid FROM pylovo.grid_result" in query
    assert query.endswith("ORDER BY capacity, gs.kcid, gs.bcid")
    assert instance.dbc.stored["basic"] == (
        '{"250": 2, "400": 1}', '{"250": [40, 12], "400": [55]}', '{"250": [80, 30], "400": [99]}')


def test_cable_lengths_are_summed_in_the_order_of_grid_key_and_line_index():
    instance = calculator([("NAYY_4_150", 1.25), ("NAYY_4_50", 0.5)])
    instance.analyze_cable_lengths_for_plz(91301)
    query = instance.dbc.cur.statements[0]
    # a float sum in another order differs in the last digit between databases
    assert "SUM(COALESCE(pl.parallel, 1) * COALESCE(pl.length_km, 0.0) ORDER BY gr.kcid, gr.bcid, pl.pp_index)" in query
    assert instance.dbc.stored["cable"] == '{"NAYY_4_150": 1.25, "NAYY_4_50": 0.5}'
