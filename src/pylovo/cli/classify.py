"""Command-line entry point ``pylovo-classify``: grid classification pipeline, interactive or scripted with options."""
import argparse
import re
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
    from pylovo.classification.clustering.report_grid_counts import report_grid_counts
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
    """Parse the command line. Every option answers one question of the pipeline; unanswered ones are asked."""
    parser = parsers.classify_parser()
    args = parser.parse_args()
    if args.manual and not (args.parameters and args.clusters):
        parser.error("--manual needs --parameters and --clusters")
    if (args.parameters or args.clusters) and not args.manual:
        parser.error("--parameters and --clusters need --manual")
    return args


def load_yaml(filepath):
    """Load a YAML file."""
    with open(filepath, "r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def _dump_key(key, value) -> str:
    """Return ``key: value`` as YAML text, a list as a block sequence."""
    return yaml.dump({key: value}, default_flow_style=False, sort_keys=False)


def _replace_key_text(text: str, key: str, value) -> str:
    """Replace the value of the top-level ``key`` in YAML ``text``.

    The comment on the key line and the indentation of a block sequence stay. Comments between the items of a
    replaced list are lost.
    """
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if re.match(rf"{re.escape(key)}\s*:", line)), None)
    if start is None:
        raise ValueError(f"Key {key!r} is not a plain top-level key of the file.")
    last = start
    items = []
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if re.match(r"-(\s|$)", stripped):  # an item of the block sequence that is the value
            last = i
            items.append(lines[i])
        elif stripped and not stripped.startswith("#"):  # the next key
            break
    comment = re.search(r"\s+#.*$", lines[start].split(":", 1)[1].rstrip("\r\n"))
    indent = re.match(r"\s*", items[0]).group() if items else ""
    new_lines = _dump_key(key, value).splitlines(keepends=True)
    new_lines[0] = new_lines[0].rstrip("\n") + (comment.group() if comment else "") + "\n"
    new_lines[1:] = [indent + line for line in new_lines[1:]]
    lines[start:last + 1] = new_lines
    return "".join(lines)


def save_yaml(filepath, data):
    """Write the values of ``data`` to a YAML file and change nothing else.

    Only the text of the top-level keys whose value differs is rewritten, so comments, key order and formatting stay.
    A key that is not in the file yet is appended. Values are scalars or lists of scalars without `` #``.

    Args:
        filepath: The YAML file.
        data: Key-value pairs, usually the loaded content with some values changed.

    Raises:
        ValueError: If a changed key is not a plain top-level key, or the file holds other values than ``data``
            after the edit (a layout this edit does not support). The file is not written then.
    """
    with open(filepath, "r", encoding="utf-8") as file:
        original = file.read()
    current = yaml.safe_load(original) or {}
    text = original
    for key, value in data.items():
        if key not in current:
            text += ("" if not text or text.endswith("\n") else "\n") + _dump_key(key, value)
        elif current[key] != value:
            text = _replace_key_text(text, key, value)
    try:
        saved = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        saved = {}
    if any(saved.get(key) != value for key, value in data.items()):
        raise ValueError(f"Cannot update {filepath} in place: unsupported layout of a changed key. "
                         "Edit the file by hand.")
    if text != original:
        with open(filepath, "w", encoding="utf-8") as file:
            file.write(text)


def _ask(prompt: str) -> str:
    """Read one answer from the terminal; a closed standard input ends the run with a message."""
    try:
        return input(prompt).strip()
    except EOFError:
        sys.exit("\nNo more input (standard input is closed), stopping. "
                 "The options of 'pylovo-classify --help' answer the questions without input.")


def ask_yes_no(question: str) -> bool:
    """Ask a yes/no question until the answer is y, yes, n or no (any case)."""
    while True:
        answer = _ask(f"\n{question} (yes/no): ").lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Invalid input. Please enter 'yes' or 'no'.")


