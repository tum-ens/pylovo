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


def test_duplicate_ids_are_refused_instead_of_the_last_row_winning(tmp_path):
    csv = "external_id,lon,lat,transformer_rated_power\nST-1,11.7,48.0,400\nST-2,11.8,48.1,\nST-1,11.9,48.2,630\n"
    with pytest.raises(ValueError, match=r"duplicate external_id in rows \[2, 4\]"):
        read(tmp_path, csv)


def test_ids_that_collide_after_the_source_is_normalised_are_duplicates(tmp_path):
    csv = "external_id,lon,lat,source\nA,11.7,48.0,My Region\nA,11.8,48.1,my_region\n"
    with pytest.raises(ValueError, match=r"duplicate external_id in rows \[2, 3\]"):
        read(tmp_path, csv)


def test_the_same_id_in_two_sources_is_fine(tmp_path):
    df = read(tmp_path, "external_id,lon,lat,source\nA,11.7,48.0,north\nA,11.8,48.1,south\n")
    assert df["osm_id"].tolist() == ["dso/north/A", "dso/south/A"]


@pytest.mark.parametrize("rating", ["400kVA", "0", "-250", "630.7", "n/a"])
def test_an_unusable_rating_is_refused(tmp_path, rating):
    csv = f"external_id,lon,lat,transformer_rated_power\nA,11.7,48.0,{rating}\nB,11.8,48.1,400\n"
    with pytest.raises(ValueError, match=r"invalid transformer_rated_power.* in rows \[2\]"):
        read(tmp_path, csv)


def test_blank_and_whole_number_ratings_are_kept(tmp_path):
    df = read(tmp_path, "external_id,lon,lat,transformer_rated_power\nA,11.7,48.0,400\nB,11.8,48.1,\nC,11.9,48.2,630.0\nD,12.0,48.3, 250 \n")
    assert df["transformer_rated_power"].isna().tolist() == [False, True, False, False]
    assert df["transformer_rated_power"].dropna().astype(int).tolist() == [400, 630, 250]


def test_a_file_without_ratings_imports_them_as_unknown(tmp_path):
    df = read(tmp_path, "external_id,lon,lat\nA,11.7,48.0\n")
    assert df["transformer_rated_power"].isna().all()


def test_every_problem_is_reported_in_one_message(tmp_path):
    csv = "external_id,lon,lat,transformer_rated_power\n,11.7,48.0,400\nB,,48.1,400\nC,11.9,48.2,x\nD,12.0,48.3,400\nD,12.1,48.4,400\n"
    with pytest.raises(ValueError) as error:
        read(tmp_path, csv)
    message = str(error.value)
    for expected in ("empty external_id in rows [2]", "invalid lon/lat in rows [3]", "in rows [4]",
                     "duplicate external_id in rows [5, 6]"):
        assert expected in message


def test_ids_that_look_like_missing_values_are_kept(tmp_path):
    df = read(tmp_path, "external_id,lon,lat\nNA,11.7,48.0\nnull,11.8,48.1\nNone,11.9,48.2\n")
    assert df["osm_id"].tolist() == ["dso/csv/NA", "dso/csv/null", "dso/csv/None"]


class FakeClient:
    """Records the statements of the import instead of sending them to a database."""
    log: list = []

    class conn:
        @staticmethod
        def commit():
            FakeClient.log.append(("commit",))

        @staticmethod
        def rollback():
            FakeClient.log.append(("rollback",))

        @staticmethod
        def cursor():
            return FakeCursor()

    def close(self):
        pass


class FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        FakeClient.log.append(("execute", " ".join(sql.split()), params))

    def executemany(self, sql, rows):
        FakeClient.log.append(("executemany", len(list(rows))))


@pytest.fixture()
def fake_client(monkeypatch):
    FakeClient.log = []
    monkeypatch.setattr(dso.dbc, "DatabaseClient", FakeClient)
    return FakeClient


@pytest.mark.parametrize("source, prefix", [("my_region", "dso/my_region/"), ("Aying", "dso/aying/"), ("ü", "dso/_/")])
def test_replace_source_compares_the_prefix_literally_not_as_a_like_pattern(tmp_path, fake_client, source, prefix):
    path = write(tmp_path, "external_id,lon,lat\nA,11.7,48.0\n")
    assert dso.import_dso_transformers_csv(path, source=source, replace_source=True) == 1
    delete = next(entry for entry in fake_client.log if entry[0] == "execute")
    assert "LIKE" not in delete[1].upper() and "starts_with(osm_id, %(prefix)s)" in delete[1]
    assert delete[2] == {"prefix": prefix, "type": "dso"}


def test_a_refused_csv_changes_nothing(tmp_path, fake_client):
    path = write(tmp_path, "external_id,lon,lat\nA,11.7,48.0\nA,11.8,48.1\n")
    with pytest.raises(ValueError, match="duplicate external_id"):
        dso.import_dso_transformers_csv(path, source="x", replace_source=True)
    assert fake_client.log == []
