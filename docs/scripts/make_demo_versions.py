"""Generate the grid versions that the documentation figures are made from.

The figures in ``docs/source/images/generation`` show the OSM-derived demo region
PLZ 85653 (Aying). They need a pylovo database with a completed ``pylovo-setup``
and the demo region in the InfDB-shaped ``basedata``/``opendata`` schemas.

Three versions are generated with ``pylovo-generate --plz 85653``:

* the version in ``config/config_generation.yaml`` as it is (default, greenfield);
* ``docs_bf``: the same configuration with ``USE_OPEN_TRANSFORMER_POSITIONS: True``
  (brownfield: existing OSM/LoD2 transformer positions are used);
* ``docs_km``: the same configuration with ``MAX_BUILDINGS_PER_KCID: 150`` so that
  the street network component of the demo region is split by k-means.

The extra versions are created from temporary copies of ``config/`` so the
repository configuration stays untouched. Run from the repository root::

    uv run python docs/scripts/make_demo_versions.py

The script only adds versions; existing versions are skipped by pylovo.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_PLZ = 85653

#: Version id -> configuration overrides (on top of config/config_generation.yaml).
DEMO_VERSIONS: dict[str, dict] = {
    "docs_bf": {
        "VERSION_COMMENT": "docs demo: brownfield with open transformer positions",
        "USE_OPEN_TRANSFORMER_POSITIONS": True,
    },
    "docs_km": {
        "VERSION_COMMENT": "docs demo: small k-means clusters",
        "MAX_BUILDINGS_PER_KCID": 150,
    },
}


def _pylovo_command(name: str) -> str:
    """Return the path of a pylovo console script next to the running interpreter."""
    candidate = Path(sys.executable).with_name(name)
    return str(candidate) if candidate.exists() else name


def _print_target_database() -> None:
    """Print the database that the pylovo commands will write to."""
    from pylovo import config_loader

    print(f"Target database: {config_loader.HOST}:{config_loader.PORT}/{config_loader.DBNAME}")


def _run_generate(workdir: Path, plz: int) -> None:
    command = [_pylovo_command("pylovo-generate"), "--plz", str(plz)]
    print(f"$ (cd {workdir}) {' '.join(command)}")
    subprocess.run(command, cwd=workdir, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plz", type=int, default=DEMO_PLZ, help="Postcode to generate (default: 85653)")
    parser.add_argument("--skip-default", action="store_true", help="Do not generate the default version")
    args = parser.parse_args()

    _print_target_database()

    if not args.skip_default:
        _run_generate(REPO_ROOT, args.plz)

    base_config = yaml.safe_load((REPO_ROOT / "config" / "config_generation.yaml").read_text(encoding="utf-8"))
    for version_id, overrides in DEMO_VERSIONS.items():
        with tempfile.TemporaryDirectory(prefix=f"pylovo_{version_id}_") as tmp:
            workdir = Path(tmp)
            shutil.copytree(REPO_ROOT / "config", workdir / "config")
            config = dict(base_config, VERSION_ID=version_id, **overrides)
            with open(workdir / "config" / "config_generation.yaml", "w", encoding="utf-8") as handle:
                yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
            _run_generate(workdir, args.plz)


if __name__ == "__main__":
    main()
