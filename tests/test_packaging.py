"""Packaging metadata of pyproject.toml against the files the code loads and the modules it imports."""
import ast
import importlib.metadata
import re
import sys
import tomllib
from fnmatch import fnmatch
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_sql_file_of_the_package_is_in_the_wheel_and_sdist():
    patterns = PYPROJECT["tool"]["hatch"]["build"]["include"]
    sql_files = [path.relative_to(ROOT).as_posix() for path in (ROOT / "src" / "pylovo").rglob("*.sql")]
    assert sql_files
    missing = [name for name in sql_files if not any(fnmatch(name, pattern) for pattern in patterns)]
    assert not missing, f"SQL files that DatabaseConstructor loads but the build leaves out: {missing}"


def test_requires_python_matches_the_interpreter_and_the_numba_wheels():
    # numba 0.62.1 and llvmlite 0.45.1 have no Python 3.14 wheels: lift the cap together with the numba bump.
    supported = SpecifierSet(PYPROJECT["project"]["requires-python"])
    assert (ROOT / ".python-version").read_text().strip() in supported
    assert "3.13" in supported
    assert "3.14" not in supported


def declared_packages(extra):
    """Canonical names of the core dependencies and of one extra."""
    requirements = PYPROJECT["project"]["dependencies"] + PYPROJECT["project"]["optional-dependencies"][extra]
    return {canonicalize_name(Requirement(requirement).name) for requirement in requirements}


def imported_packages(folder):
    """Distributions that the modules below ``folder`` import outside ``try`` blocks (optional imports)."""
    modules = set()
    for path in folder.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        optional = {id(node) for block in ast.walk(tree) if isinstance(block, ast.Try)
                    for statement in block.body for node in ast.walk(statement)}
        for node in ast.walk(tree):
            if id(node) in optional:
                continue
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                modules.add(node.module.split(".")[0])
    modules -= set(sys.stdlib_module_names) | {"pylovo", "pylovo_api"}
    distributions = importlib.metadata.packages_distributions()
    distributions.setdefault("mpl_toolkits", ["matplotlib"])  # without the plots extra installed
    return {canonicalize_name(distributions.get(module, [module])[0]) for module in modules}


def test_imported_packages_are_declared():
    # The plotting modules need the plots extra, pylovo_api needs the api extra.
    undeclared = {
        "src/pylovo": imported_packages(ROOT / "src" / "pylovo") - declared_packages("plots"),
        "api/pylovo_api": imported_packages(ROOT / "api" / "pylovo_api") - declared_packages("api"),
    }
    assert undeclared == {"src/pylovo": set(), "api/pylovo_api": set()}


def test_requirements_txt_pins_the_locked_versions():
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {canonicalize_name(package["name"]): package["version"] for package in lock["package"]}
    pins = {}
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("#", "-e")):
            requirement = Requirement(line)
            pins[canonicalize_name(requirement.name)] = next(iter(requirement.specifier)).version
    assert pins
    assert {name: pin for name, pin in pins.items() if locked.get(name) != pin} == {}, \
        "requirements.txt is stale: uv export --format requirements-txt --no-hashes --no-annotate --no-dev"


def test_pytest_runs_both_test_folders_and_shows_the_skip_reasons():
    options = PYPROJECT["tool"]["pytest"]["ini_options"]
    assert options["testpaths"] == ["tests", "api/tests"]
    assert "-ra" in options["addopts"].split()


def env_example_names():
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)\s*=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))


def test_env_example_documents_only_variables_the_code_reads():
    source = "\n".join(path.read_text(encoding="utf-8") for folder in ("src", "api/pylovo_api")
                       for path in (ROOT / folder).rglob("*.py"))
    assert sorted(name for name in env_example_names() if f'"{name}"' not in source) == []


def test_env_example_documents_every_variable_of_the_config_loader():
    source = (ROOT / "src" / "pylovo" / "config_loader.py").read_text(encoding="utf-8")
    read = set(re.findall(r'(?:getenv|get_required_env_var|get_int_env_var)\(\s*"([A-Z][A-Z0-9_]*)"', source))
    assert read
    assert sorted(read - env_example_names()) == []


def test_tests_workflow_installs_the_extras_the_tests_import():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8"))
    commands = [step["run"] for job in workflow["jobs"].values() for step in job["steps"] if "run" in step]
    command = next(command for command in commands if "pytest" in command)
    assert "--locked" in command
    assert "--extra api" in command    # fastapi for api/tests
    assert "--extra plots" in command  # matplotlib for the MV plotting tests, which skip without it


def test_citation_file_declares_a_cff_version_that_cffconvert_accepts():
    citation = yaml.safe_load((ROOT / "CITATION.cff").read_text(encoding="utf-8"))
    assert citation["cff-version"] == "1.2.0"  # 1.0.0 fails the validation of cffconvert and GitHub
