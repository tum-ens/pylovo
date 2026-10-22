"""``pylovo-export``: PLZ without grids are reported and an empty export fails (no database)."""
import sys

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, Point

from pylovo.cli import export
from pylovo.plotting.gis_preparation import io_geodata


def frames(plz: int, grids: bool):
    if not grids:
        return gpd.GeoDataFrame(), gpd.GeoDataFrame()
    lines = gpd.GeoDataFrame({"plz": [plz]}, geometry=[LineString([(0, 0), (1, 1)])], crs="EPSG:4326")
    buses = gpd.GeoDataFrame({"plz": [plz, plz]}, geometry=[Point(0, 0), Point(1, 1)], crs="EPSG:4326")
    return lines, buses


@pytest.fixture()
def grids_of(monkeypatch):
    """Only the PLZ named by the test have grids."""
    have = set()
    monkeypatch.setattr(io_geodata, "get_bus_line_geo_for_plz", lambda plz: frames(plz, plz in have))
    return have


def test_plz_without_grids_are_left_out_and_returned(grids_of, tmp_path):
    grids_of.add(80331)
    lines, buses = tmp_path / "lines.csv", tmp_path / "bus.csv"
    missing = io_geodata.save_geodata_as_csv(pd.DataFrame({"plz": [80331, 80333]}), str(lines), str(buses))
    assert missing == [80333]
    assert set(pd.read_csv(lines)["plz"]) == {80331} and len(pd.read_csv(buses)) == 2


def test_nothing_is_written_when_no_plz_has_grids(grids_of, tmp_path):
    lines, buses = tmp_path / "lines.csv", tmp_path / "bus.csv"
    lines.write_text("earlier export\n")
    missing = io_geodata.save_geodata_as_csv(pd.DataFrame({"plz": [80333]}), str(lines), str(buses))
    assert missing == [80333]
    assert lines.read_text() == "earlier export\n" and not buses.exists()


def run(monkeypatch, capsys, without_grids, *argv):
    monkeypatch.setattr(export, "save_geodata_as_csv", lambda df_plz, data_path_lines, data_path_bus: without_grids)
    monkeypatch.setattr(sys, "argv", ["pylovo-export", *argv])
    code = 0
    try:
        export.main()
    except SystemExit as exc:
        code = exc.code
    return code, capsys.readouterr().out


def test_an_empty_export_warns_and_exits_1(monkeypatch, capsys, tmp_path):
    code, out = run(monkeypatch, capsys, [80333], "--plz", "80333", "--output", str(tmp_path))
    assert code == 1
    assert "✗ PLZ 80333 has no grids" in out and "Nothing exported" in out and "✓" not in out


def test_a_partly_empty_export_warns_but_succeeds(monkeypatch, capsys, tmp_path):
    code, out = run(monkeypatch, capsys, [80333], "--plz", "80331", "80333", "--output", str(tmp_path))
    assert code == 0
    assert "✗ PLZ 80333 has no grids" in out and "✓ Exported geodata for PLZ: [80331]" in out


def test_the_file_names_stay_those_of_the_qgis_templates(monkeypatch, capsys, tmp_path):
    for plz_args, suffix in ((["80331"], "single_grid"), (["80331", "80333"], "multiple_grids")):
        _, out = run(monkeypatch, capsys, [], "--plz", *plz_args, "--output", str(tmp_path))
        assert f"lines_{suffix}.csv" in out and f"bus_{suffix}.csv" in out


def test_kcid_and_bcid_without_grid_are_reported_as_ignored(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(export, "save_geodata_as_csv", lambda df_plz, data_path_lines, data_path_bus: [])
    monkeypatch.setattr(sys, "argv", ["pylovo-export", "--plz", "80331", "--kcid", "4", "--bcid", "30",
                                      "--output", str(tmp_path)])
    export.main()
    assert "Warning: --kcid and --bcid are ignored without --grid" in capsys.readouterr().err
