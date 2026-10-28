"""Check that every data file in git has a licence in DATA_LICENSES.md (pre-commit hook and tests/test_packaging.py).

A data file (table, geodata, archive, dump) is listed when its path, or a folder that holds it (ending in ``/``),
stands in backticks in DATA_LICENSES.md. Input data of grid operators never belongs in the repository.

    python3 tests/check_data_licenses.py [FILE ...]     # without files: every file that git tracks
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_SUFFIXES = {".csv", ".tsv", ".xls", ".xlsx", ".ods", ".geojson", ".gpkg", ".shp", ".shx", ".dbf", ".parquet",
                 ".feather", ".sqlite", ".db", ".dump", ".backup", ".pkl", ".pickle", ".npy", ".npz", ".h5", ".hdf5",
                 ".nc", ".zip", ".gz", ".bz2", ".xz", ".7z", ".tar"}


def listed_paths(licenses: Path = ROOT / "DATA_LICENSES.md") -> set[str]:
    """Files and folders that DATA_LICENSES.md names in backticks."""
    return set(re.findall(r"`([^`\s]+)`", licenses.read_text(encoding="utf-8")))


def unlisted(files, listed) -> list[str]:
    """The data files among ``files`` that are not listed, neither themselves nor one of their folders."""
    missing = []
    for name in files:
        path = Path(name).as_posix()
        if Path(path).suffix.lower() not in DATA_SUFFIXES:
            continue
        parts = path.split("/")
        folders = {"/".join(parts[:i]) + "/" for i in range(1, len(parts))}
        if path not in listed and not folders & listed:
            missing.append(path)
    return missing


def tracked_files() -> list[str]:
    """The files in the git index, staged new files included."""
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [name for name in out.split("\0") if name]


def main(files) -> int:
    missing = unlisted(files or tracked_files(), listed_paths())
    for path in missing:
        print(f"{path}: data file without a licence; list it in DATA_LICENSES.md or keep it out of git")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
