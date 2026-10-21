"""Configuration of pylovo, loaded once at import time.

Importing this module (which every pylovo module does, directly or through
``import pylovo``) reads

* the database connection and path settings from a ``.env`` file, and
* the YAML files ``config_generation_lv.yaml`` (the LV grids; its former name ``config_generation.yaml`` is still
  read), ``config_generation_mv.yaml`` (the MV stage, from the same directory), ``config_analysis.yaml``,
  ``config_classification.yaml`` and ``config_clustering.yaml``,

and exposes their entries as module-level constants (``VERSION_ID``, ``VN``,
``TRANSFORMERS``, ...). Changing a YAML file therefore needs a new Python process. The ``MV_*``
keys of the MV stage are checked with the rules of :mod:`pylovo.mv_grid.options`.

``.env`` lookup:
    ``load_dotenv(find_dotenv(), override=True)``. ``find_dotenv`` walks up from the
    directory of this file (for an editable install: ``src/pylovo`` -> ``src`` ->
    repository root) and takes the first ``.env`` it finds; in an interactive session,
    a notebook or under a debugger it walks up from the current working directory
    instead. Because of ``override=True`` the values in that ``.env`` **replace**
    environment variables of the same name that are already set in the shell.

YAML lookup:
    See :func:`get_config_search_paths`. The first location is ``./config`` of the
    current working directory, so pylovo commands are normally run from the
    repository root (or from a directory with its own ``config/``).

``PROJECT_ROOT`` is the current working directory at import time, not the location
of the package; relative output paths (``log/``, ``RESULT_DIR``, ``QGIS/``) are resolved
against it as well.
"""

import os
import warnings
from pathlib import Path

import pandas as pd
import yaml
from dotenv import find_dotenv, load_dotenv

from pylovo.mv_grid import options as _mv_options


def get_config_search_paths():
    """Return the directories searched for the YAML files, in priority order.

    1. ``<cwd>/config``
    2. ``$PYLOVO_ROOT/config`` if ``PYLOVO_ROOT`` is set (Docker / pip install)
    3. ``$PYLOVO_CONFIG_DIR`` if it is set; otherwise ``~/.config/pylovo`` (Linux/macOS)
       and ``<cwd>/.pylovo``
    4. ``config/`` of the source checkout this module was imported from, if it exists

    The environment variables may also be set in the ``.env`` file, which is loaded
    before the YAML files.

    Returns:
        List of Path objects to search for config files
    """
    search_paths = []

    # 1. Current working directory (highest priority for development)
    search_paths.append(Path.cwd() / "config")

    # 2. PYLOVO_ROOT/config (Docker/pip install scenario)
    pylovo_root = os.getenv("PYLOVO_ROOT")
    if pylovo_root:
        search_paths.append(Path(pylovo_root) / "config")

    # 3. User config directory
    user_config_dir = os.getenv("PYLOVO_CONFIG_DIR")
    if user_config_dir:
        search_paths.append(Path(user_config_dir))
    else:
        # Default user config locations
        if os.name == "posix":  # Linux/Mac
            search_paths.append(Path.home() / ".config" / "pylovo")
        search_paths.append(Path.cwd() / ".pylovo")  # Project-local config

    # 4. Source checkout (src layout: src/pylovo/config_loader.py -> <repo>/config)
    checkout_config = Path(__file__).parent.parent.parent / "config"
    if checkout_config.exists():
        search_paths.append(checkout_config)

    return search_paths


def find_config_file(*filenames: str) -> Path:
    """Return the config file of the first search path that contains one of ``filenames`` (the first of them).

    Args:
        filenames: Names of the config file, e.g. ``"config_generation_lv.yaml", "config_generation.yaml"``

    Raises:
        FileNotFoundError: If no search path (see :func:`get_config_search_paths`)
            contains one of the files.
    """
    for search_path in get_config_search_paths():
        for filename in filenames:
            config_file = search_path / filename
            if config_file.exists():
                return config_file

    # Config not found - provide helpful error message
    raise FileNotFoundError(
        f"Config file '{' or '.join(filenames)}' not found in any search location.\n"
        f"Searched: {[str(p) for p in get_config_search_paths()]}\n\n"
        f"To get started:\n"
        f"1. Clone the repository: git clone https://github.com/tum-ens/pylovo.git\n"
        f"2. Navigate to the repo: cd pylovo\n"
        f"3. Install: pip install -e . (or uv sync)\n"
        f"4. Edit configs in config/ directory\n"
        f"5. Run: pylovo-setup\n"
    )


