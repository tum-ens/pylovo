"""Command-line entry point ``pylovo-classify``: interactive grid classification pipeline."""
import argparse

import yaml

from pylovo.config_loader import get_config_search_paths
from pylovo.classification.clustering.apply_clustering_for_visualisation import apply_clustering_for_visualisation
from pylovo.classification.clustering.get_no_clusters_for_clustering import get_no_clusters_for_clustering
from pylovo.classification.clustering.prepare_data_for_clustering import prepare_data_for_clustering
from pylovo.classification.clustering.get_parameters_for_clustering import get_parameters_for_clustering


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
    parser = argparse.ArgumentParser(
        prog="pylovo-classify",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Interactive classification of the generated grids into representative grid types.",
        epilog=(
            "The pipeline asks questions on the terminal and then:\n"
            "  1. creates the sample set of CLASSIFICATION_VERSION (config/config_classification.yaml),\n"
            "  2. imports buildings (USE_INFDB=False only), generates and analyses the sample grids,\n"
            "  3. filters the grids by the thresholds of config/config_clustering.yaml,\n"
            "  4. selects the clustering parameters and numbers of clusters (asked or computed)\n"
            "     and writes them to config/config_clustering.yaml,\n"
            "  5. clusters the grids and writes pylovo.transformer_classified.\n"
            "Set a new CLASSIFICATION_VERSION before each run; an existing version is rejected."
        ),
    )
    return parser.parse_args()


def load_yaml(filepath):
    """Load a YAML file."""
    with open(filepath, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def save_yaml(filepath, data):
    """Save a YAML file."""
    with open(filepath, "w", encoding="utf-8") as file:
        yaml.dump(data, file, default_flow_style=False, sort_keys=False)


def get_user_confirmation():
    """Asks the user if they want to manually assign clustering parameters."""
    while True:
        user_input = input("\nDo you prefer to assign clustering parameters and number of clusters? "
                           "Otherwise, optimal values will be calculated and assigned accordingly. (yes/no): ").strip().lower()
        if user_input in ["yes", "no"]:
            return user_input == "yes"
        print("Invalid input. Please enter 'yes' or 'no'.")


def get_custom_clustering_parameters():
    """Gets custom clustering parameters from user input."""
    while True:
        user_input = input("\nPlease enter desired clustering parameters (comma-separated). "
                           "For example: no_branches,max_no_of_households_of_a_branch,avg_trafo_dis\n> ").strip()
        params = [param.strip() for param in user_input.split(",") if param.strip()]
        if params:
            return params
        print("Invalid input. Please enter at least one parameter.")


def get_custom_cluster_numbers():
    """Gets custom cluster numbers for KMedoids, KMeans, and GMM from user input while ensuring valid ranges."""

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
        user_input = input(f"\nPlease enter desired number of clusters for KMedoid, KMeans, and GMM "
                           f"(comma-separated, allowed: {allowed_values}). "
                           f"For example: 4,5,4\n> ").strip()
        try:
            values = [int(num.strip()) for num in user_input.split(",") if num.strip()]

            # Check if exactly 3 values are provided
            if len(values) != 3:
                print("Invalid input. Please enter exactly 3 integer values separated by commas.")
                continue

            # Check if all values are within the allowed range
            if all(val in allowed_values_set for val in values):
                return values

            print(f"Invalid input. All values must be within the allowed range {min_val}-{max_val}. Try again.")

        except ValueError:
            print("Invalid input. Please enter numeric values only.")


def update_list_of_clustering_parameters() -> list:
    """Runs get_parameters_for_clustering and updates LIST_OF_CLUSTERING_PARAMETERS in config_clustering.yaml.

    Returns:
        list: The selected clustering parameters.
    """
    params = get_parameters_for_clustering()

    # Update YAML file
    config = load_yaml(CONFIG_CLUSTERING_PATH)
    config["LIST_OF_CLUSTERING_PARAMETERS"] = params
    save_yaml(CONFIG_CLUSTERING_PATH, config)
    print("LIST_OF_CLUSTERING_PARAMETERS updated in config_clustering.yaml")
    return params


def update_number_of_clusters(list_of_clustering_parameters: list | None = None):
    """Runs get_no_clusters_for_clustering and updates cluster numbers in config_clustering.yaml.

    Args:
        list_of_clustering_parameters: Parameters to evaluate the CH index on;
            ``None`` uses LIST_OF_CLUSTERING_PARAMETERS loaded at start-up.
    """
    df_no_clusters = get_no_clusters_for_clustering(list_of_clustering_parameters)
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
        "N_CLUSTERS_KMEDOID": get_no_clusters_from_df(["KMedoids", "kmedoids", "kmedoid"], "N_CLUSTERS_KMEDOID"),
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

    # Step 1: Ensure user has configured `config_classification.yaml`
    config_classification = load_yaml(CONFIG_CLASSIFICATION_PATH)
    print(f"Using classification version: {config_classification['CLASSIFICATION_VERSION']}")

    # Step 2: Ask user if they want to apply additional filtering then Run prepare_data_for_clustering.py
    user_input = input("\nDo you want to apply additional filtering on top of the default filters? (yes/no): ").strip().lower()
    apply_additional_filtering = user_input == "yes"

    user_input = input(
        "\nDo you want to restrict sampling to PLZ present in postcode_result for the configured VERSION_ID? (yes/no): "
    ).strip().lower()
    sample_from_postcode_result_only = user_input == "yes"

    print("\nRunning prepare_data_for_clustering.py...")
    prepare_data_for_clustering(
        additional_filtering=apply_additional_filtering,
        sample_from_postcode_result_only=sample_from_postcode_result_only,
    )

    # Step 3: Ask user for manual input or automatic assignment
    if get_user_confirmation():
        # User wants to enter clustering parameters manually
        clustering_parameters = get_custom_clustering_parameters()
        cluster_numbers = get_custom_cluster_numbers()

        # Update YAML file manually
        config = load_yaml(CONFIG_CLUSTERING_PATH)
        config["LIST_OF_CLUSTERING_PARAMETERS"] = clustering_parameters
        config["N_CLUSTERS_KMEDOID"], config["N_CLUSTERS_KMEANS"], config["N_CLUSTERS_GMM"] = cluster_numbers
        save_yaml(CONFIG_CLUSTERING_PATH, config)

        print("\nManually assigned clustering parameters and number of clusters updated in config_clustering.yaml")
    else:
        # Step 4: Automatically update clustering parameters and cluster numbers
        print("\nGetting parameters for clustering...")
        clustering_parameters = update_list_of_clustering_parameters()

        print("\nGetting number of clusters for clustering...")
        update_number_of_clusters(clustering_parameters)

    # Step 5: Run apply_clustering_for_visualisation.py with the values just written;
    # pylovo.config_loader still holds the values loaded at start-up.
    print("\nRunning apply_clustering_for_visualisation.py...")
    config = load_yaml(CONFIG_CLUSTERING_PATH)
    apply_clustering_for_visualisation(
        list_of_clustering_parameters=config["LIST_OF_CLUSTERING_PARAMETERS"],
        n_clusters_kmeans=config["N_CLUSTERS_KMEANS"],
        n_clusters_gmm=config["N_CLUSTERS_GMM"],
    )

    print("\nClassification process completed successfully!")


if __name__ == "__main__":
    main()

