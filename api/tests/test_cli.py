"""``pylovo-api`` options: accepted Host names for a reverse proxy, removed browser options."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pylovo_api.app import create_app
from pylovo_api.cli import host_names, main


def test_allowed_hosts_for_a_proxy(project):
    assert host_names(["GridPlanner.local:18780", "[::1]:80", "", "proxy"]) == {"gridplanner.local", "[::1]", "proxy"}
    client = TestClient(create_app(project, allowed_hosts=host_names(["gridplanner.local:18780"])))
    assert client.get("/api/health", headers={"Host": "gridplanner.local:18780"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "127.0.0.1:18780"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "evil.example:18780"}).status_code == 421
    assert TestClient(create_app(project, allow_any_host=True)).get(
        "/api/health", headers={"Host": "evil.example"}).status_code == 200  # --host 0.0.0.0


@pytest.mark.parametrize("argv", [["--open"], ["--plugin", "gridexpand=/gridexpand/"]])
def test_browser_options_are_gone(argv, capsys):
    with pytest.raises(SystemExit) as exc:  # argparse refuses them before anything starts
        main(argv)
    assert exc.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err
