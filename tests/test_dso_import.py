"""DSO transformer CSV: reading, validation and the SQL of the import (fake database client, no database)."""
import io

import pytest

from pylovo.data_import import dso_transformers as dso


def write(tmp_path, text, name="transformers.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def read(tmp_path, text, source=None):
    return dso._read_transformer_csv(write(tmp_path, text), source=source)


def test_ids_stay_text_as_in_the_file(tmp_path):
    df = read(tmp_path, "external_id,lon,lat\n00123,11.7,48.0\n1001,11.8,48.1\nST-3,11.9,48.2\n")
    assert df["osm_id"].tolist() == ["dso/csv/00123", "dso/csv/1001", "dso/csv/ST-3"]


@pytest.mark.parametrize("ids", [["ST-1", "", "ST-3"], ["1001", "", "1003"]], ids=["text ids", "numeric ids with a gap"])
def test_a_blank_id_is_refused_with_its_row_number(tmp_path, ids):
    rows = "".join(f"{i},{11.7 + n / 10},48.0\n" for n, i in enumerate(ids))
    with pytest.raises(ValueError, match=r"empty external_id in rows \[3\]"):
        read(tmp_path, "external_id,lon,lat\n" + rows)


def test_blank_coordinates_are_refused_with_their_row_numbers(tmp_path):
    with pytest.raises(ValueError, match=r"invalid lon/lat in rows \[2, 4\]"):
        read(tmp_path, "external_id,lon,lat\nA,,48.0\nB,11.8,48.1\nC,11.9,north\n")


def test_a_blank_source_cell_gets_the_default_source_and_not_nan(tmp_path):
    df = read(tmp_path, "external_id,lon,lat,source\nA,11.7,48.0,Aying\nB,11.8,48.1,\n")
    assert df["osm_id"].tolist() == ["dso/aying/A", "dso/csv/B"]


def test_the_source_argument_overrides_the_column(tmp_path):
    df = read(tmp_path, "external_id,lon,lat,source\nA,11.7,48.0,Aying\n", source="my_region")
    assert df["osm_id"].tolist() == ["dso/my_region/A"]


def test_missing_required_columns_are_named(tmp_path):
    with pytest.raises(ValueError, match="Missing required column.*lat"):
        read(tmp_path, "external_id,lon\nA,11.7\n")


def test_the_parser_marks_the_rows_instead_of_raising_for_the_api_preview():
    csv = io.BytesIO(b"external_id,lon,lat\nA,11.7,48.0\n,11.8,48.1\nC,,48.2\n")
    df = dso.parse_transformer_csv(csv)
    assert df["problem"].tolist() == ["", "empty external_id", "invalid lon/lat"]
