"""Shared helpers of pylovo.

- directories and logging: :func:`get_user_data_dir`, :func:`reset_log_directory`,
  :func:`create_logger`,
- electrical load aggregation used for transformer and cable sizing:
  :func:`build_load_components`, :func:`simultaneous_peak_load` (:class:`CoincidentLoads` for
  many node sets of the same buildings), :func:`category_simultaneous_load`,
  :func:`allocate_consumer_simultaneous_loads`,
  :func:`planning_nodes`, :func:`design_current_ka`,
- OpenStreetMap downloads through the Overpass API: :func:`query_overpass_for_geojson`.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import osm2geojson
import pandas as pd
import requests

from pylovo.config_loader import DEFAULT_POWER_FACTOR, VN

# Log timestamps are written in German local time (CET/CEST).
LOG_TIMEZONE = ZoneInfo("Europe/Berlin")

# OpenStreetMap services require an identifying User-Agent: overpass-api.de answers the default
# python-requests agent with HTTP 406, the OSM tile servers send "Access blocked" tiles.
PYLOVO_USER_AGENT = "pylovo (https://github.com/tum-ens/pylovo)"
# (connect, read) timeout in seconds. The read timeout is longer than the
# server-side [timeout:500] of the queries in data/transformer_data/overpass_queries.
OVERPASS_TIMEOUT_S = (30, 600)


def get_user_data_dir() -> Path:
    """Return the directory with user-provided input data.

    The directory holds building shapefiles, street network SQL files and the
    processed transformer GeoJSON files of the file-based (``USE_INFDB=False``)
    data path. The first match wins:

    1. ``PYLOVO_DATA_DIR`` environment variable (explicit data directory),
    2. ``PYLOVO_ROOT`` environment variable + ``/data`` (Docker-friendly),
    3. ``<current working directory>/data`` (development checkout).

    Returns:
        Path of the user data directory (it is not checked for existence).
    """
    # Explicit data directory
    data_dir = os.getenv("PYLOVO_DATA_DIR")
    if data_dir:
        return Path(data_dir)

    # Project root + data (Docker-friendly)
    pylovo_root = os.getenv("PYLOVO_ROOT")
    if pylovo_root:
        return Path(pylovo_root) / "data"

    # Fallback to current working directory
    return Path.cwd() / "data"


def reset_log_directory() -> Path:
    """Empty ``./log`` (keeping ``.gitkeep``) and make sure it exists.

    Returns:
        Path of the log directory, relative to the current working directory.
    """
    log_dir = Path("log")
    if log_dir.exists():
        for item in log_dir.iterdir():
            if item.name != ".gitkeep":
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def create_logger(name: str, log_file, log_level) -> logging.Logger:
    """Configure the named logger to write to ``log_file`` and to the console.

    Handlers from a previous call with the same ``name`` are closed and replaced,
    so every object that creates its logger this way (``GridGenerator``,
    ``DatabaseClient``, ...) logs each message exactly once. The logger does not
    propagate to the root logger.

    Args:
        name: Logger name (shown in every message).
        log_file: Path of the log file; missing parent directories are created.
        log_level: Logging level, e.g. ``"INFO"`` or ``logging.DEBUG``.

    Returns:
        The configured logger.
    """
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name=name)
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()  # Clear existing handlers to prevent duplication

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    formatter.converter = lambda timestamp: datetime.fromtimestamp(timestamp, tz=LOG_TIMEZONE).timetuple()

    # to print log messages to a file
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(formatter)

    # to print log messages to console
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(log_level)
    logger.propagate = False

    return logger


# =============================================================================
# Electrical load aggregation
# =============================================================================
NONRESIDENTIAL_CATEGORIES = frozenset({"Commercial", "Public"})
LOAD_COMPONENT_COLUMNS = (
    "consumer_vertex",
    "category",
    "installed_kw",
    "load_units",
)


def _get_sim_factor(consumer_cat_df, definition):
    """Return the simultaneity factor of consumer category ``definition``.

    ``consumer_cat_df`` is either the consumer category table (with a
    ``definition`` column) or the same table indexed by ``definition``.
    """
    if "definition" in consumer_cat_df.columns:
        matches = consumer_cat_df.loc[consumer_cat_df["definition"] == definition, "sim_factor"]
        if len(matches) != 1:
            raise KeyError(f"Expected one simultaneity factor for {definition!r}, found {len(matches)}.")
        return float(matches.iloc[0])
    try:
        return float(consumer_cat_df.loc[definition]["sim_factor"])
    except KeyError as exc:
        raise KeyError(f"No simultaneity factor configured for {definition!r}.") from exc


def build_load_components(buildings_df):
    """Return electrical load components for the supplied building rows.

    A mixed-use building contributes two records at the same consumer vertex:
    one residential record and one record for its original non-residential use.
    Non-residential components classified as MV-direct are deliberately omitted
    from the LV model.

    Args:
        buildings_df: Building rows with ``vertice_id``, ``households``,
            ``residential_peak_load_in_kw``, ``nonresidential_peak_load_in_kw``,
            ``nonresidential_use`` and ``nonresidential_mv_direct``.

    Returns:
        DataFrame with the columns :data:`LOAD_COMPONENT_COLUMNS`; ``load_units`` is
        the number of households for residential components and 1 otherwise.

    Raises:
        ValueError: If a non-residential load has a use other than
            ``Commercial`` or ``Public``.
    """
    components = [
        {
            "consumer_vertex": row.vertice_id,
            "category": category,
            "installed_kw": installed_kw,
            "load_units": load_units,
        }
        for row in buildings_df.itertuples(index=False)
        for category, installed_kw, load_units in _building_load_components(row)
    ]
    return pd.DataFrame.from_records(components, columns=LOAD_COMPONENT_COLUMNS)


def _building_load_components(row) -> list[tuple[str, float, float]]:
    """Return ``(category, installed_kw, load_units)`` of each load component of one building row.

    See :func:`build_load_components` for the rules.

    Raises:
        ValueError: If a non-residential load has a use other than ``Commercial`` or ``Public``.
    """
    components = []
    residential_kw = (
        0.0 if pd.isna(row.residential_peak_load_in_kw) else float(row.residential_peak_load_in_kw)
    )
    households = 0.0 if pd.isna(row.households) else float(row.households)
    if residential_kw > 0 and households > 0:
        components.append(("Residential", residential_kw, households))

    nonresidential_kw = (
        0.0
        if pd.isna(row.nonresidential_peak_load_in_kw)
        else float(row.nonresidential_peak_load_in_kw)
    )
    mv_direct = False if pd.isna(row.nonresidential_mv_direct) else bool(row.nonresidential_mv_direct)
    if nonresidential_kw > 0 and not mv_direct:
        category = row.nonresidential_use
        if category not in NONRESIDENTIAL_CATEGORIES:
            raise ValueError(
                f"Building at vertex {row.vertice_id} has a non-residential load "
                f"but invalid nonresidential_use={category!r}."
            )
        components.append((category, nonresidential_kw, 1.0))
    return components


def planning_nodes(buildings_df: pd.DataFrame) -> pd.Series:
    """Return the street-side node at which each building is planned.

    That is ``agg_connection_point`` where connection-point aggregation
    (``AGGREGATE_NEARBY_CONNECTION_POINTS``) assigned one, otherwise the building's
    own ``connection_point``.

    Args:
        buildings_df: Building rows with ``connection_point`` and optionally
            ``agg_connection_point``.

    Returns:
        Series aligned with ``buildings_df``.
    """
    planning_column = "agg_connection_point" if "agg_connection_point" in buildings_df.columns else "connection_point"
    nodes = buildings_df[planning_column]
    if planning_column == "agg_connection_point" and "connection_point" in buildings_df.columns:
        nodes = nodes.fillna(buildings_df["connection_point"])
    return nodes


def simultaneous_peak_load(buildings_df, consumer_cat_df, vertice_ids):
    """Return the coincident peak load (kW) of the buildings planned at the given nodes.

    Buildings are selected by :func:`planning_nodes`. Their load components are
    grouped by consumer category, :func:`category_simultaneous_load` is applied per
    category, and the category results are added.

    Args:
        buildings_df: Building rows (see :func:`build_load_components`).
        consumer_cat_df: Consumer categories with their ``sim_factor``.
        vertice_ids: Street-side planning node ids.

    Returns:
        Coincident peak load in kW (0.0 if no building matches).
    """
    subset_df = buildings_df[planning_nodes(buildings_df).isin(vertice_ids)]
    components = build_load_components(subset_df)

    total_sim_load = 0.0
    for category, rows in components.groupby("category"):
        total_sim_load += category_simultaneous_load(
            rows["installed_kw"].sum(),
            rows["load_units"].sum(),
            _get_sim_factor(consumer_cat_df, category),
        )
    return total_sim_load


class CoincidentLoads:
    """The load components of a set of buildings, built once for many coincident-load queries.

    ``CoincidentLoads(buildings_df, consumer_cat_df).simultaneous_peak_load(vertice_ids)`` returns
    exactly :func:`simultaneous_peak_load` ``(buildings_df, consumer_cat_df, vertice_ids)``
    (same components, category order and summation order) without rebuilding the components
    for every node set; feeder planning and transformer assignment query thousands of node
    sets of the same buildings. ``buildings_df`` must not change afterwards.
    """

    def __init__(self, buildings_df, consumer_cat_df):
        self._consumer_cat_df = consumer_cat_df
        self._sim_factors = {}
        nodes, categories, installed_kw, load_units = [], [], [], []
        invalid_nodes, self._invalid_errors = [], []
        for node, row in zip(planning_nodes(buildings_df).tolist(), buildings_df.itertuples(index=False)):
            try:
                row_components = _building_load_components(row)
            except ValueError as error:  # raised like simultaneous_peak_load once the row is selected
                invalid_nodes.append(node)
                self._invalid_errors.append(error)
                continue
            for category, component_kw, component_units in row_components:
                nodes.append(node)
                categories.append(category)
                installed_kw.append(component_kw)
                load_units.append(component_units)
        self._categories = sorted(set(categories))  # groupby("category") order
        code_by_category = {category: code for code, category in enumerate(self._categories)}
        self._nodes = np.asarray(nodes, dtype=float)
        self._codes = np.asarray([code_by_category[c] for c in categories], dtype=np.int64)
        self._installed_kw = np.asarray(installed_kw, dtype=float)
        self._load_units = np.asarray(load_units, dtype=float)
        self._invalid_nodes = np.asarray(invalid_nodes, dtype=float)

    def simultaneous_peak_load(self, vertice_ids):
        """Return the coincident peak load (kW) of the buildings planned at the given nodes."""
        wanted = np.asarray(list(vertice_ids), dtype=float)
        if len(self._invalid_nodes):
            invalid = np.flatnonzero(np.isin(self._invalid_nodes, wanted))
            if len(invalid):
                raise self._invalid_errors[invalid[0]]
        selected = np.isin(self._nodes, wanted)
        codes = self._codes[selected]
        installed_kw = self._installed_kw[selected]
        load_units = self._load_units[selected]
        total_sim_load = 0.0
        for code in np.unique(codes):
            in_category = codes == code
            category = self._categories[code]
            if category not in self._sim_factors:
                self._sim_factors[category] = _get_sim_factor(self._consumer_cat_df, category)
            total_sim_load += category_simultaneous_load(
                installed_kw[in_category].sum(),
                load_units[in_category].sum(),
                self._sim_factors[category],
            )
        return total_sim_load


def allocate_consumer_simultaneous_loads(consumer_list, buildings_df, consumer_cat_df):
    """Calculate power-flow snapshot components and service design loads.

    Transformer and feeder sizing use grouped simultaneity per main category.
    The power-flow snapshot distributes each grouped category total in
    proportion to installed category power. Service cables are instead sized
    from the local coincident load of all consumers physically connected behind
    that cable.

    Args:
        consumer_list: Consumer vertices of the grid.
        buildings_df: Building rows of the grid (see :func:`build_load_components`).
        consumer_cat_df: Consumer categories with their ``sim_factor``.

    Returns:
        Tuple ``(service_design_load_per_consumer, powerflow_snapshot_components)``:
        the service design load in kW per consumer vertex, and per consumer vertex
        a list of records with ``category``, ``installed_kw``, ``load_units``,
        ``simultaneous_kw`` (power-flow snapshot) and ``service_design_kw``.
    """
    components = build_load_components(buildings_df)
    components["simultaneous_kw"] = 0.0

    for category, indices in components.groupby("category").groups.items():
        rows = components.loc[indices]
        sim_factor = _get_sim_factor(consumer_cat_df, category)
        installed_total_kw = rows["installed_kw"].sum()
        grouped_sim_kw = category_simultaneous_load(
            installed_total_kw, rows["load_units"].sum(), sim_factor
        )
        utilization = grouped_sim_kw / installed_total_kw if installed_total_kw > 0 else 0.0
        components.loc[indices, "simultaneous_kw"] = rows["installed_kw"] * utilization

    service_design_load_per_consumer = {consumer: 0.0 for consumer in consumer_list}
    powerflow_snapshot_components = {consumer: [] for consumer in consumer_list}

    grouped = components.groupby(["consumer_vertex", "category"], as_index=False).agg(
        installed_kw=("installed_kw", "sum"),
        load_units=("load_units", "sum"),
        simultaneous_kw=("simultaneous_kw", "sum"),
    )
    grouped["service_design_kw"] = [
        category_simultaneous_load(
            row.installed_kw,
            row.load_units,
            _get_sim_factor(consumer_cat_df, row.category),
        )
        for row in grouped.itertuples(index=False)
    ]
    for consumer, rows in grouped.groupby("consumer_vertex"):
        if consumer not in powerflow_snapshot_components:
            continue
        records = rows[
            ["category", "installed_kw", "load_units", "simultaneous_kw", "service_design_kw"]
        ].to_dict("records")
        powerflow_snapshot_components[consumer] = records
        service_design_load_per_consumer[consumer] = float(rows["service_design_kw"].sum())

    return service_design_load_per_consumer, powerflow_snapshot_components


def category_simultaneous_load(installed_power, load_count, sim_factor):
    """Return the coincident load of ``load_count`` consumers of one category.

    ``P_sim = P_installed * (g + (1 - g) * n ** (-3/4))`` with the category's
    simultaneity factor ``g`` and ``n = load_count``.

    Args:
        installed_power: Sum of the individual peak loads (any power unit).
        load_count: Number of consumers (households for residential loads).
        sim_factor: Simultaneity factor ``g`` of the category (0..1).

    Returns:
        Coincident load in the unit of ``installed_power``; 0 for missing,
        zero or negative power or count.
    """
    if installed_power is None or load_count is None:
        return 0
    if float(installed_power) <= 0 or float(load_count) <= 0:
        return 0
    else:
        sim_load = installed_power * (sim_factor + (1 - sim_factor) * (float(load_count) ** (-3 / 4)))

    return sim_load


def design_current_ka(load_kw: float) -> float:
    """Return the three-phase line current in kA of an active power at nominal voltage.

    ``I = P / (sqrt(3) * VN * cos(phi))`` with ``VN`` in V and ``cos(phi) =
    DEFAULT_POWER_FACTOR``, so kW / V gives kA.

    Args:
        load_kw: Active power in kW, e.g. a coincident peak load.

    Returns:
        Current in kA.
    """
    return load_kw / (VN * DEFAULT_POWER_FACTOR * np.sqrt(3))


# =============================================================================
# OpenStreetMap / Overpass API
# =============================================================================
def osmjson_to_geojson(osmjson: dict) -> dict:
    """Convert JSON dict received from overpass api to GeoJSON dictionary.

    The OSM ``tags`` of each feature are moved directly into its ``properties``.

    Args:
        osmjson: JSON dictionary received from overpass api

    Returns:
        GeoJSON representation of osmjson
    """
    geojson = osm2geojson.json2geojson(osmjson)

    # put attributes in "tags" directly into "properties"
    for feature in geojson['features']:
        if "tags" in feature["properties"]:
            feature["properties"].update(feature["properties"].pop("tags"))

    return geojson


def query_overpass_for_geojson(overpass_url: str, query: str) -> dict:
    """Execute an Overpass API query and convert the result to GeoJSON.

    The query is sent as a form-encoded POST (long queries do not fit into a URL)
    with a descriptive User-Agent, which the public Overpass instances require.

    Args:
        overpass_url: Overpass API interpreter URL,
            e.g. ``https://overpass-api.de/api/interpreter``.
        query: Overpass QL query that requests ``[out:json]``.

    Returns:
        GeoJSON representation of overpass results

    Raises:
        requests.HTTPError: If the server answers with an error status
            (for example 429 or 504 when it is overloaded).
        requests.Timeout: If the server does not answer within
            :data:`OVERPASS_TIMEOUT_S`.
    """
    response = requests.post(
        overpass_url,
        data={"data": query},
        headers={"User-Agent": PYLOVO_USER_AGENT},
        timeout=OVERPASS_TIMEOUT_S,
    )
    response.raise_for_status()

    # convert JSON data to GeoJSON format
    osmjson = response.json()
    geojson = osmjson_to_geojson(osmjson)

    return geojson
