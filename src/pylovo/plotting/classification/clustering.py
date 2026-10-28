"""
Clustering analysis plotting functions.
This module contains functions for visualizing clustering results and quality metrics,
including CH/DB indices, cluster distributions, the representative grids and 3D visualizations.

Cluster ``k`` has the colour ``TUMPalette[k]`` (cycled) in every figure. The matplotlib functions return their
figure (the index functions their table) and never call ``plt.show()``: in a notebook assign the figure
(``fig = ...``), so that it is shown once.
"""
import json
from math import ceil, pi
from typing import Sequence, Tuple
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from pyproj import CRS, Transformer
from sklearn.cluster import KMeans
from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score
from sklearn.mixture import GaussianMixture

from pylovo.classification.clustering.clustering_algorithms import scaled_key_figures
from pylovo.config_loader import TUMPalette, LIST_OF_CLUSTERING_PARAMETERS, REGIO7_REGIO5_GEM_DICT

# Legend labels of the algorithms of the CH and DB index plots, in the order of the algorithms.
_ALGORITHM_LABELS = ['k-means', 'GMM full', 'GMM diagonal', 'GMM tied', 'GMM spherical']


def cluster_color(cluster) -> str:
    """Return the colour of a cluster: ``TUMPalette`` in the order of the cluster numbers, cycled."""
    return TUMPalette[int(cluster) % len(TUMPalette)]


def _wrap_name(name: str, width: int = 16) -> str:
    """Break a parameter name after its underscores into lines of at most ``width`` characters (if possible)."""
    parts = name.split("_")
    tokens = [part + "_" for part in parts[:-1]] + [parts[-1]]
    lines = [""]
    for token in tokens:
        if lines[-1] and len(lines[-1]) + len(token) > width:
            lines.append("")
        lines[-1] += token
    return "\n".join(lines)


def plot_radar_graph(
    representative_networks: pd.DataFrame,
    list_of_parameters: list,
    figsize: Tuple[int, int] = (10, 10)
) -> Figure:
    """
    Plot representative networks as radar graphs.

    One polar panel per representative grid (three per row), titled with its cluster; each parameter is
    normalized by its maximum over all representative grids.

    Args:
        representative_networks: Table of parameters of representative networks
            with a ``clusters`` column.
        list_of_parameters: Parameters to plot in radar graph.
        figsize: Figure size in inches (width, height). Default: (10, 10).

    Returns:
        matplotlib.figure.Figure: The Figure object containing the radar plots.
    """
    # Prepare data
    representative_networks = representative_networks.reset_index()
    list_of_parameters_copy = list_of_parameters.copy()
    list_of_parameters_copy.append('clusters')
    representative_networks = representative_networks[list_of_parameters_copy]

    # Normalize values
    max_values = representative_networks.max()
    representative_networks_normalized = representative_networks.copy()
    for column in representative_networks:
        representative_networks_normalized[column] = representative_networks[column] / max_values[column]

    # Move clusters column to first position
    first_column = representative_networks_normalized.pop('clusters')
    representative_networks_normalized.insert(0, 'clusters', first_column)

    def make_spider(row, title, color, ax):
        """Create a single radar plot."""
        categories = list(representative_networks_normalized)[1:]
        N = len(categories)
        angles = [n / float(N) * 2 * pi for n in range(N)]
        angles += angles[:1]

        ax.set_theta_offset(pi / 2)
        ax.set_theta_direction(-1)
        ax.set_xticks(angles[:-1], [_wrap_name(category) for category in categories], color='grey', size=8)

        ax.set_rlabel_position(0)
        ax.set_yticks([0.33, 0.66, 1.0], ["33%", "66%", "100%"], color="grey", size=7)
        ax.set_ylim(0, 1.1)

        values = representative_networks_normalized.loc[row].drop('clusters').values.flatten().tolist()
        values += values[:1]
        ax.plot(angles, values, color=color, linewidth=2, linestyle='solid')
        ax.fill(angles, values, color=color, alpha=0.4)
        ax.set_title(title, size=11, color=color, y=1.25)

    # Create figure and subplots: three panels per row, at least two rows (the
    # former fixed 2 x 3 grid failed for 7 clusters, see NO_OF_CLUSTERS_ALLOWED)
    n_plots = len(representative_networks.index)
    n_rows = max(2, ceil(n_plots / 3))
    fig = plt.figure(figsize=figsize, dpi=96)

    for row in range(0, n_plots):
        cluster = int(representative_networks.loc[row, 'clusters'])
        ax = plt.subplot(n_rows, 3, row + 1, polar=True)
        make_spider(row=row, title=f"Cluster {cluster}", color=cluster_color(cluster), ax=ax)
    # room for the parameter names at the sides of neighbouring panels
    fig.subplots_adjust(wspace=0.7, hspace=0.65)

    return fig