def parse_parameter_names(text: str) -> list:
    """Split a comma-separated list of clustering parameters and check the names.

    Raises:
        ValueError: If the list is empty or a name is not a column of the parameter table.
    """
    allowed = [name for name in CLUSTERING_PARAMETERS if name not in IDENTIFIER_COLUMNS]
    params = [param.strip() for param in text.split(",") if param.strip()]
    unknown = [param for param in params if param not in allowed]
    if not params:
        raise ValueError("Please enter at least one parameter.")
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(unknown)}. Allowed: {', '.join(allowed)}.")
    return params


def get_custom_clustering_parameters():
    """Gets custom clustering parameters from user input; the names must be columns of the parameter table."""
    while True:
        user_input = _ask("\nPlease enter desired clustering parameters (comma-separated). "
                          "For example: no_branches,max_no_of_households_of_a_branch,avg_trafo_dis\n> ")
        try:
            return parse_parameter_names(user_input)
        except ValueError as exc:
            print(f"Invalid input. {exc}")


def _allowed_cluster_numbers() -> list:
    """Return NO_OF_CLUSTERS_ALLOWED of config_classification.yaml."""
    allowed_values = load_yaml(CONFIG_CLASSIFICATION_PATH).get("NO_OF_CLUSTERS_ALLOWED", [])
    if not allowed_values:
        print("Warning: NO_OF_CLUSTERS_ALLOWED is missing or empty in config_classification.yaml. Defaulting to [3, 4, 5, 6, 7].")
        allowed_values = [3, 4, 5, 6, 7]  # Fallback in case the value is missing
    return allowed_values


def parse_cluster_numbers(text: str, allowed_values: list) -> list:
    """Split two comma-separated cluster numbers (k-means, GMM) and check them against the allowed values.

    Raises:
        ValueError: If they are not exactly two integers from ``allowed_values``.
    """
    try:
        values = [int(num.strip()) for num in text.split(",") if num.strip()]
    except ValueError:
        raise ValueError("Please enter numeric values only.") from None
    if len(values) != 2:
        raise ValueError("Please enter exactly 2 integer values separated by commas.")
    if not all(val in allowed_values for val in values):
        raise ValueError(f"All values must be in NO_OF_CLUSTERS_ALLOWED {allowed_values}.")
    return values


def get_custom_cluster_numbers():
    """Gets custom cluster numbers for KMeans and GMM from user input while ensuring valid ranges."""
    allowed_values = _allowed_cluster_numbers()
    while True:
        user_input = _ask(f"\nPlease enter desired number of clusters for KMeans and GMM "
                          f"(comma-separated, allowed: {allowed_values}). "
                          f"For example: 4,5\n> ")
        try:
            return parse_cluster_numbers(user_input, allowed_values)
        except ValueError as exc:
            print(f"Invalid input. {exc}")


def select_number_of_clusters(list_of_clustering_parameters: list | None = None,
                              additional_filtering: bool | None = None) -> dict:
    """Run get_no_clusters_for_clustering, print the Calinski-Harabasz table and return the chosen numbers.

    Args:
        list_of_clustering_parameters: Parameters to evaluate the CH index on;
            ``None`` uses LIST_OF_CLUSTERING_PARAMETERS loaded at start-up.
        additional_filtering: Also leave out the grids below the four lower thresholds.

    Returns:
        dict: ``N_CLUSTERS_KMEANS`` and ``N_CLUSTERS_GMM``, the best numbers of k-means and of the tied GMM.
    """
    df_no_clusters = get_no_clusters_for_clustering(list_of_clustering_parameters, additional_filtering)
    print("Calinski-Harabasz index at the best number of clusters of each algorithm (higher is better):")
    print(df_no_clusters.to_string(index=False))
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

    cluster_counts = {
        "N_CLUSTERS_KMEANS": get_no_clusters_from_df(["kmeans", "KMeans"], "N_CLUSTERS_KMEANS"),
        "N_CLUSTERS_GMM": get_no_clusters_from_df(["GMM tied", "gmm_tied", "gmm tied"], "N_CLUSTERS_GMM"),
    }
    print("Selected number of clusters: " + ", ".join(f"{key}={value}" for key, value in cluster_counts.items()))
    return cluster_counts


