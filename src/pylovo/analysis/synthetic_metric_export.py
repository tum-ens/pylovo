"""Export synthetic comparison metrics from the PyLoVo database."""
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
import pandapower as pp

from pylovo.analysis.grid_analysis import compute_comparison_parameters
from pylovo.database.config_table_structure import CREATE_QUERIES

if TYPE_CHECKING:
    from pylovo.analysis.parameter_calculation import ParameterCalculator


MINI_GRID_BUS_THRESHOLD = 5

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


def process_synthetic_grids(
    dbc,
    plz: int,
    output_dir: Path,
    output_suffix: str = "",
    with_service_lines: bool = False,
) -> pd.DataFrame:
    """Export synthetic comparison metrics for one postcode area."""
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


def export_synthetic_comparison_parameters_for_plz(
    calculator: "ParameterCalculator",
    plz: int,
    limit: int | None = None,
    output_dir: Path | None = None,
    output_suffix: str = "",
    with_service_lines: bool = False,
) -> pd.DataFrame:
    """Compute, persist, and export comparison metrics for synthetic grids."""
    calculator.plz = plz

    _reset_grid_parameters_table(calculator)
    calculator.dbc.conn.commit()

    calculator.dbc.cur.execute(
        """
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
                """
                SELECT grid_result_id
                FROM pylovo.grid_result
                WHERE plz=%s AND kcid=%s AND bcid=%s AND version_id=%s
                """,
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
            params = build_metric_error_row(net, exc)
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
    csv_path = out_dir / metric_filename("synthetic_grid_metrics.csv", output_suffix)
    select_export_columns(df, SYNTHETIC_METRIC_ID_COLUMNS).to_csv(csv_path, index=False)
    print(f"Saved synthetic grid metrics to {csv_path}")
    return df


def select_export_columns(df: pd.DataFrame, id_columns: list[str]) -> pd.DataFrame:
    """Return the benchmark-facing metrics table with available columns only."""
    selected = [col for col in id_columns + COMPARISON_METRIC_COLUMNS if col in df.columns]
    return df[selected].copy()


def build_metric_error_row(net: pp.pandapowerNet | None, exc: Exception) -> dict:
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


def metric_filename(filename: str, output_suffix: str = "") -> str:
    if not output_suffix:
        return filename
    clean = output_suffix.strip().strip("_")
    if not clean:
        return filename
    path = Path(filename)
    return f"{path.stem}_{clean}{path.suffix}"


def _upsert_comparison_parameters(
    calculator: "ParameterCalculator",
    grid_result_id: int,
    params: dict,
) -> None:
    query = """
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
    calculator.dbc.cur.execute("DROP TABLE IF EXISTS pylovo.grid_parameters")
    calculator.dbc.cur.execute(CREATE_QUERIES["grid_parameters"])


__all__ = [
    "COMPARISON_METRIC_COLUMNS",
    "MINI_GRID_BUS_THRESHOLD",
    "SYNTHETIC_METRIC_ID_COLUMNS",
    "build_metric_error_row",
    "export_synthetic_comparison_parameters_for_plz",
    "metric_filename",
    "process_synthetic_grids",
    "select_export_columns",
]
