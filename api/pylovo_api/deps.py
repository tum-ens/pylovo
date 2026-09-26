"""Shared helpers for the API routers."""
from __future__ import annotations

from fastapi import HTTPException, Request

from pylovo_api.jobs import JobManager


def jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def coverage(request: Request):
    """The :class:`~pylovo_api.coverage.InputCoverage` engine of the app (region gate)."""
    return request.app.state.coverage


def ensure_no_writer(request: Request) -> None:
    """Refuse direct database edits while a database-writing job runs."""
    busy = jobs(request).running_writer()
    if busy:
        raise HTTPException(409, f"'{busy.title}' is running. Edits are blocked until it has finished.")


def require_confirm(given: object, expected: str, what: str) -> None:
    """Destructive actions need the user to type an exact confirmation text."""
    if str(given or "").strip() != expected:
        raise HTTPException(400, f"Confirmation required: type '{expected}' to {what}.")


def config_version_id() -> str:
    from pylovo_api.config_io import current_values

    return str(current_values().get("VERSION_ID", ""))
