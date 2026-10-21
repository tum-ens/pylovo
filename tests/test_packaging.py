"""Packaging metadata of pyproject.toml against the files the code loads and the modules it imports."""
import tomllib
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_sql_file_of_the_package_is_in_the_wheel_and_sdist():
    patterns = PYPROJECT["tool"]["hatch"]["build"]["include"]
    sql_files = [path.relative_to(ROOT).as_posix() for path in (ROOT / "src" / "pylovo").rglob("*.sql")]
    assert sql_files
    missing = [name for name in sql_files if not any(fnmatch(name, pattern) for pattern in patterns)]
    assert not missing, f"SQL files that DatabaseConstructor loads but the build leaves out: {missing}"
