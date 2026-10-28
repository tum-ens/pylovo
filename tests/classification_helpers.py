"""Synthetic grid parameters, a fake cursor and a fake database client for the classification tests (no database, no network)."""
from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager

import numpy as np
import pandas as pd

from pylovo.analysis.grid_analysis import KEY_FIGURES_VERSION
from pylovo.config_loader import CLUSTERING_PARAMETERS

# four grid archetypes: rural, village, suburb, urban (centre of the log-normal parameter distributions)
ARCHETYPES = [
    dict(n=12, branches=2, per_branch=6, dis=0.45, mva=0.16, hh=1.2),
    dict(n=35, branches=3, per_branch=12, dis=0.25, mva=0.25, hh=1.4),
    dict(n=70, branches=5, per_branch=14, dis=0.20, mva=0.40, hh=1.8),
    dict(n=140, branches=7, per_branch=20, dis=0.15, mva=0.63, hh=4.0),
]


def synthetic_clustering_parameters(n_per_archetype=(40, 40, 30, 20), seed=0) -> pd.DataFrame:
    """Return a frame with exactly the ``CLUSTERING_PARAMETERS`` columns of ``config_clustering.yaml``."""
    rng = np.random.default_rng(seed)
    rows = []
    for arch, count in zip(ARCHETYPES, n_per_archetype):
        for _ in range(count):
            conn = max(3, int(arch["n"] * rng.lognormal(0, 0.25)))
            branches = max(1, int(round(arch["branches"] * rng.lognormal(0, 0.2))))
            households = max(conn, int(conn * arch["hh"] * rng.lognormal(0, 0.15)))
            avg_dis = arch["dis"] * rng.lognormal(0, 0.25)
            max_dis = avg_dis * rng.uniform(1.4, 2.2)
            length = conn * rng.uniform(0.03, 0.06)
            power = households * 0.0042 * rng.uniform(0.9, 1.1)
            res = avg_dis * rng.uniform(0.8, 1.2)
            rows.append(dict(
                no_connection_buses=int(conn * rng.uniform(0.5, 0.9)),
                no_branches=branches,
                no_house_connections=conn,
                no_house_connections_per_branch=conn / branches,
                no_households=households,
                no_household_equ=power * 1000 / 4.2,
                no_household_equ_per_branch=households / branches,
                max_household_equ_of_a_branch=households / branches * rng.uniform(1.1, 1.8),
                house_distance_km=rng.uniform(0.01, 0.05),
                transformer_mva=arch["mva"],
                osm_trafo=bool(rng.integers(0, 2)),
                max_trafo_dis=max_dis,
                avg_trafo_dis=avg_dis,
                cable_length_km=length,
                cable_len_per_house=length / conn,
                max_power_mw=power,
                simultaneous_peak_load_mw=power * 0.4,
                resistance=res,
                reactance=res * 0.3,
                ratio=3.3,
                vsw_per_branch=res / branches,
                max_vsw_of_a_branch=res * rng.uniform(0.3, 0.8),
            ))
    df = pd.DataFrame(rows)
    df.insert(0, "version_id", "1")
    df.insert(1, "plz", 80000 + np.arange(len(df)) // 3)
    df.insert(2, "bcid", np.arange(len(df)) % 3)
    df.insert(3, "kcid", 1)
    return df[CLUSTERING_PARAMETERS].copy()


class FakeCursor:
    """Records executed statements; ``fetch_values`` is handed out by fetchone/fetchall."""

    def __init__(self, fetchone_values=None, fetchall_values=None):
        self.executed: list[tuple[str, object]] = []
        self.fetchone_values = list(fetchone_values or [])
        self.fetchall_values = list(fetchall_values or [])
        self.statusmessage = "FAKE"
        self.closed = False

    def execute(self, query, params=None):
        self.executed.append((" ".join(str(query).split()), params))

    def fetchone(self):
        return self.fetchone_values.pop(0) if self.fetchone_values else None

    def fetchall(self):
        return self.fetchall_values.pop(0) if self.fetchall_values else []

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.commits = 0
        self.log: list[str] = []

    def commit(self):
        self.commits += 1
        self.log.append("commit")

    def close(self):
        pass


class FakeEngine:
    """A SQLAlchemy engine whose ``begin()`` yields a connection; ``log`` records the order of the events."""

    def __init__(self):
        self.log: list[tuple] = []
        self.connection = self

    def execute(self, statement, params=None):
        self.log.append(("execute", " ".join(str(statement).split()), params))

    @contextmanager
    def begin(self):
        self.log.append(("begin",))
        try:
            yield self.connection
        except BaseException:
            self.log.append(("rollback",))
            raise
        self.log.append(("commit",))


class FakeDbc:
    """Stands in for ``DatabaseClient`` (cursor, connection, engine, view refresh)."""

    def __init__(self, cur=None):
        self.cur = cur or FakeCursor()
        self.conn = FakeConnection()
        self.sqla_engine = FakeEngine()
        self.refreshed = 0

    def refresh_materialized_views(self):
        self.refreshed += 1


def ddl_columns(create_query: str) -> list[str]:
    """Column names of a ``CREATE TABLE`` statement of ``config_table_structure`` (constraints skipped)."""
    body = create_query[create_query.index("(") + 1: create_query.rindex(")")]
    columns = []
    for line in body.splitlines():
        token = line.strip().rstrip(",")
        if not token or token.startswith(("CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "REFERENCES", "ON DELETE", "--")):
            continue
        name = token.split()[0].strip('"')
        if name.isidentifier() and name.upper() not in {"FOREIGN", "REFERENCES", "ON", "CONSTRAINT"}:
            columns.append(name)
    return columns


# --------------------------------------------------------------------------------------- SQLite stand-in for the SQL
SQLITE_TABLES = """
ATTACH DATABASE ':memory:' AS pylovo;
CREATE TABLE pylovo.grid_result (grid_result_id INTEGER, version_id TEXT, plz INTEGER, kcid INTEGER, bcid INTEGER,
    power_flow_status TEXT);
CREATE TABLE pylovo.clustering_parameters (grid_result_id INTEGER, no_households INTEGER, avg_trafo_dis REAL,
    max_trafo_dis REAL, no_house_connections INTEGER, vsw_per_branch REAL, key_figures_version INTEGER);
CREATE TABLE pylovo.classification_version (classification_id INTEGER, key_figures_version INTEGER);
CREATE TABLE pylovo.buildings_result (grid_result_id INTEGER, households INTEGER);
CREATE TABLE pylovo.sample_set (classification_id INTEGER, plz INTEGER, ags INTEGER);
CREATE TABLE pylovo.municipal_register (plz INTEGER, ags INTEGER, pop INTEGER, area REAL, lat REAL, lon REAL,
    name_city TEXT, regio7 INTEGER, regio5 INTEGER, pop_den REAL);
CREATE TABLE pylovo.plz_parameters (version_id TEXT, plz INTEGER);
CREATE TABLE pylovo.transformer_classified (grid_result_id INTEGER, classification_id INTEGER);
"""

# grid_result_id -> (plz, kcid, bcid) of version "v1"
SQLITE_GRIDS = {1: (80001, 1, 1), 2: (80001, 1, 2), 3: (80001, 1, 3), 4: (80001, 1, 4), 5: (80002, 1, 1),
                6: (80002, 1, 2), 7: (80003, 1, 1), 8: (80003, 1, 2), 9: (80004, 1, 1)}

# key figures (no_households, avg_trafo_dis, max_trafo_dis, no_house_connections, vsw_per_branch); grids 7 to 9 have none
SQLITE_FIGURES = {
    1: (100, 0.30, 1.0, 40, 0.50),   # normal
    2: (100, 0.30, 3.0, 40, 0.50),   # transformer distance above the upper limit
    3: (100, 0.30, 1.0, 40, 0.50),   # has a building with 200 households
    4: (10, 0.05, 1.0, 5, 0.10),     # small filling grid, below all four lower bounds
    5: (100, 0.30, 1.0, 40, 0.50),   # normal
    6: (100, 0.30, None, 40, 0.50),  # no max_trafo_dis: a missing value never filters
}


def sqlite_classification_db() -> sqlite3.Connection:
    """Return an in-memory SQLite database with the tables of the classification SQL (schema ``pylovo``).

    Classification 1 samples PLZ 80001 to 80005, classification 2 only PLZ 80001. PLZ 80003 has grids without key
    figures and no PLZ analysis, PLZ 80004 has a grid without key figures although its PLZ was analysed and PLZ
    80005 has no grid. Classification 1 has a result for grids 1 and 5.
    """
    db = sqlite3.connect(":memory:")
    db.executescript(SQLITE_TABLES)
    for grid_id, (plz, kcid, bcid) in SQLITE_GRIDS.items():
        db.execute("INSERT INTO pylovo.grid_result VALUES (?, 'v1', ?, ?, ?, 'converged')", (grid_id, plz, kcid, bcid))
    for grid_id, figures in SQLITE_FIGURES.items():
        db.execute("INSERT INTO pylovo.clustering_parameters VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (grid_id, *figures, KEY_FIGURES_VERSION))
    db.executemany("INSERT INTO pylovo.buildings_result VALUES (?, ?)", [(1, 3), (3, 200), (3, 4)])
    db.executemany("INSERT INTO pylovo.sample_set VALUES (?, ?, ?)",
                   [(1, plz, 9000000 + plz) for plz in range(80001, 80006)] + [(2, 80001, 9080001)])
    db.executemany("INSERT INTO pylovo.municipal_register VALUES (?, ?, 5000, 10.0, 48.0, 11.0, 'town', 73, 53, 500.0)",
                   [(plz, 9000000 + plz) for plz in range(80001, 80006)])
    db.executemany("INSERT INTO pylovo.plz_parameters VALUES ('v1', ?)", [(80001,), (80002,), (80004,)])
    db.executemany("INSERT INTO pylovo.transformer_classified VALUES (?, 1)", [(1,), (5,)])
    return db


def sqlite_read_sql_query(db: sqlite3.Connection):
    """Return a ``pd.read_sql_query`` that runs the pyformat SQL of the classification code on ``db``."""
    real = pd.read_sql_query

    def read(query, con=None, params=None):
        return real(re.sub(r"%\((\w+)\)s", r":\1", query), db, params=params)

    return read


def make_sqlite_dc(monkeypatch):
    """Return a ``DatabaseCommunication`` without connection whose queries run on the SQLite stand-in.

    ``VERSION_ID`` is ``v1`` and ``CLASSIFICATION_VERSION`` 1; the database is ``dc.sqlite``.
    """
    import pylovo.classification.database_communication.database_communication as dcm

    db = sqlite_classification_db()
    monkeypatch.setattr(dcm.pd, "read_sql_query", sqlite_read_sql_query(db))
    monkeypatch.setattr(dcm, "VERSION_ID", "v1")
    monkeypatch.setattr(dcm, "CLASSIFICATION_VERSION", 1)
    dc = object.__new__(dcm.DatabaseCommunication)  # skip __init__: no connection
    dc.dbc = FakeDbc()
    dc.sqlite = db
    return dc
