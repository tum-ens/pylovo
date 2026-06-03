import yaml
import os
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv, find_dotenv


def get_config_search_paths():
    """
    Get list of paths to search for configuration files, in priority order.

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

    # 4. Legacy location (for backward compatibility during migration)
    try:
        # Check if we're in development mode (src layout exists)
        legacy_path = Path(__file__).parent.parent.parent / "config"
        if legacy_path.exists():
            search_paths.append(legacy_path)
    except:
        pass

    return search_paths


def load_yaml_config(filename: str):
    """
    Loads a YAML configuration file from user directories.

    Search order:
    1. Current working directory config/
    2. User config directory (~/.config/pylovo/ or PYLOVO_CONFIG_DIR)
    3. Project-local .pylovo/ directory

    Args:
        filename: Name of the config file (e.g., "config_database.yaml")

    Returns:
        Loaded configuration dictionary
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
    """Get required environment variable with clear error message if missing."""
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
    """Get integer environment variable with a typed fallback."""
    value = os.getenv(var_name)
    if value is None or value == "":
        return default

    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable '{var_name}' must be an integer, got: {value}") from exc

# =============================================================================
# PROJECT ROOT AND CONFIG LOADING
# =============================================================================
# Load Project Root (for backward compatibility and development)
PROJECT_ROOT = Path.cwd()  # Use current working directory as project root

# Load all configurations with new hybrid system
CONFIG_GENERATION = load_yaml_config("config_generation.yaml")
CONFIG_ANALYSIS = load_yaml_config("config_analysis.yaml")
CONFIG_CLASSIFICATION = load_yaml_config("config_classification.yaml")
CONFIG_CLUSTERING = load_yaml_config("config_clustering.yaml")

# =============================================================================
# DATABASE CONFIGURATION (from .env file)
# =============================================================================
# Load database connection configuration from .env file
load_dotenv(find_dotenv(), override=True)

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
else:
    INFDB_DBNAME = None
    INFDB_USER = None
    INFDB_HOST = None
    INFDB_PORT = None
    INFDB_PASSWORD = None
    INFDB_SOURCE_SCHEMA = None

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

# Parallel execution configuration
N_JOBS_PERCENT = CONFIG_GENERATION.get("N_JOBS_PERCENT", 50)
AVAILABLE_CORES = os.cpu_count() or 1
N_JOBS = max(1, round(AVAILABLE_CORES * N_JOBS_PERCENT / 100))

# Result directory configuration
RESULT_DIR = os.path.join(os.getcwd(), CONFIG_GENERATION.get("RESULT_DIR", "results"))

# Electrical backend configuration
ELECTRICAL_BACKEND = CONFIG_GENERATION.get("ELECTRICAL_BACKEND", "pandapower")

# =============================================================================
# GRID GENERATION CONFIGURATION (from CONFIG_GENERATION)
# =============================================================================
# Version information
VERSION_ID = CONFIG_GENERATION["VERSION_ID"]
VERSION_COMMENT = CONFIG_GENERATION["VERSION_COMMENT"]

# Load calculation parameters
PEAK_LOAD_HOUSEHOLD = CONFIG_GENERATION["PEAK_LOAD_HOUSEHOLD"]
SIM_FACTOR = CONFIG_GENERATION["SIM_FACTOR"]
DEFAULT_POWER_FACTOR = CONFIG_GENERATION["DEFAULT_POWER_FACTOR"]