def _clustering_columns(list_of_clustering_parameters: Sequence[str] | None) -> list[str]:
    """Return the given clustering columns or, if ``None``, the configured ones."""
    if list_of_clustering_parameters is None:
        return list(LIST_OF_CLUSTERING_PARAMETERS)
    return list(list_of_clustering_parameters)


def _plot_index_lines(df_index: pd.DataFrame, ax: plt.Axes | None, ylabel: str, legend: bool) -> None:
    """Draw one line per algorithm of an index table (``no_clusters`` and one column per algorithm)."""
    if ax is None:
        _, ax = plt.subplots(figsize=(7, 4.5))
    sns.lineplot(data=pd.melt(df_index, ['no_clusters']), y='value', x='no_clusters', hue='variable',
                 palette=TUMPalette[:len(_ALGORITHM_LABELS)], ax=ax)
    handles, _ = ax.get_legend_handles_labels()
    ax.legend(handles, _ALGORITHM_LABELS, title='Clustering algorithm')
    ax.set(xlabel='Number of clusters', ylabel=ylabel)
    ax.set_xticks(list(df_index['no_clusters']))
    if legend:
        sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))
    else:
        ax.get_legend().remove()


def plot_ch_index_for_clustering_algos(
    df_plz_parameters: pd.DataFrame,
    no_of_clusters_allowed: Sequence[int] = range(3, 8),
    list_of_clustering_parameters: Sequence[str] | None = None,
    ax: plt.Axes | None = None,
    legend: bool = True,
    transform: str | None = None,
) -> pd.DataFrame:
    """
    Plot Calinski-Harabasz index for different clustering algorithms.

    Args:
        df_plz_parameters: Set of parameters for grids.
        no_of_clusters_allowed: Cluster numbers to test. Default: range(3, 8).
        list_of_clustering_parameters: Columns used for clustering. Defaults to
            ``LIST_OF_CLUSTERING_PARAMETERS`` from ``config_clustering.yaml``.
        ax: Axes to draw into. If None, creates a new figure.
        legend: Draw the legend of the algorithms (right of the axes).
        transform: Transform of the key figures before the standardisation; ``None`` uses ``TRANSFORM`` of
            ``config_clustering.yaml``.

    Returns:
        pd.DataFrame: Comparison table of optimal cluster numbers for each algorithm.
    """
    X = scaled_key_figures(df_plz_parameters, _clustering_columns(list_of_clustering_parameters), transform)

    df_ch_comparison = pd.DataFrame(columns=['algorithm', 'no_clusters', 'ch_index'])
    df_ch_index = pd.DataFrame({'no_clusters': list(no_of_clusters_allowed)})

    # Test different algorithms
    algorithms = [
        ('kmeans', lambda n: KMeans(n_clusters=n, random_state=0)),
        ('gmm_full', lambda n: GaussianMixture(n_components=n, covariance_type='full', random_state=1)),
        ('gmm_diag', lambda n: GaussianMixture(n_components=n, covariance_type='diag', random_state=1)),
        ('gmm_tied', lambda n: GaussianMixture(n_components=n, covariance_type='tied', random_state=1)),
        ('gmm_sph', lambda n: GaussianMixture(n_components=n, covariance_type='spherical', random_state=1))
    ]

    for algo_name, algo_func in algorithms:
        ch_list = []
        for n_clusters in no_of_clusters_allowed:
            model = algo_func(n_clusters).fit(X)
            labels = model.labels_ if hasattr(model, 'labels_') else model.predict(X)
            ch_index = calinski_harabasz_score(X, labels)
            ch_list.append(ch_index)

        df_ch_index[f'ch_index_{algo_name}'] = ch_list
        idxmax = df_ch_index[f'ch_index_{algo_name}'].idxmax()
        opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
        ch_index_opt = df_ch_index[f'ch_index_{algo_name}'].max()
        df_ch_comparison.loc[len(df_ch_comparison)] = [algo_name, opt_no_clusters, ch_index_opt]

    # Plot results
    _plot_index_lines(df_ch_index, ax, 'Calinski-Harabasz index (higher is better)', legend)

    return df_ch_comparison


