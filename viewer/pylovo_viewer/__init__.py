"""Read-only browser map of a pylovo database (``pylovo-viewer``).

The viewer shows the LV grids, the MV grids, their input data and context grids of the database in
the active ``.env`` as map layers. It only reads: every transaction is read-only and it never
imports pylovo's database code, so it cannot create, migrate or reset anything.
"""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("pylovo")  # the package ships in the pylovo distribution
except PackageNotFoundError:
    __version__ = "unknown"
