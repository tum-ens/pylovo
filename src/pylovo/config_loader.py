"""Configuration of pylovo, loaded once at import time.

Importing this module (which every pylovo module does, directly or through
``import pylovo``) reads

* the database connection and path settings from a ``.env`` file, and
* the four YAML files ``config_generation.yaml``, ``config_analysis.yaml``,
  ``config_classification.yaml`` and ``config_clustering.yaml``,

and exposes their entries as module-level constants (``VERSION_ID``, ``VN``,
``TRANSFORMERS``, ...). Changing a YAML file therefore needs a new Python process.

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
of the package; relative output paths (``log/``, ``RESULT_DIR``, ``QGIS/``, the
``CSV_FILE_LIST`` entries) are resolved against it as well.
"""

import os
from pathlib import Path

import pandas as pd
import yaml
from dotenv import find_dotenv, load_dotenv


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


def load_yaml_config(filename: str):
    """Load a YAML configuration file from the first search path that contains it.

    Args:
        filename: Name of the config file (e.g., "config_generation.yaml")

    Returns:
        Loaded configuration dictionary

    Raises:
        FileNotFoundError: If no search path (see :func:`get_config_search_paths`)
            contains the file.
    """
    # Try user-defined locations
    for search_path in get_config_search_paths():
        config_file = search_path / filename
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as file:
                return yaml.safe_load(file)

    # Config not found - provide helpful error message
    raise FileNotFoundError(
        f"Config file '{filename}' not found in any search location.\n"
        f"Searched: {[str(p) for p in get_config_search_paths()]}\n\n"
        f"To get started:\n"
        f"1. Clone the repository: git clone https://github.com/tum-ens/pylovo.git\n"
        f"2. Navigate to the repo: cd pylovo\n"
        f"3. Install: pip install -e . (or uv sync)\n"
        f"4. Edit configs in config/ directory\n"
        f"5. Run: pylovo-setup\n"
    )


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
    """Build and validate the consumer category table of ``config_generation.yaml``.

    A ``peak_load`` given as the string ``PEAK_LOAD_HOUSEHOLD`` is replaced by that
    value; ``null`` peak loads (categories sized per m2) become NaN.

    Raises:
        ValueError: If ``definition`` or ``sim_factor`` is missing or a definition
            occurs twice.
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
    return categories


# =============================================================================
# .ENV AND CONFIG LOADING
# =============================================================================
# Load .env first, so PYLOVO_ROOT / PYLOVO_CONFIG_DIR set there also apply to the
# YAML search below. override=True: the .env wins over variables of the shell.
load_dotenv(find_dotenv(), override=True)

# Current working directory at import time (not the package location).
PROJECT_ROOT = Path.cwd()

CONFIG_GENERATION = load_yaml_config("config_generation.yaml")
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
if USE_INFDB:
    INFDB_DBNAME = DBNAME
    INFDB_USER = DBUSER
    INFDB_HOST = HOST
    INFDB_PORT = PORT
    INFDB_PASSWORD = PASSWORD
    INFDB_SOURCE_SCHEMA = os.getenv("INFDB_SOURCE_SCHEMA", "pylovo_input")
    INFDB_OPENDATA_SCHEMA = os.getenv("INFDB_OPENDATA_SCHEMA", "opendata")
else:
    INFDB_DBNAME = None
    INFDB_USER = None
    INFDB_HOST = None
    INFDB_PORT = None
    INFDB_PASSWORD = None
    INFDB_SOURCE_SCHEMA = None
    INFDB_OPENDATA_SCHEMA = None

# Validation Data Path
GRID_DATA_PATH = os.getenv("GRID_DATA_PATH")

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
USE_DSO_TRANSFORMER_POSITIONS = CONFIG_GENERATION.get("USE_DSO_TRANSFORMER_POSITIONS", False)
USE_OPEN_TRANSFORMER_POSITIONS = CONFIG_GENERATION.get("USE_OPEN_TRANSFORMER_POSITIONS", True)
# Manual (UI) positions alone, without the OSM and LoD2 candidates (see database.transformer_sources)
USE_MANUAL_TRANSFORMER_POSITIONS = CONFIG_GENERATION.get("USE_MANUAL_TRANSFORMER_POSITIONS", False)

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
MAX_BUILDINGS_PER_KCID = CONFIG_GENERATION["MAX_BUILDINGS_PER_KCID"]
MIN_SHARED_PREFIX_LENGTH_M = CONFIG_GENERATION.get("MIN_SHARED_PREFIX_LENGTH_M", 0)
AGGREGATE_NEARBY_CONNECTION_POINTS = CONFIG_GENERATION.get("AGGREGATE_NEARBY_CONNECTION_POINTS", False)
CONNECTION_POINT_AGGREGATION_RADIUS_M = CONFIG_GENERATION.get("CONNECTION_POINT_AGGREGATION_RADIUS_M", 25)
CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS = CONFIG_GENERATION.get("CONNECTION_POINT_AGGREGATION_MAX_BUILDINGS", 8)
K_MEANS_SEED = CONFIG_GENERATION["K_MEANS_SEED"]

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
N_CLUSTERS_KMEDOID = CONFIG_CLUSTERING["N_CLUSTERS_KMEDOID"]
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
# DATA IMPORT CONFIGURATION (only relevant without InfDB)
# =============================================================================
# Paths are relative to the current working directory.
CSV_FILE_LIST = [
    {"path": os.path.join("data", "postcode.csv"), "table_name": "postcode"},
]

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