def plot_db_index_for_clustering_algos(
    df_plz_parameters: pd.DataFrame,
    no_of_clusters_allowed: Sequence[int] = range(3, 8),
    list_of_clustering_parameters: Sequence[str] | None = None,
    ax: plt.Axes | None = None,
    legend: bool = True,
    transform: str | None = None,
) -> pd.DataFrame:
    """
    Plot Davies-Bouldin index for different clustering algorithms.

    Args:
        df_plz_parameters: Set of parameters for grids.
        no_of_clusters_allowed: Cluster numbers to test. Default: range(3, 8).
        list_of_clustering_parameters: Columns used for clustering. Defaults to
            ``LIST_OF_CLUSTERING_PARAMETERS`` from ``config_clustering.yaml``.
        ax: Axes to draw into. If None, creates a new figure.
        legend: Draw the legend of the algorithms (right of the axes).
        transform: Transform of the key figures before the standardisation; ``None`` uses ``TRANSFORM`` of
            ``config_clustering.yaml``.

    Returns:
        pd.DataFrame: Comparison table of optimal cluster numbers for each algorithm.
    """
    X = scaled_key_figures(df_plz_parameters, _clustering_columns(list_of_clustering_parameters), transform)

    df_db_comparison = pd.DataFrame(columns=['algorithm', 'no_clusters', 'db_index'])
    df_db_index = pd.DataFrame({'no_clusters': list(no_of_clusters_allowed)})

    # same seeds as plot_ch_index_for_clustering_algos, so the results are reproducible
    algorithms = [
        ('kmeans', lambda n: KMeans(n_clusters=n, random_state=0)),
        ('gmm_full', lambda n: GaussianMixture(n_components=n, covariance_type='full', random_state=1)),
        ('gmm_diag', lambda n: GaussianMixture(n_components=n, covariance_type='diag', random_state=1)),
        ('gmm_tied', lambda n: GaussianMixture(n_components=n, covariance_type='tied', random_state=1)),
        ('gmm_sph', lambda n: GaussianMixture(n_components=n, covariance_type='spherical', random_state=1))
    ]

    for algo_name, algo_func in algorithms:
        db_list = []
        for n_clusters in no_of_clusters_allowed:
            model = algo_func(n_clusters).fit(X)
            labels = model.labels_ if hasattr(model, 'labels_') else model.predict(X)
            db_index = davies_bouldin_score(X, labels)
            db_list.append(db_index)

        df_db_index[f'db_index_{algo_name}'] = db_list
        idxmin = df_db_index[f'db_index_{algo_name}'].idxmin()
        opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
        db_index_opt = df_db_index[f'db_index_{algo_name}'].min()
        df_db_comparison.loc[len(df_db_comparison)] = [algo_name, opt_no_clusters, db_index_opt]

    # Plot results
    _plot_index_lines(df_db_index, ax, 'Davies-Bouldin index (lower is better)', legend)

    return df_db_comparison


def plot_percentage_of_clusters(df_plz_parameters: pd.DataFrame, ax: plt.Axes | None = None) -> Figure:
    """
    Plot distribution of clusters as a bar chart.

    Args:
        df_plz_parameters: Set of parameters for clustered grids with 'clusters' column.
        ax: Axes to draw into. If None, creates a new figure.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the cluster distribution plot.
    """
    len_grids = len(df_plz_parameters)
    clusters_perc = df_plz_parameters['clusters'].value_counts() / len_grids
    clusters_perc = pd.DataFrame(clusters_perc)
    clusters_perc['count'] = clusters_perc['count'] * 100
    clusters_perc = clusters_perc.sort_index().reset_index()

    if ax is None:
        fig = plt.figure(figsize=(8, 6))
        ax = fig.gca()
    else:
        fig = ax.get_figure()
    sns.barplot(data=clusters_perc, y='count', x='clusters', ax=ax)
    ax.set_ylabel('Share of the grids (%)')
    ax.set_xlabel('Cluster')

    return fig


