"""``GET /api/health``: liveness and contract version without database access."""
from __future__ import annotations

import subprocess
from importlib.metadata import version

import psycopg2
import pylovo_api
from fastapi.testclient import TestClient
from pylovo_api import code_state
from pylovo_api.app import create_app


def test_health_answers_without_database(project, monkeypatch):
    calls = []

    def refuse(*_a, **_k):
        calls.append(1)
        raise AssertionError("/api/health must not connect to the database")

    monkeypatch.setattr(psycopg2, "connect", refuse)
    app = create_app(project)
    touched = []
    monkeypatch.setattr(app.state.coverage, "touch", lambda: touched.append(1))
    bare = TestClient(app)  # no lifespan: the coverage worker does not start
    r = bare.get("/api/health")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert list(body) == ["ok", "service", "api", "version", "revision"]
    assert body["ok"] is True and body["service"] == "pylovo-api"
    assert body["api"] == pylovo_api.API_VERSION and isinstance(body["api"], int)
    assert body["version"] == version("pylovo")
    assert body["revision"] == code_state.STARTED_REVISION
    assert not calls and not touched  # no database, and health checks do not count as UI activity
    assert bare.get("/api/health", headers={"host": "evil.example"}).status_code == 421  # Host allowlist applies


def test_openapi_version_is_the_contract_version(project):
    assert create_app(project).openapi()["info"]["version"] == str(pylovo_api.API_VERSION)


def test_git_revision(monkeypatch):
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("PYLOVO_REVISION", sha)
    assert code_state.git_revision() == sha
    monkeypatch.setenv("PYLOVO_REVISION", "not a sha")
    assert code_state.git_revision() is None
    monkeypatch.delenv("PYLOVO_REVISION")

    def no_git(*_a, **_k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert code_state.git_revision() is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, "", "not a git repo"))
    assert code_state.git_revision() is None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, sha + "\n", ""))
    assert code_state.git_revision() == sha
