"""Run pylovo's database tests on a disposable PostgreSQL container, as the CI does.

Usage (from the repository root; needs Docker and GDAL's ``ogr2ogr``)::

    uv run --locked --extra api --extra plots python tests/db/run_database_tests.py
    uv run --locked --extra api --extra plots python tests/db/run_database_tests.py -- -x -k toy_town

The script

1. starts the image of the CI (PostgreSQL 17, PostGIS 3.5, pgRouting 4.0, as the InfDB) as the container
   ``pylovo-test-db`` on ``127.0.0.1:55432`` and creates the databases of the four test groups,
2. builds the toy town into ``pylovo_test`` (``build_test_database.py``),
3. runs the database tests (:data:`TESTS`, arguments after ``--`` go to pytest) and
4. removes the container again (``--keep`` keeps it for a look with ``psql``).

All commands get ``PYLOVO_ENV_FILE=""``: the ``.env`` of the checkout is not read, so they reach only the
container, never the database of the ``.env``. The tests and their variables are listed in the development
guide (section "Database tests" of ``docs/source/development/index.rst``).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "pgrouting/pgrouting:17-3.5-4.0.1"
CONTAINER = "pylovo-test-db"
TESTS = ["tests/test_toy_town.py", "tests/test_postgis_fixtures.py", "tests/test_database_review.py", "api/tests"]


def _environment(port: int) -> dict:
    dsn = f"host=127.0.0.1 port={port} user=postgres password=postgres dbname="
    return {
        **os.environ,
        "PYLOVO_ENV_FILE": "",
        "DBNAME": "pylovo_test", "DBUSER": "postgres", "PASSWORD": "postgres", "HOST": "127.0.0.1", "PORT": str(port),
        "PYLOVO_TOY_TOWN_DATABASE": "pylovo_test",
        "PYLOVO_API_TEST_DATABASE": "pylovo_test",
        "PYLOVO_FIXTURE_DB": dsn + "pylovo_fixture",
        "PYLOVO_REVIEW_TEST_DSN": dsn + "pylovo_review",
    }


def _start_container(port: int) -> None:
    subprocess.run(["docker", "run", "-d", "--name", CONTAINER, "-p", f"127.0.0.1:{port}:5432",
                    "-e", "POSTGRES_PASSWORD=postgres", "-e", "POSTGRES_DB=pylovo_test", IMAGE],
                   check=True, stdout=subprocess.DEVNULL)
    # over TCP: the server of the image's first-start initialisation listens only on the socket
    ready = ["docker", "exec", CONTAINER, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "postgres"]
    for _ in range(120):
        if subprocess.run(ready).returncode == 0:
            break
        time.sleep(1)
    else:
        sys.exit(f"The container {CONTAINER} did not accept connections within two minutes.")
    for database in ("pylovo_fixture", "pylovo_review"):
        subprocess.run(["docker", "exec", CONTAINER, "psql", "-q", "-h", "127.0.0.1", "-U", "postgres",
                        "-c", f"CREATE DATABASE {database}"], check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=55432, help="host port of the container (default: 55432)")
    parser.add_argument("--keep", action="store_true", help=f"keep the container (remove it with docker rm -f {CONTAINER})")
    parser.add_argument("pytest_args", nargs="*", help="arguments for pytest, after --")
    args = parser.parse_args(argv)

    if subprocess.run(["docker", "container", "inspect", CONTAINER], capture_output=True).returncode == 0:
        sys.exit(f"The container {CONTAINER} exists already (from --keep?): remove it with docker rm -f {CONTAINER}.")
    env = _environment(args.port)
    _start_container(args.port)
    try:
        build = [sys.executable, "tests/db/build_test_database.py", "--database", "pylovo_test"]
        if subprocess.run(build, cwd=ROOT, env=env).returncode:
            return 1
        return subprocess.run([sys.executable, "-m", "pytest", *TESTS, *args.pytest_args], cwd=ROOT, env=env).returncode
    finally:
        if args.keep:
            print(f"Kept the container {CONTAINER}: psql -h 127.0.0.1 -p {args.port} -U postgres pylovo_test "
                  f"(password postgres); remove it with docker rm -f {CONTAINER}.")
        else:
            subprocess.run(["docker", "rm", "-f", CONTAINER], stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    sys.exit(main())
