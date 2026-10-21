"""Headless HTTP API of pylovo (``pylovo-api``) for browser clients and scripts."""
from importlib.metadata import PackageNotFoundError, version

API_VERSION = 2
"""Contract version of the HTTP API (``/api/health``, ``info.version`` of ``api/openapi.json``).

Bump it only for breaking changes (removed or renamed routes, parameters or response fields); a
client can refuse an API version it does not know.
"""

try:
    __version__ = version("pylovo")  # the package ships in the pylovo distribution
except PackageNotFoundError:
    __version__ = "unknown"