def plot_clusters_per_regiostar_class(
    df_plz_parameters: pd.DataFrame, level: str = "regio7", ax: plt.Axes | None = None
) -> Figure:
    """
    Plot the share of every cluster in the grids of each RegioStaR class (stacked bars).

    Args:
        df_plz_parameters: Clustered grids with ``clusters`` and ``regio7`` columns.
        level: ``"regio7"`` for the seven RegioStaR 7 classes, ``"regio5"`` for the five RegioStaR Gem 5 classes
            (mapped with ``REGIO7_REGIO5_GEM_DICT``).
        ax: Axes to draw into. If None, creates a new figure.

    Returns:
        matplotlib.figure.Figure: The figure with the stacked bars; every bar adds up to 1.
    """
    if level not in ("regio7", "regio5"):
        raise ValueError(f"level must be 'regio7' or 'regio5', got {level!r}")
    classes = df_plz_parameters['regio7']
    if level == "regio5":
        classes = classes.map(REGIO7_REGIO5_GEM_DICT)
    shares = pd.crosstab(classes.astype(int).rename('class'), df_plz_parameters['clusters'], normalize='index')

    if ax is None:
        fig, ax = plt.subplots(figsize=(7, 4.5))
    else:
        fig = ax.get_figure()
    shares.plot(kind='bar', stacked=True, ax=ax, color=[cluster_color(cluster) for cluster in shares.columns],
                width=0.8)
    ax.set_xlabel('RegioStaR 7 class' if level == "regio7" else 'RegioStaR Gem 5 class')
    ax.set_ylabel('Share of the grids of the class')
    ax.tick_params(axis='x', labelrotation=0)
    ax.legend(title='Cluster', loc='upper left', bbox_to_anchor=(1, 1))

    return fig


def plot_stacked_distribution_of_clusters_per_regio_5(
    df_plz_parameters: pd.DataFrame
) -> Figure:
    """
    Plot stacked bar chart of cluster distribution for each regio 5 class.

    The classes are the RegioStaR Gem 5 classes; :func:`plot_clusters_per_regiostar_class` also draws the seven
    RegioStaR 7 classes.

    Args:
        df_plz_parameters: Set of parameters for clustered grids with 'clusters' and 'regio7' columns.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the stacked distribution plot.
    """
    fig = plot_clusters_per_regiostar_class(df_plz_parameters, level="regio5")
    fig.axes[0].set_title('Clusters per RegioStaR Gem 5 class')
    return fig


def plot_bar_distribution_of_clusters_per_regio_5(df_plz_parameters: pd.DataFrame) -> Figure:
    """
    Plot bar chart of cluster distribution for each RegioStaR class.

    Despite the name, the bars are grouped by the RegioStaR 7 class (``regio7``), one bar per cluster.

    Args:
        df_plz_parameters: Set of parameters for clustered grids with 'clusters' and 'regio7' columns.

    Returns:
        matplotlib.figure.Figure: The figure of the seaborn ``catplot``.
    """
    x, y = 'regio7', 'clusters'
    grid = (df_plz_parameters
            .groupby(x)[y]
            .value_counts(normalize=True)
            .mul(100)
            .rename('percent')
            .reset_index()
            .pipe((sns.catplot, 'data'), x=x, y='percent', hue=y, kind='bar'))
    grid.set_axis_labels('RegioStaR 7 class', 'Share of the grids of the class (%)')
    if grid.legend is not None:
        grid.legend.set_title('Cluster')
    return grid.figure


