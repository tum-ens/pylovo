"""
pylovo - Python tool for Low-Voltage distribution grid generation

A comprehensive tool for generating synthetic low-voltage distribution grids
based on open data sources. Designed for energy system modeling research.

Importing a module that uses :mod:`pylovo.config_loader` reads ``.env`` and the YAML files in ``./config``
at import time (see that module for the lookup rules). Importing the package itself does not: the classes below
are imported when they are first used, so ``pylovo-* --help`` works without configuration.
"""

import importlib
from importlib.metadata import version, PackageNotFoundError
from typing import TYPE_CHECKING

try:
    __version__ = version("pylovo")
except PackageNotFoundError:
    __version__ = "unknown"

#: Public name -> module that defines it (imported on first use, see :func:`__getattr__`).
_LAZY = {
    # Core components
    "DatabaseClient": "pylovo.database.database_client",
    "GridGenerator": "pylovo.grid_generator",
    "DatabaseConstructor": "pylovo.database.database_constructor",
    # Electrical backend utilities
    "IElectricalBackend": "pylovo.electrical_backend",
    "create_backend": "pylovo.electrical_backend",
    "BusSpec": "pylovo.electrical_backend",
    "LineSpec": "pylovo.electrical_backend",
    "LoadSpec": "pylovo.electrical_backend",
    "TransformerSpec": "pylovo.electrical_backend",
    "ExtGridSpec": "pylovo.electrical_backend",
    "normalize_cable_name": "pylovo.electrical_backend",
}

if TYPE_CHECKING:
    from pylovo.database.database_client import DatabaseClient
    from pylovo.database.database_constructor import DatabaseConstructor
    from pylovo.electrical_backend import (
        BusSpec,
        ExtGridSpec,
        IElectricalBackend,
        LineSpec,
        LoadSpec,
        TransformerSpec,
        create_backend,
        normalize_cable_name,
    )
    from pylovo.grid_generator import GridGenerator


def __getattr__(name: str):
    """Import a public name of :data:`__all__` when it is first used (``pylovo.GridGenerator``, ``from pylovo import ...``)."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    # Version
    "__version__",
    # Core classes
    "DatabaseClient",
    "GridGenerator",
    "DatabaseConstructor",
    # Backend interface
    "IElectricalBackend",
    "create_backend",
    # Component specs
    "BusSpec",
    "LineSpec",
    "LoadSpec",
    "TransformerSpec",
    "ExtGridSpec",
    "normalize_cable_name",
]
