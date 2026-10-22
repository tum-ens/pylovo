"""File-based building import: which table a shapefile goes into, and when a PLZ is imported (no database)."""
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from pylovo.data_import import import_buildings as ib


@pytest.mark.parametrize("path, table", [
    ("/data/buildings/Res_9162000.shp", "res"),
    ("/data/buildings/Oth_9162000.shp", "oth"),
    ("/data/Others/buildings/Res_9162000.shp", "res"),              # the folder name does not count
    ("/home/user/Research/buildings/Oth_9162000.shp", "oth"),
    ("/data/Residential/Oth_9162000.shp", "oth"),
])
def test_the_table_follows_the_file_name_only(path, table):
    assert ib.create_list_of_shp_files([path]) == [{"path": path, "table_name": table}]


def test_a_file_name_without_res_or_oth_is_refused_whatever_its_folders_are_called():
    with pytest.raises(ValueError, match="cannot be assigned to 'res' or 'oth'"):
        ib.create_list_of_shp_files(["/data/Res/Others/buildings_9162000.shp"])


class FakeLogger:
    def info(self, message):
        pass


class FakeClient:
    """The ``ags_log`` of the database: ``imported`` holds the AGS that are recorded as imported."""

    def __init__(self):
        self.imported = []

    def get_ags_log(self):
        return pd.DataFrame({"ags": self.imported}, dtype="int64")

    def write_ags_log(self, ags):
        self.imported.append(ags)


class FakeConstructor:
    """Records the files that would go to ``ogr2ogr`` instead of running it."""

    loaded = []

    def __init__(self, dbc_obj=None):
        pass

    def ogr_to_db(self, files):
        FakeConstructor.loaded.extend(files)


@pytest.fixture
def buildings_dir(monkeypatch, tmp_path):
    """The data directory of a PLZ with the two AGS 9175131 and 9184123 (no shapefile yet)."""
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ib, "resolve_regions", lambda client, plz: (None, pd.DataFrame({"ags": [9175131, 9184123]})))
    monkeypatch.setattr(ib, "DatabaseConstructor", FakeConstructor)
    FakeConstructor.loaded = []
    (tmp_path / "buildings").mkdir()
    return tmp_path / "buildings"


def add_shapefiles(folder, *ags_list):
    for ags in ags_list:
        for kind in ("Res", "Oth"):
            (folder / f"{kind}_{ags}.shp").touch()


def test_a_plz_with_a_missing_shapefile_imports_nothing_and_fails_every_time(buildings_dir):
    add_shapefiles(buildings_dir, 9175131)
    client = FakeClient()
    generator = SimpleNamespace(dbc=client, plz=85609, logger=FakeLogger())

    for _ in range(2):                                  # a repeated call behaves the same
        with pytest.raises(FileNotFoundError, match=r"\[9184123\].*nothing was imported"):
            ib.import_buildings_for_single_plz(generator)

    assert FakeConstructor.loaded == []
    assert client.imported == []


def test_a_plz_is_imported_when_the_shapefiles_of_all_its_ags_exist(buildings_dir):
    add_shapefiles(buildings_dir, 9175131, 9184123)
    client = FakeClient()

    ib.import_buildings_for_single_plz(SimpleNamespace(dbc=client, plz=85609, logger=FakeLogger()))

    assert sorted(Path(file["path"]).name for file in FakeConstructor.loaded) == [
        "Oth_9175131.shp", "Oth_9184123.shp", "Res_9175131.shp", "Res_9184123.shp"]
    assert sorted(client.imported) == [9175131, 9184123]
