"""Shared helpers of the MV grid notebooks in this folder (read-only).

The notebooks change to the repository root first and import this module afterwards (see their Setup cell), so
that pylovo reads the ``.env`` and ``config/`` of this checkout. Every function only reads from the database:
the client of :func:`read_only_client` runs read-only transactions.

- :func:`read_only_client`, :func:`resolve`, :func:`default_variant`, :func:`current_variants`: the database client,
  the LV version and the MV variants to analyse.
- :func:`region_parts`, :func:`load_variant`: the parts of the planning region (separate groups of PLZ) and the
  results of one MV variant (:class:`Variant`) with the region part of every district and the region filter.
- :func:`district_table`, :func:`ring_table`, :func:`summary_table`, :func:`case_table`, :func:`clipped_table`,
  :func:`demand_anchors`: tables per district, ring, variant/region/area class and power-flow case, the circuit
  length per km² clipped to the region and the demand anchor.
- :func:`flat_parameters`, :func:`variant_parameters`, :func:`parameter_table`, :func:`dimension_of`: the parameter
  snapshots of variants.
- :func:`plot_ring_map`, :func:`region_layers`, :func:`plot_generic_grid`: maps coloured per ring;
  :func:`plot_voltage_budget`: the voltage budget of the stations.
- :func:`pretty`, :func:`show_blocks`: tables for display; :func:`match_district`, :func:`district_pairs`:
  districts of two variants with the same substation.
- Benchmarks: :func:`generic_tables`, :func:`grid_metrics`, :func:`generic_rings_and_feeders` (the generic metric
  code of :mod:`pylovo.mv_grid.metrics`), :func:`literature_table` and :func:`traffic_light_table`,
  :func:`egon_districts`, :func:`egon_comparison`, :func:`read_ding0_district`, :func:`earth_fault_table`.
- Electrical details: :func:`repairs_table`, :func:`flag_counts`, :func:`coupling_table`, :func:`coupling_left_out`.

Variants planned with another MV stage version (column ``mv_stage_version`` of ``pylovo-mv list``) are never selected
by default, but an explicit selection reads them as stored.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

#: Session options of the read-only SQLAlchemy engine (GeoDataFrame readers).
READ_ONLY_OPTIONS = "-c search_path=pylovo,public -c default_transaction_read_only=on"
#: Polygons of the planning region closer than this (m) belong to the same region part.
REGION_GAP_M = 5000.0
#: Parameter groups of the variant snapshot that define the comparisons of the variant notebook (first match).
DIMENSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("solver", ("topology.local_search_rounds", "topology.neighbours", "topology.ls_time_guard_s")),
    ("routing", ("routing.",)),
    ("sources", ("sources.",)),
    ("stubs and links", ("topology.stub_", "topology.link_", "topology.tie_")),
    ("equipment and rules", ("",)),
)


def _end(dbc: Any) -> None:
    """End the read transaction of the psycopg2 connection (no idle transaction is left open)."""
    dbc.conn.rollback()


def read_only_client() -> Any:
    """A :class:`~pylovo.database.database_client.DatabaseClient` whose transactions are read-only.

    Both the psycopg2 connection and the SQLAlchemy engine of the GeoDataFrame readers refuse every write.
    """
    from sqlalchemy import create_engine

    from pylovo.database.database_client import DatabaseClient

    dbc = DatabaseClient()
    dbc.conn.set_session(readonly=True)
    dbc.sqla_engine.dispose()
    dbc.sqla_engine = create_engine(dbc.db_path, connect_args={"options": READ_ONLY_OPTIONS})
    return dbc


def current_variants(listing: pd.DataFrame) -> list[str]:
    """The done variants of a version listing (``pylovo-mv list``) of the current MV stage version, in listing
    order."""
    from pylovo.mv_grid.options import MV_STAGE_VERSION

    current = (listing["status"] == "done") & (listing["mv_stage_version"] == MV_STAGE_VERSION)
    return [str(v) for v in listing.loc[current, "mv_variant_id"]]


def default_variant(listing: pd.DataFrame) -> str | None:
    """The default MV variant of a version listing (``pylovo-mv list``).

    ``MV_VARIANT_ID`` of ``config/config_generation_mv.yaml`` if the version has this variant, else the newest done
    variant of the current MV stage version (latest ``finished_at``). None if there is neither.
    """
    from pylovo import config_loader

    configured = str(config_loader.MV_VARIANT_ID)
    if configured in set(listing["mv_variant_id"].astype(str)):
        return configured
    current = listing[listing["mv_variant_id"].astype(str).isin(current_variants(listing))]
    if current.empty:
        return None
    newest = current.sort_values(["finished_at", "created_at"], ascending=False, na_position="last")
    return str(newest["mv_variant_id"].iloc[0])


def resolve(dbc: Any, version_id: str | None = None, variants: Sequence[str] | None = None,
            select: str = "default") -> tuple[str, list[str], pd.DataFrame]:
    """The LV version and MV variants to analyse, and the variant listing of the version (``pylovo-mv list``).

    Args:
        dbc: Database client.
        version_id: LV version; None: ``VERSION_ID`` of ``config/config_generation_lv.yaml``.
        variants: MV variants; None: selected by ``select``. Unknown ids are reported and dropped.
        select: The selection when ``variants`` is None: ``"default"`` the one variant of :func:`default_variant`,
            ``"current"`` all done variants of the current MV stage version (:func:`current_variants`).

    Returns:
        ``(version_id, variants, listing)``.
    """
    from pylovo import config_loader
    from pylovo.mv_grid.options import MV_STAGE_VERSION

    if select not in ("default", "current"):
        raise ValueError(f"select must be 'default' or 'current', got {select!r}")
    version = str(version_id or config_loader.VERSION_ID)
    listing = dbc.list_variants(version)
    _end(dbc)
    available = [str(v) for v in listing["mv_variant_id"]]
    if variants is None and select == "current":
        chosen = current_variants(listing)
    elif variants is None:
        default = default_variant(listing)
        chosen = [default] if default else []
    else:
        chosen = [str(v) for v in variants if str(v) in available]
        missing = [str(v) for v in variants if str(v) not in available]
        if missing:
            print(f"MV variants not in version {version}: {', '.join(missing)}")
    stages = dict(zip(available, listing["mv_stage_version"]))
    for name in chosen:
        if stages.get(name) != MV_STAGE_VERSION:
            print(f"{name}: planned with MV stage version {stages.get(name)}; its stored results are read as they are.")
    if not chosen:
        print(f"Version {version} has no done MV variant to analyse; run pylovo-mv generate first or set variants.")
    return version, chosen, listing


def region_parts(dbc: Any, plz_list: Iterable[int], gap_m: float = REGION_GAP_M) -> Any:
    """The parts of the planning region: groups of PLZ polygons less than ``gap_m`` apart.

    Returns:
        GeoDataFrame (``TARGET_EPSG``) with ``region`` (1 = largest part), ``area_km2``, ``n_plz``, ``plz`` (sorted
        list) and the union of the PLZ polygons as geometry.
    """
    import geopandas as gpd
    import shapely

    from pylovo.config_loader import TARGET_EPSG

    plz = sorted({int(p) for p in plz_list})
    dbc.cur.execute("SELECT plz, ST_AsBinary(geom) FROM pylovo.postcode WHERE plz = ANY(%s) ORDER BY plz", (plz,))
    rows = dbc.cur.fetchall()
    _end(dbc)
    polygons = gpd.GeoDataFrame({"plz": [int(r[0]) for r in rows]},
                                geometry=[shapely.from_wkb(bytes(r[1])) for r in rows], crs=f"EPSG:{TARGET_EPSG}")
    merged = shapely.union_all(polygons.geometry.buffer(gap_m / 2.0).to_numpy())
    groups = list(getattr(merged, "geoms", [merged]))
    records = []
    for group in groups:
        members = polygons[polygons.intersects(group)]
        records.append({"plz": sorted(members["plz"]), "n_plz": len(members),
                        "geometry": shapely.union_all(members.geometry.to_numpy())})
    parts = gpd.GeoDataFrame(records, geometry="geometry", crs=polygons.crs)
    parts["area_km2"] = parts.area / 1e6
    parts = parts.sort_values("area_km2", ascending=False).reset_index(drop=True)
    parts.insert(0, "region", np.arange(1, len(parts) + 1))
    return parts


@dataclass
class Variant:
    """The results of one MV variant (layers of ``MvResultMixin.read_mv_layers``, rings and the variant row).

    ``grids``, ``nodes``, ``lines`` and ``rings`` carry the region part of their district (``region``, 1 = the
    largest part); ``grids`` and ``rings`` also an ``area_class``.
    """

    version_id: str
    variant_id: str
    label: str
    row: dict
    nodes: Any
    lines: Any
    grids: Any
    substations: Any
    settlements: Any
    rings: pd.DataFrame
    parts: Any

    @property
    def layers(self) -> dict:
        """The layers for the map functions of :mod:`pylovo.plotting.mv_grid`."""
        return {"nodes": self.nodes, "lines": self.lines, "grids": self.grids, "substations": self.substations,
                "settlements": self.settlements}

    @property
    def params(self) -> dict:
        """The parameter snapshot of the variant (``mv_variant.mv_parameters``)."""
        return self.row.get("mv_parameters") or {}

    @property
    def summary(self) -> dict:
        """The run summary of the variant (``mv_variant.summary``)."""
        return self.row.get("summary") or {}

    def snapshot(self, path: str, default: Any = None) -> Any:
        """One value of the parameter snapshot by dotted path, e.g. ``"taps.lv_design_tolerance_pu"``."""
        value: Any = self.params
        for key in path.split("."):
            if not isinstance(value, Mapping) or key not in value:
                return default
            value = value[key]
        return value


def _nearest_part(points: Any, parts: Any) -> list[int]:
    """The region part nearest to every point (0 for a missing point)."""
    out: list[int] = []
    for point in points:
        if point is None or getattr(point, "is_empty", True):
            out.append(0)
            continue
        distance = parts.distance(point)
        out.append(int(parts.loc[distance.idxmin(), "region"]))
    return out


def load_variant(dbc: Any, version_id: str, variant_id: str, parts: Any = None, regions: Sequence[int] | None = None,
                 plz: Sequence[int] | None = None, label: str | None = None) -> Variant:
    """Read one MV variant and keep the districts of the region filter.

    A district belongs to the region part nearest to its substation. ``regions`` keeps the districts of these
    parts, ``plz`` the districts with at least one node in these PLZ; whole districts are kept.

    Args:
        dbc: Database client.
        version_id: LV version.
        variant_id: MV variant.
        parts: Region parts of :func:`region_parts`; None: the parts of the variant's PLZ.
        regions: Region parts to keep (``region`` numbers); None: all.
        plz: PLZ whose districts are kept; None: all.
        label: Label of the variant in tables and plots; default the variant id.
    """
    row = dbc.variant_row(version_id, variant_id)
    if row is None:
        raise LookupError(f"MV variant {variant_id} of version {version_id} does not exist")
    layers = dbc.read_mv_layers(version_id, variant_id)
    rings = dbc.read_mv_rings(version_id, variant_id)
    _end(dbc)
    if parts is None:
        parts = region_parts(dbc, row["plz_list"])
    nodes, lines, grids = layers["nodes"], layers["lines"], layers["grids"]
    substations, settlements = layers["substations"], layers["settlements"]
    points = substations.set_index("substation_id").geometry.reindex(grids["substation_id"])
    grids["region"] = np.asarray(_nearest_part(list(points), parts), dtype=np.int64)
    grids["area_class"] = grids["metrics"].map(lambda m: m.get("area_class") if isinstance(m, Mapping) else None)
    keep = pd.Series(True, index=grids.index)
    if regions:
        keep &= grids["region"].isin([int(r) for r in regions])
    if plz:
        with_plz = set(nodes.loc[nodes["plz"].isin([int(p) for p in plz]), "mv_grid_id"].dropna().astype(int))
        keep &= grids["mv_grid_id"].isin(with_plz)
    grids = grids[keep.to_numpy()].copy()
    ids = set(grids["mv_grid_id"].astype(int))
    region_of = dict(zip(grids["mv_grid_id"].astype(int), grids["region"]))
    nodes = nodes[nodes["mv_grid_id"].isin(ids)].copy()
    lines = lines[lines["mv_grid_id"].isin(ids)].copy()
    rings = rings[rings["mv_grid_id"].isin(ids)].copy()
    for frame in (nodes, lines, rings):
        frame["region"] = np.asarray([region_of[int(g)] for g in frame["mv_grid_id"]], dtype=np.int64)
    used = set(grids["substation_id"].astype(int))
    if regions or plz:
        substations = substations[substations["substation_id"].isin(used)].copy()
    substations["used"] = substations["substation_id"].isin(used)
    if len(settlements) and "settlement_id" in nodes.columns:
        settlements = settlements[settlements["settlement_id"].isin(set(nodes["settlement_id"].dropna()))].copy()
    return Variant(version_id=version_id, variant_id=variant_id, label=label or variant_id, row=row, nodes=nodes,
                   lines=lines, grids=grids, substations=substations, settlements=settlements, rings=rings,
                   parts=parts)


# ---------------------------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------------------------


def _busbar_customers(load_stats: Any) -> tuple[int, float]:
    record = load_stats.get("busbar_customers") if isinstance(load_stats, Mapping) else None
    if not isinstance(record, Mapping):
        return 0, 0.0
    return int(record.get("n") or 0), float(record.get("kw") or 0.0)


def _ratio(num: Any, den: Any) -> float:
    num, den = float(num), float(den)
    return num / den if den else float("nan")


def district_table(v: Variant) -> pd.DataFrame:
    """One row per district (MV grid) of a variant.

    Counts, circuit length, rings and half-rings (``mv_ring``), loading and voltage extremes per case, repairs,
    band conflicts and taps, peak load and runtime. ``n_customers`` are MV customers with an own MV node,
    ``customer_mw`` their own peak (``p_own_kw``), ``n_busbar_customers`` and ``busbar_customer_mw`` the MV
    customers below ``customer_min_mv_kw`` hung on the LV busbar of a station; ``n_ties`` and
    ``tie_km`` count the normally-open ties owned by the district (excluded from ``circuit_km``).
    """
    g = pd.DataFrame(v.grids.drop(columns=v.grids.geometry.name)).set_index("mv_grid_id", drop=False)
    metrics = g["metrics"].map(lambda m: m if isinstance(m, Mapping) else {})
    nodes, lines, rings = v.nodes, v.lines, v.rings
    stations = nodes[nodes["kind"] == "station"]
    busbar = pd.DataFrame([_busbar_customers(s) for s in stations["load_stats"]], columns=["n", "kw"],
                          index=stations.index)
    busbar["mv_grid_id"] = stations["mv_grid_id"]
    loads = nodes[nodes["kind"].isin(["station", "customer"])]
    ties = lines[lines["role"] == "tie"].copy()
    installed = lines[lines["role"] != "tie"].copy()
    for frame in (installed, ties):
        parallel = pd.to_numeric(frame["parallel"], errors="coerce").fillna(1)
        frame["_circuit_km"] = pd.to_numeric(frame["length_km"], errors="coerce") * parallel
        frame["_cable_km"] = pd.to_numeric(frame["cable_km"], errors="coerce") * parallel
        frame["_overhead_km"] = pd.to_numeric(frame["overhead_km"], errors="coerce") * parallel
    nops = lines[lines["normally_open"].astype(bool) & (lines["role"] != "tie")]
    ring_rows = rings[rings["kind"] == "ring"]
    halves = pd.concat([ring_rows[["mv_grid_id", "half_a_km"]].rename(columns={"half_a_km": "km"}),
                        ring_rows[["mv_grid_id", "half_b_km"]].rename(columns={"half_b_km": "km"})])
    feeders = rings.groupby(["mv_grid_id", "kind"]).size().unstack(fill_value=0)
    repairs = g["repairs"].map(lambda r: r if isinstance(r, list) else [])

    def per_grid(frame: pd.DataFrame, column: str, how: str) -> pd.Series:
        if frame.empty:
            return pd.Series(np.nan, index=g.index, dtype=float)
        values = pd.to_numeric(frame[column], errors="coerce")
        return values.groupby(frame["mv_grid_id"].astype(int)).agg(how).reindex(g.index)

    def count(frame: pd.DataFrame) -> pd.Series:
        return frame.groupby(frame["mv_grid_id"].astype(int)).size().reindex(g.index, fill_value=0)

    in_region = v.substations.set_index("substation_id")["in_region"].reindex(g["substation_id"]).to_numpy()
    out = pd.DataFrame({
        "version": v.version_id, "variant": v.label, "region": g["region"], "mv_grid_id": g["mv_grid_id"],
        "substation_id": g["substation_id"], "area_class": g["area_class"], "status": g["status"],
        "power_flow_status": g["power_flow_status"], "coupled_status": g["coupled_status"],
        "complete": g["complete"].astype("boolean"), "substation_in_region": in_region,
        "area_km2": g["area_km2"], "vn_kv": g["vn_kv"],
        "hv_mv_units": g["hv_mv_transformers"], "hv_mv_unit_mva": g["hv_mv_transformer_mva"],
        "peak_load_mw": g["peak_load_mw"], "sum_station_mva": g["sum_station_kva"] / 1000.0,
        "n_stations": g["n_stations"], "n_customers": g["n_customers"],
        "n_busbar_customers": busbar.groupby(busbar["mv_grid_id"].astype(int))["n"].sum().reindex(g.index)
        .fillna(0).astype(int),
        "busbar_customer_mw": busbar.groupby(busbar["mv_grid_id"].astype(int))["kw"].sum().reindex(g.index)
        .fillna(0.0) / 1000.0,
        "customer_mw": per_grid(nodes[nodes["kind"] == "customer"], "p_own_kw", "sum").fillna(0.0) / 1000.0,
        "n_dedicated_customers": count(nodes[(nodes["kind"] == "customer") & (nodes["connection"] == "dedicated")]),
        "n_joints": g["n_joints"],
        "n_rings": feeders["ring"].reindex(g.index, fill_value=0) if "ring" in feeders else 0,
        "n_dedicated_feeders": feeders["dedicated"].reindex(g.index, fill_value=0) if "dedicated" in feeders else 0,
        "n_radial_feeders": feeders["radial"].reindex(g.index, fill_value=0) if "radial" in feeders else 0,
        "n_stub_nodes": g["n_stub_nodes"], "n_feeder_bays": g["n_feeder_bays"],
        "n_nops": count(nops), "n_ties": count(ties),
        "tie_km": per_grid(ties, "_circuit_km", "sum").fillna(0.0),
        "route_km": per_grid(installed, "length_km", "sum").fillna(0.0),
        "circuit_km": per_grid(installed, "_circuit_km", "sum").fillna(0.0),
        "cable_km": per_grid(installed, "_cable_km", "sum").fillna(0.0),
        "overhead_km": per_grid(installed, "_overhead_km", "sum").fillna(0.0),
        "trench_km": g["trench_km"],
        "ring_members_mean": per_grid(ring_rows, "n_members", "mean"),
        "ring_members_max": per_grid(ring_rows, "n_members", "max"),
        "ring_km_mean": per_grid(ring_rows, "length_km", "mean"),
        "ring_km_max": per_grid(ring_rows, "length_km", "max"),
        "half_ring_km_mean": per_grid(halves, "km", "mean"),
        "half_ring_km_median": per_grid(halves, "km", "median"),
        "half_ring_km_max": per_grid(halves, "km", "max"),
        "station_spacing_m": metrics.map(lambda m: m.get("station_spacing_m")).astype(float),
        "n1_secure_share": metrics.map(lambda m: m.get("n1_secure_share")).astype(float),
        "max_loading_percent": g["max_line_loading_percent"],
        "max_loading_n1_percent": g["max_line_loading_n1_percent"],
        "max_drop_percent": g["max_voltage_drop_percent"],
        "max_drop_n1_percent": g["max_voltage_drop_n1_percent"],
        "min_vm_load_pu": per_grid(loads, "vm_load_pu", "min"),
        "min_vm_n1_pu": per_grid(loads, "vm_n1_pu", "min"),
        "hv_mv_loading_n1_percent": metrics.map(lambda m: m.get("hv_mv_loading_n1_percent")).astype(float),
        "n_repairs": repairs.map(len),
        "repairs": repairs.map(lambda r: ", ".join(f"{k} {n}" for k, n in
                                                   sorted(Counter(x.get("action") for x in r).items()))),
        "n_band_conflicts": g["n_band_conflicts"], "n_tap_limit": g["n_tap_limit"],
        "n_tap_nonzero": count(stations[pd.to_numeric(stations["mv_lv_tap_pos"], errors="coerce").fillna(0) != 0]),
        "length_savings_m": metrics.map(lambda m: m.get("length_savings_m")).astype(float),
        "length_final_m": metrics.map(lambda m: m.get("length_final_m")).astype(float),
        "solver_gain_percent": metrics.map(lambda m: m.get("solver_gain_percent")).astype(float),
        "solver_runtime_s": metrics.map(lambda m: m.get("solver_runtime_s")).astype(float),
        "runtime_s": g["runtime_s"],
        "violations": metrics.map(lambda m: ", ".join(str(x) for x in (m.get("violations") or []))),
    })
    out["n_supply_points"] = out["n_stations"].fillna(0) + out["n_customers"].fillna(0)
    out["cable_share"] = out["cable_km"] / out["circuit_km"]
    out["km_per_station"] = out["circuit_km"] / out["n_stations"]
    out["km_per_supply_point"] = out["circuit_km"] / out["n_supply_points"]
    out["km_per_km2"] = out["circuit_km"] / out["area_km2"]
    out["customer_share"] = out["n_customers"] / out["n_supply_points"]
    return out.reset_index(drop=True)


def ring_table(v: Variant) -> pd.DataFrame:
    """The ``mv_ring`` rows of a variant with ``version``, ``variant`` and ``region``."""
    r = v.rings.copy()
    r.insert(0, "variant", v.label)
    r.insert(0, "version", v.version_id)
    return r


_SUMS = ("n_stations", "n_customers", "customer_mw", "n_dedicated_customers", "n_busbar_customers",
         "busbar_customer_mw", "n_rings", "n_dedicated_feeders", "n_radial_feeders", "n_stub_nodes", "n_feeder_bays",
         "n_nops", "n_ties", "tie_km", "route_km", "circuit_km", "cable_km", "overhead_km", "trench_km", "area_km2",
         "peak_load_mw", "n_repairs", "n_band_conflicts", "n_tap_limit", "n_tap_nonzero", "runtime_s")
_MAXES = ("max_loading_percent", "max_loading_n1_percent", "max_drop_percent", "max_drop_n1_percent")
_MINS = ("min_vm_load_pu", "min_vm_n1_pu")


def summary_table(districts: pd.DataFrame, rings: pd.DataFrame, by: Sequence[str] = ("variant",)) -> pd.DataFrame:
    """Totals and ratios per group of districts, with ring statistics pooled over the rings of the group.

    Districts are grouped by their columns ``by``; rings by the same columns of the ring table, where
    ``area_class`` is the ring's own majority class (a rural district can hold urban rings). Ratios are formed
    from the sums (``km_per_station`` = Σ circuit km / Σ stations); per-substation values are district means.
    """
    by = list(by)
    d = districts.copy()
    d["_done"] = d["status"] == "done"
    d["_failed"] = d["status"] == "failed"
    d["_converged"] = d["power_flow_status"] == "converged"
    d["_complete"] = d["complete"].fillna(False).astype(bool)
    grouped = d.groupby(by, dropna=False, sort=False)
    out = grouped.size().rename("n_districts").to_frame()
    out["n_done"] = grouped["_done"].sum()
    out["n_failed"] = grouped["_failed"].sum()
    out["n_complete"] = grouped["_complete"].sum()
    out["n_without_violation"] = grouped["_converged"].sum()
    for column in _SUMS:
        out[column] = grouped[column].sum(min_count=1)
    for column in _MAXES:
        out[column] = grouped[column].max()
    for column in _MINS:
        out[column] = grouped[column].min()
    out["n_supply_points"] = out["n_stations"] + out["n_customers"]
    out["cable_share"] = out["cable_km"] / out["circuit_km"]
    out["km_per_station"] = out["circuit_km"] / out["n_stations"]
    out["km_per_supply_point"] = out["circuit_km"] / out["n_supply_points"]
    out["km_per_km2_cells"] = out["circuit_km"] / out["area_km2"]
    out["customer_share"] = out["n_customers"] / out["n_supply_points"]
    out["supply_points_per_substation"] = grouped["n_supply_points"].mean()
    out["circuit_km_per_substation"] = grouped["circuit_km"].mean()
    out["feeders_per_substation"] = grouped["n_feeder_bays"].mean()
    r = rings[rings["kind"] == "ring"]
    if len(r):
        halves = pd.concat([r[by + ["half_a_km"]].rename(columns={"half_a_km": "km"}),
                            r[by + ["half_b_km"]].rename(columns={"half_b_km": "km"})])
        rg = r.groupby(by, dropna=False, sort=False)
        hg = halves.groupby(by, dropna=False, sort=False)["km"]
        ring_stats = pd.DataFrame({
            "ring_members_mean": rg["n_members"].mean(), "ring_members_median": rg["n_members"].median(),
            "ring_members_max": rg["n_members"].max(), "stations_per_ring": rg["n_stations"].mean(),
            "ring_km_mean": rg["length_km"].mean(), "ring_km_max": rg["length_km"].max(),
            "half_ring_km_mean": hg.mean(), "half_ring_km_median": hg.median(), "half_ring_km_max": hg.max(),
        })
        out = out.join(ring_stats, how="outer")
    return out


def clipped_table(variants: Sequence[Variant], by_region: bool = True) -> pd.DataFrame:
    """Circuit km per km² of the planning region (lines clipped to it), per variant and region part.

    Uses :func:`pylovo.analysis.mv_validation.region_metrics` on all lines of the variant (ties and open lines count, as
    in published circuit lengths); the area is the selected region parts. Columns of ``region_metrics``; region 0
    is all selected parts together.
    """
    import shapely

    from pylovo.analysis import mv_validation

    rows = []
    for v in variants:
        nodes, _ = generic_tables(v)
        parts = v.parts[v.parts["region"].isin(set(v.grids["region"].dropna().astype(int)))]
        scopes = [(0, shapely.union_all(parts.geometry.to_numpy()))]
        if by_region:
            scopes += [(int(r), geom) for r, geom in zip(parts["region"], parts.geometry)]
        for scope, geom in scopes:
            rows.append({"variant": v.label, "region": scope, **mv_validation.region_metrics(v.lines, geom, nodes)})
    return pd.DataFrame(rows).set_index(["variant", "region"])


def generic_tables(v: Variant, grid_ids: Iterable[int] | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The generic node and line tables of :mod:`pylovo.mv_grid.metrics` (all districts or ``grid_ids``)."""
    nodes, lines = v.nodes, v.lines
    if grid_ids is not None:
        ids = {int(g) for g in grid_ids}
        nodes = nodes[nodes["mv_grid_id"].isin(ids)]
        lines = lines[lines["mv_grid_id"].isin(ids)]
    gn = pd.DataFrame({"node_id": nodes["mv_node_id"].astype(int).to_numpy(), "kind": nodes["kind"].to_numpy(),
                       "x": nodes.geometry.x.to_numpy(), "y": nodes.geometry.y.to_numpy(),
                       "area_class": nodes["area_class"].to_numpy(), "mv_grid_id": nodes["mv_grid_id"].to_numpy()})
    gl = pd.DataFrame({"line_id": lines["mv_line_id"].astype(int).to_numpy(),
                       "from_node": lines["from_node"].astype(int).to_numpy(),
                       "to_node": lines["to_node"].astype(int).to_numpy(),
                       "length_km": lines["length_km"].astype(float).to_numpy(), "kind": lines["kind"].to_numpy(),
                       "cable_km": lines["cable_km"].astype(float).to_numpy(),
                       "overhead_km": lines["overhead_km"].astype(float).to_numpy(),
                       "normally_open": lines["normally_open"].astype(bool).to_numpy(),
                       "parallel": lines["parallel"].astype(int).to_numpy(),
                       "open_at_node": [int(n) if pd.notna(n) else None for n in lines["open_at_node"]],
                       "role": lines["role"].to_numpy(), "mv_grid_id": lines["mv_grid_id"].to_numpy()})
    return gn, gl


