"""Shared helpers of the modules of the MV stage that use the database: the planning modules imported on first use,
the configuration, the database client, the loggers, one-line error texts and the number of worker processes.

:mod:`~pylovo.mv_grid.generator`, :mod:`~pylovo.mv_grid.preparation`, :mod:`~pylovo.mv_grid.post_steps`,
:mod:`~pylovo.mv_grid.export`, :mod:`~pylovo.mv_grid.coupling` and :mod:`~pylovo.mv_grid.interconnection` use them.
The planning modules are pure and do not. This module imports only the standard library at import time, so an
LV-only run that loads the MV CLI loads no MV planning module.
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType

#: Memory of one district worker after its imports plus a large district (GB), measured ~0.3 + district data.
WORKER_GB: float = 0.6
#: Memory kept free for the parent, PostgreSQL and the user's other processes (GB).
RESERVED_GB: float = 2.0
#: Default upper bound of the district workers (``min(4, N_JOBS)``).
DEFAULT_MAX_JOBS: int = 4


def module(name: str) -> ModuleType:
    """``pylovo.mv_grid.<name>``, imported on first use."""
    return importlib.import_module(f"pylovo.mv_grid.{name}")


def config() -> ModuleType:
    """:mod:`pylovo.config_loader`, imported on first use."""
    return importlib.import_module("pylovo.config_loader")


def database_client(log_file: str):
    """A new :class:`~pylovo.database.database_client.DatabaseClient` that logs to ``log_file``."""
    from pylovo.database.database_client import DatabaseClient

    return DatabaseClient(log_file=log_file)


def create_logger(name: str, log_file: str) -> logging.Logger:
    """The logger ``name`` on ``log_file`` and the console at the configured ``LOG_LEVEL``."""
    from pylovo import utils

    return utils.create_logger(name, log_file, config().LOG_LEVEL)


def close_logger(logger: logging.Logger) -> None:
    """Close and remove the handlers of ``logger``."""
    for handler in list(logger.handlers):
        handler.close()
    logger.handlers.clear()


def one_line(exc: BaseException) -> str:
    """``Type: message`` on one line, at most 2,000 characters."""
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:2000]


def mem_available_gb() -> float | None:
    """``MemAvailable`` of ``/proc/meminfo`` in GB, or None where it cannot be read."""
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def worker_count(jobs: int | None, n_pending: int, per_worker_gb: float = WORKER_GB) -> int:
    """Number of district workers: ``min(jobs, n_pending)`` capped by the free memory.

    Args:
        jobs: Requested workers; None means ``min(4, N_JOBS)``.
        n_pending: Number of districts to plan.
        per_worker_gb: Memory of one worker (GB).

    Returns:
        ``n = min(jobs, n_pending)``, then at most ``max(1, int((MemAvailable_GB - 2.0) / per_worker_gb))``;
        0 when nothing is pending.
    """
    if n_pending <= 0:
        return 0
    if jobs is None:
        jobs = min(DEFAULT_MAX_JOBS, int(config().N_JOBS))
    n = max(1, min(int(jobs), int(n_pending)))
    available = mem_available_gb()
    if available is not None and per_worker_gb > 0:
        n = min(n, max(1, int((available - RESERVED_GB) / per_worker_gb)))
    return n
