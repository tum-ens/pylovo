"""The app's OpenAPI schema equals the committed contract snapshot ``api/openapi.json``."""
from __future__ import annotations

import difflib
import json

import pytest
from pylovo_api import API_VERSION
from pylovo_api.app import create_app
from pylovo_api.openapi import SNAPSHOT, UPDATE_HINT, render


def test_openapi_snapshot_is_current(project):
    current = render(create_app(project))
    committed = SNAPSHOT.read_text(encoding="utf-8") if SNAPSHOT.is_file() else ""
    if committed != current:
        diff = list(difflib.unified_diff(committed.splitlines(), current.splitlines(), "api/openapi.json (committed)",
                                         "app", lineterm="", n=2))
        shown = "\n".join(diff[:80]) + ("\n…" if len(diff) > 80 else "")
        pytest.fail(f"The HTTP API differs from the contract snapshot api/openapi.json.\n{UPDATE_HINT}\n\n{shown}",
                    pytrace=False)


def test_snapshot_carries_the_contract_version_and_health():
    schema = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert schema["info"]["version"] == str(API_VERSION)
    assert "get" in schema["paths"]["/api/health"]
    assert all(path.startswith("/api/") for path in schema["paths"])  # headless: no page, no static files
