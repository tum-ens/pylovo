"""Headless HTTP API of pylovo (``pylovo-api``); the browser UI that uses it lives in GridPlanner."""
from importlib.metadata import PackageNotFoundError, version

API_VERSION = 1
"""Contract version of the HTTP API (``/api/health``, ``info.version`` of ``api/openapi.json``).

Bump it only for breaking changes (removed or renamed routes, parameters or response fields); the
UI refuses an API version it does not know.
"""

try:
    __version__ = version("pylovo")  # the package ships in the pylovo distribution
except PackageNotFoundError:
    __version__ = "unknown"
