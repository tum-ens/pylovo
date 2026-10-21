"""
OpenDSS backend implementation.

This subpackage provides the OpenDSS-based electrical simulation backend.
OpenDSS uses a stateful circuit model with explicit component ordering.

Key characteristics:
    - Line codes must be created before lines that reference them
    - Buses are created implicitly when referenced by components
    - Uses altdss Python bindings for the OpenDSS engine

Usage:
    from pylovo.electrical_backend.opendss import OpenDSSBackend
    # or via factory:
    from pylovo.electrical_backend import create_backend
    backend = create_backend("opendss")
"""

from .backend import OpenDSSBackend, OpenDSSBackendError

__all__ = ["OpenDSSBackend", "OpenDSSBackendError"]
