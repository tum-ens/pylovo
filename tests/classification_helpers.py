"""Synthetic grid parameters, a fake cursor and a fake database client for the classification tests (no database, no network)."""
from __future__ import annotations

from contextlib import contextmanager

import numpy as np
import pandas as pd

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
                no_households_per_branch=households / branches,
                max_no_of_households_of_a_branch=households / branches * rng.uniform(1.1, 1.8),
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
