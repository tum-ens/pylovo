"""``pylovo-export`` argument wiring and output files (no database; the export functions are replaced by recorders).

Not asserted: exit codes and messages for PLZ without grids."""
import sys

import pandas as pd
import pytest

from pylovo.cli import export


@pytest.fixture
def calls(monkeypatch):
    recorded = []

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read_net_db(self, plz, kcid, bcid):
            recorded.append(("read_net_db", plz, kcid, bcid))
            return "net"

    def save_geodata_as_csv(df_plz, data_path_lines, data_path_bus):
        recorded.append(("plz export", df_plz["plz"].tolist(), data_path_lines, data_path_bus))
        return []                                                   # the PLZ without grids

    def get_bus_line_geo_for_network(pandapower_net, plz):
        recorded.append(("geo", pandapower_net, plz))
        return pd.DataFrame({"line": [1]}), pd.DataFrame({"bus": [2]})

    monkeypatch.setattr(export, "DatabaseClient", Client)
    monkeypatch.setattr(export, "save_geodata_as_csv", save_geodata_as_csv)
    monkeypatch.setattr(export, "get_bus_line_geo_for_network", get_bus_line_geo_for_network)
    return recorded


def run(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["pylovo-export", *args])
    export.main()


def test_one_plz_is_exported_as_a_single_grid_file_pair_into_the_output_directory(monkeypatch, calls, tmp_path):
    out = tmp_path / "new" / "dir"
    run(monkeypatch, "--plz", "80803", "--output", str(out))

    assert out.is_dir()                                                  # created on demand
    assert calls == [("plz export", [80803], str(out / "lines_single_grid.csv"), str(out / "bus_single_grid.csv"))]


def test_several_plz_are_exported_together(monkeypatch, calls, tmp_path):
    run(monkeypatch, "--plz", "80803", "80639", "91720", "--output", str(tmp_path))
    assert calls == [("plz export", [80803, 80639, 91720], str(tmp_path / "lines_multiple_grids.csv"),
                      str(tmp_path / "bus_multiple_grids.csv"))]


def test_the_default_output_directory_is_qgis_under_the_project_root(monkeypatch, calls, tmp_path):
    monkeypatch.setattr(export, "PROJECT_ROOT", tmp_path)
    run(monkeypatch, "--plz", "80803")
    assert calls[0][2] == str(tmp_path / "QGIS" / "lines_single_grid.csv")


@pytest.mark.parametrize("kcid, bcid", [("4", "30"), ("0", "-3")])      # zero and negative (brownfield) ids are valid
def test_one_grid_is_read_from_the_database_and_written_as_line_and_bus_csv(monkeypatch, calls, tmp_path, kcid, bcid):
    run(monkeypatch, "--grid", "--plz", "91207", "--kcid", kcid, "--bcid", bcid, "--output", str(tmp_path))

    assert calls == [("read_net_db", 91207, int(kcid), int(bcid)), ("geo", "net", 91207)]
    assert pd.read_csv(tmp_path / "lines_single_grid.csv")["line"].tolist() == [1]
    assert pd.read_csv(tmp_path / "bus_single_grid.csv")["bus"].tolist() == [2]


@pytest.mark.parametrize("args", [["--grid", "--plz", "91207"], ["--grid", "--plz", "91207", "--kcid", "1"],
                                  ["--grid", "--plz", "91207", "80803", "--kcid", "1", "--bcid", "1"], []])
def test_incomplete_grid_exports_are_refused_before_anything_is_read(monkeypatch, calls, tmp_path, args):
    with pytest.raises(SystemExit) as refused:
        run(monkeypatch, *args, "--output", str(tmp_path))
    assert refused.value.code == 2 and calls == []