def main():
    """Run the classification pipeline (see ``pylovo-classify --help``)."""
    args = _parse_args()
    print("Running classification pipeline...")

    # Check the scripted values first: a typo must not cost the hours of a run.
    scripted = None
    if args.manual:
        try:
            scripted = (parse_parameter_names(args.parameters),
                        parse_cluster_numbers(args.clusters, _allowed_cluster_numbers()))
        except ValueError as exc:
            sys.exit(f"pylovo-classify: {exc}")

    # Step 1: Ensure user has configured `config_classification.yaml`; an existing version is rejected
    # before any question is asked.
    config_classification = load_yaml(CONFIG_CLASSIFICATION_PATH)
    version = config_classification['CLASSIFICATION_VERSION']
    print(f"Using classification version: {version}")
    if classification_version_exists():
        sys.exit(f"Classification version {version} already exists. Set a new CLASSIFICATION_VERSION in "
                 f"{CONFIG_CLASSIFICATION_PATH} (or delete the old one with 'pylovo-delete classification').")

    # Step 2: Ask user if they want to apply additional filtering then Run prepare_data_for_clustering.py
    apply_additional_filtering = args.additional_filtering
    if apply_additional_filtering is None:
        apply_additional_filtering = ask_yes_no(
            "Do you want to apply additional filtering on top of the default filters?")
    sample_from_postcode_result_only = args.restrict_to_generated
    if sample_from_postcode_result_only is None:
        sample_from_postcode_result_only = ask_yes_no(
            "Do you want to restrict sampling to PLZ present in postcode_result for the configured VERSION_ID?")

    print("\nRunning prepare_data_for_clustering.py...")
    plz_without_grids = prepare_data_for_clustering(sample_from_postcode_result_only=sample_from_postcode_result_only)

    # Step 3: Manual input or automatic assignment
    manual = args.manual or (not args.auto and ask_yes_no(
        "Do you prefer to assign clustering parameters and number of clusters? "
        "Otherwise, optimal values will be calculated and assigned accordingly."))
    if manual:
        if scripted is None:
            clustering_parameters = get_custom_clustering_parameters()
            cluster_numbers = get_custom_cluster_numbers()
        else:
            clustering_parameters, cluster_numbers = scripted
        cluster_counts = dict(zip(("N_CLUSTERS_KMEANS", "N_CLUSTERS_GMM"), cluster_numbers))
    else:
        # Step 4: Automatically select clustering parameters and cluster numbers
        print("\nGetting parameters for clustering...")
        clustering_parameters = get_parameters_for_clustering(apply_additional_filtering)
        print(f"Selected clustering parameters: {', '.join(clustering_parameters)}")

        print("\nGetting number of clusters for clustering...")
        cluster_counts = select_number_of_clusters(clustering_parameters, apply_additional_filtering)

    # The filters are applied when the grids are read: the answer belongs to the configuration of this classification.
    config = load_yaml(CONFIG_CLUSTERING_PATH)
    config["LIST_OF_CLUSTERING_PARAMETERS"] = clustering_parameters
    config.update(cluster_counts)
    config["ADDITIONAL_FILTERING"] = apply_additional_filtering
    save_yaml(CONFIG_CLUSTERING_PATH, config)
    print("\nClustering parameters, number of clusters and the filter choice written to config_clustering.yaml")

    # Step 5: Run apply_clustering_for_visualisation.py with the values just written;
    # pylovo.config_loader still holds the values loaded at start-up.
    print("\nRunning apply_clustering_for_visualisation.py...")
    apply_clustering_for_visualisation(
        list_of_clustering_parameters=clustering_parameters,
        n_clusters_kmeans=cluster_counts["N_CLUSTERS_KMEANS"],
        n_clusters_gmm=cluster_counts["N_CLUSTERS_GMM"],
        additional_filtering=apply_additional_filtering,
    )

    report_grid_counts(additional_filtering=apply_additional_filtering, plz_without_grids=plz_without_grids)
    print("\nClassification process completed successfully!")


if __name__ == "__main__":
    main()
