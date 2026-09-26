"""Queries of manual load edits (:mod:`pylovo.load_editing`) and their audit table ``pylovo.load_edit``.

All methods are keyed by ``grid_result_id`` and work for any version (they never use the
configured ``VERSION_ID``). Like the other mixins they run in the current transaction and do not
commit, except :meth:`LoadEditMixin.ensure_load_edit_schema`.

:meth:`LoadEditMixin.replace_pandapower_load_rows` uses ``AnalysisMixin._insert_pandapower_load_rows``,
so the class composing this mixin must also include ``AnalysisMixin`` (``DatabaseClient`` does).
"""
from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import Json

from pylovo.database.base_mixin import BaseMixin

_BUILDING_COLUMNS = (
    "objectid", "vertice_id", "type", "building_use", "street", "house_number", "floor_area", "floor_number",
    "households", "residential_floor_area", "nonresidential_floor_area", "nonresidential_use",
    "residential_peak_load_in_kw", "nonresidential_peak_load_in_kw", "nonresidential_mv_direct", "peak_load_in_kw",
)
_LOAD_COLUMNS = (
    "grid_result_id", "pp_index", "name", "bus", "p_mw", "q_mvar", "service_design_p_mw", "operating_point_basis",
    "category", "load_units", "consumer_vertex", "const_z_percent", "const_i_percent", "sn_mva", "scaling",
    "in_service", "type", "controllable", "max_p_mw", "min_p_mw", "max_q_mvar", "min_q_mvar",
)
_EDITABLE = ("households", "residential_floor_area", "nonresidential_floor_area", "nonresidential_use",
             "residential_peak_load_in_kw", "nonresidential_peak_load_in_kw", "nonresidential_mv_direct",
             "peak_load_in_kw")
_DROP_COLUMNS = ("max_feeder_voltage_drop_pu", "max_service_voltage_drop_pu", "max_total_lv_voltage_drop_pu")
_JSON_COLUMNS = ("changes", "before_building", "after_building", "before_grid", "after_grid", "before_loads",
                 "before_bus_zones", "removed_analysis", "impact", "parameters", "reproduction")
_ANALYSIS_TABLES = (  # load-dependent analysis rows an edit removes (and an exact undo restores)
    ("plz_parameters", "version_id = %(v)s AND plz = %(plz)s"),
    ("clustering_parameters", "grid_result_id = %(g)s"),
    ("grid_parameters", "grid_result_id = %(g)s"),
)


def _dumps(value: Any) -> str:
    return json.dumps(value, allow_nan=False)


