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


def export_synthetic_comparison_parameters_for_plz(
    calculator: "ParameterCalculator",
    plz: int,
    limit: int = None,
    output_dir: Path | None = None,
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
        try:
            net = calculator.dbc.read_net_db(plz, kcid, bcid, version_id=calculator.version_id)
            if len(net.bus) < MINI_GRID_BUS_THRESHOLD:
                continue
            calculator.dbc.cur.execute(
                f"SELECT grid_result_id FROM pylovo.grid_result WHERE plz=%s AND kcid=%s AND bcid=%s AND version_id=%s",
                (plz, kcid, bcid, calculator.version_id),
            )
            grid_result_id = calculator.dbc.cur.fetchone()[0]

            params = compute_comparison_parameters(calculator, net)
            params["power_flow_status"] = power_flow_status
            params["grid_result_id"] = grid_result_id
            params["kcid"] = kcid
            params["bcid"] = bcid
            metrics_list.append(params)

            _upsert_comparison_parameters(calculator, grid_result_id, params)
        except Exception as exc:
            calculator.dbc.logger.error(f"Error processing grid {kcid}_{bcid}: {exc}")
            calculator.dbc.conn.rollback()

        calculator.dbc.conn.commit()

    calculator.dbc.conn.commit()

    if not metrics_list:
        return pd.DataFrame()

    df = pd.DataFrame(metrics_list)
    out_dir = Path(output_dir) if output_dir is not None else Path("validation/grid_comparison/metrics")
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "synthetic_grid_metrics.csv"
    df.to_csv(csv_path, index=False)
    print(f"Saved synthetic grid metrics to {csv_path}")
    return df


def iter_real_grid_files(data_path: str) -> list[Path]:
    """Return only LV JSON subnet files from the configured real-grid directory.

    Searches both the top-level directory and a ``regular/`` subdirectory
    (produced by :func:`~pylovo.analysis.validation_helpers.split_to_subgrids`).
    """
    path = Path(data_path)
    candidates = sorted(path.glob("*.json")) + sorted((path / "regular").glob("*.json"))
    return [file_path for file_path in candidates if file_path.stem.startswith("LV_")]



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


def process_synthetic_grids(dbc: DatabaseClient, plz: int, output_dir: Path) -> pd.DataFrame:
    """Calculate and export comparison parameters for synthetic grids in one postcode area."""
    print(f"Processing synthetic grids for PLZ {plz}...")
    from pylovo.analysis.parameter_calculation import ParameterCalculator

    calc = ParameterCalculator()
    calc.dbc = dbc
    return export_synthetic_comparison_parameters_for_plz(calc, plz, output_dir=output_dir)


def process_real_grids(dbc: DatabaseClient, data_path: str, plz: int, output_dir: Path) -> pd.DataFrame:
    """Calculate and export comparison parameters for real LV JSON subnets."""
    print(f"Processing real grids from {data_path}...")

    buildings_gdf = _load_buildings_for_plz(dbc, plz)

    from pylovo.analysis.parameter_calculation import ParameterCalculator

    metrics_list = []
    calc = ParameterCalculator()
    calc.dbc = dbc

    for file_path in iter_real_grid_files(data_path):
        try:
            net = pp.from_json(str(file_path))
            consumer_buses = _infer_real_grid_consumer_buses(net, buildings_gdf)

            params = compute_comparison_parameters(calc, net, consumer_buses=consumer_buses)
            params["grid_name"] = file_path.stem
            params["file_name"] = file_path.name
            metrics_list.append(params)
        except Exception as exc:
            print(f"Error processing real grid {file_path.name}: {exc}")

    if not metrics_list:
        return pd.DataFrame()

    df = pd.DataFrame(metrics_list)
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "real_grid_metrics.csv"
    df.to_csv(csv_path, index=False)
    print(f"Saved real grid metrics to {csv_path}")
    return df


def run_grid_comparison(plz: int, output_dir: Path, data_path: str | None = None) -> None:
    """Run the full comparison workflow and write both metrics CSV files."""
    with DatabaseClient() as dbc:
        process_synthetic_grids(dbc, plz, output_dir)

        grid_data_path = Path(data_path) if data_path is not None else Path(GRID_DATA_PATH)
        if grid_data_path.exists():
            process_real_grids(dbc, str(grid_data_path), plz, output_dir)
        else:
            print(f"GRID_DATA_PATH not found or invalid: {grid_data_path}")


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
        if pooled_iqr <= 0:
            pooled_iqr = float(np.nanstd(pooled))
        if pooled_iqr <= 0:
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
    "process_real_grids",
    "process_synthetic_grids",
    "run_grid_comparison",
]