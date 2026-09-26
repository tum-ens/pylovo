"""Paths and runtime settings of pylovo-api.

The UI always works on one pylovo *project root*: the directory that holds ``config/`` (and,
in a source checkout, ``.env``). All CLI jobs run with this directory as working directory,
exactly as if the user had typed the command there, so ``config/`` and ``.env`` resolve the
same way for the UI and for the command line.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = "config_generation.yaml"
STATE_DIR_NAME = ".pylovo-api"


def _is_project_root(path: Path) -> bool:
    return (path / "config" / CONFIG_FILE).is_file()


def find_project_root(explicit: str | os.PathLike | None = None) -> Path:
    """Return the pylovo project root.

    Search order: the explicit ``--root`` argument, ``$PYLOVO_ROOT``, the current working
    directory and its parents, and finally the source checkout this package lives in.

    Raises:
        FileNotFoundError: If no directory with ``config/config_generation.yaml`` is found.
    """
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not _is_project_root(path):
            raise FileNotFoundError(f"{path} does not contain config/{CONFIG_FILE}")
        return path
    candidates: list[Path] = []
    if os.getenv("PYLOVO_ROOT"):
        candidates.append(Path(os.environ["PYLOVO_ROOT"]).expanduser())
    cwd = Path.cwd().resolve()
    candidates += [cwd, *cwd.parents]
    candidates.append(Path(__file__).resolve().parents[2])  # <root>/api/pylovo_api
    for path in candidates:
        if _is_project_root(path):
            return path.resolve()
    raise FileNotFoundError(
        f"No pylovo project root found (a directory with config/{CONFIG_FILE}). "
        "Start pylovo-api inside your pylovo checkout or pass --root."
    )


@dataclass(frozen=True)
class Paths:
    """All file-system locations the API reads or writes."""

    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def config_file(self) -> Path:
        return self.config_dir / CONFIG_FILE

    @property
    def state_dir(self) -> Path:
        """Private working directory of the API (backups, uploads, job logs, exports)."""
        return self.root / STATE_DIR_NAME

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "config-backups"

    @property
    def uploads_dir(self) -> Path:
        return self.state_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def exports_dir(self) -> Path:
        return self.state_dir / "exports"

    @property
    def tmp_dir(self) -> Path:
        return self.state_dir / "tmp"

    def ensure(self) -> None:
        for directory in (self.backups_dir, self.uploads_dir, self.jobs_dir, self.exports_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)
        gitignore = self.state_dir / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("# Created by pylovo-api: private working files\n*\n", encoding="utf-8")


_paths: Paths | None = None


def init_paths(root: Path) -> Paths:
    """Set the project root for this process and create the API state directory."""
    global _paths
    _paths = Paths(root=root)
    _paths.ensure()
    return _paths


def paths() -> Paths:
    """Return the paths of the active project root (``init_paths`` must have run)."""
    if _paths is None:
        return init_paths(find_project_root())
    return _paths
