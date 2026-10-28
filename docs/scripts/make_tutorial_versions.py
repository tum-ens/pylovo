"""Generate the version variants of the tutorials and export them into ``data/examples/versions``.

The versions notebook compares the example areas of the Bavaria run (version ``1``) with four variants that
change one setting each. This script writes them with ``pylovo-generate --plz 80805 85256 85307 --no-mv`` from
temporary copies of ``config/`` (the repository configuration stays untouched) and exports them read-only with
:func:`pylovo.examples.export_example_data` (``variant_of="1"``).

Run it against a database you may write to, which holds the setup and the input data of the three areas, e.g. a
sandbox with external basedata (``PYLOVO_ENV_FILE=/path/to/.env.sandbox``). ``--database`` must name the target
database, so the script never writes to the database of another ``.env`` by accident. From the repository root::

    PYLOVO_ENV_FILE=.env.sandbox uv run --extra plots python docs/scripts/make_tutorial_versions.py --database NAME

Add ``--with-baseline`` to also generate ``tut-base`` (the settings of version 1) and check that it reproduces
the grids of version 1. The script only adds versions; existing versions are skipped by pylovo.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AREAS = (80805, 85256, 85307)
MAP_AREA = 85256

#: Version id -> configuration overrides (on top of config/config_generation_lv.yaml).
TUTORIAL_VERSIONS: dict[str, dict] = {
    "tut-base": {"VERSION_COMMENT": "tutorial baseline: the settings of the Bavaria run (version 1)"},
    "tut-osm": {"VERSION_COMMENT": "tutorial: existing station positions from open data (OSM, LoD2)",
                "USE_OPEN_TRANSFORMER_POSITIONS": True},
    "tut-reach": {"VERSION_COMMENT": "tutorial: greenfield stations may reach 1500 m instead of 1000 m",
                  "MAX_GREENFIELD_TRAFO_DISTANCE": 1500},
    "tut-cabin": {"VERSION_COMMENT": "tutorial: houses connected through cable cabinets instead of tee joints",
                  "HOUSE_CONNECTION": "cabinet"},
    "tut-large": {"VERSION_COMMENT": "tutorial: one rating step larger stations (rural 400/630, else 630/800 kVA)",
                  "TRANSFORMER_MAPPING": {1: [400, 630], 2: [630, 800], 3: [630, 800]}},
}


def _pylovo_command(name: str) -> str:
    """Return the path of a pylovo console script next to the running interpreter."""
    candidate = Path(sys.executable).with_name(name)
    return str(candidate) if candidate.exists() else name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--database", required=True, help="name of the target database (must match DBNAME)")
    parser.add_argument("--with-baseline", action="store_true", help="also generate tut-base")
    parser.add_argument("--export-only", action="store_true", help="only export the versions that exist")
    args = parser.parse_args()

    from pylovo import config_loader

    if args.database != config_loader.DBNAME:
        sys.exit(f"--database {args.database} does not match the configured database {config_loader.DBNAME}")
    print(f"Target database: {config_loader.HOST}:{config_loader.PORT}/{config_loader.DBNAME}")
    versions = {k: v for k, v in TUTORIAL_VERSIONS.items() if args.with_baseline or k != "tut-base"}
    # the generation reads its reference data (transformer positions, ...) from the data directory of the checkout
    environment = {**os.environ, "PYLOVO_DATA_DIR": os.environ.get("PYLOVO_DATA_DIR", str(REPO_ROOT / "data"))}
    if not args.export_only:
        base_config = yaml.safe_load((REPO_ROOT / "config" / "config_generation_lv.yaml").read_text(encoding="utf-8"))
        for version_id, overrides in versions.items():
            with tempfile.TemporaryDirectory(prefix=f"pylovo_{version_id}_") as tmp:
                workdir = Path(tmp)
                shutil.copytree(REPO_ROOT / "config", workdir / "config")
                config = dict(base_config, VERSION_ID=version_id, **overrides)
                with open(workdir / "config" / "config_generation_lv.yaml", "w", encoding="utf-8") as handle:
                    yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
                command = [_pylovo_command("pylovo-generate"), "--plz", *map(str, AREAS), "--no-mv"]
                print(f"$ (cd {workdir}) {' '.join(command)}")
                subprocess.run(command, cwd=workdir, env=environment, check=True, stdin=subprocess.DEVNULL)

    from pylovo.examples import EXAMPLE_AREAS, export_example_data

    for version_id in versions:
        if version_id == "tut-base":
            continue
        folder = REPO_ROOT / "data" / "examples" / "versions" / version_id
        export_example_data(folder, {plz: EXAMPLE_AREAS[plz] for plz in AREAS}, version_id=version_id,
                            map_areas=[MAP_AREA], variant_of="1")
        print(f"wrote {folder.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
