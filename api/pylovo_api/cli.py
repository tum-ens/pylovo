"""``pylovo-api``: start the HTTP API server."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import IO

ENV_ALLOWED_HOSTS = "PYLOVO_API_ALLOWED_HOSTS"


def host_names(values: list[str]) -> set[str]:
    """Host names of ``host[:port]`` values (the ``Host`` check ignores the port)."""
    names = set()
    for value in values:
        value = value.strip().lower()
        if not value:
            continue
        names.add(value.split("]")[0] + "]" if value.startswith("[") else value.rsplit(":", 1)[0])
    return names


def claim_project(state_dir: Path) -> IO[str] | None:
    """Take the lock of the one ``pylovo-api`` of a project directory, or exit.

    A server adopts the running jobs that an earlier server left in the job records and stops them on
    exit. uvicorn also runs that exit when the port is taken. A second server on the same directory would
    therefore stop the job of the first one, write into its job log and delete its job files.

    Args:
        state_dir: Private working directory of the API (``Paths.state_dir``).

    Returns:
        The open lock file. The lock ends when the handle is closed or the process exits, so there is no
        stale lock to remove. ``None`` where file locks are not available (Windows).
    """
    try:
        import fcntl
    except ImportError:  # no fcntl on Windows: no lock there
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    handle = open(state_dir / "server.lock", "a+", encoding="utf-8")
    try:
        # flock, not lockf: two handles in one process must conflict as well
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        pid = handle.read().strip() or "unknown"
        handle.close()
        sys.exit(f"pylovo-api already runs for {state_dir.parent} (process {pid}). Use that server or stop it "
                 "first: a second one would stop its running job.")
    handle.truncate(0)
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, switch to the project root and run the web server."""
    parser = argparse.ArgumentParser(
        prog="pylovo-api",
        description="Headless HTTP API of pylovo for browser clients and scripts: set up the database, prepare "
                    "transformer data, edit the generation config, generate grids and inspect the results.",
        epilog="The API can drop database schemas and delete results. It binds to 127.0.0.1 by default; "
               "only use --host 0.0.0.0 on a trusted network. Requests need a Host name of localhost, the --host "
               "value or an --allowed-host, also when all interfaces are bound.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument("--root", help="pylovo project directory with config/ (default: auto-detect)")
    parser.add_argument("--log-level", default="warning", choices=["critical", "error", "warning", "info", "debug"],
                        help="uvicorn log level (default: warning)")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="HOST",
                        help="also accept this Host header name (without port), e.g. of a reverse proxy or the address "
                             f"other machines use (repeatable; also {ENV_ALLOWED_HOSTS}, comma-separated)")
    args = parser.parse_args(argv)
    extra_hosts = host_names([*args.allowed_host, *os.getenv(ENV_ALLOWED_HOSTS, "").split(",")])

    from pylovo_api.settings import Paths, find_project_root

    try:
        root = find_project_root(args.root)
    except FileNotFoundError as exc:
        parser.error(str(exc))
    # pylovo resolves config/ relative to the working directory, exactly as the CLI does.
    os.chdir(root)

    try:
        import uvicorn
    except ImportError:
        sys.exit("pylovo-api needs the 'api' extra: uv sync --extra api")

    from pylovo_api.app import create_app

    lock = claim_project(Paths(root=root).state_dir)
    bind_all = args.host in ("0.0.0.0", "::")
    app = create_app(root, allowed_hosts={args.host, *extra_hosts})
    if bind_all:
        print("WARNING: pylovo-api is reachable from other machines. Anyone who can reach it can drop your schema.\n"
              "It answers only requests whose Host is localhost or one of the --allowed-host names "
              f"({', '.join(sorted(extra_hosts)) or 'none given'}).", file=sys.stderr)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"pylovo-api for {root}\n  → {url}/api/health  (API docs: {url}/docs; Ctrl+C to stop)")
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level,
                                           timeout_graceful_shutdown=3))
    app.state.project_lock = lock  # held until server.run() returns
    app.state.server = server  # log streams end as soon as the server is asked to stop (Ctrl+C)
    server.run()  # running jobs are stopped and recorded as cancelled on exit


if __name__ == "__main__":
    main()
