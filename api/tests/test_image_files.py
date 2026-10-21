"""The container image without ``docker build``: no plotting packages, an unprivileged user, a health check, safe mounts."""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap

import yaml
from conftest import ROOT

DOCKER = ROOT / "api" / "docker"
PLOTS_PACKAGES = ("matplotlib", "mpl_toolkits", "plotly", "seaborn", "contextily", "ipywidgets", "nbformat", "igraph")


def test_the_api_and_its_job_commands_import_without_the_plots_packages():
    """The image installs no ``plots`` extra, so nothing the server or a job starts may need these packages."""
    from pylovo_api.jobs import SCRIPT_MODULES

    modules = ["pylovo_api.app", "pylovo_api.tasks", "pylovo_api.chain", *SCRIPT_MODULES.values()]
    code = textwrap.dedent(f"""
        import importlib, sys

        class Blocked:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {PLOTS_PACKAGES!r}:
                    raise ImportError(name + " is not installed in the image")

        sys.meta_path.insert(0, Blocked())
        for module in {modules!r}:
            importlib.import_module(module)
        print("imported", len({modules!r}))
    """)
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=300, check=False)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == f"imported {len(modules)}"


def test_the_dockerfile_installs_no_plots_extra_and_runs_unprivileged_with_a_health_check():
    text = (DOCKER / "Dockerfile").read_text(encoding="utf-8")
    syncs = [line for line in text.splitlines() if "uv sync" in line]
    assert len(syncs) == 2 and all("--extra api" in line and "plots" not in line for line in syncs)
    assert re.search(r"^USER pylovo$", text, re.MULTILINE)
    assert re.search(r"^HEALTHCHECK .*\\\n\s+CMD .*/api/health", text, re.MULTILINE)


def test_compose_stops_when_a_mounted_path_is_missing_and_passes_the_revision():
    service = yaml.safe_load((DOCKER / "compose.yaml").read_text(encoding="utf-8"))["services"]["pylovo-api"]
    assert "PYLOVO_REVISION" in service["build"]["args"]
    mounts = {mount["target"]: mount for mount in service["volumes"]}
    assert set(mounts) == {"/app/.env", "/app/config", "/app/data", "/app/log", "/app/.pylovo-api"}
    assert all(mount["type"] == "bind" and mount["bind"]["create_host_path"] is False for mount in mounts.values())
    assert mounts["/app/.env"]["read_only"] is True
