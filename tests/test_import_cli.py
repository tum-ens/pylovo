"""``pylovo-import`` wires its subcommands to the import functions with the parsed arguments (no database, no network)."""
import sys

import pytest

from pylovo.cli import import_data
from pylovo.data_import.transformers_ui import DEFAULT_HOST


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    class Constructor:
        def ogr_to_db(self, layers, skip_failures=False):
            recorded.append(("ogr_to_db", layers, skip_failures))

    monkeypatch.setattr(import_data, "fetch_trafos", lambda relation_id: recorded.append(("fetch", relation_id)))
    monkeypatch.setattr(import_data, "process_trafos", lambda relation_id: recorded.append(("process", relation_id)))
    monkeypatch.setattr(import_data, "get_trafos_processed_target_geojson_path", lambda relation_id: f"/data/{relation_id}.geojson")
    monkeypatch.setattr(import_data, "DatabaseConstructor", Constructor)
    monkeypatch.setattr(import_data, "import_dso_transformers_csv",
                        lambda csv_path, source=None, replace_source=False: recorded.append(
                            ("dso", csv_path, source, replace_source)) or 3)
    monkeypatch.setattr(import_data, "run_transformers_ui", lambda **options: recorded.append(("ui", options)))
    return recorded


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pylovo-import", *args])
    import_data.main()


def test_osm_transformers_are_fetched_processed_and_appended_without_failing_on_existing_rows(monkeypatch, calls):
    run(monkeypatch, "transformers-osm", "--relation-id", "62464")
    assert calls == [("fetch", 62464), ("process", 62464),
                     ("ogr_to_db", [{"path": "/data/62464.geojson", "table_name": "transformers"}], True)]


@pytest.mark.parametrize("args, expected", [
    (["transformers-dso-csv", "t.csv"], ("dso", "t.csv", None, False)),
    (["transformers-dso-csv", "t.csv", "--source", "region_a", "--replace-source"], ("dso", "t.csv", "region_a", True)),
])
def test_dso_positions_are_imported_from_the_csv_with_source_and_replace_flag(monkeypatch, calls, capsys, args, expected):
    run(monkeypatch, *args)
    assert calls == [expected]
    assert "Imported 3 DSO transformer positions" in capsys.readouterr().out


def test_the_legacy_transformer_ui_gets_its_options_and_binds_to_the_local_host_by_default(monkeypatch, calls):
    run(monkeypatch, "transformers-ui")
    (_, defaults), = calls
    assert defaults["host"] == DEFAULT_HOST and DEFAULT_HOST in ("127.0.0.1", "localhost", "::1")
    assert defaults["debug"] is False and defaults["cleanup"] is False and defaults["auto_cleanup"] is False

    calls.clear()
    run(monkeypatch, "transformers-ui", "--host", "0.0.0.0", "--port", "5055", "--debug", "--cleanup", "--auto-cleanup")
    assert calls == [("ui", {"host": "0.0.0.0", "port": 5055, "debug": True, "cleanup": True, "auto_cleanup": True})]


@pytest.mark.parametrize("args", [["transformers-osm"], ["transformers-osm", "--relation-id", "abc"],
                                  ["transformers-dso-csv"], ["unknown"]])
def test_invalid_arguments_are_refused_before_anything_is_imported(monkeypatch, calls, args):
    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, *args)
    assert refused.value.code == 2 and calls == []


def test_without_a_command_the_help_is_shown(monkeypatch, calls, capsys):
    run(monkeypatch)
    assert calls == [] and "usage: pylovo-import" in capsys.readouterr().out


def test_a_failing_import_exits_with_one_and_does_not_load_anything(monkeypatch, calls, capsys):
    def failing(relation_id):
        raise ConnectionError("Overpass unreachable")

    monkeypatch.setattr(import_data, "fetch_trafos", failing)
    with pytest.raises(SystemExit) as failed:
        run(monkeypatch, "transformers-osm", "--relation-id", "62464")
    assert failed.value.code == 1 and calls == []
    assert "Overpass unreachable" in capsys.readouterr().out
