"""Database settings of the viewer: the ``.env`` that pylovo uses, or the one given with ``--env``.

The file is read with :func:`dotenv.dotenv_values` and never loaded into ``os.environ``. The viewer
does not import :mod:`pylovo.config_loader`, which would load the checkout's own ``.env`` with
``override=True`` and import the whole library (pandapower, scikit-learn, ...).
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dotenv import dotenv_values

REQUIRED = ("DBNAME", "DBUSER", "PASSWORD", "HOST", "PORT")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SettingsError(RuntimeError):
    """The database settings are missing or invalid."""


@dataclass(frozen=True)
class Settings:
    """Connection settings and schema names, with the same keys and defaults as pylovo's ``.env``."""

    dbname: str
    user: str
    host: str
    port: int
    password: str = field(repr=False)
    target_epsg: int = 25832
    source_schema: str = "pylovo_input"
    opendata_schema: str = "opendata"
    osm_schema: str = "osm"
    env_file: str | None = None

    def public(self) -> dict:
        """The settings without the password."""
        data = asdict(self)
        data.pop("password")
        return data

    def describe(self) -> str:
        return f"{self.dbname} on {self.host}:{self.port}"


def find_env_file(explicit: str | os.PathLike | None = None, cwd: Path | None = None) -> Path | None:
    """The ``.env`` to use.

    ``explicit`` if given; otherwise the first ``.env`` above the installed package (pylovo's own
    lookup, so a checkout uses its ``.env``), then the first one above ``cwd``.
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SettingsError(f"no such .env file: {path}")
        return path.resolve()
    for start in (Path(__file__).resolve().parent, (cwd or Path.cwd()).resolve()):
        for directory in (start, *start.parents):
            candidate = directory / ".env"
            if candidate.is_file():
                return candidate
    return None


def load_settings(env_file: Path | None) -> Settings:
    """Settings from ``env_file``; a key missing there is taken from the environment."""
    values = {k: v for k, v in (dotenv_values(env_file) if env_file else {}).items() if v not in (None, "")}

    def get(key: str, default: str | None = None) -> str | None:
        return values.get(key) or os.environ.get(key) or default

    missing = [key for key in REQUIRED if not get(key)]
    if missing:
        raise SettingsError(f"{', '.join(missing)} missing in {env_file or 'the environment (no .env found)'}")
    schemas = {"source_schema": get("INFDB_SOURCE_SCHEMA", "pylovo_input"),
               "opendata_schema": get("INFDB_OPENDATA_SCHEMA", "opendata"),
               "osm_schema": get("INFDB_OSM_SCHEMA", "osm")}
    for name, value in schemas.items():
        if not _IDENT.match(value):
            raise SettingsError(f"{name} {value!r} is not a plain schema name")
    try:
        port, epsg = int(get("PORT")), int(get("TARGET_EPSG", "25832"))
    except ValueError as exc:
        raise SettingsError(f"PORT and TARGET_EPSG must be integers ({exc})") from None
    return Settings(dbname=get("DBNAME"), user=get("DBUSER"), password=get("PASSWORD"), host=get("HOST"),
                    port=port, target_epsg=epsg, env_file=str(env_file) if env_file else None, **schemas)
