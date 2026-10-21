"""Build the database of pylovo's database tests: the toy town (``toy_town.py``) plus its generated grids.

Usage (from the repository root, after creating an empty PostgreSQL >= 15 database with PostGIS and
pgRouting >= 3.8 available)::

    DBNAME=pylovo_test DBUSER=postgres PASSWORD=postgres HOST=127.0.0.1 PORT=5432 \\
        uv run --locked python tests/db/build_test_database.py --database pylovo_test

The database is the one pylovo's configuration points to (environment variables or a ``.env``, which
replaces environment variables, see :mod:`pylovo.config_loader`); ``--database`` must name it, so a ``.env``
of a real database is never used by accident. The script refuses a database that already has the pylovo
or the InfDB source schema. It

1. writes the toy town into the InfDB source schemas (``USE_INFDB=True``, no ``USE_EXTERNAL_BASEDATA``),
2. runs ``pylovo-setup`` (schema, transformers, postcodes and municipal register), then adds the toy
   postcodes to ``pylovo.municipal_register``,
3. runs ``pylovo-generate --plz 99001 99002`` and the per-grid analysis (``pylovo-analyze --per-grid``)
   for the versions of :data:`VERSIONS`: ``1`` with the ``config/`` of the checkout unchanged, ``2`` with
   the open (here: LoD2) transformer positions (brownfield), ``3`` residential-only.

The commands run in a temporary directory with a copy of ``config/`` (logs and results stay out of the
checkout), ``PYLOVO_ROOT`` points to the checkout for the reference data in ``data/``. It takes less than
a minute. The tests that use the database are listed in the development guide (section "Tests" of
``docs/source/development/index.rst``).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import toy_town

ROOT = Path(__file__).resolve().parents[2]
#: version id -> changes of config_generation_lv.yaml
VERSIONS = {
    "1": {},
    "2": {"VERSION_COMMENT": "toy town, open transformer positions", "USE_OPEN_TRANSFORMER_POSITIONS": True},
    "3": {"VERSION_COMMENT": "toy town, residential only", "RESIDENTIAL_ONLY_GENERATION": True},
}


def _run(command: list[str], cwd: Path, env: dict) -> None:
    started = time.time()
    print(f"$ {' '.join(command)}", flush=True)
    log = cwd / "build.log"
    with log.open("a") as out:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, check=False)
    if result.returncode:
        sys.exit(f"{command[0]} failed with exit code {result.returncode}:\n" + "".join(log.open().readlines()[-40:]))
    print(f"  done in {time.time() - started:.0f} s", flush=True)


def _command(name: str) -> str:
    local = Path(sys.executable).parent / name
    return str(local) if local.exists() else (shutil.which(name) or name)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database", required=True,
                        help="name of the empty database pylovo's configuration points to (confirmation)")
    args = parser.parse_args(argv)

    os.chdir(ROOT)  # the configuration is read relative to the working directory
    import psycopg2

    from pylovo import config_loader as cl

    if cl.DBNAME != args.database:
        sys.exit(f"pylovo's configuration points to the database {cl.DBNAME!r} (environment or .env), "
                 f"not to {args.database!r}: nothing was changed.")
    if not cl.USE_INFDB or cl.USE_EXTERNAL_BASEDATA:
        sys.exit("The toy town needs USE_INFDB=True and USE_EXTERNAL_BASEDATA=False: nothing was changed.")
    conn = psycopg2.connect(dbname=cl.DBNAME, user=cl.DBUSER, password=cl.PASSWORD, host=cl.HOST, port=cl.PORT)
    with conn, conn.cursor() as cur:
        cur.execute("SELECT nspname FROM pg_namespace WHERE nspname IN ('pylovo', %s)", (cl.INFDB_SOURCE_SCHEMA,))
        if existing := [row[0] for row in cur.fetchall()]:
            sys.exit(f"The database {cl.DBNAME!r} already has the schema(s) {', '.join(existing)}: "
                     "build the test database into an empty database.")
        cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
        counts = toy_town.write_source_data(cur, cl.INFDB_SOURCE_SCHEMA, cl.INFDB_OPENDATA_SCHEMA)
    print(f"Toy town written to {cl.HOST}:{cl.PORT}/{cl.DBNAME}: {counts}", flush=True)

    import yaml

    env = {**os.environ, "PYLOVO_ROOT": str(ROOT), "PYTHONHASHSEED": "0"}
    with tempfile.TemporaryDirectory(prefix="pylovo-test-db-") as tmp:
        project = Path(tmp)
        shutil.copytree(ROOT / "config", project / "config")
        _run([_command("pylovo-setup")], project, env)
        with conn, conn.cursor() as cur:
            toy_town.write_municipal_register(cur)
        path = project / "config" / "config_generation_lv.yaml"
        default = yaml.safe_load(path.read_text(encoding="utf-8"))
        for version_id, changes in VERSIONS.items():
            if changes:
                config = {**default, **changes, "VERSION_ID": version_id}
                path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
            plz = list(map(str, toy_town.GENERATED_PLZ))
            _run([_command("pylovo-generate"), "--plz", *plz, "--no-parallel", "--no-mv"], project, env)
            _run([_command("pylovo-analyze"), "--plz", *plz, "--per-grid"], project, env)
    conn.close()
    print(f"Test database {cl.DBNAME!r} is ready (versions {', '.join(VERSIONS)}).")


if __name__ == "__main__":
    main()
