"""A fake read-only database server for the viewer tests: canned answers by query, every query recorded."""
from __future__ import annotations

import dataclasses
import re
from typing import Any

import pytest
from pylovo_viewer.settings import Settings

TABLES = {
    "pylovo.version": ["version_id", "version_comment", "created_at"],
    "pylovo.grid_result": ["grid_result_id", "version_id", "kcid", "bcid", "plz", "transformer_rated_power",
                           "power_flow_status", "ont_vertice_id", "grid"],
    "pylovo.transformer_positions": ["grid_result_id", "geom", "osm_id", "osm", "lod2"],
    "pylovo.lines_result_cache": ["id", "grid_result_id", "geom", "std_type", "parallel", "length_km",
                                  "feeder_section_id", "version_id", "from_bus", "to_bus"],
    "pylovo.pandapower_line": ["grid_result_id", "name"],
    "pylovo.buildings_result": ["version_id", "objectid", "grid_result_id", "geom", "centroid", "type"],
    "pylovo.postcode_result": ["version_id", "postcode_result_plz", "geom"],
    "pylovo.postcode": ["plz", "note", "geom"],
    "pylovo.split_points": ["split_point_id", "grid_result_id", "split_bus", "outgoing_count", "geom"],
}
GEOMETRY = {"geom", "centroid"}


class FakeDB:
    """Answers the queries the viewer sends, from ``tables`` (default: the LV tables above)."""

    def __init__(self, settings: Settings | None = None, tables: dict[str, list[str]] | None = None,
                 server: list[str] | None = None) -> None:
        self.settings = settings or Settings(dbname="fake", user="u", host="localhost", port=5432, password="secret",
                                             source_schema="basedata")
        self.tables = TABLES if tables is None else tables
        self.server = server if server is not None else [self.settings.dbname]   # databases in pg_database
        self.calls: list[tuple[str, Any]] = []
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.errors: dict[str, Exception] = {}   # raised for a query that contains the key
        self.closed = False

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        for marker, error in self.errors.items():
            if marker in sql:
                raise error
        if "information_schema.columns" in sql:
            return [{"t": t, "c": c, "u": "geometry" if c in GEOMETRY else "text"} for t, cols in self.tables.items()
                    for c in cols]
        if "FROM pg_database" in sql:
            return [{"datname": name} for name in self.server]
        if "table_name = 'version'" in sql:
            return [{"pylovo": "pylovo.version" in self.tables}]
        if "AS has" in sql:
            return [{"t": t, "has": True} for t in re.findall(r"'([a-z_]+\.[a-z_]+)' AS t", sql)]
        for marker, rows in self.rows.items():
            if marker in sql:
                return [dict(r) for r in rows]
        return []

    def one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        self.closed = True


class FakeServer:
    """Several fake databases on one server: ``connect`` opens the one its settings name."""

    LV = TABLES

    def __init__(self, databases: dict[str, dict[str, list[str]]], default: str) -> None:
        self.tables, self.opened = databases, []
        self.rows: dict[str, dict[str, list[dict[str, Any]]]] = {}   # canned rows of the databases opened later
        self.default = FakeDB(dataclasses.replace(FakeDB().settings, dbname=default), databases[default], list(databases))

    def connect(self, settings: Settings) -> FakeDB:
        db = FakeDB(settings, self.tables.get(settings.dbname, {}), self.default.server)
        db.rows = self.rows.get(settings.dbname, {})
        self.opened.append(db)
        return db


@pytest.fixture()
def fake_db() -> FakeDB:
    return FakeDB()


@pytest.fixture()
def fake_server() -> type[FakeServer]:
    return FakeServer
