"""The grid queries without a database: one connection per request, KPI rows of the requested grids only.

A fake cursor answers by the text of the query and records every statement.
"""
from __future__ import annotations

import pytest
from pylovo_api import db, diagnostics_data, grid_metrics, queries

GRID_KEYS = (
    "grid_result_id", "version_id", "plz", "kcid", "bcid", "kva", "unit_mva", "units", "s_mva", "description",
    "power_flow_status", "max_total_lv_voltage_drop_pu", "max_feeder_voltage_drop_pu", "max_service_voltage_drop_pu",
    "selected_max_feeder_voltage_drop_percent", "selected_max_service_voltage_drop_percent",
    "max_total_design_voltage_drop_percent", "feeder_voltage_drop_limit_met", "service_voltage_drop_limit_met",
    "service_voltage_upgraded_count", "long_service_connection_count", "station_osm_id", "station_comment",
    "station_osm", "station_lod2", "lon", "lat", "buildings", "households", "installed_kw", "loads", "p_mw",
    "feeder_km", "service_km", "lines", "buses", "feeders", "direct_connections", "no_branches", "max_trafo_dis",
    "avg_trafo_dis")


def grid_row(**values) -> dict:
    """One row of ``queries._grid_rows`` (every column ``_grid_record`` reads, empty unless given)."""
    return dict.fromkeys(GRID_KEYS) | {"grid_result_id": 7, "version_id": "1", "plz": 80803, "kcid": 1, "bcid": 1,
                                       "kva": 630} | values


class FakeCursor:
    """Answers with the rows of the first marker found in the statement (nothing otherwise)."""

    def __init__(self, answers: dict[str, list[dict]] | None = None):
        self.answers = answers or {}
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def _rows(self) -> list[dict]:
        sql = self.statements[-1][0]
        return next((rows for marker, rows in self.answers.items() if marker in sql), [])

    def fetchall(self):
        return [dict(row) for row in self._rows()]

    def fetchone(self):
        rows = self._rows()
        return dict(rows[0]) if rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def params_of(self, marker: str):
        return next(params for sql, params in self.statements if marker in sql)


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, **kwargs):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class Connections(list):
    """One entry per connection opened; ``cursor`` is the fake cursor all of them share."""

    cursor: FakeCursor


@pytest.fixture()
def connections(monkeypatch):
    opened = Connections()
    cursor = FakeCursor({"SELECT version_id, plz FROM pylovo.grid_result": [{"version_id": "1", "plz": 80803}],
                         "WITH grids AS": [grid_row()]})
    monkeypatch.setattr(db, "_connect", lambda readonly, **kwargs: opened.append(readonly) or FakeConnection(cursor))
    opened.cursor = cursor
    return opened


def test_grid_detail_uses_one_connection_and_computes_the_kpi_row_of_its_grid_only(connections, monkeypatch):
    monkeypatch.setattr(grid_metrics, "grid_extras", lambda detail, record, limits: {})
    detail = queries.grid_detail(7)
    assert detail["grid"]["grid_result_id"] == 7 and detail["grid"]["size_label"] == "630 kVA"
    assert len(connections) == 1
    kpi_sql = next(sql for sql, _ in connections.cursor.statements if "WITH grids AS" in sql)
    assert "g.grid_result_id = ANY(%(ids)s)" in kpi_sql and connections.cursor.params_of("WITH grids AS")["ids"] == [7]


def test_the_overview_map_uses_one_connection(connections):
    data = queries.overview_geojson("1", 80803)
    assert data["bounds"] is None and data["lines"]["features"] == [] and len(connections) == 1


def test_kpi_rows_can_be_limited_to_some_grids():
    cursor = FakeCursor()
    queries._grid_rows("1", [80803], [7, 8], cursor)
    assert cursor.statements[-1][1] == {"v": "1", "plz": [80803], "ids": [7, 8]}
    queries._grid_rows("1", None, None, cursor)
    assert cursor.statements[-1][1] == {"v": "1"} and "%(ids)s" not in cursor.statements[-1][0]
    queries._grid_rows("1", [80803], [], cursor)  # no grid asked for: no rows, not the whole PLZ
    assert cursor.statements[-1][1]["ids"] == [] and "%(ids)s" in cursor.statements[-1][0]


def test_diagnostics_compute_the_kpi_rows_of_the_requested_grids_only(monkeypatch):
    def head(gid, plz):
        return {"grid_result_id": gid, "version_id": "1", "plz": plz, "amp_feeder": None, "amp_service": None,
                "fp": f"md5-{gid}", "ext_grid": None, "settlement_type": 2}

    cursor = FakeCursor({"AS ext_grid": [head(7, 80803), head(8, 80803), head(9, 80804)]})
    asked = []

    def rows(version_id, plz, grid_ids=None, cur=None):
        asked.append((version_id, plz, grid_ids, cur))
        return [grid_row(grid_result_id=g, plz=plz[0]) for g in grid_ids]

    monkeypatch.setattr(queries, "_grid_rows", rows)
    inputs = diagnostics_data._load_chunk_with(cursor, [7, 8, 9], light=True)
    assert asked == [("1", [80803], [7, 8], cursor), ("1", [80804], [9], cursor)]  # not every grid of the PLZ
    assert sorted(inputs) == [7, 8, 9] and inputs[9]["grid"]["plz"] == 80804 and inputs[7]["fingerprint"] == "md5-7"
