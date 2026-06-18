"""Helpers for comparing synthetic PyLoVo grids with real DSO grids."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

import geopandas as gpd
import numpy as np
import pandas as pd
import pandapower as pp
from scipy.stats import wasserstein_distance
from shapely import wkt
from shapely.geometry import Point

from pylovo.analysis.grid_analysis import compute_comparison_parameters
from pylovo.analysis.validation_helpers import MINI_GRID_BUS_THRESHOLD
from pylovo.config_loader import GRID_DATA_PATH, TARGET_EPSG, VERSION_ID
from pylovo.database.config_table_structure import CREATE_QUERIES
from pylovo.database.database_client import DatabaseClient

if TYPE_CHECKING:
    from pylovo.analysis.parameter_calculation import ParameterCalculator



COMPARISON_METRIC_COLUMNS = [
    "feeder_lines",
    "graph_length",
    "avg_trafo_distance",
    "max_trafo_distance",
    "transformer_mva",
    "graph_resistance",
]

SYNTHETIC_METRIC_ID_COLUMNS = [
    "grid_result_id",
    "kcid",
    "bcid",
    "power_flow_status",
    "metric_status",
    "metric_error",
]

REAL_METRIC_ID_COLUMNS = [
    "grid_name",
    "file_name",
    "metric_status",
    "metric_error",
]

def export_synthetic_comparison_parameters_for_plz(
    calculator: "ParameterCalculator",
    plz: int,
    limit: int = None,
    output_dir: Path | None = None,
    output_suffix: str = "",
    with_service_lines: bool = False,
) -> pd.DataFrame:
    """Compute, persist, and export the active comparison parameter set for synthetic grids.

    ``synthetic_grid_metrics.csv`` contains all synthetic grids with ``power_flow_status``.
    """
    calculator.plz = plz

    _reset_grid_parameters_table(calculator)
    calculator.dbc.conn.commit()

    calculator.dbc.cur.execute(
        f"""
        SELECT kcid, bcid, COALESCE(power_flow_status, 'converged')
        FROM pylovo.grid_result
                WHERE plz = %s AND version_id = %s
        ORDER BY kcid, bcid
        """,
        (plz, str(calculator.version_id)),
    )
    grids = calculator.dbc.cur.fetchall()
    if limit is not None:
        grids = grids[:limit]
    print(f"Calculating comparison parameters for {len(grids)} grids in PLZ {plz}...")

    metrics_list = []

    for kcid, bcid, power_flow_status in grids:
        grid_result_id = None
        net = None
        try:
            net = calculator.dbc.read_net_db(plz, kcid, bcid, version_id=calculator.version_id)
            if len(net.bus) < MINI_GRID_BUS_THRESHOLD:
                continue
            calculator.dbc.cur.execute(
                f"SELECT grid_result_id FROM pylovo.grid_result WHERE plz=%s AND kcid=%s AND bcid=%s AND version_id=%s",
                (plz, kcid, bcid, calculator.version_id),
            )
            grid_result_id = calculator.dbc.cur.fetchone()[0]

            params = compute_comparison_parameters(
                calculator,
                net,
                with_service_lines=with_service_lines,
            )
            params["power_flow_status"] = power_flow_status
            params["grid_result_id"] = grid_result_id
            params["kcid"] = kcid
            params["bcid"] = bcid
            metrics_list.append(params)

            _upsert_comparison_parameters(calculator, grid_result_id, params)
        except Exception as exc:
            calculator.dbc.logger.error(f"Error processing grid {kcid}_{bcid}: {exc}")
            calculator.dbc.conn.rollback()
            params = _build_metric_error_row(net, exc)
            params["power_flow_status"] = power_flow_status
            params["grid_result_id"] = grid_result_id
            params["kcid"] = kcid
            params["bcid"] = bcid
            metrics_list.append(params)

        calculator.dbc.conn.commit()

    calculator.dbc.conn.commit()

    if not metrics_list:
        return pd.DataFrame()

    df = pd.DataFrame(metrics_list)
    out_dir = Path(output_dir) if output_dir is not None else Path("validation/grid_comparison/metrics")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / _metric_filename("synthetic_grid_metrics.csv", output_suffix)
    _select_export_columns(df, SYNTHETIC_METRIC_ID_COLUMNS).to_csv(csv_path, index=False)
    print(f"Saved synthetic grid metrics to {csv_path}")
    return df


def iter_real_grid_files(data_path: str) -> list[Path]:
    """Return LV subnet files for real-vs-synthetic comparison.

    The current DSO preprocessing writes two explicit variants: ``logical/`` for
    traceability and ``radialized/`` for comparison against PyLoVo's radial
    synthetic grids.  When the new layout is present, only regular radialized
    grids with an LV comparison load marker are used.  Older JSON layouts remain
    readable for transition periods.
    """
    path = Path(data_path)
    radialized = sorted((path / "radialized").glob("LV_*__radialized__regular__lvload.xlsx"))
    if radialized:
        return radialized

    legacy_radialized = sorted((path / "radialized").glob("LV_*__radialized__regular__ns.json"))
    if legacy_radialized:
        return legacy_radialized

    candidates = (
        sorted(path.glob("LV_*.xlsx"))
        + sorted(path.glob("LV_*.json"))
        + sorted((path / "regular").glob("LV_*.json"))
    )
    return [file_path for file_path in candidates if file_path.stem.startswith("LV_")]


def _load_real_grid_file(file_path: Path) -> pp.pandapowerNet:
    if file_path.suffix.lower() == ".xlsx":
        return pp.from_excel(str(file_path))
    return pp.from_json(str(file_path))



def extract_bus_geometries(net: pp.pandapowerNet) -> gpd.GeoDataFrame:
    """Extract bus point geometries from pandapower bus.geo JSON strings."""
    geoms = []
    indices = []

    if "geo" not in net.bus.columns:
        return gpd.GeoDataFrame(columns=["bus_index", "geometry"], geometry="geometry", crs="EPSG:4326")

    for idx, row in net.bus.iterrows():
        geo_value = row.get("geo")
        if not isinstance(geo_value, str):
            continue
        try:
            geo_dict = json.loads(geo_value)
        except json.JSONDecodeError:
            continue

        coordinates = geo_dict.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) != 2:
            continue

        geoms.append(Point(coordinates[0], coordinates[1]))
        indices.append(idx)

    if not geoms:
        return gpd.GeoDataFrame(columns=["bus_index", "geometry"], geometry="geometry", crs="EPSG:4326")

    xs = [point.x for point in geoms]
    ys = [point.y for point in geoms]
    is_geographic = (
        min(xs) >= -180 and max(xs) <= 180 and min(ys) >= -90 and max(ys) <= 90
    )
    crs = "EPSG:4326" if is_geographic else f"EPSG:{TARGET_EPSG}"

    return gpd.GeoDataFrame({"bus_index": indices}, geometry=geoms, crs=crs)


def process_synthetic_grids(
    dbc: DatabaseClient,
    plz: int,
    output_dir: Path,
    output_suffix: str = "",
    with_service_lines: bool = False,
) -> pd.DataFrame:
    """Calculate and export comparison parameters for synthetic grids in one postcode area."""
    print(f"Processing synthetic grids for PLZ {plz}...")
    from pylovo.analysis.parameter_calculation import ParameterCalculator

    calc = ParameterCalculator()
    calc.dbc = dbc
    return export_synthetic_comparison_parameters_for_plz(
        calc,
        plz,
        output_dir=output_dir,
        output_suffix=output_suffix,
        with_service_lines=with_service_lines,
    )


def process_real_grids(
    dbc: DatabaseClient,
    data_path: str,
    plz: int,
    output_dir: Path,
    output_suffix: str = "",
    with_service_lines: bool = False,
) -> pd.DataFrame:
    """Calculate and export comparison parameters for real LV subnets."""
    print(f"Processing real grids from {data_path}...")

    buildings_gdf = _load_buildings_for_plz(dbc, plz)

    from pylovo.analysis.parameter_calculation import ParameterCalculator

    metrics_list = []
    calc = ParameterCalculator()
    calc.dbc = dbc

    for file_path in iter_real_grid_files(data_path):
        net = None
        try:
            net = _load_real_grid_file(file_path)
            consumer_buses = _infer_real_grid_consumer_buses(net, buildings_gdf)

            params = compute_comparison_parameters(
                calc,
                net,
                consumer_buses=consumer_buses,
                with_service_lines=with_service_lines,
            )
            params["grid_name"] = file_path.stem
            params["file_name"] = file_path.name
            metrics_list.append(params)
        except Exception as exc:
            print(f"Error processing real grid {file_path.name}: {exc}")
            params = _build_metric_error_row(net, exc)
            params["grid_name"] = file_path.stem
            params["file_name"] = file_path.name
            metrics_list.append(params)

    if not metrics_list:
        return pd.DataFrame()

    df = pd.DataFrame(metrics_list)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / _metric_filename("real_grid_metrics.csv", output_suffix)
    _select_export_columns(df, REAL_METRIC_ID_COLUMNS).to_csv(csv_path, index=False)
    print(f"Saved real grid metrics to {csv_path}")
    return df


def run_grid_comparison(
    plz: int,
    output_dir: Path,
    data_path: str | None = None,
    output_suffix: str = "",
    with_service_lines: bool = False,
) -> None:
    """Run the full comparison workflow and write both metrics CSV files."""
    with DatabaseClient() as dbc:
        synthetic_df = process_synthetic_grids(
            dbc,
            plz,
            output_dir,
            output_suffix=output_suffix,
            with_service_lines=with_service_lines,
        )

        grid_data_path = Path(data_path) if data_path is not None else Path(GRID_DATA_PATH)
        if grid_data_path.exists():
            real_df = process_real_grids(
                dbc,
                str(grid_data_path),
                plz,
                output_dir,
                output_suffix=output_suffix,
                with_service_lines=with_service_lines,
            )
            _write_input_audit(synthetic_df, real_df, output_dir, output_suffix=output_suffix)
        else:
            print(f"GRID_DATA_PATH not found or invalid: {grid_data_path}")



def _select_export_columns(df: pd.DataFrame, id_columns: list[str]) -> pd.DataFrame:
    """Return the clean benchmark-facing metrics table."""
    selected = [col for col in id_columns + COMPARISON_METRIC_COLUMNS if col in df.columns]
    return df[selected].copy()


def _classify_normalized_wasserstein(normalized_distance: float) -> str:
    """Map normalized Wasserstein distance to a qualitative fit category."""
    if normalized_distance <= 0.10:
        return "excellent"
    if normalized_distance <= 0.25:
        return "good"
    if normalized_distance <= 0.50:
        return "acceptable"
    return "poor"


def compute_wasserstein_summary(
    df: pd.DataFrame,
    metrics: list[str],
    source_col: str = "source",
    synthetic_label: str = "Synthetic",
    real_label: str = "Real",
) -> pd.DataFrame:
    """Compute Earth Mover's Distance (Wasserstein-1) per metric for synthetic vs real data.

    Returns one row per metric with:
    - `wasserstein_distance`: absolute EMD in metric units
    - `normalized_wasserstein`: EMD divided by pooled IQR (scale-free)
    - `quality`: qualitative interpretation based on normalized distance
    """
    if df.empty:
        return pd.DataFrame(
            columns=[
                "metric",
                "synthetic_n",
                "real_n",
                "wasserstein_distance",
                "pooled_iqr",
                "normalized_wasserstein",
                "quality",
            ]
        )

    if source_col not in df.columns:
        raise KeyError(f"Required source column '{source_col}' is missing.")

    rows = []
    for metric in metrics:
        if metric not in df.columns:
            continue

        synth_vals = pd.to_numeric(
            df.loc[df[source_col] == synthetic_label, metric], errors="coerce"
        ).dropna()
        real_vals = pd.to_numeric(
            df.loc[df[source_col] == real_label, metric], errors="coerce"
        ).dropna()

        if synth_vals.empty or real_vals.empty:
            rows.append(
                {
                    "metric": metric,
                    "synthetic_n": int(len(synth_vals)),
                    "real_n": int(len(real_vals)),
                    "wasserstein_distance": np.nan,
                    "pooled_iqr": np.nan,
                    "normalized_wasserstein": np.nan,
                    "quality": "insufficient_data",
                }
            )
            continue

        pooled = pd.concat([synth_vals, real_vals], ignore_index=True)
        q25, q75 = np.nanpercentile(pooled, [25, 75])
        pooled_iqr = float(q75 - q25)
        if pooled_iqr <= 1e-6:
            pooled_iqr = float(np.nanstd(pooled))
        if pooled_iqr <= 1e-6:
            pooled_iqr = 1.0

        emd = float(wasserstein_distance(synth_vals.to_numpy(), real_vals.to_numpy()))
        normalized = emd / pooled_iqr

        rows.append(
            {
                "metric": metric,
                "synthetic_n": int(len(synth_vals)),
                "real_n": int(len(real_vals)),
                "wasserstein_distance": emd,
                "pooled_iqr": pooled_iqr,
                "normalized_wasserstein": normalized,
                "quality": _classify_normalized_wasserstein(normalized),
            }
        )

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    return result.sort_values("normalized_wasserstein", ascending=True).reset_index(drop=True)


def _load_buildings_for_plz(dbc: DatabaseClient, plz: int) -> gpd.GeoDataFrame | None:
    """Load building geometries for the postcode area used in real-grid comparison fallback."""
    try:
        query = f"""
            SELECT br.objectid, br.peak_load_in_kw, ST_AsText(br.geom) as wkt
            FROM pylovo.buildings_result br
            JOIN pylovo.grid_result gr ON br.grid_result_id = gr.grid_result_id
            WHERE gr.plz = {plz} AND br.version_id = '{VERSION_ID}'
        """
        buildings_df = pd.read_sql(query, dbc.sqla_engine)
    except Exception as exc:
        print(f"Error fetching buildings: {exc}")
        return None

    if buildings_df.empty:
        print("No buildings found for this PLZ.")
        return None

    buildings_df["geometry"] = buildings_df["wkt"].apply(wkt.loads)
    return gpd.GeoDataFrame(buildings_df, geometry="geometry", crs=f"EPSG:{TARGET_EPSG}")


def _infer_real_grid_consumer_buses(net: pp.pandapowerNet, buildings_gdf: gpd.GeoDataFrame | None) -> list[int] | None:
    """Infer consumer buses for real grids when no explicit load buses are available."""
    if not net.load.empty:
        return net.load["bus"].unique().tolist()

    if buildings_gdf is None:
        return None

    if "geo" not in net.bus.columns or not net.bus["geo"].notna().any():
        return None

    bus_gdf = extract_bus_geometries(net)
    if bus_gdf.empty:
        return None

    if buildings_gdf.crs is None:
        buildings_gdf = buildings_gdf.set_crs(epsg=TARGET_EPSG, allow_override=True)

    if bus_gdf.crs.to_string() != buildings_gdf.crs.to_string():
        bus_gdf = bus_gdf.to_crs(buildings_gdf.crs)

    joined = gpd.sjoin_nearest(buildings_gdf, bus_gdf, distance_col="dist")
    print(f"DEBUG: Matched {len(joined)} buildings to buses.")
    return joined["bus_index"].dropna().astype(int).unique().tolist()


def _build_metric_error_row(net: pp.pandapowerNet | None, exc: Exception) -> dict:
    """Return an explicit failed-metric row instead of silently emitting zeros."""
    row = {
        "metric_status": "error",
        "metric_error": str(exc),
        "uses_synthetic_naming": np.nan,
        "root_bus": np.nan,
        "bus_count": np.nan,
        "line_count": np.nan,
        "active_line_count": np.nan,
        "consumer_bus_count": np.nan,
        "load_count": np.nan,
        "trafo_count": np.nan,
        "ext_grid_count": np.nan,
        "negative_length_count": np.nan,
        "zero_length_count": np.nan,
        "missing_length_count": np.nan,
        "feeder_lines": np.nan,
        "feeder_lines_first_hop": np.nan,
        "feeder_lines_label_aware": np.nan,
        "feeder_lines_terminal_topology": np.nan,
        "feeder_lines_terminal_backbone": np.nan,
        "feeder_lines_expand_all": np.nan,
        "feeder_lines_collapse_non_kvs": np.nan,
        "feeder_count_delta_label_aware": np.nan,
        "feeder_count_delta_terminal_topology": np.nan,
        "feeder_count_delta_expand_all": np.nan,
        "feeder_count_delta_collapse_non_kvs": np.nan,
        "buildings_per_feeder": np.nan,
        "graph_length": np.nan,
        "avg_trafo_distance": np.nan,
        "max_trafo_distance": np.nan,
        "transformer_mva": np.nan,
        "graph_resistance": np.nan,
    }
    if net is None:
        return row

    line_df = net.line
    active_line_df = line_df[line_df["in_service"]] if "in_service" in line_df.columns else line_df
    length = pd.to_numeric(line_df.get("length_km", pd.Series(dtype=float)), errors="coerce")
    bus_names = net.bus["name"].fillna("") if "name" in net.bus.columns else pd.Series(dtype=str)
    uses_synthetic_naming = bool(bus_names.astype(str).str.contains("LVbus", na=False).any())
    if uses_synthetic_naming:
        consumer_bus_count = int(bus_names.astype(str).str.contains("Consumer Nodebus", na=False).sum())
    elif "bus" in net.load.columns:
        consumer_bus_count = int(net.load["bus"].dropna().nunique())
    else:
        consumer_bus_count = np.nan
    row.update({
        "uses_synthetic_naming": uses_synthetic_naming,
        "bus_count": int(len(net.bus)),
        "line_count": int(len(line_df)),
        "active_line_count": int(len(active_line_df)),
        "consumer_bus_count": consumer_bus_count,
        "load_count": int(len(net.load)),
        "trafo_count": int(len(net.trafo)),
        "ext_grid_count": int(len(net.ext_grid)),
        "negative_length_count": int((length < 0).sum()),
        "zero_length_count": int((length == 0).sum()),
        "missing_length_count": int(length.isna().sum()),
    })
    return row


def _metric_filename(filename: str, output_suffix: str = "") -> str:
    """Return a metric filename with an optional suffix before the extension."""
    if not output_suffix:
        return filename
    clean_suffix = output_suffix.strip().strip("_")
    if not clean_suffix:
        return filename
    path = Path(filename)
    return f"{path.stem}_{clean_suffix}{path.suffix}"


def _write_input_audit(
    synthetic_df: pd.DataFrame,
    real_df: pd.DataFrame,
    output_dir: Path,
    output_suffix: str = "",
) -> pd.DataFrame:
    """Write a compact source-level audit for the comparison inputs."""
    audit_cols = [
        "metric_status",
        "metric_error",
        "uses_synthetic_naming",
        "root_bus",
        "bus_count",
        "line_count",
        "active_line_count",
        "consumer_bus_count",
        "load_count",
        "trafo_count",
        "ext_grid_count",
        "negative_length_count",
        "zero_length_count",
        "missing_length_count",
        "feeder_lines",
        "feeder_lines_first_hop",
        "feeder_lines_label_aware",
        "feeder_lines_terminal_topology",
        "feeder_lines_terminal_backbone",
        "feeder_lines_expand_all",
        "feeder_lines_collapse_non_kvs",
        "feeder_count_delta_label_aware",
        "feeder_count_delta_terminal_topology",
        "feeder_count_delta_expand_all",
        "feeder_count_delta_collapse_non_kvs",
        "buildings_per_feeder",
    ]
    frames = []
    if not synthetic_df.empty:
        synth = synthetic_df.copy()
        synth["source"] = "Synthetic"
        frames.append(synth)
    if not real_df.empty:
        real = real_df.copy()
        real["source"] = "Real"
        frames.append(real)
    if not frames:
        return pd.DataFrame()

    audit_df = pd.concat(frames, ignore_index=True, sort=False)
    id_cols = [col for col in ["source", "grid_result_id", "kcid", "bcid", "grid_name", "file_name", "power_flow_status"] if col in audit_df.columns]
    selected_cols = id_cols + [col for col in audit_cols if col in audit_df.columns]
    audit_df = audit_df[selected_cols]
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / _metric_filename("comparison_input_audit.csv", output_suffix)
    audit_df.to_csv(csv_path, index=False)
    print(f"Saved comparison input audit to {csv_path}")
    return audit_df


def _upsert_comparison_parameters(calculator: "ParameterCalculator", grid_result_id: int, params: dict) -> None:
    """Persist the active comparison metric set to ``grid_parameters``."""
    query = f"""
        INSERT INTO pylovo.grid_parameters (
            grid_result_id,
            power_flow_status,
            feeder_lines,
            buildings_per_feeder,
            graph_length,
            avg_trafo_distance,
            max_trafo_distance,
            transformer_mva,
            graph_resistance
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (grid_result_id) DO UPDATE SET
        power_flow_status = EXCLUDED.power_flow_status,
        feeder_lines = EXCLUDED.feeder_lines,
        buildings_per_feeder = EXCLUDED.buildings_per_feeder,
        graph_length = EXCLUDED.graph_length,
        avg_trafo_distance = EXCLUDED.avg_trafo_distance,
        max_trafo_distance = EXCLUDED.max_trafo_distance,
        transformer_mva = EXCLUDED.transformer_mva,
        graph_resistance = EXCLUDED.graph_resistance;
    """
    calculator.dbc.cur.execute(
        query,
        (
            grid_result_id,
            params["power_flow_status"],
            params["feeder_lines"],
            params["buildings_per_feeder"],
            params["graph_length"],
            params["avg_trafo_distance"],
            params["max_trafo_distance"],
            params["transformer_mva"],
            params["graph_resistance"],
        ),
    )


def _reset_grid_parameters_table(calculator: "ParameterCalculator") -> None:
    """Recreate ``grid_parameters`` with the current comparison schema."""
    calculator.dbc.cur.execute("DROP TABLE IF EXISTS pylovo.grid_parameters")
    calculator.dbc.cur.execute(CREATE_QUERIES["grid_parameters"])


__all__ = [
    "compute_wasserstein_summary",
    "export_synthetic_comparison_parameters_for_plz",
    "extract_bus_geometries",
    "iter_real_grid_files",
    "_load_real_grid_file",
    "process_real_grids",
    "process_synthetic_grids",
    "run_grid_comparison",
    "_write_input_audit",
]