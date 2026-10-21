"""Shared helpers for the API routers."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException, Request

from pylovo_api.jobs import JobConflict, JobManager


def jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def coverage(request: Request):
    """The :class:`~pylovo_api.coverage.InputCoverage` engine of the app (region gate)."""
    return request.app.state.coverage


@contextmanager
def writer_lease(request: Request) -> Iterator[None]:
    """Hold while a direct edit (transformers, configuration) writes: 409 if a database-writing job runs, and no job starts meanwhile."""
    try:
        with jobs(request).db_edit_lease(any_writer=True):
            yield
    except JobConflict as exc:
        raise HTTPException(409, str(exc)) from exc


def require_confirm(given: object, expected: str, what: str) -> None:
    """Destructive actions need the user to type an exact confirmation text."""
    if str(given or "").strip() != expected:
        raise HTTPException(400, f"Confirmation required: type '{expected}' to {what}.")


def config_version_id() -> str:
    from pylovo_api.config_io import current_values

    return str(current_values().get("VERSION_ID", ""))
