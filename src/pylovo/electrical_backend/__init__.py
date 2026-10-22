"""
Electrical backend package for pylovo.

This package provides a unified interface for electrical grid simulation
using different backend engines (pandapower, OpenDSS).

Quick start:
    from pylovo.electrical_backend import create_backend, BusSpec, LineSpec

    backend = create_backend("pandapower")
    backend.initialize_circuit("my_grid", "source_bus", 20.0)
    backend.create_component(BusSpec(name="bus1", voltage_kv=0.4))

Package structure:
    - core/: Shared interfaces and data classes
    - pandapower/: Pandapower backend implementation (default)
    - opendss/: OpenDSS backend implementation (needs the optional ``altdss`` package)
    - factory.py: Backend registration and creation

The backend classes are imported lazily, so importing this package does not
import pandapower or altdss.
"""

from .core.backend_base import IElectricalBackend
from .core.specs import (
    ComponentSpec,
    BusSpec,
    LineSpec,
    LoadSpec,
    TransformerSpec,
    ExtGridSpec,
    normalize_cable_name,
)
from .core.equipment import CableEquipment, TransformerEquipment
from .factory import (
    create_backend,
    register_backend,
    available_backends,
    BackendFactory,
)


def __getattr__(name: str):
    """Lazy access for backend classes to keep import time low."""
    if name in {"PandapowerBackend", "PandapowerBackendError"}:
        from .pandapower.backend import PandapowerBackend, PandapowerBackendError

        return {
            "PandapowerBackend": PandapowerBackend,
            "PandapowerBackendError": PandapowerBackendError,
        }[name]

    if name in {"OpenDSSBackend", "OpenDSSBackendError"}:
        from .opendss.backend import OpenDSSBackend, OpenDSSBackendError

        return {
            "OpenDSSBackend": OpenDSSBackend,
            "OpenDSSBackendError": OpenDSSBackendError,
        }[name]

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core interface
    "IElectricalBackend",
    # Component specs
    "ComponentSpec",
    "BusSpec",
    "TransformerSpec",
    "LineSpec",
    "LoadSpec",
    "ExtGridSpec",
    "normalize_cable_name",
    # Equipment classes
    "CableEquipment",
    "TransformerEquipment",
    # Factory
    "create_backend",
    "register_backend",
    "available_backends",
    "BackendFactory",
    # Backend classes (lazy loaded)
    "PandapowerBackend",
    "PandapowerBackendError",
    "OpenDSSBackend",
    "OpenDSSBackendError",
]
