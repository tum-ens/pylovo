from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import ipywidgets as widgets
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import plotly.express as px
import seaborn as sns
from IPython.display import Markdown, display

from pylovo.analysis.comparison_helpers import (
    compute_wasserstein_summary,
    iter_real_grid_files,
)
from pylovo.config_loader import GRID_DATA_PATH, VERSION_ID
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.validation.metric_validation import plot_comparison_distribution_plotly


DEFAULT_METRICS = [
    "feeder_lines",
    "graph_length",
    "avg_trafo_distance",
    "max_trafo_distance",
    "transformer_mva",
    "graph_resistance",
]

DEFAULT_LABELS = {
    "feeder_lines": "Feeder Lines (count)",
    "buildings_per_feeder": "Buildings per Feeder",
    "graph_length": "Graph Length (km)",
    "avg_trafo_distance": "Avg. Distance to Trafo (km)",
    "max_trafo_distance": "Max. Distance to Trafo (km)",
    "transformer_mva": "Transformer Rating (MVA)",
    "graph_resistance": "Graph Resistance Proxy (Ohm)",
}

STATUS_ORDER = ["converged", "voltage_violation", "not_converged", "unknown"]


@dataclass(frozen=True)
class ComparisonNotebookData:
    synthetic_path: Path
    real_path: Path
    metrics: list[str]
    requested_metrics: list[str]
    missing_metrics: list[str]
    labels: dict[str, str]
    df_synth_all: pd.DataFrame
    df_real: pd.DataFrame
    df_all: pd.DataFrame
    status_counts: pd.DataFrame
    status_metric_wasserstein: pd.DataFrame
    status_overview: pd.DataFrame
    wasserstein_table: pd.DataFrame


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_metrics_path(filename: str, metrics_dir: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if metrics_dir is not None:
        candidates.append(Path(metrics_dir) / filename)

    project_root = _project_root()
    candidates.extend(
        [
            project_root / "validation" / "metrics" / filename,
            project_root / "validation" / "grid_comparison" / "metrics" / filename,
            project_root / "metrics" / filename,
            Path.cwd() / "validation" / "metrics" / filename,
            Path.cwd() / "metrics" / filename,
        ]
    )

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def _load_synthetic_metrics(metrics_dir: Path | None = None) -> tuple[pd.DataFrame, Path]:
    synthetic_path = resolve_metrics_path("synthetic_grid_metrics.csv", metrics_dir)
    if synthetic_path is None:
        raise FileNotFoundError(
            "Synthetic comparison metrics CSV was not found. Run `uv run pylovo-validate compare-grids`."
        )

    df_synth = pd.read_csv(synthetic_path)
    if "power_flow_status" not in df_synth.columns:
        df_synth["power_flow_status"] = "converged"
    df_synth["power_flow_status"] = df_synth["power_flow_status"].fillna("unknown")
    df_synth["Type"] = "Synthetic"
    df_synth["source"] = "Synthetic"
    return df_synth, synthetic_path


def _load_real_metrics(metrics_dir: Path | None = None) -> tuple[pd.DataFrame, Path]:
    real_path = resolve_metrics_path("real_grid_metrics.csv", metrics_dir)
    if real_path is None:
        raise FileNotFoundError(
            "Real comparison metrics CSV was not found. Run `uv run pylovo-validate compare-grids`."
        )

    df_real = pd.read_csv(real_path)
    df_real["Type"] = "Real"
    df_real["source"] = "Real"
    return df_real, real_path


def _status_counts(df_synth_all: pd.DataFrame) -> pd.DataFrame:
    status_counts = (
        df_synth_all["power_flow_status"]
        .fillna("unknown")
        .value_counts()
        .rename_axis("power_flow_status")
        .reset_index(name="count")
    )
    status_counts["power_flow_status"] = pd.Categorical(
        status_counts["power_flow_status"],
        categories=STATUS_ORDER,
        ordered=True,
    )
    return status_counts.sort_values("power_flow_status").reset_index(drop=True)


def compute_status_diagnostics(
    df_synth_all: pd.DataFrame,
    df_real: pd.DataFrame,
    metrics: list[str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows: list[pd.DataFrame] = []
    available_statuses = df_synth_all["power_flow_status"].dropna().unique().tolist()

    for status in STATUS_ORDER:
        if status not in available_statuses:
            continue

        synthetic_slice = df_synth_all[df_synth_all["power_flow_status"] == status].copy()
        combined = pd.concat([synthetic_slice, df_real], ignore_index=True, sort=False)
        table = compute_wasserstein_summary(combined, metrics)
        if table.empty:
            continue
        table.insert(0, "power_flow_status", status)
        rows.append(table)

    if not rows:
        empty = pd.DataFrame(
            columns=[
                "power_flow_status",
                "metric",
                "synthetic_n",
                "real_n",
                "wasserstein_distance",
                "pooled_iqr",
                "normalized_wasserstein",
                "quality",
            ]
        )
        return empty, pd.DataFrame(
            columns=[
                "power_flow_status",
                "synthetic_n",
                "real_n",
                "mean_normalized_wasserstein",
                "median_normalized_wasserstein",
                "worst_metric",
                "worst_normalized_wasserstein",
            ]
        )

    status_metric_wasserstein = pd.concat(rows, ignore_index=True)
    overview = (
        status_metric_wasserstein.sort_values("normalized_wasserstein", ascending=False)
        .groupby("power_flow_status", as_index=False)
        .agg(
            synthetic_n=("synthetic_n", "max"),
            real_n=("real_n", "max"),
            mean_normalized_wasserstein=("normalized_wasserstein", "mean"),
            median_normalized_wasserstein=("normalized_wasserstein", "median"),
            worst_metric=("metric", "first"),
            worst_normalized_wasserstein=("normalized_wasserstein", "first"),
        )
    )
    overview["power_flow_status"] = pd.Categorical(
        overview["power_flow_status"],
        categories=STATUS_ORDER,
        ordered=True,
    )
    overview = overview.sort_values("power_flow_status").reset_index(drop=True)
    return status_metric_wasserstein, overview


def load_notebook_data(
    metrics_dir: Path | None = None,
    metrics: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> ComparisonNotebookData:
    requested_metrics = list(metrics) if metrics is not None else list(DEFAULT_METRICS)
    active_labels = dict(labels) if labels is not None else dict(DEFAULT_LABELS)

    df_synth_all, synthetic_path = _load_synthetic_metrics(metrics_dir)
    df_real, real_path = _load_real_metrics(metrics_dir)
    df_all = pd.concat([df_synth_all, df_real], ignore_index=True, sort=False)
    available_metrics = [metric for metric in requested_metrics if metric in df_all.columns]
    missing_metrics = [metric for metric in requested_metrics if metric not in df_all.columns]

    status_counts = _status_counts(df_synth_all)
    status_metric_wasserstein, status_overview = compute_status_diagnostics(
        df_synth_all,
        df_real,
        available_metrics,
    )
    wasserstein_table = compute_wasserstein_summary(df_all, available_metrics)

    return ComparisonNotebookData(
        synthetic_path=synthetic_path,
        real_path=real_path,
        metrics=available_metrics,
        requested_metrics=requested_metrics,
        missing_metrics=missing_metrics,
        labels=active_labels,
        df_synth_all=df_synth_all,
        df_real=df_real,
        df_all=df_all,
        status_counts=status_counts,
        status_metric_wasserstein=status_metric_wasserstein,
        status_overview=status_overview,
        wasserstein_table=wasserstein_table,
    )


def render_top_overview(data: ComparisonNotebookData) -> ComparisonNotebookData:
    display(Markdown("### Data Inputs"))
    display(
        pd.DataFrame(
            {
                "source": ["Synthetic", "Real"],
                "path": [str(data.synthetic_path), str(data.real_path)],
                "rows": [len(data.df_synth_all), len(data.df_real)],
            }
        )
    )

    if data.missing_metrics:
        missing_metrics_text = ", ".join(data.missing_metrics)
        display(
            Markdown(
                f"**Missing exported metrics:** {missing_metrics_text}. "
                "Regenerate the comparison CSVs to include them in the calibration views."
            )
        )

    display(Markdown("### Status-Stratified Diagnostics"))
    display(data.status_counts)
    if not data.status_overview.empty:
        display(data.status_overview.round(4))

    display(Markdown("### Primary Wasserstein Score"))
    if data.wasserstein_table.empty:
        display(Markdown("No Wasserstein results available."))
    else:
        display_cols = [
            "metric",
            "synthetic_n",
            "real_n",
            "wasserstein_distance",
            "normalized_wasserstein",
            "quality",
        ]
        display(data.wasserstein_table[display_cols].round(4))

    return data


def load_and_render_overview(
    metrics_dir: Path | None = None,
    metrics: list[str] | None = None,
    labels: dict[str, str] | None = None,
) -> ComparisonNotebookData:
    data = load_notebook_data(metrics_dir=metrics_dir, metrics=metrics, labels=labels)
    return render_top_overview(data)


def show_distribution_selector(
    df: pd.DataFrame,
    metrics: list[str],
    labels: dict[str, str],
    *,
    plot_type: str,
    height: int = 520,
    width: int | None = None,
):
    def _plot(metric: str) -> None:
        fig = plot_comparison_distribution_plotly(
            df,
            metric_col=metric,
            title=f"{plot_type.title()}: {labels.get(metric, metric)}",
            plot_type=plot_type,
        )
        fig.update_yaxes(title_text=labels.get(metric, metric))
        layout_kwargs = {"height": height}
        if width is not None:
            layout_kwargs["width"] = width
        fig.update_layout(**layout_kwargs)
        fig.show()

    control = widgets.interactive(
        _plot,
        metric=widgets.Dropdown(options=metrics, description="Parameter:"),
    )
    display(control)
    return None


def plot_boxplot_overview(
    df: pd.DataFrame,
    metric_cols: list[str],
    *,
    labels: dict[str, str] | None = None,
    source_col: str = "source",
    palette: dict[str, str] | None = None,
    hue_order: list[str] | None = None,
    title: str = "Box Plot Overview: All Metrics (Synthetic vs. Real)",
    whis: float = 1.5,
    showfliers: bool = False,
):
    if df.empty:
        raise ValueError("No data available for the boxplot overview.")

    n_cols = 3
    n_rows = int(np.ceil(len(metric_cols) / n_cols))
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(5 * n_cols, 4.5 * n_rows))
    axes = np.atleast_1d(axes).ravel()

    active_palette = palette or {"Synthetic": "steelblue", "Real": "crimson"}
    active_hue_order = hue_order or ["Synthetic", "Real"]

    for i, metric in enumerate(metric_cols):
        ax = axes[i]
        sub = df[[source_col, metric]].dropna(subset=[metric])
        sns.boxplot(
            data=sub,
            x=source_col,
            y=metric,
            hue=source_col,
            order=active_hue_order,
            hue_order=active_hue_order,
            palette=active_palette,
            dodge=False,
            legend=False,
            whis=whis,
            showfliers=showfliers,
            width=0.45,
            flierprops=dict(marker="o", markersize=3, alpha=0.5),
            ax=ax,
        )

        iqr_parts: list[str] = []
        for source_name in active_hue_order:
            source_values = pd.to_numeric(
                sub.loc[sub[source_col] == source_name, metric],
                errors="coerce",
            ).dropna()
            if source_values.empty:
                continue
            q1 = float(source_values.quantile(0.25))
            q3 = float(source_values.quantile(0.75))
            iqr_parts.append(f"{source_name[0]}: {q3 - q1:.3g}")

        ax.set_title((labels or {}).get(metric, metric), fontsize=10)
        ax.set_xlabel("")
        ax.set_ylabel((labels or {}).get(metric, metric), fontsize=8)
        ax.grid(axis="y", alpha=0.25)
        if iqr_parts:
            ax.text(
                0.02,
                0.98,
                "IQR " + " | ".join(iqr_parts),
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=8,
                bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "none"},
            )

    for j in range(len(metric_cols), len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(title, fontsize=13, fontweight="bold", y=0.995)
    fig.text(
        0.5,
        0.965,
        f"Whiskers span Q1 - {whis}*IQR to Q3 + {whis}*IQR; panel labels show per-source IQR.",
        ha="center",
        va="top",
        fontsize=9,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    plt.show()
    return fig


def plot_metric_kde_diagonal(
    df: pd.DataFrame,
    metric_cols: list[str],
    *,
    labels: dict[str, str] | None = None,
    hue_col: str = "source",
    palette: dict[str, str] | None = None,
    show_hist_bars: bool = False,
    bins: int = 24,
    n_cols: int = 3,
    title: str = "Per-metric KDE View (Synthetic vs. Real)",
):
    plot_data = df[metric_cols + [hue_col]].dropna(subset=[hue_col]).copy()
    if plot_data.empty:
        raise ValueError("No data available for KDE diagonal view.")

    n_metrics = len(metric_cols)
    n_rows = int(np.ceil(n_metrics / n_cols))
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(5.2 * n_cols, 3.6 * n_rows))
    axes = np.atleast_1d(axes).ravel()

    hue_order = list(plot_data[hue_col].dropna().unique())

    for i, metric in enumerate(metric_cols):
        ax = axes[i]
        sub = plot_data[[metric, hue_col]].dropna(subset=[metric])
        if sub.empty:
            ax.set_visible(False)
            continue

        if show_hist_bars:
            sns.histplot(
                data=sub,
                x=metric,
                hue=hue_col,
                hue_order=hue_order,
                palette=palette,
                bins=bins,
                stat="count",
                common_norm=False,
                element="bars",
                alpha=0.28,
                kde=True,
                ax=ax,
                legend=(i == 0),
            )
            ax.set_ylabel("Count")
        else:
            sns.kdeplot(
                data=sub,
                x=metric,
                hue=hue_col,
                hue_order=hue_order,
                palette=palette,
                common_norm=False,
                fill=False,
                linewidth=1.8,
                ax=ax,
                legend=(i == 0),
            )
            ax.set_ylabel("Density")

        ax.set_title((labels or {}).get(metric, metric), fontsize=10)
        ax.set_xlabel((labels or {}).get(metric, metric))
        ax.grid(alpha=0.15)

    for j in range(n_metrics, len(axes)):
        axes[j].set_visible(False)

    handles, legend_labels = axes[0].get_legend_handles_labels()
    if handles:
        if axes[0].legend_ is not None:
            axes[0].legend_.remove()
        fig.legend(handles, legend_labels, title="Source", loc="upper right")

    fig.suptitle(title, y=1.02, fontsize=13)
    fig.tight_layout()
    plt.show()
    return fig


def _load_real_net(file_path: Path):
    suffix = file_path.suffix.lower()
    if suffix == ".json":
        import pandapower as pp

        return pp.from_json(str(file_path))
    if suffix in {".xlsx", ".xls", ".xlsm"}:
        import pandapower as pp

        return pp.from_excel(str(file_path))
    raise ValueError(f"Unsupported real-grid format: {file_path.name}")


def _collect_cable_type_rows(net, source: str) -> list[dict[str, object]]:
    if net.line.empty or "std_type" not in net.line.columns:
        return []

    rows: list[dict[str, object]] = []
    line_df = net.line.copy()
    if "in_service" in line_df.columns:
        line_df = line_df[line_df["in_service"] != False]
    if line_df.empty:
        return rows

    grouped = line_df.groupby("std_type", dropna=True)
    for std_type, group in grouped:
        parallel = pd.to_numeric(group.get("parallel", 1), errors="coerce").fillna(1.0)
        length = pd.to_numeric(group.get("length_km", 0.0), errors="coerce").fillna(0.0)
        r_ohm = pd.to_numeric(group.get("r_ohm_per_km", np.nan), errors="coerce")
        x_ohm = pd.to_numeric(group.get("x_ohm_per_km", np.nan), errors="coerce")

        impedance = np.sqrt(r_ohm.pow(2) + x_ohm.pow(2))
        impedance_value = float(impedance.dropna().iloc[0]) if impedance.notna().any() else np.nan

        rows.append(
            {
                "source": source,
                "std_type": str(std_type),
                "segment_count": float(parallel.sum()),
                "total_length_km": float((length * parallel).sum()),
                "impedance_ohm_per_km": impedance_value,
            }
        )
    return rows


def load_cable_type_comparison(
    plz: int,
    *,
    real_grid_dir: Path | str | None = None,
    version_id: str = VERSION_ID,
    min_segment_count: float = 100.0,
) -> pd.DataFrame:
    real_dir = Path(real_grid_dir) if real_grid_dir is not None else Path(GRID_DATA_PATH)
    rows: list[dict[str, object]] = []

    with DatabaseClient() as dbc:
        dbc.cur.execute(
            """
            SELECT kcid, bcid
            FROM grid_result
            WHERE plz = %s AND version_id = %s
            ORDER BY kcid, bcid
            """,
            (plz, str(version_id)),
        )
        for kcid, bcid in dbc.cur.fetchall():
            net = dbc.read_net_db(plz, kcid, bcid, version_id=version_id)
            rows.extend(_collect_cable_type_rows(net, "Synthetic"))

    for file_path in iter_real_grid_files(str(real_dir)):
        try:
            net = _load_real_net(file_path)
        except Exception:
            continue
        rows.extend(_collect_cable_type_rows(net, "Real"))

    if not rows:
        return pd.DataFrame(
            columns=[
                "source",
                "std_type",
                "segment_count",
                "total_length_km",
                "impedance_ohm_per_km",
            ]
        )

    result = pd.DataFrame(rows)
    result = (
        result.groupby(["source", "std_type"], as_index=False)
        .agg(
            segment_count=("segment_count", "sum"),
            total_length_km=("total_length_km", "sum"),
            impedance_ohm_per_km=("impedance_ohm_per_km", "first"),
        )
    )
    result = result.merge(
        result.groupby("std_type", as_index=False)
        .agg(combined_segment_count=("segment_count", "sum")),
        on="std_type",
        how="left",
    )
    result = result[result["combined_segment_count"] > float(min_segment_count)].copy()
    return result.sort_values(["impedance_ohm_per_km", "std_type", "source"]).reset_index(drop=True)


def show_cable_type_comparison(
    plz: int,
    *,
    real_grid_dir: Path | str | None = None,
    version_id: str = VERSION_ID,
    min_segment_count: float = 100.0,
):
    cable_df = load_cable_type_comparison(
        plz,
        real_grid_dir=real_grid_dir,
        version_id=version_id,
        min_segment_count=min_segment_count,
    )
    if cable_df.empty:
        display(
            Markdown(
                f"No cable type comparison data available above the current threshold of "
                f"more than {min_segment_count:.0f} weighted line segments."
            )
        )
        return None

    display(
        Markdown(
            f"Showing cable types with more than {min_segment_count:.0f} weighted line segments across real and synthetic grids. "
            "`segment_count` counts every in-service `net.line` segment between modeled nodes; parallel circuits contribute via their `parallel` multiplier, so this is not a whole-feeder count."
        )
    )

    display(
        cable_df[
            [
                "std_type",
                "source",
                "segment_count",
                "total_length_km",
                "impedance_ohm_per_km",
            ]
        ].round(4)
    )

    fig = px.bar(
        cable_df,
        x="std_type",
        y="segment_count",
        color="source",
        barmode="group",
        hover_data=["total_length_km", "impedance_ohm_per_km"],
        category_orders={
            "std_type": cable_df.sort_values("impedance_ohm_per_km")["std_type"].unique().tolist(),
        },
        title=f"Cable Type Comparison Ordered by Impedance (> {min_segment_count:.0f} weighted line segments)",
        labels={
            "std_type": "Cable Type",
            "segment_count": "Weighted Line Segments",
            "source": "Source",
        },
    )
    fig.update_layout(template="plotly_white", height=500)
    fig.show()
    return fig


def build_wasserstein_bar_figure(wasserstein_table: pd.DataFrame) -> px.bar:
    fig = px.bar(
        wasserstein_table.sort_values("normalized_wasserstein", ascending=False),
        x="metric",
        y="normalized_wasserstein",
        color="quality",
        title="Normalized Wasserstein Distance by Metric",
        labels={
            "metric": "Metric",
            "normalized_wasserstein": "Normalized Wasserstein Distance",
            "quality": "Quality",
        },
        category_orders={
            "quality": ["excellent", "good", "acceptable", "poor", "insufficient_data"],
        },
    )
    fig.add_hline(y=0.10, line_dash="dot", line_color="green", annotation_text="excellent")
    fig.add_hline(y=0.25, line_dash="dot", line_color="royalblue", annotation_text="good")
    fig.update_layout(template="plotly_white", height=500)
    return fig


def show_wasserstein_summary(
    data: ComparisonNotebookData | pd.DataFrame,
    metrics: list[str] | None = None,
):
    if isinstance(data, ComparisonNotebookData):
        wasserstein_table = data.wasserstein_table
    else:
        if metrics is None:
            raise ValueError("metrics must be provided when passing a dataframe to show_wasserstein_summary().")
        wasserstein_table = compute_wasserstein_summary(data, metrics)

    if wasserstein_table.empty:
        display(Markdown("No Wasserstein results available."))
        return None

    display(wasserstein_table[[
        "metric",
        "synthetic_n",
        "real_n",
        "wasserstein_distance",
        "normalized_wasserstein",
        "quality",
    ]].round(4))
    fig = build_wasserstein_bar_figure(wasserstein_table)
    fig.show()
    return fig


__all__ = [
    "ComparisonNotebookData",
    "DEFAULT_LABELS",
    "DEFAULT_METRICS",
    "build_wasserstein_bar_figure",
    "compute_status_diagnostics",
    "load_and_render_overview",
    "load_cable_type_comparison",
    "load_notebook_data",
    "plot_boxplot_overview",
    "plot_metric_kde_diagonal",
    "render_top_overview",
    "resolve_metrics_path",
    "show_cable_type_comparison",
    "show_distribution_selector",
    "show_wasserstein_summary",
]