class LoadEditMixin(BaseMixin):
    """Read and write the data of one grid for a load edit, and keep its audit rows."""

    # ------------------------------------------------------------------ helpers
    def _le_rows(self, query: str, params: Any = None) -> list[dict]:
        self.cur.execute(query, params)
        if self.cur.description is None:
            return []
        names = [d[0] for d in self.cur.description]
        return [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in self.cur.fetchall()]

    def _le_row(self, query: str, params: Any = None) -> dict | None:
        rows = self._le_rows(query, params)
        return rows[0] if rows else None

    def _le_regclass(self, name: str) -> bool:
        return self._le_row("SELECT to_regclass(%s) IS NOT NULL AS ok", (name,))["ok"]

    def load_edit_table_exists(self) -> bool:
        """Whether ``pylovo.load_edit`` exists (databases set up before load editing lack it)."""
        return self._le_regclass("pylovo.load_edit")

    def ensure_load_edit_schema(self, lock_timeout_ms: int = 3000) -> None:
        """Create ``pylovo.load_edit`` if missing, in its own transaction, and commit.

        Its foreign key takes a SHARE ROW EXCLUSIVE lock on ``grid_result``, so the statement waits
        at most ``lock_timeout_ms`` behind a running generation transaction.

        Raises:
            psycopg2.errors.LockNotAvailable: If ``grid_result`` stayed locked.
            psycopg2.errors.InsufficientPrivilege: If the user may not create tables in ``pylovo``.
        """
        from pylovo.database.config_table_structure import CREATE_QUERIES

        self.conn.rollback()
        try:
            if self.load_edit_table_exists():
                self.conn.rollback()
                return
            self.cur.execute("SET LOCAL lock_timeout = %s", (f"{int(lock_timeout_ms)}ms",))
            self.cur.execute(CREATE_QUERIES["load_edit"])
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ grid and buildings
    def fetch_load_edit_grid(self, grid_result_id: int, with_net: bool = True) -> dict | None:
        """Head of a grid: identifiers, stored validation columns, md5 of the network JSON, the
        latest audit row id and the version's ``generation_parameters``; with ``with_net`` also the
        network JSON text (``grid_text``)."""
        last = ("(SELECT max(e.load_edit_id) FROM pylovo.load_edit e WHERE e.grid_result_id = g.grid_result_id)"
                if self.load_edit_table_exists() else "NULL::bigint")
        return self._le_row(
            f"""SELECT g.grid_result_id, g.version_id, g.plz, g.kcid, g.bcid, g.transformer_rated_power,
                       g.power_flow_status, g.max_feeder_voltage_drop_pu, g.max_service_voltage_drop_pu,
                       g.max_total_lv_voltage_drop_pu, {'g.grid::text' if with_net else 'NULL::text'} AS grid_text,
                       md5(g.grid::text) AS grid_md5, {last} AS last_edit_id, v.generation_parameters
                FROM pylovo.grid_result g JOIN pylovo.version v USING (version_id)
                WHERE g.grid_result_id = %s""", (grid_result_id,))

    def lock_load_edit_grid(self, grid_result_id: int) -> dict | None:
        """Lock the grid row (``FOR NO KEY UPDATE NOWAIT``) and return its md5 and latest audit row id.

        Raises:
            psycopg2.errors.LockNotAvailable: If another transaction holds the row.
        """
        return self._le_row(
            """SELECT md5(g.grid::text) AS grid_md5,
                      (SELECT max(e.load_edit_id) FROM pylovo.load_edit e
                        WHERE e.grid_result_id = g.grid_result_id) AS last_edit_id
               FROM pylovo.grid_result g WHERE g.grid_result_id = %s
               FOR NO KEY UPDATE OF g NOWAIT""", (grid_result_id,))

    def fetch_load_edit_buildings(self, version_id: str, grid_result_id: int) -> list[dict]:
        """The ``buildings_result`` rows of a grid with the columns a load edit needs."""
        return self._le_rows(f"SELECT {', '.join(_BUILDING_COLUMNS)} FROM pylovo.buildings_result "
                          "WHERE version_id = %s AND grid_result_id = %s", (version_id, grid_result_id))

    def fetch_load_edit_building(self, version_id: str, grid_result_id: int, objectid: str,
                                 lock: bool = False) -> dict | None:
        """One building of a grid (``FOR UPDATE`` when ``lock``)."""
        return self._le_row(f"SELECT {', '.join(_BUILDING_COLUMNS)} FROM pylovo.buildings_result "
                         "WHERE version_id = %s AND grid_result_id = %s AND objectid = %s"
                         + (" FOR UPDATE" if lock else ""), (version_id, grid_result_id, objectid))

    def fetch_pandapower_loads(self, grid_result_id: int) -> list[dict]:
        """``pandapower_load`` rows of a grid ordered by ``pp_index``."""
        return self._le_rows(f"SELECT {', '.join(_LOAD_COLUMNS)} FROM pylovo.pandapower_load "
                          "WHERE grid_result_id = %s ORDER BY pp_index", (grid_result_id,))

    def fetch_pandapower_consumer_buses(self, grid_result_id: int) -> list[dict]:
        """``pp_index``, ``name`` and ``zone`` of the consumer buses of a grid."""
        return self._le_rows("SELECT pp_index, name, zone FROM pylovo.pandapower_bus "
                          "WHERE grid_result_id = %s AND name LIKE 'Consumer Nodebus %%' ORDER BY pp_index",
                          (grid_result_id,))

    def count_pandapower_elements(self, grid_result_id: int) -> dict[str, int]:
        """Row counts of ``pandapower_bus``, ``_line`` and ``_trafo`` of a grid."""
        return self._le_row(
            """SELECT (SELECT count(*) FROM pylovo.pandapower_bus WHERE grid_result_id = %(g)s) AS bus,
                      (SELECT count(*) FROM pylovo.pandapower_line WHERE grid_result_id = %(g)s) AS line,
                      (SELECT count(*) FROM pylovo.pandapower_trafo WHERE grid_result_id = %(g)s) AS trafo""",
            {"g": grid_result_id})

    # ------------------------------------------------------------------ writes of an edit
    def update_building_load_inputs(self, version_id: str, grid_result_id: int, objectid: str,
                                    values: dict[str, Any]) -> None:
        """Set the load inputs and component peaks of one building.

        Raises:
            RuntimeError: If not exactly one row was updated.
        """
        columns = [k for k in _EDITABLE if k in values]
        self.cur.execute(
            f"UPDATE pylovo.buildings_result SET {', '.join(f'{c} = %({c})s' for c in columns)} "
            "WHERE version_id = %(v)s AND grid_result_id = %(g)s AND objectid = %(o)s",
            {**{c: values[c] for c in columns}, "v": version_id, "g": grid_result_id, "o": objectid})
        if self.cur.rowcount != 1:
            raise RuntimeError(f"expected to update 1 building row, updated {self.cur.rowcount}")

    def snapshot_pandapower_loads(self, grid_result_id: int) -> list[dict]:
        """The ``pandapower_load`` rows of a grid as JSON objects (without the serial id)."""
        return self._le_row(
            """SELECT COALESCE(jsonb_agg(to_jsonb(l) - 'pandapower_load_id' ORDER BY l.pp_index), '[]'::jsonb) AS rows
               FROM pylovo.pandapower_load l WHERE l.grid_result_id = %s""", (grid_result_id,))["rows"]

    def replace_pandapower_load_rows(self, grid_result_id: int, load_df) -> None:
        """Replace the ``pandapower_load`` rows of a grid by ``net.load`` (as ``save_pandapower_net_with_sql``)."""
        self.cur.execute("DELETE FROM pylovo.pandapower_load WHERE grid_result_id = %s", (grid_result_id,))
        self._insert_pandapower_load_rows(grid_result_id, load_df)

    def restore_pandapower_load_rows(self, grid_result_id: int, rows: list[dict]) -> None:
        """Replace the ``pandapower_load`` rows of a grid by rows from :meth:`snapshot_pandapower_loads`."""
        self.cur.execute("DELETE FROM pylovo.pandapower_load WHERE grid_result_id = %s", (grid_result_id,))
        columns = ", ".join(_LOAD_COLUMNS)
        self.cur.execute(
            f"""INSERT INTO pylovo.pandapower_load ({columns})
                SELECT {columns} FROM jsonb_populate_recordset(NULL::pylovo.pandapower_load, %s::jsonb)""",
            (_dumps(rows),))

    def update_pandapower_bus_zones(self, grid_result_id: int, zones: list[tuple[int, str | None]]) -> None:
        """Set the ``zone`` of consumer buses: ``[(pp_index, zone)]``."""
        for pp_index, zone in zones:
            self.cur.execute("UPDATE pylovo.pandapower_bus SET zone = %s WHERE grid_result_id = %s AND pp_index = %s",
                             (zone, grid_result_id, int(pp_index)))

    def save_revalidated_grid(self, grid_result_id: int, json_text: str, power_flow_status: str,
                              drops: dict[str, float | None]) -> str:
        """Store the edited network and its validation power flow; return ``md5(grid::text)``.

        The design-diagnostic columns (cable planning) are left as generated.
        """
        row = self._le_row(
            f"""UPDATE pylovo.grid_result SET grid = %(grid)s, power_flow_status = %(status)s,
                       {', '.join(f'{c} = %({c})s' for c in _DROP_COLUMNS)}
                WHERE grid_result_id = %(g)s RETURNING md5(grid::text) AS md5""",
            {"grid": json_text, "status": power_flow_status, "g": grid_result_id,
             **{c: drops.get(c) for c in _DROP_COLUMNS}})
        return row["md5"]

    def restore_grid_from_load_edit(self, load_edit_id: int) -> str:
        """Put back the network JSON (verbatim) and the validation columns stored before an edit."""
        row = self._le_row(
            f"""UPDATE pylovo.grid_result g SET grid = e.before_net,
                       power_flow_status = e.before_grid->>'power_flow_status',
                       {', '.join(f"{c} = (e.before_grid->>'{c}')::float8" for c in _DROP_COLUMNS)}
                FROM pylovo.load_edit e
                WHERE e.load_edit_id = %s AND g.grid_result_id = e.grid_result_id
                RETURNING md5(g.grid::text) AS md5""", (load_edit_id,))
        return row["md5"] if row else None

    # ------------------------------------------------------------------ derived analysis rows
    def load_dependent_analysis_rows(self, version_id: str, plz: int, grid_result_id: int) -> dict[str, Any]:
        """Which analysis rows an edit of the grid would remove (and how many classification rows it outdates)."""
        params = {"v": version_id, "plz": plz, "g": grid_result_id}
        out: dict[str, Any] = {}
        for table, where in _ANALYSIS_TABLES:
            out[table] = bool(self._le_regclass(f"pylovo.{table}")
                              and self._le_row(f"SELECT EXISTS (SELECT 1 FROM pylovo.{table} WHERE {where}) AS e", params)["e"])
        out["classification_rows"] = (self._le_row("SELECT count(*) AS n FROM pylovo.transformer_classified "
                                                "WHERE grid_result_id = %(g)s", params)["n"]
                                      if self._le_regclass("pylovo.transformer_classified") else 0)
        return out

    def active_load_edit_token(self, version_id: str, plz: int) -> str:
        """md5 of the ordered ids of the active (not undone) edits of a PLZ and version."""
        return self._le_row(
            """SELECT md5(COALESCE(string_agg(load_edit_id::text, ',' ORDER BY load_edit_id), '')) AS token
               FROM pylovo.load_edit WHERE version_id = %s AND plz = %s AND undone_at IS NULL""",
            (version_id, plz))["token"]

    def capture_load_dependent_analysis(self, version_id: str, plz: int, grid_result_id: int,
                                        grid_token: str) -> dict[str, Any]:
        """Copy and delete the analysis rows that depend on the loads of the grid.

        pylovo-analyze skips existing rows, so a deleted row is recomputed by the next run
        instead of surviving with pre-edit numbers. The copies are stored in the audit row with
        two tokens (the grid's md5 and the active edits of the PLZ before the edit), so an undo
        restores them only when the networks they came from are back.

        Returns:
            ``{table: row as JSON | None, 'plz_token': ..., 'grid_token': ...}``.
        """
        params = {"v": version_id, "plz": plz, "g": grid_result_id}
        out: dict[str, Any] = {"plz_token": self.active_load_edit_token(version_id, plz), "grid_token": grid_token}
        for table, where in _ANALYSIS_TABLES:
            if not self._le_regclass(f"pylovo.{table}"):
                out[table] = None
                continue
            row = self._le_row(f"SELECT to_jsonb(t) AS j FROM pylovo.{table} t WHERE {where}", params)
            out[table] = row["j"] if row else None
            if row:
                self.cur.execute(f"DELETE FROM pylovo.{table} WHERE {where}", params)
        return out

    def restore_load_dependent_analysis(self, version_id: str, plz: int, grid_result_id: int) -> dict[str, bool]:
        """After an undo: delete analysis rows computed on the edited state and restore valid copies.

        A copied ``plz_parameters`` row is valid when the active edits of the PLZ are the ones at
        capture time; a copied grid row is valid when the grid's md5 equals the md5 at capture time.
        """
        params = {"v": version_id, "plz": plz, "g": grid_result_id}
        for table, where in _ANALYSIS_TABLES:
            if self._le_regclass(f"pylovo.{table}"):
                self.cur.execute(f"DELETE FROM pylovo.{table} WHERE {where}", params)
        token = self.active_load_edit_token(version_id, plz)
        grid_md5 = self._le_row("SELECT md5(grid::text) AS m FROM pylovo.grid_result WHERE grid_result_id = %s",
                             (grid_result_id,))["m"]
        restored: dict[str, bool] = {}
        for table, _ in _ANALYSIS_TABLES:
            restored[table] = False
            if not self._le_regclass(f"pylovo.{table}"):
                continue
            if table == "plz_parameters":
                match = ("version_id = %(v)s AND plz = %(plz)s AND removed_analysis->>'plz_token' = %(t)s", token)
            else:
                match = ("grid_result_id = %(g)s AND removed_analysis->>'grid_token' = %(t)s", grid_md5)
            row = self._le_row(
                f"""SELECT removed_analysis->'{table}' AS j FROM pylovo.load_edit
                    WHERE {match[0]} AND jsonb_typeof(removed_analysis->'{table}') = 'object'
                    ORDER BY load_edit_id DESC LIMIT 1""", {**params, "t": match[1]})
            if row:
                self.cur.execute(f"INSERT INTO pylovo.{table} SELECT * FROM jsonb_populate_record(NULL::pylovo.{table}, %s::jsonb)",
                                 (_dumps(row["j"]),))
                restored[table] = True
        return restored

    # ------------------------------------------------------------------ audit rows
    def insert_load_edit(self, row: dict[str, Any]) -> int:
        """Insert an audit row and return its id.

        ``before_net`` is copied verbatim (in SQL) from the grid's current network JSON, so call
        this before :meth:`save_revalidated_grid` and set the md5 of the new network afterwards
        with :meth:`set_load_edit_after_md5`.
        """
        values = {k: Json(row[k], dumps=_dumps) if k in _JSON_COLUMNS else row.get(k)
                  for k in ("version_id", "grid_result_id", "plz", "objectid", "action", "reason", "before_net_md5",
                            "client", *_JSON_COLUMNS)}
        columns = list(values)
        result = self._le_row(
            f"""INSERT INTO pylovo.load_edit ({', '.join(columns)}, before_net, after_net_md5)
                SELECT {', '.join(f'%({c})s' for c in columns)}, g.grid, repeat('0', 32)
                FROM pylovo.grid_result g WHERE g.grid_result_id = %(grid_result_id)s
                RETURNING load_edit_id""", values)
        return int(result["load_edit_id"])

    def set_load_edit_after_md5(self, load_edit_id: int, after_net_md5: str) -> None:
        """Store the md5 that ``save_revalidated_grid`` returned."""
        self.cur.execute("UPDATE pylovo.load_edit SET after_net_md5 = %s WHERE load_edit_id = %s",
                         (after_net_md5, load_edit_id))

    def fetch_load_edit(self, load_edit_id: int) -> dict | None:
        """One audit row without ``before_net``."""
        return self._le_row(
            """SELECT load_edit_id, version_id, grid_result_id, plz, objectid, action, changes, reason, before_building,
                      after_building, before_grid, after_grid, before_loads, before_bus_zones, before_net_md5,
                      after_net_md5, removed_analysis, created_at, undone_at, undone_by
               FROM pylovo.load_edit WHERE load_edit_id = %s""", (load_edit_id,))

    def mark_load_edit_undone(self, load_edit_id: int, undone_by: str) -> None:
        self.cur.execute("UPDATE pylovo.load_edit SET undone_at = clock_timestamp(), undone_by = %s "
                         "WHERE load_edit_id = %s AND undone_at IS NULL", (undone_by, load_edit_id))
        if self.cur.rowcount != 1:
            raise RuntimeError(f"load edit {load_edit_id} is already undone")

    def latest_active_load_edit(self, grid_result_id: int) -> dict | None:
        return self._le_row("SELECT load_edit_id FROM pylovo.load_edit WHERE grid_result_id = %s AND undone_at IS NULL "
                         "ORDER BY load_edit_id DESC LIMIT 1", (grid_result_id,))

    def first_load_edit_of_building(self, version_id: str, objectid: str) -> dict | None:
        """The earliest audit row of a building (its ``before_building`` holds the generated inputs)."""
        if not self.load_edit_table_exists():
            return None
        return self._le_row("SELECT load_edit_id, before_building FROM pylovo.load_edit WHERE version_id = %s "
                         "AND objectid = %s ORDER BY load_edit_id LIMIT 1", (version_id, objectid))

    def generated_net_md5(self, grid_result_id: int) -> str | None:
        """md5 of the network JSON as generated (the ``before_net_md5`` of the grid's first edit)."""
        row = self._le_row("SELECT before_net_md5 FROM pylovo.load_edit WHERE grid_result_id = %s "
                        "ORDER BY load_edit_id LIMIT 1", (grid_result_id,))
        return row["before_net_md5"] if row else None

    def version_has_load_edits(self, version_id: str) -> bool:
        if not self.load_edit_table_exists():
            return False
        return self._le_row("SELECT EXISTS (SELECT 1 FROM pylovo.load_edit WHERE version_id = %s) AS e",
                         (version_id,))["e"]

    def load_edit_history(self, grid_result_id: int | None = None, version_id: str | None = None,
                          plz: int | None = None) -> list[dict]:
        """Audit rows (newest first) of a grid or a version, with ``undoable`` and ``undo_blocked_by``."""
        if not self.load_edit_table_exists():
            return []
        where, params = [], {}
        if grid_result_id is not None:
            where.append("e.grid_result_id = %(g)s")
            params["g"] = grid_result_id
        if version_id is not None:
            where.append("e.version_id = %(v)s")
            params["v"] = version_id
        if plz is not None:
            where.append("e.plz = %(plz)s")
            params["plz"] = plz
        rows = self._le_rows(
            f"""SELECT e.load_edit_id, e.version_id, e.grid_result_id, e.plz, g.kcid, g.bcid, e.objectid, e.action,
                       e.changes, e.reason, e.before_grid, e.after_grid, e.before_building, e.after_building,
                       e.removed_analysis - 'plz_token' - 'grid_token' AS removed_analysis,
                       e.impact->'loads'->'changed' AS loads_changed, e.db_user, e.client, e.created_at,
                       e.undone_at, e.undone_by, br.street, br.house_number, br.type,
                       (SELECT max(x.load_edit_id) FROM pylovo.load_edit x
                         WHERE x.grid_result_id = e.grid_result_id AND x.undone_at IS NULL) AS latest_active
                FROM pylovo.load_edit e
                JOIN pylovo.grid_result g ON g.grid_result_id = e.grid_result_id
                LEFT JOIN pylovo.buildings_result br ON br.version_id = e.version_id AND br.objectid = e.objectid
                {'WHERE ' + ' AND '.join(where) if where else ''}
                ORDER BY e.load_edit_id DESC""", params)
        for row in rows:
            latest = row.pop("latest_active")
            row["address"] = " ".join(p for p in (row.pop("street"), row.pop("house_number")) if p) or None
            row["removed_analysis"] = {k: v is not None for k, v in (row["removed_analysis"] or {}).items()}
            row["undoable"] = row["undone_at"] is None and latest == row["load_edit_id"]
            row["undo_blocked_by"] = latest if row["undone_at"] is None and latest != row["load_edit_id"] else None
        return rows

    def load_edit_grid_summary(self, version_id: str | None = None, grid_ids: list[int] | None = None) -> list[dict]:
        """Per edited grid: active edits, buildings whose inputs differ from generation, whether the
        stored network differs from the generated one (``modified``) and ``revision``, the number of
        edits and undos (it grows with every write)."""
        if not self.load_edit_table_exists():
            return []
        where, params = ["TRUE"], {}
        if version_id is not None:
            where.append("e.version_id = %(v)s")
            params["v"] = version_id
        if grid_ids is not None:
            where.append("e.grid_result_id = ANY(%(ids)s)")
            params["ids"] = list(grid_ids)
        return self._le_rows(
            f"""WITH e AS (SELECT * FROM pylovo.load_edit e WHERE {' AND '.join(where)}),
                first_b AS (SELECT DISTINCT ON (version_id, objectid) version_id, objectid, grid_result_id,
                                   before_building o
                            FROM e ORDER BY version_id, objectid, load_edit_id),
                first_g AS (SELECT DISTINCT ON (grid_result_id) grid_result_id, before_net_md5
                            FROM e ORDER BY grid_result_id, load_edit_id)
                SELECT g.grid_result_id, g.version_id, g.plz,
                       (SELECT count(*) FROM e WHERE e.grid_result_id = g.grid_result_id AND e.undone_at IS NULL) AS active_edits,
                       (SELECT count(*) FROM e WHERE e.grid_result_id = g.grid_result_id) AS all_edits,
                       (SELECT max(e.created_at) FROM e WHERE e.grid_result_id = g.grid_result_id) AS last_edit_at,
                       (SELECT count(*) + count(e.undone_at) FROM e WHERE e.grid_result_id = g.grid_result_id) AS revision,
                       (SELECT count(*) FROM first_b f JOIN pylovo.buildings_result br
                             ON br.version_id = f.version_id AND br.objectid = f.objectid
                         WHERE f.grid_result_id = g.grid_result_id AND (
                               br.households IS DISTINCT FROM (f.o->>'households')::int
                            OR br.residential_floor_area IS DISTINCT FROM (f.o->>'residential_floor_area')::float8
                            OR br.nonresidential_floor_area IS DISTINCT FROM (f.o->>'nonresidential_floor_area')::float8
                            OR br.nonresidential_use IS DISTINCT FROM (f.o->>'nonresidential_use'))) AS edited_buildings,
                       (md5(g.grid::text) <> fg.before_net_md5) AS modified,
                       ARRAY(SELECT f.objectid FROM first_b f JOIN pylovo.buildings_result br
                                  ON br.version_id = f.version_id AND br.objectid = f.objectid
                              WHERE f.grid_result_id = g.grid_result_id AND (
                                    br.households IS DISTINCT FROM (f.o->>'households')::int
                                 OR br.residential_floor_area IS DISTINCT FROM (f.o->>'residential_floor_area')::float8
                                 OR br.nonresidential_floor_area IS DISTINCT FROM (f.o->>'nonresidential_floor_area')::float8
                                 OR br.nonresidential_use IS DISTINCT FROM (f.o->>'nonresidential_use'))) AS edited_objectids
                FROM first_g fg JOIN pylovo.grid_result g ON g.grid_result_id = fg.grid_result_id
                ORDER BY g.grid_result_id""", params)
