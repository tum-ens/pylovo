import osm2geojson
import requests
import shutil
import os
from pathlib import Path
import logging
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo


UTC_PLUS_1 = ZoneInfo("Europe/Berlin")


def get_user_data_dir() -> Path:
    """
    Get the user data directory for pylovo.

    This directory contains user-provided data like building shapefiles,
    street network SQL files, and processed transformer GeoJSON files.

    Priority order:
    1. PYLOVO_DATA_DIR environment variable (explicit data directory)
    2. PYLOVO_ROOT environment variable + /data (Docker-friendly)
    3. Current working directory / data (development)

    Returns
    -------
    Path
        Path to the user data directory
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


def reset_log_directory():
    # Delete and recreate the log directory (preserving .gitkeep)
    log_dir = Path("log")
    if log_dir.exists():
        # Remove all files except .gitkeep
        for item in log_dir.iterdir():
            if item.name != ".gitkeep":
                if item.is_file():
                    item.unlink()
                elif item.is_dir():
                    shutil.rmtree(item)
        # Ensure the directory exists
        log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir

def create_logger(name, log_file, log_level):
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name=name)
    logger.handlers.clear()  # Clear existing handlers to prevent duplication

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    formatter.converter = lambda timestamp: datetime.fromtimestamp(timestamp, tz=UTC_PLUS_1).timetuple()

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


NONRESIDENTIAL_CATEGORIES = frozenset({"Commercial", "Public"})
LOAD_COMPONENT_COLUMNS = (
    "consumer_vertex",
    "category",
    "installed_kw",
    "load_units",
)


def _get_sim_factor(consumer_cat_df, definition):
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
    """
    components = []

    for row in buildings_df.itertuples(index=False):
        consumer_vertex = row.vertice_id
        residential_kw = (
            0.0 if pd.isna(row.residential_peak_load_in_kw) else float(row.residential_peak_load_in_kw)
        )
        households = 0.0 if pd.isna(row.households) else float(row.households)
        if residential_kw > 0 and households > 0:
            components.append(
                {
                    "consumer_vertex": consumer_vertex,
                    "category": "Residential",
                    "installed_kw": residential_kw,
                    "load_units": households,
                }
            )

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
                    f"Building at vertex {consumer_vertex} has a non-residential load "
                    f"but invalid nonresidential_use={category!r}."
                )
            components.append(
                {
                    "consumer_vertex": consumer_vertex,
                    "category": category,
                    "installed_kw": nonresidential_kw,
                    "load_units": 1.0,
                }
            )

    return pd.DataFrame.from_records(components, columns=LOAD_COMPONENT_COLUMNS)


def simultaneousPeakLoad(buildings_df, consumer_cat_df, vertice_ids):
    # Calculates the simultaneous peak load of buildings with given street-side planning node ids.
    planning_column = "agg_connection_point" if "agg_connection_point" in buildings_df.columns else "connection_point"
    planning_nodes = buildings_df[planning_column]
    if planning_column == "agg_connection_point" and "connection_point" in buildings_df.columns:
        planning_nodes = planning_nodes.fillna(buildings_df["connection_point"])
    subset_df = buildings_df[planning_nodes.isin(vertice_ids)]
    components = build_load_components(subset_df)

    total_sim_load = 0.0
    for category, rows in components.groupby("category"):
        total_sim_load += oneSimultaneousLoad(
            rows["installed_kw"].sum(),
            rows["load_units"].sum(),
            _get_sim_factor(consumer_cat_df, category),
        )
    return total_sim_load


def allocate_consumer_simultaneous_loads(consumer_list, buildings_df, consumer_cat_df):
    """Allocate grouped simultaneity-consistent loads to consumer vertices.

    Transformer and feeder sizing use grouped simultaneity per main category.
    Power-flow loads, however, are attached to consumer vertices. This helper
    distributes the grouped category load back to those consumer vertices while
    preserving the grouped total and aggregating duplicate building rows per
    vertex.
    """
    components = build_load_components(buildings_df)
    components["simultaneous_kw"] = 0.0

    for category, indices in components.groupby("category").groups.items():
        rows = components.loc[indices]
        sim_factor = _get_sim_factor(consumer_cat_df, category)
        individual_sim = rows.apply(
            lambda row: oneSimultaneousLoad(row["installed_kw"], row["load_units"], sim_factor),
            axis=1,
        )
        grouped_sim_kw = oneSimultaneousLoad(
            rows["installed_kw"].sum(), rows["load_units"].sum(), sim_factor
        )
        individual_total = individual_sim.sum()
        scale = grouped_sim_kw / individual_total if individual_total > 0 else 0.0
        components.loc[indices, "simultaneous_kw"] = individual_sim * scale

    sim_load_per_consumer = {consumer: 0.0 for consumer in consumer_list}
    component_loads = {consumer: [] for consumer in consumer_list}

    grouped = components.groupby(["consumer_vertex", "category"], as_index=False).agg(
        installed_kw=("installed_kw", "sum"),
        load_units=("load_units", "sum"),
        simultaneous_kw=("simultaneous_kw", "sum"),
    )
    for consumer, rows in grouped.groupby("consumer_vertex"):
        if consumer not in component_loads:
            continue
        records = rows[["category", "installed_kw", "load_units", "simultaneous_kw"]].to_dict("records")
        component_loads[consumer] = records
        sim_load_per_consumer[consumer] = float(rows["simultaneous_kw"].sum())

    return sim_load_per_consumer, component_loads


def oneSimultaneousLoad(installed_power, load_count, sim_factor):
    # calculation of the simultaneaous load of multiple consumers of the same kind (public, commercial or residential)
    # Safe guards: zero/negative loads or counts yield 0
    if installed_power is None or load_count is None:
        return 0
    if float(installed_power) <= 0 or float(load_count) <= 0:
        return 0
    else:
        sim_load = installed_power * (sim_factor + (1 - sim_factor) * (float(load_count) ** (-3 / 4)))

    return sim_load


def osmjson_to_geojson(osmjson: dict[str, str]) -> dict[str, str]:
    """Convert JSON dict received from overpass api to GeoJSON dictionary.

    Args:
        osmjson: JSON dictionary received from overpass api

    Returns:
        dict: GeoJSON representation of osmjson

    """
    geojson = osm2geojson.json2geojson(osmjson)

    # put attributes in "tags" directly into "properties"
    for feature in geojson['features']:
        if "tags" in feature["properties"]:
            feature["properties"].update(feature["properties"].pop("tags"))

    return geojson


def query_overpass_for_geojson(overpass_url: str, query: str) -> dict[str, str]:
    """Execute an overpass turbo query and convert results to GeoJSON.

    Args:
        overpass_url: Overpass API URL
        query: Query string

    Returns:
        dict: GeoJSON representation of overpass results

    """
    # call api for data
    response = requests.get(overpass_url, params={'data': query})
    response.raise_for_status()

    # convert JSON data to GeoJSON format
    osmjson = response.json()
    geojson = osmjson_to_geojson(osmjson)

    return geojson
