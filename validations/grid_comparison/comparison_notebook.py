from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import ipywidgets as widgets
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import seaborn as sns
from IPython.display import Markdown, display

from validations.grid_comparison.common import (
    metric_filename,
    metric_output_suffixes,
    validation_grid_data_path,
    validation_grid_split_subdir,
)
from validations.grid_comparison.scoring import (
    compute_wasserstein_summary,
    get_wasserstein_thresholds,
    iter_real_grid_files,
)
from pylovo.config_loader import GRID_DATA_PATH, VERSION_ID
from pylovo.database.database_client import DatabaseClient
from pylovo.plotting.validation.metric_validation import plot_comparison_distribution_plotly


DEFAULT_METRICS = [
    "transformer_mva",
    "avg_trafo_distance",
    "max_trafo_distance",
    "feeder_lines",
    "graph_length",
    "graph_resistance",
]

DEFAULT_LABELS = {
    "transformer_mva": "Transformer Rating (MVA)",
    "avg_trafo_distance": "Avg. Distance to Trafo (km)",
    "max_trafo_distance": "Max. Distance to Trafo (km)",
    "feeder_lines": "Feeder Lines (count)",
    "graph_length": "Graph Length (km)",
    "graph_resistance": "Graph Resistance Proxy (Ohm)",
}

STATUS_ORDER = ["converged", "voltage_violation", "not_converged", "unknown"]


@dataclass(frozen=True)
class ComparisonNotebookData:
    synthetic_path: Path
    real_path: Path
    swn_path: Path | None
    metrics: list[str]
    requested_metrics: list[str]
    missing_metrics: list[str]
    labels: dict[str, str]
    df_synth_all: pd.DataFrame
    df_real: pd.DataFrame
    df_swn: pd.DataFrame
    df_all: pd.DataFrame
    status_counts: pd.DataFrame
    status_metric_wasserstein: pd.DataFrame
    status_overview: pd.DataFrame
    wasserstein_table: pd.DataFrame


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_metric_filenames(metrics_dir: Path | None = None) -> tuple[str, str]:
    """Return notebook-friendly synthetic and real metrics filenames.

    The primary names follow the CLI convention. If those files have not been
    generated yet, fall back to the newest available matching CSV so the
    notebook remains usable after configuration changes.
    """
    metrics_root = Path(metrics_dir) if metrics_dir is not None else _project_root() / "validations" / "metrics"
    split_subdir = validation_grid_split_subdir()
    synthetic_suffix, real_suffix, _ = metric_output_suffixes(None, split_subdir)
    expected_synthetic = metric_filename("synthetic_grid_metrics.csv", synthetic_suffix)
    expected_real = metric_filename("real_grid_metrics.csv", real_suffix)

    return (
        _existing_or_newest_metric_filename(metrics_root, expected_synthetic, "synthetic_grid_metrics*.csv"),
        _existing_or_newest_metric_filename(metrics_root, expected_real, "real_grid_metrics*.csv"),
    )


def _existing_or_newest_metric_filename(metrics_dir: Path, expected_filename: str, pattern: str) -> str:
    expected_path = metrics_dir / expected_filename
    if expected_path.exists():
        return expected_filename

    candidates = sorted(
        metrics_dir.glob(pattern),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0].name
    return expected_filename


def selected_metric_filenames(
    synthetic_metrics_file: str | None = None,
    real_metrics_file: str | None = None,
    metrics_dir: Path | None = None,
) -> tuple[str, str]:
    default_synthetic, default_real = default_metric_filenames(metrics_dir)
    return (
        synthetic_metrics_file.strip() if synthetic_metrics_file and synthetic_metrics_file.strip() else default_synthetic,
        real_metrics_file.strip() if real_metrics_file and real_metrics_file.strip() else default_real,
    )