def plot_clusters_in_parameter_space(
    df_parameters: pd.DataFrame,
    list_of_parameters: Sequence[str],
    representative_networks: pd.DataFrame | None = None,
    max_points: int | None = 4000,
    seed: int = 0,
    height: float = 2.4,
) -> Figure:
    """
    Plot the clustered grids in the space of the clustering parameters (corner pair plot).

    The panels below the diagonal show every pair of parameters, one point per grid in the colour of its cluster;
    the diagonal shows the distribution of each parameter per cluster. The representative grids are drawn as large
    stars with a black edge.

    Args:
        df_parameters: Clustered grids with the parameters and a ``clusters`` column.
        list_of_parameters: Parameters to plot, usually the clustering parameters.
        representative_networks: The representative grids (same columns). None draws no stars.
        max_points: Draw a random subset of this many grids (all clusters keep their share), so that the points
            stay readable and the image small; the title gives the number drawn. None draws every grid.
        seed: Seed of the random subset.
        height: Height of one panel in inches.

    Returns:
        matplotlib.figure.Figure: The figure of the pair plot.
    """
    columns = list(list_of_parameters)
    data = df_parameters[columns + ['clusters']].dropna()
    total = len(data)
    if max_points is not None and total > max_points:
        data = data.sample(max_points, random_state=seed)
    clusters = sorted(int(cluster) for cluster in df_parameters['clusters'].unique())
    data = data.assign(clusters=data['clusters'].astype(int))
    palette = {cluster: cluster_color(cluster) for cluster in clusters}

    grid = sns.pairplot(data, vars=columns, hue='clusters', hue_order=clusters, palette=palette, corner=True,
                        height=height, plot_kws={'s': 6, 'alpha': 0.5, 'linewidth': 0},
                        diag_kws={'common_norm': False})
    if grid.legend is not None:
        grid.legend.set_title('Cluster')
        for handle in grid.legend.legend_handles:
            handle.set_alpha(1)
            if hasattr(handle, 'set_sizes'):
                handle.set_sizes([40])
    if representative_networks is not None and len(representative_networks):
        star_colors = [cluster_color(cluster) for cluster in representative_networks['clusters']]
        for i, y_column in enumerate(columns):
            for j, x_column in enumerate(columns[:i]):
                grid.axes[i, j].scatter(representative_networks[x_column], representative_networks[y_column],
                                        marker='*', s=260, c=star_colors, edgecolors='black', linewidths=1,
                                        zorder=5)
    shown = f"{len(data)} of {total} grids" if len(data) < total else f"{total} grids"
    stars = ", stars: representative grids" if representative_networks is not None else ""
    grid.figure.suptitle(f"{shown}{stars}", y=1.01)

    return grid.figure


def _local_projection(lon: float, lat: float) -> Transformer:
    """Return a transformer from WGS84 to metres east and north of the point (azimuthal equidistant)."""
    crs = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True)


def _geojson_coordinates(geo) -> list | None:
    """Return the coordinates of a GeoJSON text (pandapower ``geo`` column), or None if there is none."""
    if not isinstance(geo, str) or not geo.strip():
        return None
    return json.loads(geo).get('coordinates')


def _grid_geometry(net) -> tuple[list[tuple[np.ndarray, bool]], np.ndarray, np.ndarray]:
    """Return the lines (WGS84 vertices, ``True`` for a service cable), the consumers and the station of a net.

    A line is a service cable if one of its buses carries a load. The station is the low-voltage bus of the first
    transformer, or the first bus with geodata if the net has no transformer.
    """
    bus_geo = net.bus['geo'] if 'geo' in net.bus.columns else pd.Series(dtype=object)
    bus_xy = {bus: coordinates[:2] for bus, geo in bus_geo.items()
              if (coordinates := _geojson_coordinates(geo)) is not None}
    load_buses = set(net.load['bus']) if len(net.load) else set()

    lines = []
    for _, line in net.line.iterrows():
        coordinates = _geojson_coordinates(line['geo']) if 'geo' in net.line.columns else None
        if coordinates is None and line['from_bus'] in bus_xy and line['to_bus'] in bus_xy:
            coordinates = [bus_xy[line['from_bus']], bus_xy[line['to_bus']]]
        if coordinates:
            service = line['from_bus'] in load_buses or line['to_bus'] in load_buses
            lines.append((np.asarray(coordinates, dtype=float)[:, :2], service))
    consumers = np.asarray([bus_xy[bus] for bus in sorted(load_buses) if bus in bus_xy], dtype=float).reshape(-1, 2)
    station_bus = net.trafo['lv_bus'].iloc[0] if len(net.trafo) and net.trafo['lv_bus'].iloc[0] in bus_xy else None
    if station_bus is None:
        if not bus_xy:
            raise ValueError("The net has no bus with geodata")
        station_bus = next(iter(bus_xy))
    return lines, consumers, np.asarray(bus_xy[station_bus], dtype=float)


