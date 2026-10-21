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


@pytest.mark.xfail(strict=True, reason="CLASSIF-03: generation and analysis delete rows of the configured classification's sample set")
def test_generation_and_analysis_do_not_touch_the_sample_set():
    for name in ("grid_generator.py", "analysis/parameter_calculation.py"):
        assert "delete_plz_from_sample_set_table" not in (SRC / name).read_text()


@pytest.mark.xfail(strict=True, reason="CLASSIF-01: pylovo-classify imports matplotlib, seaborn and plotly, which are only in the plots extra")
def test_cli_imports_without_the_plots_extra():
    code = ("import sys\nfor m in ('matplotlib', 'matplotlib.pyplot', 'seaborn', 'plotly', 'plotly.express'):\n"
            "    sys.modules[m] = None\nimport pylovo.cli.classify\n")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-300:]


def test_all_classification_modules_import():
    pytest.importorskip("matplotlib")
    pytest.importorskip("seaborn")
    pytest.importorskip("plotly")
    code = "import " + ", ".join(
        ["pylovo.cli.classify", "pylovo.classification.sampling.sample", "pylovo.classification.clustering.filter_grids",
         "pylovo.classification.clustering.prepare_data_for_clustering",
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


@pytest.mark.xfail(strict=True, reason="CLASSIF-13: 2_0_kmeans_representative_grids.ipynb plots six representative grids, the shipped config has four")
def test_kmeans_notebook_does_not_index_more_grids_than_the_shipped_config_has():
    import json

    from pylovo.config_loader import N_CLUSTERS_KMEANS

    nb = json.loads((REPO / "notebook_tutorials/classification/clustering/2_0_kmeans_representative_grids.ipynb").read_text())
    code = "\n".join("".join(c["source"]) for c in nb["cells"] if c["cell_type"] == "code")
    highest = max(int(i) for i in re.findall(r"representative_networks\.iloc\[(\d+)\]", code))
    assert highest < N_CLUSTERS_KMEANS