def resolve_metrics_path(filename: str | Path, metrics_dir: Path | None = None) -> Path | None:
    filename = Path(filename)
    candidates: list[Path] = []
    if filename.is_absolute():
        candidates.append(filename)
    elif metrics_dir is not None:
        candidates.append(Path(metrics_dir) / filename)

    project_root = _project_root()
    candidates.extend(
        [
            project_root / "validations" / "metrics" / filename,
            project_root / "validation" / "metrics" / filename,
            project_root / "validation" / "grid_comparison" / "metrics" / filename,
            project_root / "metrics" / filename,
            Path.cwd() / "validations" / "metrics" / filename,
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


def _load_synthetic_metrics(
    metrics_dir: Path | None = None,
    metrics_filename: str | Path = "synthetic_grid_metrics.csv",
) -> tuple[pd.DataFrame, Path]:
    synthetic_path = resolve_metrics_path(metrics_filename, metrics_dir)
    if synthetic_path is None:
        raise FileNotFoundError(
            f"Synthetic comparison metrics CSV '{metrics_filename}' was not found. "
            "Run `uv run pylovo-validate compare-grids` or adjust SYNTHETIC_METRICS_FILE."
        )

    df_synth = pd.read_csv(synthetic_path)
    if "power_flow_status" not in df_synth.columns:
        df_synth["power_flow_status"] = "converged"
    df_synth["power_flow_status"] = df_synth["power_flow_status"].fillna("unknown")
    df_synth["Type"] = "Synthetic"
    df_synth["source"] = "Synthetic"
    return df_synth, synthetic_path


def _load_real_metrics(
    metrics_dir: Path | None = None,
    metrics_filename: str | Path = "real_grid_metrics.csv",
) -> tuple[pd.DataFrame, Path]:
    real_path = resolve_metrics_path(metrics_filename, metrics_dir)
    if real_path is None:
        raise FileNotFoundError(
            f"Real comparison metrics CSV '{metrics_filename}' was not found. "
            "Run `uv run pylovo-validate compare-grids` or adjust REAL_METRICS_FILE."
        )

    df_real = pd.read_csv(real_path)
    df_real["Type"] = "Real"
    df_real["source"] = "Real"
    return df_real, real_path


def _load_swn_metrics(
    metrics_filename: str | Path | None = None,
    *,
    metrics_dir: Path | None = None,
    scope: str = "backbone",
) -> tuple[pd.DataFrame, Path | None]:
    if metrics_filename is None:
        default_dir = validation_grid_data_path() / "swn2pandapower"
        metric_paths = [default_dir / "SWN_metrics.csv"]
        metric_paths.extend(sorted(default_dir.glob("SWN_*/SWN_metrics.csv")))
        metric_paths = [path.resolve() for path in metric_paths if path.exists()]
        if not metric_paths:
            return pd.DataFrame(), None
        swn_path = default_dir.resolve()
    else:
        swn_path = resolve_metrics_path(metrics_filename, metrics_dir)
        if swn_path is None:
            raise FileNotFoundError(
                f"SWN comparison metrics CSV '{metrics_filename}' was not found. "
                "Run the SWN converter or adjust SWN_METRICS_FILE."
            )

        metric_paths = [swn_path]

    df_swn = pd.concat(
        [pd.read_csv(path) for path in metric_paths], ignore_index=True
    )
    if "scope" in df_swn.columns:
        df_swn = df_swn.loc[df_swn["scope"] == scope].copy()
    df_swn["Type"] = "SWN"
    df_swn["source"] = "SWN"
    return df_swn, swn_path


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
    synthetic_metrics_filename: str | Path = "synthetic_grid_metrics.csv",
    real_metrics_filename: str | Path = "real_grid_metrics.csv",
    swn_metrics_filename: str | Path | None = None,
    swn_scope: str = "backbone",
) -> ComparisonNotebookData:
    requested_metrics = list(metrics) if metrics is not None else list(DEFAULT_METRICS)
    active_labels = dict(labels) if labels is not None else dict(DEFAULT_LABELS)

    df_synth_all, synthetic_path = _load_synthetic_metrics(
        metrics_dir,
        metrics_filename=synthetic_metrics_filename,
    )
    df_real, real_path = _load_real_metrics(
        metrics_dir,
        metrics_filename=real_metrics_filename,
    )
    df_swn, swn_path = _load_swn_metrics(
        swn_metrics_filename,
        metrics_dir=metrics_dir,
        scope=swn_scope,
    )
    df_all = pd.concat(
        [df_synth_all, df_real, df_swn], ignore_index=True, sort=False
    )
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
        swn_path=swn_path,
        metrics=available_metrics,
        requested_metrics=requested_metrics,
        missing_metrics=missing_metrics,
        labels=active_labels,
        df_synth_all=df_synth_all,
        df_real=df_real,
        df_swn=df_swn,
        df_all=df_all,
        status_counts=status_counts,
        status_metric_wasserstein=status_metric_wasserstein,
        status_overview=status_overview,
        wasserstein_table=wasserstein_table,
    )


