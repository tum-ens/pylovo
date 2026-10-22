"""Every reader of data files finds them through ``PYLOVO_DATA_DIR`` (else ``$PYLOVO_ROOT/data``, else ``./data``)."""
import json
from pathlib import Path

import pytest

from pylovo import utils
from pylovo.cli import setup
from pylovo.config_loader import TARGET_EPSG
from pylovo.data_import import import_transformers, municipal_register

EMPTY_COLLECTION = {"type": "FeatureCollection", "features": []}


@pytest.fixture
def work_dir(monkeypatch, tmp_path):
    """A working directory without ``data/`` and no data directory variable set."""
    monkeypatch.delenv("PYLOVO_DATA_DIR", raising=False)
    monkeypatch.delenv("PYLOVO_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_data_dir_resolution_order(monkeypatch, work_dir):
    assert utils.get_user_data_dir() == work_dir / "data"
    monkeypatch.setenv("PYLOVO_ROOT", str(work_dir / "root"))
    assert utils.get_user_data_dir() == work_dir / "root" / "data"
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(work_dir / "elsewhere"))
    assert utils.get_user_data_dir() == work_dir / "elsewhere"


def test_transformer_paths_follow_the_data_dir_at_call_time(monkeypatch, work_dir):
    def paths():
        return [Path(path) for path in (
            import_transformers.get_substations_geojson_path(7),
            import_transformers.get_shopping_mall_geojson_path(7),
            import_transformers.get_trafos_processed_target_geojson_path(7))]

    assert paths() == [
        work_dir / "data" / "transformer_data" / "fetched_trafos" / "7_substations.geojson",
        work_dir / "data" / "transformer_data" / "fetched_trafos" / "7_shopping_mall.geojson",
        work_dir / "data" / "transformer_data" / "processed_trafos" / f"7_trafos_processed_{TARGET_EPSG}.geojson"]
    monkeypatch.setenv("PYLOVO_ROOT", str(work_dir / "root"))
    assert all(path.is_relative_to(work_dir / "root" / "data" / "transformer_data") for path in paths())
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(work_dir / "elsewhere"))
    assert all(path.is_relative_to(work_dir / "elsewhere" / "transformer_data") for path in paths())


def test_fetching_transformers_reads_the_queries_and_writes_the_results_in_the_data_dir(monkeypatch, work_dir):
    data_dir = work_dir / "elsewhere"
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(data_dir))
    for name in ("substations", "shopping_mall"):
        query = touch(data_dir / "transformer_data" / "overpass_queries" / f"{name}_query.txt")
        query.write_text(f"{name} in $relation_id$")
    queries = []
    monkeypatch.setattr(import_transformers, "query_overpass_for_geojson",
                        lambda url, query: queries.append(query) or EMPTY_COLLECTION)

    import_transformers.fetch_trafos(7)

    assert queries == ["substations in 3600000007", "shopping_mall in 3600000007"]
    fetched = data_dir / "transformer_data" / "fetched_trafos"
    assert json.loads((fetched / "7_substations.geojson").read_text()) == EMPTY_COLLECTION
    assert (fetched / "7_shopping_mall.geojson").exists()
    assert not (work_dir / "data").exists()


def test_processing_transformers_writes_the_result_to_the_data_dir(monkeypatch, work_dir):
    data_dir = work_dir / "elsewhere"
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(data_dir))
    nodes = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [11.5 + i / 10, 48.1]},
              "properties": {"type": "node", "id": i}} for i in (1, 2)]
    fetched = data_dir / "transformer_data" / "fetched_trafos"
    touch(fetched / "7_substations.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": nodes}))
    touch(fetched / "7_shopping_mall.geojson").write_text(json.dumps(EMPTY_COLLECTION))

    import_transformers.process_trafos(7)

    processed = data_dir / "transformer_data" / "processed_trafos" / f"7_trafos_processed_{TARGET_EPSG}.geojson"
    assert processed.exists()
    assert len(json.loads(processed.read_text())["features"]) == 2
    assert not (work_dir / "data").exists()


def test_register_files_are_read_from_the_data_dir(monkeypatch, work_dir):
    data_dir = work_dir / "elsewhere"
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(data_dir))
    register_dir = data_dir / "municipal_register"
    expected = [str(register_dir / name) for name in municipal_register.REGISTER_FILES]

    assert municipal_register.missing_input_files() == expected
    for name in expected:
        touch(Path(name))
    assert municipal_register.missing_input_files() == []
    assert municipal_register._get_data_file_path("regiostar/regiostar.xlsx") == str(
        register_dir / "regiostar" / "regiostar.xlsx")


def test_missing_register_file_names_the_data_dir_variable(monkeypatch, work_dir):
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(work_dir / "elsewhere"))
    with pytest.raises(FileNotFoundError, match="PYLOVO_DATA_DIR"):
        municipal_register._get_data_file_path("regiostar/regiostar.xlsx")


def test_register_files_without_a_data_dir_variable(monkeypatch, work_dir):
    # Nothing set: the data folder of the checkout, whatever the working directory is.
    assert municipal_register.missing_input_files() == []
    # Only PYLOVO_ROOT: its data folder.
    monkeypatch.setenv("PYLOVO_ROOT", str(work_dir))
    expected = [str(work_dir / "data" / "municipal_register" / name) for name in municipal_register.REGISTER_FILES]
    assert municipal_register.missing_input_files() == expected


class PostcodeConstructor:
    """Records the CSV files that setup imports instead of touching a database."""

    def __init__(self):
        self.csv_files = None

    def csv_to_db(self, csv_file_list):
        self.csv_files = csv_file_list


def test_setup_reads_the_postcode_csv_from_the_data_dir(monkeypatch, work_dir):
    monkeypatch.setattr(setup, "USE_INFDB", False)
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(work_dir / "elsewhere"))
    constructor = PostcodeConstructor()
    step = next(step for step in setup.import_steps(constructor) if step.table == "postcode")
    postcode_csv = work_dir / "elsewhere" / "postcode.csv"

    assert step.check() == [f"missing input file {postcode_csv}"]
    touch(postcode_csv)
    assert step.check() == []
    step.run()
    assert constructor.csv_files == [{"path": postcode_csv, "table_name": "postcode"}]
