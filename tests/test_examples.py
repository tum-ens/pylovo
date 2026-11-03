"""The example data of the tutorials (``data/examples``) and :class:`pylovo.examples.TutorialData` without a database.

No database and no internet: every test reads the example data of the repository.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("geopandas")

from pylovo import examples  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[1] / "data" / "examples"


@pytest.fixture(autouse=True)
def example_data(monkeypatch):
    monkeypatch.setenv("PYLOVO_EXAMPLE_DIR", str(EXAMPLES))


def test_the_manifest_lists_the_example_areas():
    manifest = json.loads((EXAMPLES / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest["areas"]) == sorted(str(plz) for plz in examples.EXAMPLE_AREAS)
    for plz, area in manifest["areas"].items():
        assert tuple(area["grid"]) == tuple(examples.EXAMPLE_AREAS[int(plz)]["grid"])
        assert (EXAMPLES / area["file"]).is_file()
        assert all((EXAMPLES / net).is_file() for net in area["nets"].values())
    assert manifest["attribution"] == examples.EXAMPLE_ATTRIBUTION


@pytest.mark.parametrize("plz", sorted(examples.EXAMPLE_AREAS))
def test_an_example_area_reads_like_the_database(plz):
    data = examples.TutorialData(plz, source="examples")
    assert data.source == "example data" and data.version_id == "1"
    layers = data.layers
    assert {"postcode", "grids", "buildings", "lines", "streets", "connections", "exclusions"} <= set(layers)
    assert len(layers["grids"]) == len(data.grid_ids) > 0
    assert layers["lines"].crs.to_epsg() == 25832
    assert data.typical_grid in data.grid_ids
    net = data.net(*data.typical_grid)
    assert len(net.line) > 0 and not net.res_bus.empty
    assert data.voltage_band["lv_reference_pu"] == pytest.approx(0.96)
    assert data.feeder_limit_percent == 5
    assert len(data.summary()) == len(data.grid_ids)
    levels = data.voltage_levels()
    assert (levels["chain"]["kcid"], levels["chain"]["bcid"]) == data.typical_grid
    assert {"lines", "buses"} <= set(levels["hv"]) and len(levels["mv_grid"]["lines"]) > 0


def test_the_region_tables_hold_all_postcode_areas():
    data = examples.TutorialData(85307, source="examples")
    summary = data.region_summary()
    assert summary["plz"].nunique() > 1000 and {1, 2, 3} <= set(summary["settlement_type"])
    assert len(data.region_summary([85307, 85256])) == 15 + 26
    assert len(data.postcodes()) == summary["plz"].nunique() + 1     # one postcode area has no grid


def test_the_example_data_hold_only_the_typical_nets():
    data = examples.TutorialData(85307, source="examples")
    other = next(grid for grid in data.grid_ids if grid != data.typical_grid)
    with pytest.raises(FileNotFoundError, match="nets of the grids"):
        data.net(*other)


VARIANTS = {"tut-osm": (23, 14, 42), "tut-reach": (24, 13, 50), "tut-cabin": (26, 15, 50), "tut-large": (21, 13, 37)}


@pytest.mark.parametrize("version", sorted(VARIANTS))
def test_a_version_variant_reads_its_own_grids_on_the_buildings_of_the_example_version(version):
    manifest = json.loads((EXAMPLES / "versions" / version / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["variant_of"] == "1" and manifest["version_id"] == version
    data = examples.TutorialData(85256, version_id=version, source="examples")
    assert data.version_id == version and data.source == "example data"
    base = examples.TutorialData(85256, source="examples").layers["buildings"]
    buildings = data.layers["buildings"]
    assert len(data.grid_ids) == VARIANTS[version][0]
    assert set(buildings["objectid"]) == set(base["objectid"])          # the same footprints, other grids
    assert len(data.layers["streets"]) == len(examples.TutorialData(85256, source="examples").layers["streets"])
    assert set(zip(buildings["kcid"], buildings["bcid"])) <= set(data.grid_ids)
    assert data.typical_grid in data.grid_ids                          # the median grid, not the one of version 1
    assert data.parameters["cable_dimensioning"]["vn"] == 400
    for plz, grids in zip((85307, 80805), VARIANTS[version][1:]):
        other = examples.TutorialData(plz, version_id=version, source="examples")
        assert len(other.summary()) == grids
        with pytest.raises(FileNotFoundError, match="not its map layers"):
            _ = other.layers


def test_an_unknown_version_names_the_versions_of_the_example_data():
    with pytest.raises(FileNotFoundError, match="hold version 1 and the variants tut-cabin, tut-large, tut-osm"):
        examples.TutorialData(85256, version_id="nope", source="examples")


def test_without_a_database_the_example_data_are_used(monkeypatch):
    monkeypatch.setattr(examples, "_open_database", lambda plz, version_id: (None, "no database in this test"))
    data = examples.TutorialData(85256)
    assert data.source == "example data" and data.reason == "no database in this test"
    assert "example data" in repr(data)
    with pytest.raises(FileNotFoundError, match="from the database"):
        examples.TutorialData(85256, source="database")
    with pytest.raises(FileNotFoundError, match="not in the example data"):
        examples.TutorialData(10115)


def test_the_tutorial_modules_import_without_any_configuration(tmp_path):
    """The example data are for readers without a database: the modules of the tutorials must not read .env."""
    code = ("import sys\n"
            "import pylovo.examples, pylovo.plotting.style, pylovo.plotting.lv_grid, pylovo.plotting.voltage_levels\n"
            "import pylovo.plotting.generation.networks\n"
            "assert 'pylovo.config_loader' not in sys.modules, 'the configuration was imported'\n")
    env = {k: v for k, v in os.environ.items() if k not in ("DBNAME", "DBUSER", "HOST", "PORT", "PASSWORD")}
    result = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
