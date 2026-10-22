"""The Excel export of synthetic grids (``pylovo.analysis.validation_helpers``) with a fake database client."""
import pandapower as pp
import pytest

from pylovo.analysis import validation_helpers


def small_net():
    net = pp.create_empty_network()
    b0, b1 = pp.create_bus(net, 0.4), pp.create_bus(net, 0.4)
    pp.create_ext_grid(net, b0)
    pp.create_line(net, b0, b1, 0.1, "NAYY 4x150 SE")
    pp.create_load(net, b1, 0.01)
    return net


class FakeClient:
    """Answers the grid query with ``rows`` and returns a small net for every grid except negative bcids."""

    def __init__(self, rows):
        self.rows, self.queries, self.read = rows, [], []
        self.cur = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.queries.append((sql, params))

    def fetchall(self):
        return self.rows

    def read_net_db(self, plz, kcid, bcid):
        if bcid < 0:
            raise ValueError("no grid")
        self.read.append((plz, kcid, bcid))
        return small_net()


@pytest.fixture()
def client(monkeypatch):
    fake = FakeClient([(85653, 1, 2), (85653, 1, -1)])
    monkeypatch.setattr("pylovo.database.database_client.DatabaseClient", lambda: fake)
    return fake


def test_every_grid_of_the_version_is_written_and_missing_grids_are_skipped(client, tmp_path):
    from pylovo.config_loader import VERSION_ID

    written = validation_helpers.export_synthetic_grids_to_excel(tmp_path / "excel")
    assert written == [tmp_path / "excel" / "grid_85653_1_2.xlsx"] and written[0].is_file()
    assert client.queries[0][1] == (VERSION_ID,)
    assert len(pp.from_excel(str(written[0])).line) == 1


def test_one_grid_is_written_without_the_version_query(client, tmp_path):
    written = validation_helpers.export_synthetic_grids_to_excel(tmp_path, plz=85653, kcid=1, bcid=2)
    assert written == [tmp_path / "grid_85653_1_2.xlsx"] and client.queries == []


def test_a_partial_grid_key_is_refused(client, tmp_path):
    with pytest.raises(ValueError, match="all three"):
        validation_helpers.export_synthetic_grids_to_excel(tmp_path, plz=85653)
