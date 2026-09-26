"""
pylovo - Python tool for Low-Voltage distribution grid generation

A comprehensive tool for generating synthetic low-voltage distribution grids
based on open data sources. Designed for energy system modeling research.

Importing the package (or any submodule) imports :mod:`pylovo.config_loader`,
which reads ``.env`` and the YAML files in ``./config`` at import time; see that
module for the lookup rules.
"""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("pylovo")
except PackageNotFoundError:
    __version__ = "unknown"

# Core components
from pylovo.database.database_client import DatabaseClient
from pylovo.grid_generator import GridGenerator
from pylovo.database.database_constructor import DatabaseConstructor

# Electrical backend utilities
from pylovo.electrical_backend import (
    IElectricalBackend,
    create_backend,
    BusSpec,
    LineSpec,
    LoadSpec,
    TransformerSpec,
    ExtGridSpec,
    normalize_cable_name,
)

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
