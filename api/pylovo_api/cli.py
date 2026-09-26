"""``pylovo-api``: start the HTTP API server."""
from __future__ import annotations

import argparse
import os
import sys

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


def main(argv: list[str] | None = None) -> None:
    """Parse arguments, switch to the project root and run the web server."""
    parser = argparse.ArgumentParser(
        prog="pylovo-api",
        description="Headless HTTP API for pylovo (used by the GridPlanner UI): set up the database, prepare "
                    "transformer data, edit the generation config, generate grids and inspect the results.",
        epilog="The API can drop database schemas and delete results. It binds to 127.0.0.1 by default; "
               "only use --host 0.0.0.0 on a trusted network.")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default: 8765)")
    parser.add_argument("--root", help="pylovo project directory with config/ (default: auto-detect)")
    parser.add_argument("--log-level", default="warning", choices=["critical", "error", "warning", "info", "debug"],
                        help="uvicorn log level (default: warning)")
    parser.add_argument("--allowed-host", action="append", default=[], metavar="HOST",
                        help="also accept this Host header name, e.g. of a reverse proxy (repeatable; also "
                             f"{ENV_ALLOWED_HOSTS}, comma-separated)")
    args = parser.parse_args(argv)
    extra_hosts = host_names([*args.allowed_host, *os.getenv(ENV_ALLOWED_HOSTS, "").split(",")])

    from pylovo_api.settings import find_project_root

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

    bind_all = args.host in ("0.0.0.0", "::")
    app = create_app(root, allowed_hosts={args.host, *extra_hosts}, allow_any_host=bind_all)
    if bind_all:
        print("WARNING: pylovo-api is reachable from other machines. Anyone who can reach it can drop your schema.",
              file=sys.stderr)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"pylovo-api for {root}\n  → {url}/api/health  (API docs: {url}/docs; Ctrl+C to stop)")
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level,
                                           timeout_graceful_shutdown=3))
    app.state.server = server  # log streams end as soon as the server is asked to stop (Ctrl+C)
    server.run()  # running jobs are stopped and recorded as cancelled on exit


if __name__ == "__main__":
    main()