def demand_anchors(dbc: Any, v: Variant) -> pd.DataFrame:
    """The demand anchor of :func:`pylovo.mv_grid.inputs.demand_anchor` per scope of a variant.

    Scopes: ``all`` (the PLZ of the selected region parts), ``region`` (``key``: each region part) and
    ``area_class`` (``key``: the PLZ of each class; a PLZ takes the majority class of its stations). The load is
    pylovo's coincidence law on the stations and MV customers of the scope's PLZ (all districts), the population
    that of ``municipal_register``.
    """
    from pylovo.mv_grid.inputs import demand_anchor
    from pylovo.mv_grid.parameters import LvVersionParameters

    lv = LvVersionParameters.from_generation_parameters(v.version_id, dbc.lv_snapshot(v.version_id))
    table = dbc.variant_node_table(v.version_id, v.variant_id)
    _end(dbc)
    parts = v.parts[v.parts["region"].isin(set(v.grids["region"].dropna().astype(int)))]
    plz_all = sorted({int(p) for plz in parts["plz"] for p in plz} & set(v.row["plz_list"]))
    population = dbc.plz_population(plz_all)
    _end(dbc)
    stations = table[table["kind"] == "station"]
    urban_share = stations.groupby("plz")["urban"].mean()
    class_of_plz = {int(p): ("urban" if s > 0.5 else "rural") for p, s in urban_share.items()}
    scopes: list[tuple[str, Any, list[int]]] = [("all", None, plz_all)]
    scopes += [("region", int(r), sorted(set(plz) & set(plz_all))) for r, plz in zip(parts["region"], parts["plz"])]
    for cls in ("rural", "urban"):
        scopes.append(("area_class", cls, sorted(p for p in plz_all if class_of_plz.get(p) == cls)))
    rows = []
    for scope, key, plz in scopes:
        if not plz:
            continue
        anchor = demand_anchor(table[table["plz"].isin(plz)], population[population["plz"].isin(plz)], lv)
        rows.append({"variant": v.label, "scope": scope, "key": key, **{k: anchor[k] for k in (
            "kw_per_inhabitant", "coincident_mw", "population", "n_plz", "customer_mw", "n_customer_nodes",
            "busbar_customer_mw", "excluded_mw", "approximate")}})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------------------------
