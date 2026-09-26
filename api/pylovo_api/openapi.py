"""The OpenAPI contract snapshot ``api/openapi.json``.

The GridPlanner UI relies on the routes, parameters and response shapes of this API. The
snapshot is FastAPI's schema with sorted keys, so a change of the API shows up as a readable
diff: ``api/tests/test_openapi.py`` fails when the app differs from it, and CI runs
``oasdiff breaking`` of a pull request's snapshot against the base branch's snapshot.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI

SNAPSHOT = Path(__file__).resolve().parents[1] / "openapi.json"
UPDATE_HINT = ("If the change is intended, run `uv run --extra api python api/scripts/export_openapi.py` and "
               "commit api/openapi.json. For a breaking change (removed or renamed route, parameter or response "
               "field) also bump API_VERSION in api/pylovo_api/__init__.py.")


def render(app: FastAPI | None = None) -> str:
    """FastAPI's OpenAPI schema as JSON text with sorted keys and a final newline.

    Args:
        app: The app to describe (default: an app on a temporary project root).
    """
    if app is None:
        from pylovo_api.app import create_app

        with tempfile.TemporaryDirectory() as root:
            return render(create_app(Path(root)))
    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