def render_top_overview(data: ComparisonNotebookData) -> ComparisonNotebookData:
    display(Markdown("### Data Inputs"))
    sources = ["Synthetic", "Real"]
    paths = [str(data.synthetic_path), str(data.real_path)]
    row_counts = [len(data.df_synth_all), len(data.df_real)]
    if data.swn_path is not None:
        sources.append("SWN")
        paths.append(str(data.swn_path))
        row_counts.append(len(data.df_swn))
    display(
        pd.DataFrame(
            {
                "source": sources,
                "path": paths,
                "rows": row_counts,
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
    synthetic_metrics_filename: str | Path = "synthetic_grid_metrics.csv",
    real_metrics_filename: str | Path = "real_grid_metrics.csv",
    swn_metrics_filename: str | Path | None = None,
    swn_scope: str = "backbone",
) -> ComparisonNotebookData:
    data = load_notebook_data(
        metrics_dir=metrics_dir,
        metrics=metrics,
        labels=labels,
        synthetic_metrics_filename=synthetic_metrics_filename,
        real_metrics_filename=real_metrics_filename,
        swn_metrics_filename=swn_metrics_filename,
        swn_scope=swn_scope,
    )
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
    dropdown = widgets.Dropdown(options=metrics, description="Parameter:")
    output = widgets.Output()

    def _build_figure(metric: str):
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
        return fig

    def _render(metric: str) -> None:
        with output:
            output.clear_output(wait=True)
            display(_build_figure(metric))

    def _on_metric_change(change) -> None:
        if change["name"] == "value":
            _render(change["new"])

    dropdown.observe(_on_metric_change, names="value")
    _render(dropdown.value)
    control = widgets.VBox([dropdown, output])
    display(control)
    return control


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
    save_path: str | Path | None = None,
):
    if df.empty:
        raise ValueError("No data available for the boxplot overview.")

    n_cols = 3
    n_rows = int(np.ceil(len(metric_cols) / n_cols))
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(5.6 * n_cols, 5.0 * n_rows))
    axes = np.atleast_1d(axes).ravel()

    active_palette = palette or {
        "Synthetic": "steelblue",
        "Real": "crimson",
        "SWN": "darkorange",
    }
    active_hue_order = hue_order or [
        source for source in ("Synthetic", "Real", "SWN") if source in set(df[source_col])
    ]

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

        ax.set_title((labels or {}).get(metric, metric), fontsize=17)
        ax.set_xlabel("")
        ax.set_ylabel((labels or {}).get(metric, metric), fontsize=16)
        ax.tick_params(axis="both", labelsize=14)
        ax.grid(axis="y", alpha=0.25)
        if iqr_parts:
            ax.text(
                0.02,
                0.98,
                "IQR " + " | ".join(iqr_parts),
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=11,
                bbox={"boxstyle": "round,pad=0.25", "facecolor": "white", "alpha": 0.75, "edgecolor": "none"},
            )

    for j in range(len(metric_cols), len(axes)):
        axes[j].set_visible(False)

    fig.suptitle(title, fontsize=20, fontweight="bold", y=0.995)
    fig.text(
        0.5,
        0.965,
        f"Whiskers span Q1 - {whis}*IQR to Q3 + {whis}*IQR; panel labels show per-source IQR.",
        ha="center",
        va="top",
        fontsize=13,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    if save_path is not None:
        fig.savefig(Path(save_path), bbox_inches="tight")
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
    upper_percentile: float | None = None,
    clip_nonnegative: bool = True,
    save_path: str | Path | None = None,
):
    if upper_percentile is not None and not 0 < upper_percentile <= 1:
        raise ValueError("upper_percentile must be in the interval (0, 1].")

    plot_data = df[metric_cols + [hue_col]].dropna(subset=[hue_col]).copy()
    if plot_data.empty:
        raise ValueError("No data available for KDE diagonal view.")

    n_metrics = len(metric_cols)
    n_rows = int(np.ceil(n_metrics / n_cols))
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(5.6 * n_cols, 4.2 * n_rows))
    axes = np.atleast_1d(axes).ravel()

    hue_order = list(plot_data[hue_col].dropna().unique())

    for i, metric in enumerate(metric_cols):
        ax = axes[i]
        sub = plot_data[[metric, hue_col]].dropna(subset=[metric]).copy()
        sub[metric] = pd.to_numeric(sub[metric], errors="coerce")
        sub = sub.dropna(subset=[metric])
        if upper_percentile is not None and not sub.empty:
            upper_bound = float(sub[metric].quantile(upper_percentile))
            sub = sub[sub[metric] <= upper_bound]
        if sub.empty:
            ax.set_visible(False)
            continue

        kde_clip = (0, None) if clip_nonnegative else None
        kde_cut = 0 if clip_nonnegative else 3

        if show_hist_bars:
            sns.histplot(
                data=sub,
                x=metric,
                hue=hue_col,
                hue_order=hue_order,
                palette=palette,
                bins=bins,
                stat="probability",
                common_norm=False,
                element="bars",
                alpha=0.28,
                kde=True,
                kde_kws={"cut": kde_cut, "clip": kde_clip},
                ax=ax,
                legend=(i == 0),
            )
            ax.set_ylabel("Share of Grids", fontsize=16)
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
                cut=kde_cut,
                clip=kde_clip,
                ax=ax,
                legend=(i == 0),
            )
            ax.set_ylabel("Density", fontsize=16)

        ax.set_title((labels or {}).get(metric, metric), fontsize=17)
        ax.set_xlabel("")
        ax.tick_params(axis="both", labelsize=14)
        ax.grid(alpha=0.15)

    for j in range(n_metrics, len(axes)):
        axes[j].set_visible(False)

    handles, legend_labels = axes[0].get_legend_handles_labels()
    if handles:
        if axes[0].legend_ is not None:
            axes[0].legend_.remove()
        fig.legend(handles, legend_labels, title="Source", loc="upper right", fontsize=15, title_fontsize=15)

    fig.suptitle(title, y=1.02, fontsize=20, fontweight="bold")
    fig.tight_layout()
    if save_path is not None:
        fig.savefig(Path(save_path), bbox_inches="tight")
    plt.show()
    return fig


