"""The processed transformers of Bavaria are reprojected to TARGET_EPSG instead of fetched again (no database)."""
import geopandas as gpd
import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Point

from pylovo import utils
from pylovo.data_import import import_transformers as it
from pylovo.database import database_constructor
from pylovo.database.database_constructor import DatabaseConstructor

POINTS_25832 = [(701198.554, 5328188.674), (690000.0, 5300000.0)]


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    """An empty data directory (``PYLOVO_DATA_DIR``) and an empty folder for the copy that the wheel ships."""
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(utils, "get_packaged_defaults_dir", lambda: tmp_path / "packaged")
    return tmp_path / "data" / "transformer_data", tmp_path / "packaged" / "data" / "transformer_data"


@pytest.fixture
def no_download(monkeypatch):
    def fetch_or_process(relation_id):
        raise AssertionError("the transformers must not be fetched from the Overpass API")

    monkeypatch.setattr(it, "fetch_trafos", fetch_or_process)
    monkeypatch.setattr(it, "process_trafos", fetch_or_process)


def write_processed(folder, epsg, relation_id=7):
    path = folder / "processed_trafos" / f"{relation_id}_trafos_processed_{epsg}.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf = gpd.GeoDataFrame({"osm_id": ["node/1", "node/2"]}, geometry=[Point(xy) for xy in POINTS_25832], crs=25832)
    gdf.to_crs(epsg=epsg).to_file(path, driver="GeoJSON")
    return path


@pytest.mark.parametrize("shipped_in", ["data directory", "wheel"])
def test_the_25832_file_is_reprojected_instead_of_fetching_again(monkeypatch, dirs, no_download, shipped_in):
    data, packaged = dirs
    monkeypatch.setattr(it, "TARGET_EPSG", 3035)
    write_processed(data if shipped_in == "data directory" else packaged, 25832)

    path = it.get_processed_trafos(7)

    # the result is a file of the data directory, whichever copy it was made from
    assert path == str(data / "processed_trafos" / "7_trafos_processed_3035.geojson")
    result = gpd.read_file(path)
    assert result.crs.to_epsg() == 3035
    assert list(result["osm_id"]) == ["node/1", "node/2"]
    expected = [Transformer.from_crs(25832, 3035, always_xy=True).transform(*xy) for xy in POINTS_25832]
    np.testing.assert_allclose([(point.x, point.y) for point in result.geometry], expected, atol=1e-3)
    assert not (packaged / "processed_trafos" / "7_trafos_processed_3035.geojson").exists()


def test_an_existing_file_of_the_target_crs_is_used_as_it_is(monkeypatch, dirs, no_download):
    data, _ = dirs
    monkeypatch.setattr(it, "TARGET_EPSG", 3035)
    write_processed(data, 25832)
    own = write_processed(data, 3035)
    monkeypatch.setattr(it, "reproject_trafos", lambda source, target: pytest.fail("reprojected again"))

    assert it.get_processed_trafos(7) == str(own)


def test_the_25832_file_of_the_wheel_is_loaded_in_place(monkeypatch, dirs, no_download):
    data, packaged = dirs
    monkeypatch.setattr(it, "TARGET_EPSG", 25832)
    shipped = write_processed(packaged, 25832)

    assert it.get_processed_trafos(7) == str(shipped)
    assert not data.exists()


def test_a_relation_without_a_processed_file_is_fetched_and_processed(monkeypatch, dirs):
    data, _ = dirs
    calls = []
    monkeypatch.setattr(it, "TARGET_EPSG", 3035)
    monkeypatch.setattr(it, "fetch_trafos", lambda relation_id: calls.append(("fetch", relation_id)))
    monkeypatch.setattr(it, "process_trafos", lambda relation_id: calls.append(("process", relation_id)))

    assert it.get_processed_trafos(9) == str(data / "processed_trafos" / "9_trafos_processed_3035.geojson")
    assert calls == [("fetch", 9), ("process", 9)]


def test_setup_loads_the_reprojected_transformers_without_a_download(monkeypatch, dirs, no_download):
    data, _ = dirs
    monkeypatch.setattr(it, "TARGET_EPSG", 3035)
    monkeypatch.setattr(database_constructor, "RELATION_ID", 7)
    write_processed(data, 25832)
    constructor = DatabaseConstructor.__new__(DatabaseConstructor)          # no database connection
    loaded = []
    constructor.ogr_to_db = lambda files: loaded.extend(files)

    constructor.transformers_to_db(clear_existing=False)

    assert loaded == [{"path": str(data / "processed_trafos" / "7_trafos_processed_3035.geojson"),
                       "table_name": "transformers"}]
