"""Packaging of the core install, removed-module references, sample-set bookkeeping and notebook API compatibility."""
import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from classification_notebook_check import check as check_notebook

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "pylovo"
CLASSIFICATION_FILES = [*sorted((SRC / "classification").rglob("*.py")), SRC / "cli" / "classify.py",
                        *sorted((SRC / "plotting" / "classification").rglob("*.py"))]


def test_generation_and_analysis_do_not_touch_the_sample_set():
    for name in ("grid_generator.py", "analysis/parameter_calculation.py"):
        assert "delete_plz_from_sample_set_table" not in (SRC / name).read_text()


def test_cli_without_the_plots_extra_stops_with_a_message():
    """The plotting stack is only in the ``plots`` extra; ``pylovo-classify`` must say so instead of a traceback."""
    code = ("import sys\nfor m in ('matplotlib', 'matplotlib.pyplot', 'seaborn', 'plotly', 'plotly.express'):\n"
            "    sys.modules[m] = None\nimport pylovo.cli.classify\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0
    assert "--extra plots" in result.stderr and "Traceback" not in result.stderr, result.stderr[-400:]


def test_cli_does_not_hide_other_import_errors():
    code = ("import sys\nsys.modules['factor_analyzer'] = None\nimport pylovo.cli.classify\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert result.returncode != 0 and "ModuleNotFoundError" in result.stderr and "--extra plots" not in result.stderr


def test_all_classification_modules_import():
    pytest.importorskip("matplotlib")
    pytest.importorskip("seaborn")
    pytest.importorskip("plotly")
    code = "import " + ", ".join(
        ["pylovo.cli.classify", "pylovo.classification.sampling.sample",
         "pylovo.classification.clustering.prepare_data_for_clustering", "pylovo.classification.clustering.report_grid_counts",
         "pylovo.classification.clustering.get_parameters_for_clustering",
         "pylovo.classification.clustering.get_no_clusters_for_clustering",
         "pylovo.classification.clustering.apply_clustering_for_visualisation",
         "pylovo.classification.utils.get_average_values_clustering_parameters", "pylovo.plotting.classification"])
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]


def test_no_reference_to_removed_modules_or_kmedoids_library():
    removed = ("sklearn_extra", "calc_deviation_of_clustering_method", "replace_plz", "kmedoids_clustering")
    for path in CLASSIFICATION_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or "", *[a.name for a in node.names]]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not any(r in n for n in names for r in removed), (path, names)


def test_imports_of_the_classification_code_are_declared_or_in_the_plots_extra():
    import tomllib

    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    declared = {re.split(r"[<>=!\[ ]", d, maxsplit=1)[0].lower().replace("-", "_")
                for d in [*project["dependencies"], *project["optional-dependencies"]["plots"]]}
    declared |= {"sklearn": "scikit_learn", "yaml": "pyyaml", "mpl_toolkits": "matplotlib", "dotenv": "python_dotenv"}.keys()
    alias = {"sklearn": "scikit_learn", "yaml": "pyyaml", "mpl_toolkits": "matplotlib", "dotenv": "python_dotenv"}
    stdlib = sys.stdlib_module_names
    undeclared = set()
    for path in CLASSIFICATION_FILES:
        for node in ast.walk(ast.parse(path.read_text())):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for module in modules:
                top = module.split(".")[0]
                if top in stdlib or top == "pylovo":
                    continue
                if alias.get(top, top).lower().replace("-", "_") not in declared:
                    undeclared.add(top)
    assert not undeclared, undeclared


NOTEBOOKS = sorted((REPO / "notebook_tutorials" / "classification").rglob("*.ipynb"))


def test_classification_notebooks_use_existing_pylovo_api():
    pytest.importorskip("matplotlib")
    pytest.importorskip("seaborn")
    pytest.importorskip("plotly")
    assert NOTEBOOKS, "notebooks not found"
    problems = {str(nb.relative_to(REPO)): check_notebook(nb) for nb in NOTEBOOKS}
    assert not {k: v for k, v in problems.items() if v}


