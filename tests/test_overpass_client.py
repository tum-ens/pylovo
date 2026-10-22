"""Overpass client: incomplete answers are errors and a busy server is asked again (fake requests, no network)."""
import time

import pytest
import requests

from pylovo import utils


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code, self.payload = status, payload if payload is not None else {"elements": []}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


@pytest.fixture()
def server(monkeypatch):
    """requests.post answers the queued responses in order; the pauses are recorded, not slept."""
    class Server:
        queue, calls, pauses = [], 0, []

    Server.queue, Server.calls, Server.pauses = [], 0, []

    def post(url, **kwargs):
        Server.calls += 1
        return Server.queue.pop(0)

    monkeypatch.setattr(utils.requests, "post", post)
    monkeypatch.setattr(time, "sleep", Server.pauses.append)
    return Server


NODE = {"type": "node", "id": 1, "lat": 48.0, "lon": 11.7, "tags": {"power": "substation"}}


@pytest.mark.parametrize("payload", [
    {"elements": [NODE], "remark": 'runtime error: Query timed out in "query" at line 5 after 502 seconds.'},
    {"elements": [], "remark": 'runtime error: Query ran out of memory in "query" at line 5.'},
])
def test_a_remark_of_the_server_is_an_error_not_a_partial_result(server, payload):
    server.queue = [Response(200, payload)]
    with pytest.raises(RuntimeError, match="Overpass did not finish the query: runtime error"):
        utils.query_overpass_for_geojson("https://overpass.example/api/interpreter", "[out:json];")


def test_a_complete_answer_is_converted(server):
    server.queue = [Response(200, {"elements": [NODE]})]
    geojson = utils.query_overpass_for_geojson("https://overpass.example/api/interpreter", "[out:json];")
    assert len(geojson["features"]) == 1


@pytest.mark.parametrize("status", [429, 504])
def test_a_busy_server_is_asked_again_after_growing_pauses(server, status):
    server.queue = [Response(status), Response(status), Response(200, {"elements": [NODE]})]
    geojson = utils.query_overpass_for_geojson("https://overpass.example/api/interpreter", "[out:json];")
    assert len(geojson["features"]) == 1
    assert server.calls == 3 and server.pauses == [30, 60]


def test_the_last_busy_answer_raises_after_the_attempts(server):
    server.queue = [Response(504), Response(504), Response(504)]
    with pytest.raises(requests.HTTPError, match="504"):
        utils.query_overpass_for_geojson("https://overpass.example/api/interpreter", "[out:json];")
    assert server.calls == utils.OVERPASS_ATTEMPTS and len(server.pauses) == utils.OVERPASS_ATTEMPTS - 1


@pytest.mark.parametrize("status", [400, 403, 500])
def test_other_errors_are_not_retried(server, status):
    server.queue = [Response(status)]
    with pytest.raises(requests.HTTPError):
        utils.query_overpass_for_geojson("https://overpass.example/api/interpreter", "[out:json];")
    assert server.calls == 1 and server.pauses == []


def test_a_truncated_download_is_never_written_to_the_raw_files(server, tmp_path, monkeypatch):
    from pylovo.data_import import import_transformers

    queries = tmp_path / "overpass_queries"
    queries.mkdir()
    (queries / "substations_query.txt").write_text("area($relation_id$)")
    (queries / "shopping_mall_query.txt").write_text("area($relation_id$)")
    monkeypatch.setattr(import_transformers, "_transformer_data_dir", lambda: tmp_path)
    monkeypatch.setattr(import_transformers, "get_substations_geojson_path", lambda relation_id: str(tmp_path / "s.geojson"))
    monkeypatch.setattr(import_transformers, "get_shopping_mall_geojson_path", lambda relation_id: str(tmp_path / "m.geojson"))
    server.queue = [Response(200, {"elements": [NODE]}),
                    Response(200, {"elements": [], "remark": "runtime error: Query timed out"})]
    with pytest.raises(RuntimeError, match="did not finish"):
        import_transformers.fetch_trafos(9)
    assert not (tmp_path / "s.geojson").exists() and not (tmp_path / "m.geojson").exists()
