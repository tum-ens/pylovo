"""``pylovo-viewer``: start the map viewer, or download its context data.

    pylovo-viewer [--env FILE] [--host HOST] [--port PORT] [--no-browser] [--data-dir DIR]
    pylovo-viewer fetch etrago [--scenario NAME] [--data-dir DIR]

The arguments are parsed before anything else, so ``--help`` never touches the database.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8770


def default_data_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "pylovo-viewer"


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pylovo-viewer",
        description="Read-only browser map of the pylovo database in the .env and the other pylovo databases "
                    "of its server: LV and MV grids, input data and grid context. It never writes to a database.")
    p.add_argument("--env", help="the .env with the database settings (default: the one pylovo uses); "
                                 "PYLOVO_VIEWER_DATABASES there limits the databases offered")
    p.add_argument("--host", default="127.0.0.1",
                   help="interface to listen on (default 127.0.0.1; there is no login, so only widen this on a "
                        "trusted network)")
    p.add_argument("--port", type=int, help=f"port (default {DEFAULT_PORT}, or the next free one)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.add_argument("--data-dir", type=Path, default=default_data_dir(),
                   help="folder for downloaded data such as eTraGo (default %(default)s)")
    sub = p.add_subparsers(dest="command", metavar="fetch")
    fetch = sub.add_parser("fetch", help="download context data once (eTraGo grid of Germany)")
    fetch.add_argument("dataset", choices=["etrago"], help="etrago: 110-380 kV grid of eGon-data (about 40 s)")
    fetch.add_argument("--scenario", default="eGon2035", help="eGon scenario (default %(default)s)")
    fetch.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    return p


def free_port(host: str, wanted: int | None) -> int:
    """``wanted`` if given (it must be free), else the first free port from :data:`DEFAULT_PORT`."""
    for port in ([wanted] if wanted else range(DEFAULT_PORT, DEFAULT_PORT + 30)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"pylovo-viewer: port {wanted} is in use" if wanted else "pylovo-viewer: no free port found")


def can_open_browser() -> bool:
    """A browser can show the page here (VS Code's remote terminals set BROWSER to open it locally)."""
    return bool(os.environ.get("BROWSER") or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                or sys.platform in ("darwin", "win32"))


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "fetch":
        from pylovo_viewer import etrago
        meta = etrago.fetch(args.data_dir, args.scenario, log=lambda line: print(f"  {line}", flush=True))
        print(f"eTraGo {args.scenario}: {meta['counts']['lines']} lines and {meta['counts']['buses']} buses in "
              f"{etrago.dataset_dir(args.data_dir, args.scenario)}")
        return

    try:
        import uvicorn
    except ImportError:
        raise SystemExit("pylovo-viewer needs the 'viewer' extra: uv sync --extra viewer") from None
    from pylovo_viewer.app import LOCAL_HOSTS, create_app
    from pylovo_viewer.db import Database, DatabaseError
    from pylovo_viewer.settings import SettingsError, find_env_file, load_settings

    try:
        env_file = find_env_file(args.env)
        settings = load_settings(env_file)
    except SettingsError as exc:
        raise SystemExit(f"pylovo-viewer: {exc}") from None
    db = Database(settings)
    try:
        db.one("SELECT 1")
        app = create_app(settings, args.data_dir, db, local_only=args.host in LOCAL_HOSTS)
    except DatabaseError as exc:
        raise SystemExit(f"pylovo-viewer: cannot use {settings.describe()} ({env_file}): {exc}") from None
    port = free_port(args.host, args.port)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{port}/"
    print(f"pylovo viewer: {url}\n  database {settings.describe()} (read-only), settings from {env_file}", flush=True)
    if args.host not in LOCAL_HOSTS:
        print("  warning: listening beyond this machine; the viewer has no login", flush=True)
    if not args.no_browser and can_open_browser():
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