# Consumer categories for load calculation
CONSUMER_CATEGORIES = pd.DataFrame(CONFIG_GENERATION["CONSUMER_CATEGORIES"])
# Patch: replace string placeholder references (e.g. 'PEAK_LOAD_HOUSEHOLD') with actual numeric value
if not CONSUMER_CATEGORIES.empty and "peak_load" in CONSUMER_CATEGORIES.columns:
    def _resolve_peak_load(val):
        if isinstance(val, str) and val.strip() == "PEAK_LOAD_HOUSEHOLD":
            return PEAK_LOAD_HOUSEHOLD
        return val
    CONSUMER_CATEGORIES["peak_load"] = CONSUMER_CATEGORIES["peak_load"].apply(_resolve_peak_load)
    # enforce numeric (None / null stay as NaN for categories using per m2 metrics)
    CONSUMER_CATEGORIES["peak_load"] = pd.to_numeric(CONSUMER_CATEGORIES["peak_load"], errors="coerce")

# Equipment data
TRANSFORMERS = pd.DataFrame(CONFIG_GENERATION["TRANSFORMERS"])
FEEDER_CABLES = pd.DataFrame(CONFIG_GENERATION["FEEDER_CABLES"])
CONSUMER_CONNECTION_CABLES = pd.DataFrame(CONFIG_GENERATION["CONSUMER_CONNECTION_CABLES"])

# Derived combined equipment table for database storage and shared consumers.
CONFIG_EQUIPMENT_DATA = pd.concat(
    [TRANSFORMERS, FEEDER_CABLES, CONSUMER_CONNECTION_CABLES],
    ignore_index=True,
)

# =============================================================================
# VOLTAGE PROPERTIES (from CONFIG_GENERATION)
# =============================================================================
VN = CONFIG_GENERATION["VN"]
V_BAND_LOW = CONFIG_GENERATION["V_BAND_LOW"]
V_BAND_HIGH = CONFIG_GENERATION["V_BAND_HIGH"]

# =============================================================================
# CABLE DIMENSIONING PARAMETERS (from CONFIG_GENERATION)
# =============================================================================
# Calculate maximum cable current from all configured cable pools (largest available cable)
# This ensures the current limit is always based on the actual largest configured cable.
ALL_CABLES = pd.concat([FEEDER_CABLES, CONSUMER_CONNECTION_CABLES], ignore_index=True)
MAX_CABLE_CURRENT_KA = ALL_CABLES["max_i_a"].max() / 1000  # Convert A to kA

# Load thresholds for different voltage drop limits
SMALL_LOAD_THRESHOLD_KW = CONFIG_GENERATION["SMALL_LOAD_THRESHOLD_KW"]

# Voltage drop limits for consumer connections (as percentage of nominal voltage per km)
VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM = CONFIG_GENERATION["VOLTAGE_DROP_SMALL_LOAD_PERCENT_PER_KM"]
VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM = CONFIG_GENERATION["VOLTAGE_DROP_LARGE_LOAD_PERCENT_PER_KM"]

# Voltage drop limit for feeder cables (total voltage drop as percentage of nominal voltage)
VOLTAGE_DROP_DISTRIBUTION_PERCENT = CONFIG_GENERATION["VOLTAGE_DROP_DISTRIBUTION_PERCENT"]

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

# =============================================================================
# GRID GENERATION PARAMETERS (from CONFIG_GENERATION)
# =============================================================================
MAX_BROWNFIELD_TRAFO_DISTANCE = CONFIG_GENERATION["MAX_BROWNFIELD_TRAFO_DISTANCE"]
MAX_GREENFIELD_TRAFO_DISTANCE = CONFIG_GENERATION["MAX_GREENFIELD_TRAFO_DISTANCE"]
LARGE_COMPONENT_LOWER_BOUND = CONFIG_GENERATION["LARGE_COMPONENT_LOWER_BOUND"]
MIN_SHARED_PREFIX_LENGTH_M = CONFIG_GENERATION.get("MIN_SHARED_PREFIX_LENGTH_M", 0)
LARGE_COMPONENT_DIVIDER = CONFIG_GENERATION["LARGE_COMPONENT_DIVIDER"]
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

# Setup seaborn palette
try:
    import seaborn as sns
    sns.set_palette(sns.color_palette(TUMPalette))
except ImportError:
    pass  # seaborn not installed, skip palette setup

