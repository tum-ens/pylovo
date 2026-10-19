"""Queries of the MV results (``pylovo.mv_variant``, ``mv_substation``, ``mv_grid``, ``mv_node``, ``mv_line``, ``mv_ring``).

Registers MV variants and their locks, writes the region steps and the districts of a variant, reads
the results back for plots and exports, and lists and deletes variants. Every method takes
``version_id`` (and ``mv_variant_id`` where relevant) explicitly and never uses the configured
``VERSION_ID``. Like the other mixins its methods run in the current transaction and do not commit
unless their docstring says so.

Ids are pre-allocated from the identity sequences (``nextval``) and inserted explicitly, so the mapping
from plan keys (and input rows) to ``mv_node_id``, ``mv_line_id``, ``substation_id`` and ``mv_grid_id`` is
deterministic. Nullable integer columns come back as ``float`` columns with NaN for NULL.
"""
from __future__ import annotations

import dataclasses
import json
import math
from collections import Counter
from typing import TYPE_CHECKING, Any, Mapping, Sequence

import numpy as np
import pandas as pd
from psycopg2.extras import execute_values

from pylovo.config_loader import TARGET_EPSG
from pylovo.database.base_mixin import BaseMixin

if TYPE_CHECKING:   # imported lazily at run time: an LV-only run never loads the MV stage modules
    import geopandas as gpd
    import pandapower as pp

    from pylovo.mv_grid.model import DistrictPlan, MvRunSummary
    from pylovo.mv_grid.parameters import LvVersionParameters, MvParameters

#: Advisory lock key of a running MV variant (next to 907360 setup and 907361 PLZ generation).
VARIANT_LOCK_KEY = 907362

#: Columns of ``pylovo.mv_node`` that :meth:`MvResultMixin.insert_nodes` writes when the rows have them.
NODE_INSERT_COLUMNS: tuple[str, ...] = (
    "mv_grid_id", "kind", "connection_hint", "connection", "substation_id", "grid_result_id", "customer_objectid",
    "mastr_connection_id", "voltage_class", "technology", "plz", "plz_settlement_type", "settlement_id",
    "area_class", "station_source", "households", "n_buildings", "load_stats", "installed_kw", "p_own_kw",
    "gen_solar_kw", "gen_wind_kw", "gen_other_kw", "sn_kva", "attach_edge_id", "attach_fraction",
    "attach_offset_m", "bridged", "lv_design_source_vm_pu", "lv_max_drop_pu", "flags",
)
_SQL_TYPES: dict[str, str] = {
    "mv_node_id": "bigint", "mv_grid_id": "bigint", "kind": "varchar", "connection_hint": "varchar",
    "connection": "varchar", "substation_id": "bigint", "grid_result_id": "bigint", "customer_objectid": "text",
    "mastr_connection_id": "text", "voltage_class": "varchar", "technology": "varchar", "plz": "integer",
    "plz_settlement_type": "integer", "settlement_id": "bigint", "area_class": "varchar", "station_source": "varchar",
    "households": "double precision", "n_buildings": "integer", "load_stats": "jsonb",
    "installed_kw": "double precision", "p_own_kw": "double precision", "gen_solar_kw": "double precision",
    "gen_wind_kw": "double precision", "gen_other_kw": "double precision", "sn_kva": "double precision",
    "attach_edge_id": "bigint", "attach_fraction": "double precision", "attach_offset_m": "double precision",
    "bridged": "boolean", "lv_design_source_vm_pu": "double precision", "lv_max_drop_pu": "double precision",
    "flags": "text[]", "x": "double precision", "y": "double precision", "feeder_id": "integer",
    "ring_id": "integer", "ring_position": "integer", "stub_parent": "bigint", "vm_load_pu": "double precision",
    "vm_n1_pu": "double precision", "vm_feed_in_pu": "double precision", "mv_lv_tap_pos": "integer",
    "lv_busbar_vm_pu": "double precision", "lv_min_vm_pu": "double precision",
    "lv_busbar_vm_feed_in_pu": "double precision", "band_conflict": "boolean",
    "coupled_lv_busbar_vm_pu": "double precision", "coupled_lv_min_vm_pu": "double precision",
    "coupled_lv_busbar_vm_feed_in_pu": "double precision", "coupled_lv_max_vm_pu": "double precision",
    "coupled_op_lv_busbar_vm_pu": "double precision",
}
#: NOT NULL columns of ``mv_node`` with a default, and the value a missing entry takes.
_NODE_DEFAULTS: dict[str, Any] = {"gen_solar_kw": 0.0, "gen_wind_kw": 0.0, "gen_other_kw": 0.0, "bridged": False,
                                  "flags": []}
#: Result columns of ``mv_node`` that a district result sets (reset before a district is saved again).
NODE_RESULT_COLUMNS: tuple[str, ...] = (
    "connection", "feeder_id", "ring_id", "ring_position", "stub_parent", "vm_load_pu", "vm_n1_pu", "vm_feed_in_pu",
    "mv_lv_tap_pos", "lv_busbar_vm_pu", "lv_min_vm_pu", "lv_busbar_vm_feed_in_pu", "band_conflict",
)
#: Columns that :meth:`MvResultMixin.save_coupled_results` writes.
COUPLED_COLUMNS: tuple[str, ...] = (
    "coupled_lv_busbar_vm_pu", "coupled_lv_min_vm_pu", "coupled_lv_busbar_vm_feed_in_pu", "coupled_lv_max_vm_pu",
    "coupled_op_lv_busbar_vm_pu",
)
#: ``load_stats`` keys and the NODE_COLUMNS suffix of each consumer category.
_CATEGORY_SUFFIX: dict[str, str] = {"Residential": "res", "Commercial": "com", "Public": "pub", "Unknown": "unk"}
_FEED_IN_CASES = ("feed_in_wind", "feed_in_pv")
_N1_CASES = ("n1a", "n1b")
_VIOLATION_STATUSES = ("voltage_violation", "loading_violation", "not_converged")

#: Columns of the node layer of :meth:`MvResultMixin.read_mv_layers` (every column but the internal ones;
#: ``load_stats`` holds the station-busbar customers that ``pylovo-mv couple`` adds at the LV busbar).
_NODE_LAYER_COLUMNS = (
    "mv_node_id", "version_id", "mv_variant_id", "mv_grid_id", "kind", "connection_hint", "connection",
    "substation_id", "grid_result_id", "customer_objectid", "mastr_connection_id", "voltage_class", "technology",
    "plz", "plz_settlement_type", "settlement_id", "area_class", "station_source", "households", "n_buildings",
    "load_stats", "installed_kw", "p_own_kw", "gen_solar_kw", "gen_wind_kw", "gen_other_kw", "sn_kva", "feeder_id",
    "ring_id", "ring_position", "stub_parent", "attach_offset_m", "bridged", "vm_load_pu", "vm_n1_pu", "vm_feed_in_pu",
    "lv_design_source_vm_pu", "lv_max_drop_pu", "mv_lv_tap_pos", "lv_busbar_vm_pu", "lv_min_vm_pu",
    "lv_busbar_vm_feed_in_pu", "band_conflict", "coupled_lv_busbar_vm_pu", "coupled_lv_min_vm_pu",
    "coupled_lv_busbar_vm_feed_in_pu", "coupled_lv_max_vm_pu", "coupled_op_lv_busbar_vm_pu", "flags", "geom",
)
#: Columns of the grid layer (``net`` is never read by the plot readers).
_GRID_LAYER_COLUMNS = (
    "mv_grid_id", "version_id", "mv_variant_id", "substation_id", "status", "power_flow_status", "coupled_status",
    "vn_kv", "complete", "area_km2", "hv_mv_transformers", "hv_mv_transformer_mva", "hv_mv_sizing", "oltc_tap_pos",
    "peak_load_mw", "peak_feed_in_mw", "sum_station_kva", "n_stations", "n_customers", "n_generators", "n_joints",
    "n_rings", "n_stub_nodes", "n_feeder_bays", "circuit_km", "cable_km", "overhead_km", "trench_km", "bridge_km",
    "max_voltage_drop_percent", "max_voltage_drop_n1_percent", "max_voltage_rise_percent",
    "max_line_loading_percent", "max_line_loading_n1_percent", "max_line_loading_feed_in_percent",
    "n_band_conflicts", "n_tap_limit", "metrics", "repairs", "error", "runtime_s", "geom",
)


