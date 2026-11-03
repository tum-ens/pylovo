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


def matrix_duplicates(points):
    """The close duplicates as the full distance matrix of earlier versions found them (the reference)."""
    import numpy as np

    distance_matrix = points.apply(lambda c: points.distance(c))
    distance_matrix = distance_matrix.where(np.triu(np.ones(distance_matrix.shape)).astype(bool))
    np.fill_diagonal(distance_matrix.values, float("nan"))
    return set(distance_matrix[(distance_matrix < it.MIN_DISTANCE_BETWEEN_TRAFOS).any(axis=1)].index)


def test_close_duplicates_keep_the_last_of_each_group_as_the_distance_matrix_did():
    import geopandas as gpd
    import numpy as np

    rng = np.random.default_rng(7)
    xy = rng.uniform(0, 250, size=(400, 2))
    # exact duplicates, a chain of three, a pair at exactly the limit (kept) and one just below it (dropped)
    extra = [(10, 10), (10, 10), (100, 0), (106, 0), (112, 0), (300, 300), (308, 300), (400, 400), (407.99, 400)]
    points = gpd.GeoSeries(gpd.points_from_xy(*np.vstack([xy, extra]).T), crs=25832)
    points.index = points.index * 3 + 5          # labels that are not positions
    dropped = set(it.close_duplicates(points, it.MIN_DISTANCE_BETWEEN_TRAFOS))
    assert dropped == matrix_duplicates(points)
    labels = list(points.index[-9:])
    assert labels[0] in dropped and labels[1] not in dropped                  # the later duplicate stays
    assert {labels[2], labels[3]} <= dropped and labels[4] not in dropped     # the end of the chain stays
    assert labels[5] not in dropped and labels[7] in dropped                  # 8 m apart is not a duplicate


def test_close_duplicates_ignore_missing_and_empty_geometries():
    import geopandas as gpd
    from shapely.geometry import Point

    points = gpd.GeoSeries([Point(0, 0), None, Point(), Point(1, 0)], crs=25832)
    assert list(it.close_duplicates(points, 8)) == [0]


def test_close_duplicates_of_a_federal_state_need_no_distance_matrix():
    # 20,000 points: the matrix took 3.2 GB per copy; the spatial index takes a few MB
    import tracemalloc

    import geopandas as gpd
    import numpy as np

    rng = np.random.default_rng(1)
    points = gpd.GeoSeries(gpd.points_from_xy(*rng.uniform(0, 100_000, size=(2, 20_000))), crs=25832)
    tracemalloc.start()
    try:
        it.close_duplicates(points, it.MIN_DISTANCE_BETWEEN_TRAFOS)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 50 * 2 ** 20


def test_within_union_equals_within_the_union_of_the_areas():
    import geopandas as gpd
    from shapely.geometry import Point, box

    areas = gpd.GeoSeries([box(0, 0, 10, 10), box(10, 0, 20, 10), box(30, 30, 40, 40)], crs=25832)
    geometries = gpd.GeoSeries([Point(5, 5), Point(10, 5), Point(0, 5), Point(25, 25), box(2, 2, 18, 8),
                                box(35, 35, 45, 45), None, Point()], crs=25832)
    # inside, on the seam of two areas (inside the union), on the outer edge, outside, a polygon across the seam,
    # a polygon partly outside, missing and empty
    expected = geometries.within(areas.union_all()).tolist()
    assert it.within_union(geometries, areas).tolist() == expected == [True, True, False, False, True, False, False,
                                                                       False]


def test_close_transformers_are_reduced_to_one(fetched, tmp_path, monkeypatch):
    # three stations 3 m apart (WGS84 near 48 deg N: 1e-5 deg lon is about 0.74 m) and one far away
    monkeypatch.setattr(it, "get_trafos_processed_target_geojson_path", lambda relation_id: str(tmp_path / "out.geojson"))
    point = lambda osm_id, lon: feature(osm_id, {"type": "Point", "coordinates": [lon, 48.0]})
    fetched([point(1, 11.0), point(2, 11.00004), point(3, 11.00008), point(4, 11.01)])
    it.process_trafos(9)
    out = json.loads((tmp_path / "out.geojson").read_text())
    assert sorted(f["properties"]["osm_id"] for f in out["features"]) == ["node/3", "node/4"]
