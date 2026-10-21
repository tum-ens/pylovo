"""Browser map of ``pylovo-import transformers-ui``: the page and the routes it uses (no database, no browser)."""
import re
from contextlib import contextmanager

import pytest

from pylovo.data_import.transformers_ui import TransformerMapUI


@pytest.fixture(scope="module")
def page() -> str:
    return TransformerMapUI._get_html_template(None)      # the page needs no instance state


def test_postcodes_from_01001_on_are_accepted(page):
    check = re.search(r"const plzNum = parseInt\(plzText, 10\);\s*if \(([^\n]*)\) \{", page)
    assert check is not None
    assert "plzNum < 1001" in check.group(1) and "plzNum > 99999" in check.group(1)
    assert "plzNum < 10000" not in page


def test_the_urls_carry_the_validated_postcode_number_not_the_typed_text(page):
    assert "/api/plz-bounds/${plzNum}" in page and "/api/transformer-positions/${plzNum}" in page
    assert "${plz}" not in page


def test_values_of_a_transformer_are_escaped_before_they_enter_the_popup_html(page):
    popup = page[page.index("const popupContent = `"):page.index("marker.bindPopup(popupContent)")]
    expressions = [expression.strip() for expression in re.findall(r"\$\{([^}]*)\}", popup)]
    for field in ("osm_id", "type", "geom_type", "transformer_rated_power"):
        unescaped = [e for e in expressions if f"position.{field}" in e and not e.startswith("escapeHtml(")]
        assert unescaped == [], field
    assert "('${" not in popup, "an id must not be pasted into a JavaScript string of an onclick attribute"
    assert 'onclick="updateTransformerCapacity(this.dataset.osmId)"' in popup
    assert 'onclick="deleteTransformer(this.dataset.osmId)"' in popup


def test_status_messages_are_shown_as_text(page):
    status = page[page.index("function showStatus"):page.index("// Test regex patterns")]
    assert "box.textContent = message" in status and "${message}" not in status


def test_leaflet_is_pinned_and_checked_by_its_hash(page):
    tags = re.findall(r"<(?:link|script)[^>]*unpkg\.com/leaflet@[^>]*>", page)
    assert len(tags) == 2
    for tag in tags:
        assert "leaflet@1.9.4/" in tag and 'integrity="sha256-' in tag and "crossorigin" in tag


@pytest.fixture()
def client(monkeypatch):
    pytest.importorskip("flask", reason="needs the extra legacy-ui")
    monkeypatch.setattr(TransformerMapUI, "_test_database_connection", lambda self: True)
    asked = []

    class Database:
        def get_transformer_positions_for_plz_trafo_ui(self, plz):
            asked.append(plz)
            return [{"osm_id": 'manual/1"><img src=x>', "transformer_rated_power": 250, "type": "Manual",
                     "geom_type": "Point", "within_shopping": False, "geom_wkt": "POINT(11.7 48.0)"}]

    @contextmanager
    def database(self, max_retries=3):
        yield Database()

    monkeypatch.setattr(TransformerMapUI, "_get_db_client", database)
    app_client = TransformerMapUI().app.test_client()
    app_client.asked = asked
    return app_client


def test_the_server_serves_the_page_and_the_positions_of_a_postcode_with_a_leading_zero(client, page):
    assert client.get("/").get_data(as_text=True) == page
    answer = client.get("/api/transformer-positions/01067").get_json()
    assert client.asked == [1067] and answer["success"] is True
    assert answer["positions"][0]["osm_id"] == 'manual/1"><img src=x>'     # the page escapes it when it draws it
