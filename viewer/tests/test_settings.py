"""Tests of the viewer's .env handling (no database)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pylovo_viewer.settings import SettingsError, find_env_file, load_settings

ENV = 'DBNAME="db"\nDBUSER="user"\nPASSWORD="secret"\nHOST="localhost"\nPORT="5432"\n'


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_explicit_env_file(tmp_path):
    path = write(tmp_path, ENV)
    assert find_env_file(path) == path.resolve()
    with pytest.raises(SettingsError, match="no such .env"):
        find_env_file(tmp_path / "missing.env")


def test_load_settings_with_pylovo_defaults(tmp_path, monkeypatch):
    for key in ("INFDB_SOURCE_SCHEMA", "INFDB_OPENDATA_SCHEMA", "INFDB_OSM_SCHEMA", "PYLOVO_VIEWER_DATABASES"):
        monkeypatch.delenv(key, raising=False)
    s = load_settings(write(tmp_path, ENV + "TARGET_EPSG = 3035 # comment\n"))
    assert (s.dbname, s.user, s.host, s.port, s.target_epsg) == ("db", "user", "localhost", 5432, 3035)
    assert (s.source_schema, s.opendata_schema, s.osm_schema) == ("basedata", "opendata", "osm")
    assert s.databases == () and "password" not in s.public() and "secret" not in repr(s)


def test_databases_to_offer(tmp_path):
    s = load_settings(write(tmp_path, ENV + 'PYLOVO_VIEWER_DATABASES="infdb, infdb_mv,,infdb_test"\n'))
    assert s.databases == ("infdb", "infdb_mv", "infdb_test")


def test_env_file_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DBNAME", "from_shell")
    monkeypatch.setenv("INFDB_SOURCE_SCHEMA", "input_data")
    s = load_settings(write(tmp_path, ENV))
    assert s.dbname == "db"                  # as pylovo: the .env replaces the shell value
    assert s.source_schema == "input_data"   # a key missing in the file comes from the environment


@pytest.mark.parametrize("text, message", [
    ('DBNAME="db"\n', "DBUSER, PASSWORD, HOST, PORT missing"),
    (ENV + 'INFDB_OSM_SCHEMA="osm; drop"\n', "not a plain schema name"),
    (ENV.replace('"5432"', '"port"'), "must be integers"),
])
def test_invalid_settings(tmp_path, monkeypatch, text, message):
    for key in ("DBUSER", "PASSWORD", "HOST", "PORT"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(SettingsError, match=message):
        load_settings(write(tmp_path, text))
