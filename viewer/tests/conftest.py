"""A fake read-only database for the viewer tests: canned answers by query, every query recorded."""
from __future__ import annotations

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
}
GEOMETRY = {"geom", "centroid"}


class FakeDB:
    """Answers the queries the viewer sends, from the tables above."""

    def __init__(self) -> None:
        self.settings = Settings(dbname="fake", user="u", host="localhost", port=5432, password="secret",
                                 source_schema="basedata")
        self.calls: list[tuple[str, Any]] = []
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def query(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if "information_schema.columns" in sql:
            return [{"t": t, "c": c, "u": "geometry" if c in GEOMETRY else "text"} for t, cols in TABLES.items()
                    for c in cols]
        if "AS has" in sql:
            return [{"t": t, "has": True} for t in re.findall(r"'([a-z_]+\.[a-z_]+)' AS t", sql)]
        for marker, rows in self.rows.items():
            if marker in sql:
                return [dict(r) for r in rows]
        return []

    def one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None


@pytest.fixture()
def fake_db() -> FakeDB:
    return FakeDB()
