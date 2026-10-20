"""The databases the viewer offers, with one read-only site per database.

Every API request names its database (``db``); without it, the request goes to ``DBNAME`` of the
``.env``. The viewer offers the databases on the same server that the ``.env`` user may connect to:
those listed in ``PYLOVO_VIEWER_DATABASES``, or else every one with a ``pylovo.version`` table. The
list follows the server: it is read again when it is older than a minute, on a reload, and when a
request names a database that is not on it. A database that left the server is closed.

Each database has its own connection pool, catalog, layer service and response cache (a
:class:`Site`), opened on its first request, so nothing is shared between databases.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pylovo_viewer.catalog import Catalog
from pylovo_viewer.db import Database, DatabaseError
from pylovo_viewer.layers import LRU, LayerService
from pylovo_viewer.settings import Settings

ON_SERVER = """SELECT datname FROM pg_database
               WHERE NOT datistemplate AND datallowconn AND has_database_privilege(datname, 'CONNECT')
               ORDER BY datname"""
HAS_PYLOVO = """SELECT EXISTS (SELECT 1 FROM information_schema.tables
                               WHERE table_schema = 'pylovo' AND table_name = 'version') AS pylovo"""


@dataclass
class Site:
    """One database: its read-only connections, what it holds, its layers and its cached responses."""

    db: Database
    catalog: Catalog
    service: LayerService
    cache: LRU

    @classmethod
    def open(cls, db: Database, data_dir: Path) -> Site:
        catalog = Catalog(db)
        return cls(db, catalog, LayerService(db, catalog, data_dir), LRU(64))


class Sites:
    """The databases offered on the server of :class:`Settings` and their sites."""

    def __init__(self, settings: Settings, data_dir: Path, db: Database | None = None,
                 connect: Callable[[Settings], Database] = Database, max_age_s: float = 60.0) -> None:
        self.settings, self.data_dir, self.connect, self.max_age_s = settings, data_dir, connect, max_age_s
        self._lock = threading.Lock()
        self._sites = {settings.dbname: Site.open(db or connect(settings), data_dir)}
        self._names: list[str] = []
        self._listed_at = float("-inf")

    @property
    def default(self) -> str:
        return self.settings.dbname

    def names(self, refresh: bool = False, max_age_s: float | None = None) -> list[str]:
        """The databases offered, the default first; read again when older than ``max_age_s``."""
        with self._lock:
            age = time.monotonic() - self._listed_at
            if refresh or age > (self.max_age_s if max_age_s is None else max_age_s):
                self._names = self._discover()
                self._listed_at = time.monotonic()
                for name in [n for n in self._sites if n not in self._names]:
                    self._sites.pop(name).db.close()
            return list(self._names)

    def _discover(self) -> list[str]:
        on_server = [row["datname"] for row in self._sites[self.default].db.query(ON_SERVER)]
        if self.settings.databases:
            others = [name for name in self.settings.databases if name in on_server]
        else:
            others = [name for name in on_server if name != self.default and self._has_pylovo(name)]
        return list(dict.fromkeys([self.default, *others]))

    def _has_pylovo(self, name: str) -> bool:
        site = self._sites.get(name)
        db = site.db if site else self.connect(dataclasses.replace(self.settings, dbname=name))
        try:
            return bool(db.one(HAS_PYLOVO)["pylovo"])
        except DatabaseError:
            return False   # no access, or the database went away
        finally:
            if site is None:
                db.close()

    def get(self, name: str | None) -> Site:
        """The site of database ``name`` (the default for ``None``), opened on first use."""
        name = name or self.default
        if name != self.default and name not in self.names(max_age_s=float("inf")) \
                and name not in self.names(refresh=True):
            raise LookupError(f"no database {name} to show on {self.settings.host}:{self.settings.port}")
        site = self._sites.get(name)
        if site is None:
            opened = Site.open(self.connect(dataclasses.replace(self.settings, dbname=name)), self.data_dir)
            with self._lock:
                site = self._sites.setdefault(name, opened)
            if site is not opened:
                opened.db.close()
        return site

    def close(self) -> None:
        with self._lock:
            for site in self._sites.values():
                site.db.close()
