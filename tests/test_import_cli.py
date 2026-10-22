"""``pylovo-import`` wires its subcommands to the import functions with the parsed arguments (no database, no network)."""
import sys
from types import SimpleNamespace

import pytest

from pylovo.cli import import_data
from pylovo.data_import.transformers_ui import DEFAULT_HOST


DUPLICATE = 'ERROR:  duplicate key value violates unique constraint "transformers_pkey"'


@pytest.fixture
def ogr():
    """What the fake table and ogr2ogr answer: rows before and after the append, GeoJSON features, errors."""
    return SimpleNamespace(before=100, after=100, features=100, errors=[])


@pytest.fixture
def calls(monkeypatch, ogr):
    recorded = []

    class Constructor:
        reads = 0

        def row_count(self, table_name):
            assert table_name == "transformers"
            type(self).reads += 1
            return ogr.before if type(self).reads == 1 else ogr.after

        def ogr_to_db(self, layers, skip_failures=False):
            recorded.append(("ogr_to_db", layers, skip_failures))
            return ogr.errors

    monkeypatch.setattr(import_data, "fetch_trafos", lambda relation_id: recorded.append(("fetch", relation_id)))
    monkeypatch.setattr(import_data, "process_trafos", lambda relation_id: recorded.append(("process", relation_id)))
    monkeypatch.setattr(import_data, "get_trafos_processed_target_geojson_path", lambda relation_id: f"/data/{relation_id}.geojson")
    monkeypatch.setattr(import_data, "DatabaseConstructor", Constructor)
    monkeypatch.setattr(import_data, "_count_features", lambda path: ogr.features)
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


@pytest.mark.parametrize("before, after, features, errors, summary", [
    (21424, 21424, 21424, [DUPLICATE], "added 0, skipped 21424"),
    (100, 130, 50, [DUPLICATE], "added 30, skipped 20"),
    (0, 150, 150, [], "added 150, skipped 0"),
])
def test_osm_import_reports_the_added_and_the_skipped_transformers(monkeypatch, calls, ogr, capsys,
                                                                   before, after, features, errors, summary):
    ogr.before, ogr.after, ogr.features, ogr.errors = before, after, features, errors
    run(monkeypatch, "transformers-osm", "--relation-id", "62464")
    last_line = capsys.readouterr().out.splitlines()[-1]
    assert last_line.startswith("✓ Completed in ") and last_line.endswith(f": {summary}")


def test_osm_import_fails_on_errors_other_than_duplicate_keys_after_reporting_the_counts(monkeypatch, calls, ogr, capsys):
    ogr.features, ogr.errors = 100, [DUPLICATE, 'ERROR:  invalid input syntax for type integer: "x"']
    with pytest.raises(SystemExit) as failed:
        run(monkeypatch, "transformers-osm", "--relation-id", "62464")
    assert failed.value.code == 1
    output = capsys.readouterr().out
    assert "added 0, skipped 100" in output and 'invalid input syntax for type integer: "x"' in output
    assert "Completed" not in output


@pytest.mark.parametrize("args, expected", [
    (["transformers-dso-csv", "t.csv"], ("dso", "t.csv", None, False)),
    (["transformers-dso-csv", "t.csv", "--source", "region_a", "--replace-source"], ("dso", "t.csv", "region_a", True)),
])
def test_dso_positions_are_imported_from_the_csv_with_source_and_replace_flag(monkeypatch, calls, capsys, args, expected):
    run(monkeypatch, *args)
    assert calls == [expected]
    assert "Imported 3 DSO transformer positions" in capsys.readouterr().out


def test_the_transformer_map_gets_its_options_and_binds_to_the_local_host_by_default(monkeypatch, calls):
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
