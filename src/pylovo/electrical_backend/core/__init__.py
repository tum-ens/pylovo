"""
Core abstractions for electrical backends.

This subpackage contains shared interfaces and data classes used by all backend
implementations.

Contents:
    - IElectricalBackend: Abstract interface all backends must implement
    - Component specs: BusSpec, LineSpec, LoadSpec, TransformerSpec, ExtGridSpec

Usage:
    from pylovo.electrical_backend.core import IElectricalBackend, BusSpec
    # or via public API:
    from pylovo.electrical_backend import IElectricalBackend, BusSpec
"""

from .backend_base import IElectricalBackend
from .specs import (
    ComponentSpec,
    BusSpec,
    LineSpec,
    LoadSpec,
    TransformerSpec,
    ExtGridSpec,
    normalize_cable_name,
)

__all__ = [
    "IElectricalBackend",
    "ComponentSpec",
    "BusSpec",
    "LineSpec",
    "LoadSpec",
    "TransformerSpec",
    "ExtGridSpec",
    "normalize_cable_name",
]