def plot_scenario_kde_diagonal(
    scenarios: dict[str, str],
    metric_cols: list[str],
    *,
    metrics_dir: Path | None = None,
    real_metrics_filename: str | Path | None = None,
    labels: dict[str, str] | None = None,
    palette: dict[str, str] | None = None,
    show_hist_bars: bool = False,
    bins: int = 24,
    n_cols: int = 3,
    title: str = "Per-metric KDE View by Scenario",
    upper_percentile: float | None = None,
    clip_nonnegative: bool = True,
):
    """Plot metric distributions for several synthetic scenarios and one real reference.

    ``show_hist_bars=True`` overlays the same semi-transparent probability histogram +
    KDE combination used in the 2-group view instead of KDE-only lines - useful to see
    per-scenario sample density, not just the smoothed curve shape.
    """
    plot_df = load_scenario_metrics(
        scenarios,
        metrics_dir=metrics_dir,
        real_metrics_filename=real_metrics_filename,
        dataset_col="dataset",
    )
    dataset_order = list(scenarios.keys()) + ["Real"]
    active_palette = palette or {
        dataset_order[0]: "#4C78A8",
        **{label: color for label, color in zip(dataset_order[1:], ["#F58518", "#54A24B", "#B279A2", "#E45756"])}
    }

    fig = plot_metric_kde_diagonal(
        plot_df,
        metric_cols,
        labels=labels,
        hue_col="dataset",
        palette=active_palette,
        show_hist_bars=show_hist_bars,
        bins=bins,
        n_cols=n_cols,
        title=title,
        upper_percentile=upper_percentile,
        clip_nonnegative=clip_nonnegative,
    )
    for legend in list(fig.legends):
        legend.remove()
    handles = [
        Line2D([0], [0], color=active_palette.get(label, "black"), linewidth=2.0, label=label)
        for label in dataset_order
    ]
    fig.legend(handles=handles, title="", loc="upper right")
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

    line_df["parallel"] = pd.to_numeric(line_df.get("parallel", 1), errors="coerce").fillna(1.0)
    # Group by the exact parallel count (not just a parallel>1 boolean): this preserves how many
    # physical cables a given segment represents (2, 3, ...), needed for the parallel-cable
    # breakdown table below. "is_parallel" (parallel>1) marks capacity-limit runs, a signal
    # independent of cross-section.
    line_df["is_parallel"] = line_df["parallel"] > 1

    grouped = line_df.groupby(["std_type", "parallel"], dropna=True)
    for (std_type, parallel_value), group in grouped:
        parallel = group["parallel"]
        length = pd.to_numeric(group.get("length_km", 0.0), errors="coerce").fillna(0.0)
        r_ohm = pd.to_numeric(group.get("r_ohm_per_km", np.nan), errors="coerce")
        x_ohm = pd.to_numeric(group.get("x_ohm_per_km", np.nan), errors="coerce")

        impedance = np.sqrt(r_ohm.pow(2) + x_ohm.pow(2))
        impedance_value = float(impedance.dropna().iloc[0]) if impedance.notna().any() else np.nan

        rows.append(
            {
                "source": source,
                "std_type": str(std_type),
                "parallel": int(parallel_value),
                "is_parallel": bool(parallel_value > 1),
                "occurrence_count": int(len(group)),
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
    rows = _collect_all_cable_type_rows(plz, real_grid_dir=real_grid_dir, version_id=version_id)

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
            )    
    )

    total_length_by_source = cable_df.groupby("source")["total_length_km"].sum()
    total_length_lines = [
        f"- **{source}:** {total_length_by_source.get(source, 0.0):.2f} km"
        for source in ["Synthetic", "Real"]
        if source in set(cable_df["source"])
    ]
    display(
        Markdown(
            "**Total length of the displayed cable types** (Basis for the percentage values in the histogram below):\n\n"
            + "\n".join(total_length_lines)
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
        ]
        .rename(columns={"std_type": "Cable Type"})
        .round(4)
    )

    source_order = [source for source in ["Synthetic", "Real"] if source in set(cable_df["source"])]

    fig = px.histogram(
        cable_df,
        x="impedance_ohm_per_km",
        y="total_length_km",
        histfunc="sum",
        histnorm="percent",
        color="source",
        barmode="overlay",
        opacity=0.6,
        nbins=30,
        category_orders={"source": source_order},
        color_discrete_map={"Synthetic": "steelblue", "Real": "crimson"},
        title=f"Cable Impedance Distribution — Share of Installed Length (> {min_segment_count:.0f} weighted line segments)",
        labels={
            "impedance_ohm_per_km": "Impedance (Ohm/km)",
            "source": "Source",
        },
    )
    # histnorm="percent" normalizes each source's own bars to its own 100% (one trace per
    # color/source), so Synthetic and Real are compared by shape/share, not by raw volume --
    # the absolute total_length_km per source is shown separately above instead.
    fig.update_yaxes(title_text="Share of Installed Length (%)", ticksuffix="%")
    fig.update_layout(template="plotly_white", width=900, height=500, bargap=0.05)
    fig.show()

    parallel_share_df = compute_parallel_cable_share(
        plz, real_grid_dir=real_grid_dir, version_id=version_id
    )
    parallel_lines = []
    for source in ["Synthetic", "Real"]:
        row = parallel_share_df[parallel_share_df["source"] == source]
        if row.empty or pd.isna(row["parallel_share_pct"].iloc[0]):
            parallel_lines.append(f"- **{source}:** no data")
        else:
            parallel_lines.append(
                f"- **{source}:** {row['parallel_share_pct'].iloc[0]:.2f}% of material length "
                f"({row['parallel_length_km'].iloc[0]:.3f} km)"
            )

    display(
        Markdown(
            "\n\n" + "\n".join(parallel_lines)
        )
    )

    parallel_breakdown_df = compute_parallel_cable_breakdown(
        plz, real_grid_dir=real_grid_dir, version_id=version_id
    )
    if parallel_breakdown_df.empty:
        display(Markdown("No parallel-laid cables found."))
    else:
        display(
            parallel_breakdown_df.rename(
                columns={
                    "std_type": "Cable Type",
                    "parallel": "parallel (count)",
                    "occurrence_count": "Occurrences",
                    "total_length_km": "Material Length (km)",
                }
            ).round(4)
        )
    return fig