# Parameter snapshots
# ---------------------------------------------------------------------------------------------------------------


def flat_parameters(snapshot: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """The parameter snapshot as ``{dotted path: value}`` (lists stay values)."""
    out: dict[str, Any] = {}
    for key, value in snapshot.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            out.update(flat_parameters(value, path))
        else:
            out[path] = value
    return out


def variant_parameters(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """The parameter snapshot of an MV variant as ``{dotted path: value}`` (:func:`flat_parameters`)."""
    return flat_parameters(snapshot)


def parameter_table(variants: Sequence[Variant], only_differences: bool = True) -> pd.DataFrame:
    """Parameters of several variants side by side (rows: dotted snapshot paths of :func:`variant_parameters`,
    columns: variant labels)."""
    flat = {v.label: {k: (str(x) if isinstance(x, (list, tuple)) else x)
                      for k, x in variant_parameters(v.params).items()} for v in variants}
    table = pd.DataFrame(flat)
    if only_differences and len(table.columns) > 1:
        table = table[table.astype(str).nunique(axis=1) > 1]
    table.insert(0, "dimension", [dimension_of(k) for k in table.index])
    return table.sort_values("dimension", kind="stable")


def dimension_of(path: str) -> str:
    """The comparison dimension of a snapshot path (:data:`DIMENSIONS`, first match)."""
    for name, prefixes in DIMENSIONS:
        if any(path.startswith(p) for p in prefixes):
            return name
    return DIMENSIONS[-1][0]


# ---------------------------------------------------------------------------------------------------------------
# Maps
# ---------------------------------------------------------------------------------------------------------------


def region_layers(v: Variant, region: int) -> dict:
    """The map layers of the districts of one region part, with the substations without a district within
    :data:`REGION_GAP_M` of the part."""
    ids = set(v.grids.loc[v.grids["region"] == int(region), "mv_grid_id"].astype(int))
    used = set(v.grids.loc[v.grids["mv_grid_id"].isin(ids), "substation_id"].astype(int))
    part = v.parts.loc[v.parts["region"] == int(region)].geometry.iloc[0]
    near = v.substations.distance(part) <= REGION_GAP_M
    unused = ~v.substations["used"]
    nodes = v.nodes[v.nodes["mv_grid_id"].isin(ids)]
    settlements = v.settlements
    if len(settlements) and "settlement_id" in nodes.columns:
        settlements = settlements[settlements["settlement_id"].isin(set(nodes["settlement_id"].dropna()))]
    return {"nodes": nodes, "lines": v.lines[v.lines["mv_grid_id"].isin(ids)],
            "grids": v.grids[v.grids["mv_grid_id"].isin(ids)],
            "substations": v.substations[v.substations["substation_id"].isin(used) | (unused & near)],
            "settlements": settlements}


def plot_ring_map(v: Variant, regions: Sequence[int] | None = None, ax: Any = None, figsize=(12, 12),
                  basemap: bool = False, title: str | None = None) -> Any:
    """All districts of a variant on one map, every district coloured per ring.

    Draws :func:`pylovo.plotting.mv_grid.plot_mv_grid_map` (``color_by="ring"``) of each district into one axes:
    district cells dashed, overhead lines dashed, stub lines thinner, ties grey and dotted, normally-open points
    as open red circles; substations as triangles (hollow: substation without a district).
    """
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    from pylovo.plotting.mv_grid import maps
    from pylovo.plotting.mv_grid.maps import NODE_STYLES

    if ax is None:
        fig, ax = plt.subplots(figsize=figsize)
    else:
        fig = ax.get_figure()
    grids = v.grids if not regions else v.grids[v.grids["region"].isin(list(regions))]
    for gid in grids["mv_grid_id"].astype(int):
        maps.plot_mv_grid_map(v.layers, gid, color_by="ring", ax=ax, settlements=False)
    nodes = v.nodes[v.nodes["mv_grid_id"].isin(set(grids["mv_grid_id"]))]
    x, y = nodes.geometry.x, nodes.geometry.y
    pad = 0.04 * max(x.max() - x.min(), y.max() - y.min(), 1000.0)
    subs = v.substations
    if regions:
        parts = v.parts[v.parts["region"].isin(list(regions))]
        subs = subs[[parts.distance(p).min() <= REGION_GAP_M for p in subs.geometry]]
    unused = subs[~subs["used"]]
    if len(unused):
        ax.scatter(unused.geometry.x, unused.geometry.y, marker="^", s=110, facecolors="none",
                   edgecolors=NODE_STYLES["busbar"]["color"], linewidths=1.2, zorder=9)
    ax.set_xlim(x.min() - pad, x.max() + pad)
    ax.set_ylim(y.min() - pad, y.max() + pad)
    lines = v.lines[v.lines["mv_grid_id"].isin(set(grids["mv_grid_id"]))]
    handles = [Line2D([], [], color="#333333", linewidth=1.6, label="cable (colour: ring)")]
    if (lines["kind"] == "overhead").any():
        handles.append(Line2D([], [], color="#333333", linewidth=1.6, linestyle="--", label="overhead line"))
    if (lines["role"] == "stub").any():
        handles.append(Line2D([], [], color="#333333", linewidth=0.8, label="stub line"))
    if (lines["role"] == "tie").any():
        handles.append(Line2D([], [], color="#9a9a9a", linewidth=1.0, linestyle=":", label="tie (normally open)"))
    handles += [Line2D([], [], marker="^", linestyle="none", markerfacecolor=NODE_STYLES["busbar"]["color"],
                       markeredgecolor="white", markersize=11, label="HV/MV substation")]
    if len(unused):
        handles.append(Line2D([], [], marker="^", linestyle="none", markerfacecolor="none",
                              markeredgecolor=NODE_STYLES["busbar"]["color"], markersize=11,
                              label="substation without district"))
    handles += [Line2D([], [], marker="o", linestyle="none", markerfacecolor="none", markeredgecolor="#d62728",
                       markersize=9, label="normally-open point"),
                Line2D([], [], color="#444444", linewidth=0.9, linestyle="--", label="district cell")]
    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9)
    not_tie = lines["role"] != "tie"
    scope = f", region {', '.join(map(str, regions))}" if regions else ""
    n_rings = int(((v.rings["kind"] == "ring") & v.rings["mv_grid_id"].isin(set(grids["mv_grid_id"]))).sum())
    ax.set_title(title or f"{v.label}{scope}: {len(grids)} districts, {lines.loc[not_tie, 'length_km'].sum():,.0f} km, "
                 f"{n_rings} rings", fontsize=13, fontweight="bold")
    if basemap:
        add_basemap(ax, v.nodes.crs)
    return fig


def plot_voltage_budget(v: Variant, lv_reference_vm_pu: float, lv_min_limit_pu: float = 0.90,
                        title: str | None = None) -> Any:
    """The voltage budget of the stations of a variant (:func:`pylovo.plotting.mv_grid.plot_voltage_budget`) with
    the busbar, tap and tolerance values of its snapshot, from the HV/MV busbar to the lowest LV voltage."""
    from pylovo.plotting import mv_grid as mvplot

    return mvplot.plot_voltage_budget(
        v.nodes, busbar_vm_pu=v.snapshot("voltage.busbar_vm_pu", 1.02),
        busbar_band_pu=v.snapshot("voltage.busbar_band_pu", 0.015),
        tap_step_percent=v.snapshot("taps.step_percent", 2.5), lv_reference_vm_pu=lv_reference_vm_pu,
        lv_design_tolerance_pu=v.snapshot("taps.lv_design_tolerance_pu", 0.002), lv_min_limit_pu=lv_min_limit_pu,
        title=title)


def add_basemap(ax: Any, crs: Any) -> None:
    """OpenStreetMap tiles under a map (needs internet access; a failure is a warning)."""
    import warnings

    import contextily as cx

    from pylovo.plotting.utils import OSM_TILE_HEADERS

    try:
        cx.add_basemap(ax, crs=crs.to_string() if hasattr(crs, "to_string") else crs,
                       source=cx.providers.OpenStreetMap.Mapnik, headers=OSM_TILE_HEADERS,
                       attribution="Map data © OpenStreetMap contributors (ODbL)")
    except Exception as exc:  # the map is still useful without tiles
        warnings.warn(f"OpenStreetMap basemap not drawn: {exc}", stacklevel=2)


def generic_layers(nodes: pd.DataFrame, lines: pd.DataFrame) -> dict:
    """Map layers for :func:`pylovo.plotting.mv_grid.plot_mv_grid_map` from generic tables (e.g. a Ding0 grid).

    Node ids become integers; every line gets the ring of :func:`pylovo.mv_grid.metrics.node_roles` and the role
    ``stub`` off the main paths; lines without geometry are drawn straight between their nodes.
    """
    from pylovo.mv_grid import metrics

    nodes, lines = metrics.normalise_tables(nodes, lines)
    roles = metrics.node_roles(nodes, lines).set_index("node_id")
    code = {n: i for i, n in enumerate(nodes["node_id"])}
    layer_nodes = pd.DataFrame({"mv_node_id": [code[n] for n in nodes["node_id"]], "kind": nodes["kind"].to_numpy(),
                                "x": nodes["x"].to_numpy(), "y": nodes["y"].to_numpy(),
                                "ring_id": roles["ring"].reindex(nodes["node_id"]).to_numpy()})
    internal = lines[lines["from_node"].isin(code) & lines["to_node"].isin(code)]

    def ring_of(a: Any, b: Any) -> Any:
        ra, rb = roles["ring"].get(a), roles["ring"].get(b)
        return ra if pd.notna(ra) else rb

    def role_of(a: Any, b: Any) -> str:
        return "stub" if "stub" in (roles["position"].get(a), roles["position"].get(b)) else "ring"

    layer_lines = pd.DataFrame({
        "mv_line_id": np.arange(len(internal)),
        "from_node": [code[a] for a in internal["from_node"]], "to_node": [code[b] for b in internal["to_node"]],
        "kind": internal["kind"].to_numpy(), "length_km": internal["length_km"].to_numpy(),
        "normally_open": internal["normally_open"].to_numpy(),
        "open_at_node": [code.get(n) if n is not None else None for n in internal["open_at_node"]],
        "parallel": internal["parallel"].to_numpy(),
        "ring_id": [ring_of(a, b) for a, b in zip(internal["from_node"], internal["to_node"])],
        "role": [role_of(a, b) for a, b in zip(internal["from_node"], internal["to_node"])]})
    return {"nodes": layer_nodes, "lines": layer_lines}


def plot_generic_grid(nodes: pd.DataFrame, lines: pd.DataFrame, ax: Any = None, title: str | None = None,
                      figsize=(9, 9)) -> Any:
    """Map of a grid in the generic format coloured per ring (``plot_mv_grid_map`` on :func:`generic_layers`)."""
    from pylovo.plotting.mv_grid import plot_mv_grid_map

    layers = generic_layers(nodes, lines)
    return plot_mv_grid_map(layers, 0, color_by="ring", ax=ax, figsize=figsize, settlements=False, title=title)


# ---------------------------------------------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------------------------------------------

#: Row blocks of the summary tables (columns of :func:`summary_table`), shown by :func:`show_blocks`.
BLOCKS: dict[str, list[str]] = {
    "Districts": ["n_districts", "n_done", "n_failed", "n_complete", "n_without_violation", "area_km2"],
    "Supply points": ["n_stations", "n_customers", "customer_mw", "n_dedicated_customers", "n_busbar_customers",
                      "busbar_customer_mw", "customer_share"],
    "Rings and feeders": ["n_rings", "n_dedicated_feeders", "n_radial_feeders",
                          "n_feeder_bays", "feeders_per_substation", "n_nops", "n_stub_nodes", "n_ties", "tie_km",
                          "ring_members_mean", "ring_members_median", "ring_members_max", "stations_per_ring",
                          "half_ring_km_mean", "half_ring_km_median", "half_ring_km_max", "ring_km_mean",
                          "ring_km_max"],
    "Lengths": ["route_km", "circuit_km", "cable_km", "overhead_km", "cable_share", "trench_km", "km_per_station",
                "km_per_supply_point", "km_per_km2_cells", "km_per_km2", "circuit_km_per_substation",
                "supply_points_per_substation"],
    "Loading and voltage": ["peak_load_mw", "max_loading_percent", "max_loading_n1_percent", "max_drop_percent",
                            "max_drop_n1_percent", "min_vm_load_pu", "min_vm_n1_pu"],
    "Repairs, band conflicts and taps": ["n_repairs", "n_band_conflicts", "n_tap_limit", "n_tap_nonzero"],
    "Demand anchor": ["kw_per_inhabitant", "population"],
    "Runtime": ["runtime_s"],
}


def _fmt(value: Any, name: str = "") -> str:
    """A number as text, formatted by the metric name (``*_pu`` four decimals, shares three, counts integer)."""
    if value is None or value is pd.NA or value is pd.NaT:
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "no"
    if isinstance(value, (int, np.integer)):
        return f"{int(value):,}"
    if isinstance(value, (float, np.floating)):
        x = float(value)
        if not np.isfinite(x):
            return ""
        if x == 0:
            return "0"
        if name.endswith("_pu"):
            return f"{x:.4f}"
        if "share" in name or name.startswith("ari"):
            return f"{x:.3f}"
        if x.is_integer() and (name.startswith("n_") or name == "population"):
            return f"{int(x):,}"
        a = abs(x)
        if a >= 100:
            return f"{x:,.0f}"
        if a >= 10:
            return f"{x:.1f}"
        if a >= 1:
            return f"{x:.2f}"
        return f"{x:.3f}"
    return str(value)


def _name(key: Any) -> str:
    return key if isinstance(key, str) else str(key[-1] if isinstance(key, tuple) else key)


def pretty(df: pd.DataFrame, rows: bool = False) -> pd.DataFrame:
    """A text copy of a table for display; numbers are formatted by the metric names of the columns (or of the
    index with ``rows=True``)."""
    out = df.astype(object).copy()
    if rows:
        for i, key in enumerate(out.index):
            out.iloc[i] = [_fmt(x, _name(key)) for x in out.iloc[i]]
    else:
        for j, key in enumerate(out.columns):
            out.iloc[:, j] = [_fmt(x, _name(key)) for x in out.iloc[:, j]]
    return out


def show_blocks(table: pd.DataFrame, blocks: Mapping[str, Sequence[str]] = BLOCKS) -> None:
    """Display a summary table transposed (metrics as rows), one block of :data:`BLOCKS` at a time."""
    from IPython.display import Markdown, display

    for name, columns in blocks.items():
        present = [c for c in columns if c in table.columns]
        if present:
            display(Markdown(f"**{name}**"))
            display(pretty(table[present].T, rows=True))


# ---------------------------------------------------------------------------------------------------------------
# Matching districts
# ---------------------------------------------------------------------------------------------------------------


def _substation_points(v: Variant) -> pd.Series:
    """Substation point of every district (index ``mv_grid_id``)."""
    points = v.substations.set_index("substation_id").geometry
    return pd.Series(list(points.reindex(v.grids["substation_id"])), index=v.grids["mv_grid_id"].astype(int))


def match_district(v_from: Variant, grid_id: int, v_to: Variant) -> tuple[int, float]:
    """The district of ``v_to`` whose substation lies nearest to the substation of ``grid_id`` of ``v_from``.

    Returns:
        ``(mv_grid_id, distance_m)``.
    """
    point = _substation_points(v_from)[int(grid_id)]
    points = _substation_points(v_to)
    distance = pd.Series([point.distance(p) if p is not None else np.inf for p in points], index=points.index)
    best = int(distance.idxmin())
    return best, float(distance[best])


def district_pairs(v_ref: Variant, v_other: Variant, max_m: float = 200.0) -> pd.DataFrame:
    """Districts of two variants with substations at most ``max_m`` apart (``mv_grid_id_ref``, ``mv_grid_id``,
    ``distance_m``), e.g. the same OSM substation in two variants."""
    rows = []
    for gid in v_ref.grids["mv_grid_id"].astype(int):
        other, distance = match_district(v_ref, gid, v_other)
        if distance <= max_m:
            rows.append({"mv_grid_id_ref": gid, "mv_grid_id": other, "distance_m": distance})
    return pd.DataFrame(rows, columns=["mv_grid_id_ref", "mv_grid_id", "distance_m"])


def default_district(v: Variant) -> int:
    """The done district with the most stations (the default of the detail maps)."""
    done = v.grids[v.grids["status"] == "done"]
    frame = done if len(done) else v.grids
    return int(frame.sort_values(["n_stations", "mv_grid_id"], ascending=[False, True])["mv_grid_id"].iloc[0])


# ---------------------------------------------------------------------------------------------------------------
# Benchmarks: eGon-data districts, Ding0 2023 grids, literature ranges
# ---------------------------------------------------------------------------------------------------------------


def selected_region(v: Variant) -> Any:
    """The union of the region parts that hold the variant's selected districts (shapely geometry)."""
    import shapely

    parts = v.parts[v.parts["region"].isin(set(v.grids["region"].dropna().astype(int)))]
    return shapely.union_all(parts.geometry.to_numpy())


def egon_districts(dbc: Any, region_wkb: bytes) -> pd.DataFrame:
    """The eGon-data MV grid districts that intersect a region: ``bus_id`` and ``geom_wkb`` (whole polygons as
    MultiPolygon WKB in ``TARGET_EPSG``), sorted by ``bus_id``.

    Reads ``pylovo.egon_mv_grid_district`` if it has rows, else the copy in ``INFDB_OPENDATA_SCHEMA``.

    Raises:
        LookupError: If neither table exists with rows.
    """
    from psycopg2 import sql

    from pylovo.config_loader import INFDB_OPENDATA_SCHEMA, TARGET_EPSG

    for schema in ("pylovo", INFDB_OPENDATA_SCHEMA):
        if not schema:
            continue
        dbc.cur.execute("SELECT c IS NOT NULL AND has_table_privilege(c, 'SELECT') "
                        "FROM (SELECT to_regclass(%s) AS c) AS r", (f"{schema}.egon_mv_grid_district",))
        if not dbc.cur.fetchone()[0]:
            continue
        table = sql.Identifier(schema, "egon_mv_grid_district")
        dbc.cur.execute(sql.SQL("SELECT EXISTS (SELECT 1 FROM {})").format(table))
        if not dbc.cur.fetchone()[0]:
            continue
        dbc.cur.execute(sql.SQL(
            "SELECT bus_id::bigint, ST_AsBinary(ST_Multi(ST_Transform(geom, {epsg}))) FROM {table} "
            "WHERE ST_Intersects(ST_Transform(geom, {epsg}), ST_GeomFromWKB(%s, {epsg})) ORDER BY bus_id"
        ).format(table=table, epsg=sql.Literal(int(TARGET_EPSG))), (bytes(region_wkb),))
        rows = dbc.cur.fetchall()
        return pd.DataFrame({"bus_id": [int(r[0]) for r in rows], "geom_wkb": [bytes(r[1]) for r in rows]})
    raise LookupError("no eGon-data MV grid districts: pylovo.egon_mv_grid_district and the copy in "
                      "INFDB_OPENDATA_SCHEMA are missing or empty")


def egon_comparison(dbc: Any, v: Variant) -> Any:
    """The eGon-data MV grid districts that intersect the selected region parts, compared with the districts.

    Returns:
        GeoDataFrame (eGon polygons, ``TARGET_EPSG``) with ``bus_id``, ``area_km2``, ``share_in_region``,
        ``mv_grid_ids`` (districts whose substation matches the eGon substation: ``egon_bus_id``, the eGon-data
        substation within ``egon_match_m`` of the OSM site), ``district_km2`` (their cells),
        ``iou`` (cells against the eGon polygon inside the region) and ``substation_distance_m`` (eGon substation
        to the nearest substation of the variant); ``attrs["substations"]``: the eGon-data substations within 5 km
        of the region (``bus_id``, ``x``, ``y``).
    """
    import geopandas as gpd
    import shapely

    region = selected_region(v)
    rows = egon_districts(dbc, shapely.to_wkb(region))
    subs = dbc.egon_substations(shapely.to_wkb(region), 5000.0)
    _end(dbc)
    egon = gpd.GeoDataFrame({"bus_id": rows["bus_id"].astype(int)},
                            geometry=[shapely.from_wkb(bytes(w)) for w in rows["geom_wkb"]], crs=v.parts.crs)
    egon["area_km2"] = egon.area / 1e6
    inside = egon.geometry.intersection(region)
    egon["share_in_region"] = inside.area / egon.area
    ours = v.substations.assign(egon_id=pd.to_numeric(v.substations["egon_bus_id"], errors="coerce").astype("Int64"))
    grid_of_sub = v.grids.groupby("substation_id")["mv_grid_id"].apply(lambda s: [int(x) for x in s])
    cells = v.grids.set_index("mv_grid_id").geometry
    ids, km2, iou = [], [], []
    for bus, part in zip(egon["bus_id"], inside):
        matched = [g for s in ours.loc[ours["egon_id"] == bus, "substation_id"] for g in grid_of_sub.get(s, [])]
        ids.append(matched)
        geoms = [cells[g] for g in matched if cells.get(g) is not None]
        cell = shapely.union_all(geoms) if geoms else None
        km2.append(cell.area / 1e6 if cell is not None else np.nan)
        union = cell.union(part).area if cell is not None else 0.0
        iou.append(cell.intersection(part).area / union if cell is not None and union > 0 else np.nan)
    egon["mv_grid_ids"] = ids
    egon["district_km2"] = km2
    egon["iou"] = iou
    points = gpd.GeoSeries(gpd.points_from_xy(subs["x"], subs["y"]), index=subs["bus_id"].astype(int),
                           crs=v.parts.crs)
    ours_points = v.substations.geometry
    egon["substation_distance_m"] = [float(ours_points.distance(points[b]).min()) if b in points.index and len(
        ours_points) else np.nan for b in egon["bus_id"]]
    egon = egon.sort_values("share_in_region", ascending=False).reset_index(drop=True)
    egon.attrs["substations"] = subs
    return egon


def read_ding0_district(folder: Any, epsg: int | None = None) -> dict:
    """District polygon, area and population of a Ding0 2023 grid (``topology/network.csv``)."""
    from pathlib import Path

    import geopandas as gpd
    import shapely

    from pylovo.config_loader import TARGET_EPSG

    topo = Path(folder)
    topo = topo / "topology" if (topo / "topology").is_dir() else topo
    network = pd.read_csv(topo / "network.csv")
    geom = gpd.GeoSeries([shapely.from_wkt(network["mv_grid_district_geom"].iloc[0])], crs="EPSG:4326")
    geom = geom.to_crs(epsg or TARGET_EPSG)
    return {"geometry": geom.iloc[0], "area_km2": float(geom.area.iloc[0]) / 1e6,
            "population": int(network["mv_grid_district_population"].iloc[0])}


#: Columns of :func:`grid_metrics` (keys of :func:`pylovo.mv_grid.metrics.topology_metrics`).
GRID_METRICS: tuple[str, ...] = (
    "n_busbars", "n_stations", "n_customers", "n_rings", "n_two_leg", "n_radial", "n_nops", "n_ties", "tie_km",
    "circuit_km", "cable_km", "overhead_km", "cable_share", "km_per_station", "km_per_supply_point",
    "ring_members_mean", "ring_members_max", "ring_km_mean", "feeder_km_mean", "feeder_km_max",
    "feeder_supply_points_mean", "feeders_per_substation", "station_spacing_m", "n1_secure_share", "stub_share",
)


def grid_metrics(nodes: pd.DataFrame, lines: pd.DataFrame) -> dict:
    """:func:`pylovo.mv_grid.metrics.topology_metrics` of a grid, reduced to :data:`GRID_METRICS` plus ``vn_kv``
    (busbar voltage when the node table has it)."""
    from pylovo.mv_grid import metrics

    tm = metrics.topology_metrics(nodes, lines)
    out = {k: tm.get(k) for k in GRID_METRICS}
    if "vn_kv" in nodes.columns:
        out["vn_kv"] = float(nodes.loc[nodes["kind"] == "busbar", "vn_kv"].max())
    return out


def generic_rings_and_feeders(v: Variant, grid_ids: Iterable[int] | None = None
                              ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Rings and feeders of :func:`pylovo.mv_grid.metrics.ring_table` and ``feeder_table`` for every district,
    with ``variant``, ``mv_grid_id``, ``complete`` and the district class ``district_class``."""
    from pylovo.mv_grid import metrics

    nodes, lines = generic_tables(v, grid_ids)
    grids = v.grids.set_index("mv_grid_id")
    ring_frames, feeder_frames = [], []
    for gid, gn in nodes.groupby("mv_grid_id"):
        gl = lines[lines["mv_grid_id"] == gid]
        if gl.empty:
            continue
        extra = {"variant": v.label, "mv_grid_id": int(gid), "complete": bool(grids.at[int(gid), "complete"]),
                 "district_class": grids.at[int(gid), "area_class"]}
        ring_frames.append(metrics.ring_table(gn, gl).assign(**extra))
        feeder_frames.append(metrics.feeder_table(gn, gl).assign(**extra))
    return (pd.concat(ring_frames, ignore_index=True) if ring_frames else pd.DataFrame(),
            pd.concat(feeder_frames, ignore_index=True) if feeder_frames else pd.DataFrame())


def literature_table(v: Variant, districts: pd.DataFrame, rings: pd.DataFrame, feeders: pd.DataFrame,
                     power_factor: float, complete_only: bool = True) -> pd.DataFrame:
    """The metrics of ``LITERATURE_RANGES`` per area class of one variant (input of ``compare_to_literature``).

    Districts count by their majority class, rings and feeders by their own class, stations by their node class.
    Ring and feeder values are means; ``km_per_supply_point``, ``cable_share``, ``km_per_km2`` and
    ``customer_share`` are ratios of sums; per-substation values are district means; ``substation_peak_mva`` is
    the district peak over ``power_factor``. With ``complete_only`` only complete districts count (substation in
    the region, cell not cut by its border); without any complete district all districts count (``basis``).

    Returns:
        One row per area class (index ``variant``, ``class``) with ``area_class``, ``basis``, ``n_districts`` and
        the metric columns.
    """
    from pylovo.mv_grid import metrics

    d = districts[districts["variant"] == v.label]
    basis = "complete districts"
    if complete_only and d["complete"].fillna(False).astype(bool).any():
        d = d[d["complete"].fillna(False).astype(bool)]
    else:
        basis = "all districts"
    ids = set(d["mv_grid_id"].astype(int))
    r = rings[rings["mv_grid_id"].isin(ids) & rings["kind"].isin(["ring", "mesh"])] if len(rings) else rings
    f = feeders[feeders["mv_grid_id"].isin(ids) & feeders["kind"].isin(["ring", "mesh"])
                & (feeders["feeder_supply_points"] > 0)] if len(feeders) else feeders
    nodes, _ = generic_tables(v, ids)
    rows = []
    for cls in ("rural", "urban"):
        dc = d[d["area_class"] == cls]
        rc = r[r["area_class"] == cls] if len(r) else r
        fc = f[f["area_class"] == cls] if len(f) else f
        if dc.empty and (len(rc) == 0):
            continue
        sp = dc["n_supply_points"].sum()
        rows.append({
            "variant": v.label, "class": cls, "area_class": cls, "basis": basis, "n_districts": len(dc),
            "n_rings": len(rc),
            "ring_members": rc["ring_members"].mean() if len(rc) else np.nan,
            "ring_km": rc["ring_km"].mean() if len(rc) else np.nan,
            "feeder_supply_points": fc["feeder_supply_points"].mean() if len(fc) else np.nan,
            "feeder_km": fc["feeder_km"].mean() if len(fc) else np.nan,
            "feeders_per_substation": dc["n_feeder_bays"].mean(),
            "supply_points_per_substation": dc["n_supply_points"].mean(),
            "km_per_supply_point": _ratio(dc["circuit_km"].sum(), sp),
            "cable_share": _ratio(dc["cable_km"].sum(), dc["circuit_km"].sum()),
            "km_per_km2": _ratio(dc["circuit_km"].sum(), dc["area_km2"].sum()),
            "circuit_km_per_substation": dc["circuit_km"].mean(),
            "station_spacing_m": metrics.station_spacing_m(nodes[nodes["area_class"] == cls]),
            "customer_share": _ratio(dc["n_customers"].sum(), sp),
            "substation_peak_mva": (dc["peak_load_mw"] / power_factor).mean(),
            "hv_mv_unit_mva": dc["hv_mv_unit_mva"].mean(),
            "peak_to_station_rating": _ratio(dc["peak_load_mw"].sum() / power_factor, dc["sum_station_mva"].sum()),
            "max_line_loading_percent": dc["max_loading_percent"].max(),
        })
    return pd.DataFrame(rows).set_index(["variant", "class"])


#: Light tints of the traffic-light statuses for table cells.
STATUS_TINTS = {"green": "#c9ebc9", "amber": "#ffe2b3", "red": "#f7c4c4", "n/a": "#eeeeee"}
#: Status marks next to the values, so that the status does not rely on the colour alone.
STATUS_MARKS = {"green": "✓", "amber": "~", "red": "✗", "n/a": ""}


def traffic_light_table(comparison: pd.DataFrame) -> Any:
    """A styled table of ``compare_to_literature`` rows: one row per (class, metric, reference), one column per
    variant; cells show the value with a status mark (✓ green, ~ amber, ✗ red) and are tinted by status."""
    c = comparison[comparison["status"] != "n/a"].copy()

    def range_text(low: Any, high: Any, typical: Any) -> str:
        parts = []
        if pd.notna(low) or pd.notna(high):
            parts.append(f"{f'{low:,g}' if pd.notna(low) else '…'} to {f'{high:,g}' if pd.notna(high) else '…'}")
        if pd.notna(typical):
            parts.append(f"typical {typical:,g}")
        return ", ".join(parts)

    c["reference"] = [f"{label}: {range_text(lo, hi, ty)}" for label, lo, hi, ty in
                      zip(c["label"], c["low"], c["high"], c["typical"])]
    c["text"] = [f"{_fmt(x, m)} {STATUS_MARKS.get(s, '')}" for x, m, s in
                 zip(c["value"], c["metric"], c["status"])]
    key = ["class", "metric", "reference"]
    values = c.pivot_table(index=key, columns="variant", values="text", aggfunc="first", sort=False)
    status = c.pivot_table(index=key, columns="variant", values="status", aggfunc="first", sort=False)
    return values.style.apply(lambda col: [f"background-color: {STATUS_TINTS.get(s, '')}" if isinstance(s, str)
                                           else "" for s in status[col.name]], axis=0)


# ---------------------------------------------------------------------------------------------------------------
# Repairs, flags and coupling
# ---------------------------------------------------------------------------------------------------------------


def repairs_table(v: Variant) -> pd.DataFrame:
    """One row per repair action of the electrical repair loop (``mv_grid.repairs``)."""
    rows = []
    for gid, repairs in zip(v.grids["mv_grid_id"], v.grids["repairs"]):
        for item in repairs if isinstance(repairs, list) else []:
            rows.append({"variant": v.label, "mv_grid_id": int(gid), **{k: item.get(k) for k in (
                "round", "case", "violation", "action", "reason", "element", "value", "limit")}})
    return pd.DataFrame(rows, columns=["variant", "mv_grid_id", "round", "case", "violation", "action", "reason",
                                       "element", "value", "limit"])


def flag_counts(v: Variant) -> pd.Series:
    """Number of nodes per flag (``mv_node.flags``)."""
    flags = [f for fl in v.nodes["flags"] for f in (fl if isinstance(fl, (list, tuple, np.ndarray)) else [])]
    return pd.Series(Counter(flags), dtype=int).sort_index().rename(v.label)


#: Lists of station node ids in ``mv_grid.metrics['coupling']``.
COUPLING_LISTS: tuple[str, ...] = ("not_merged", "lv_stored_not_converged", "design_not_converged", "no_budget",
                                   "no_mv_bus", "not_coupled", "unknown_lv_grid", "lv_error")


def coupling_table(v: Variant) -> pd.DataFrame:
    """The coupling diagnostics of every district (``mv_grid.metrics['coupling']``, flattened without its lists);
    the lists of :data:`COUPLING_LISTS` become counts (``n_<list>``)."""
    rows = []
    for gid, status, m in zip(v.grids["mv_grid_id"], v.grids["coupled_status"], v.grids["metrics"]):
        coupling = (m or {}).get("coupling") if isinstance(m, Mapping) else None
        if not isinstance(coupling, Mapping):
            continue
        flat = flat_parameters({k: x for k, x in coupling.items() if not isinstance(x, list)})
        flat.update({f"n_{k}": len(coupling.get(k) or []) for k in COUPLING_LISTS})
        rows.append({"variant": v.label, "mv_grid_id": int(gid), "coupled_status": status, **flat})
    return pd.DataFrame(rows)


def coupling_left_out(v: Variant) -> pd.DataFrame:
    """The stations in the coupling lists (``not_merged``, ``lv_stored_not_converged``, ...) with their LV grid."""
    nodes = v.nodes.set_index("mv_node_id")
    rows = []
    for gid, m in zip(v.grids["mv_grid_id"], v.grids["metrics"]):
        coupling = (m or {}).get("coupling") if isinstance(m, Mapping) else None
        if not isinstance(coupling, Mapping):
            continue
        for name in COUPLING_LISTS:
            for item in coupling.get(name) or []:
                node = int(item) if isinstance(item, (int, float, np.integer)) else None
                row = nodes.loc[node] if node is not None and node in nodes.index else None
                rows.append({"variant": v.label, "mv_grid_id": int(gid), "list": name, "mv_node_id": node,
                             "grid_result_id": row["grid_result_id"] if row is not None else None,
                             "sn_kva": row["sn_kva"] if row is not None else None,
                             "flags": ", ".join(row["flags"]) if row is not None and isinstance(
                                 row["flags"], (list, tuple, np.ndarray)) else "",
                             "item": None if node is not None else str(item)})
    return pd.DataFrame(rows, columns=["variant", "mv_grid_id", "list", "mv_node_id", "grid_result_id", "sn_kva",
                                       "flags", "item"])


def line_record(v: Variant, kind: str, vn_kv: float) -> dict:
    """The conductor record (``equipment.cable`` or ``equipment.overhead``) of the variant at a district voltage;
    with ``MV_NOMINAL_VOLTAGE_KV`` auto the 10 kV districts use ``equipment.<kind>_10kv``. Empty if the stored
    record has another voltage."""
    for path in ([f"equipment.{kind}_10kv"] if float(vn_kv) == 10.0 else []) + [f"equipment.{kind}"]:
        record = v.snapshot(path)
        if isinstance(record, Mapping) and float(record.get("vn_kv", vn_kv)) == float(vn_kv):
            return dict(record)
    return {}


def earth_fault_table(v: Variant, districts: pd.DataFrame) -> pd.DataFrame:
    """Capacitive earth-fault current per district (:func:`pylovo.analysis.mv_validation.earth_fault_current_a`)
    with the earth capacitance of the variant's conductors at the district voltage (module defaults where unknown)."""
    from pylovo.analysis import mv_validation

    rows = []
    for gid, cable, overhead, vn in zip(districts["mv_grid_id"], districts["cable_km"], districts["overhead_km"],
                                        districts["vn_kv"]):
        c_cable = line_record(v, "cable", vn).get("c_nf_per_km")
        c_overhead = line_record(v, "overhead", vn).get("c_nf_per_km")
        kwargs = {"overhead_c_nf_per_km": float(c_overhead)} if c_overhead is not None else {}
        current = mv_validation.earth_fault_current_a(cable, overhead, vn, cable_c_nf_per_km=c_cable, **kwargs)
        rows.append({"variant": v.label, "mv_grid_id": int(gid), "vn_kv": vn, "cable_km": cable,
                     "overhead_km": overhead, "cable_c_nf_per_km": c_cable, "earth_fault_a": current})
    return pd.DataFrame(rows)


def case_table(v: Variant) -> pd.DataFrame:
    """One row per district and planning case (``pylovo.mv_grid.model.CASES``: load, n1a, n1b) of the MV-only
    district net after the repairs (``mv_grid.metrics``): ``converged``, ``min_vm_pu``, ``max_vm_pu``,
    ``max_loading_percent``, ``slack_p_mw`` and ``unsupplied_nodes``."""
    from pylovo.mv_grid.model import CASES

    rows = []
    for gid, m in zip(v.grids["mv_grid_id"], v.grids["metrics"]):
        m = m if isinstance(m, Mapping) else {}
        for case in CASES:
            rows.append({"variant": v.label, "mv_grid_id": int(gid), "case": case,
                         **{k: m.get(f"{case}_{k}") for k in ("converged", "min_vm_pu", "max_vm_pu",
                                                              "max_loading_percent", "slack_p_mw",
                                                              "unsupplied_nodes")}})
    return pd.DataFrame(rows)
