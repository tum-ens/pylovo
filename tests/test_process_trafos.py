"""process_trafos: a clear message when the filters leave no transformer (no database)."""
import json

import pytest

from pylovo.data_import import import_transformers as it


def feature(osm_id, geometry):
    return {"type": "Feature", "properties": {"type": "way" if geometry["type"] == "Polygon" else "node", "id": osm_id,
                                              "power": "substation"}, "geometry": geometry}


def polygon(x, y, size):
    ring = [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]
    return {"type": "Polygon", "coordinates": [ring]}


@pytest.fixture()
def fetched(tmp_path, monkeypatch):
    """Writes the fetched substations of relation 9 (and no shopping mall) and returns the writer."""
    substations, malls = tmp_path / "substations.geojson", tmp_path / "shopping_mall.geojson"
    monkeypatch.setattr(it, "get_substations_geojson_path", lambda relation_id: str(substations))
    monkeypatch.setattr(it, "get_shopping_mall_geojson_path", lambda relation_id: str(malls))
    malls.write_text(json.dumps({"type": "FeatureCollection", "features": []}))

    def write(features):
        substations.write_text(json.dumps({"type": "FeatureCollection", "features": features}))

    return write


@pytest.mark.parametrize("features", [
    [],                                                       # nothing fetched
    [feature(1, polygon(11.7, 48.0, 0.01))],                  # only a substation area above AREA_THRESHOLD
], ids=["empty fetch", "only a large substation area"])
def test_no_transformer_left_gives_a_clear_error(fetched, features):
    fetched(features)
    with pytest.raises(ValueError, match="No transformer is left in relation 9"):
        it.process_trafos(9)
