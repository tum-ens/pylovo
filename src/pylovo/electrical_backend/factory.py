"""
Backend factory and registry for electrical backends.

This module provides:
    - Backend registration via register_backend()
    - Backend discovery via available_backends()
    - Backend instantiation via create_backend()

Backends are registered with lazy imports to avoid loading heavy dependencies
(pandapower, altdss) until actually needed.

Usage:
    from pylovo.electrical_backend import create_backend

    # Create default (pandapower) backend
    backend = create_backend()

    # Create specific backend
    backend = create_backend("opendss", logger=my_logger)
"""

from typing import Callable, Dict, Iterable, Optional

from .core.backend_base import IElectricalBackend


# Type alias for backend factory functions
BackendFactory = Callable[[Optional[object]], IElectricalBackend]

# Internal registry mapping normalized names to factory functions
_REGISTRY: Dict[str, BackendFactory] = {}


def _normalize(name: str) -> str:
    """Normalize backend key for consistent lookups."""
    return name.strip().lower()


def register_backend(name: str, factory: BackendFactory) -> None:
    """
    Register a backend factory under the given name.

    Args:
        name: Backend name (case-insensitive)
        factory: Callable that takes an optional logger and returns IElectricalBackend
    """
    _REGISTRY[_normalize(name)] = factory


def get_backend_factory(name: str) -> BackendFactory:
    """
    Get the factory for a backend by name.

    Args:
        name: Backend name (case-insensitive)

    Returns:
        Factory callable

    Raises:
        KeyError: If backend not registered
    """
    key = _normalize(name)
    if key not in _REGISTRY:
        raise KeyError(name)
    return _REGISTRY[key]


def available_backends() -> Iterable[str]:
    """Return sorted list of available backend names."""
    return tuple(sorted(_REGISTRY.keys()))


# =============================================================================
# Built-in Backend Registration (lazy imports)
# =============================================================================


def _pandapower_ctor(logger: Optional[object]) -> IElectricalBackend:
    """Lazy constructor for PandapowerBackend."""
    from .pandapower.backend import PandapowerBackend

    return PandapowerBackend(logger=logger)


def _opendss_ctor(logger: Optional[object]) -> IElectricalBackend:
    """Lazy constructor for OpenDSSBackend."""
    from .opendss.backend import OpenDSSBackend

    return OpenDSSBackend(logger=logger)


# Register built-in backends
register_backend("pandapower", _pandapower_ctor)
register_backend("opendss", _opendss_ctor)


# =============================================================================
# Public API
# =============================================================================


def create_backend(
    backend_name: str = "pandapower", logger: Optional[object] = None
) -> IElectricalBackend:
    """
    Create a backend instance by name.

    Args:
        backend_name: Name of backend ("pandapower" or "opendss")
        logger: Optional logger instance

    Returns:
        Configured backend instance

    Raises:
        ValueError: If backend name is unknown
    """
    try:
        factory = get_backend_factory(backend_name)
    except KeyError:
        raise ValueError(
            f"Unknown backend: {backend_name}. "
            f"Available: {', '.join(available_backends())}"
        ) from None
    return factory(logger)


__all__ = [
    "create_backend",
    "register_backend",
    "available_backends",
    "BackendFactory",
]