def read_yaml(path: Path) -> dict:
    """Read one YAML configuration file (an empty file gives an empty dictionary)."""
    with open(path, "r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def load_yaml_config(filename: str):
    """Load a YAML configuration file from the first search path that contains it (:func:`find_config_file`).

    Args:
        filename: Name of the config file (e.g., "config_analysis.yaml")

    Returns:
        Loaded configuration dictionary
    """
    return read_yaml(find_config_file(filename))


def get_required_env_var(var_name: str, description: str) -> str:
    """Return a required environment variable (usually set in ``.env``).

    Raises:
        ValueError: If the variable is not set; a setup hint is printed first.
    """
    value = os.getenv(var_name)
    if value is None:
        print("=" * 80)
        print("❌ MISSING DATABASE CONFIGURATION")
        print("=" * 80)
        print(f"Environment variable '{var_name}' is not set.")
        print(f"Description: {description}")
        print()
        print("📋 SETUP REQUIRED:")
        print("1. Create a .env file in the project root directory")
        print("2. Update the values with your actual database credentials")
        print()
        print("=" * 80)
        raise ValueError(f"Missing required environment variable: {var_name}")
    return value


def get_int_env_var(var_name: str, default: int) -> int:
    """Return an integer environment variable, or ``default`` if it is unset or empty.

    Raises:
        ValueError: If the variable is set but not an integer.
    """
    value = os.getenv(var_name)
    if value is None or value == "":
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable '{var_name}' must be an integer, got: {value}") from exc

def _consumer_categories_from_config(config_generation: dict, peak_load_household) -> pd.DataFrame:
    """Build and validate the consumer category table of ``config_generation_lv.yaml``.

    A ``peak_load`` given as the string ``PEAK_LOAD_HOUSEHOLD`` is replaced by that
    value; ``null`` peak loads (categories sized per m2) become NaN.

    Raises:
        ValueError: If ``definition`` or ``sim_factor`` is missing, a definition
            occurs twice or a ``sim_factor`` is not a number from 0 to 1 (``null`` is NaN).
    """
    categories = pd.DataFrame(config_generation["CONSUMER_CATEGORIES"])
    if not categories.empty and "peak_load" in categories.columns:
        def _resolve_peak_load(val):
            if isinstance(val, str) and val.strip() == "PEAK_LOAD_HOUSEHOLD":
                return peak_load_household
            return val
        categories["peak_load"] = categories["peak_load"].apply(_resolve_peak_load)
        categories["peak_load"] = pd.to_numeric(categories["peak_load"], errors="coerce")

    missing_columns = {"definition", "sim_factor"}.difference(categories.columns)
    if missing_columns:
        raise ValueError(
            "CONSUMER_CATEGORIES is missing required columns: "
            f"{sorted(missing_columns)}"
        )
    if categories["definition"].duplicated().any():
        duplicate_definitions = categories.loc[
            categories["definition"].duplicated(keep=False), "definition"
        ].tolist()
        raise ValueError(f"Duplicate consumer category definitions: {duplicate_definitions}")
    categories["sim_factor"] = pd.to_numeric(categories["sim_factor"], errors="raise")
    invalid = ~categories["sim_factor"].between(0, 1)       # NaN and infinity are outside, too
    if invalid.any():
        raise ValueError("CONSUMER_CATEGORIES: sim_factor must be a number from 0 to 1, check "
                         f"{categories.loc[invalid, 'definition'].tolist()}")
    return categories


def _check_cable_ampacities(cables: pd.DataFrame, key: str) -> None:
    """Raise ValueError unless every cable of the catalogue ``key`` has a ``max_i_a`` above 0.

    The cable sizing searches the cable that carries the design current; without an ampacity above 0 it never ends.
    """
    if "max_i_a" not in cables.columns:
        raise ValueError(f"{key}: max_i_a is missing")
    valid = pd.to_numeric(cables["max_i_a"], errors="coerce") > 0
    if not valid.all():
        raise ValueError(f"{key}: max_i_a must be a number above 0, check {cables.loc[~valid, 'name'].tolist()}")


def _check_transformer_mapping(mapping: dict, transformers: pd.DataFrame) -> None:
    """Raise ValueError unless every settlement type of ``TRANSFORMER_MAPPING`` lists ratings of ``TRANSFORMERS``."""
    defined = set(transformers["s_max_kva"])
    for settlement_type, ratings in mapping.items():
        if not isinstance(ratings, (list, tuple)) or not ratings:
            raise ValueError(f"TRANSFORMER_MAPPING[{settlement_type}] must be a list of ratings in kVA, got {ratings!r}")
        unknown = [rating for rating in ratings if rating not in defined]
        if unknown:
            raise ValueError(f"TRANSFORMER_MAPPING[{settlement_type}] lists {unknown} kVA, "
                             f"which TRANSFORMERS does not define ({sorted(defined)})")


# =============================================================================
# .ENV AND CONFIG LOADING
# =============================================================================
# Load .env first, so PYLOVO_ROOT / PYLOVO_CONFIG_DIR set there also apply to the
# YAML search below. override=True: the .env wins over variables of the shell.
load_dotenv(find_dotenv(), override=True)

# Current working directory at import time (not the package location).
PROJECT_ROOT = Path.cwd()

#: The configuration of the LV grids, then its name from before the MV keys moved to their own file.
GENERATION_CONFIG_FILES: tuple[str, ...] = ("config_generation_lv.yaml", "config_generation.yaml")
#: The configuration of the MV stage, read from the directory of the LV configuration.
GENERATION_MV_CONFIG_FILE = "config_generation_mv.yaml"
CONFIG_GENERATION_FILE = find_config_file(*GENERATION_CONFIG_FILES)
if (CONFIG_GENERATION_FILE.name != GENERATION_CONFIG_FILES[-1]
        and (CONFIG_GENERATION_FILE.parent / GENERATION_CONFIG_FILES[-1]).exists()):
    warnings.warn(f"{CONFIG_GENERATION_FILE.parent / GENERATION_CONFIG_FILES[-1]} is not read: "
                  f"{CONFIG_GENERATION_FILE.name} in the same folder replaces it", stacklevel=1)
CONFIG_GENERATION = read_yaml(CONFIG_GENERATION_FILE)
CONFIG_GENERATION_MV_FILE = CONFIG_GENERATION_FILE.parent / GENERATION_MV_CONFIG_FILE
CONFIG_GENERATION_MV = read_yaml(CONFIG_GENERATION_MV_FILE) if CONFIG_GENERATION_MV_FILE.exists() else {}
CONFIG_ANALYSIS = load_yaml_config("config_analysis.yaml")
CONFIG_CLASSIFICATION = load_yaml_config("config_classification.yaml")
CONFIG_CLUSTERING = load_yaml_config("config_clustering.yaml")

# =============================================================================
# DATABASE CONFIGURATION (from .env file)
# =============================================================================
# Primary database connection (required)
DBNAME = get_required_env_var("DBNAME", "Database name for Pylovo")
DBUSER = get_required_env_var("DBUSER", "Database username")
HOST = get_required_env_var("HOST", "Database host address")
PORT = get_required_env_var("PORT", "Database port number")
PASSWORD = get_required_env_var("PASSWORD", "Database password")

# INFDB (external database) connection (recommended)
USE_INFDB = os.getenv("USE_INFDB", "True").lower() in ("true", "1", "yes")
# InfDB source data in another database than the pylovo schema; unset INFDB_* values fall back to the pylovo connection
USE_EXTERNAL_BASEDATA = os.getenv("USE_EXTERNAL_BASEDATA", "False").lower() in ("true", "1", "yes")
if USE_INFDB:
    if USE_EXTERNAL_BASEDATA:
        INFDB_DBNAME = os.getenv("INFDB_DBNAME") or DBNAME
        INFDB_USER = os.getenv("INFDB_USER") or DBUSER
        INFDB_HOST = os.getenv("INFDB_HOST") or HOST
        INFDB_PORT = os.getenv("INFDB_PORT") or PORT
        INFDB_PASSWORD = os.getenv("INFDB_PASSWORD") or PASSWORD
    else:
        INFDB_DBNAME = DBNAME
        INFDB_USER = DBUSER
        INFDB_HOST = HOST
        INFDB_PORT = PORT
        INFDB_PASSWORD = PASSWORD
    INFDB_SOURCE_SCHEMA = os.getenv("INFDB_SOURCE_SCHEMA", "basedata")
    INFDB_OPENDATA_SCHEMA = os.getenv("INFDB_OPENDATA_SCHEMA", "opendata")
    # OSM layers of the InfDB OSM importer (pgosm-flex): HV/MV substations for the MV stage
    INFDB_OSM_SCHEMA = os.getenv("INFDB_OSM_SCHEMA", "osm")
else:
    INFDB_DBNAME = None
    INFDB_USER = None
    INFDB_HOST = None
    INFDB_PORT = None
    INFDB_PASSWORD = None
    INFDB_SOURCE_SCHEMA = None
    INFDB_OPENDATA_SCHEMA = None
    INFDB_OSM_SCHEMA = None

# Geospatial CRS configuration
TARGET_EPSG = get_int_env_var("TARGET_EPSG", 25832)

# =============================================================================
# EXECUTION CONFIGURATION (from CONFIG_GENERATION)
# =============================================================================
ANALYZE_GRIDS = CONFIG_GENERATION["ANALYZE_GRIDS"]
SAVE_GRID_FOLDER = CONFIG_GENERATION["SAVE_GRID_FOLDER"]
LOG_LEVEL = CONFIG_GENERATION["LOG_LEVEL"]

# Parallel execution configuration: PARALLEL is the default of ``pylovo-generate`` for several
# PLZ (``--parallel`` / ``--no-parallel`` override it), N_JOBS_PERCENT the share of cores used.
PARALLEL = bool(CONFIG_GENERATION.get("PARALLEL", True))
N_JOBS_PERCENT = CONFIG_GENERATION.get("N_JOBS_PERCENT", 50)
AVAILABLE_CORES = os.cpu_count() or 1
N_JOBS = max(1, round(AVAILABLE_CORES * N_JOBS_PERCENT / 100))

# Result directory configuration
RESULT_DIR = os.path.join(os.getcwd(), CONFIG_GENERATION.get("RESULT_DIR", "results"))

# Electrical backend configuration
ELECTRICAL_BACKEND = CONFIG_GENERATION.get("ELECTRICAL_BACKEND", "pandapower")
RESIDENTIAL_ONLY_GENERATION = CONFIG_GENERATION.get("RESIDENTIAL_ONLY_GENERATION", False)
# Leave out buildings without street and house number (outbuildings such as sheds, garages, barns).
EXCLUDE_BUILDINGS_WITHOUT_ADDRESS = CONFIG_GENERATION.get("EXCLUDE_BUILDINGS_WITHOUT_ADDRESS", False)

# =============================================================================
# GRID GENERATION CONFIGURATION (from CONFIG_GENERATION)
# =============================================================================
# Version information
VERSION_ID = CONFIG_GENERATION["VERSION_ID"]
VERSION_COMMENT = CONFIG_GENERATION["VERSION_COMMENT"]

# Load calculation parameters
PEAK_LOAD_HOUSEHOLD = CONFIG_GENERATION["PEAK_LOAD_HOUSEHOLD"]
DEFAULT_POWER_FACTOR = CONFIG_GENERATION["DEFAULT_POWER_FACTOR"]

# Consumer categories for load calculation (validated table and category -> simultaneity factor)
CONSUMER_CATEGORIES = _consumer_categories_from_config(CONFIG_GENERATION, PEAK_LOAD_HOUSEHOLD)
SIM_FACTOR = CONSUMER_CATEGORIES.set_index("definition")["sim_factor"].astype(float).to_dict()

# Equipment catalogues; grid_role tells the three pools apart in CONFIG_EQUIPMENT_DATA.
TRANSFORMERS = pd.DataFrame(CONFIG_GENERATION["TRANSFORMERS"])
TRANSFORMERS["grid_role"] = "transformer"
FEEDER_CABLES = pd.DataFrame(CONFIG_GENERATION["FEEDER_CABLES"])
FEEDER_CABLES["grid_role"] = "feeder"
CONSUMER_CONNECTION_CABLES = pd.DataFrame(CONFIG_GENERATION["CONSUMER_CONNECTION_CABLES"])
CONSUMER_CONNECTION_CABLES["grid_role"] = "consumer_connection"
_check_cable_ampacities(FEEDER_CABLES, "FEEDER_CABLES")
_check_cable_ampacities(CONSUMER_CONNECTION_CABLES, "CONSUMER_CONNECTION_CABLES")

# Derived combined equipment table for database storage and shared consumers.
CONFIG_EQUIPMENT_DATA = pd.concat(
    [TRANSFORMERS, FEEDER_CABLES, CONSUMER_CONNECTION_CABLES],
    ignore_index=True,
)

# Nominal voltage used for grid generation and electrical design.
VN = CONFIG_GENERATION["VN"]

# Post-solution power-flow assessment thresholds. These values do not affect
# solver convergence, grid topology, or cable sizing.
POWER_FLOW_VOLTAGE_LIMITS = CONFIG_ANALYSIS["POWER_FLOW_VOLTAGE_LIMITS"]
POWER_FLOW_MIN_VM_PU = POWER_FLOW_VOLTAGE_LIMITS["MIN_VM_PU"]
POWER_FLOW_MAX_VM_PU = POWER_FLOW_VOLTAGE_LIMITS["MAX_VM_PU"]

# =============================================================================
# CABLE DIMENSIONING PARAMETERS (from CONFIG_GENERATION)
# =============================================================================
# Maximum simultaneous current allowed while grouping nodes into one planned feeder branch.
# This is a topology-splitting parameter, not a final cable ampacity limit.
FEEDER_SPLIT_MAX_CURRENT_KA = CONFIG_GENERATION["FEEDER_SPLIT_MAX_CURRENT_KA"]
# Planned coincident current / ampacity of the feeder and service cable designs; 1.0 sizes at the full ampacity.
# FEEDER_PLANNING_UTILIZATION is its former name (it then applied to the feeders only) and is still read.
if (
    "CABLE_PLANNING_UTILIZATION" in CONFIG_GENERATION and "FEEDER_PLANNING_UTILIZATION" in CONFIG_GENERATION
    and CONFIG_GENERATION["CABLE_PLANNING_UTILIZATION"] != CONFIG_GENERATION["FEEDER_PLANNING_UTILIZATION"]
):
    raise ValueError("Set CABLE_PLANNING_UTILIZATION only; FEEDER_PLANNING_UTILIZATION is its former name")
CABLE_PLANNING_UTILIZATION = float(
    CONFIG_GENERATION.get("CABLE_PLANNING_UTILIZATION", CONFIG_GENERATION.get("FEEDER_PLANNING_UTILIZATION", 1.0))
)
if not CABLE_PLANNING_UTILIZATION > 0:
    raise ValueError(f"CABLE_PLANNING_UTILIZATION must be positive, got {CABLE_PLANNING_UTILIZATION}")
# House-connection technique: "joint" (tee joints on the street cable, cabinets only at split points) or
# "cabinet" (houses connected from cable distribution cabinets along the feeders, CABINET_SPACING_M apart at most).
HOUSE_CONNECTION = str(CONFIG_GENERATION.get("HOUSE_CONNECTION", "joint"))
if HOUSE_CONNECTION not in ("joint", "cabinet"):
    raise ValueError(f"HOUSE_CONNECTION must be 'joint' or 'cabinet', got {HOUSE_CONNECTION!r}")
CABINET_SPACING_M = float(CONFIG_GENERATION.get("CABINET_SPACING_M", 100))
if not CABINET_SPACING_M > 0:
    raise ValueError(f"CABINET_SPACING_M must be positive, got {CABINET_SPACING_M}")
# Standard feeder cable, laid in parallel for load and voltage before larger cables are used; None = smallest sufficient design (previous behaviour).
FEEDER_STANDARD_CABLE = CONFIG_GENERATION.get("FEEDER_STANDARD_CABLE")
if FEEDER_STANDARD_CABLE is not None and FEEDER_STANDARD_CABLE not in set(FEEDER_CABLES["name"]):
    raise ValueError(f"FEEDER_STANDARD_CABLE must name a FEEDER_CABLES entry, got {FEEDER_STANDARD_CABLE!r}")
# Most parallel standard cables per feeder section (only used with FEEDER_STANDARD_CABLE).
MAX_PARALLEL_FEEDER_CABLES = int(CONFIG_GENERATION.get("MAX_PARALLEL_FEEDER_CABLES", 4))
if MAX_PARALLEL_FEEDER_CABLES < 1:
    raise ValueError(f"MAX_PARALLEL_FEEDER_CABLES must be at least 1, got {MAX_PARALLEL_FEEDER_CABLES}")

# End-to-end transformer-to-connection-point feeder voltage-drop planning envelope.
MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT = CONFIG_GENERATION[
    "MAX_END_TO_END_FEEDER_VOLTAGE_DROP_PERCENT"
]

# Total service-cable voltage-drop limit at the building-local coincident design load.
MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT = CONFIG_GENERATION[
    "MAX_SERVICE_DESIGN_VOLTAGE_DROP_PERCENT"
]

# Commercial/Public loads above this threshold are assumed to connect through a dedicated MV-side supply.
MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW = CONFIG_GENERATION["MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW"]

# Consumer connection cables are defined directly by CONSUMER_CONNECTION_CABLES.

# =============================================================================
# SETTLEMENT TYPE THRESHOLDS (from CONFIG_GENERATION)
# =============================================================================
RURAL_MAX_HOUSEHOLDS = CONFIG_GENERATION["RURAL_MAX_HOUSEHOLDS"]
URBAN_MIN_HOUSEHOLDS = CONFIG_GENERATION["URBAN_MIN_HOUSEHOLDS"]
RURAL_MIN_BUILDING_DISTANCE = CONFIG_GENERATION["RURAL_MIN_BUILDING_DISTANCE"]
URBAN_MAX_BUILDING_DISTANCE = CONFIG_GENERATION["URBAN_MAX_BUILDING_DISTANCE"]

# Transformer mapping: Settlement Type -> Allowed Transformer Capacities (s_max_kva)
TRANSFORMER_MAPPING = CONFIG_GENERATION.get("TRANSFORMER_MAPPING", {
    1: [250, 400, 630],
    2: [250, 400, 630],
    3: [250, 400, 630]
})
_check_transformer_mapping(TRANSFORMER_MAPPING, TRANSFORMERS)
# CALIBRATION (temp): planned coincident load / rated power of a catalogue transformer.
# 1.0 sizes and splits at the full rating (previous behaviour).
TRANSFORMER_PLANNING_UTILIZATION = float(CONFIG_GENERATION.get("TRANSFORMER_PLANNING_UTILIZATION", 1.0))
# CALIBRATION (temp): greenfield stations are drawn at random among the distance-feasible positions whose
# load-weighted distance cost is at most (1 + tolerance) times the optimum. 0 keeps the optimal position.
GREENFIELD_TRAFO_POSITION_TOLERANCE = float(CONFIG_GENERATION.get("GREENFIELD_TRAFO_POSITION_TOLERANCE", 0.0))

# =============================================================================
# GRID GENERATION PARAMETERS (from CONFIG_GENERATION)
# =============================================================================
MAX_BROWNFIELD_TRAFO_DISTANCE = CONFIG_GENERATION["MAX_BROWNFIELD_TRAFO_DISTANCE"]
# A building joins only its nearest existing station; when that one is full it is left for greenfield.
BROWNFIELD_NEAREST_ONLY = bool(CONFIG_GENERATION.get("BROWNFIELD_NEAREST_ONLY", False))
USE_DSO_TRANSFORMER_POSITIONS = CONFIG_GENERATION.get("USE_DSO_TRANSFORMER_POSITIONS", False)
USE_OPEN_TRANSFORMER_POSITIONS = CONFIG_GENERATION.get("USE_OPEN_TRANSFORMER_POSITIONS", True)
# Manual (UI) positions alone, without the OSM and LoD2 candidates (see database.transformer_sources)
USE_MANUAL_TRANSFORMER_POSITIONS = CONFIG_GENERATION.get("USE_MANUAL_TRANSFORMER_POSITIONS", False)
# Postcode borders (see pylovo.postcode_border): a PLZ also routes over the InfDB streets of its neighboring PLZ.
# A configuration without the key keeps the PLZ graph; file input (no InfDB) has no neighboring streets.
ROUTE_ACROSS_POSTCODE_BORDERS = CONFIG_GENERATION.get("ROUTE_ACROSS_POSTCODE_BORDERS", False)
if not isinstance(ROUTE_ACROSS_POSTCODE_BORDERS, bool):
    raise ValueError(f"ROUTE_ACROSS_POSTCODE_BORDERS must be true or false, got {ROUTE_ACROSS_POSTCODE_BORDERS!r}")
ROUTE_ACROSS_POSTCODE_BORDERS = ROUTE_ACROSS_POSTCODE_BORDERS and USE_INFDB

# Station voltage of the validation power flow (pylovo.station_voltage). Missing keys keep the
# behaviour from before they existed: MV side at 1.0 p.u., neutral tap.
LV_REFERENCE_VOLTAGE_PU = CONFIG_GENERATION.get("LV_REFERENCE_VOLTAGE_PU")
if LV_REFERENCE_VOLTAGE_PU is not None:
    LV_REFERENCE_VOLTAGE_PU = float(LV_REFERENCE_VOLTAGE_PU)
    if not 0.8 <= LV_REFERENCE_VOLTAGE_PU <= 1.2:
        raise ValueError(f"LV_REFERENCE_VOLTAGE_PU must be between 0.8 and 1.2 p.u., got {LV_REFERENCE_VOLTAGE_PU}")
MAX_GREENFIELD_TRAFO_DISTANCE = CONFIG_GENERATION["MAX_GREENFIELD_TRAFO_DISTANCE"]
# CALIBRATION (temp): standard deviation (m) of a per-cluster greenfield distance limit drawn around
# MAX_GREENFIELD_TRAFO_DISTANCE and clipped to +/- 2 standard deviations. 0 keeps one fixed limit.
MAX_GREENFIELD_TRAFO_DISTANCE_STD = float(CONFIG_GENERATION.get("MAX_GREENFIELD_TRAFO_DISTANCE_STD", 0.0))
MERGE_GREENFIELD_CLUSTERS = CONFIG_GENERATION.get("MERGE_GREENFIELD_CLUSTERS", False)
GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA = CONFIG_GENERATION.get("GREENFIELD_CLUSTER_MERGE_TRANSFORMER_KVA", [400, 630])
# A merged pair needs a smaller cluster of at most this many loaded buildings; None merges clusters of any size.
GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS = CONFIG_GENERATION.get("GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS")
if GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS is not None and (
        isinstance(GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS, bool)
        or not isinstance(GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS, int)
        or GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS < 1):
    raise ValueError(
        f"GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS must be a positive integer or null, got {GREENFIELD_CLUSTER_MERGE_MAX_BUILDINGS!r}")
MAX_BUILDINGS_PER_KCID = CONFIG_GENERATION["MAX_BUILDINGS_PER_KCID"]
MIN_SHARED_PREFIX_LENGTH_M = CONFIG_GENERATION.get("MIN_SHARED_PREFIX_LENGTH_M", 0)
AGGREGATE_NEARBY_CONNECTION_POINTS = CONFIG_GENERATION.get("AGGREGATE_NEARBY_CONNECTION_POINTS", False)
CONNECTION_POINT_AGGREGATION_RADIUS_M = CONFIG_GENERATION.get("CONNECTION_POINT_AGGREGATION_RADIUS_M", 25)
CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS = CONFIG_GENERATION.get("CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS", 8)
K_MEANS_SEED = CONFIG_GENERATION["K_MEANS_SEED"]

# =============================================================================
# MEDIUM-VOLTAGE (MV) GRID CONFIGURATION (from CONFIG_GENERATION_MV)
# =============================================================================
# Not in the snapshot of VERSION_ID: the MV stage stores them per MV variant (pylovo.mv_variant), and only
# pylovo.mv_grid.parameters.MvParameters.from_config reads them. Rules and defaults: pylovo.mv_grid.options;
# a missing key takes the shipped value, so configurations without these keys keep importing. The MV_* keys of the
# MV stage in the LV file (as in the former config_generation.yaml) are still read.


def _mv_config(lv: dict, mv: dict) -> dict:
    """The MV stage keys of both files; a key may be in both only with the same value."""
    names = set(_mv_options.YAML_KEYS) | set(_mv_options.REMOVED_YAML_KEYS)
    for key in mv:
        if key not in names:
            raise ValueError(f"{key} is no key of the MV stage (the LV keys belong in {CONFIG_GENERATION_FILE.name}): "
                             f"{GENERATION_MV_CONFIG_FILE} holds only {', '.join(_mv_options.YAML_KEYS)}")
    config = {key: value for key, value in lv.items() if key in names}
    for key, value in mv.items():
        if key in config and config[key] != value:
            raise ValueError(f"{key} is set in {CONFIG_GENERATION_FILE.name} and in {GENERATION_MV_CONFIG_FILE} with "
                             f"different values; keep it in {GENERATION_MV_CONFIG_FILE} only")
        config[key] = value
    return config


CONFIG_MV = _mv_config(CONFIG_GENERATION, CONFIG_GENERATION_MV)


def _mv_value(key: str):
    return _mv_options.coerce_mv_value(key, CONFIG_MV.get(key, _mv_options.YAML_KEYS[key].default))


# Execution flag (in no snapshot): run the MV stage after the LV grids in pylovo-generate (--mv / --no-mv)
MV_GRID_GENERATION = _mv_value("MV_GRID_GENERATION")
MV_VARIANT_ID = _mv_value("MV_VARIANT_ID")
MV_VARIANT_COMMENT = _mv_value("MV_VARIANT_COMMENT")
MV_NOMINAL_VOLTAGE_KV = _mv_value("MV_NOMINAL_VOLTAGE_KV")
MV_MAX_STATIONS_PER_RING = _mv_value("MV_MAX_STATIONS_PER_RING")
MV_STANDARD_CABLE = _mv_value("MV_STANDARD_CABLE")
MV_OVERHEAD_LINE = _mv_value("MV_OVERHEAD_LINE")
MV_LINE_PLANNING_UTILIZATION = _mv_value("MV_LINE_PLANNING_UTILIZATION")
MV_MAX_VOLTAGE_DROP_PERCENT = _mv_value("MV_MAX_VOLTAGE_DROP_PERCENT")
_mv_options.check_removed_keys(CONFIG_MV)

# =============================================================================
# ANALYSIS CONFIGURATION (from CONFIG_ANALYSIS)
# =============================================================================
MUNICIPAL_REGISTER = CONFIG_ANALYSIS["MUNICIPAL_REGISTER"]
PLOT_COLOR_DICT = CONFIG_ANALYSIS["PLOT_COLOR_DICT"]

# =============================================================================
# CLASSIFICATION CONFIGURATION (from CONFIG_CLASSIFICATION)
# =============================================================================
CLASSIFICATION_VERSION = CONFIG_CLASSIFICATION["CLASSIFICATION_VERSION"]
CLASSIFICATION_VERSION_COMMENT = CONFIG_CLASSIFICATION["CLASSIFICATION_VERSION_COMMENT"]
CLASSIFICATION_REGION = CONFIG_CLASSIFICATION["CLASSIFICATION_REGION"]
NO_OF_CLUSTERS_ALLOWED = CONFIG_CLASSIFICATION["NO_OF_CLUSTERS_ALLOWED"]
N_SAMPLES = CONFIG_CLASSIFICATION["N_SAMPLES"]
REGION_DICT = CONFIG_CLASSIFICATION["REGION_DICT"]
REGIOSTAR7_DICT = CONFIG_CLASSIFICATION["REGIOSTAR7_DICT"]
REGIO7_REGIO5_GEM_DICT = CONFIG_CLASSIFICATION["REGIO7_REGIO5_GEM_DICT"]

# =============================================================================
# CLUSTERING CONFIGURATION (from CONFIG_CLUSTERING)
# =============================================================================
CLUSTERING_PARAMETERS = CONFIG_CLUSTERING["CLUSTERING_PARAMETERS"]
LIST_OF_CLUSTERING_PARAMETERS = CONFIG_CLUSTERING["LIST_OF_CLUSTERING_PARAMETERS"]
N_CLUSTERS_KMEANS = CONFIG_CLUSTERING["N_CLUSTERS_KMEANS"]
N_CLUSTERS_GMM = CONFIG_CLUSTERING["N_CLUSTERS_GMM"]

# Clustering thresholds
THRESHOLD_MAX_TRAFO_DIS = CONFIG_CLUSTERING["THRESHOLD_MAX_TRAFO_DIS"]
THRESHOLD_HOUSEHOLDS_PER_BUILDING = CONFIG_CLUSTERING["THRESHOLD_HOUSEHOLDS_PER_BUILDING"]
THRESHOLD_AVG_TRAFO_DIS = CONFIG_CLUSTERING["THRESHOLD_AVG_TRAFO_DIS"]
THRESHOLD_NO_HOUSE_CONNECTIONS = CONFIG_CLUSTERING["THRESHOLD_NO_HOUSE_CONNECTIONS"]
THRESHOLD_VSW_PER_BRANCH = CONFIG_CLUSTERING["THRESHOLD_VSW_PER_BRANCH"]
THRESHOLD_NO_HOUSEHOLDS = CONFIG_CLUSTERING["THRESHOLD_NO_HOUSEHOLDS"]

# =============================================================================
# PLOTTING CONFIGURATION (from CONFIG_ANALYSIS)
# =============================================================================
# Plotly configuration
ACCESS_TOKEN_PLOTLY = os.getenv("ACCESS_TOKEN_PLOTLY")

# TUM Color definitions
TUMBlue = CONFIG_ANALYSIS["COLORS"]["TUMBlue"]
TUMGreen = CONFIG_ANALYSIS["COLORS"]["TUMGreen"]
TUMOrange = CONFIG_ANALYSIS["COLORS"]["TUMOrange"]
TUMIvory = CONFIG_ANALYSIS["COLORS"]["TUMIvory"]
TUMBlue4 = CONFIG_ANALYSIS["COLORS"]["TUMBlue4"]
TUMBlue2 = CONFIG_ANALYSIS["COLORS"]["TUMBlue2"]
TUMGray2 = CONFIG_ANALYSIS["COLORS"]["TUMGray2"]

# TUM Color palettes
TUMPalette = CONFIG_ANALYSIS["PALETTES"]["TUMPalette"]
TUMPalette1 = CONFIG_ANALYSIS["PALETTES"]["TUMPalette1"]
TUMPalette2 = CONFIG_ANALYSIS["PALETTES"]["TUMPalette2"]
TUMPalette3 = CONFIG_ANALYSIS["PALETTES"]["TUMPalette3"]

# Network visualization colors
NODE_COLOR_TRAFO = CONFIG_ANALYSIS["NETWORK_COLORS"]["NODE_COLOR_TRAFO"]
NODE_COLOR_CONSUMER = CONFIG_ANALYSIS["NETWORK_COLORS"]["NODE_COLOR_CONSUMER"]
NODE_COLOR_CONNECTION_BUS = CONFIG_ANALYSIS["NETWORK_COLORS"]["NODE_COLOR_CONNECTION_BUS"]

# Plot style defaults
DEFAULT_FIGURE_SIZE = tuple(CONFIG_ANALYSIS["PLOT_DEFAULTS"]["FIGURE_SIZE"])
DEFAULT_DPI = CONFIG_ANALYSIS["PLOT_DEFAULTS"]["DPI"]
DEFAULT_FONT_SIZE = CONFIG_ANALYSIS["PLOT_DEFAULTS"]["FONT_SIZE"]
DEFAULT_TITLE_FONT_SIZE = CONFIG_ANALYSIS["PLOT_DEFAULTS"]["TITLE_FONT_SIZE"]
DEFAULT_GRID_ALPHA = CONFIG_ANALYSIS["PLOT_DEFAULTS"]["GRID_ALPHA"]

# Setup seaborn palette (optional "plots" extra)
try:
    import seaborn as sns
    sns.set_palette(sns.color_palette(TUMPalette))
except ImportError:
    pass  # seaborn not installed, skip palette setup
