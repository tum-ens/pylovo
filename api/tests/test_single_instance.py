"""One ``pylovo-api`` per project directory: a second server must stop before it touches the first one's jobs."""
from __future__ import annotations

import os

import pytest
from pylovo_api.cli import claim_project, main
from pylovo_api.settings import Paths

pytest.importorskip("fcntl")  # the lock is not available on Windows


def test_second_claim_exits_until_the_first_is_released(project):
    state_dir = Paths(root=project).state_dir
    first = claim_project(state_dir)
    with pytest.raises(SystemExit) as exc:
        claim_project(state_dir)
    assert f"process {os.getpid()}" in str(exc.value) and str(project) in str(exc.value)
    assert (state_dir / "server.lock").read_text() == str(os.getpid())  # the refused claim left it alone
    first.close()
    claim_project(state_dir).close()


def test_second_server_exits_before_the_app_is_created(project, monkeypatch):
    import pylovo_api.app

    def create_app(*args, **kwargs):
        pytest.fail("a second server must not create the app (its shutdown would stop the first one's job)")

    monkeypatch.setattr(pylovo_api.app, "create_app", create_app)
    monkeypatch.chdir(project)  # main changes the working directory
    first = claim_project(Paths(root=project).state_dir)
    try:
        with pytest.raises(SystemExit) as exc:
            main(["--root", str(project)])
        assert "already runs" in str(exc.value)
    finally:
        first.close()
