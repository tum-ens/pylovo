"""Fetch OSM transformers from the Overpass API and filter them to LV substations.

Files are read from and written to ``transformer_data`` in the data directory
(:func:`pylovo.utils.get_user_data_dir`: ``$PYLOVO_DATA_DIR``, else ``$PYLOVO_ROOT/data``, else
``./data``), which is looked up at every call. The query templates are also read from the copy in the
wheel if the data directory has none (:func:`pylovo.utils.reference_data_path`); results are always
written to the data directory.

- ``overpass_queries/``: Overpass query templates (``$relation_id$`` is replaced).
- ``fetched_trafos/<relation_id>_{substations,shopping_mall}.geojson``: raw query results.
- ``processed_trafos/<relation_id>_trafos_processed_<TARGET_EPSG>.geojson``: filtered result that
  ``pylovo-setup`` and ``pylovo-import transformers-osm`` load into ``pylovo.transformers``. The processed
  file of Bavaria in EPSG:25832 ships with pylovo; for another ``TARGET_EPSG`` it is reprojected
  (:func:`get_processed_trafos`).
"""
import json
import os
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from pylovo.config_loader import TARGET_EPSG
from pylovo.utils import get_user_data_dir, query_overpass_for_geojson, reference_data_path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
RELATION_ID_BASE = 3600000000  # Overpass area id = 3600000000 + OSM relation id; do not change

# OSM relation used by pylovo-setup (see docs); Bavaria --> 2145268
RELATION_ID = 2145268
SHIPPED_EPSG = 25832  # CRS of the processed transformer GeoJSON of Bavaria that ships with pylovo

# Filter thresholds of process_trafos()
AREA_THRESHOLD = 60  # m²; larger substation polygons are HV/MV substations
MIN_DISTANCE_BETWEEN_TRAFOS = 8  # m; of two closer transformers only the later one is kept
VOLTAGE_THRESHOLD = 110000  # V; transformers tagged with this voltage or more are removed


def _transformer_data_dir() -> Path:
    """Return the ``transformer_data`` folder of the data directory."""
    return get_user_data_dir() / "transformer_data"


def _overpass_query_path(name: str) -> Path:
    """Return the Overpass query template ``<name>_query.txt``: in the data directory, else the copy in the wheel."""
    relative = Path("transformer_data", "overpass_queries", f"{name}_query.txt")
    return reference_data_path(_transformer_data_dir() / "overpass_queries" / f"{name}_query.txt", relative)


def get_substations_geojson_path(relation_id: int) -> str:
    """Return the path of the raw substation/transformer GeoJSON of an OSM relation."""
    return str(_transformer_data_dir() / "fetched_trafos" / f"{relation_id}_substations.geojson")


def get_shopping_mall_geojson_path(relation_id: int) -> str:
    """Return the path of the raw shopping-mall GeoJSON of an OSM relation."""
    return str(_transformer_data_dir() / "fetched_trafos" / f"{relation_id}_shopping_mall.geojson")


def get_trafos_processed_target_geojson_path(relation_id: int) -> str:
    """Return the path of the processed transformer GeoJSON (in ``TARGET_EPSG``) of an OSM relation."""
    return str(_transformer_data_dir() / "processed_trafos" / f"{relation_id}_trafos_processed_{TARGET_EPSG}.geojson")


def _processed_trafos_file(relation_id: int, epsg: int) -> Path:
    """Return the processed transformer GeoJSON in ``epsg``: in the data directory, else the copy in the wheel."""
    name = f"{relation_id}_trafos_processed_{epsg}.geojson"
    return reference_data_path(_transformer_data_dir() / "processed_trafos" / name,
                               Path("transformer_data", "processed_trafos", name))


