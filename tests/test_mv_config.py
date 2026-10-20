"""The ``MV_*`` keys of ``config_generation.yaml`` and their loader block in ``pylovo.config_loader``.

The import tests run ``import pylovo.config_loader`` in a subprocess against a temporary copy of ``config/``
(the pattern of ``tests/test_generation_config.py``); none of them opens a database connection.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from pylovo.mv_grid.options import YAML_KEYS, coerce_mv_value

CONFIG = Path(__file__).resolve().parents[1] / "config"
PROBE = ("import pylovo.config_loader as c; "
         "print(c.MV_GRID_GENERATION, c.MV_VARIANT_ID, c.MV_NOMINAL_VOLTAGE_KV, c.MV_MAX_STATIONS_PER_RING, "
         "c.MV_LINE_PLANNING_UTILIZATION, "
         "c.MV_STANDARD_CABLE, c.INFDB_OSM_SCHEMA is None or isinstance(c.INFDB_OSM_SCHEMA, str))")


def shipped_text() -> str:
    return (CONFIG / "config_generation.yaml").read_text(encoding="utf-8")


def test_shipped_values_equal_the_defaults():
    """Loader defaults equal the shipped values, so configurations without the keys behave the same."""
    values = yaml.safe_load(shipped_text())
    for name, spec in YAML_KEYS.items():
        assert name in values, name
        assert coerce_mv_value(name, values[name]) == spec.default, name


def test_every_key_has_its_help_text_directly_above_or_inline():
    """The API shows the comment lines directly above a key (or its inline comment) as help text."""
    lines = shipped_text().splitlines()
    first = min(i for i, line in enumerate(lines) if line.startswith("MV_GRID_GENERATION:"))
    assert lines.index("# MEDIUM-VOLTAGE (MV) GRIDS") < first
    for i, line in enumerate(lines):
        key = line.split(":", 1)[0]
        if key in YAML_KEYS:
            assert lines[i - 1].startswith("# ") or " # " in line, key
    keys = [line.split(":", 1)[0] for line in lines if line[:1].isupper() and ":" in line]
    assert keys[-len(YAML_KEYS):] == list(YAML_KEYS)          # the last section, in the order of YAML_KEYS


def test_loader_exposes_the_keys():
    from pylovo import config_loader

    for name, spec in YAML_KEYS.items():
        assert getattr(config_loader, name) == spec.default, name
    names = {name for name in vars(config_loader) if name.startswith("MV_")}
    assert names == {*YAML_KEYS, "MV_DIRECT_CONNECTION_LOAD_THRESHOLD_KW"}     # the latter is an LV-version key


def run_probe(tmp_path: Path, replace: dict[str, str | None]) -> subprocess.CompletedProcess:
    """Import the loader with ``config_generation.yaml`` changed: key -> new line (None drops the key line; a key
    that is not in the file is appended)."""
    shutil.copytree(CONFIG, tmp_path / "config")
    path = tmp_path / "config" / "config_generation.yaml"
    out, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        key = line.split(":", 1)[0]
        if key in replace:
            seen.add(key)
            if replace[key] is not None:
                out.append(f"{key}: {replace[key]}")
        else:
            out.append(line)
    out += [f"{key}: {value}" for key, value in replace.items() if key not in seen and value is not None]
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return subprocess.run([sys.executable, "-c", PROBE], cwd=tmp_path, capture_output=True, text=True, check=False)


def test_a_config_without_mv_keys_imports_with_the_defaults(tmp_path):
    proc = run_probe(tmp_path, {name: None for name in YAML_KEYS})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["False", "1", "auto", "20", "0.6", "None", "True"]


def test_valid_values_are_coerced(tmp_path):
    proc = run_probe(tmp_path, {"MV_VARIANT_ID": "7", "MV_NOMINAL_VOLTAGE_KV": "10", "MV_MAX_STATIONS_PER_RING": "15",
                                "MV_LINE_PLANNING_UTILIZATION": "0.5",
                                "MV_STANDARD_CABLE": "'NA2XS2Y 1x240 RM/25 6/10 kV'"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split()[:5] == ["False", "7", "10.0", "15", "0.5"]


def test_removed_keys_of_older_configurations(tmp_path):
    """An older configuration may keep a removed key with the value the stage still has, but no other value."""
    proc = run_probe(tmp_path, {"MV_RING_SOLVER": "savings", "MV_STUBS": "rural", "MV_SUPPLY_AREAS": "routed"})
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("key, value, message", [
    ("MV_RING_SOLVER", "sweep", "MV_RING_SOLVER 'sweep' is no longer supported"),
    ("MV_NOMINAL_VOLTAGE_KV", "15", "MV_NOMINAL_VOLTAGE_KV must be one of auto, 20, 10"),
    ("MV_MAX_STATIONS_PER_RING", "0", "MV_MAX_STATIONS_PER_RING must be between 1 and 100"),
    ("MV_GRID_GENERATION", '"False"', "MV_GRID_GENERATION must be True or False"),
    ("MV_VARIANT_ID", '"variant-too-long"', "MV_VARIANT_ID must be a text of 1 to 10 characters"),
    ("MV_SUPPLY_AREAS", "egon", "MV_SUPPLY_AREAS 'egon' is no longer supported"),
])
def test_invalid_values_fail_at_import_with_the_key_name(tmp_path, key, value, message):
    proc = run_probe(tmp_path, {key: value})
    assert proc.returncode != 0
    assert message in proc.stderr.strip().splitlines()[-1]