def _py(value: Any) -> Any:
    """``value`` as a plain Python object for psycopg2: numpy scalars unwrapped, NaN, NaT and NA as None."""
    if value is None:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and math.isnan(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _py_int(value: Any) -> int | None:
    value = _py(value)
    return None if value is None else int(value)


def _parallel_count(value: Any) -> int:
    """Return a validated installed circuit count (one when omitted)."""
    value = _py(value)
    if value is None:
        return 1
    if isinstance(value, bool):
        raise ValueError("parallel must be a positive integer")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("parallel must be a positive integer") from exc
    if not math.isfinite(number) or number < 1 or not number.is_integer():
        raise ValueError("parallel must be a positive integer")
    return int(number)


def _py_float(value: Any) -> float | None:
    value = _py(value)
    return None if value is None else float(value)


def _json_safe(value: Any) -> Any:
    """JSON-native copy of ``value`` (numpy unwrapped, NaN and inf as None, tuples as lists)."""
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(_json_safe(value), allow_nan=False, sort_keys=True)


def _flags(value: Any) -> list[str]:
    """A flags cell as a list of texts (None, NaN or '' give an empty list; a text gives one flag)."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return []
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in value if v is not None and str(v)]


def _cast_template(columns: Sequence[str], overrides: Mapping[str, str] | None = None) -> str:
    """``(%s::type, ...)`` for ``execute_values`` (explicit casts keep NULL-only columns typed)."""
    overrides = overrides or {}
    parts = []
    for column in columns:
        if column in overrides:
            parts.append(overrides[column])
        else:
            parts.append(f"%s::{_SQL_TYPES[column]}")
    return "(" + ", ".join(parts) + ")"


def node_columns_frame(rows: pd.DataFrame) -> pd.DataFrame:
    """Map ``mv_node`` rows (as read by the MV readers) to :data:`~pylovo.mv_grid.model.NODE_COLUMNS`.

    ``inst_*``/``units_*`` come from ``load_stats`` (0 when a category is absent); ``p_fixed_kw = p_own_kw`` for
    customers, the fixed load of the station-busbar customers (``load_stats['busbar_customers']['kw']``,
    :func:`pylovo.mv_grid.inputs.busbar_customers`) for stations, else 0; ``lv_sn_kva`` is the rating of the
    station's LV grid (``load_stats['busbar_customers']['lv_sn_kva']`` of a station upsized in the MV model by
    :func:`pylovo.mv_grid.inputs.upsize_busbar_stations`, else ``sn_kva``); ``urban = (area_class == 'urban')``;
    ``gen_level`` is ``'mv'`` for generators, ``'lv'`` for stations with LV-side generation and ``''`` otherwise.
    The busbar rows come first, then ascending ``mv_node_id``; the index is ``mv_node_id``.

    Args:
        rows: Columns ``mv_node_id``, ``kind``, ``connection_hint``, ``x``, ``y``, ``attach_edge_id``,
            ``attach_fraction``, ``attach_offset_m``, ``bridged``, ``settlement_id``, ``area_class``, ``plz``,
            ``plz_settlement_type``, ``load_stats``, ``p_own_kw``, ``gen_solar_kw``, ``gen_wind_kw``,
            ``gen_other_kw``, ``sn_kva``, ``grid_result_id``, ``lv_design_source_vm_pu``, ``lv_max_drop_pu``.

    Returns:
        A frame with exactly the NODE_COLUMNS (plus nothing else), index ``mv_node_id``.
    """
    from pylovo.mv_grid.inputs import busbar_record
    from pylovo.mv_grid.model import NODE_COLUMNS

    df = pd.DataFrame(rows).copy()
    if df.empty:
        return pd.DataFrame(columns=list(NODE_COLUMNS), index=pd.Index([], name="mv_node_id", dtype="int64"))
    stats = df["load_stats"] if "load_stats" in df.columns else pd.Series([None] * len(df), index=df.index)
    stats = stats.map(lambda s: json.loads(s) if isinstance(s, str) else (s or {}))
    for category, suffix in _CATEGORY_SUFFIX.items():
        pair = stats.map(lambda s, c=category: s.get(c) if isinstance(s, Mapping) else None)
        df[f"inst_{suffix}"] = pair.map(lambda p: float(p[0] or 0.0) if p else 0.0).astype(float)
        df[f"units_{suffix}"] = pair.map(lambda p: float(p[1] or 0.0) if p else 0.0).astype(float)
    kind = df["kind"].astype(str)
    p_own = pd.to_numeric(df["p_own_kw"], errors="coerce").fillna(0.0) if "p_own_kw" in df.columns else 0.0
    busbar_kw = stats.map(lambda s: float(busbar_record(s).get("kw") or 0.0))
    df["p_fixed_kw"] = np.where(kind == "customer", p_own, np.where(kind == "station", busbar_kw, 0.0))
    sn_kva = pd.to_numeric(df["sn_kva"], errors="coerce") if "sn_kva" in df.columns else pd.Series(np.nan, df.index)
    lv_sn_kva = pd.to_numeric(stats.map(lambda s: busbar_record(s).get("lv_sn_kva")), errors="coerce")
    df["lv_sn_kva"] = lv_sn_kva.where(lv_sn_kva.notna(), sn_kva).astype(float)
    df["urban"] = df["area_class"].astype(object).eq("urban") if "area_class" in df.columns else False
    for column in ("gen_solar_kw", "gen_wind_kw", "gen_other_kw"):
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0.0).astype(float) if column in df.columns \
            else 0.0
    lv_gen = (df["gen_solar_kw"] + df["gen_wind_kw"] + df["gen_other_kw"]) > 0
    df["gen_level"] = np.where(kind == "generator", "mv", np.where((kind == "station") & lv_gen, "lv", ""))
    df["bridged"] = df["bridged"].fillna(False).astype(bool) if "bridged" in df.columns else False
    for column in NODE_COLUMNS:
        if column not in df.columns:
            df[column] = np.nan
    df["_busbar_first"] = (kind != "busbar").astype(int)
    df = df.sort_values(["_busbar_first", "mv_node_id"], kind="mergesort")
    out = df.set_index(df["mv_node_id"].astype("int64").rename("mv_node_id"))[list(NODE_COLUMNS)]
    return out


class MvResultMixin(BaseMixin):
    """Write, read, list and delete the MV results of a version."""

    # ------------------------------------------------------------------------------------------ variants, locks
    def try_lock_variant(self, version_id: str, variant_id: str) -> bool:
        """Take the session advisory lock of an MV variant (key 907362) without waiting.

        The lock lives on this connection until :meth:`unlock_variant` or the end of the session; commits and
        rollbacks do not release it.

        Returns:
            False if another session holds it (another MV run of the variant is active).
        """
        self.cur.execute("SELECT pg_try_advisory_lock(%s, hashtext(%s))",
                         (VARIANT_LOCK_KEY, f"{version_id}/{variant_id}"))
        return bool(self.cur.fetchone()[0])

    def unlock_variant(self, version_id: str, variant_id: str) -> None:
        """Release the session advisory lock of an MV variant (call it outside a failed transaction)."""
        self.cur.execute("SELECT pg_advisory_unlock(%s, hashtext(%s))",
                         (VARIANT_LOCK_KEY, f"{version_id}/{variant_id}"))

    def variant_row(self, version_id: str, variant_id: str) -> dict | None:
        """The ``pylovo.mv_variant`` row of an MV variant as a dict, or None if it does not exist."""
        self.cur.execute(
            """SELECT version_id, mv_variant_id, mv_variant_comment, mv_parameters, lv_parameters, plz_list,
                      graph_id, status, lv_fingerprint, summary, created_at, finished_at
               FROM pylovo.mv_variant WHERE version_id = %s AND mv_variant_id = %s""",
            (version_id, variant_id))
        row = self.cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in self.cur.description]
        out = dict(zip(names, row))
        for key in ("mv_parameters", "lv_parameters", "lv_fingerprint", "summary"):
            if isinstance(out.get(key), str):
                out[key] = json.loads(out[key])
        out["plz_list"] = [int(p) for p in (out.get("plz_list") or [])]
        return out

    def check_variant_parameters(self, version_id: str, params: "MvParameters") -> None:
        """Raise if an MV variant exists with other parameters (cheap check before the graph build; no write).

        Raises:
            ValueError: The parameters differ from the stored snapshot (the message names the paths).
        """
        row = self.variant_row(version_id, params.variant_id)
        if row is not None:
            self._compare_variant_snapshot(version_id, params, row["mv_parameters"])

    def _compare_variant_snapshot(self, version_id: str, params: "MvParameters", stored: Any) -> None:
        from pylovo.version_snapshot import MV_ADDED_LATER, compare_snapshots

        if isinstance(stored, str):
            stored = json.loads(stored)
        expected = json.loads(json.dumps(params.to_snapshot(), allow_nan=False, sort_keys=True))
        differences, not_recorded = compare_snapshots(stored or {}, expected, added_later=MV_ADDED_LATER)
        variant_id = params.variant_id
        if differences:
            more = " …" if len(differences) > 5 else ""
            raise ValueError(
                f"MV parameters differ from the stored snapshot of MV variant {variant_id} of version {version_id} "
                f"({', '.join(differences[:5])}{more}). Use another --variant or pylovo-mv delete --version "
                f"{version_id} --variant {variant_id}.")
        if not_recorded:
            self.logger.warning(f"The stored snapshot of MV variant {variant_id} of version {version_id} predates "
                                f"the parameters {', '.join(not_recorded)}")

    def register_variant(self, version_id: str, params: "MvParameters", plz_list: Sequence[int], graph_id: str,
                         lv: "LvVersionParameters", lv_fingerprint: dict) -> str:
        """Insert the MV variant once, or check it against the stored snapshot (mirrors the LV version rule); commits.

        Union semantics of the PLZ set (DESIGN §2.5): if ``plz_list`` is a subset of the stored set and the routing
        graph is the stored one, the run resumes; otherwise the stored row gets the union, the new graph and
        fingerprint ('grow': the caller rebuilds every district with :meth:`reset_variant_region`). A changed graph
        (changed source rows or graph build) always rebuilds: the stored attachments refer to the edge ids of the
        stored graph.

        Returns:
            ``'new'``, ``'resume'`` or ``'grow'``.

        Raises:
            ValueError: The parameters differ from the stored snapshot of the variant.
        """
        variant_id = params.variant_id
        snapshot = json.dumps(params.to_snapshot(), allow_nan=False, sort_keys=True)
        plz = sorted({int(p) for p in plz_list})
        try:
            self.cur.execute(
                """INSERT INTO pylovo.mv_variant (version_id, mv_variant_id, mv_variant_comment, mv_parameters,
                                                  lv_parameters, plz_list, graph_id, status, lv_fingerprint)
                   VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::integer[], %s, 'running', %s::jsonb)
                   ON CONFLICT (version_id, mv_variant_id) DO NOTHING""",
                (version_id, variant_id, params.comment, snapshot, _dumps(lv.as_json()), plz, graph_id,
                 _dumps(lv_fingerprint)))
            if self.cur.rowcount == 1:
                mode = "new"
            else:
                row = self.variant_row(version_id, variant_id)
                if row is None:   # pragma: no cover - deleted between the insert and the read
                    raise RuntimeError(f"MV variant {variant_id} of version {version_id} vanished during registration")
                self._compare_variant_snapshot(version_id, params, row["mv_parameters"])
                stored = set(row["plz_list"])
                if set(plz) <= stored and row.get("graph_id") == graph_id:
                    mode = "resume"
                    self.cur.execute(
                        """UPDATE pylovo.mv_variant SET status = 'running', finished_at = NULL
                           WHERE version_id = %s AND mv_variant_id = %s""", (version_id, variant_id))
                else:
                    mode = "grow"
                    union = sorted(stored | set(plz))
                    self.cur.execute(
                        """UPDATE pylovo.mv_variant
                           SET plz_list = %s::integer[], graph_id = %s, lv_fingerprint = %s::jsonb,
                               lv_parameters = %s::jsonb, status = 'running', summary = NULL, finished_at = NULL
                           WHERE version_id = %s AND mv_variant_id = %s""",
                        (union, graph_id, _dumps(lv_fingerprint), _dumps(lv.as_json()), version_id, variant_id))
            self.conn.commit()
            return mode
        except Exception:
            self.conn.rollback()
            raise

    def variant_region_built(self, version_id: str, variant_id: str) -> bool:
        """Whether the region steps of the variant were committed (it has substations)."""
        self.cur.execute("SELECT EXISTS (SELECT 1 FROM pylovo.mv_substation WHERE version_id = %s "
                         "AND mv_variant_id = %s)", (version_id, variant_id))
        return bool(self.cur.fetchone()[0])

    def reset_variant_region(self, version_id: str, variant_id: str) -> None:
        """Delete the substations, grids, nodes, lines and rings of a variant; the ``mv_variant`` row stays."""
        params = (version_id, variant_id)
        self.cur.execute("DELETE FROM pylovo.mv_line WHERE version_id = %s AND mv_variant_id = %s", params)
        self.cur.execute("DELETE FROM pylovo.mv_ring WHERE version_id = %s AND mv_variant_id = %s", params)
        self.cur.execute("DELETE FROM pylovo.mv_substation WHERE version_id = %s AND mv_variant_id = %s", params)
        self.cur.execute("DELETE FROM pylovo.mv_node WHERE version_id = %s AND mv_variant_id = %s", params)

    def _allocate_ids(self, table: str, column: str, n: int) -> np.ndarray:
        """Pre-allocate ``n`` ids of an identity column (ascending, int64)."""
        if n <= 0:
            return np.zeros(0, dtype=np.int64)
        self.cur.execute(
            f"SELECT nextval(pg_get_serial_sequence('pylovo.{table}', '{column}')) FROM generate_series(1, %s)",
            (int(n),))
        return np.array(sorted(int(r[0]) for r in self.cur.fetchall()), dtype=np.int64)

    # ------------------------------------------------------------------------------------------ region steps
    def insert_substations(self, version_id: str, variant_id: str, sites: pd.DataFrame) -> np.ndarray:
        """Insert the HV/MV substation sites of a variant.

        Args:
            sites: One row per site with ``x``, ``y`` (busbar point, TARGET_EPSG), ``source`` and ``source_ids``
                (list of texts); optional ``name``, ``operator``, ``voltage_tag``, ``egon_bus_id``,
                ``egon_distance_m``, ``in_region`` (default False), ``attach_edge_id``, ``attach_fraction``,
                ``attach_offset_m``, ``osm_ratings_mva`` (list), ``flags`` (list) and ``footprint_wkb``
                (WKB of the footprint polygon, or None).

        Returns:
            The new ``substation_id`` per row, in row order.
        """
        sites = pd.DataFrame(sites).reset_index(drop=True)
        ids = self._allocate_ids("mv_substation", "substation_id", len(sites))
        if not len(sites):
            return ids
        values = []
        for i, row in enumerate(sites.to_dict("records")):
            footprint = row.get("footprint_wkb")
            ratings = row.get("osm_ratings_mva")
            values.append((
                int(ids[i]), version_id, variant_id, str(row.get("source") or "osm"),
                [str(s) for s in (row.get("source_ids") or [])], _py(row.get("name")), _py(row.get("operator")),
                _py(row.get("voltage_tag")), _py_int(row.get("egon_bus_id")), _py_float(row.get("egon_distance_m")),
                bool(_py(row.get("in_region")) or False), _py_int(row.get("attach_edge_id")),
                _py_float(row.get("attach_fraction")), _py_float(row.get("attach_offset_m")),
                None if ratings is None or (isinstance(ratings, float) and math.isnan(ratings))
                else [float(r) for r in ratings],
                _flags(row.get("flags")), float(row["x"]), float(row["y"]),
                None if footprint is None or (isinstance(footprint, float) and math.isnan(footprint))
                else bytes(footprint),
            ))
        execute_values(
            self.cur,
            """INSERT INTO pylovo.mv_substation (substation_id, version_id, mv_variant_id, source, source_ids, name,
                   operator, voltage_tag, egon_bus_id, egon_distance_m, in_region, attach_edge_id, attach_fraction,
                   attach_offset_m, osm_ratings_mva, flags, geom, footprint)
               VALUES %s""",
            values,
            template=(f"(%s, %s, %s, %s, %s::text[], %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::double precision[], "
                      f"%s::text[], ST_SetSRID(ST_MakePoint(%s, %s), {int(TARGET_EPSG)}), "
                      f"ST_Multi(ST_CollectionExtract(ST_GeomFromWKB(%s, {int(TARGET_EPSG)}), 3)))"),
            page_size=1000)
        return ids

    def flag_substations(self, substation_ids: Sequence[int], flag: str) -> None:
        """Append ``flag`` to the flags of the given substations (once)."""
        ids = [int(i) for i in substation_ids]
        if ids:
            self.cur.execute(
                """UPDATE pylovo.mv_substation SET flags = array_append(flags, %s::text)
                   WHERE substation_id = ANY(%s) AND NOT (%s::text = ANY(flags))""", (flag, ids, flag))

    def insert_nodes(self, version_id: str, variant_id: str, rows: pd.DataFrame) -> np.ndarray:
        """Insert MV nodes of a variant.

        Args:
            rows: One row per node with ``kind``, ``connection_hint``, ``x``, ``y`` (TARGET_EPSG) and any of the
                ``mv_node`` columns of :data:`NODE_INSERT_COLUMNS` (others are ignored). ``load_stats`` is a dict
                ``{category: [installed_kw, units]}``, ``flags`` a list of texts.

        Returns:
            The new ``mv_node_id`` per row, in row order.

        Raises:
            ValueError: A required column is missing.
        """
        rows = pd.DataFrame(rows).reset_index(drop=True)
        missing = [c for c in ("kind", "connection_hint", "x", "y") if c not in rows.columns]
        if missing and len(rows):
            raise ValueError(f"MV node rows need the columns {', '.join(missing)}")
        ids = self._allocate_ids("mv_node", "mv_node_id", len(rows))
        if not len(rows):
            return ids
        columns = [c for c in NODE_INSERT_COLUMNS if c in rows.columns]
        columns += [c for c in _NODE_DEFAULTS if c not in columns]
        values = []
        for i, record in enumerate(rows.to_dict("records")):
            row = [int(ids[i]), version_id, variant_id]
            for column in columns:
                value = record.get(column)
                if column == "flags":
                    value = _flags(value)
                elif column == "load_stats":
                    value = None if value is None or (isinstance(value, float) and math.isnan(value)) \
                        else _dumps(value if not isinstance(value, str) else json.loads(value))
                else:
                    value = _py(value)
                    if value is None and column in _NODE_DEFAULTS:
                        value = _NODE_DEFAULTS[column]
                row.append(value)
            row += [float(record["x"]), float(record["y"])]
            values.append(tuple(row))
        names = ", ".join(["mv_node_id", "version_id", "mv_variant_id", *columns, "geom"])
        template = ("(%s, %s, %s, " + ", ".join(f"%s::{_SQL_TYPES[c]}" for c in columns)
                    + f", ST_SetSRID(ST_MakePoint(%s, %s), {int(TARGET_EPSG)}))")
        execute_values(self.cur, f"INSERT INTO pylovo.mv_node ({names}) VALUES %s", values, template=template,
                       page_size=1000)
        return ids

    def update_node_attachments(self, rows: pd.DataFrame) -> None:
        """Write the graph attachment of nodes (and of the substations of busbar nodes).

        Args:
            rows: ``mv_node_id`` and any of ``attach_edge_id``, ``attach_fraction``, ``attach_offset_m``,
                ``settlement_id``, ``area_class``, ``bridged``, ``x`` and ``y`` (both: the node moves, e.g. a
                customer to its street transfer point) and ``flags`` (appended, without duplicates).
        """
        rows = pd.DataFrame(rows).reset_index(drop=True)
        if not len(rows):
            return
        candidates = ("attach_edge_id", "attach_fraction", "attach_offset_m", "settlement_id", "area_class",
                      "bridged")
        columns = [c for c in candidates if c in rows.columns]
        move = "x" in rows.columns and "y" in rows.columns
        has_flags = "flags" in rows.columns
        values_columns = ["mv_node_id", *columns] + (["x", "y"] if move else []) + (["flags"] if has_flags else [])
        assignments = [f"{c} = v.{c}" for c in columns]
        if move:
            assignments.append(f"geom = CASE WHEN v.x IS NULL OR v.y IS NULL THEN n.geom "
                               f"ELSE ST_SetSRID(ST_MakePoint(v.x, v.y), {int(TARGET_EPSG)}) END")
        if has_flags:
            assignments.append("flags = ARRAY(SELECT DISTINCT f FROM unnest(n.flags || v.flags) f ORDER BY f)")
        if not assignments:
            return
        values = []
        for record in rows.to_dict("records"):
            row = [int(record["mv_node_id"])]
            row += [_py(record.get(c)) for c in columns]
            if move:
                row += [_py_float(record.get("x")), _py_float(record.get("y"))]
            if has_flags:
                row.append(_flags(record.get("flags")))
            values.append(tuple(row))
        execute_values(
            self.cur,
            f"""UPDATE pylovo.mv_node n SET {', '.join(assignments)}
                FROM (VALUES %s) AS v({', '.join(values_columns)})
                WHERE n.mv_node_id = v.mv_node_id""",
            values, template=_cast_template(values_columns), page_size=1000)
        attach = [c for c in ("attach_edge_id", "attach_fraction", "attach_offset_m") if c in columns]
        if attach:
            self.cur.execute(
                f"""UPDATE pylovo.mv_substation s SET {', '.join(f'{c} = n.{c}' for c in attach)}
                    FROM pylovo.mv_node n
                    WHERE n.kind = 'busbar' AND n.substation_id = s.substation_id AND n.mv_node_id = ANY(%s)""",
                ([int(i) for i in rows["mv_node_id"]],))

    def set_node_grids(self, assignment: pd.DataFrame) -> None:
        """Assign nodes to their MV grid (supply district).

        Args:
            assignment: ``mv_node_id`` and ``mv_grid_id`` (NaN or None: not assigned); optional ``bridged``.
        """
        assignment = pd.DataFrame(assignment).reset_index(drop=True)
        if not len(assignment):
            return
        columns = ["mv_node_id", "mv_grid_id"] + (["bridged"] if "bridged" in assignment.columns else [])
        values = [tuple(_py(v) for v in rec) for rec in assignment[columns].itertuples(index=False, name=None)]
        sets = ["mv_grid_id = v.mv_grid_id"]
        if "bridged" in columns:
            sets.append("bridged = COALESCE(v.bridged, n.bridged)")
        execute_values(
            self.cur,
            f"""UPDATE pylovo.mv_node n SET {', '.join(sets)}
                FROM (VALUES %s) AS v({', '.join(columns)}) WHERE n.mv_node_id = v.mv_node_id""",
            values, template=_cast_template(columns), page_size=1000)

    def create_grids(self, version_id: str, variant_id: str, substation_ids: Sequence[int], vn_kv: float
                     ) -> dict[int, int]:
        """Create one pending ``mv_grid`` per substation and put the substation's busbar node into it.

        Returns:
            ``{substation_id: mv_grid_id}``.
        """
        substation_ids = [int(s) for s in substation_ids]
        ids = self._allocate_ids("mv_grid", "mv_grid_id", len(substation_ids))
        if not substation_ids:
            return {}
        execute_values(
            self.cur,
            """INSERT INTO pylovo.mv_grid (mv_grid_id, version_id, mv_variant_id, substation_id, status, vn_kv)
               VALUES %s""",
            [(int(g), version_id, variant_id, s, "pending", float(vn_kv)) for g, s in zip(ids, substation_ids)],
            page_size=1000)
        self.cur.execute(
            """UPDATE pylovo.mv_node n SET mv_grid_id = g.mv_grid_id
               FROM pylovo.mv_grid g
               WHERE n.version_id = %s AND n.mv_variant_id = %s AND n.kind = 'busbar'
                 AND n.substation_id = g.substation_id AND g.mv_grid_id = ANY(%s)""",
            (version_id, variant_id, [int(g) for g in ids]))
        return dict(zip(substation_ids, (int(g) for g in ids)))

    def write_grid_areas(self, version_id: str, variant_id: str, plz_list: Sequence[int]) -> None:
        """Write the district polygons (``mv_grid.geom``, ``area_km2``, ``complete``), DESIGN §4.1.

        The polygon of a district is the union of the Voronoi cells of its stations and customers, clipped to the
        planning region (the PLZ polygons); the cells partition the region. A district is complete when its
        substation lies in the region and less than 10 % of its cell boundary runs along the region border (within
        1 m of it).
        """
        self.cur.execute(
            """
            WITH region AS (
                SELECT u.geom, ST_Buffer(ST_Boundary(u.geom), 1.0) AS border
                FROM (SELECT ST_UnaryUnion(ST_Collect(geom)) AS geom FROM pylovo.postcode
                      WHERE plz = ANY(%(plz)s)) u),
            pts AS (SELECT mv_grid_id, geom FROM pylovo.mv_node
                    WHERE version_id = %(v)s AND mv_variant_id = %(var)s AND kind IN ('station', 'customer')
                      AND mv_grid_id IS NOT NULL),
            vor AS (SELECT row_number() OVER () AS cid, d.geom
                    FROM ST_Dump((SELECT ST_VoronoiPolygons(ST_Collect(p.geom), 0.0, (SELECT geom FROM region))
                                  FROM pts p)) d),
            lab AS (SELECT DISTINCT ON (v.cid) v.cid, p.mv_grid_id, v.geom
                    FROM vor v JOIN pts p ON ST_Intersects(v.geom, p.geom) ORDER BY v.cid, p.mv_grid_id),
            cell AS (SELECT l.mv_grid_id,
                            ST_Multi(ST_CollectionExtract(ST_Intersection(ST_UnaryUnion(ST_Collect(l.geom)),
                                                                          r.geom), 3)) AS geom
                     FROM lab l CROSS JOIN region r GROUP BY l.mv_grid_id, r.geom)
            UPDATE pylovo.mv_grid g
            SET geom = c.geom, area_km2 = ST_Area(c.geom) / 1e6,
                complete = s.in_region AND COALESCE(
                    ST_Length(ST_Intersection(ST_Boundary(c.geom), r.border))
                    / NULLIF(ST_Length(ST_Boundary(c.geom)), 0), 0) < 0.10
            FROM cell c, pylovo.mv_substation s, region r
            WHERE g.mv_grid_id = c.mv_grid_id AND s.substation_id = g.substation_id
              AND g.version_id = %(v)s AND g.mv_variant_id = %(var)s AND NOT ST_IsEmpty(c.geom)
            """,
            {"v": version_id, "var": variant_id, "plz": [int(p) for p in plz_list]})

    def grid_areas(self, version_id: str, variant_id: str) -> dict[int, float]:
        """The area of every district of a variant in km² (``mv_grid.area_km2``, NaN where it has none)."""
        self.cur.execute("""SELECT mv_grid_id, area_km2 FROM pylovo.mv_grid
                            WHERE version_id = %s AND mv_variant_id = %s ORDER BY mv_grid_id""",
                         (version_id, variant_id))
        return {int(g): (float(a) if a is not None else math.nan) for g, a in self.cur.fetchall()}

    def set_grid_voltages(self, version_id: str, variant_id: str, voltages: pd.DataFrame,
                          records: Mapping[int, dict]) -> None:
        """Set the nominal voltage of the districts and record its density rule (``MV_NOMINAL_VOLTAGE_KV``).

        Args:
            voltages: Index ``mv_grid_id``, column ``vn_kv`` (``pylovo.mv_grid.districts.district_voltages``).
            records: ``{mv_grid_id: entry}`` stored as ``mv_grid.metrics['voltage_level']`` (kept when the
                district is saved, :meth:`save_district`).
        """
        values = [(int(g), float(vn), _dumps(records.get(int(g), {})), version_id, variant_id)
                  for g, vn in voltages["vn_kv"].items()]
        if not values:
            return
        execute_values(
            self.cur,
            """UPDATE pylovo.mv_grid g
               SET vn_kv = v.vn_kv,
                   metrics = COALESCE(g.metrics, '{}'::jsonb) || jsonb_build_object('voltage_level', v.record)
               FROM (VALUES %s) AS v(mv_grid_id, vn_kv, record, version_id, mv_variant_id)
               WHERE g.mv_grid_id = v.mv_grid_id AND g.version_id = v.version_id
                 AND g.mv_variant_id = v.mv_variant_id""",
            values, template="(%s::bigint, %s::double precision, %s::jsonb, %s::varchar, %s::varchar)",
            page_size=1000)

    def variant_node_table(self, version_id: str, variant_id: str) -> pd.DataFrame:
        """All nodes of a variant in NODE_COLUMNS form plus ``mv_grid_id`` and ``substation_id`` (no joints)."""
        frame = self._node_rows("n.version_id = %(v)s AND n.mv_variant_id = %(var)s",
                                {"v": version_id, "var": variant_id})
        table = node_columns_frame(frame)
        extra = frame.set_index(frame["mv_node_id"].astype("int64"))[["mv_grid_id", "substation_id"]] \
            if len(frame) else pd.DataFrame(columns=["mv_grid_id", "substation_id"])
        return table.join(extra)

    def _node_rows(self, where: str, params: dict) -> pd.DataFrame:
        query = f"""SELECT n.mv_node_id, n.mv_grid_id, n.substation_id, n.kind, n.connection_hint,
                           ST_X(n.geom) AS x, ST_Y(n.geom) AS y, n.attach_edge_id, n.attach_fraction,
                           n.attach_offset_m, n.bridged, n.settlement_id, n.area_class, n.plz,
                           n.plz_settlement_type, n.load_stats, n.p_own_kw, n.gen_solar_kw, n.gen_wind_kw,
                           n.gen_other_kw, n.sn_kva, n.grid_result_id, n.lv_design_source_vm_pu, n.lv_max_drop_pu
                    FROM pylovo.mv_node n
                    WHERE {where} AND n.kind <> 'joint'
                    ORDER BY (n.kind = 'busbar') DESC, n.mv_node_id"""
        self.cur.execute(query, params)
        names = [d[0] for d in self.cur.description]
        return pd.DataFrame(self.cur.fetchall(), columns=names)

    def pending_grids(self, version_id: str, variant_id: str) -> list[tuple[int, int]]:
        """The ``pending`` and ``failed`` grids of a variant as ``(mv_grid_id, n_nodes)``, largest first."""
        self.cur.execute(
            """SELECT g.mv_grid_id, count(n.mv_node_id) AS n_nodes
               FROM pylovo.mv_grid g LEFT JOIN pylovo.mv_node n ON n.mv_grid_id = g.mv_grid_id
               WHERE g.version_id = %s AND g.mv_variant_id = %s AND g.status IN ('pending', 'failed')
               GROUP BY g.mv_grid_id ORDER BY n_nodes DESC, g.mv_grid_id""",
            (version_id, variant_id))
        return [(int(g), int(n)) for g, n in self.cur.fetchall()]

    def hv_boundary(self, version_id: str, variant_id: str) -> pd.DataFrame:
        """The HV boundary of a variant for a coupling with eTraGo: one row per supplying HV/MV substation.

        ``etrago_bus_id`` is the eGon bus id of the substation, which is the id of its 110 kV bus in the eTraGo grid
        model of eGon-data (NULL for a site without an eGon match, flag ``no_egon_bus``). ``p_mw`` and ``q_mvar``
        are the load case at the MV busbar (sum over the districts of the substation: their coincident peak plus
        the MV losses), so the MV and LV demand can replace eTraGo's load at that bus. ``lon`` and ``lat`` are
        WGS84.

        Returns:
            Columns ``substation_id``, ``name``, ``operator``, ``source``, ``etrago_bus_id``,
            ``egon_distance_m``, ``flags``, ``n_grids``, ``vn_kv``, ``p_mw``, ``q_mvar``, ``peak_load_mw``,
            ``n_transformers``, ``transformer_mva``, ``loading_n0_percent``, ``loading_n1_percent``, ``lon``,
            ``lat``, ordered by ``substation_id``.
        """
        self.cur.execute(
            """SELECT s.substation_id, s.name, s.operator, s.source, s.egon_bus_id AS etrago_bus_id, s.egon_distance_m,
                      s.flags, count(g.mv_grid_id) AS n_grids, max(g.vn_kv) AS vn_kv,
                      sum((g.metrics->>'load_slack_p_mw')::double precision) AS p_mw,
                      sum((g.metrics->>'load_slack_q_mvar')::double precision) AS q_mvar,
                      s.peak_load_mw, s.n_transformers, s.transformer_mva, s.loading_n0_percent,
                      s.loading_n1_percent, ST_X(ST_Transform(s.geom, 4326)) AS lon,
                      ST_Y(ST_Transform(s.geom, 4326)) AS lat
               FROM pylovo.mv_substation s
               JOIN pylovo.mv_grid g ON g.version_id = s.version_id AND g.mv_variant_id = s.mv_variant_id
                                    AND g.substation_id = s.substation_id AND g.status = 'done'
               WHERE s.version_id = %s AND s.mv_variant_id = %s
               GROUP BY s.substation_id
               ORDER BY s.substation_id""", (version_id, variant_id))
        table = pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])
        table["etrago_bus_id"] = pd.array([None if b is None or pd.isna(b) else int(b) for b in table["etrago_bus_id"]],
                                          dtype="Int64")
        return table

    def load_district_nodes(self, mv_grid_id: int) -> tuple[pd.DataFrame, dict]:
        """The nodes of one district in NODE_COLUMNS form (busbar first) and its grid row.

        Returns:
            ``(nodes, grid)``: ``grid`` has ``mv_grid_id``, ``version_id``, ``mv_variant_id``, ``substation_id``,
            ``vn_kv``, ``status``, ``osm_ratings_mva`` (list or None), ``in_region``, ``substation_flags`` and
            ``egon_bus_id`` (the eTraGo 110 kV bus of the substation, or None).

        Raises:
            LookupError: The grid does not exist.
        """
        self.cur.execute(
            """SELECT g.mv_grid_id, g.version_id, g.mv_variant_id, g.substation_id, g.vn_kv, g.status,
                      s.osm_ratings_mva, s.in_region, s.flags AS substation_flags, s.egon_bus_id
               FROM pylovo.mv_grid g JOIN pylovo.mv_substation s ON s.substation_id = g.substation_id
               WHERE g.mv_grid_id = %s""", (int(mv_grid_id),))
        row = self.cur.fetchone()
        if row is None:
            raise LookupError(f"MV grid {mv_grid_id} does not exist")
        grid = dict(zip([d[0] for d in self.cur.description], row))
        frame = self._node_rows("n.mv_grid_id = %(g)s", {"g": int(mv_grid_id)})
        return node_columns_frame(frame), grid

    # ------------------------------------------------------------------------------------------ district results
    def save_district(self, version_id: str, variant_id: str, mv_grid_id: int, plan: "DistrictPlan",
                      runtime_s: float, *, requirements: Mapping[int, float] | None = None) -> None:
        """Store the result of one district in one transaction; commits (rolls back and re-raises on error).

        Deletes the grid's lines (role other than tie), rings and joints, resets the result columns of its
        nodes, pre-allocates ids for the joints and lines, inserts the joints, updates the node results (role,
        feeder, ring, position, voltages, budgets, bridged, and the plan's node flags of
        ``plan.topology.diagnostics['node_flags']`` added to the region flags), inserts the lines and rings, writes the
        ``mv_node_id``/``mv_line_id`` columns into the stored net and sets the grid row (``status = 'done'``,
        counts, extremes, metrics, repairs, net) and the sizing columns of its substation.

        Args:
            plan: The district plan of :func:`pylovo.mv_grid.planner.plan_district`.
            runtime_s: Wall-clock time of the district (s).
            requirements: Stored LV requirement (``ext_grid.vm_pu``) per ``grid_result_id``, written to
                ``mv_node.lv_design_source_vm_pu`` (optional).
        """
        try:
            self._save_district(version_id, variant_id, int(mv_grid_id), plan, float(runtime_s), requirements)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _save_district(self, version_id: str, variant_id: str, mv_grid_id: int, plan: "DistrictPlan",
                       runtime_s: float, requirements: Mapping[int, float] | None) -> None:
        topo = plan.topology
        g_params = (version_id, variant_id, mv_grid_id)
        # 1. old results of this district
        self.cur.execute("""DELETE FROM pylovo.mv_line WHERE version_id = %s AND mv_variant_id = %s
                            AND mv_grid_id = %s AND role <> 'tie'""", g_params)
        self.cur.execute("""DELETE FROM pylovo.mv_ring WHERE version_id = %s AND mv_variant_id = %s
                            AND mv_grid_id = %s""", g_params)
        self.cur.execute("""DELETE FROM pylovo.mv_node WHERE version_id = %s AND mv_variant_id = %s
                            AND mv_grid_id = %s AND kind = 'joint'""", g_params)
        self.cur.execute(
            f"""UPDATE pylovo.mv_node SET {', '.join(f'{c} = NULL' for c in NODE_RESULT_COLUMNS)}
                WHERE version_id = %s AND mv_variant_id = %s AND mv_grid_id = %s""", g_params)
        # 2. the district's nodes (kind, area, rating) as stored
        self.cur.execute(
            """SELECT mv_node_id, kind, area_class, sn_kva, grid_result_id, connection_hint FROM pylovo.mv_node
               WHERE version_id = %s AND mv_variant_id = %s AND mv_grid_id = %s ORDER BY mv_node_id""", g_params)
        stored = {int(r[0]): {"kind": r[1], "area_class": r[2], "sn_kva": _py_float(r[3]),
                              "grid_result_id": _py_int(r[4]), "connection_hint": r[5]}
                  for r in self.cur.fetchall()}
        busbar_ids = [k for k, v in stored.items() if v["kind"] == "busbar"]
        # 3. ids for joints and lines
        joint_keys = sorted(topo.joints, reverse=True)       # -1, -2, ... in that order
        joint_ids = self._allocate_ids("mv_node", "mv_node_id", len(joint_keys))
        key_to_id: dict[int, int] = {k: k for k in stored}
        key_to_id.update({k: int(i) for k, i in zip(joint_keys, joint_ids)})
        lines = sorted(topo.lines, key=lambda ln: ln.key)
        line_ids = self._allocate_ids("mv_line", "mv_line_id", len(lines))
        line_key_to_id = {ln.key: int(i) for ln, i in zip(lines, line_ids)}
        for ln in lines:
            for key in (ln.from_key, ln.to_key):
                if key not in key_to_id:
                    raise ValueError(f"line {ln.key} of MV grid {mv_grid_id} references node key {key}, which is "
                                     "neither a node of the grid nor a joint")
        # 4. joints
        if joint_keys:
            execute_values(
                self.cur,
                """INSERT INTO pylovo.mv_node (mv_node_id, version_id, mv_variant_id, mv_grid_id, kind,
                        connection_hint, connection, feeder_id, ring_id, geom) VALUES %s""",
                [(key_to_id[k], version_id, variant_id, mv_grid_id, "joint", "joint", "joint",
                  _py_int(topo.node_feeder.get(k)), _py_int(topo.node_ring.get(k)),
                  float(topo.joints[k][0]), float(topo.joints[k][1])) for k in joint_keys],
                template=f"(%s, %s, %s, %s, %s, %s, %s, %s, %s, ST_SetSRID(ST_MakePoint(%s, %s), {int(TARGET_EPSG)}))",
                page_size=1000)
        # 5. node results
        cases = plan.cases or {}
        vm = {name: (case.vm_pu if case is not None and case.converged else None) for name, case in cases.items()}

        def vm_of(key: int, names: Sequence[str], how: str) -> float | None:
            values = []
            for name in names:
                series = vm.get(name)
                if series is not None and key in series.index:
                    value = _py_float(series.loc[key])
                    if value is not None:
                        values.append(value)
            if not values:
                return None
            return min(values) if how == "min" else max(values)

        bridged = {int(k) for k in (plan.metrics or {}).get("bridged_nodes", []) or []}
        plan_flags = {int(k): v for k, v in ((topo.diagnostics or {}).get("node_flags") or {}).items()}
        node_values = []
        for key in sorted(stored):
            budget = plan.budgets.get(key) if plan.budgets else None
            gr_id = stored[key]["grid_result_id"]
            requirement = None
            if requirements is not None and gr_id is not None:
                requirement = _py_float(requirements.get(gr_id))
            node_values.append((
                key, topo.node_role.get(key, "busbar" if stored[key]["kind"] == "busbar" else None),
                _py_int(topo.node_feeder.get(key)), _py_int(topo.node_ring.get(key)),
                _py_int(topo.node_position.get(key)), _py_int(key_to_id.get(topo.stub_parent.get(key))
                                                              if key in topo.stub_parent else None),
                vm_of(key, ("load",), "min"), vm_of(key, _N1_CASES, "min"), vm_of(key, _FEED_IN_CASES, "max"),
                None if budget is None else int(budget.tap_pos),
                None if budget is None else _py_float(budget.lv_busbar_vm_pu),
                None if budget is None else _py_float(budget.lv_min_vm_pu),
                None if budget is None else _py_float(budget.lv_busbar_vm_feed_in_pu),
                None if budget is None else bool(budget.band_conflict),
                key in bridged, requirement, _flags(plan_flags.get(key)),
            ))
        columns = ["mv_node_id", "connection", "feeder_id", "ring_id", "ring_position", "stub_parent", "vm_load_pu",
                   "vm_n1_pu", "vm_feed_in_pu", "mv_lv_tap_pos", "lv_busbar_vm_pu", "lv_min_vm_pu",
                   "lv_busbar_vm_feed_in_pu", "band_conflict", "bridged", "lv_design_source_vm_pu", "flags"]
        sets = [f"{c} = v.{c}" for c in columns[1:-3]]
        sets.append("bridged = n.bridged OR v.bridged")
        sets.append("lv_design_source_vm_pu = COALESCE(v.lv_design_source_vm_pu, n.lv_design_source_vm_pu)")
        # the region flags stay; the plan's flags (topology and electrical, planner.merged_node_flags) are added
        sets.append("flags = ARRAY(SELECT DISTINCT f FROM unnest(n.flags || v.flags) f ORDER BY f)")
        if node_values:
            execute_values(
                self.cur,
                f"""UPDATE pylovo.mv_node n SET {', '.join(sets)}
                    FROM (VALUES %s) AS v({', '.join(columns)}) WHERE n.mv_node_id = v.mv_node_id""",
                node_values, template=_cast_template(columns), page_size=1000)
        # 6. lines
        loading = {name: (case.loading_percent if case is not None and case.converged else None)
                   for name, case in cases.items()}

        def loading_of(key: int, names: Sequence[str]) -> float | None:
            values = []
            for name in names:
                series = loading.get(name)
                if series is not None and key in series.index:
                    value = _py_float(series.loc[key])
                    if value is not None:
                        values.append(value)
            return max(values) if values else None

        line_values = []
        for ln in lines:
            geom = ln.geometry
            line_values.append((
                line_key_to_id[ln.key], version_id, variant_id, mv_grid_id, key_to_id[ln.from_key],
                key_to_id[ln.to_key], ln.role, ln.kind, _py_int(ln.feeder_id), _py_int(ln.ring_id), ln.std_type,
                _parallel_count(ln.parallel), max(float(ln.length_m) / 1000.0, 0.001),
                float(ln.cable_m or 0.0) / 1000.0,
                float(ln.overhead_m or 0.0) / 1000.0, _py_float(ln.straight_m / 1000.0 if ln.straight_m is not None
                                                                else None),
                float(ln.bridge_m or 0.0), bool(ln.normally_open),
                None if ln.open_at_key is None else key_to_id.get(ln.open_at_key),
                loading_of(ln.key, ("load",)), loading_of(ln.key, _N1_CASES), loading_of(ln.key, _FEED_IN_CASES),
                None if geom is None else geom.wkb,
            ))
        if line_values:
            execute_values(
                self.cur,
                """INSERT INTO pylovo.mv_line (mv_line_id, version_id, mv_variant_id, mv_grid_id, from_node, to_node,
                       role, kind, feeder_id, ring_id, std_type, parallel, length_km, cable_km, overhead_km,
                       straight_km, bridge_m, normally_open, open_at_node, loading_load_percent, loading_n1_percent,
                       loading_feed_in_percent, geom) VALUES %s""",
                line_values,
                template=("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                          f"ST_GeomFromWKB(%s, {int(TARGET_EPSG)}))"),
                page_size=1000)
        # 7. rings
        busbar_vm = None
        if busbar_ids and vm.get("load") is not None and busbar_ids[0] in vm["load"].index:
            busbar_vm = _py_float(vm["load"].loc[busbar_ids[0]])
        ring_rows = _ring_rows(plan, stored, key_to_id, vm, loading, busbar_vm)
        if ring_rows:
            execute_values(
                self.cur,
                """INSERT INTO pylovo.mv_ring (version_id, mv_variant_id, mv_grid_id, ring_id, kind, n_members,
                       n_stations, n_customers, n_generators, n_stub_nodes, length_km, half_a_km, half_b_km, stub_km,
                       overhead_km, p_ring_kw, p_half_a_kw, p_half_b_kw, nop_node, max_drop_percent,
                       max_drop_n1_percent, max_loading_percent, max_loading_n1_percent, area_class) VALUES %s""",
                [(version_id, variant_id, mv_grid_id, *row) for row in ring_rows], page_size=1000)
        # 8. stored net with ids
        net_json = _net_json(plan.net, key_to_id, line_key_to_id)
        # 9. grid row and substation sizing
        grid = _grid_values(plan, stored, busbar_vm, vm, loading, runtime_s)
        # the region step's entry of the nominal voltage rule (set_grid_voltages) stays in the metrics
        assignments = ", ".join(
            ("metrics = %(metrics)s::jsonb || CASE WHEN metrics ? 'voltage_level' "
             "THEN jsonb_build_object('voltage_level', metrics -> 'voltage_level') ELSE '{}'::jsonb END")
            if c == "metrics" else f"{c} = %({c})s::jsonb" if c == "repairs" else f"{c} = %({c})s" for c in grid)
        self.cur.execute(
            f"""UPDATE pylovo.mv_grid SET status = 'done', error = NULL, {assignments}, net = %(net)s::json
                WHERE mv_grid_id = %(g)s AND version_id = %(v)s AND mv_variant_id = %(var)s""",
            {**grid, "net": net_json, "g": mv_grid_id, "v": version_id, "var": variant_id})
        if self.cur.rowcount != 1:
            raise LookupError(f"MV grid {mv_grid_id} of variant {variant_id} of version {version_id} does not exist")
        metrics = plan.metrics or {}
        self.cur.execute(
            """UPDATE pylovo.mv_substation s
               SET n_transformers = %(n)s, transformer_mva = %(mva)s, transformer_sizing = %(sizing)s,
                   peak_load_mw = %(peak)s, peak_feed_in_mw = %(feed)s, loading_n0_percent = %(n0)s,
                   loading_n1_percent = %(n1)s
               FROM pylovo.mv_grid g WHERE g.mv_grid_id = %(g)s AND s.substation_id = g.substation_id""",
            {"n": _py_int(plan.hv_mv_units), "mva": _py_float(plan.hv_mv_unit_mva), "sizing": plan.hv_mv_sizing,
             "peak": _py_float(plan.peak_load_mw), "feed": _py_float(plan.peak_feed_in_mw),
             "n0": _py_float(metrics.get("hv_mv_loading_n0_percent")),
             "n1": _py_float(metrics.get("hv_mv_loading_n1_percent")), "g": mv_grid_id})

    def mark_grid_failed(self, mv_grid_id: int, error: str) -> None:
        """Set ``status = 'failed'`` and the error text (one line, at most 2,000 characters) of a grid; commits."""
        text = " ".join(str(error).split())[:2000]
        try:
            self.cur.execute("UPDATE pylovo.mv_grid SET status = 'failed', error = %s WHERE mv_grid_id = %s",
                             (text, int(mv_grid_id)))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def variant_totals(self, version_id: str, variant_id: str) -> dict:
        """Grid counts by status and the summed results of a variant (for :class:`MvRunSummary`); ``grids_by_kv``
        counts the grids per nominal voltage (``{"20": n, "10": m}``)."""
        self.cur.execute(
            """SELECT count(*), count(*) FILTER (WHERE status = 'done'), count(*) FILTER (WHERE status = 'failed'),
                       count(*) FILTER (WHERE status = 'pending'),
                       COALESCE(sum(n_stations), 0), COALESCE(sum(n_customers), 0), COALESCE(sum(n_generators), 0),
                       COALESCE(sum(circuit_km), 0), COALESCE(sum(cable_km), 0),
                       count(*) FILTER (WHERE power_flow_status = ANY(%s)), COALESCE(sum(n_band_conflicts), 0)
                FROM pylovo.mv_grid WHERE version_id = %s AND mv_variant_id = %s""",
            (list(_VIOLATION_STATUSES), version_id, variant_id))
        row = self.cur.fetchone()
        keys = ("grids_total", "grids_done", "grids_failed", "grids_pending", "n_stations", "n_customers",
                "n_generators", "circuit_km", "cable_km", "n_violation_grids", "n_band_conflicts")
        out = {k: (float(v) if k in ("circuit_km", "cable_km") else int(v)) for k, v in zip(keys, row)}
        out["cable_share"] = out["cable_km"] / out["circuit_km"] if out["circuit_km"] > 0 else 0.0
        self.cur.execute(
            """SELECT g.vn_kv, count(*) FROM pylovo.mv_grid g WHERE g.version_id = %s AND g.mv_variant_id = %s
               GROUP BY g.vn_kv ORDER BY g.vn_kv DESC""", (version_id, variant_id))
        out["grids_by_kv"] = {f"{float(vn):g}": int(n) for vn, n in self.cur.fetchall() if vn is not None}
        return out

    def finish_variant(self, version_id: str, variant_id: str, summary: "MvRunSummary",
                       extra: Mapping[str, Any] | None = None) -> None:
        """Store the run summary, the final status and ``finished_at`` of a variant; commits.

        Args:
            extra: Further entries of the stored summary next to the fields of ``summary`` (e.g. ``ties``).
        """
        data = summary.to_dict()
        if extra:
            data.update(extra)
        try:
            self.cur.execute(
                """UPDATE pylovo.mv_variant SET status = %s, summary = %s::jsonb, finished_at = now()
                   WHERE version_id = %s AND mv_variant_id = %s""",
                (summary.status, _dumps(data), version_id, variant_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def delete_variants(self, version_id: str, variant_ids: Sequence[str] | None) -> int:
        """Delete MV variants of a version with all their results (cascade); commits.

        Args:
            variant_ids: The variants to delete; None or empty deletes every MV variant of the version.

        Returns:
            The number of deleted variants.
        """
        try:
            if variant_ids:
                self.cur.execute("DELETE FROM pylovo.mv_variant WHERE version_id = %s AND mv_variant_id = ANY(%s)",
                                 (version_id, [str(v) for v in variant_ids]))
            else:
                self.cur.execute("DELETE FROM pylovo.mv_variant WHERE version_id = %s", (version_id,))
            count = int(self.cur.rowcount)
            self.conn.commit()
            return count
        except Exception:
            self.conn.rollback()
            raise

    def list_variants(self, version_id: str | None) -> pd.DataFrame:
        """The MV variants with generation mode/status, electrical outcome counts, km and a ``stale`` flag.

        ``stale`` is True when the LV fingerprint of the variant's PLZ (``MvInputMixin.lv_fingerprint``) differs
        from the one stored at creation (load edits, deleted or regenerated LV grids), None when it cannot be
        computed.
        """
        where = "WHERE v.version_id = %(v)s" if version_id is not None else ""
        self.cur.execute(
            f"""SELECT v.version_id, v.mv_variant_id, v.mv_variant_comment, v.status,
                       COALESCE((v.mv_parameters #>> '{{generation,include_generation}}')::boolean, TRUE)
                           AS include_generation,
                       v.plz_list, v.graph_id,
                       v.lv_fingerprint, v.created_at, v.finished_at,
                       COALESCE(s.n_grids, 0) AS n_grids, COALESCE(s.n_done, 0) AS n_done,
                       COALESCE(s.n_failed, 0) AS n_failed, s.n_stations, s.circuit_km, s.cable_share,
                       COALESCE(e.n_electrical_pass, 0) AS n_electrical_pass,
                       COALESCE(e.n_electrical_violations, 0) AS n_electrical_violations,
                       COALESCE(e.n_electrical_not_checked, 0) AS n_electrical_not_checked
                FROM pylovo.mv_variant v
                LEFT JOIN pylovo.mv_variant_stats s ON s.version_id = v.version_id
                                                     AND s.mv_variant_id = v.mv_variant_id
                LEFT JOIN (
                    SELECT version_id, mv_variant_id,
                           count(*) FILTER (WHERE power_flow_status = 'converged') AS n_electrical_pass,
                           count(*) FILTER (WHERE power_flow_status = ANY(%(violations)s))
                               AS n_electrical_violations,
                           count(*) FILTER (WHERE power_flow_status IS NULL OR power_flow_status NOT IN
                               ('converged', 'voltage_violation', 'loading_violation', 'not_converged'))
                               AS n_electrical_not_checked
                    FROM pylovo.mv_grid GROUP BY version_id, mv_variant_id
                ) e ON e.version_id = v.version_id AND e.mv_variant_id = v.mv_variant_id
                {where}
                ORDER BY v.version_id, v.mv_variant_id""",
            {"v": version_id, "violations": list(_VIOLATION_STATUSES)})
        names = [d[0] for d in self.cur.description]
        df = pd.DataFrame(self.cur.fetchall(), columns=names)
        if df.empty:
            df["n_plz"] = pd.Series(dtype=int)
            df["stale"] = pd.Series(dtype=object)
            return df
        df["n_plz"] = df["plz_list"].map(lambda p: len(p or []))
        stale = []
        for record in df.to_dict("records"):
            stored = record["lv_fingerprint"]
            if isinstance(stored, str):
                stored = json.loads(stored)
            self.cur.execute("SAVEPOINT mv_list_fingerprint")
            try:
                current = self.lv_fingerprint(record["version_id"], record["plz_list"] or [])
                stale.append(_json_safe(current) != _json_safe(stored) if stored is not None else None)
                self.cur.execute("RELEASE SAVEPOINT mv_list_fingerprint")
            except Exception as exc:   # e.g. a version whose LV results were deleted
                self.logger.debug(f"LV fingerprint of {record['version_id']}/{record['mv_variant_id']}: {exc}")
                self.cur.execute("ROLLBACK TO SAVEPOINT mv_list_fingerprint")
                stale.append(None)
        df["stale"] = stale
        return df

    # ------------------------------------------------------------------------------------------ readers
    def _read_geo(self, query: str, params: dict, geom_col: str = "geom") -> "gpd.GeoDataFrame":
        import geopandas as gpd

        with self.sqla_engine.begin() as connection:
            return gpd.read_postgis(query, con=connection, params=params, geom_col=geom_col)

    def read_mv_layers(self, version_id: str, variant_id: str, mv_grid_id: int | None = None
                       ) -> dict[str, "gpd.GeoDataFrame"]:
        """The map layers of a variant (or of one of its grids) for plots: never reads ``mv_grid.net``.

        Returns:
            ``{"nodes", "lines", "substations", "grids", "settlements"}``, each a GeoDataFrame in TARGET_EPSG
            (``settlements``: the settlements of the variant's routing graph that touch its nodes).
        """
        params = {"v": version_id, "var": variant_id, "g": None if mv_grid_id is None else int(mv_grid_id)}
        grid_filter = "" if mv_grid_id is None else " AND mv_grid_id = %(g)s"
        layers = {
            "nodes": self._read_geo(
                f"SELECT {', '.join(_NODE_LAYER_COLUMNS)} FROM pylovo.mv_node "
                f"WHERE version_id = %(v)s AND mv_variant_id = %(var)s{grid_filter} ORDER BY mv_node_id", params),
            "lines": self._read_geo(
                "SELECT mv_line_id, version_id, mv_variant_id, mv_grid_id, from_node, to_node, role, kind, feeder_id,"
                " ring_id, std_type, parallel, length_km, cable_km, overhead_km, straight_km, bridge_m,"
                " normally_open, open_at_node, loading_load_percent, loading_n1_percent, loading_feed_in_percent,"
                " geom FROM pylovo.mv_line "
                f"WHERE version_id = %(v)s AND mv_variant_id = %(var)s{grid_filter} ORDER BY mv_line_id", params),
            "grids": self._read_geo(
                f"SELECT {', '.join(_GRID_LAYER_COLUMNS)} FROM pylovo.mv_grid "
                f"WHERE version_id = %(v)s AND mv_variant_id = %(var)s{grid_filter} ORDER BY mv_grid_id", params),
        }
        substation_filter = "" if mv_grid_id is None else \
            " AND substation_id IN (SELECT substation_id FROM pylovo.mv_grid WHERE mv_grid_id = %(g)s)"
        layers["substations"] = self._read_geo(
            "SELECT substation_id, version_id, mv_variant_id, source, source_ids, name, operator, voltage_tag,"
            " egon_bus_id, egon_distance_m, in_region, attach_offset_m, osm_ratings_mva, n_transformers,"
            " transformer_mva, transformer_sizing, peak_load_mw, peak_feed_in_mw, loading_n0_percent,"
            " loading_n1_percent, flags, geom FROM pylovo.mv_substation "
            f"WHERE version_id = %(v)s AND mv_variant_id = %(var)s{substation_filter} ORDER BY substation_id", params)
        layers["settlements"] = self._read_geo(
            f"""SELECT st.graph_id, st.settlement_id, st.n_buildings, st.length_km, st.urban, st.geom
                FROM pylovo.mv_graph_settlement st
                JOIN pylovo.mv_variant v ON v.graph_id = st.graph_id
                WHERE v.version_id = %(v)s AND v.mv_variant_id = %(var)s
                  AND st.settlement_id IN (SELECT DISTINCT settlement_id FROM pylovo.mv_node
                                           WHERE version_id = %(v)s AND mv_variant_id = %(var)s{grid_filter}
                                             AND settlement_id IS NOT NULL)
                ORDER BY st.settlement_id""", params)
        return layers

    def read_mv_rings(self, version_id: str, variant_id: str) -> pd.DataFrame:
        """The ``pylovo.mv_ring`` rows of a variant."""
        self.cur.execute(
            """SELECT r.*, g.complete FROM pylovo.mv_ring r JOIN pylovo.mv_grid g ON g.mv_grid_id = r.mv_grid_id
               WHERE r.version_id = %s AND r.mv_variant_id = %s ORDER BY r.mv_grid_id, r.ring_id""",
            (version_id, variant_id))
        return pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])

    def read_variant_stats(self, version_id: str | None = None) -> pd.DataFrame:
        """The view ``pylovo.mv_variant_stats`` (of one version, or all)."""
        if version_id is None:
            self.cur.execute("SELECT * FROM pylovo.mv_variant_stats ORDER BY version_id, mv_variant_id")
        else:
            self.cur.execute("SELECT * FROM pylovo.mv_variant_stats WHERE version_id = %s ORDER BY mv_variant_id",
                             (version_id,))
        return pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])

    def read_mv_grid_net(self, mv_grid_id: int) -> "pp.pandapowerNet":
        """The stored pandapower net of an MV grid (HV side, fixed OLTC tap, load-case results).

        Raises:
            LookupError: The grid does not exist or has no stored net (not done).
        """
        import pandapower as pp

        self.cur.execute("SELECT net::text FROM pylovo.mv_grid WHERE mv_grid_id = %s", (int(mv_grid_id),))
        row = self.cur.fetchone()
        if row is None or row[0] is None:
            raise LookupError(f"MV grid {mv_grid_id} has no stored net")
        return pp.from_json_string(row[0])

    def save_coupled_results(self, mv_grid_id: int, rows: pd.DataFrame, status: str, metrics: dict) -> None:
        """Store the results of ``pylovo-mv couple`` for one grid; commits.

        Args:
            rows: ``mv_node_id`` and any of the ``coupled_*`` columns of ``mv_node``.
            status: ``done``, ``partial`` or ``failed`` (``mv_grid.coupled_status``).
            metrics: Coupling diagnostics, stored as ``metrics.coupling`` of the grid.
        """
        try:
            rows = pd.DataFrame(rows).reset_index(drop=True)
            columns = ["mv_node_id"] + [c for c in COUPLED_COLUMNS if c in rows.columns]
            if len(rows) and len(columns) > 1:
                values = [tuple(_py(v) for v in rec) for rec in rows[columns].itertuples(index=False, name=None)]
                execute_values(
                    self.cur,
                    f"""UPDATE pylovo.mv_node n SET {', '.join(f'{c} = v.{c}' for c in columns[1:])}
                        FROM (VALUES %s) AS v({', '.join(columns)})
                        WHERE n.mv_node_id = v.mv_node_id AND n.mv_grid_id = {int(mv_grid_id)}""",
                    values, template=_cast_template(columns), page_size=1000)
            self.cur.execute(
                """UPDATE pylovo.mv_grid
                   SET coupled_status = %s,
                       metrics = COALESCE(metrics, '{}'::jsonb) || jsonb_build_object('coupling', %s::jsonb)
                   WHERE mv_grid_id = %s""",
                (status, _dumps(metrics or {}), int(mv_grid_id)))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------------------------------ ties (WP14)
    def tie_grids(self, version_id: str, variant_id: str) -> pd.DataFrame:
        """The districts of a variant for the tie step (:mod:`pylovo.mv_grid.interconnection`).

        Returns:
            One row per ``mv_grid`` (index ``mv_grid_id``): ``substation_id``, ``status``, ``vn_kv``, ``complete``,
            ``in_region`` (of the substation), ``n_stations``, ``n_customers``, ``n_rings`` and ``circuit_km``
            (without ties).
        """
        self.cur.execute(
            """SELECT g.mv_grid_id, g.substation_id, g.status, g.vn_kv, g.complete, s.in_region, g.n_stations,
                      g.n_customers, g.n_rings, g.circuit_km
               FROM pylovo.mv_grid g JOIN pylovo.mv_substation s ON s.substation_id = g.substation_id
               WHERE g.version_id = %s AND g.mv_variant_id = %s
               ORDER BY g.mv_grid_id""", (version_id, variant_id))
        frame = pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])
        frame.index = pd.Index(frame.pop("mv_grid_id").astype("int64"), name="mv_grid_id")
        return frame

    def tie_candidate_nodes(self, version_id: str, variant_id: str) -> pd.DataFrame:
        """The nodes that may hold a tie or a link: stations and MV customers looped into an open ring of a done
        district or on a stub of one (ties take only the ring members).

        Returns:
            Index ``mv_node_id``: ``mv_grid_id``, ``kind``, ``connection``, ``ring_id``, ``ring_kind``,
            ``feeder_id``, ``ring_position``, ``x``, ``y``, ``attach_edge_id``, ``attach_fraction``,
            ``attach_offset_m``, ``settlement_id``, ``area_class``, ``degree`` (the MV lines at the node,
            without ties and links: 1 at a stub end) and ``settlement_type`` (of the node's PLZ: 1 rural, 2
            semi-urban, 3 urban; customers take the type of their PLZ in ``postcode_result``), ordered by
            ``mv_node_id``.
        """
        variant = (version_id, variant_id)
        self.cur.execute(
            """WITH ends AS (
                   SELECT from_node AS node FROM pylovo.mv_line
                   WHERE version_id = %s AND mv_variant_id = %s AND role NOT IN ('tie', 'link')
                   UNION ALL
                   SELECT to_node FROM pylovo.mv_line
                   WHERE version_id = %s AND mv_variant_id = %s AND role NOT IN ('tie', 'link')),
               degree AS (SELECT node, count(*) AS degree FROM ends GROUP BY node)
               SELECT n.mv_node_id, n.mv_grid_id, n.kind, n.connection, n.ring_id, r.kind AS ring_kind, n.feeder_id,
                      n.ring_position, ST_X(n.geom) AS x, ST_Y(n.geom) AS y, n.attach_edge_id, n.attach_fraction,
                      n.attach_offset_m, n.settlement_id, n.area_class, coalesce(d.degree, 0) AS degree,
                      coalesce(n.plz_settlement_type, pr.settlement_type) AS settlement_type
               FROM pylovo.mv_node n
               JOIN pylovo.mv_grid g ON g.mv_grid_id = n.mv_grid_id
               JOIN pylovo.mv_ring r ON r.mv_grid_id = n.mv_grid_id AND r.ring_id = n.ring_id
               LEFT JOIN degree d ON d.node = n.mv_node_id
               LEFT JOIN pylovo.postcode_result pr
                      ON pr.version_id = n.version_id AND pr.postcode_result_plz = n.plz
               WHERE n.version_id = %s AND n.mv_variant_id = %s AND g.status = 'done' AND r.kind = 'ring'
                 AND n.connection IN ('ring', 'stub') AND n.kind IN ('station', 'customer')
               ORDER BY n.mv_node_id""", (*variant, *variant, *variant))
        frame = pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])
        frame.index = pd.Index(frame.pop("mv_node_id").astype("int64"), name="mv_node_id")
        for column in ("mv_grid_id", "ring_id", "feeder_id", "ring_position", "attach_edge_id", "attach_fraction",
                       "attach_offset_m", "settlement_id", "x", "y", "degree", "settlement_type"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        return frame

    def save_ties(self, version_id: str, variant_id: str, ties: pd.DataFrame,
                  grid_metrics: Mapping[int, dict], role: str = "tie") -> int:
        """Replace the normally-open ties (or, with ``role`` link, the links inside the districts) of a variant in one
        transaction; commits (rolls back and re-raises on error).

        Deletes the variant's ``mv_line`` rows of the role, resets ``mv_ring.has_tie`` (ties only) and removes
        ``mv_grid.metrics['ties']`` (``['links']``). Then inserts one ``mv_line`` per row (the role,
        ``normally_open``, ``open_at_node = to_node``, ``mv_grid_id`` of the ``from_node``, no ring or feeder, no
        loadings), sets ``mv_ring.has_tie`` on the rings at both ends of a tie and writes ``grid_metrics`` as
        ``mv_grid.metrics['ties']`` (``['links']``).

        Args:
            ties: Ties of :func:`pylovo.mv_grid.interconnection.plan_ties` (``from_node``, ``to_node``,
                ``from_grid``, ``to_grid``, ``from_ring``, ``to_ring``, ``kind``, ``std_type``, ``length_m``,
                ``cable_m``, ``overhead_m``, ``straight_m``, ``geometry``).
            grid_metrics: ``{mv_grid_id: entry}`` of :func:`pylovo.mv_grid.interconnection.grid_tie_metrics`.

        Returns:
            The number of stored ties.
        """
        if role not in ("tie", "link"):
            raise ValueError(f"role must be tie or link, got {role!r}")
        key = "ties" if role == "tie" else "links"
        variant = (version_id, variant_id)
        ties = pd.DataFrame(ties).reset_index(drop=True)
        try:
            self.cur.execute("""DELETE FROM pylovo.mv_line
                                WHERE version_id = %s AND mv_variant_id = %s AND role = %s""", (*variant, role))
            if role == "tie":
                self.cur.execute("""UPDATE pylovo.mv_ring SET has_tie = false
                                    WHERE version_id = %s AND mv_variant_id = %s AND has_tie""", variant)
            self.cur.execute("""UPDATE pylovo.mv_grid SET metrics = metrics - %s
                                WHERE version_id = %s AND mv_variant_id = %s AND metrics ? %s""",
                             (key, *variant, key))
            ids = self._allocate_ids("mv_line", "mv_line_id", len(ties))
            if len(ties):
                values = [(
                    int(ids[i]), version_id, variant_id, int(tie["from_grid"]), int(tie["from_node"]),
                    int(tie["to_node"]), role, str(tie["kind"]), str(tie["std_type"]),
                    _parallel_count(tie.get("parallel", 1)),
                    max(float(tie["length_m"]) / 1000.0, 0.001), float(tie["cable_m"] or 0.0) / 1000.0,
                    float(tie["overhead_m"] or 0.0) / 1000.0, _py_float(float(tie["straight_m"]) / 1000.0), 0.0, True,
                    int(tie["to_node"]), tie["geometry"].wkb,
                ) for i, tie in enumerate(ties.to_dict("records"))]
                execute_values(
                    self.cur,
                    """INSERT INTO pylovo.mv_line (mv_line_id, version_id, mv_variant_id, mv_grid_id, from_node, to_node,
                           role, kind, std_type, parallel, length_km, cable_km, overhead_km, straight_km, bridge_m,
                           normally_open, open_at_node, geom) VALUES %s""",
                    values,
                    template=("(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                              f"ST_GeomFromWKB(%s, {int(TARGET_EPSG)}))"),
                    page_size=1000)
                if role == "tie":
                    rings = sorted({(int(g), int(r)) for g, r in zip(ties["from_grid"], ties["from_ring"])}
                                   | {(int(g), int(r)) for g, r in zip(ties["to_grid"], ties["to_ring"])})
                    execute_values(
                        self.cur,
                        """UPDATE pylovo.mv_ring r SET has_tie = true FROM (VALUES %s) AS v(mv_grid_id, ring_id)
                           WHERE r.mv_grid_id = v.mv_grid_id AND r.ring_id = v.ring_id""",
                        rings, template="(%s::bigint, %s::integer)", page_size=1000)
            if grid_metrics:
                execute_values(
                    self.cur,
                    f"""UPDATE pylovo.mv_grid g
                        SET metrics = COALESCE(g.metrics, '{{}}'::jsonb) || jsonb_build_object('{key}', v.entry)
                        FROM (VALUES %s) AS v(mv_grid_id, entry) WHERE g.mv_grid_id = v.mv_grid_id""",
                    [(int(g), _dumps(entry)) for g, entry in sorted(grid_metrics.items())],
                    template="(%s::bigint, %s::jsonb)", page_size=1000)
            self.conn.commit()
            return int(len(ties))
        except Exception:
            self.conn.rollback()
            raise

    def read_ties(self, version_id: str, variant_id: str) -> pd.DataFrame:
        """The normally-open ties of a variant with the district and ring of both ends (no geometry).

        Returns:
            One row per tie, ordered by ``mv_line_id``: ``mv_line_id``, ``from_grid`` (= ``mv_grid_id``),
            ``from_node``, ``from_ring``, ``to_grid``, ``to_node``, ``to_ring``, ``kind``, ``std_type``,
            ``length_km``, ``cable_km``, ``overhead_km``, ``straight_km``, ``geom_km``, ``normally_open`` and
            ``open_at_node``.
        """
        self.cur.execute(
            """SELECT l.mv_line_id, l.mv_grid_id AS from_grid, l.from_node, a.ring_id AS from_ring,
                      b.mv_grid_id AS to_grid, l.to_node, b.ring_id AS to_ring, l.kind, l.std_type, l.length_km,
                      l.cable_km, l.overhead_km, l.straight_km, ST_Length(l.geom) / 1000.0 AS geom_km,
                      l.normally_open, l.open_at_node
               FROM pylovo.mv_line l
               JOIN pylovo.mv_node a ON a.mv_node_id = l.from_node
               JOIN pylovo.mv_node b ON b.mv_node_id = l.to_node
               WHERE l.version_id = %s AND l.mv_variant_id = %s AND l.role = 'tie'
               ORDER BY l.mv_line_id""", (version_id, variant_id))
        return pd.DataFrame(self.cur.fetchall(), columns=[d[0] for d in self.cur.description])

    def display_input(self, version_id: str, variant_id: str) -> tuple[list, dict[int, tuple[float, float]]]:
        """The lines of a variant for :func:`pylovo.mv_grid.display.layout` and its busbar nodes (id -> x, y).

        A line's path key is (MV grid, ring, role); lines without a ring (ties, links) form no paths.
        """
        import shapely

        from pylovo.mv_grid.display import DisplayLine

        self.cur.execute(
            """SELECT mv_line_id, from_node, to_node, mv_grid_id, ring_id, role, ST_AsBinary(geom)
               FROM pylovo.mv_line WHERE version_id = %s AND mv_variant_id = %s ORDER BY mv_line_id""",
            (version_id, variant_id))
        lines = [DisplayLine(int(i), shapely.from_wkb(bytes(wkb)), int(a), int(b),
                             None if ring is None else (int(grid), int(ring), role))
                 for i, a, b, grid, ring, role, wkb in self.cur.fetchall()]
        self.cur.execute(
            """SELECT mv_node_id, ST_X(geom), ST_Y(geom) FROM pylovo.mv_node
               WHERE version_id = %s AND mv_variant_id = %s AND kind = 'busbar'""", (version_id, variant_id))
        return lines, {int(n): (float(x), float(y)) for n, x, y in self.cur.fetchall()}

    def save_display_geometry(self, version_id: str, variant_id: str, lines: Mapping[int, Any],
                              busbars: Mapping[int, Any]) -> None:
        """Replace the display geometry of a variant: ``lines`` (mv_line_id -> LineString) and ``busbars`` (busbar
        node id -> circle Polygon, stored on the node's substation); every other row gets NULL. Commits."""
        try:
            key = (version_id, variant_id)
            self.cur.execute("UPDATE pylovo.mv_line SET display_geom = NULL "
                             "WHERE version_id = %s AND mv_variant_id = %s AND display_geom IS NOT NULL", key)
            self.cur.execute("UPDATE pylovo.mv_substation SET display_geom = NULL "
                             "WHERE version_id = %s AND mv_variant_id = %s AND display_geom IS NOT NULL", key)
            epsg = int(TARGET_EPSG)
            if lines:
                execute_values(
                    self.cur,
                    f"""UPDATE pylovo.mv_line l SET display_geom = ST_GeomFromWKB(v.wkb, {epsg})
                        FROM (VALUES %s) AS v(mv_line_id, wkb)
                        WHERE l.mv_line_id = v.mv_line_id""",
                    [(int(i), g.wkb) for i, g in lines.items()], template="(%s::bigint, %s::bytea)", page_size=1000)
            if busbars:
                execute_values(
                    self.cur,
                    f"""UPDATE pylovo.mv_substation s SET display_geom = ST_GeomFromWKB(v.wkb, {epsg})
                        FROM (VALUES %s) AS v(mv_node_id, wkb)
                        JOIN pylovo.mv_node n ON n.mv_node_id = v.mv_node_id
                        WHERE s.substation_id = n.substation_id""",
                    [(int(n), g.wkb) for n, g in busbars.items()], template="(%s::bigint, %s::bytea)")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise


# ---------------------------------------------------------------------------------------------- helpers
def _ring_rows(plan: "DistrictPlan", stored: Mapping[int, dict], key_to_id: Mapping[int, int],
               vm: Mapping[str, Any], loading: Mapping[str, Any], busbar_vm: float | None) -> list[tuple]:
    """``mv_ring`` values per planned ring (without version, variant and grid)."""
    topo = plan.topology
    lines_by_ring: dict[int, list] = {}
    for ln in topo.lines:
        if ln.ring_id is not None and ln.role != "tie":
            lines_by_ring.setdefault(int(ln.ring_id), []).append(ln)
    stub_nodes = Counter(int(topo.node_ring[k]) for k, role in topo.node_role.items()
                         if role == "stub" and k in topo.node_ring and topo.node_ring[k] is not None)
    rows = []
    for ring in sorted(topo.rings, key=lambda r: r.ring_id):
        rid = int(ring.ring_id)
        members = [m for m in ring.members if m in stored]
        kinds = Counter(stored[m]["kind"] for m in members)
        n_members = sum(1 for m in members if stored[m]["kind"] in ("station", "customer", "generator"))
        ring_lines = lines_by_ring.get(rid, [])
        stub_m = sum(ln.length_m for ln in ring_lines if ln.role == "stub") if ring.kind != "radial" else 0.0
        total_m = sum(ln.length_m for ln in ring_lines)
        overhead_m = sum(ln.overhead_m or 0.0 for ln in ring_lines)
        check = ring.check

        def worst(names: Sequence[str], how: str, keys: Sequence[int], table: Mapping[str, Any]) -> float | None:
            values = []
            for name in names:
                series = table.get(name)
                if series is None:
                    continue
                present = [k for k in keys if k in series.index]
                if present:
                    part = pd.to_numeric(series.loc[present], errors="coerce").dropna()
                    if len(part):
                        values.append(float(part.min() if how == "min" else part.max()))
            if not values:
                return None
            return min(values) if how == "min" else max(values)

        node_keys = [k for k, r in topo.node_ring.items() if r is not None and int(r) == rid]
        line_keys = [ln.key for ln in ring_lines]
        vm_min = worst(("load",), "min", node_keys, vm)
        vm_n1 = worst(_N1_CASES, "min", node_keys, vm)
        drop = None if busbar_vm is None or vm_min is None else 100.0 * (busbar_vm - vm_min)
        drop_n1 = None if busbar_vm is None or vm_n1 is None else 100.0 * (busbar_vm - vm_n1)
        areas = Counter(stored[m]["area_class"] for m in members if stored[m]["area_class"])
        area = sorted(a for a, c in areas.items() if c == max(areas.values()))[0] if areas else None
        rows.append((
            rid, ring.kind, n_members, kinds.get("station", 0), kinds.get("customer", 0), kinds.get("generator", 0),
            int(stub_nodes.get(rid, 0)), (total_m - stub_m) / 1000.0,
            None if check is None else _py_float(check.half_a_m / 1000.0),
            None if check is None else _py_float(check.half_b_m / 1000.0),
            stub_m / 1000.0, overhead_m / 1000.0,
            None if check is None else _py_float(check.p_ring_kw),
            None if check is None else _py_float(check.p_a_kw),
            None if check is None else _py_float(check.p_b_kw),
            None if ring.nop_key is None else key_to_id.get(ring.nop_key),
            drop, drop_n1, worst(("load",), "max", line_keys, loading), worst(_N1_CASES, "max", line_keys, loading),
            area,
        ))
    return rows


def _series_extreme(cases_table: Mapping[str, Any], names: Sequence[str], how: str,
                    keys: Sequence[int] | None = None) -> float | None:
    values = []
    for name in names:
        series = cases_table.get(name)
        if series is None or not len(series):
            continue
        part = series if keys is None else series.loc[[k for k in keys if k in series.index]]
        part = pd.to_numeric(part, errors="coerce").dropna()
        if len(part):
            values.append(float(part.min() if how == "min" else part.max()))
    if not values:
        return None
    return min(values) if how == "min" else max(values)


def _grid_values(plan: "DistrictPlan", stored: Mapping[int, dict], busbar_vm: float | None, vm: Mapping[str, Any],
                 loading: Mapping[str, Any], runtime_s: float) -> dict[str, Any]:
    """The result columns of ``mv_grid`` for one district plan."""
    topo = plan.topology
    roles = topo.node_role
    modelled = [k for k, v in stored.items() if roles.get(k) != "excluded" and v["kind"] != "busbar"]
    kinds = Counter(stored[k]["kind"] for k in modelled)
    circuits = [ln for ln in topo.lines if ln.role != "tie"]
    # mv_line stores a minimum 1 m per route, including zero-length switch sections.
    route_km = sum(max(float(ln.length_m) / 1000.0, 0.001) for ln in circuits)
    circuit_km = sum(max(float(ln.length_m) / 1000.0, 0.001) * _parallel_count(ln.parallel) for ln in circuits)
    cable_km = sum((ln.cable_m or 0.0) * _parallel_count(ln.parallel) for ln in circuits) / 1000.0
    overhead_km = sum((ln.overhead_m or 0.0) * _parallel_count(ln.parallel) for ln in circuits) / 1000.0
    busbars = {k for k, v in stored.items() if v["kind"] == "busbar"}
    bays = sum(1 for ln in circuits if ln.from_key in busbars or ln.to_key in busbars)
    metrics = plan.metrics or {}
    non_busbar = [k for k in stored if k not in busbars] + list(topo.joints)
    vm_min = _series_extreme(vm, ("load",), "min", non_busbar)
    vm_n1 = _series_extreme(vm, _N1_CASES, "min", non_busbar)
    vm_feed = _series_extreme(vm, _FEED_IN_CASES, "max", non_busbar)
    budgets = list((plan.budgets or {}).values())
    return {
        "power_flow_status": plan.power_flow_status,
        "hv_mv_transformers": _py_int(plan.hv_mv_units),
        "hv_mv_transformer_mva": _py_float(plan.hv_mv_unit_mva),
        "hv_mv_sizing": plan.hv_mv_sizing,
        "oltc_tap_pos": _py_int(plan.oltc_tap),
        "peak_load_mw": _py_float(plan.peak_load_mw),
        "peak_feed_in_mw": _py_float(plan.peak_feed_in_mw),
        "sum_station_kva": float(sum(stored[k]["sn_kva"] or 0.0 for k in modelled if stored[k]["kind"] == "station")),
        "n_stations": kinds.get("station", 0),
        "n_customers": kinds.get("customer", 0),
        "n_generators": kinds.get("generator", 0),
        "n_joints": len(topo.joints),
        "n_rings": sum(1 for r in topo.rings if r.kind == "ring"),
        "n_stub_nodes": sum(1 for k, role in roles.items() if role == "stub" and k in stored),
        "n_feeder_bays": int(metrics.get("n_feeder_bays") or bays),
        "circuit_km": circuit_km,
        "cable_km": cable_km,
        "overhead_km": overhead_km,
        "trench_km": _py_float(metrics.get("trench_km")),
        "bridge_km": sum(ln.bridge_m or 0.0 for ln in circuits) / 1000.0,
        "max_voltage_drop_percent": None if busbar_vm is None or vm_min is None else 100.0 * (busbar_vm - vm_min),
        "max_voltage_drop_n1_percent": None if busbar_vm is None or vm_n1 is None else 100.0 * (busbar_vm - vm_n1),
        "max_voltage_rise_percent": None if busbar_vm is None or vm_feed is None else 100.0 * (vm_feed - busbar_vm),
        "max_line_loading_percent": _series_extreme(loading, ("load",), "max"),
        "max_line_loading_n1_percent": _series_extreme(loading, _N1_CASES, "max"),
        "max_line_loading_feed_in_percent": _series_extreme(loading, _FEED_IN_CASES, "max"),
        "n_band_conflicts": sum(1 for b in budgets if b.band_conflict),
        "n_tap_limit": sum(1 for b in budgets if b.tap_limit),
        "metrics": _dumps({**metrics, "route_km": route_km, "circuit_km": circuit_km,
                           "cable_km": cable_km, "overhead_km": overhead_km,
                           "cable_share": cable_km / circuit_km if circuit_km else None,
                           "km_per_supply_point": circuit_km / (kinds.get("station", 0) + kinds.get("customer", 0))
                           if kinds.get("station", 0) + kinds.get("customer", 0) else None,
                           "km_per_station": circuit_km / kinds.get("station", 0) if kinds.get("station", 0) else None,
                           "circuit_km_per_substation": circuit_km,
                           "violations": [dataclasses.asdict(v) for v in plan.violations or []]}),
        "repairs": _dumps(list(plan.repairs or [])),
        "runtime_s": float(runtime_s),
    }


def _net_json(net: Any, key_to_id: Mapping[int, int], line_key_to_id: Mapping[int, int]) -> str | None:
    """``pp.to_json`` of the stored net with ``mv_node_id``/``mv_line_id`` filled from the plan keys."""
    if net is None:
        return None
    import pandapower as pp

    def ids(keys: pd.Series, mapping: Mapping[int, int]) -> np.ndarray:
        # float64 with NaN where a row has no plan key (e.g. the HV busbar), as the electrical module creates them
        return np.array([float(mapping[int(k)]) if pd.notna(k) and int(k) in mapping else np.nan for k in keys],
                        dtype=np.float64)

    for table in ("bus", "load", "sgen"):
        frame = getattr(net, table, None)
        if frame is not None and "node_key" in frame.columns:
            frame["mv_node_id"] = ids(frame["node_key"], key_to_id)
    for table in ("line", "switch"):
        frame = getattr(net, table, None)
        if frame is not None and "line_key" in frame.columns:
            frame["mv_line_id"] = ids(frame["line_key"], line_key_to_id)
    return pp.to_json(net)