def build_wasserstein_bar_figure(wasserstein_table: pd.DataFrame) -> px.bar:
    ordered = wasserstein_table.sort_values("normalized_wasserstein", ascending=False)
    fig = px.bar(
        ordered,
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
    _add_per_metric_threshold_markers(fig, ordered["metric"].tolist())
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


def load_scenario_metrics(
    scenarios: dict[str, str],
    *,
    metrics_dir: Path | None = None,
    real_metrics_filename: str | Path | None = None,
    dataset_col: str = "dataset",
    include_real: bool = True,
) -> pd.DataFrame:
    """Load several synthetic metric CSVs (e.g. one per generator parameter variant), tagged by `dataset_col`.

    Each key in `scenarios` becomes a value in `dataset_col`; the real reference (if
    `include_real`) is tagged as "Real". Filenames are resolved the same way as the
    single-scenario loaders, so relative names are looked up under `metrics_dir`.
    """
    if not scenarios:
        raise ValueError("At least one scenario is required.")

    frames: list[pd.DataFrame] = []
    first_real_filename = real_metrics_filename

    for scenario_label, synthetic_filename in scenarios.items():
        df_synth, synthetic_path = _load_synthetic_metrics(
            metrics_dir,
            metrics_filename=synthetic_filename,
        )
        scenario_frame = df_synth.copy()
        scenario_frame[dataset_col] = scenario_label
        frames.append(scenario_frame)

        if include_real and first_real_filename is None:
            synthetic_name = Path(synthetic_path).name
            first_real_filename = synthetic_name.replace("synthetic_grid_metrics", "real_grid_metrics", 1)

    if include_real:
        if first_real_filename is None:
            raise ValueError("Could not infer real metrics filename.")
        df_real, _ = _load_real_metrics(metrics_dir, metrics_filename=first_real_filename)
        real_frame = df_real.copy()
        real_frame[dataset_col] = "Real"
        frames.append(real_frame)

    return pd.concat(frames, ignore_index=True, sort=False)


def compare_scenarios_to_real(
    scenarios: dict[str, str],
    metrics: list[str],
    *,
    metrics_dir: Path | None = None,
    real_metrics_filename: str | Path | None = None,
    dataset_col: str = "dataset",
) -> pd.DataFrame:
    """Per-scenario Wasserstein distance against the real reference, one row per (scenario, metric).

    Useful to rank several generator parameter variants by how close each one gets
    to the real grid population, metric by metric.
    """
    rows: list[pd.DataFrame] = []
    for scenario_label, synthetic_filename in scenarios.items():
        combined = load_scenario_metrics(
            {scenario_label: synthetic_filename},
            metrics_dir=metrics_dir,
            real_metrics_filename=real_metrics_filename,
            dataset_col=dataset_col,
        )
        table = compute_wasserstein_summary(
            combined,
            metrics,
            source_col=dataset_col,
            synthetic_label=scenario_label,
            real_label="Real",
        )
        if table.empty:
            continue
        table.insert(0, dataset_col, scenario_label)
        rows.append(table)

    if not rows:
        return pd.DataFrame(
            columns=[
                dataset_col,
                "metric",
                "synthetic_n",
                "real_n",
                "wasserstein_distance",
                "pooled_iqr",
                "normalized_wasserstein",
                "quality",
            ]
        )
    return pd.concat(rows, ignore_index=True)


def _add_per_metric_threshold_markers(fig, metrics: list[str]) -> None:
    """Overlay excellent/good/acceptable threshold ticks at each metric's x position, instead
    of a single flat horizontal line across the whole chart. Thresholds are calibrated per
    metric (see CALIBRATED_WASSERSTEIN_THRESHOLDS in validations/grid_comparison/scoring.py) -- a global
    reference line would only be correct for whichever metric happens to match it and
    misleading for the rest."""
    for label, level_index, color in [
        ("excellent", 0, "seagreen"),
        ("good", 1, "royalblue"),
        ("acceptable", 2, "darkorange"),
    ]:
        y_values = [get_wasserstein_thresholds(metric)[level_index] for metric in metrics]
        fig.add_trace(
            go.Scatter(
                x=metrics,
                y=y_values,
                mode="markers",
                marker=dict(symbol="line-ew", size=28, line=dict(color=color, width=3)),
                name=f"{label} threshold",
                hovertemplate=f"{label}: " + "%{y:.4f}<extra></extra>",
            )
        )


def build_scenario_wasserstein_bar_figure(
    scenario_wasserstein_table: pd.DataFrame,
    *,
    dataset_col: str = "dataset",
) -> px.bar:
    fig = px.bar(
        scenario_wasserstein_table,
        x="metric",
        y="normalized_wasserstein",
        color=dataset_col,
        barmode="group",
        title="Normalized Wasserstein Distance vs. Real, by Scenario",
        labels={
            "metric": "Metric",
            "normalized_wasserstein": "Normalized Wasserstein Distance",
            dataset_col: "Scenario",
        },
    )
    metrics = list(dict.fromkeys(scenario_wasserstein_table["metric"]))
    _add_per_metric_threshold_markers(fig, metrics)
    fig.update_layout(template="plotly_white", height=500)
    return fig


def _collect_all_cable_type_rows(
    plz: int,
    *,
    real_grid_dir: Path | str | None = None,
    version_id: str = VERSION_ID,
) -> list[dict[str, object]]:
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

    return rows


def compute_parallel_cable_share(
    plz: int,
    *,
    real_grid_dir: Path | str | None = None,
    version_id: str = VERSION_ID,
) -> pd.DataFrame:
    """Share of installed cable length that needed a parallel run (capacity limit), per source.

    Computed from the full, unfiltered cable inventory rather than the
    ``min_segment_count``-filtered table: parallel runs are rare by construction, and that
    same threshold (tuned to drop noisy one-off cable types from the per-type comparison)
    would otherwise wipe out the very signal this metric is meant to show.
    """
    rows = _collect_all_cable_type_rows(plz, real_grid_dir=real_grid_dir, version_id=version_id)
    if not rows:
        return pd.DataFrame(columns=["source", "total_length_km", "parallel_length_km", "parallel_share_pct"])

    df = pd.DataFrame(rows)
    summary = df.groupby("source", as_index=True).agg(total_length_km=("total_length_km", "sum"))
    parallel_length = df[df["is_parallel"]].groupby("source")["total_length_km"].sum()
    summary["parallel_length_km"] = parallel_length.reindex(summary.index).fillna(0.0)
    summary["parallel_share_pct"] = np.where(
        summary["total_length_km"] > 0,
        100.0 * summary["parallel_length_km"] / summary["total_length_km"],
        np.nan,
    )
    return summary.reset_index()


def compute_parallel_cable_breakdown(
    plz: int,
    *,
    real_grid_dir: Path | str | None = None,
    version_id: str = VERSION_ID,
) -> pd.DataFrame:
    """Which cable types are laid in parallel, at what parallel count, and how often.

    Unfiltered (no ``min_segment_count``) for the same reason as ``compute_parallel_cable_share``:
    parallel runs are rare by construction and would otherwise be dropped by a threshold tuned
    for the main per-cable-type comparison.
    """
    rows = _collect_all_cable_type_rows(plz, real_grid_dir=real_grid_dir, version_id=version_id)
    if not rows:
        return pd.DataFrame(
            columns=["source", "std_type", "parallel", "occurrence_count", "total_length_km"]
        )

    df = pd.DataFrame(rows)
    df = df[df["is_parallel"]]
    if df.empty:
        return pd.DataFrame(
            columns=["source", "std_type", "parallel", "occurrence_count", "total_length_km"]
        )

    breakdown = (
        df.groupby(["source", "std_type", "parallel"], as_index=False)
        .agg(
            occurrence_count=("occurrence_count", "sum"),
            total_length_km=("total_length_km", "sum"),
        )
        .sort_values("occurrence_count", ascending=False)
        .reset_index(drop=True)
    )
    return breakdown


def plot_correlation_matrix_comparison(
    df: pd.DataFrame,
    metric_cols: list[str],
    *,
    labels: dict[str, str] | None = None,
    source_col: str = "source",
    synthetic_label: str = "Synthetic",
    real_label: str = "Real",
    method: str = "spearman",
    show_diff: bool = True,
    title: str = "Correlation Matrix Comparison: Synthetic vs. Real",
    figsize: tuple[float, float] | None = None,
    annot_fontsize: int = 8,
) -> plt.Figure:
    active_labels = labels or {}
    short_names = [active_labels.get(m, m).split("(")[0].strip() for m in metric_cols]

    def _corr(source_name: str) -> pd.DataFrame:
        sub = df[df[source_col] == source_name][metric_cols].copy()
        for col in metric_cols:
            sub[col] = pd.to_numeric(sub[col], errors="coerce")
        sub = sub.dropna()
        if len(sub) < 3:
            return pd.DataFrame(np.nan, index=short_names, columns=short_names)
        corr = sub.corr(method=method)
        corr.index = short_names
        corr.columns = short_names
        return corr

    corr_synth = _corr(synthetic_label)
    corr_real = _corr(real_label)
    corr_diff = corr_synth - corr_real

    # vertical layout: 2 main panels side-by-side on top, diff below full-width
    if show_diff:
        fig = plt.figure(figsize=figsize or (11, 10))
        ax0 = fig.add_subplot(2, 2, 1)
        ax1 = fig.add_subplot(2, 2, 2)
        ax2 = fig.add_subplot(2, 1, 2)
        panel_axes = [ax0, ax1, ax2]
    else:
        fig, panel_axes = plt.subplots(1, 2, figsize=figsize or (11, 4.5))

    common_kws = dict(
        annot=True,
        fmt=".2f",
        linewidths=0.5,
        annot_kws={"size": annot_fontsize},
        square=False,
        cbar_kws={"shrink": 0.8},
    )

    mask = np.triu(np.ones_like(corr_synth, dtype=bool), k=1)

    sns.heatmap(corr_synth, ax=panel_axes[0], vmin=-1, vmax=1,
                cmap="RdBu_r", mask=mask, **common_kws)
    panel_axes[0].set_title(f"{synthetic_label}  ({method.title()} r)", fontsize=10, fontweight="bold")

    sns.heatmap(corr_real, ax=panel_axes[1], vmin=-1, vmax=1,
                cmap="RdBu_r", mask=mask, **common_kws)
    panel_axes[1].set_title(f"{real_label}  ({method.title()} r)", fontsize=10, fontweight="bold")

    if show_diff:
        diff_abs_max = max(float(corr_diff.abs().max().max()), 0.01)
        sns.heatmap(corr_diff, ax=panel_axes[2], vmin=-diff_abs_max, vmax=diff_abs_max,
                    cmap="PuOr", mask=mask, **common_kws)
        panel_axes[2].set_title(
            f"Difference  ({synthetic_label} − {real_label})  ·  purple = synth higher  |  orange = real higher",
            fontsize=9, fontweight="bold",
        )

    fig.suptitle(title, fontsize=12, fontweight="bold", y=1.01)
    fig.tight_layout()
    plt.show()
    return fig


__all__ = [
    "ComparisonNotebookData",
    "DEFAULT_LABELS",
    "DEFAULT_METRICS",
    "default_metric_filenames",
    "selected_metric_filenames",
    "load_scenario_metrics",
    "compare_scenarios_to_real",
    "build_scenario_wasserstein_bar_figure",
    "compute_parallel_cable_share",
    "compute_parallel_cable_breakdown",
    "plot_correlation_matrix_comparison",
    "build_wasserstein_bar_figure",
    "compute_status_diagnostics",
    "load_and_render_overview",
    "load_cable_type_comparison",
    "load_notebook_data",
    "plot_boxplot_overview",
    "plot_metric_kde_diagonal",
    "plot_scenario_kde_diagonal",
    "render_top_overview",
    "resolve_metrics_path",
    "show_cable_type_comparison",
    "show_distribution_selector",
    "show_wasserstein_summary",
]
