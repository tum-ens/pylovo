"""What the connected database holds: tables, columns and whether a few tables have rows.

One viewer serves very different databases (the paper InfDB with LoD2 but no MV results, the MV
test database with MV, eGon and OSM power data but no citydb, a fresh setup without any results).
The layer registry asks the catalog whether a layer's tables exist; a layer without its data is
listed as unavailable with the reason instead of failing.
"""
from __future__ import annotations

import time

from pylovo_viewer.db import Database

LOD2_CLASSES = ("Building", "RoofSurface", "WallSurface", "GroundSurface")


class Catalog:
    """Tables (``schema.table``) with their columns and types, probed at start-up and on request."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.columns: dict[str, dict[str, str]] = {}
        self.has_rows: dict[str, bool] = {}
        self.citydb_classes: dict[str, int] | None = None
        self.probed_at = 0.0
        self.refresh()

    def refresh(self) -> None:
        s = self.db.settings
        schemas = sorted({"pylovo", "citydb", s.source_schema, s.opendata_schema, s.osm_schema})
        columns: dict[str, dict[str, str]] = {}
        for row in self.db.query("""SELECT table_schema || '.' || table_name AS t, column_name AS c, udt_name AS u
                                    FROM information_schema.columns WHERE table_schema = ANY(%s)
                                    ORDER BY table_schema, table_name, ordinal_position""", (schemas,)):
            columns.setdefault(row["t"], {})[row["c"]] = row["u"]
        self.columns = columns
        checks = [t for t in self.row_checks() if t in columns]
        self.has_rows = {}
        if checks:
            sql = " UNION ALL ".join(f"SELECT '{t}' AS t, EXISTS (SELECT 1 FROM {t}) AS has" for t in checks)
            self.has_rows = {row["t"]: bool(row["has"]) for row in self.db.query(sql)}
        self.citydb_classes = None
        if self.has("citydb.objectclass", "classname", "id") and self.has("citydb.feature") \
                and self.has("citydb.property") and self.has("citydb.geometry_data"):
            rows = self.db.query("SELECT classname, id FROM citydb.objectclass WHERE classname = ANY(%s)",
                                 (list(LOD2_CLASSES),))
            classes = {row["classname"]: int(row["id"]) for row in rows}
            if "Building" in classes and self.has_rows.get("citydb.feature"):
                self.citydb_classes = classes
        self.probed_at = time.time()

    def row_checks(self) -> list[str]:
        """Tables whose emptiness decides whether a layer is offered."""
        s = self.db.settings
        return ["pylovo.version", "pylovo.mv_variant", "pylovo.transformer_classified", "pylovo.mv_customers",
                "citydb.feature", f"{s.osm_schema}.infrastructure_line", f"{s.osm_schema}.infrastructure_polygon",
                f"{s.opendata_schema}.egon_hvmv_substation", f"{s.opendata_schema}.egon_mv_grid_district"]

    def has(self, table: str, *columns: str) -> bool:
        cols = self.columns.get(table)
        return cols is not None and all(c in cols for c in columns)

    def missing(self, requirement: str) -> str | None:
        """Why ``requirement`` (``schema.table`` or ``schema.table(col, col)``) is not met, or ``None``."""
        table, _, rest = requirement.partition("(")
        table = table.strip()
        if table not in self.columns:
            return f"no table {table} in this database"
        wanted = [c.strip() for c in rest.rstrip(")").split(",") if c.strip()]
        absent = [c for c in wanted if c not in self.columns[table]]
        if absent:
            return f"{table} has no column {', '.join(absent)}"
        return None

    def geometry_columns(self, table: str) -> set[str]:
        return {c for c, udt in self.columns.get(table, {}).items() if udt in ("geometry", "geography")}

    def summary(self) -> dict:
        return {"tables": len(self.columns), "rows": self.has_rows, "lod2": self.citydb_classes is not None,
                "probed_at": self.probed_at}