def reproject_trafos(source: Path, target: Path) -> None:
    """Write the processed transformer GeoJSON ``source`` (``SHIPPED_EPSG``) reprojected to ``TARGET_EPSG`` as ``target``.

    Args:
        source: Processed GeoJSON in ``SHIPPED_EPSG`` (the CRS in its file name counts).
        target: File to write; missing folders are created.
    """
    gdf = gpd.read_file(source).set_crs(SHIPPED_EPSG, allow_override=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_crs(epsg=TARGET_EPSG).to_file(target, driver="GeoJSON")


def get_processed_trafos(relation_id: int) -> str:
    """Return the processed transformer GeoJSON of an OSM relation in ``TARGET_EPSG``, creating it if there is none.

    1. The file for ``TARGET_EPSG`` is used as it is: in the data directory, and for 25832 also the copy in the wheel.
    2. Otherwise the processed 25832 file that ships with pylovo (Bavaria) is reprojected to ``TARGET_EPSG`` and
       written to the data directory: offline and fast.
    3. Only if there is no such file either (another relation), the transformers are fetched from the Overpass API
       and processed, which takes 30 to 50 minutes for a federal state.

    Delete the file of ``TARGET_EPSG`` in the data directory to create it again.

    Args:
        relation_id: OSM relation id of the area (``RELATION_ID`` for ``pylovo-setup``).

    Returns:
        Path of the processed GeoJSON in ``TARGET_EPSG``.
    """
    processed = _processed_trafos_file(relation_id, TARGET_EPSG)
    if processed.is_file():
        return str(processed)

    processed = Path(get_trafos_processed_target_geojson_path(relation_id))
    shipped = _processed_trafos_file(relation_id, SHIPPED_EPSG)
    if shipped.is_file():
        print(f"{processed} does not exist -> reproject {shipped} to EPSG:{TARGET_EPSG}")
        reproject_trafos(shipped, processed)
    else:
        print(f"{processed} does not exist -> fetch transformer data from the Overpass API and process it")
        fetch_trafos(relation_id)
        process_trafos(relation_id)
    return str(processed)


def fetch_trafos(relation_id: int) -> None:
    """Query the Overpass API for transformers and shopping malls inside an OSM relation.

    Deutsche Bahn and historic/abandoned substations are already excluded by the query. The results
    are written to :func:`get_substations_geojson_path` and :func:`get_shopping_mall_geojson_path`.

    Args:
        relation_id: OSM relation id of the area.
    """
    with open(_overpass_query_path("substations"), "r") as f:
        overpass_query_substations = f.read()
    with open(_overpass_query_path("shopping_mall"), "r") as f:
        overpass_query_mall = f.read()

    area_id = str(RELATION_ID_BASE + relation_id)
    overpass_query_substations = overpass_query_substations.replace("$relation_id$", area_id)
    overpass_query_mall = overpass_query_mall.replace("$relation_id$", area_id)

    geojson_substations = query_overpass_for_geojson(OVERPASS_URL, overpass_query_substations)
    geojson_mall = query_overpass_for_geojson(OVERPASS_URL, overpass_query_mall)

    substations_path = get_substations_geojson_path(relation_id)
    os.makedirs(os.path.dirname(substations_path), exist_ok=True)
    with open(substations_path, "w") as f:
        json.dump(geojson_substations, f, indent=2)
    with open(get_shopping_mall_geojson_path(relation_id), "w") as f:
        json.dump(geojson_mall, f, indent=2)


def _print_count(step: str, gdf: gpd.GeoDataFrame) -> None:
    """Print the number of remaining transformers after a filter step."""
    print(f"{step}: {len(gdf)} transformers")


def process_trafos(relation_id: int) -> None:
    """Filter the fetched OSM substations to LV transformers and write the processed GeoJSON.

    Filter steps (distances and areas in ``TARGET_EPSG``):

    1. drop points that lie inside a substation polygon,
    2. drop substations with an area of ``AREA_THRESHOLD`` m² or more,
    3. drop transformers tagged with ``VOLTAGE_THRESHOLD`` V or more,
    4. of transformers closer than ``MIN_DISTANCE_BETWEEN_TRAFOS`` m keep only the last one,
    5. drop transformers inside shopping malls.

    The centroid of each remaining geometry becomes its point geometry; ``osm_id`` is ``<type>/<id>``.

    Args:
        relation_id: OSM relation id whose fetched files are processed (see :func:`fetch_trafos`).
    """
    gdf_substations = gpd.read_file(get_substations_geojson_path(relation_id))
    _print_count("Fetched", gdf_substations)

    # The GeoJSON is in WGS84 (EPSG:4326); areas and distances need a projected CRS.
    gdf_substations = gdf_substations.to_crs(epsg=TARGET_EPSG)

    # 1. points inside substation polygons describe the same station
    gdf_substations['geom_type'] = gdf_substations.geom_type
    gdf_points = gdf_substations[gdf_substations['geom_type'] == 'Point']
    gdf_polygon = gdf_substations[gdf_substations['geom_type'] == 'Polygon']
    if not gdf_polygon.empty:
        union_of_polygons = gdf_polygon.geometry.union_all()
        gdf_substations = gdf_substations.drop(gdf_points[gdf_points.within(union_of_polygons)].index)
    _print_count("After step 1 (points inside polygons)", gdf_substations)

    # 2. large substation areas (Umspannwerke) are not LV transformers
    gdf_substations['area'] = gdf_substations.area
    gdf_substations = gdf_substations.drop(gdf_substations[gdf_substations['area'] >= AREA_THRESHOLD].index)
    _print_count("After step 2 (area)", gdf_substations)

    # 3. high-voltage transformers; values that are not numbers (e.g. "20000;400") are kept
    if 'voltage' in gdf_substations.columns:
        gdf_substations['voltage'] = (
            gdf_substations['voltage'].fillna(1).apply(lambda x: pd.to_numeric(x, errors='coerce')))
        gdf_substations['voltage'] = gdf_substations['voltage'].astype(float)
        gdf_substations = gdf_substations.drop(
            gdf_substations[gdf_substations['voltage'] >= VOLTAGE_THRESHOLD].index)
    _print_count("After step 3 (voltage)", gdf_substations)
    if gdf_substations.empty:
        raise ValueError(f"No transformer is left in relation {relation_id} after the filters of steps 1 to 3")

    # 4. transformers closer than MIN_DISTANCE_BETWEEN_TRAFOS: drop every row that has a close
    #    neighbour further down the table (upper triangle of the distance matrix)
    gdf_substations['centroid'] = gdf_substations.centroid
    distance_matrix = gdf_substations['centroid'].apply(lambda c: gdf_substations['centroid'].distance(c))
    distance_matrix = distance_matrix.where(np.triu(np.ones(distance_matrix.shape)).astype(bool))
    np.fill_diagonal(distance_matrix.values, float('nan'))
    distance_matrix = distance_matrix[(distance_matrix < MIN_DISTANCE_BETWEEN_TRAFOS).any(axis=1)]
    gdf_substations = gdf_substations.drop(index=list(distance_matrix.index))
    _print_count("After step 4 (close duplicates)", gdf_substations)

    # 5. transformers inside shopping malls
    gdf_shopping = gpd.read_file(get_shopping_mall_geojson_path(relation_id))
    if gdf_shopping.empty:
        gdf_substations['within_shopping'] = False
    else:
        union_of_shopping = gdf_shopping.to_crs(epsg=TARGET_EPSG).geometry.union_all()
        gdf_substations['within_shopping'] = gdf_substations.within(union_of_shopping)
    gdf_substations = gdf_substations.drop(gdf_substations[gdf_substations['within_shopping']].index)
    _print_count("After step 5 (shopping malls)", gdf_substations)

    # Use the centroid as the only geometry and drop the tag columns (every column with gaps).
    gdf_substations = gdf_substations.drop('geometry', axis=1)
    gdf_substations = gdf_substations.rename(columns={"centroid": "geometry"}).set_geometry("geometry")
    gdf_substations = gdf_substations.dropna(axis='columns')

    # osm_id as used for buildings: "<node|way|relation>/<id>"
    gdf_substations['id'] = gdf_substations.apply(lambda row: f"{row['type']}/{row['id']}", axis=1)
    gdf_substations = gdf_substations.rename(columns={"id": "osm_id"})
    if "@id" in gdf_substations:
        gdf_substations = gdf_substations.drop('@id', axis=1)

    processed_path = Path(get_trafos_processed_target_geojson_path(relation_id))
    processed_path.parent.mkdir(parents=True, exist_ok=True)

    gdf_substations.to_file(processed_path, driver='GeoJSON')
