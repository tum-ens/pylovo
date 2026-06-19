import osm2geojson
import requests
import shutil
import os
from pathlib import Path
import logging
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
    log_file = log_file
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


SIMULTANEITY_CATEGORY_GROUPS = (
    ("Residential", "SFH", ("SFH", "MFH", "AB", "TH")),
    ("Commercial", "Commercial", ("Commercial",)),
    ("Public", "Public", ("Public",)),
    ("Industrial", "Industrial", ("Industrial",)),
)


def _get_sim_factor(consumer_cat_df, definition):
    if "definition" in consumer_cat_df.columns:
        return consumer_cat_df.loc[consumer_cat_df["definition"] == definition, "sim_factor"].item()
    return consumer_cat_df.loc[definition]["sim_factor"]


def simultaneousPeakLoad(buildings_df, consumer_cat_df, vertice_ids):
    # Calculates the simultaneous peak load of buildings with given street-side planning node ids.
    planning_column = "agg_connection_point" if "agg_connection_point" in buildings_df.columns else "connection_point"
    subset_df = buildings_df[buildings_df[planning_column].isin(vertice_ids)]

    # Sim loads from each category to dictionary
    category_load_dict = {}
    for _group_name, factor_definition, member_types in SIMULTANEITY_CATEGORY_GROUPS:
        # Aggregate total installed power from the category cat
        installed_power = subset_df[subset_df['type'].isin(member_types)]["peak_load_in_kw"].values.sum()  # n*P_0
        # building amount from cat
        load_count = subset_df[subset_df['type'].isin(member_types)]['households'].values.sum()
        if load_count == 0:
            continue

        sim_factor = _get_sim_factor(consumer_cat_df, factor_definition)  # g_inf

        # Calculate simultaneous load (Kerber.2011) Gl. 3.2 - S. 23
        sim_load = oneSimultaneousLoad(installed_power, load_count, sim_factor)
        category_load_dict[factor_definition] = sim_load

    # print(category_load_dict)
    # Calculate total sim load (Kiefer S. 142)
    total_sim_load = sum(category_load_dict.values())
    # print(f"Total sim load: {total_sim_load}")

    return total_sim_load


def allocate_consumer_simultaneous_loads(consumer_list, buildings_df, consumer_cat_df):
    """Allocate grouped simultaneity-consistent loads to consumer vertices.

    Transformer and feeder sizing use grouped simultaneity per main category.
    Power-flow loads, however, are attached to consumer vertices. This helper
    distributes the grouped category load back to those consumer vertices while
    preserving the grouped total and aggregating duplicate building rows per
    vertex.
    """
    sim_load_per_building = {consumer: 0.0 for consumer in consumer_list}
    load_units = {consumer: 0 for consumer in consumer_list}
    load_type = {consumer: "SFH" for consumer in consumer_list}
    scale_by_type = {}

    for _group_name, factor_definition, member_types in SIMULTANEITY_CATEGORY_GROUPS:
        category_rows = buildings_df[buildings_df["type"].isin(member_types)]
        if category_rows.empty:
            continue

        total_individual_sim_kw = 0.0
        for row in category_rows.itertuples():
            sim_factor = _get_sim_factor(consumer_cat_df, row.type)
            total_individual_sim_kw += oneSimultaneousLoad(
                row.peak_load_in_kw, row.households, sim_factor
            )

        grouped_sim_kw = oneSimultaneousLoad(
            category_rows["peak_load_in_kw"].sum(),
            category_rows["households"].sum(),
            _get_sim_factor(consumer_cat_df, factor_definition),
        )
        scale = grouped_sim_kw / total_individual_sim_kw if total_individual_sim_kw > 0 else 0.0
        for member_type in member_types:
            scale_by_type[member_type] = scale

    for row in buildings_df.itertuples():
        load_units[row.vertice_id] += row.households
        if load_type[row.vertice_id] == "SFH":
            load_type[row.vertice_id] = row.type
        elif load_type[row.vertice_id] != row.type:
            load_type[row.vertice_id] = "Mixed"

        sim_load_per_building[row.vertice_id] += oneSimultaneousLoad(
            row.peak_load_in_kw,
            row.households,
            _get_sim_factor(consumer_cat_df, row.type),
        ) * scale_by_type.get(row.type, 1.0)

    for consumer, consumer_type in load_type.items():
        if consumer_type == "Mixed":
            load_type[consumer] = "Commercial"

    return sim_load_per_building, load_units, load_type


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
