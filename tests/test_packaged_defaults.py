"""The defaults and reference data that the wheel ships are the last fallback of every lookup (no database)."""
from pathlib import Path

import pytest

import pylovo
from pylovo import config_loader, utils
from pylovo.data_import import import_transformers, municipal_register

EMPTY_COLLECTION = {"type": "FeatureCollection", "features": []}


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """A wheel outside a checkout: nothing in ./config, no checkout above the package, defaults in ``packaged``."""
    work = tmp_path / "work"
    work.mkdir()
    packaged = tmp_path / "packaged"
    monkeypatch.chdir(work)
    for name in ("PYLOVO_ROOT", "PYLOVO_CONFIG_DIR", "PYLOVO_DATA_DIR"):
        monkeypatch.delenv(name, raising=False)
    # <tmp>/site-packages/pylovo/config_loader.py: the "checkout config" would be <tmp>/config
    monkeypatch.setattr(config_loader, "__file__", str(tmp_path / "site-packages" / "pylovo" / "config_loader.py"))
    monkeypatch.setattr(config_loader, "get_packaged_defaults_dir", lambda: packaged)
    monkeypatch.setattr(utils, "get_packaged_defaults_dir", lambda: packaged)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    return work, packaged


def test_the_packaged_defaults_are_the_folder_next_to_the_package():
    assert config_loader.get_packaged_defaults_dir() == Path(pylovo.__file__).parent / "_defaults"


def test_the_packaged_config_is_the_last_search_location(installed):
    work, packaged = installed
    assert packaged / "config" not in config_loader.get_config_search_paths()      # not shipped: not searched
    (packaged / "config").mkdir(parents=True)

    paths = config_loader.get_config_search_paths()

    assert paths[0] == work / "config"
    assert paths[-1] == packaged / "config"


def test_the_checkout_config_comes_before_the_packaged_one(installed, tmp_path):
    work, packaged = installed
    (packaged / "config").mkdir(parents=True)
    (tmp_path / "config").mkdir()

    paths = config_loader.get_config_search_paths()

    assert paths[-2:] == [tmp_path / "config", packaged / "config"]


def test_a_config_file_comes_from_the_packaged_defaults_only_if_nothing_else_has_it(installed):
    work, packaged = installed
    touch(packaged / "config" / "config_analysis.yaml")
    touch(packaged / "config" / "config_clustering.yaml")

    assert config_loader.find_config_file("config_analysis.yaml") == packaged / "config" / "config_analysis.yaml"
    touch(work / "config" / "config_analysis.yaml")
    assert config_loader.find_config_file("config_analysis.yaml") == work / "config" / "config_analysis.yaml"
    assert config_loader.find_config_file("config_clustering.yaml") == packaged / "config" / "config_clustering.yaml"


def test_a_missing_config_file_lists_the_searched_folders(installed):
    with pytest.raises(FileNotFoundError, match="not found in any search location"):
        config_loader.find_config_file("config_analysis.yaml")


def test_the_env_lookup_tries_the_working_directory_only_if_the_package_walk_finds_none(monkeypatch):
    monkeypatch.delenv("PYLOVO_ENV_FILE", raising=False)
    calls = []

    def walk(usecwd=False):
        calls.append(usecwd)
        return "/work/.env" if usecwd else ""

    monkeypatch.setattr(config_loader, "find_dotenv", walk)
    assert config_loader.find_env_file() == "/work/.env"
    assert calls == [False, True]

    calls.clear()
    monkeypatch.setattr(config_loader, "find_dotenv", lambda usecwd=False: calls.append(usecwd) or "/venv/.env")
    assert config_loader.find_env_file() == "/venv/.env"            # the first walk keeps its precedence
    assert calls == [False]



def test_pylovo_env_file_replaces_the_env_lookup(monkeypatch):
    monkeypatch.setattr(config_loader, "find_dotenv", lambda usecwd=False: pytest.fail("the .env was searched"))
    monkeypatch.setenv("PYLOVO_ENV_FILE", "/elsewhere/test.env")
    assert config_loader.find_env_file() == "/elsewhere/test.env"
    monkeypatch.setenv("PYLOVO_ENV_FILE", "")
    assert config_loader.find_env_file() == ""                      # empty: no .env at all


def test_reference_data_comes_from_the_data_dir_before_the_packaged_copy(installed):
    work, packaged = installed
    relative = Path("municipal_register") / "regiostar" / "regiostar.xlsx"
    user = work / "data" / relative

    assert utils.reference_data_path(user, relative) == user                       # neither has it: name the data dir
    touch(packaged / "data" / relative)
    assert utils.reference_data_path(user, relative) == packaged / "data" / relative
    touch(user)
    assert utils.reference_data_path(user, relative) == user


def test_the_register_files_are_read_from_the_wheel_when_the_data_dir_has_none(monkeypatch, installed):
    work, packaged = installed
    monkeypatch.setenv("PYLOVO_DATA_DIR", str(work / "data"))
    user_register = work / "data" / "municipal_register"
    assert municipal_register.missing_input_files() == [
        str(user_register / name) for name in municipal_register.REGISTER_FILES]

    for name in municipal_register.REGISTER_FILES:
        touch(packaged / "data" / "municipal_register" / name)
    assert municipal_register.missing_input_files() == []
    packaged_file = packaged / "data" / "municipal_register" / "regiostar" / "regiostar.xlsx"
    assert municipal_register._get_data_file_path("regiostar/regiostar.xlsx") == str(packaged_file)

    touch(user_register / "regiostar" / "regiostar.xlsx")          # a file of the data dir wins, file by file
    assert municipal_register._get_data_file_path("regiostar/regiostar.xlsx") == str(
        user_register / "regiostar" / "regiostar.xlsx")
    assert municipal_register._get_data_file_path("gemeindeverzeichnis/plz_einwohner.xlsx") == str(
        packaged / "data" / "municipal_register" / "gemeindeverzeichnis" / "plz_einwohner.xlsx")


def test_the_overpass_queries_come_from_the_wheel_and_the_results_go_to_the_data_dir(monkeypatch, installed):
    work, packaged = installed
    for name in ("substations", "shopping_mall"):
        touch(packaged / "data" / "transformer_data" / "overpass_queries" / f"{name}_query.txt").write_text(
            f"{name} in $relation_id$")
    queries = []
    monkeypatch.setattr(import_transformers, "query_overpass_for_geojson",
                        lambda url, query: queries.append(query) or EMPTY_COLLECTION)

    import_transformers.fetch_trafos(7)

    assert queries == ["substations in 3600000007", "shopping_mall in 3600000007"]
    fetched = work / "data" / "transformer_data" / "fetched_trafos"
    assert (fetched / "7_substations.geojson").exists() and (fetched / "7_shopping_mall.geojson").exists()
    assert not (packaged / "data" / "transformer_data" / "fetched_trafos").exists()
