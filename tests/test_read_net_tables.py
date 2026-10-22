"""read_net_db(..., tables=...) deserializes only the requested tables, exactly as the full read."""

import json
import logging

import pandapower as pp

from pylovo.database.analysis_mixin import AnalysisMixin


class _Cursor:
    def __init__(self, grid):
        self.grid = grid

    def execute(self, query, vars=None):
        pass

    def fetchall(self):
        return [(json.loads(self.grid),)]  # psycopg2 returns the json column parsed


def test_pruned_read_gives_the_requested_tables_of_the_full_read():
    net = pp.create_empty_network()
    buses = [pp.create_bus(net, vn_kv=0.4, name=f"b{i}", geodata=(11.0 + i / 3, 48.1)) for i in range(3)]
    pp.create_line(net, buses[0], buses[1], length_km=0.1 / 3, std_type="NAYY 4x150 SE")
    pp.create_load(net, buses[2], p_mw=0.01 / 7, category="Residential")
    client = object.__new__(AnalysisMixin)
    client.cur, client.logger = _Cursor(pp.to_json(net)), logging.getLogger("read-net-test")

    full = client.read_net_db(1, 1, 1)
    pruned = client.read_net_db(1, 1, 1, tables=frozenset({"bus", "load"}))
    assert pruned.bus.equals(full.bus) and pruned.load.equals(full.load)
    assert pruned.line.empty and len(full.line) == 1
