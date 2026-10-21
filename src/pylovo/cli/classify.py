"""Command-line entry point ``pylovo-classify``: interactive grid classification pipeline."""
import argparse
import sys

import yaml

from pylovo.cli import parsers
from pylovo.config_loader import CLUSTERING_PARAMETERS, get_config_search_paths

# Packages of the ``plots`` extra that the classification code imports.
PLOTS_EXTRA_PACKAGES = ("matplotlib", "mpl_toolkits", "seaborn", "plotly")

try:
    from pylovo.classification.clustering.apply_clustering_for_visualisation import apply_clustering_for_visualisation
    from pylovo.classification.clustering.get_no_clusters_for_clustering import get_no_clusters_for_clustering
    from pylovo.classification.clustering.prepare_data_for_clustering import prepare_data_for_clustering
    from pylovo.classification.clustering.get_parameters_for_clustering import get_parameters_for_clustering
    from pylovo.classification.sampling.sample import classification_version_exists
except ModuleNotFoundError as exc:
    if (exc.name or "").split(".")[0] not in PLOTS_EXTRA_PACKAGES:
        raise
    sys.exit(f"pylovo-classify needs the plotting packages of the 'plots' extra ('{exc.name}' is missing). "
             "Install them with 'uv sync --extra plots' (pip: 'pip install pylovo[plots]') "
             "or start the command with 'uv run --extra plots pylovo-classify'.")

# Columns of the clustering parameter table that identify a grid; they cannot be clustered.
IDENTIFIER_COLUMNS = ("version_id", "plz", "bcid", "kcid")


def _config_file(filename: str) -> str:
    """Return the path of the config file that ``pylovo.config_loader`` reads.

    It is the first match in the config search paths (``./config`` first), so the
    values written here are the ones pylovo uses in later runs.
    """
    for search_path in get_config_search_paths():
        candidate = search_path / filename
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"Config file '{filename}' not found in {get_config_search_paths()}")


# Paths to the YAML config files that the pipeline reads and updates
CONFIG_CLASSIFICATION_PATH = _config_file("config_classification.yaml")
CONFIG_CLUSTERING_PATH = _config_file("config_clustering.yaml")


def _parse_args() -> argparse.Namespace:
    """Parse the command line; the pipeline itself takes no options."""
    parser = parsers.classify_parser()
    return parser.parse_args()