def test_kmeans_notebook_plots_every_cluster_instead_of_hard_coded_positions():
    """CLASSIF-13: the notebook indexed six representative grids, but the number of k-means clusters is 3 to 7."""
    import json

    nb = json.loads((REPO / "notebook_tutorials/classification/clustering/2_0_kmeans_representative_grids.ipynb").read_text())
    code = "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
    assert not re.findall(r"representative_networks\.iloc\[\d+\]", code)
    assert "representative_networks.iterrows()" in code


def notebook_code(path: Path) -> str:
    import json

    nb = json.loads(path.read_text(encoding="utf-8"))
    return "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")


def test_representative_notebooks_show_every_cluster_and_select_columns_by_name():
    """The GMM notebook hard-coded four representatives; both discarded the plotly figures of their loop."""
    for name in ("2_0_kmeans_representative_grids", "3_0_gmm_representative_grids"):
        code = notebook_code(REPO / f"notebook_tutorials/classification/clustering/{name}.ipynb")
        assert not re.findall(r"representative_networks\.iloc\[\d+\]", code), name
        assert "representative_networks.iterrows()" in code, name
        assert "display(plot_simple_grid(" in code and "display(plot_with_generic_coordinates(" in code, name
    for nb in NOTEBOOKS:
        assert not re.search(r"\.iloc\[\s*:\s*,\s*\[", notebook_code(nb)), f"columns by position in {nb.name}"


NEW_NOTEBOOKS = [REPO / "notebook_tutorials/classification/01_sample_and_key_figures.ipynb",
                 REPO / "notebook_tutorials/classification/02_clusters_and_representatives.ipynb"]
GERMAN = re.compile(r"Anzahl|Klasse|Stichprobe|Anteil|Verteilung|Erkl[äa]rt|Kumulativ|Algorithmus|Gemeindetyp")


@pytest.mark.parametrize("path", NEW_NOTEBOOKS, ids=lambda path: path.name)
def test_new_notebooks_have_one_parameter_cell_no_warning_filter_and_no_sql(path):
    import json

    nb = json.loads(path.read_text(encoding="utf-8"))
    tagged = [c for c in nb["cells"] if "parameters" in c.get("metadata", {}).get("tags", [])]
    assert len(tagged) == 1 and tagged[0]["cell_type"] == "code"
    code = notebook_code(path)
    assert "filterwarnings" not in code and "simplefilter" not in code, "warnings stay visible"
    assert not re.search(r"\bSELECT\b|\bFROM pylovo\.|get_geo_df_join|\._\w+\(", code), "public API only"
    assert not GERMAN.search("".join("".join(c["source"]) for c in nb["cells"]))
    for cell in nb["cells"]:
        for output in cell.get("outputs", []):
            assert output["output_type"] != "error"
            assert not (output["output_type"] == "stream" and output["name"] == "stderr"), "a warning was stored"
            assert not any("plotly" in mime for mime in output.get("data", {})), "static figures only"


@pytest.mark.parametrize("path", NOTEBOOKS, ids=lambda path: path.name)
def test_stored_outputs_contain_no_address_home_path_or_credential(path):
    """The notebooks are published: the failure names the kind of leak, never the value."""
    import json

    from pylovo import config_loader

    nb = json.loads(path.read_text(encoding="utf-8"))
    parts = []
    for cell in nb["cells"]:
        for output in cell.get("outputs", []):
            parts += [json.dumps(value) for key, value in output.items() if key not in ("data", "metadata")]
            parts += [json.dumps(value) for mime, value in output.get("data", {}).items()
                      if not mime.startswith("image/")]
    text = "\n".join(parts)
    leaks = [name for name, pattern in (("IPv4 address", r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
                                        ("home path", r"/home/|/Users/|[A-Z]:\\\\Users"))
             if re.search(pattern, text)]
    for name in ("HOST", "INFDB_HOST", "DBUSER", "INFDB_USER", "PASSWORD", "INFDB_PASSWORD"):
        value = str(getattr(config_loader, name, None) or "")
        # whole words of four characters or more: the CI values ("ci") are parts of ordinary words
        if (len(value) >= 4 and value not in ("localhost", "127.0.0.1", "postgres")
                and re.search(rf"(?<![\w.-]){re.escape(value)}(?![\w-])", text)):
            leaks.append(f"value of {name}")
    assert not leaks, leaks
