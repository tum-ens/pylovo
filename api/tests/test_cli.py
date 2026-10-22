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


@pytest.mark.parametrize("argv", [["--open"], ["--plugin", "gridexpand=/gridexpand/"]])
def test_browser_options_are_gone(argv, capsys):
    with pytest.raises(SystemExit) as exc:  # argparse refuses them before anything starts
        main(argv)
    assert exc.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize("bind", ["0.0.0.0", "::"])
def test_binding_all_interfaces_keeps_the_host_allow_list(project, monkeypatch, capsys, bind):
    import uvicorn

    started = {}

    class Server:
        def __init__(self, config):
            started["app"] = config.app

        def run(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.delenv("PYLOVO_API_ALLOWED_HOSTS", raising=False)
    monkeypatch.chdir(project)
    main(["--root", str(project), "--host", bind, "--allowed-host", "Proxy.example:8080"])
    client = TestClient(started["app"])
    assert client.get("/api/health", headers={"Host": "proxy.example"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "192.168.1.5:8765"}).status_code == 421
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 421
    err = capsys.readouterr().err
    assert "WARNING" in err and "proxy.example" in err