def plot_representative_grids(
    nets: Sequence,
    titles: Sequence[str] | None = None,
    colors: Sequence[str] | None = None,
    ncols: int = 3,
    panel_size: float = 3.8,
    same_scale: bool = False,
) -> Figure:
    """
    Draw grids side by side from the geodata of their pandapower nets (small multiples, no basemap).

    Every panel shows one grid in metres east and north of its transformer: the feeder cables, the service cables to
    the consumers (thinner), the consumers (dots) and the transformer (triangle). No tiles are loaded, so the figure
    works offline.

    Args:
        nets: pandapower nets of pylovo grids (``geo`` columns of ``bus`` and ``line`` as GeoJSON), e.g. read with
            ``DatabaseClient.read_net_db``.
        titles: One title per net. None numbers the panels.
        colors: One cable colour per net. None takes ``TUMPalette`` in order, the colours of clusters 0, 1, ...
        ncols: Number of panels per row.
        panel_size: Width and height of one panel in inches.
        same_scale: Give every panel the extent of the largest grid, so that the sizes can be compared; otherwise
            every panel is zoomed to its grid.

    Returns:
        matplotlib.figure.Figure: The figure with one panel per net.
    """
    nets = list(nets)
    if not nets:
        raise ValueError("No net to plot")
    titles = list(titles) if titles is not None else [f"Grid {i}" for i in range(len(nets))]
    colors = list(colors) if colors is not None else [cluster_color(i) for i in range(len(nets))]
    nrows = ceil(len(nets) / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(panel_size * ncols, panel_size * nrows + 0.4), squeeze=False)

    projected = []
    for net in nets:
        lines, consumers, station = _grid_geometry(net)
        to_metres = _local_projection(*station)
        lines = [(np.column_stack(to_metres.transform(xy[:, 0], xy[:, 1])), service) for xy, service in lines]
        consumers = np.column_stack(to_metres.transform(consumers[:, 0], consumers[:, 1])) if len(consumers) \
            else consumers
        points = np.vstack([xy for xy, _ in lines] + [consumers, np.zeros((1, 2))])
        projected.append((lines, consumers, points.min(axis=0), points.max(axis=0)))
    half_extent = max(float((upper - lower).max()) for _, _, lower, upper in projected) / 2 * 1.08

    for ax, (lines, consumers, lower, upper), title, color in zip(axes.flat, projected, titles, colors):
        for xy, service in lines:
            ax.plot(xy[:, 0], xy[:, 1], color=color, linewidth=0.6 if service else 1.6, alpha=0.8 if service else 1,
                    solid_capstyle='round', zorder=2)
        if len(consumers):
            ax.scatter(consumers[:, 0], consumers[:, 1], s=5, color='#333333', linewidths=0, zorder=3)
        ax.scatter([0], [0], marker='^', s=90, color='black', edgecolors='white', linewidths=0.8, zorder=4)
        if same_scale:
            center = (lower + upper) / 2
            ax.set_xlim(center[0] - half_extent, center[0] + half_extent)
            ax.set_ylim(center[1] - half_extent, center[1] + half_extent)
            ax.set_aspect('equal', adjustable='box')
        else:
            ax.set_aspect('equal', adjustable='datalim')
        ax.set_title(title, fontsize=10)
        ax.tick_params(labelsize=7)
        ax.grid(color='#e6e6e6', linewidth=0.5)
        ax.set_axisbelow(True)
    for ax in axes.flat[len(nets):]:
        ax.set_visible(False)
    for ax in axes[:, 0]:
        ax.set_ylabel('m north of the transformer', fontsize=8)
    for ax in axes[-1, :]:
        ax.set_xlabel('m east of the transformer', fontsize=8)

    handles = [Line2D([], [], color='#555555', linewidth=1.6, label='Feeder cable'),
               Line2D([], [], color='#555555', linewidth=0.6, label='Service cable'),
               Line2D([], [], marker='o', linestyle='', markersize=3, color='#333333', label='Consumer'),
               Line2D([], [], marker='^', linestyle='', markersize=8, color='black', label='Transformer')]
    fig.legend(handles=handles, loc='lower center', ncol=4, frameon=False, fontsize=9)
    fig.tight_layout(rect=(0, 0.4 / (panel_size * nrows + 0.4), 1, 1))

    return fig