def load_yaml(filepath):
    """Load a YAML file."""
    with open(filepath, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def save_yaml(filepath, data):
    """Save a YAML file."""
    with open(filepath, "w", encoding="utf-8") as file:
        yaml.dump(data, file, default_flow_style=False, sort_keys=False)


def _ask(prompt: str) -> str:
    """Read one answer from the terminal; a closed standard input ends the run with a message."""
    try:
        return input(prompt).strip()
    except EOFError:
        sys.exit("\nNo more input (standard input is closed), stopping.")


def ask_yes_no(question: str) -> bool:
    """Ask a yes/no question until the answer is y, yes, n or no (any case)."""
    while True:
        answer = _ask(f"\n{question} (yes/no): ").lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Invalid input. Please enter 'yes' or 'no'.")


def get_custom_clustering_parameters():
    """Gets custom clustering parameters from user input; the names must be columns of the parameter table."""
    allowed = [name for name in CLUSTERING_PARAMETERS if name not in IDENTIFIER_COLUMNS]
    while True:
        user_input = _ask("\nPlease enter desired clustering parameters (comma-separated). "
                          "For example: no_branches,max_no_of_households_of_a_branch,avg_trafo_dis\n> ")
        params = [param.strip() for param in user_input.split(",") if param.strip()]
        unknown = [param for param in params if param not in allowed]
        if not params:
            print("Invalid input. Please enter at least one parameter.")
        elif unknown:
            print(f"Invalid input. Unknown parameter(s): {', '.join(unknown)}. Allowed: {', '.join(allowed)}.")
        else:
            return params


def get_custom_cluster_numbers():
    """Gets custom cluster numbers for KMeans and GMM from user input while ensuring valid ranges."""

    # Load allowed cluster values from config_classification.yaml
    config_classification = load_yaml(CONFIG_CLASSIFICATION_PATH)
    allowed_values = config_classification.get("NO_OF_CLUSTERS_ALLOWED", [])

    if not allowed_values:
        print("Warning: NO_OF_CLUSTERS_ALLOWED is missing or empty in config_classification.yaml. Defaulting to [3, 4, 5, 6, 7].")
        allowed_values = [3, 4, 5, 6, 7]  # Fallback in case the value is missing

    # Convert to a set for quick lookup
    allowed_values_set = set(allowed_values)
    min_val, max_val = min(allowed_values), max(allowed_values)

    while True:
        user_input = _ask(f"\nPlease enter desired number of clusters for KMeans and GMM "
                          f"(comma-separated, allowed: {allowed_values}). "
                          f"For example: 4,5\n> ")
        try:
            values = [int(num.strip()) for num in user_input.split(",") if num.strip()]

            # Check if exactly 2 values are provided
            if len(values) != 2:
                print("Invalid input. Please enter exactly 2 integer values separated by commas.")
                continue

            # Check if all values are within the allowed range
            if all(val in allowed_values_set for val in values):
                return values

            print(f"Invalid input. All values must be within the allowed range {min_val}-{max_val}. Try again.")

        except ValueError:
            print("Invalid input. Please enter numeric values only.")


def update_list_of_clustering_parameters(additional_filtering: bool | None = None) -> list:
    """Runs get_parameters_for_clustering and updates LIST_OF_CLUSTERING_PARAMETERS in config_clustering.yaml.

    Args:
        additional_filtering: Also leave out the grids below the four lower thresholds.

    Returns:
        list: The selected clustering parameters.
    """
    params = get_parameters_for_clustering(additional_filtering)

    # Update YAML file
    config = load_yaml(CONFIG_CLUSTERING_PATH)
    config["LIST_OF_CLUSTERING_PARAMETERS"] = params
    save_yaml(CONFIG_CLUSTERING_PATH, config)
    print("LIST_OF_CLUSTERING_PARAMETERS updated in config_clustering.yaml")
    return params


def update_number_of_clusters(list_of_clustering_parameters: list | None = None, additional_filtering: bool | None = None):
    """Runs get_no_clusters_for_clustering and updates cluster numbers in config_clustering.yaml.

    Args:
        list_of_clustering_parameters: Parameters to evaluate the CH index on;
            ``None`` uses LIST_OF_CLUSTERING_PARAMETERS loaded at start-up.
        additional_filtering: Also leave out the grids below the four lower thresholds.
    """
    df_no_clusters = get_no_clusters_for_clustering(list_of_clustering_parameters, additional_filtering)
    config = load_yaml(CONFIG_CLUSTERING_PATH)

    def get_no_clusters_from_df(algo_names: list[str], fallback_key: str) -> int:
        for algo in algo_names:
            match = df_no_clusters[df_no_clusters["algorithm"] == algo]["no_clusters"]
            if not match.empty:
                return int(match.iloc[0])

        fallback = int(config[fallback_key])
        print(
            f"Warning: no cluster recommendation found for {algo_names}. "
            f"Keeping existing {fallback_key}={fallback}."
        )
        return fallback

    # Define the direct mapping from algorithm names to YAML keys
    cluster_counts = {
        "N_CLUSTERS_KMEANS": get_no_clusters_from_df(["kmeans", "KMeans"], "N_CLUSTERS_KMEANS"),
        "N_CLUSTERS_GMM": get_no_clusters_from_df(["GMM tied", "gmm_tied", "gmm tied"], "N_CLUSTERS_GMM"),
    }

    # Update YAML configuration with extracted values
    config.update(cluster_counts)

    # Save updated YAML file
    save_yaml(CONFIG_CLUSTERING_PATH, config)
    print("Number of clusters updated in config_clustering.yaml")


def main():
    """Run the interactive classification pipeline (see ``pylovo-classify --help``)."""
    _parse_args()
    print("Running classification pipeline...")

    # Step 1: Ensure user has configured `config_classification.yaml`; an existing version is rejected
    # before any question is asked.
    config_classification = load_yaml(CONFIG_CLASSIFICATION_PATH)
    version = config_classification['CLASSIFICATION_VERSION']
    print(f"Using classification version: {version}")
    if classification_version_exists():
        sys.exit(f"Classification version {version} already exists. Set a new CLASSIFICATION_VERSION in "
                 f"{CONFIG_CLASSIFICATION_PATH} (or delete the old one with 'pylovo-delete classification').")

    # Step 2: Ask user if they want to apply additional filtering then Run prepare_data_for_clustering.py
    apply_additional_filtering = ask_yes_no("Do you want to apply additional filtering on top of the default filters?")
    sample_from_postcode_result_only = ask_yes_no(
        "Do you want to restrict sampling to PLZ present in postcode_result for the configured VERSION_ID?"
    )

    print("\nRunning prepare_data_for_clustering.py...")
    prepare_data_for_clustering(sample_from_postcode_result_only=sample_from_postcode_result_only)

    # The filters are applied when the grids are read: the answer belongs to the configuration of this classification.
    config = load_yaml(CONFIG_CLUSTERING_PATH)
    config["ADDITIONAL_FILTERING"] = apply_additional_filtering
    save_yaml(CONFIG_CLUSTERING_PATH, config)

    # Step 3: Ask user for manual input or automatic assignment
    if ask_yes_no("Do you prefer to assign clustering parameters and number of clusters? "
                  "Otherwise, optimal values will be calculated and assigned accordingly."):
        # User wants to enter clustering parameters manually
        clustering_parameters = get_custom_clustering_parameters()
        cluster_numbers = get_custom_cluster_numbers()

        # Update YAML file manually
        config = load_yaml(CONFIG_CLUSTERING_PATH)
        config["LIST_OF_CLUSTERING_PARAMETERS"] = clustering_parameters
        config["N_CLUSTERS_KMEANS"], config["N_CLUSTERS_GMM"] = cluster_numbers
        save_yaml(CONFIG_CLUSTERING_PATH, config)

        print("\nManually assigned clustering parameters and number of clusters updated in config_clustering.yaml")
    else:
        # Step 4: Automatically update clustering parameters and cluster numbers
        print("\nGetting parameters for clustering...")
        clustering_parameters = update_list_of_clustering_parameters(apply_additional_filtering)

        print("\nGetting number of clusters for clustering...")
        update_number_of_clusters(clustering_parameters, apply_additional_filtering)

    # Step 5: Run apply_clustering_for_visualisation.py with the values just written;
    # pylovo.config_loader still holds the values loaded at start-up.
    print("\nRunning apply_clustering_for_visualisation.py...")
    config = load_yaml(CONFIG_CLUSTERING_PATH)
    apply_clustering_for_visualisation(
        list_of_clustering_parameters=config["LIST_OF_CLUSTERING_PARAMETERS"],
        n_clusters_kmeans=config["N_CLUSTERS_KMEANS"],
        n_clusters_gmm=config["N_CLUSTERS_GMM"],
        additional_filtering=apply_additional_filtering,
    )

    print("\nClassification process completed successfully!")


if __name__ == "__main__":
    main()

