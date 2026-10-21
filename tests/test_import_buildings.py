"""File-based building import: which table a shapefile goes into (no database)."""
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