def get_min_max_data_for_clusters(n_clusters: int, df_networks: pd.DataFrame) -> pd.DataFrame:
    """
    Get minimum and maximum values for each cluster and parameter.

    Args:
        n_clusters: Number of clusters.
        df_networks: Network parameters with 'clusters' column.

    Returns:
        pd.DataFrame: Min/max per cluster of attribute 1 ``no_house_connections``,
        attribute 2 ``cable_len_per_house`` and attribute 3 ``transformer_mva``.
    """
    df_min_max = pd.DataFrame(
        columns=['attribute1_min', 'attribute1_max', 'attribute2_min', 'attribute2_max',
                 'attribute3_min', 'attribute3_max'])

    for n in range(0, n_clusters):
        networks_cluster_n = df_networks.groupby('clusters').get_group(n)
        df_min_max.at[n, 'attribute1_min'] = networks_cluster_n['no_house_connections'].min()
        df_min_max.at[n, 'attribute1_max'] = networks_cluster_n['no_house_connections'].max()
        df_min_max.at[n, 'attribute2_min'] = networks_cluster_n['cable_len_per_house'].min()
        df_min_max.at[n, 'attribute2_max'] = networks_cluster_n['cable_len_per_house'].max()
        df_min_max.at[n, 'attribute3_min'] = networks_cluster_n['transformer_mva'].min()
        df_min_max.at[n, 'attribute3_max'] = networks_cluster_n['transformer_mva'].max()

    return df_min_max


def plot_clusters_3D(df_min_max: pd.DataFrame, df_centroids: pd.DataFrame) -> Figure:
    """
    Plot clusters as 3D boxes with centroids.

    Args:
        df_min_max: Min/max values per cluster from :func:`get_min_max_data_for_clusters`.
        df_centroids: Cluster centroids with columns ``0``, ``1``, ``2`` in the attribute
            order of ``df_min_max``; an optional ``color`` column in ``df_min_max`` sets
            the box colors.

    Returns:
        matplotlib.figure.Figure: The Figure object containing the 3D cluster plot.
    """
    fig = plt.figure(figsize=(10, 10))
    ax = fig.add_subplot(111, projection='3d')

    for _index, row in df_min_max.iterrows():
        Z = np.array([[row['attribute1_min'], row['attribute2_min'], row['attribute3_min']],
                      [row['attribute1_max'], row['attribute2_min'], row['attribute3_min']],
                      [row['attribute1_max'], row['attribute2_max'], row['attribute3_min']],
                      [row['attribute1_min'], row['attribute2_max'], row['attribute3_min']],
                      [row['attribute1_min'], row['attribute2_min'], row['attribute3_max']],
                      [row['attribute1_max'], row['attribute2_min'], row['attribute3_max']],
                      [row['attribute1_max'], row['attribute2_max'], row['attribute3_max']],
                      [row['attribute1_min'], row['attribute2_max'], row['attribute3_max']]])
        ax.scatter3D(Z[:, 0], Z[:, 1], Z[:, 2])
        verts = [[Z[0], Z[1], Z[2], Z[3]],
                 [Z[4], Z[5], Z[6], Z[7]],
                 [Z[0], Z[1], Z[5], Z[4]],
                 [Z[2], Z[3], Z[7], Z[6]],
                 [Z[1], Z[2], Z[6], Z[5]],
                 [Z[4], Z[7], Z[3], Z[0]]]
        color = row.get('color', 'blue')
        ax.add_collection3d(Poly3DCollection(verts, facecolors=color, linewidths=1,
                                             edgecolors='r', alpha=.20))

    ax.scatter(df_centroids[0], df_centroids[1], df_centroids[2], color='black', marker='*')
    ax.set_xlabel('House connections')
    ax.set_ylabel('Cable length per house (km)')
    ax.set_zlabel('Transformer rating (MVA)')
    ax.zaxis.labelpad = -0.7

    return fig
